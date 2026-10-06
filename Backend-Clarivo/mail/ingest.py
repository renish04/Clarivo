"""
Turn one Gmail message into project data.

``ingest_message`` is the whole of what an email becomes: an EMAIL#
record for the inbox UI, a correspondence document holding the cleaned
body as project context, and one document per usable attachment pushed
through the existing S3 -> Lambda extraction pipeline.

Two orderings in here are load-bearing and are not matters of taste:

* **The email record is written before anything else.** Its conditional
  write is the idempotency key for the entire function.  Gmail's
  notifications can be delivered twice, and two syncs can overlap, so
  the same message really does arrive more than once — and the second
  arrival must not create a second copy of the body in Weaviate or
  re-upload the attachments.  Claiming the message first means every
  expensive step below runs exactly once.

* **An attachment's DynamoDB stub is written before its bytes reach
  S3.** The PutObject event fires the extraction Lambda immediately, and
  the Lambda's ``update_item`` creates the record if it does not exist
  yet.  Upload first and the Lambda can write ``body`` and
  ``status="extracted"`` before we have written anything — at which
  point our write would land on top of a record that was already
  finished.
"""

import base64
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from django.conf import settings

from documents.dynamo import (
    confirm_document,
    get_document,
    put_text_document,
    set_followup_status,
)

from .parsing import clean_body, parse_message
from .routing import route_email
from .storage import (
    find_contact_by_email,
    get_thread_link,
    put_email_record,
    put_thread_link,
    update_email_doc_ids,
)

logger = logging.getLogger(__name__)

# Only the types the extraction Lambda can actually read.  Anything else
# would be uploaded, trigger the Lambda, and be parked at
# ``pending_ocr`` forever.
SUPPORTED_ATTACHMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg"}

IMAGE_EXTENSIONS = {".jpg", ".jpeg"}

# Images below this are signature logos, social-media icons and tracking
# pixels, not documents.  Applied to images only: a small PDF is
# unusual but can still be a real one-line invoice.
MIN_IMAGE_BYTES = 15 * 1024

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# Anything outside this set becomes "_" in an S3 key.  The Lambda reads
# the key out of an S3 event, where it arrives URL-encoded — so a space
# comes through as "+", a "+" is indistinguishable from a space, and a
# literal "%" starts an escape sequence that was never encoded.  Keeping
# keys to this alphabet sidesteps all of it.
_UNSAFE_KEY_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")

# How much of the subject goes into a correspondence document's filename.
MAX_SUBJECT_IN_FILENAME = 60


def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def sanitize_filename(filename):
    """Reduce *filename* to characters that survive an S3 event round trip."""
    cleaned = _UNSAFE_KEY_CHARS_RE.sub("_", filename or "")
    # Leading dots would make the name look like an extension-only file,
    # and an empty result would produce a key ending in "/".
    cleaned = cleaned.lstrip(".")
    return cleaned or "attachment"


def _extension_of(filename):
    return os.path.splitext(filename or "")[1].lower()


def is_usable_attachment(attachment):
    """True if this attachment is worth putting through the pipeline."""
    if attachment.get("is_inline"):
        return False

    extension = _extension_of(attachment.get("filename"))
    if extension not in SUPPORTED_ATTACHMENT_EXTENSIONS:
        return False

    if extension in IMAGE_EXTENSIONS:
        try:
            size = int(attachment.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size < MIN_IMAGE_BYTES:
            return False

    return True


def _correspondence_filename(headers, received_at):
    """Build the human-readable filename for a correspondence document."""
    sender = headers.get("from_name") or headers.get("from_email") or "unknown sender"

    subject = (headers.get("subject") or "").strip() or "(no subject)"
    if len(subject) > MAX_SUBJECT_IN_FILENAME:
        subject = subject[:MAX_SUBJECT_IN_FILENAME]

    date = received_at or datetime.now(timezone.utc)
    return f"Email from {sender} — {subject} ({date.strftime('%d %b %Y')})"


def _find_part_data(part, wanted_filename):
    """Find an attachment part's inline base64 data by filename.

    Gmail only hands back an ``attachmentId`` when the part's content is
    stored separately; for a small enough part the bytes come down inside
    the message itself, in ``body.data``, with no id to fetch.  Without
    this, such an attachment would have to be skipped — so the already
    fetched message is searched rather than giving up on the file.
    """
    if not part:
        return ""

    if part.get("filename") == wanted_filename:
        data = (part.get("body") or {}).get("data")
        if data:
            return data

    for child in part.get("parts") or []:
        found = _find_part_data(child, wanted_filename)
        if found:
            return found

    return ""


def _decode_attachment_data(data):
    """base64url-decode Gmail attachment data, restoring stripped padding."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def ingest_message(account, service, message_id):
    """Ingest one Gmail message into the project it belongs to.

    Returns a summary dict, or ``None`` when the message was skipped —
    because it is the user's own mail, because no routing rule claimed
    it, or because it had already been ingested.  A skipped message
    leaves nothing at all behind.
    """
    message = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

    parsed = parse_message(message)
    headers = parsed["headers"]
    from_email = headers.get("from_email", "")

    # -- Skip the user's own mail -------------------------------------
    # Without this, a follow-up Clarivo sends would be ingested back in
    # as inbound supplier correspondence the moment it appeared in the
    # thread, and its own text would become project evidence.
    if from_email and from_email == (account.email or "").strip().lower():
        logger.info("Skipping message %s — sent by the account owner", message_id)
        return None

    cleaned_body = clean_body(parsed.get("body_text", ""))
    # Handed to route_email so it matches project names against the
    # cleaned text rather than quoted reply history.
    parsed["cleaned_body"] = cleaned_body

    # -- Route it, or drop it -----------------------------------------
    route = route_email(account, parsed)
    if route is None:
        return None

    project_id, invoice_doc_id, route_reason = route
    thread_id = parsed.get("thread_id") or ""
    received_at = parsed.get("internal_date")

    # -- Claim the message --------------------------------------------
    written = put_email_record(
        project_id,
        {
            "gmail_message_id": parsed["gmail_message_id"] or message_id,
            "thread_id": thread_id,
            "direction": "inbound",
            "from_name": headers.get("from_name", ""),
            "from_email": from_email,
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "received_at": received_at.isoformat() if received_at else "",
            "body_text": cleaned_body,
            "rfc_message_id": headers.get("rfc_message_id", ""),
            "route_reason": route_reason,
            # Filled in by update_email_doc_ids once the documents exist.
            "attachment_doc_ids": [],
            "correspondence_doc_id": "",
        },
    )
    if not written:
        # Already ingested by an earlier (or concurrent) sync.
        return None

    # -- Link the thread ----------------------------------------------
    # So a later reply routes by thread, which is the only rule that is
    # certain rather than inferred.  Only created if absent: an existing
    # link may carry an invoice_doc_id that must not be disturbed.
    if thread_id and get_thread_link(account.email, thread_id) is None:
        put_thread_link(account.email, thread_id, project_id)

    # -- Correspondence document --------------------------------------
    correspondence_doc_id = ""
    if cleaned_body:
        correspondence_doc_id = str(uuid.uuid4())
        put_text_document(
            project_id=project_id,
            doc_id=correspondence_doc_id,
            filename=_correspondence_filename(headers, received_at),
            file_type="email",
            doc_type="correspondence",
            body=cleaned_body,
            status="extracted",
            extra={
                "source_email_id": parsed["gmail_message_id"] or message_id,
                "source_thread_id": thread_id,
                "sender_email": from_email,
            },
        )
        logger.info(
            "Created correspondence document %s for project %s",
            correspondence_doc_id,
            project_id,
        )

    # -- Attachments ---------------------------------------------------
    attachment_doc_ids = []
    for attachment in parsed.get("attachments", []):
        if not is_usable_attachment(attachment):
            continue

        doc_id = _ingest_attachment(
            account=account,
            service=service,
            message=message,
            message_id=message_id,
            project_id=project_id,
            thread_id=thread_id,
            from_email=from_email,
            source_email_id=parsed["gmail_message_id"] or message_id,
            attachment=attachment,
        )
        if doc_id:
            attachment_doc_ids.append(doc_id)

    # -- Backfill the email record ------------------------------------
    update_email_doc_ids(
        project_id,
        parsed["gmail_message_id"] or message_id,
        attachment_doc_ids=attachment_doc_ids,
        correspondence_doc_id=correspondence_doc_id,
    )

    # -- Invalidate stale verdicts ------------------------------------
    _handle_new_evidence(project_id, invoice_doc_id, from_email)

    return {
        "project_id": project_id,
        "attachment_doc_ids": attachment_doc_ids,
        "correspondence_doc_id": correspondence_doc_id,
        "route_reason": route_reason,
    }


def _ingest_attachment(
    account,
    service,
    message,
    message_id,
    project_id,
    thread_id,
    from_email,
    source_email_id,
    attachment,
):
    """Store one attachment as a document and upload it to S3.

    Returns the new doc_id, or ``None`` if the attachment could not be
    fetched.
    """
    filename = sanitize_filename(attachment.get("filename"))
    extension = _extension_of(filename)
    doc_id = str(uuid.uuid4())
    s3_key = f"projects/{project_id}/documents/{doc_id}/{filename}"

    # The stub goes first.  See the module docstring: the Lambda fires on
    # the PutObject below and will create this record itself if we have
    # not, and then our write would land on top of its extraction result.
    confirm_document(
        project_id=project_id,
        doc_id=doc_id,
        filename=filename,
        s3_key=s3_key,
        file_type=CONTENT_TYPES.get(extension, attachment.get("mime_type", "")),
        extra={
            "source_email_id": source_email_id,
            "source_thread_id": thread_id,
            "origin": "email",
            "sender_email": from_email,
        },
    )

    try:
        data = attachment.get("attachment_id")
        if data:
            response = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=data)
                .execute()
            )
            raw_data = response.get("data", "")
        else:
            # Small attachments come inline, with no id to fetch.
            raw_data = _find_part_data(
                message.get("payload"), attachment.get("filename")
            )

        if not raw_data:
            raise ValueError("attachment carried no data")

        file_bytes = _decode_attachment_data(raw_data)
    except Exception:
        logger.exception(
            "Could not download attachment %r from message %s",
            attachment.get("filename"),
            message_id,
        )
        # The stub is left in place at ``pending_extraction``.  It is
        # visible in the Files tab as an upload that never completed,
        # which is more honest than silently dropping the file.
        return None

    _s3_client().put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=s3_key,
        Body=file_bytes,
        ContentType=CONTENT_TYPES.get(extension, "application/octet-stream"),
    )

    logger.info(
        "Uploaded email attachment %s (%d bytes) as document %s",
        filename,
        len(file_bytes),
        doc_id,
    )
    return doc_id


def _handle_new_evidence(project_id, invoice_doc_id, from_email):
    """Mark follow-up replies and invalidate verdicts the email undercuts.

    An email is new information about a supplier, so any invoice of
    theirs that was already checked was checked without it.
    """
    from detection.services import reset_supplier_checked_invoices

    if invoice_doc_id:
        # A reply on a thread Clarivo started about one specific invoice.
        set_followup_status(project_id, invoice_doc_id, "reply_received")

        invoice = get_document(project_id, invoice_doc_id)
        if invoice and invoice.get("status") == "checked":
            from documents.dynamo import reset_document_check_results

            logger.info(
                "Reply received on invoice %s — sending it back to 'classified'",
                invoice_doc_id,
            )
            reset_document_check_results(project_id, invoice_doc_id)
        return

    # Otherwise, if we know which supplier this address belongs to, every
    # checked invoice of theirs is now judged on incomplete evidence.
    contact = find_contact_by_email(project_id, from_email)
    if contact and contact.get("supplier_name"):
        reset_supplier_checked_invoices(
            project_id,
            contact["supplier_name"],
            reason="an email from the supplier",
        )
