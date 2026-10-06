"""
Discrepancy detection as a callable function, independent of any request.

Lifted out of ``CheckProjectView`` so that an email sync can run a check
with no browser involved.  The view is now a thin wrapper around
``run_detection``, which is the only implementation.

Two things are new relative to the old view body, both for the sake of
running unattended:

* It brings the project up to date first, via ``process_pending`` — a
  background sync may have ingested documents that nobody has embedded
  or classified yet, and an invoice cannot be checked against evidence
  that has not been indexed.
* Each invoice is claimed before Gemini is called, so a background run
  and someone pressing "Check Project" cannot analyse the same invoice
  twice — which would burn two expensive detection calls to write the
  same findings.
"""

import logging
import time

from documents.dynamo import (
    claim_document_status,
    list_documents,
    reset_document_check_results,
    update_document_check_results,
    update_document_status,
)
from documents.services import process_pending

logger = logging.getLogger(__name__)


def reset_supplier_checked_invoices(project_id, supplier, reason="new evidence"):
    """Send a supplier's already-checked invoices back to ``classified``.

    A completed check is only as good as the evidence it was made
    against.  When something new arrives about a supplier — an order, a
    delivery note, a governing document (Part 8), or now an email from
    that supplier — any invoice of theirs that was already judged was
    judged without it, so the verdict is stale and has to be recomputed.

    Clearing the result fields as well as the status matters: a stale
    ``findings`` list left in place would keep appearing in the
    discrepancy table as though it were current.

    Parameters
    ----------
    project_id : int | str
    supplier : str
        The supplier name to match.  Compared case-insensitively against
        the ``supplier`` attribute on each invoice.
    reason : str
        Included in the log line, so it is possible to tell a reset
        caused by a new document from one caused by an email.

    Returns
    -------
    list[str]
        The doc_ids that were reset.
    """
    if not supplier:
        return []

    supplier_lower = supplier.strip().lower()
    reset_doc_ids = []

    for doc in list_documents(project_id):
        if (
            doc.get("doc_type") == "invoice"
            and doc.get("status") == "checked"
            and doc.get("supplier", "").strip().lower() == supplier_lower
        ):
            target_doc_id = doc.get("SK", "").replace("DOC#", "")
            if not target_doc_id:
                continue
            print(
                f"[RESET] Sending invoice {target_doc_id} back to 'classified' "
                f"due to {reason} from {supplier}"
            )
            reset_document_check_results(project_id, target_doc_id)
            reset_doc_ids.append(target_doc_id)

    return reset_doc_ids

# Gemini free-tier rate limiting: pause between detection calls.
DETECTION_CALL_DELAY_SECONDS = 2

# The discrepancy outcomes detect_discrepancies can report.
_SUMMARY_KEYS = ("clean", "flagged", "auto_resolved", "needs_more_info")


def run_detection(project_id):
    """Check every classified invoice in a project for discrepancies.

    Returns the summary dict the endpoint has always returned:
    ``{"clean": n, "flagged": n, "auto_resolved": n, "needs_more_info": n}``.
    """
    print(f"\n=== [CHECK] Running detection for Project {project_id} ===")

    # Bring any freshly ingested documents up to ``classified`` first, so
    # that evidence which arrived by email is in Weaviate and carries a
    # doc_type before the invoices below are judged against it.
    process_pending(project_id)

    all_docs = list_documents(project_id)

    invoices_to_check = [
        doc for doc in all_docs
        if doc.get("doc_type") == "invoice" and doc.get("status") == "classified"
    ]

    print(f"[CHECK] Found {len(invoices_to_check)} invoices waiting to be checked.")

    summary = {key: 0 for key in _SUMMARY_KEYS}

    for i, doc in enumerate(invoices_to_check):
        doc_id = doc["SK"].replace("DOC#", "")

        # Claim it.  A losing caller skips the invoice rather than
        # duplicating an expensive Gemini detection call — the caller
        # that won will write the findings.
        if not claim_document_status(project_id, doc_id, "classified", "checking"):
            print(f"[CHECK] Invoice {doc_id} is already being checked elsewhere; skipping.")
            continue

        print(
            f"\n[CHECK] Processing invoice {i+1}/{len(invoices_to_check)} "
            f"(Doc {doc_id})..."
        )

        try:
            # verify_grounding is already baked inside detect_discrepancies.
            from detection.detect import detect_discrepancies

            result = detect_discrepancies(project_id, doc_id)
        except Exception:
            # Put it back so the next run retries it, and keep going:
            # one invoice that trips the LLM must not abandon the rest.
            logger.exception("Detection failed for invoice %s", doc_id)
            print(f"[CHECK] ERROR: detection failed for {doc_id}; reverting status.")
            update_document_status(project_id, doc_id, "classified")
            continue

        discrepancy_status = result.get("status", "needs_more_info")

        if discrepancy_status in summary:
            summary[discrepancy_status] += 1
        else:
            summary["needs_more_info"] += 1
            discrepancy_status = "needs_more_info"

        print(f"[CHECK] Saving results to DynamoDB (Status: {discrepancy_status})...")
        update_document_check_results(
            project_id=project_id,
            doc_id=doc_id,
            discrepancy_status=discrepancy_status,
            findings=result.get("findings", []),
            resolution=result.get("resolution", ""),
            table_row_markdown=result.get("table_row_markdown", ""),
            new_status="checked",
        )

        # Gemini free-tier rate limiting (except after the last one).
        if i < len(invoices_to_check) - 1:
            print(f"[CHECK] Waiting {DETECTION_CALL_DELAY_SECONDS} seconds to respect rate limits...")
            time.sleep(DETECTION_CALL_DELAY_SECONDS)

    print(f"\n=== [CHECK] Complete. Summary: {summary} ===\n")
    return summary
