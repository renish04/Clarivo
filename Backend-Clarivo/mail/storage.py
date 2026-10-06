"""
DynamoDB persistence for ingested email.

Same table and same single-table design as ``documents/dynamo.py`` and
``chat/storage.py``: a project's data all lives in one
``PROJECT#<id>`` partition, separated by sort-key prefix.  This module
adds two more prefixes to that partition —

    EMAIL#<gmail_message_id>      one ingested email, for the inbox UI
    CONTACT#<normalised supplier> a supplier's email address

— alongside the existing ``DOC#`` and ``CHAT#``.

Thread links are the exception and live in their own partition:

    PK = THREAD#<account_email>#<thread_id>,  SK = LINK

They have to, because of who asks for them.  When a reply arrives, the
webhook knows the mailbox and the Gmail thread id but *not* which
project the thread belongs to — that is the very thing it is looking
up — so the thread has to be the partition key.  The account email is
part of it because Gmail thread ids are only unique within a single
mailbox; two users could hold the same thread id for unrelated
conversations.

One consequence worth knowing: ``delete_project_items`` sweeps a whole
``PROJECT#`` partition, so it cleans up EMAIL# and CONTACT# items for
free, but it cannot reach THREAD# items — they are not in that
partition and are not indexed by project.  A deleted project therefore
leaves its thread links behind, which is why ``get_thread_link``
callers must treat the ``project_id`` it returns as a claim to verify
rather than a fact.
"""

import logging
import re
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from django.conf import settings

logger = logging.getLogger(__name__)

# Module-level resource — created once on first import, then reused.
# Same pattern (and same table) as documents/dynamo.py.
_dynamodb = boto3.resource(
    "dynamodb",
    region_name=settings.AWS_REGION,
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
)
_table = _dynamodb.Table(settings.DYNAMODB_TABLE_NAME)

# Sort-key prefixes.  Single source of truth, so the read and write
# paths can never disagree about the layout.
EMAIL_SK_PREFIX = "EMAIL#"
CONTACT_SK_PREFIX = "CONTACT#"
THREAD_SK = "LINK"

# How much a source's claim about a supplier's address is worth.  A
# human typing it in beats an address a real email actually came from,
# which in turn beats one scraped out of a document by an LLM.
SOURCE_TRUST = {
    "manual": 3,
    "email_sender": 2,
    "document": 1,
}

# Corporate-form words that carry no identifying information.  "Acme
# Pvt. Ltd." and "Acme Limited" are the same supplier, and an invoice
# and a delivery note rarely spell it the same way.
_SUPPLIER_STOPWORDS = {"pvt", "ltd", "private", "limited", "co", "and"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _project_pk(project_id):
    return f"PROJECT#{project_id}"


# ---------------------------------------------------------------------------
# Email records
# ---------------------------------------------------------------------------

def put_email_record(project_id, record):
    """Store one ingested email, if it is not already stored.

    The conditional write is what makes the whole ingestion pipeline
    idempotent.  Gmail's Pub/Sub notifications may be delivered late,
    out of order, or more than once, and two syncs can overlap — the
    push webhook and a "Sync now" click racing each other — so the same
    message genuinely does arrive twice.  Keying on the Gmail message id
    and refusing to overwrite means the second arrival is a no-op that
    costs one write unit, rather than a duplicate inbox entry and a
    second copy of the body embedded into the project context.

    Returns
    -------
    bool
        ``True`` if this call wrote the record, ``False`` if it already
        existed.  Callers use that to decide whether to do the expensive
        follow-on work (uploading attachments, embedding the body).
    """
    gmail_message_id = record["gmail_message_id"]

    item = {
        "PK": _project_pk(project_id),
        "SK": f"{EMAIL_SK_PREFIX}{gmail_message_id}",
        "gmail_message_id": gmail_message_id,
        "thread_id": record.get("thread_id", ""),
        # "inbound" for mail we ingested, "outbound" for follow-ups
        # Clarivo sent — both belong in the same thread view.
        "direction": record.get("direction", "inbound"),
        "from_name": record.get("from_name", ""),
        "from_email": record.get("from_email", ""),
        "to": record.get("to", ""),
        "subject": record.get("subject", ""),
        "received_at": record.get("received_at", ""),
        "body_text": record.get("body_text", ""),
        "rfc_message_id": record.get("rfc_message_id", ""),
        # Which routing rule claimed this email, kept so the UI can
        # explain why a message landed in this project.
        "route_reason": record.get("route_reason", ""),
        "attachment_doc_ids": record.get("attachment_doc_ids") or [],
        "correspondence_doc_id": record.get("correspondence_doc_id", ""),
        "created_at": _now_iso(),
    }

    try:
        _table.put_item(
            Item=item,
            # Guards on the sort key, which only exists if the whole
            # item does — so this means "only if this email is new".
            ConditionExpression="attribute_not_exists(SK)",
        )
        return True
    except _table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(
            "Email %s already stored for project %s — skipping",
            gmail_message_id,
            project_id,
        )
        return False


def update_email_doc_ids(
    project_id,
    gmail_message_id,
    attachment_doc_ids=None,
    correspondence_doc_id=None,
):
    """Attach the created document ids to an already-stored email record.

    The email record is written *first* during ingestion, before any
    document exists, because that conditional write is what claims the
    message and stops a duplicate delivery being processed twice.  The
    document ids are therefore only known afterwards, and are filled in
    here.

    Only the arguments actually supplied are written, so a later call
    cannot blank out ids an earlier one set.

    Returns the full updated item, or ``None`` if nothing was supplied.
    """
    set_clauses = []
    values = {}

    if attachment_doc_ids is not None:
        set_clauses.append("attachment_doc_ids = :attachment_doc_ids")
        values[":attachment_doc_ids"] = attachment_doc_ids

    if correspondence_doc_id is not None:
        set_clauses.append("correspondence_doc_id = :correspondence_doc_id")
        values[":correspondence_doc_id"] = correspondence_doc_id

    if not set_clauses:
        return None

    response = _table.update_item(
        Key={
            "PK": _project_pk(project_id),
            "SK": f"{EMAIL_SK_PREFIX}{gmail_message_id}",
        },
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes")


def find_contact_by_email(project_id, email):
    """Return the contact record in *project_id* whose address is *email*.

    Used when an email arrives from an address already recorded as a
    supplier contact: the contact carries the supplier name, which is
    what identifies whose invoices the email is evidence about.

    Returns the contact item, or ``None``.
    """
    wanted = (email or "").strip().lower()
    if not wanted:
        return None

    for contact in list_contacts(project_id):
        if (contact.get("email") or "").strip().lower() == wanted:
            return contact
    return None


def list_email_records(project_id):
    """Return a project's ingested emails, newest first.

    Paginated explicitly: bodies are stored up to 8000 characters, so a
    busy project's emails can exceed DynamoDB's 1 MB response limit, and
    a single-page read would quietly stop showing older mail.

    The sort order comes from ``received_at`` rather than from the sort
    key, because the sort key holds a Gmail message id — those are
    *roughly* chronological but nothing documents them as sortable.
    """
    items = []
    start_key = None

    while True:
        kwargs = {
            "KeyConditionExpression": (
                Key("PK").eq(_project_pk(project_id))
                & Key("SK").begins_with(EMAIL_SK_PREFIX)
            ),
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key

        response = _table.query(**kwargs)
        items.extend(response.get("Items", []))

        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break

    items.sort(key=lambda item: item.get("received_at") or "", reverse=True)
    return items


# ---------------------------------------------------------------------------
# Thread links
# ---------------------------------------------------------------------------

def _thread_pk(account_email, thread_id):
    return f"THREAD#{(account_email or '').strip().lower()}#{thread_id}"


def put_thread_link(account_email, thread_id, project_id, invoice_doc_id=None):
    """Record which project (and optionally which invoice) a thread is about.

    This is routing rule (1): a reply to a follow-up Clarivo sent is
    routed by its thread, with no guessing from subject or sender.

    ``invoice_doc_id`` is only written when one is supplied.  A link is
    first created when a follow-up is sent about a specific invoice, and
    later touched again as replies arrive — at which point the caller
    may not know, or care, which invoice started it.  Writing ``None``
    then would erase the one piece of information the link was created
    to carry, so the attribute is left alone instead.

    Returns the full stored item.
    """
    update_expression = "SET project_id = :project_id, updated_at = :now"
    values = {
        ":project_id": str(project_id),
        ":now": _now_iso(),
    }

    if invoice_doc_id:
        update_expression += ", invoice_doc_id = :invoice_doc_id"
        values[":invoice_doc_id"] = invoice_doc_id

    response = _table.update_item(
        Key={
            "PK": _thread_pk(account_email, thread_id),
            "SK": THREAD_SK,
        },
        UpdateExpression=update_expression,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes")


def get_thread_link(account_email, thread_id):
    """Look up the project a Gmail thread belongs to.

    Returns the link item, or ``None`` if this thread has never been
    linked.  The ``project_id`` it carries is unverified: the project may
    since have been deleted (see the module docstring), so callers should
    confirm it still exists before routing an email into it.
    """
    response = _table.get_item(
        Key={
            "PK": _thread_pk(account_email, thread_id),
            "SK": THREAD_SK,
        },
    )
    return response.get("Item")


# ---------------------------------------------------------------------------
# Supplier contacts
# ---------------------------------------------------------------------------

def normalize_supplier_name(supplier_name):
    """Reduce a supplier name to a stable key.

    The same supplier is written half a dozen ways across a project's
    documents — "Acme Pvt. Ltd.", "ACME PRIVATE LIMITED", "Acme & Co" —
    and each spelling would otherwise become its own contact, so routing
    rule (3) would fail to recognise a sender it had already seen.
    """
    if not supplier_name:
        return ""

    # Lowercase, then keep only letters, digits and spaces.  This is what
    # turns "Pvt." into the bare word "pvt" so it can be dropped below,
    # and what makes "Acme & Co" and "Acme and Co" converge.
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", supplier_name.lower())

    words = [word for word in cleaned.split() if word not in _SUPPLIER_STOPWORDS]

    if not words:
        # A name made of nothing but corporate-form words, e.g. "Ltd".
        # Falling back to the cleaned text keeps it distinguishable
        # instead of collapsing every such supplier onto one empty key.
        return " ".join(cleaned.split())

    return " ".join(words)


def upsert_contact(project_id, supplier_name, email, source):
    """Record a supplier's email address, unless a better source already did.

    *source* is one of ``"manual"``, ``"email_sender"`` or
    ``"document"``.  A lower-trust source never overwrites a
    higher-trust one: an address an LLM pulled out of a PDF must not
    replace one the user typed in, or one taken from an email that
    actually arrived.

    The trust comparison runs as a DynamoDB ``ConditionExpression``
    rather than as a read followed by a write, so two concurrent syncs
    cannot both read "no contact yet" and then race to write.

    Returns
    -------
    bool
        ``True`` if the contact was written, ``False`` if an
        existing higher-trust entry was left in place.
    """
    normalised = normalize_supplier_name(supplier_name)
    if not normalised:
        logger.warning(
            "Refusing to store a contact with no usable supplier name (%r)",
            supplier_name,
        )
        return False

    trust = SOURCE_TRUST.get(source)
    if trust is None:
        raise ValueError(
            f"Unknown contact source {source!r}; expected one of "
            f"{sorted(SOURCE_TRUST)}"
        )

    try:
        _table.update_item(
            Key={
                "PK": _project_pk(project_id),
                "SK": f"{CONTACT_SK_PREFIX}{normalised}",
            },
            UpdateExpression=(
                "SET supplier_name = :supplier_name, email = :email, "
                "#src = :source, source_trust = :trust, updated_at = :now"
            ),
            # Equal trust is allowed through, so a fresh sighting from
            # the same kind of source refreshes a changed address.
            ConditionExpression=(
                "attribute_not_exists(SK) OR source_trust <= :trust"
            ),
            ExpressionAttributeNames={"#src": "source"},
            ExpressionAttributeValues={
                ":supplier_name": supplier_name,
                # Normalised, so later comparisons against a parsed
                # sender address are plain equality tests.
                ":email": (email or "").strip().lower(),
                ":source": source,
                ":trust": trust,
                ":now": _now_iso(),
            },
        )
        return True
    except _table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(
            "Keeping existing higher-trust contact for %r in project %s "
            "(not overwriting with %s)",
            supplier_name,
            project_id,
            source,
        )
        return False


def list_contacts(project_id):
    """Return every known supplier contact for a project."""
    response = _table.query(
        KeyConditionExpression=(
            Key("PK").eq(_project_pk(project_id))
            & Key("SK").begins_with(CONTACT_SK_PREFIX)
        ),
    )
    return response.get("Items", [])
