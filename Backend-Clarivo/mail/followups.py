"""Follow-up drafting: deciding what to say to a supplier, and when.

A draft is written once and then kept, because it is text a user may
have edited and will put their own name to. That makes staleness the
central problem of this module: the invoice a draft argues about keeps
moving. A delivery note arrives and a quantity mismatch resolves itself;
the user verifies a piece of evidence and a finding becomes sendable; a
second invoice turns the case into a duplicate. A draft written before
any of that is no longer about the same dispute.

``findings_fingerprint`` is how a stored draft notices. It reduces the
part of an invoice a follow-up is actually written from to a short
string, stored on the draft as ``findings_hash``. Comparing it against
the invoice's current fingerprint answers the one question the
regeneration rule needs: has the case changed underneath this text?
"""

import hashlib
import json

# Detection's verdicts, split by whether they leave anything to chase.
# A case is open while the invoice is wrong (flagged) or while the
# evidence to judge it is missing (needs_more_info); it is settled when
# the invoice is right (clean) or when the project's own documents
# already explain the discrepancy (auto_resolved).
OPEN_STATUSES = {"flagged", "needs_more_info"}
SETTLED_STATUSES = {"clean", "auto_resolved"}

# What ingest.py writes on an invoice when a supplier answers a
# follow-up on its linked thread.
REPLY_RECEIVED = "reply_received"

# Every state followup_state can return, in the order it tests them.
# Exposed so an API serialising these does not have to restate the list.
FOLLOWUP_STATES = (
    "not_needed",
    "resolved",
    "needs_draft",
    "reply_received",
    "awaiting_reply",
    "draft_stale",
    "draft_ready",
)


def _text(value):
    """Normalise one field of a stored item to comparable text.

    Everything fingerprinted here is written by an LLM and read back out
    of DynamoDB, so a field that is usually a string arrives as None on
    a fallback and, once in a while, as a number. Coercing is cheaper
    than a fingerprint call that raises.
    """
    if value is None:
        return ""

    return str(value).strip()


def findings_fingerprint(doc_item):
    """Fingerprint the part of an invoice a follow-up is written from.

    Three things go in, being the three a draft's wording depends on:
    the ``discrepancy_status``, each finding's ``type`` and
    ``description``, and the ``resolution``.

    Three things deliberately stay out:

    * **Evidence.** Which claims verified decides whether a finding is
      *included* in the email, not what the email says about it. Folding
      it in here would make every re-verification look like a changed
      case.
    * **Finding order.** Detection is an LLM call; the same two findings
      can come back in either order, and a draft must not be treated as
      stale because of that. The pairs are sorted.
    * **``table_row_markdown``.** Presentation for the Files tab, not an
      argument to a supplier.

    The result is the first 16 hex characters of a sha256. Shortened
    because this is a change detector, not a security boundary -- 64
    bits is far past what distinguishes two states of one invoice, and a
    collision costs at most one un-regenerated draft.

    Returns a 16-character string. An invoice with no findings at all
    still fingerprints, so a case going from flagged to clean registers
    as a change like any other.
    """
    findings = doc_item.get("findings") or []

    pairs = sorted(
        (_text(finding.get("type")), _text(finding.get("description")))
        for finding in findings
        if isinstance(finding, dict)
    )

    canonical = json.dumps(
        {
            "discrepancy_status": _text(doc_item.get("discrepancy_status")),
            "findings": pairs,
            "resolution": _text(doc_item.get("resolution")),
        },
        # sort_keys so the dict's own layout cannot affect the hash, and
        # tight separators so nothing depends on json's default spacing.
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _is_sent(draft):
    """True if this draft has already gone out to the supplier.

    ``sent_at`` is the field of record, but ``status`` is checked too.
    The two are written in the same call, so disagreement means a partial
    write -- and of the two ways to be wrong about it, treating a sent
    draft as unsent is the one that mails a supplier twice.
    """
    if not draft:
        return False

    return bool(draft.get("sent_at")) or draft.get("status") == "sent"


def followup_state(doc_item, draft):
    """Where this invoice stands in its follow-up conversation.

    Derived on every read from the invoice record and its draft, never
    stored. Two sources of truth for the same thing would drift the first
    time detection re-ran without the draft being touched, and the whole
    point of the fingerprint is to notice exactly that.

    *draft* is the ``DRAFT#`` item for this invoice, or ``None``.

    Returns one of:

    ``not_needed``
        Nothing to chase. Either the invoice is settled and no follow-up
        was ever sent, or -- see below -- there is no verdict to act on.
    ``resolved``
        A follow-up went out and the invoice has since come good. The
        conversation worked; the draft stays as the record of it.
    ``needs_draft``
        An open case with no draft yet. This is what the generator picks
        up.
    ``reply_received``
        The supplier answered and the case is still open: their answer
        did not settle it, so a further round is due.
    ``awaiting_reply``
        Sent, and nothing has come back yet.
    ``draft_stale``
        An unsent draft written from findings that have since changed.
        Whether it is regenerated depends on ``edited`` -- that decision
        belongs to the caller, not here.
    ``draft_ready``
        An unsent draft that still matches the case. Waiting on the user.

    The order matters, and it is the order above. A sent draft on an
    invoice that has since come good is ``resolved``, not
    ``awaiting_reply``; an open case after a reply is ``reply_received``,
    not ``awaiting_reply``.

    A document matching none of the seven -- most often one detection
    has not judged yet, and otherwise one with no draft caught between a
    Part 8 reset and its re-check -- comes back ``not_needed``, which is
    the truth at that moment: there is no verdict to follow up. Nothing
    is cached, so it corrects itself as soon as detection writes one.
    """
    status = _text(doc_item.get("discrepancy_status"))
    sent = _is_sent(draft)

    if not sent and status in SETTLED_STATUSES:
        return "not_needed"

    if sent and status in SETTLED_STATUSES:
        return "resolved"

    if status in OPEN_STATUSES and not draft:
        return "needs_draft"

    if _text(doc_item.get("followup_status")) == REPLY_RECEIVED and status in OPEN_STATUSES:
        return "reply_received"

    # Reaching here with a reply already recorded means the invoice is
    # between a reset and its re-check: the supplier answered, detection
    # has not re-judged yet, and the verdict is momentarily blank. The
    # conversation is still in flight either way, so it stays visible
    # here rather than falling through to not_needed.
    if sent:
        return "awaiting_reply"

    if draft and draft.get("findings_hash") != findings_fingerprint(doc_item):
        return "draft_stale"

    if draft:
        return "draft_ready"

    return "not_needed"
