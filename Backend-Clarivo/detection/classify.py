import json
import os
import re

from google import genai
from google.genai import types
from documents.dynamo import get_document, update_document_classification

# Initialize the Gemini client. It will automatically pick up GEMINI_API_KEY from the environment.
gemini_client = genai.Client()

# Values a model reaches for when it has nothing to give. Treated as
# "not found", so the literal word "null" never becomes a supplier name
# or the key of a contact.
_PLACEHOLDER_VALUES = {"", "null", "none", "n/a", "na", "unknown", "not found", "-"}

# Deliberately loose: this only has to reject the obvious non-address.
# The real guard against an invented address is _appears_in_body.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The issuer rules, shared verbatim between full classification and the
# backfill_supplier_emails command, so that two prompts cannot drift
# apart on the question that matters most here: which of the two parties
# named on a document actually raised it.
SUPPLIER_FIELD_RULES = (
    "- supplier: the name of the business that ISSUED the document - the seller on an invoice, "
    "the sender of a delivery note. This is NOT the buyer or the customer named on it. A document "
    "usually names both parties; pick the one that raised it. Use null if it is not clearly stated.\n"
    "- supplier_email: that issuing business's email address, exactly as printed in the document. "
    "Use null if no email address for the issuer is printed. Never return the buyer's or customer's "
    "email address, and never construct, guess or complete an address that does not literally appear "
    "in the text."
)


def _strip_code_fences(text):
    """Return the JSON inside a ```json ... ``` wrapper, if there is one.

    response_mime_type="application/json" normally rules this out, but a
    fenced block is the one way the output reliably drifts, and a whole
    classification pass is not worth losing to three backticks.
    """
    cleaned = (text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned

    # Drop the opening fence with its optional language tag, then the
    # closing fence.
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return cleaned.strip()


def _clean_supplier_name(value):
    """Reduce the model's supplier field to a name, or None."""
    if not isinstance(value, str):
        return None

    name = value.strip()
    if name.lower() in _PLACEHOLDER_VALUES:
        return None

    return name


def _appears_in_body(email, body):
    """True if *email* is actually printed in *body*.

    The prompt forbids constructing an address that is not in the
    document; this is that rule enforced rather than merely asked for.
    Both sides are compared with all whitespace removed, so an address
    that extraction broke across a line still matches.
    """
    squashed_body = re.sub(r"\s+", "", body or "").lower()
    return email in squashed_body


def _clean_supplier_email(value, body):
    """Reduce the model's supplier_email field to an address, or None.

    Stored lowercased and stripped rather than verbatim, so that later
    comparisons — against a parsed sender address, or against a stored
    contact — stay plain equality tests. upsert_contact normalises the
    same way.
    """
    if not isinstance(value, str):
        return None

    email = value.strip().lower()
    if email in _PLACEHOLDER_VALUES:
        return None

    if not _EMAIL_SHAPE.match(email):
        print(f"[CLASSIFY] WARNING: Discarding malformed supplier_email '{email}'.")
        return None

    if not _appears_in_body(email, body):
        print(
            f"[CLASSIFY] WARNING: Discarding supplier_email '{email}' - it does "
            "not appear in the document text."
        )
        return None

    return email


def extract_supplier_details(body):
    """Read just the issuer and their email off a document body.

    A deliberately narrower call than ``classify_document``: it asks only
    who issued the document, and writes nothing. It exists for the
    ``backfill_supplier_emails`` command, which has to fill in supplier
    details on documents classified before those fields existed *without*
    re-classifying them -- a fresh classification would fire the Part 8
    resets and send a whole project's checked invoices back through
    detection.

    Returns a ``(supplier, supplier_email)`` tuple, either element
    possibly ``None``. Raises on an API or parse failure, so a caller can
    tell "nothing usable printed on the document" apart from "the call
    did not work".
    """
    system_instruction = (
        "You read business documents and identify which business issued them.\n\n"
        + SUPPLIER_FIELD_RULES
        + "\n\n"
        "Return a JSON object strictly matching this format:\n"
        '{\n  "supplier": "Name of the issuing business, or null if not found",\n'
        '  "supplier_email": "Issuer email exactly as printed, or null if none is printed"\n}\n'
        "Do not include any other text or markdown fences."
    )

    response = gemini_client.models.generate_content(
        model='gemini-3.5-flash-lite',
        contents=body,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.2,
            response_mime_type="application/json"
        )
    )

    parsed = json.loads(_strip_code_fences(response.text))
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")

    return (
        _clean_supplier_name(parsed.get("supplier")),
        _clean_supplier_email(parsed.get("supplier_email"), body),
    )


def classify_document(project_id, doc_id):
    """
    Fetches the document from DynamoDB, calls Gemini to classify its intent based on the text,
    and updates the DynamoDB record with doc_type, supplier, supplier_email and a 'classified' status.
    """
    print(f"\n--- [CLASSIFY] Starting classification for Doc {doc_id} (Project {project_id}) ---")
    doc_item = get_document(project_id, doc_id)
    if not doc_item:
        print(f"[CLASSIFY] ERROR: Document {doc_id} not found in DynamoDB.")
        raise ValueError(f"Document {doc_id} for project {project_id} not found in DynamoDB.")

    body = doc_item.get("body", "")
    if not body:
        print(f"[CLASSIFY] WARNING: Document body is empty. Defaulting to 'other'.")
        update_document_classification(project_id, doc_id, "other", "classified")
        return "other"

    system_instruction = (
        "You are an expert document classifier for a business application. Your main aim is to carefully detect "
        "if a given document is an 'invoice' or not. Then consider the other options. "
        "Based on the content and intent of the document, you must classify it as exactly one of the following five types:\n"
        "1. invoice: A document requesting payment for goods or services provided.\n"
        "2. order: A document requesting goods or services (e.g., purchase order, work order).\n"
        "3. delivery: A document confirming the delivery of goods (e.g., delivery note, receipt, packing slip).\n"
        "4. governing: A document that sets baseline terms rather than recording a specific transaction "
        "(e.g., rate agreement, contract, bill of quantities).\n"
        "5. other: Any document that does not cleanly fit into the above categories.\n\n"
        "You must also identify who issued the document:\n"
        + SUPPLIER_FIELD_RULES
        + "\n\n"
        "Return a JSON object strictly matching this format:\n"
        '{\n  "doc_type": "invoice|order|delivery|governing|other",\n'
        '  "supplier": "Name of the issuing business, or null if not found",\n'
        '  "supplier_email": "Issuer email exactly as printed, or null if none is printed"\n}\n'
        "Do not include any other text or markdown fences."
    )

    try:
        print(f"[CLASSIFY] Calling Gemini for intent classification...")
        response = gemini_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=body,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
                response_mime_type="application/json"
            )
        )

        raw = _strip_code_fences(response.text)
        print(f"[CLASSIFY] Gemini raw response: '{raw}'")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")

        result_text = str(parsed.get("doc_type") or "other").lower()
        supplier_name = _clean_supplier_name(parsed.get("supplier"))
        supplier_email = _clean_supplier_email(parsed.get("supplier_email"), body)

        # Clean up any trailing punctuation just in case
        result_text = ''.join(c for c in result_text if c.isalnum())

        allowed_types = {"invoice", "order", "delivery", "governing", "other"}
        if result_text not in allowed_types:
            print(f"[CLASSIFY] WARNING: Response '{result_text}' not in allowed types. Defaulting to 'other'.")
            result_text = "other"
        else:
            print(
                f"[CLASSIFY] SUCCESS: Parsed type as '{result_text}', supplier as "
                f"'{supplier_name}', supplier email as '{supplier_email}'."
            )

    except Exception as e:
        print(f"[CLASSIFY] ERROR: Gemini API error during classification: {e}")
        result_text = "other"
        supplier_name = None
        supplier_email = None

    print(
        f"[CLASSIFY] Updating DynamoDB with doc_type='{result_text}', supplier='{supplier_name}', "
        f"supplier_email='{supplier_email}' and status='classified'"
    )
    update_document_classification(
        project_id,
        doc_id,
        result_text,
        "classified",
        supplier_name,
        supplier_email,
    )

    if supplier_name and supplier_email:
        # An address read out of a PDF is the weakest kind of contact, so
        # it goes in at source="document": the trust order inside
        # upsert_contact leaves an address learned from a real sender, or
        # one the user typed, exactly where it was.
        # Imported here rather than at module level, for the same reason
        # as the import further down.
        from mail.storage import upsert_contact

        try:
            if upsert_contact(project_id, supplier_name, supplier_email, source="document"):
                print(f"[CLASSIFY] Stored contact for '{supplier_name}': {supplier_email}")
        except Exception as e:
            # A contact only saves the user typing an address into a
            # follow-up later; losing one must not fail the
            # classification that found it.
            print(f"[CLASSIFY] WARNING: Could not store contact for '{supplier_name}': {e}")

    if result_text in ("order", "delivery", "governing"):
        # New evidence about this supplier invalidates any verdict already
        # reached without it.  Shared with email ingestion, which does the
        # same thing when a supplier replies.
        # Imported here rather than at module level: detection.services
        # pulls in documents.services, which imports this module back.
        from detection.services import reset_supplier_checked_invoices

        reset_supplier_checked_invoices(
            project_id,
            supplier_name,
            reason=f"a new {result_text} document",
        )

    print(f"--- [CLASSIFY] Complete ---")
    return result_text
