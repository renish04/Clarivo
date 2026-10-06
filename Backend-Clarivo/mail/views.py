"""
Endpoints for connecting, inspecting and disconnecting a Gmail account.

All four are one-off operations with no CRUD semantics — an OAuth
handshake is not a resource being listed or updated — so they are plain
``APIView``s rather than generics, matching the convention the rest of
the backend uses for endpoints of this shape.
"""

import base64
import hmac
import json
import logging
from datetime import datetime, timezone

import boto3
import requests
from django.conf import settings
from django.db import IntegrityError
from django.shortcuts import redirect
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.dynamo import get_document, list_documents
from projects.models import Project

from .background import trigger_sync
from .gmail_client import (
    GmailReconnectRequired,
    ensure_watch,
    start_watch,
    stop_watch,
)
from .followups import (
    DraftGenerationError,
    GmailAccountRequired,
    OPEN_STATUSES,
    followup_state,
    generate_draft,
    is_sent,
    send_followup,
)
from .storage import (
    DRAFT_FIELDS,
    get_draft,
    get_thread_link,
    list_drafts,
    list_email_records,
    update_draft,
)
from .sync import sync_mailbox

# How much of the latest message body goes into a thread's preview.
SNIPPET_LENGTH = 140
from .models import GmailAccount, OAuthState
from .oauth import (
    REVOKE_URI,
    STATE_MAX_AGE,
    build_flow,
    frontend_redirect_url,
    new_state,
    safe_return_to,
    state_is_expired,
)

logger = logging.getLogger(__name__)


class GmailOAuthStartView(APIView):
    """
    GET /api/gmail/oauth/start/?return_to=<path>

    Returns the Google consent URL the browser should be sent to.  The
    frontend performs the navigation itself rather than this endpoint
    returning a 302, because the call is made with an Authorization
    header and a redirect chain would drop it.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return_to = safe_return_to(request.query_params.get("return_to", ""))

        state = new_state()
        OAuthState.objects.create(
            state=state,
            user=request.user,
            return_to=return_to,
        )

        # Clear out handshakes this user abandoned.  Each /start/ click
        # writes a row, and only the redeemed one is deleted by the
        # callback, so without this the table grows every time someone
        # opens the consent screen and changes their mind.
        OAuthState.objects.filter(
            user=request.user,
            created_at__lt=datetime.now(timezone.utc) - STATE_MAX_AGE,
        ).delete()

        flow = build_flow()
        auth_url, _ = flow.authorization_url(
            # Required to be issued a refresh token at all.
            access_type="offline",
            # Google only returns a refresh token on the *first* consent
            # for a given client/user pair.  Forcing the consent screen
            # every time guarantees one comes back — without this, a user
            # who reconnects gets tokens with no refresh token, and the
            # account would be unable to sync the moment the access token
            # expired an hour later.
            prompt="consent",
            include_granted_scopes="true",
            # Our own state, so the callback can resolve it to a user.
            # Omitting this would let the library generate its own, which
            # we would have no record of.
            state=state,
        )

        return Response({"auth_url": auth_url}, status=status.HTTP_200_OK)


class GmailOAuthCallbackView(APIView):
    """
    GET /api/gmail/oauth/callback/

    Google redirects the *browser* here after the consent screen.  That
    is a plain top-level navigation: no Authorization header, so no DRF
    token and no ``request.user``.  Authentication is therefore disabled
    on this view, and the user's identity comes solely from the stored
    ``OAuthState`` that the ``state`` parameter resolves to.

    Nothing in the URL is trusted as an identity claim.  A ``user_id``
    query parameter, if anyone ever appends one, is ignored entirely.

    Always ends in a 302 back to the frontend — a Django traceback page
    would be a dead end for someone who is mid-flow in their browser.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        state_value = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        google_error = request.query_params.get("error", "")

        # The state has to be resolved before anything else, because it is
        # the only thing that tells us where to send the browser back to.
        oauth_state = OAuthState.objects.filter(state=state_value).first()

        if oauth_state is None:
            # Unknown, already-used, or forged state.  There is no
            # return_to to honour, so fall back to the frontend root.
            logger.warning("Gmail OAuth callback with unrecognised state")
            return redirect(frontend_redirect_url("", "error"))

        user = oauth_state.user
        return_to = oauth_state.return_to
        expired = state_is_expired(oauth_state)

        # Single use: delete it now, before the exchange, so that a
        # replayed or double-submitted callback cannot redeem the same
        # state twice even if what follows takes a while.
        oauth_state.delete()

        if expired:
            logger.warning("Gmail OAuth callback with expired state for %s", user)
            return redirect(frontend_redirect_url(return_to, "error"))

        if google_error or not code:
            # The user pressed Cancel on the consent screen, or Google
            # declined for some other reason.
            logger.info(
                "Gmail OAuth consent not granted for %s (%s)",
                user,
                google_error or "no code returned",
            )
            return redirect(frontend_redirect_url(return_to, "error"))

        try:
            account = self._complete_connection(user, code)
        except Exception:
            logger.exception("Gmail OAuth connection failed for %s", user)
            return redirect(frontend_redirect_url(return_to, "error"))

        # Registering the push watch is deliberately outside the block
        # above.  By this point the mailbox *is* connected and stored, and
        # a Pub/Sub misconfiguration should not report that as a failed
        # connection and invite the user to click Connect again.  The next
        # /status/ call retries it through ensure_watch, and until it
        # succeeds watch_expires_at stays null for the UI to act on.
        try:
            start_watch(account)
        except Exception:
            logger.exception(
                "Gmail account %s connected, but starting the push watch failed",
                account.email,
            )

        return redirect(frontend_redirect_url(return_to, "connected"))

    def _complete_connection(self, user, code):
        """Exchange *code* for tokens and persist the account."""
        flow = build_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials

        if not creds.refresh_token:
            # Without a refresh token the connection is useless: it would
            # work for one hour and then be unable to sync again. Better
            # to fail the connect than to store a mailbox that silently
            # stops working.
            raise ValueError("Google returned no refresh token")

        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()

        email = profile["emailAddress"]
        # A string holding a uint64, per the Profile schema.
        profile_history_id = str(profile.get("historyId", ""))

        # ``email`` is unique across the table, because a push
        # notification identifies a mailbox by address alone.  Catching
        # this explicitly turns what would be an IntegrityError into a
        # clean failure, rather than one user's connect attempt
        # overwriting another user's stored credentials.
        clash = (
            GmailAccount.objects.filter(email=email).exclude(user=user).exists()
        )
        if clash:
            raise ValueError(
                f"{email} is already connected to a different Clarivo account"
            )

        try:
            account, _ = GmailAccount.objects.get_or_create(
                user=user,
                defaults={"email": email},
            )
        except IntegrityError as exc:
            raise ValueError(f"Could not store the Gmail account: {exc}") from exc

        account.email = email
        account.refresh_token = creds.refresh_token
        account.access_token = creds.token or ""
        # google-auth hands back a naive UTC expiry; Django stores aware.
        account.token_expiry = (
            creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
        )
        # The profile's historyId is the cursor to start syncing from.
        # Unlike in start_watch this *is* overwritten on a reconnect: a
        # stale cursor from before the account lapsed may be too old for
        # Gmail to serve history for at all, and the point of reconnecting
        # is to resume from now.
        if profile_history_id:
            account.history_id = profile_history_id
        account.needs_reconnect = False
        account.save()

        logger.info("Gmail account %s connected for %s", email, user)
        return account


class GmailStatusView(APIView):
    """
    GET /api/gmail/status/

    Reports whether the current user has a Gmail account connected, and
    opportunistically renews the push watch while it is here.  The
    frontend calls this on load, which is what keeps the watch alive
    without a scheduled job.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = GmailAccount.objects.filter(user=request.user).first()

        if account is None:
            return Response({"connected": False}, status=status.HTTP_200_OK)

        try:
            ensure_watch(account)
        except GmailReconnectRequired:
            # ensure_watch has already flagged the account, and
            # needs_reconnect is part of the response below — which is
            # exactly what the client needs to know.
            pass
        except Exception:
            # A watch renewal problem must not take down the status
            # endpoint; the UI depends on it to render at all.
            logger.exception("Could not ensure Gmail watch for %s", account.email)

        return Response(
            {
                "connected": True,
                "email": account.email,
                "needs_reconnect": account.needs_reconnect,
                "push_enabled": settings.GMAIL_PUSH_ENABLED,
                "watch_expires_at": account.watch_expiration,
                "last_synced_at": account.last_synced_at,
            },
            status=status.HTTP_200_OK,
        )


class GmailPushView(APIView):
    """
    POST /api/gmail/push/?token=<GMAIL_PUSH_SECRET>

    The Pub/Sub push endpoint.  Google delivers a notification carrying
    only ``{emailAddress, historyId}`` — a "something changed" signal,
    not the change itself — so this does no work beyond identifying the
    mailbox and handing off to a background worker.

    It must answer within a few seconds.  Pub/Sub treats a slow or
    non-2xx response as a failed delivery and redelivers, so running the
    sync inline would turn one slow mailbox into a redelivery storm.

    Unauthenticated by necessity: Pub/Sub sends no DRF token.  Setting
    ``authentication_classes = []`` also means DRF enforces no CSRF check,
    which is what allows a POST from outside the browser at all.  The
    shared secret in the query string is the authentication.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = settings.GMAIL_PUSH_SECRET
        if not secret:
            # With no secret configured there is nothing to verify, and
            # compare_digest("", "") would happily accept any caller.
            logger.error("GMAIL_PUSH_SECRET is not set — refusing push delivery")
            return Response(
                {"detail": "Push endpoint is not configured."},
                status=status.HTTP_403_FORBIDDEN,
            )

        token = request.query_params.get("token", "")
        # Constant-time, so a wrong token cannot be guessed a character
        # at a time by timing the responses.
        if not hmac.compare_digest(token, secret):
            logger.warning("Gmail push delivery rejected: bad token")
            return Response(
                {"detail": "Invalid token."},
                status=status.HTTP_403_FORBIDDEN,
            )

        email_address = self._email_from_envelope(request.data)
        if not email_address:
            # Malformed envelope.  Answered 204 on purpose: it will not
            # parse any better on the fifth delivery, and anything other
            # than a 2xx makes Pub/Sub retry it indefinitely.
            return Response(status=status.HTTP_204_NO_CONTENT)

        account = GmailAccount.objects.filter(email__iexact=email_address).first()
        if account is None:
            # A mailbox we no longer hold — most likely disconnected
            # while a watch was still live.  Acknowledged rather than
            # errored, for the same reason as above.
            logger.info(
                "Gmail push for unknown mailbox %s — acknowledging", email_address
            )
            return Response(status=status.HTTP_204_NO_CONTENT)

        trigger_sync(account.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _email_from_envelope(data):
        """Pull emailAddress out of the Pub/Sub push envelope.

        The real payload is base64 inside ``message.data``; the
        ``historyId`` it also carries is deliberately ignored, because
        ``sync_mailbox`` always works from its own stored cursor.  That is
        what makes a duplicated or out-of-order notification harmless.
        """
        try:
            encoded = (data or {}).get("message", {}).get("data", "")
            if not encoded:
                logger.warning("Gmail push envelope had no message.data")
                return ""

            # Standard base64 with padding, per the Pub/Sub push format.
            payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
            email_address = (payload.get("emailAddress") or "").strip()

            if not email_address:
                logger.warning("Gmail push payload had no emailAddress")
            return email_address
        except Exception:
            logger.exception("Could not decode the Gmail push envelope")
            return ""


class GmailSyncNowView(APIView):
    """
    POST /api/gmail/sync/

    The "Sync now" button, and the endpoint the frontend polls every 60
    seconds when push is disabled.

    The sync itself runs inline so the response can report what was
    actually found — a background-only version would have to answer
    "started" and leave the UI guessing.  The slow part that follows
    (waiting on the extraction Lambda, embedding, classification,
    detection) is handed to the background worker.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account = GmailAccount.objects.filter(user=request.user).first()
        if account is None:
            return Response(
                {"detail": "No Gmail account is connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            summary = sync_mailbox(account)
        except GmailReconnectRequired:
            # 409 rather than 401: the request was perfectly valid, it is
            # the stored Google grant that is no longer usable.
            return Response(
                {"needs_reconnect": True},
                status=status.HTTP_409_CONFLICT,
            )

        # Hand off the follow-up work, seeding it with the projects this
        # sync just touched — the worker's own sync will find nothing new,
        # so without the seed it would have nothing to process.
        trigger_sync(account.pk, project_ids=summary.get("project_ids"))

        return Response(summary, status=status.HTTP_200_OK)


class GmailDisconnectView(APIView):
    """
    POST /api/gmail/disconnect/

    Stops the push watch, revokes the grant at Google, and deletes the
    stored connection.

    Emails and documents already ingested are deliberately left alone.
    They stopped being mailbox data the moment they were routed to a
    project; they are project evidence now, and discarding them would
    silently delete findings that reference them.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account = GmailAccount.objects.filter(user=request.user).first()

        if account is None:
            # Already disconnected.  Reporting success keeps the button
            # idempotent — a double click is not an error.
            return Response(status=status.HTTP_204_NO_CONTENT)

        # Best effort, in this order: stop the push first so Gmail is not
        # still publishing notifications for a mailbox we are about to
        # forget, then revoke, then delete.
        stop_watch(account)
        self._revoke(account)

        email = account.email
        account.delete()
        logger.info("Gmail account %s disconnected for %s", email, request.user)

        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _revoke(account):
        """Invalidate the grant at Google, ignoring failures.

        The refresh token is the one worth revoking: it is the durable
        credential, and revoking it invalidates the whole grant rather
        than just the current hour's access token.

        Failures are swallowed on purpose.  Google answers with a 400 for
        a token that is already invalid — which is precisely the case when
        the user revoked access from their Google account page, the very
        situation that makes someone come here and press Disconnect.
        Either way the local record is being deleted.
        """
        try:
            token = account.refresh_token
        except Exception:
            logger.warning(
                "Could not read the stored refresh token for %s; skipping revoke",
                account.email,
                exc_info=True,
            )
            return

        if not token:
            return

        try:
            response = requests.post(
                REVOKE_URI,
                data={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(
                    "Token revoke for %s returned HTTP %s: %s",
                    account.email,
                    response.status_code,
                    response.text[:200],
                )
        except requests.RequestException:
            logger.warning(
                "Token revoke request failed for %s (continuing anyway)",
                account.email,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Reading ingested mail
# ---------------------------------------------------------------------------

class _ProjectMailView(APIView):
    """Shared ownership check for the project-scoped mail endpoints.

    An email is routed into a project, so a project's mail is readable by
    exactly the person who owns that project and nobody else.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get_project(self, request, project_id):
        return get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

    def thread_link(self, request, thread_id):
        """The thread's link row, or None.

        Looked up through the requesting user's own connected mailbox,
        because a Gmail thread id is only unique within one mailbox.  A
        user who has since disconnected keeps their ingested mail — it is
        project data now — so the absence of an account is normal and
        simply means no link can be resolved.
        """
        account = GmailAccount.objects.filter(user=request.user).first()
        if account is None or not thread_id:
            return None
        return get_thread_link(account.email, thread_id)


def _sorted_oldest_first(records):
    """Order a thread's messages chronologically.

    ISO-8601 timestamps sort lexicographically in the same order as the
    instants they represent, so a plain string sort is correct here.
    Records with no received_at sort first rather than crashing the sort.
    """
    return sorted(records, key=lambda record: record.get("received_at") or "")


class MailThreadListView(_ProjectMailView):
    """
    GET /api/projects/<project_id>/mail/threads/

    One entry per conversation, most recently active first — the inbox
    list for a project.
    """

    def get(self, request, project_id):
        project = self.get_project(request, project_id)

        threads = {}
        for record in list_email_records(project.pk):
            # A message with no thread id becomes a thread of its own
            # rather than being lumped in with every other such message.
            key = record.get("thread_id") or f"msg:{record.get('gmail_message_id', '')}"
            threads.setdefault(key, []).append(record)

        entries = []
        for key, records in threads.items():
            ordered = _sorted_oldest_first(records)
            first, latest = ordered[0], ordered[-1]

            # The routing reason belongs to the message that pulled the
            # thread into this project, which is the first inbound one —
            # an outbound follow-up Clarivo sent has no routing reason.
            first_inbound = next(
                (r for r in ordered if r.get("direction") == "inbound"),
                None,
            )

            # Unique senders in the order they first appear, so the list
            # reads chronologically rather than arbitrarily.
            participants = list(
                dict.fromkeys(
                    r["from_email"] for r in ordered if r.get("from_email")
                )
            )

            thread_id = first.get("thread_id") or ""
            link = self.thread_link(request, thread_id)

            entries.append({
                "thread_id": thread_id,
                "subject": first.get("subject", ""),
                "participants": participants,
                "last_message_at": latest.get("received_at", ""),
                "message_count": len(ordered),
                "has_attachments": any(
                    r.get("attachment_doc_ids") for r in ordered
                ),
                "route_reason": (
                    first_inbound.get("route_reason", "") if first_inbound else ""
                ),
                "snippet": (latest.get("body_text") or "")[:SNIPPET_LENGTH],
                "linked_invoice_doc_id": (
                    link.get("invoice_doc_id") if link else None
                ) or None,
            })

        entries.sort(key=lambda entry: entry["last_message_at"] or "", reverse=True)
        return Response(entries, status=status.HTTP_200_OK)


class MailThreadDetailView(_ProjectMailView):
    """
    GET /api/projects/<project_id>/mail/threads/<thread_id>/

    Every message in one conversation, oldest first, with each message's
    attachments resolved to viewable documents.
    """

    def get(self, request, project_id, thread_id):
        project = self.get_project(request, project_id)

        records = [
            record
            for record in list_email_records(project.pk)
            if (record.get("thread_id") or "") == thread_id
        ]

        if not records:
            return Response(
                {"detail": "No such thread in this project."},
                status=status.HTTP_404_NOT_FOUND,
            )

        s3_client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )

        messages = []
        for record in _sorted_oldest_first(records):
            messages.append({
                "gmail_message_id": record.get("gmail_message_id", ""),
                "direction": record.get("direction", "inbound"),
                "from_name": record.get("from_name", ""),
                "from_email": record.get("from_email", ""),
                "to": record.get("to", ""),
                "subject": record.get("subject", ""),
                "received_at": record.get("received_at", ""),
                "body_text": record.get("body_text", ""),
                "attachments": self._attachments(
                    s3_client, project.pk, record.get("attachment_doc_ids") or []
                ),
            })

        return Response(messages, status=status.HTTP_200_OK)

    @staticmethod
    def _attachments(s3_client, project_id, doc_ids):
        """Resolve attachment doc ids to viewable document summaries.

        ``status`` is included so the UI can show that a document is
        still being read rather than rendering it as though it were ready.
        """
        from documents.dynamo import get_document

        attachments = []
        for doc_id in doc_ids:
            doc = get_document(project_id, doc_id)
            if doc is None:
                # The document was deleted, or never finished being
                # written.  Reported rather than hidden, so the message
                # does not silently claim fewer attachments than it had.
                attachments.append({
                    "doc_id": doc_id,
                    "filename": "",
                    "status": "missing",
                    "view_url": None,
                })
                continue

            # Guarded for the same reason as the documents list: a record
            # without an s3_key has no object to presign.
            s3_key = doc.get("s3_key")
            attachments.append({
                "doc_id": doc_id,
                "filename": doc.get("filename", ""),
                "status": doc.get("status", ""),
                "view_url": (
                    s3_client.generate_presigned_url(
                        "get_object",
                        Params={
                            "Bucket": settings.S3_BUCKET_NAME,
                            "Key": s3_key,
                        },
                        ExpiresIn=600,  # 10 minutes
                    )
                    if s3_key
                    else None
                ),
            })

        return attachments


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

# The order the follow-up list is presented in: what needs a person's
# attention first, what is waiting on somebody else after that, and what
# is finished last.  Deliberately not alphabetical and not chronological
# — the point of the list is to be worked down from the top.
_FOLLOWUP_STATE_ORDER = {
    state: rank
    for rank, state in enumerate(
        [
            "needs_draft",
            "reply_received",
            "draft_stale",
            "draft_ready",
            "awaiting_reply",
            "resolved",
            "not_needed",
        ]
    )
}

# What a PATCH is allowed to change.  Everything else on a draft is
# derived, or is the record of a send, and neither is the composer's to
# rewrite.
_EDITABLE_DRAFT_FIELDS = ("to", "subject", "body")


def _draft_payload(draft, state):
    """Serialise a draft item for the API.

    Built from DRAFT_FIELDS rather than by copying the item, so the
    single-table ``PK``/``SK`` never leak into a response, and a field
    added to storage has to be added here consciously.  ``round`` is cast
    because DynamoDB hands numbers back as Decimal.
    """
    payload = {field: draft.get(field) for field in DRAFT_FIELDS}
    payload["round"] = int(draft.get("round") or 1)
    payload["excluded_findings"] = list(draft.get("excluded_findings") or [])
    payload["edited"] = bool(draft.get("edited"))
    payload["followup_state"] = state
    return payload


class _FollowupView(_ProjectMailView):
    """Shared loading for the per-invoice follow-up endpoints.

    Ownership comes from ``_ProjectMailView``: a follow-up is about an
    invoice in a project, so it is the project owner's and nobody else's.
    """

    def get_invoice(self, project, doc_id):
        """The invoice document, or None."""
        return get_document(project.pk, doc_id)

    def load(self, request, project_id, doc_id):
        """Return ``(project, invoice, draft)``, or a 404 Response.

        Both a missing document and a missing draft are 404s on the same
        URL, so the detail message is what tells them apart.
        """
        project = self.get_project(request, project_id)

        invoice = self.get_invoice(project, doc_id)
        if not invoice:
            return None, Response(
                {"detail": "No such document in this project."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return (project, invoice, get_draft(project.pk, doc_id)), None


class FollowupListView(_ProjectMailView):
    """
    GET /api/projects/<project_id>/followups/

    Every invoice with a follow-up to make or made: the open cases, plus
    anything that already has a draft — including drafts whose invoice has
    since come good, which are the ones a user wants to see have worked.
    """

    def get(self, request, project_id):
        project = self.get_project(request, project_id)

        drafts = {
            draft.get("invoice_doc_id") or draft.get("SK", "").replace("DRAFT#", ""): draft
            for draft in list_drafts(project.pk)
        }

        entries = []
        for document in list_documents(project.pk):
            doc_id = document.get("SK", "").replace("DOC#", "")
            if not doc_id:
                continue

            draft = drafts.get(doc_id)
            discrepancy_status = document.get("discrepancy_status") or ""

            # A draft earns an invoice its place in the list whatever its
            # verdict now is; without that, a case that resolved after a
            # follow-up went out would vanish the moment it worked.
            if discrepancy_status not in OPEN_STATUSES and not draft:
                continue

            entries.append({
                "invoice_doc_id": doc_id,
                "invoice_filename": document.get("filename") or "",
                "supplier": document.get("supplier") or "",
                "discrepancy_status": discrepancy_status,
                "followup_state": followup_state(document, draft),
                # Null rather than 1 when there is no draft: no round of
                # this conversation has happened yet.
                "round": int(draft.get("round") or 1) if draft else None,
                "to": (draft.get("to") or "") if draft else "",
                "sent_at": (draft.get("sent_at") or "") if draft else "",
            })

        # Filename breaks ties so the list does not reshuffle between two
        # reads that found the same work outstanding.
        entries.sort(
            key=lambda entry: (
                _FOLLOWUP_STATE_ORDER.get(entry["followup_state"], len(_FOLLOWUP_STATE_ORDER)),
                (entry["invoice_filename"] or "").lower(),
            )
        )

        return Response(entries, status=status.HTTP_200_OK)


class FollowupDraftView(_FollowupView):
    """
    GET    /api/projects/<project_id>/followups/<doc_id>/draft/
    POST   /api/projects/<project_id>/followups/<doc_id>/draft/
    PATCH  /api/projects/<project_id>/followups/<doc_id>/draft/

    Read the draft, (re)generate it, or save the user's edits to it.
    """

    def get(self, request, project_id, doc_id):
        loaded, error = self.load(request, project_id, doc_id)
        if error:
            return error

        _, invoice, draft = loaded
        if not draft:
            return Response(
                {"detail": "No follow-up draft for this document."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            _draft_payload(draft, followup_state(invoice, draft)),
            status=status.HTTP_200_OK,
        )

    def post(self, request, project_id, doc_id):
        project = self.get_project(request, project_id)
        force = bool((request.data or {}).get("force", False))

        try:
            draft = generate_draft(project.pk, doc_id, request.user, force=force)
        except ValueError as exc:
            # The invoice is missing, or needs no follow-up. Both are the
            # client asking for something that does not apply, not a
            # failure on this side.
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DraftGenerationError as exc:
            # 502: the request was fine, the model's answer was not.
            # Retrying is the right response, which 400 would not suggest.
            logger.warning("Follow-up generation failed for %s: %s", doc_id, exc)
            return Response(
                {"detail": "The follow-up could not be drafted. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        invoice = get_document(project.pk, doc_id)
        return Response(
            _draft_payload(draft, followup_state(invoice or {}, draft)),
            status=status.HTTP_200_OK,
        )

    def patch(self, request, project_id, doc_id):
        loaded, error = self.load(request, project_id, doc_id)
        if error:
            return error

        project, invoice, draft = loaded
        if not draft:
            return Response(
                {"detail": "No follow-up draft for this document."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if is_sent(draft):
            # 409 rather than 403: editing a draft is allowed, editing
            # this one is not, because it is already with the supplier.
            return Response(
                {"detail": "This follow-up has already been sent and cannot be edited."},
                status=status.HTTP_409_CONFLICT,
            )

        data = request.data or {}
        fields = {}

        for field in _EDITABLE_DRAFT_FIELDS:
            if field not in data:
                continue
            value = data.get(field)
            fields[field] = ("" if value is None else str(value)).strip()

        if not fields:
            return Response(
                {
                    "detail": "Provide at least one of "
                    f"{', '.join(_EDITABLE_DRAFT_FIELDS)}."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if "to" in fields:
            fields["to"] = fields["to"].lower()
            if fields["to"] != (draft.get("to") or ""):
                # An address the user typed outranks anything Clarivo
                # worked out, and send_followup reads this to decide
                # whether to remember it as a contact.
                fields["to_source"] = "manual"

        # Marks the draft as the user's own work, which is what stops a
        # background regeneration from overwriting it later.
        fields["edited"] = True

        updated = update_draft(project.pk, doc_id, fields)
        if not updated:
            # Deleted between the read above and this write.
            return Response(
                {"detail": "No follow-up draft for this document."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            _draft_payload(updated, followup_state(invoice, updated)),
            status=status.HTTP_200_OK,
        )


class FollowupSendView(_FollowupView):
    """
    POST /api/projects/<project_id>/followups/<doc_id>/send/

    Send the follow-up, from the user's own connected Gmail.  Nothing
    else in Clarivo sends mail: this endpoint is the only path to it, and
    it is only ever reached because somebody pressed send.
    """

    def post(self, request, project_id, doc_id):
        loaded, error = self.load(request, project_id, doc_id)
        if error:
            return error

        project, _, draft = loaded
        if not draft:
            return Response(
                {"detail": "No follow-up draft for this document."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if is_sent(draft):
            # Checked here as well as inside send_followup, so that a
            # double-submitted form gets a 409 rather than a 400 — and so
            # the second press cannot reach Gmail at all.
            return Response(
                {"detail": "This follow-up has already been sent."},
                status=status.HTTP_409_CONFLICT,
            )

        data = request.data or {}

        try:
            sent = send_followup(
                project.pk,
                doc_id,
                request.user,
                to=data.get("to"),
                subject=data.get("subject"),
                body=data.get("body"),
            )
        except GmailAccountRequired as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except GmailReconnectRequired:
            # 409, as everywhere else here: the request was valid, the
            # stored Google grant is not.
            return Response(
                {"needs_reconnect": True},
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "sent": True,
                "gmail_thread_id": sent.get("gmail_thread_id") or "",
                "sent_at": sent.get("sent_at") or "",
            },
            status=status.HTTP_200_OK,
        )
