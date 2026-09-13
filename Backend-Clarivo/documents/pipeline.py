"""
Background ingestion pipeline.

A document has to travel from ``pending_extraction`` to ``classified``,
and until now the browser drove every hop of that journey — which meant
the pipeline only advanced when someone clicked Refresh, and stalled
entirely if they navigated away from the Files tab.

This module moves ownership server-side.  ``ConfirmUploadView`` enqueues
a document and returns immediately; a single daemon worker thread drains
the queue and carries each document the rest of the way.  The frontend
polls the document list purely to *display* progress.

One worker, not a pool: processing serially is what keeps concurrent
uploads from firing simultaneous Gemini calls and tripping the free-tier
rate limit.  Uploads simply queue up behind each other.

The worker is in-memory, so a server restart loses whatever was in
flight.  That is recoverable rather than fatal — ``status`` in DynamoDB
*is* the state machine, so ``resume_stalled_documents`` can pick up any
document that was left mid-pipeline.
"""

import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

# How long to wait for the extraction Lambda before declaring failure.
EXTRACTION_TIMEOUT_SECONDS = 120
EXTRACTION_POLL_SECONDS = 2

# Statuses that mean extraction is already done — used both to skip the
# wait when resuming, and to decide which stages still need running.
_POST_EXTRACTION_STATUSES = {"extracted", "embedding", "embedded", "classifying"}

# Statuses a document can be resumed from after a restart.  Anything else
# is either finished (``classified``, ``checked``), permanently failed, or
# unreadable (``pending_ocr``).
RESUMABLE_STATUSES = {
    "pending_extraction",
    "extracted",
    "embedding",
    "embedded",
    "classifying",
}

_queue = queue.Queue()

_worker = None
_worker_lock = threading.Lock()

# Documents currently queued or being processed.  Guards against the same
# document being enqueued twice — by a retried upload, or by the resume
# sweep firing while the worker is already on it — which would otherwise
# double-insert its chunks into Weaviate.
_inflight = set()
_inflight_lock = threading.Lock()


def enqueue_document(project_id, doc_id):
    """Queue a document for background processing.

    Returns ``True`` if it was queued, ``False`` if it was already in
    flight and this call was therefore a no-op.
    """
    key = (str(project_id), str(doc_id))

    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)

    _ensure_worker_running()
    _queue.put(key)
    logger.info("Queued document %s (project %s) for ingestion", doc_id, project_id)
    return True


def resume_stalled_documents(project_id, items):
    """Re-queue any document left mid-pipeline by a previous server run.

    *items* is the already-fetched document list, so this costs no extra
    DynamoDB read.  Documents the worker is currently handling are
    filtered out by the in-flight guard inside ``enqueue_document``.
    """
    resumed = 0
    for item in items:
        if item.get("status") not in RESUMABLE_STATUSES:
            continue
        doc_id = item.get("SK", "").replace("DOC#", "")
        if doc_id and enqueue_document(project_id, doc_id):
            resumed += 1

    if resumed:
        logger.info("Resumed %d stalled document(s) for project %s", resumed, project_id)
    return resumed


def _ensure_worker_running():
    """Start the worker thread, if it is not already running."""
    global _worker

    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(
            target=_worker_loop,
            name="clarivo-ingestion",
            daemon=True,
        )
        _worker.start()
        logger.info("Ingestion worker thread started")


def _worker_loop():
    """Drain the queue forever, one document at a time."""
    while True:
        key = _queue.get()
        project_id, doc_id = key
        try:
            _process_document(project_id, doc_id)
        except Exception:
            # A failure on one document must never kill the worker and
            # strand every document queued behind it.
            logger.exception(
                "Ingestion failed for document %s (project %s)", doc_id, project_id
            )
        finally:
            with _inflight_lock:
                _inflight.discard(key)
            _queue.task_done()


def _process_document(project_id, doc_id):
    """Carry one document from wherever it is through to ``classified``.

    Each stage is skipped if a previous run already completed it, so a
    resumed document does not get embedded twice.
    """
    from detection.classify import classify_document
    from documents.dynamo import get_document, update_document_status
    from documents.weaviate_client import embed_document

    doc = get_document(project_id, doc_id)
    if doc is None:
        logger.error("Document %s (project %s) not found", doc_id, project_id)
        return

    status = doc.get("status")

    # -- Stage 1: wait for the extraction Lambda ----------------------
    if status not in _POST_EXTRACTION_STATUSES:
        doc = _wait_for_extraction(project_id, doc_id)
        if doc is None:
            update_document_status(project_id, doc_id, "failed_extraction")
            return
        status = doc.get("status")

    body = doc.get("body", "")
    if not body:
        # Extraction reported success but produced nothing indexable —
        # a blank scan, or a PDF with no extractable text layer.
        logger.warning("Document %s extracted to an empty body", doc_id)
        update_document_status(project_id, doc_id, "failed_extraction")
        return

    # -- Stage 2: chunk + embed into Weaviate -------------------------
    if status not in ("embedded", "classifying"):
        update_document_status(project_id, doc_id, "embedding")
        try:
            embed_document(
                project_id=str(project_id),
                doc_id=doc_id,
                body_text=body,
            )
        except Exception:
            logger.exception("Embedding failed for document %s", doc_id)
            update_document_status(project_id, doc_id, "failed_embedding")
            return
        update_document_status(project_id, doc_id, "embedded")

    # -- Stage 3: classify --------------------------------------------
    update_document_status(project_id, doc_id, "classifying")
    try:
        # classify_document writes doc_type, supplier and the
        # 'classified' status itself.
        classify_document(project_id, doc_id)
    except Exception:
        logger.exception("Classification failed for document %s", doc_id)
        update_document_status(project_id, doc_id, "failed_classification")
        return

    logger.info("Ingestion complete for document %s (project %s)", doc_id, project_id)


def _wait_for_extraction(project_id, doc_id):
    """Poll DynamoDB until the extraction Lambda has done its work.

    Django cannot be notified by the Lambda — it is not reachable from
    AWS in local development — so polling the record is the way to
    observe extraction finishing.

    Returns the extracted document, or ``None`` on timeout or on a file
    type the Lambda cannot read.
    """
    from documents.dynamo import get_document

    deadline = time.monotonic() + EXTRACTION_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        doc = get_document(project_id, doc_id)
        status = doc.get("status") if doc else None

        if status == "extracted":
            return doc

        if status == "pending_ocr":
            # The Lambda saw an extension it does not handle.  No amount
            # of waiting will change that.
            logger.warning("Document %s is an unsupported file type", doc_id)
            return None

        time.sleep(EXTRACTION_POLL_SECONDS)

    logger.error(
        "Timed out after %ss waiting for extraction of document %s",
        EXTRACTION_TIMEOUT_SECONDS,
        doc_id,
    )
    return None
