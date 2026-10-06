"""
Gmail API access for a connected account.

Everything in this module takes a ``GmailAccount`` and turns it into an
authorised Gmail service object, plus the watch registration that makes
Gmail push change notifications at us in the first place.

Two things here are easy to get wrong and are therefore handled
explicitly rather than left to chance:

* **Naive vs aware datetimes.**  ``google-auth`` deliberately works in
  *naive* UTC internally (its ``_helpers.utcnow()`` builds an aware
  datetime and then strips the tzinfo back off, for backward
  compatibility), while Django runs with ``USE_TZ = True`` and stores
  *aware* datetimes.  Handing an aware ``expiry`` to ``Credentials``
  makes its ``expired`` property raise ``TypeError`` on the comparison,
  so expiries are converted at both boundaries.

* **Watch expiry.**  A Gmail watch lasts at most 7 days and then simply
  stops publishing — no error, no notification that it lapsed.  Google's
  push guide says to call ``watch`` "at least once every 7 days" and
  recommends once per day, so ``ensure_watch`` renews well ahead of the
  deadline rather than waiting for it.
"""

import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Least privilege: read messages, and send the follow-up emails Clarivo
# drafts.  Notably *not* gmail.modify — Clarivo never alters the user's
# mailbox, so it never asks for the ability to.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Google's OAuth 2.0 token endpoint.  Credentials needs this in order to
# exchange the refresh token for a new access token.
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Renew a watch once it has less than this long left to run.
WATCH_RENEWAL_THRESHOLD = timedelta(hours=48)


class GmailReconnectRequired(Exception):
    """Raised when an account's refresh token can no longer be used.

    This is not a transient failure — retrying cannot fix it.  The user
    has to go through the OAuth consent screen again, so callers should
    stop working on this account and surface a reconnect prompt.
    """


def _to_aware(naive_utc):
    """Attach UTC to a naive datetime coming out of ``google-auth``."""
    if naive_utc is None:
        return None
    if naive_utc.tzinfo is None:
        return naive_utc.replace(tzinfo=timezone.utc)
    return naive_utc


def _to_naive_utc(aware):
    """Strip the timezone off an aware datetime, for ``google-auth``."""
    if aware is None:
        return None
    if aware.tzinfo is None:
        return aware
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def get_service(account):
    """Return an authorised Gmail API service for *account*.

    Refreshes the access token first if it is missing or expired, and
    persists the new token and expiry so the next call can reuse them
    instead of making another round trip to Google.

    Raises
    ------
    GmailReconnectRequired
        If the refresh token has been revoked or has expired.  The
        account is flagged ``needs_reconnect`` before this is raised.
    """
    creds = Credentials(
        token=account.access_token or None,
        refresh_token=account.refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
        expiry=_to_naive_utc(account.token_expiry),
    )

    # `valid` is False both when the cached token has expired and when
    # there is no cached token at all, which is the right trigger for
    # either case — both are fixed by the same refresh.
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError:
            # The grant is gone.  In a Google Cloud project still in
            # Testing mode this happens routinely: refresh tokens issued
            # by an unverified app expire after 7 days.  It also happens
            # whenever the user revokes access or changes their password.
            logger.warning(
                "Refresh token rejected for %s — reconnect required",
                account.email,
            )
            account.needs_reconnect = True
            account.save(update_fields=["needs_reconnect"])
            raise GmailReconnectRequired(
                f"Gmail access for {account.email} must be reconnected."
            )

        account.access_token = creds.token
        account.token_expiry = _to_aware(creds.expiry)
        account.save(update_fields=["access_token", "token_expiry"])
        logger.info("Refreshed Gmail access token for %s", account.email)

    # cache_discovery=False: the default on-disk discovery cache wants a
    # writable location and warns noisily when it cannot find one, and
    # the installed client library already ships a static Gmail
    # discovery document, so the cache buys nothing here.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def start_watch(account):
    """Register a Gmail push notification watch on the INBOX.

    Does nothing in polling mode: without ``GMAIL_PUSH_ENABLED`` there is
    no Pub/Sub topic to publish to and no publicly reachable webhook to
    deliver to, so asking Gmail to push would only fail.

    Returns the raw watch response, or ``None`` if push is disabled.
    """
    if not settings.GMAIL_PUSH_ENABLED:
        return None

    service = get_service(account)

    response = (
        service.users()
        .watch(
            userId="me",
            body={
                "topicName": settings.GMAIL_PUBSUB_TOPIC,
                "labelIds": ["INBOX"],
                # labelFilterBehavior replaces the deprecated
                # labelFilterAction, which the API reference notes
                # "caused incorrect behavior in some cases".  "include"
                # means notify us *only* about INBOX changes — so the
                # user's sent mail, drafts and label shuffling do not
                # each wake up a sync.
                "labelFilterBehavior": "include",
            },
        )
        .execute()
    )

    update_fields = []

    # historyId is only a useful starting point the *first* time.  On a
    # renewal the stored cursor is older, and overwriting it with the
    # current one would skip every change that arrived in between —
    # silently losing emails rather than failing visibly.
    if not account.history_id and response.get("historyId"):
        account.history_id = str(response["historyId"])
        update_fields.append("history_id")

    expiration = response.get("expiration")
    if expiration:
        # Sent as a string holding epoch milliseconds (int64), per the
        # WatchResponse schema.
        account.watch_expiration = datetime.fromtimestamp(
            int(expiration) / 1000,
            tz=timezone.utc,
        )
        update_fields.append("watch_expiration")

    if update_fields:
        account.save(update_fields=update_fields)

    logger.info(
        "Gmail watch registered for %s (expires %s)",
        account.email,
        account.watch_expiration,
    )
    return response


def ensure_watch(account):
    """Start or renew the watch if it is missing or close to expiring.

    Called on every sync, which is what keeps the watch alive without a
    scheduler: activity in the mailbox renews the registration that
    reports that activity.

    Returns the watch response if one was (re)registered, else ``None``.
    """
    if not settings.GMAIL_PUSH_ENABLED:
        return None

    expiry = account.watch_expiration
    if expiry and expiry - datetime.now(timezone.utc) > WATCH_RENEWAL_THRESHOLD:
        return None

    logger.info(
        "Gmail watch for %s needs %s",
        account.email,
        "renewing" if expiry else "registering",
    )
    return start_watch(account)


def stop_watch(account):
    """Tell Gmail to stop pushing notifications for this mailbox.

    Errors are swallowed deliberately.  This runs when a user
    disconnects their account, and by then the thing it is cleaning up
    may already be gone — the token revoked, the watch lapsed on its
    own.  None of that should be able to fail a disconnect, and an
    abandoned watch is harmless: it expires within 7 days, and the
    webhook ignores notifications for mailboxes it has no account for.
    """
    try:
        service = get_service(account)
        service.users().stop(userId="me").execute()
        logger.info("Gmail watch stopped for %s", account.email)
    except Exception:
        logger.warning(
            "Could not stop Gmail watch for %s (continuing anyway)",
            account.email,
            exc_info=True,
        )
