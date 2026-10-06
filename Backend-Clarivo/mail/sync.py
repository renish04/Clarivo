"""
The one function that moves mail from Gmail into Clarivo.

Three triggers call ``sync_mailbox``: the Pub/Sub push webhook, the
"Sync now" endpoint, and the frontend's 60-second poll in non-push mode.
None of them pass anything about *what* changed, and the push
notification deliberately does not either — it carries only an email
address and a history id, and means no more than "something happened".
The real work is always the same history sync, which is why there is one
function rather than three.

That design is what makes duplicate and out-of-order delivery a
non-event.  Pub/Sub guarantees at-least-once delivery, so the same
notification can arrive twice, and a poll can overlap a push.  Every
expensive step downstream is guarded by the conditional write in
``put_email_record``, so a message processed twice is ingested once.

The history cursor is the state that makes this incremental: Gmail is
asked only for what changed since ``account.history_id``, and the cursor
advances at the end of each run.  Gmail keeps roughly a week of history,
so a cursor older than that is rejected with a 404 — the documented
response to which is a full sync, handled below.
"""

import logging
from datetime import datetime, timezone

from googleapiclient.errors import HttpError

from .gmail_client import ensure_watch, get_service
from .ingest import ingest_message

logger = logging.getLogger(__name__)

# Gmail allows up to 500 history records per page.
HISTORY_PAGE_SIZE = 500

# How far back the recovery sync looks when the history cursor has
# expired.  Gmail keeps about a week of history, so a cursor only goes
# stale if nothing has synced for days; three days of inbox is a
# generous overlap without re-reading months of mail.
FULL_SYNC_QUERY = "in:inbox newer_than:3d"


def sync_mailbox(account):
    """Fetch and ingest everything new in *account*'s inbox.

    Returns
    -------
    dict
        ``{"fetched": n, "ingested": n, "skipped": n, "project_ids": [...]}``
        — messages seen, messages that became project data, messages
        deliberately skipped or failed, and the projects touched.

    Raises
    ------
    GmailReconnectRequired
        Propagated from ``get_service`` when the refresh token is dead.
        Callers need to see this: it is the one failure no retry fixes,
        and the user has to be told to reconnect.
    """
    summary = {"fetched": 0, "ingested": 0, "skipped": 0, "project_ids": []}

    # Deliberately not caught — see the docstring.
    service = get_service(account)

    # Renew the push registration while we are here.  Doing it on every
    # sync is what keeps the watch alive without a scheduled job: a
    # mailbox that receives mail renews the subscription that reports it.
    try:
        ensure_watch(account)
    except Exception:
        # A watch problem must not stop mail that has already arrived
        # from being ingested.
        logger.exception("Could not ensure the Gmail watch for %s", account.email)

    # -- First sync: establish a baseline and stop ---------------------
    if not account.history_id:
        profile = service.users().getProfile(userId="me").execute()
        account.history_id = str(profile.get("historyId", ""))
        account.last_synced_at = datetime.now(timezone.utc)
        account.save(update_fields=["history_id", "last_synced_at"])
        logger.info(
            "Baseline history id %s stored for %s — nothing to sync yet",
            account.history_id,
            account.email,
        )
        return summary

    # -- Collect the message ids that changed -------------------------
    try:
        message_ids, new_history_id = _collect_from_history(
            service, account.history_id
        )
    except HttpError as error:
        if getattr(error.resp, "status", None) != 404:
            raise
        # The cursor is older than the history Gmail retains.  The
        # reference is explicit that the right response is a full sync.
        logger.warning(
            "History id %s is too old for %s — falling back to a full sync",
            account.history_id,
            account.email,
        )
        message_ids, new_history_id = _collect_from_full_sync(service)

    summary["fetched"] = len(message_ids)

    # -- Ingest, one message at a time --------------------------------
    project_ids = set()
    for message_id in message_ids:
        try:
            result = ingest_message(account, service, message_id)
        except Exception:
            # One malformed email, or one attachment Gmail will not hand
            # over, must not strand every message queued behind it.  The
            # history cursor still advances: a message that fails twice
            # will fail a third time, and blocking the mailbox on it
            # would be worse than losing it.
            logger.exception(
                "Ingestion failed for message %s in %s", message_id, account.email
            )
            summary["skipped"] += 1
            continue

        if result is None:
            # The user's own mail, an email no rule claimed, or one
            # already ingested.
            summary["skipped"] += 1
            continue

        summary["ingested"] += 1
        project_ids.add(result["project_id"])

    summary["project_ids"] = sorted(project_ids)

    # -- Advance the cursor -------------------------------------------
    update_fields = ["last_synced_at"]
    if new_history_id and new_history_id != account.history_id:
        account.history_id = new_history_id
        update_fields.append("history_id")

    account.last_synced_at = datetime.now(timezone.utc)
    account.save(update_fields=update_fields)

    logger.info("Sync complete for %s: %s", account.email, summary)
    return summary


def _collect_from_history(service, start_history_id):
    """Page through history.list, returning (message_ids, newest_history_id).

    Message ids are de-duplicated while preserving the order Gmail
    reported them in, so a thread's messages are ingested oldest-first.
    One message can legitimately appear in several history records.
    """
    seen = {}
    newest_history_id = start_history_id
    page_token = None

    while True:
        request = {
            "userId": "me",
            "startHistoryId": start_history_id,
            # Only additions. Without this, a label change or a delete
            # would wake up a sync that has nothing new to ingest.
            "historyTypes": ["messageAdded"],
            # Inbox only, matching the watch registration.
            "labelId": "INBOX",
            "maxResults": HISTORY_PAGE_SIZE,
        }
        if page_token:
            request["pageToken"] = page_token

        response = service.users().history().list(**request).execute()

        for record in response.get("history", []) or []:
            for added in record.get("messagesAdded", []) or []:
                message = added.get("message") or {}
                message_id = message.get("id")
                if message_id:
                    # dict keys preserve insertion order and de-duplicate.
                    seen.setdefault(message_id, None)

        newest_history_id = _newer_history_id(
            newest_history_id, response.get("historyId")
        )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return list(seen), newest_history_id


def _collect_from_full_sync(service):
    """Recovery path: list recent inbox messages instead of history.

    Returns ``(message_ids, history_id)``.  The history id is read before
    the messages are ingested rather than after, so that anything
    arriving mid-run is picked up by the *next* sync.  Re-reading a
    message is free — ingestion is idempotent — whereas skipping one
    loses it permanently.
    """
    message_ids = []
    page_token = None

    while True:
        request = {"userId": "me", "q": FULL_SYNC_QUERY}
        if page_token:
            request["pageToken"] = page_token

        response = service.users().messages().list(**request).execute()

        for message in response.get("messages", []) or []:
            message_id = message.get("id")
            if message_id:
                message_ids.append(message_id)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # Gmail returns newest-first; ingest oldest-first so a thread reads
    # in the order it happened.
    message_ids.reverse()

    profile = service.users().getProfile(userId="me").execute()
    history_id = str(profile.get("historyId", ""))

    logger.info(
        "Full sync found %d recent inbox message(s); new cursor %s",
        len(message_ids),
        history_id,
    )
    return message_ids, history_id


def _newer_history_id(current, candidate):
    """Return whichever history id is newer.

    History ids are uint64 values delivered as strings, so they have to
    be compared numerically — ``"10000" > "9999"`` is False as a string
    comparison, which would silently rewind the cursor and re-sync the
    same window forever.
    """
    if not candidate:
        return current
    if not current:
        return str(candidate)
    try:
        return str(candidate) if int(candidate) > int(current) else current
    except (TypeError, ValueError):
        return str(candidate)
