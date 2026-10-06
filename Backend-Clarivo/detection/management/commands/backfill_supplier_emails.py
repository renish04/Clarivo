"""Backfill supplier details onto documents classified before they existed.

Classification learned to extract ``supplier_email`` after projects had
already been ingested, so their documents carry a ``supplier`` at best
and nothing to send a follow-up to.  This command fills that gap.

It deliberately does *not* call ``classify_document``.  Re-classifying a
document rewrites its ``doc_type``, which fires the Part 8 reset for
orders, delivery notes and governing documents -- sending every checked
invoice from that supplier back to ``classified`` and putting the whole
project through detection again.  A backfill must be cheap and invisible,
so it makes its own narrow Gemini call and writes only the two supplier
attributes.

Usage::

    python manage.py backfill_supplier_emails <project_id>
"""

import time

from django.core.management.base import BaseCommand, CommandError

from detection.classify import extract_supplier_details
from documents.dynamo import list_documents, update_document_supplier
from documents.services import CORRESPONDENCE_DOC_TYPE, GEMINI_CALL_DELAY_SECONDS
from mail.storage import upsert_contact


class Command(BaseCommand):
    help = (
        "Extract supplier and supplier_email for a project's documents that "
        "do not have them yet, without re-classifying anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "project_id",
            help="The Django project PK whose documents should be backfilled.",
        )

    def handle(self, *args, **options):
        project_id = options["project_id"]

        documents = list_documents(project_id)
        if not documents:
            raise CommandError(
                f"No documents found for project {project_id}. Check the "
                "project id -- an empty partition and a wrong id look alike."
            )

        candidates = [doc for doc in documents if _needs_backfill(doc)]

        self.stdout.write(
            f"Project {project_id}: {len(documents)} document(s), "
            f"{len(candidates)} to backfill."
        )
        if not candidates:
            return

        filled = 0
        no_email = 0
        failed = 0
        contacts = 0

        for index, doc in enumerate(candidates):
            doc_id = doc.get("SK", "").replace("DOC#", "")
            label = f"[{index + 1}/{len(candidates)}] {doc.get('filename') or doc_id}"

            if not doc_id:
                failed += 1
                self.stdout.write(self.style.WARNING(f"{label}: unreadable SK, skipped"))
                continue

            try:
                supplier, supplier_email = extract_supplier_details(doc["body"])
            except Exception as exc:
                # One bad document must not end the run; the next pass
                # will pick it up again, since nothing was written.
                failed += 1
                self.stdout.write(self.style.ERROR(f"{label}: extraction failed -- {exc}"))
                continue

            update_document_supplier(project_id, doc_id, supplier, supplier_email)

            if supplier and supplier_email:
                filled += 1
                stored = ""
                try:
                    if upsert_contact(project_id, supplier, supplier_email, source="document"):
                        contacts += 1
                        stored = " (contact stored)"
                    else:
                        stored = " (contact kept from a better source)"
                except Exception as exc:
                    stored = f" (contact not stored -- {exc})"

                self.stdout.write(
                    self.style.SUCCESS(f"{label}: {supplier} <{supplier_email}>{stored}")
                )
            elif supplier:
                no_email += 1
                self.stdout.write(f"{label}: {supplier}, no issuer email printed")
            else:
                no_email += 1
                self.stdout.write(f"{label}: no issuer identified")

            # Space out the Gemini calls for the free-tier rate limit,
            # but not after the last one.
            if index < len(candidates) - 1:
                time.sleep(GEMINI_CALL_DELAY_SECONDS)

        self.stdout.write(
            f"Done: {filled} with an email, {no_email} without, {failed} failed; "
            f"{contacts} contact(s) stored."
        )


def _needs_backfill(doc):
    """True if this document should get a supplier-extraction call.

    Three exclusions, each paying for a Gemini call that would tell us
    nothing: a document whose text has not been extracted yet, one that
    already has an email on it, and correspondence -- the body of an
    ingested email, whose sender address email ingestion already knows
    first-hand and stores at a higher trust than anything read out of a
    document.
    """
    if not doc.get("body"):
        return False

    if doc.get("supplier_email"):
        return False

    if doc.get("doc_type") == CORRESPONDENCE_DOC_TYPE:
        return False

    return True
