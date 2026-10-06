"""
Endpoints for connecting, inspecting and disconnecting a Gmail account.

All four are one-off operations with no CRUD semantics — an OAuth
handshake is not a resource being listed or updated — so they are plain
``APIView``s rather than generics, matching the convention the rest of
the backend uses for endpoints of this shape.
"""

import logging
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.db import IntegrityError
from django.shortcuts import redirect
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .gmail_client import (
    GmailReconnectRequired,
    ensure_watch,
    start_watch,
    stop_watch,
)
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
