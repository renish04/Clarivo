"""
Off-request processing for mail ingestion.

Everything a newly ingested email sets off — waiting for the extraction
Lambda, embedding, classification, learning supplier contacts, re-running
detection — takes far longer than an HTTP request may.  The Pub/Sub push
webhook in particular has to answer within a few seconds or Pub/Sub
treats the delivery as failed and redelivers it, so the webhook's only
job is to acknowledge and hand off to here.

PRODUCTION NOTE
---------------
In production this would be an SQS queue with a Lambda consumer: the
webhook would enqueue ``{account_id}`` and return, and the queue would
give retries, visibility timeouts, a dead-letter queue and horizontal
scaling for free.  The thread below is the prototype stand-in.  Its
limits are worth being explicit about — work in flight is lost if the
process restarts, nothing is retried, and it does not scale past one
Django process — all of which are acceptable for a capstone demo and
none of which would be in production.

Concurrency model
-----------------
One worker per account, never more.  A sync is not safe to run twice
concurrently for the same mailbox in the sense that matters here: it
would double the Gemini calls and race on the history cursor.  So each
account has a lock, and a trigger that finds the lock held does not
queue a second worker — it sets ``dirty``, and the worker already
running loops one more time before it exits.  That collapses a burst of
ten notifications into one sync plus one follow-up sync, instead of ten.
"""

import logging
import threading
import time

from django.db import connection

from documents.dynamo import get_document, list_documents
from documents.services import process_pending

from .gmail_client import GmailReconnectRequired
from .models import GmailAccount
from .storage import upsert_contact
from .sync import sync_mailbox

logger = logging.getLogger(__name__)

# How long to wait for emailed attachments to finish the ingestion
# pipeline — extraction by the Lambda, then embedding and classification
# by the ingestion worker — before detection runs without them.
EXTRACTION_TIMEOUT_SECONDS = 300
EXTRACTION_POLL_SECONDS = 5

# Statuses an attachment passes through on its way to ``classified``.
# Detection waits while any emailed attachment is in one, so an invoice
# is never judged a moment before the evidence that came with the email
# is in the index.
IN_FLIGHT_STATUSES = {
    "pending_extraction",
    "extracted",
    "embedding",
    "embedded",
    "classifying",
}

# Per-account worker state, keyed by GmailAccount pk:
#   lock             -- held for the whole duration of a worker run
#   dirty            -- a trigger arrived while the worker was running
#   pending_projects -- projects a caller has asked us to process even if
#                       the next sync itself finds nothing new
_states = {}

# Guards _states itself.  Without it, two triggers for an account that
# has never synced could each build a state dict and get a different
# lock — and then both run at once, which is the one thing the lock
# exists to prevent.
_registry_lock = threading.Lock()


def _state_for(account_id):
    """Return (creating if needed) the worker state for an account."""
    with _registry_lock:
        state = _states.get(account_id)
        if state is None:
            state = {
                "lock": threading.Lock(),
                "dirty": False,
                "pending_projects": set(),
            }
            _states[account_id] = state
        return state


def trigger_sync(account_id, project_ids=None):
    """Ask for a background sync of this account.

    *project_ids* seeds the per-project follow-up work.  It exists for
    the "Sync now" endpoint, which runs ``sync_mailbox`` synchronously so
    the browser gets a real answer, and then calls this for the slow part.
    By that point the messages have already been ingested, so the
    worker's own sync finds nothing new and would otherwise have no
    projects to process — the follow-up would silently never happen.

    Returns ``True`` if a worker was started, ``False`` if one was
    already running (in which case it has been told to loop again).
    """
    state = _state_for(account_id)

    if project_ids:
        with _registry_lock:
            state["pending_projects"].update(project_ids)

    # Acquired here, in the *calling* thread, rather than inside the
    # worker: checking in the caller and acquiring in the worker would
    # leave a window where two triggers both see the lock free and both
    # start a worker.  threading.Lock has no owner, so releasing it from
    # the worker thread is legitimate.
    if not state["lock"].acquire(blocking=False):
        state["dirty"] = True
        logger.info(
            "Sync already running for account %s — marked for another pass",
            account_id,
        )
        return False

    try:
        thread = threading.Thread(
            target=_run,
            args=(account_id,),
            name=f"clarivo-mail-sync-{account_id}",
            daemon=True,
        )
        thread.start()
    except Exception:
        # Nothing will ever release the lock if the thread never ran.
        state["lock"].release()
        raise

    logger.info("Background mail sync started for account %s", account_id)
    return True


def _run(account_id):
    """Sync, then process each touched project; repeat while dirty."""
    state = _state_for(account_id)

    try:
        while True:
            state["dirty"] = False

            with _registry_lock:
                seeded_projects = set(state["pending_projects"])
                state["pending_projects"].clear()

            try:
                account = GmailAccount.objects.get(pk=account_id)
            except GmailAccount.DoesNotExist:
                # Disconnected while we were queued.
                logger.info("Gmail account %s no longer exists", account_id)
                return

            project_ids = set(seeded_projects)

            try:
                summary = sync_mailbox(account)
                project_ids.update(summary["project_ids"])
            except GmailReconnectRequired:
                # sync_mailbox has already flagged the account; the UI
                # reads needs_reconnect from /status/.  Retrying is
                # pointless until the user re-consents.
                logger.warning(
                    "Gmail account %s needs reconnecting — stopping sync",
                    account.email,
                )
                return
            except Exception:
                logger.exception("Mail sync failed for account %s", account_id)
                # Still fall through: projects seeded by a caller may
                # have work waiting regardless of the sync failing.

            for project_id in sorted(project_ids):
                try:
                    _process_project(project_id)
                except Exception:
                    logger.exception(
                        "Post-ingestion processing failed for project %s",
                        project_id,
                    )

            if not state["dirty"]:
                return
            logger.info("Another trigger arrived — syncing account %s again", account_id)
    finally:
        state["lock"].release()
        # Django opens a connection per thread and does not close it when
        # the thread ends, so a worker that did not do this would leak an
        # SQLite connection on every run.
        connection.close()


def _process_project(project_id):
    """Bring one project up to date after new mail arrived in it."""
    pending_attachments = _pending_email_attachments(project_id)

    if pending_attachments:
        logger.info(
            "Waiting for %d emailed attachment(s) in project %s to be ingested",
            len(pending_attachments),
            project_id,
        )
        _wait_for_extraction(project_id, pending_attachments)

    # process_pending is called explicitly here, before learn_contacts,
    # because learning a contact needs the attachment's supplier — which
    # only exists once the document has been classified.  run_detection
    # calls process_pending itself too; by then there is nothing left
    # pending, so it costs one DynamoDB query and does no work.
    process_pending(project_id)

    learn_contacts(project_id, pending_attachments)

    # Correspondence documents were embedded by process_pending above, so
    # a supplier's reply is already in the retrievable context before any
    # invoice is judged against it.
    run_summary = _run_detection(project_id)
    logger.info("Detection after mail sync for project %s: %s", project_id, run_summary)


def _pending_email_attachments(project_id):
    """Doc ids of email-sourced documents still moving through ingestion.

    Read from DynamoDB rather than passed in from the sync, so that an
    attachment left behind by an earlier run — a restart mid-wait, a
    Lambda that was slow — is picked up by the next pass instead of
    being stranded.
    """
    return [
        doc.get("SK", "").replace("DOC#", "")
        for doc in list_documents(project_id)
        if doc.get("origin") == "email"
        and doc.get("status") in IN_FLIGHT_STATUSES
        and doc.get("SK")
    ]


def _wait_for_extraction(project_id, doc_ids):
    """Poll until none of *doc_ids* is still in flight, or we time out.

    The ingestion worker owns each attachment from upload to
    ``classified``, just as it does an upload from the Files tab; this
    only watches.  Polling the record is the only way to observe it —
    the extraction Lambda cannot call back into Django.

    Returns ``True`` if everything was extracted in time.
    """
    deadline = time.monotonic() + EXTRACTION_TIMEOUT_SECONDS
    waiting = set(doc_ids)

    while waiting:
        still_waiting = set()
        for doc_id in waiting:
            doc = get_document(project_id, doc_id)
            if doc and doc.get("status") in IN_FLIGHT_STATUSES:
                still_waiting.add(doc_id)
        waiting = still_waiting

        if not waiting:
            return True
        if time.monotonic() >= deadline:
            break

        time.sleep(EXTRACTION_POLL_SECONDS)

    logger.warning(
        "Gave up waiting for ingestion of %d document(s) in project %s",
        len(waiting),
        project_id,
    )
    return False


def learn_contacts(project_id, attachment_doc_ids, sender_email=None):
    """Record the address a supplier's documents actually arrived from.

    A supplier name comes out of the document text; the address it was
    sent from is the only reliable way to recognise that supplier's mail
    next time.  Pairing the two is what lets routing rule (3) work at
    all, and it is learned here rather than typed in by anyone.

    Written at ``source="email_sender"``, which outranks a name scraped
    out of a document but is still overridden by anything the user
    entered by hand.

    *sender_email* overrides the address stored on each document.  Left
    at ``None`` each document supplies its own, which is what one wants
    when a single sync brought in attachments from several senders.

    Returns the number of contacts written.
    """
    learned = 0

    for doc_id in attachment_doc_ids:
        doc = get_document(project_id, doc_id)
        if not doc:
            continue

        supplier = (doc.get("supplier") or "").strip()
        if not supplier:
            continue

        # "checked" as well as "classified": on a second loop pass,
        # detection may already have run over the document.
        if doc.get("status") not in ("classified", "checked"):
            continue

        email = sender_email or doc.get("sender_email")
        if not email:
            continue

        if upsert_contact(project_id, supplier, email, source="email_sender"):
            learned += 1
            logger.info(
                "Learned contact for %r in project %s: %s",
                supplier,
                project_id,
                email,
            )

    return learned


def _run_detection(project_id):
    """Imported lazily: detection.services pulls in a lot at import time."""
    from detection.services import run_detection

    return run_detection(project_id)
