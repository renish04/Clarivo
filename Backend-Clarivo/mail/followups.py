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

``followup_state`` reads that comparison, plus the invoice's verdict and
whether a draft has gone out, into one word describing where the
conversation stands. ``generate_draft`` acts on it: it writes a draft
when one is wanted, leaves one alone when it is not, and opens a new
round when a supplier's reply failed to settle the case.
"""

import hashlib
import json
import logging
import re

from google import genai
from google.genai import types

from documents.dynamo import get_document, list_documents
from projects.models import Project

from .prompts import FOLLOWUP_SYSTEM_PROMPT
from .storage import (
    SOURCE_TRUST,
    get_draft,
    list_contacts,
    list_email_records,
    normalize_supplier_name,
    put_draft,
)

logger = logging.getLogger(__name__)
gemini_client = genai.Client()

# How much of a supplier's reply goes into the drafting prompt. Stored
# bodies run to 8000 characters; the part that answers a follow-up is at
# the top, and the rest is usually the quoted thread underneath it.
REPLY_EXCERPT_CHARS = 2000

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


class DraftGenerationError(RuntimeError):
    """The model did not return a usable follow-up email.

    Raised instead of saving what came back, because a draft with an
    empty body would be stored as ``draft_ready`` -- and a ready draft is
    returned unchanged, so the failure would never be retried. Better to
    leave no draft and let the caller ask again.
    """


_RE_PREFIX = re.compile(r"^(?:\s*re\s*:\s*)+", re.IGNORECASE)


def _strip_re_prefixes(subject):
    """Remove every leading "Re:" from a subject.

    Several rounds on one thread would otherwise accumulate them, and
    "Re: Re: Re:" in a supplier's inbox reads as a system talking to
    itself.
    """
    return _RE_PREFIX.sub("", subject or "").strip()


def _parse_json_object(raw):
    """Parse a model response that should be a JSON object.

    ``response_mime_type`` makes a fenced block unlikely rather than
    impossible, and this is the last step before text goes in front of a
    supplier, so the fence is stripped rather than trusted away.
    """
    text = (raw or "").strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")

    return parsed


def _buyer_name(user):
    """The name a follow-up is signed with."""
    if not user:
        return ""

    return (user.get_full_name() or "").strip() or (user.get_username() or "").strip()


def _finding_label(finding):
    """How a finding is referred to when it is not in the email."""
    return (
        _text(finding.get("description"))
        or _text(finding.get("type"))
        or "unlabelled finding"
    )


def _split_findings(doc_item):
    """Separate the findings that may be sent from the ones that may not.

    A finding goes in the email only if at least one of its evidence
    entries verified -- meaning the claim was found, word for word, in
    the project's own documents. Everything else is an assertion the LLM
    could not substantiate, and putting one of those to a supplier in
    writing is the single worst thing this feature could do.

    Returns ``(included, excluded)``, where *included* is a list of
    ``(finding, verified_evidence)`` pairs -- the evidence filtered down
    to the entries that verified, so unverified claims cannot reach the
    prompt either -- and *excluded* is a list of label strings, kept to
    show the user what was held back.
    """
    included = []
    excluded = []

    for finding in doc_item.get("findings") or []:
        if not isinstance(finding, dict):
            continue

        verified = [
            evidence
            for evidence in finding.get("evidence") or []
            if isinstance(evidence, dict) and evidence.get("verified") is True
        ]

        if verified:
            included.append((finding, verified))
        else:
            excluded.append(_finding_label(finding))

    return included, excluded


def _resolve_recipient(project_id, doc_item):
    """Work out who a follow-up should go to, and on whose authority.

    Three places to look, in descending order of how much the address is
    worth. A stored contact comes first because it is the only one that
    can have been typed by a human or learned from mail that genuinely
    arrived; where several contacts normalise to the same supplier, the
    better-sourced one wins. Failing that, an address printed on one of
    the supplier's own documents -- this invoice first, then any other
    document of theirs, since a delivery note often prints an address an
    invoice does not.

    Returns ``(email, source)``, or ``("", "none")`` when nothing is
    known, which is not an error: the user is shown an empty recipient
    and fills it in.
    """
    supplier = _text(doc_item.get("supplier"))
    if not supplier:
        return "", "none"

    key = normalize_supplier_name(supplier)

    matches = [
        contact
        for contact in list_contacts(project_id)
        if normalize_supplier_name(contact.get("supplier_name")) == key
        and _text(contact.get("email"))
    ]
    if matches:
        best = max(matches, key=lambda c: SOURCE_TRUST.get(c.get("source"), 0))
        source = best.get("source")
        return (
            _text(best.get("email")).lower(),
            # A contact written by an older build, or by hand, could
            # carry a source this draft's vocabulary does not have.
            source if source in SOURCE_TRUST else "document",
        )

    own_email = _text(doc_item.get("supplier_email"))
    if own_email:
        return own_email.lower(), "document"

    for other in list_documents(project_id):
        if normalize_supplier_name(other.get("supplier")) != key:
            continue

        email = _text(other.get("supplier_email"))
        if email:
            return email.lower(), "document"

    return "", "none"


def _latest_inbound_reply(project_id, thread_id):
    """The supplier's most recent message on this follow-up's thread.

    Inbound only: the thread also holds what Clarivo sent, and quoting
    our own last email back to the model would have it answer itself.
    """
    if not thread_id:
        return ""

    # list_email_records returns newest first, by received_at.
    for record in list_email_records(project_id):
        if record.get("thread_id") != thread_id:
            continue
        if record.get("direction") != "inbound":
            continue

        return _text(record.get("body_text"))[:REPLY_EXCERPT_CHARS]

    return ""


def _build_user_message(
    project_name,
    buyer_name,
    doc_item,
    followup_type,
    round_number,
    included,
    excluded,
    supplier_reply,
):
    """Assemble the facts the email may be written from.

    Everything the model is allowed to say has to be in here, because
    the system prompt forbids it inventing anything else: an invoice
    number it is not given is one it must write around.
    """
    sections = [
        "\n".join(
            [
                f"Project name: {project_name}",
                f"Buyer name: {buyer_name}",
                f"Supplier name: {_text(doc_item.get('supplier')) or 'not recorded'}",
                f"Invoice filename: {_text(doc_item.get('filename')) or 'not recorded'}",
                f"Follow-up type: {followup_type}",
            ]
        )
    ]

    if round_number > 1:
        sections.append(
            f"This is round {round_number} of this conversation. A previous "
            "follow-up about this invoice has already been sent, and the "
            "supplier's reply did not settle it."
        )

    if included:
        lines = ["Verified findings:"]
        for index, (finding, evidence) in enumerate(included, start=1):
            lines.append(f"{index}. {_text(finding.get('type')) or 'other'}: {_finding_label(finding)}")
            for entry in evidence:
                source_doc = _text(entry.get("source_doc")) or "an unnamed document"
                lines.append(f'   - "{_text(entry.get("claim"))}" (from {source_doc})')
        sections.append("\n".join(lines))
    else:
        # Reached when every finding failed verification, or when the
        # invoice is needs_more_info and detection recorded no findings
        # at all. Either way there is nothing substantiated to put to the
        # supplier, so the email asks them to confirm the invoice rather
        # than alleging anything.
        sections.append(
            "There are no verified findings to put to the supplier. Ask them "
            "to confirm the details of this invoice -- the rates, quantities "
            "and taxes billed -- so they can be checked against our records. "
            "Do not describe any discrepancy, and do not suggest the invoice "
            "is wrong."
        )

    if excluded:
        # Named, not described: the model needs to know these exist so it
        # does not claim the invoice is fully in order, but it must not
        # repeat an allegation nothing supports.
        sections.append(
            f"For your information only -- {len(excluded)} further concern(s) "
            "could not be substantiated from our own documents. Do not "
            "mention them, describe them, or allude to them in the email."
        )

    resolution = _text(doc_item.get("resolution"))
    if resolution and followup_type == "information_request":
        sections.append(f"Information still needed:\n{resolution}")

    if supplier_reply:
        sections.append(
            "The supplier's most recent reply, in full:\n"
            "---\n"
            f"{supplier_reply}\n"
            "---"
        )

    return "\n\n".join(sections)


def generate_draft(project_id, doc_id, user, force=False):
    """Write, or decline to rewrite, the follow-up email for one invoice.

    The rule this implements is that a draft is written once and then
    left alone. It is text a user may have edited and will put their own
    name to, so it is replaced only when the user asks (*force*), or when
    the case moved underneath a draft they have not touched.

    What happens for each state:

    * no draft -- write one.
    * ``draft_ready`` -- returned unchanged; it still matches the case.
    * edited, any state -- returned unchanged unless *force*. Losing
      someone's typing to a background job is not a thing this does.
    * ``draft_stale``, unedited -- rewritten in place, keeping its round
      and its thread.
    * ``awaiting_reply`` -- returned unchanged, *even with force*. The
      email is already with the supplier; rewriting the item would erase
      the record of having sent it.
    * ``reply_received`` on a sent draft -- a new round. The thread is
      kept so the reply lands in the same conversation, the round number
      goes up, and the sent fields are cleared.

    *user* is the person asking, used to sign the email; the project
    owner stands in if there is none.

    Raises ``ValueError`` if the invoice is not there or needs no
    follow-up, and ``DraftGenerationError`` if the model's answer cannot
    be used.
    """
    doc_item = get_document(project_id, doc_id)
    if not doc_item:
        raise ValueError(f"document {doc_id} not found in project {project_id}")

    status = _text(doc_item.get("discrepancy_status"))
    if status not in OPEN_STATUSES:
        raise ValueError("no follow-up needed")

    existing = get_draft(project_id, doc_id)
    state = followup_state(doc_item, existing)

    round_number = 1
    gmail_thread_id = ""
    created_at = ""
    previous_subject = ""

    if existing:
        # Carried forward whether this is a new round or a rewrite of an
        # unsent draft: both continue the same conversation.
        round_number = int(existing.get("round") or 1)
        gmail_thread_id = _text(existing.get("gmail_thread_id"))
        created_at = _text(existing.get("created_at"))
        previous_subject = existing.get("subject") or ""

        if _is_sent(existing):
            if state != "reply_received":
                logger.info(
                    "Follow-up for invoice %s in project %s is already sent and "
                    "awaiting a reply; leaving it alone",
                    doc_id,
                    project_id,
                )
                return existing

            round_number += 1
        elif not force and (state == "draft_ready" or existing.get("edited")):
            logger.info(
                "Keeping the existing follow-up draft for invoice %s in project "
                "%s (state=%s, edited=%s)",
                doc_id,
                project_id,
                state,
                bool(existing.get("edited")),
            )
            return existing

    followup_type = "dispute" if status == "flagged" else "information_request"

    included, excluded = _split_findings(doc_item)
    if followup_type == "dispute" and not included:
        # A dispute with nothing provable behind it is not a dispute.
        # Still worth an email -- something looked wrong -- but it can
        # only ask, not allege.
        logger.info(
            "No verified findings on invoice %s in project %s; drafting an "
            "information request instead of a dispute",
            doc_id,
            project_id,
        )
        followup_type = "information_request"

    project = Project.objects.filter(id=project_id).first()
    if not project:
        raise ValueError(f"project {project_id} not found")

    buyer_name = _buyer_name(user) or _buyer_name(project.owner) or "Accounts Payable"

    to, to_source = _resolve_recipient(project_id, doc_item)
    supplier_reply = _latest_inbound_reply(project_id, gmail_thread_id)

    user_message = _build_user_message(
        project_name=project.name,
        buyer_name=buyer_name,
        doc_item=doc_item,
        followup_type=followup_type,
        round_number=round_number,
        included=included,
        excluded=excluded,
        supplier_reply=supplier_reply,
    )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=FOLLOWUP_SYSTEM_PROMPT,
                temperature=0.4,
                response_mime_type="application/json",
            ),
        )
        parsed = _parse_json_object(response.text)
    except Exception as exc:
        raise DraftGenerationError(
            f"Could not generate a follow-up for invoice {doc_id}: {exc}"
        ) from exc

    body = _text(parsed.get("body"))
    if not body:
        raise DraftGenerationError(
            f"The model returned no email body for invoice {doc_id}"
        )

    subject = _text(parsed.get("subject")) or (
        f"[{project.name}] {_text(doc_item.get('filename')) or 'Invoice'}"
    )

    if round_number > 1:
        # Gmail threads on the subject as well as the headers, so a later
        # round has to carry the first one's subject with a "Re:" on it.
        # The previous draft's subject is what the supplier actually saw;
        # this round's generated one is only a fallback.
        base = _strip_re_prefixes(previous_subject) or _strip_re_prefixes(subject)
        subject = f"Re: {base}"

    draft = put_draft(
        project_id,
        doc_id,
        {
            "round": round_number,
            "followup_type": followup_type,
            "to": to,
            "to_source": to_source,
            "subject": subject,
            "body": body,
            "status": "draft",
            "edited": False,
            "findings_hash": findings_fingerprint(doc_item),
            "excluded_findings": excluded,
            # put_draft is a whole-item write, so leaving the sent fields
            # out is what clears them for the new round.
            "gmail_thread_id": gmail_thread_id,
            "created_at": created_at,
        },
    )

    logger.info(
        "Drafted a %s follow-up (round %d) for invoice %s in project %s: "
        "%d finding(s) included, %d excluded, to=%r (%s)",
        followup_type,
        round_number,
        doc_id,
        project_id,
        len(included),
        len(excluded),
        to,
        to_source,
    )
    return draft
