"""
Pipeline stages as callable functions, independent of any HTTP request.

Until now the only way to move a document from ``extracted`` to
``classified`` was to call the two endpoints the Files tab calls.  Email
ingestion happens with no browser open, so the stages are lifted out
here and the endpoints become callers like any other.

Every stage begins by *claiming* the document: a conditional status
update that succeeds for exactly one caller.  That is what makes it safe
for the Files tab, the background ingestion worker and an email sync to
all be working the same project at the same moment.  Without it, two of
them reading ``extracted`` in the same instant would both embed the
document and insert its chunks into Weaviate twice — which silently
doubles that document's weight in every later retrieval.

The in-progress statuses (``embedding``, ``classifying``) are the claim
itself, not just progress reporting: while a document sits in one, no
other caller can pick it up.
"""

import logging
import time

from documents.dynamo import (
    claim_document_status,
    get_document,
    list_documents,
    update_document_status,
)

logger = logging.getLogger(__name__)

# Pause between Gemini calls when sweeping a batch, to stay inside the
# free tier's rate limit.  Matches the delay the detection run uses.
GEMINI_CALL_DELAY_SECONDS = 2

# Correspondence documents are the cleaned text of an ingested email.
# They are embedded as project context but never classified: asking
# Gemini "is this an invoice?" about an email body wastes a call to
# answer a question we already know, and a misfire would let an email be
# treated as an invoice and checked for discrepancies against itself.
CORRESPONDENCE_DOC_TYPE = "correspondence"


def embed_and_mark(project_id, doc_id):
    """Chunk and embed one document, moving ``extracted`` -> ``embedded``.

    Returns ``True`` if this call did the work, ``False`` if the document
    was not available to claim (already embedded, or being embedded by
    someone else right now).

    On failure the status is put back to ``extracted`` so the document is
    retried on the next sweep, and the exception is re-raised for the
    caller to log or surface.
    """
    if not claim_document_status(project_id, doc_id, "extracted", "embedding"):
        logger.info(
            "Document %s not claimable for embedding (already in progress or done)",
            doc_id,
        )
        return False

    try:
        doc = get_document(project_id, doc_id)
        if doc is None:
            raise ValueError(f"Document {doc_id} disappeared while being claimed")

        body_text = doc.get("body", "")
        if not body_text:
            # Nothing indexable: a blank scan, or a PDF with no text
            # layer.  Deliberately *not* reverted to ``extracted`` — that
            # would make every future sweep claim it, embed nothing and
            # fail again forever.  ``failed_extraction`` is terminal, and
            # is the status the ingestion worker already uses for this.
            logger.warning("Document %s has an empty body; marking failed", doc_id)
            update_document_status(project_id, doc_id, "failed_extraction")
            return False

        from documents.weaviate_client import embed_document

        chunks = embed_document(
            project_id=str(project_id),
            doc_id=doc_id,
            body_text=body_text,
        )
        logger.info("Embedded document %s into %s chunk(s)", doc_id, chunks)
    except Exception:
        update_document_status(project_id, doc_id, "extracted")
        raise

    update_document_status(project_id, doc_id, "embedded")
    return True


def classify_and_mark(project_id, doc_id):
    """Classify one document, moving ``embedded`` -> ``classified``.

    ``classify_document`` writes the ``doc_type``, the ``supplier`` and
    the ``classified`` status itself, and also runs the Part 8 reset that
    sends already-checked invoices from the same supplier back to
    ``classified`` when new evidence arrives.  That behaviour is
    unchanged; this only adds the claim around it.

    Returns ``True`` if this call did the work, ``False`` if the document
    could not be claimed.  On failure the status reverts to ``embedded``
    and the exception is re-raised.
    """
    if not claim_document_status(project_id, doc_id, "embedded", "classifying"):
        logger.info(
            "Document %s not claimable for classification "
            "(already in progress or done)",
            doc_id,
        )
        return False

    try:
        from detection.classify import classify_document

        classify_document(project_id, doc_id)
    except Exception:
        update_document_status(project_id, doc_id, "embedded")
        raise

    return True


def mark_correspondence_classified(project_id, doc_id):
    """Move an embedded correspondence document straight to ``classified``.

    Uses the same conditional transition as a claim, so it cannot race
    with anything else that might be looking at the document.
    """
    if not claim_document_status(project_id, doc_id, "embedded", "classified"):
        return False

    logger.info("Correspondence document %s marked classified (not classified by LLM)", doc_id)
    return True


def process_pending(project_id):
    """Carry every pending document in a project as far as ``classified``.

    This is the server-side equivalent of what the Files tab used to
    drive by polling: embed everything sitting at ``extracted``, then
    classify everything sitting at ``embedded``.

    Returns a dict of counts, for logging and for the detection summary.
    """
    counts = {"embedded": 0, "classified": 0, "correspondence": 0, "failed": 0}

    # -- Pass 1: embed ----------------------------------------------------
    for doc in list_documents(project_id):
        if doc.get("status") != "extracted":
            continue

        doc_id = doc.get("SK", "").replace("DOC#", "")
        if not doc_id:
            continue

        try:
            if embed_and_mark(project_id, doc_id):
                counts["embedded"] += 1
        except Exception:
            # One unembeddable document must not stop the rest of the
            # project from being processed.  embed_and_mark has already
            # put the status back for the next sweep to retry.
            counts["failed"] += 1
            logger.exception("Embedding failed for document %s", doc_id)

    # -- Pass 2: classify -------------------------------------------------
    # Re-read the list: the documents just embedded in pass 1 are the
    # whole point of this pass, and they were at ``extracted`` when the
    # first query ran.
    pending_classification = [
        doc for doc in list_documents(project_id) if doc.get("status") == "embedded"
    ]

    for index, doc in enumerate(pending_classification):
        doc_id = doc.get("SK", "").replace("DOC#", "")
        if not doc_id:
            continue

        # Correspondence is embedded like anything else but skips the
        # classifier entirely.
        if doc.get("doc_type") == CORRESPONDENCE_DOC_TYPE:
            if mark_correspondence_classified(project_id, doc_id):
                counts["correspondence"] += 1
            continue

        try:
            if classify_and_mark(project_id, doc_id):
                counts["classified"] += 1
        except Exception:
            counts["failed"] += 1
            logger.exception("Classification failed for document %s", doc_id)

        # Space out the Gemini calls, but not after the last one.
        if index < len(pending_classification) - 1:
            time.sleep(GEMINI_CALL_DELAY_SECONDS)

    logger.info("process_pending(project=%s): %s", project_id, counts)
    return counts
