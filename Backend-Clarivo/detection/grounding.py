"""
Grounding verification — does the evidence the model cited actually say
what the model claims it says?

This checks *faithfulness*, not truth.  The question is only ever "is
this supported by the document it was attributed to", never "is this
correct in the world".  An invoice that is itself wrong should produce
perfectly grounded findings — detecting that the invoice is wrong is the
job of the layer above.

The approach is a cascade of cheap, exact checks rather than one fuzzy
similarity score, because the claims here are structured records wearing
a sentence costume.  A line item carries a quantity, a unit price and an
amount, which means it is *self-checking*: quantity x unit_price must
equal amount.  Financial documents hand you redundant constraints for
free, and arithmetic catches fabricated numbers that no amount of
semantic similarity ever will — "100 widgets" and "200 widgets" embed
almost identically.

Four checks, cheapest first:

1. Numeric fields — parsed to Decimal, compared against every number in
   the cited document with a one-cent tolerance.
2. Entity      — the item name, fuzzy-matched to survive OCR noise.
3. Quote       — the verbatim span, located in the cited document.
4. Arithmetic  — internal consistency of the fields themselves.

The verdict is graded, not boolean, because "I could not find it" and "I
found it and it says something different" are different facts and want
different handling downstream.

Deliberately dependency-free: pure standard library, no Django, no AWS,
no network.  That makes the whole thing testable on its own — see
``eval_grounding.py``.
"""

import re
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

# --- verdicts ---------------------------------------------------------

VERIFIED = "verified"           # values found in the cited doc, arithmetic holds
PARTIAL = "partial"             # some values found, some not
UNSUPPORTED = "unsupported"     # values not found       (extrinsic hallucination)
CONTRADICTED = "contradicted"   # doc says something else (intrinsic hallucination)
UNRESOLVED_SOURCE = "unresolved_source"  # cited a document we do not have

# Money is compared to the cent.  Anything looser starts accepting the
# rounding errors that are exactly what we are looking for.
VALUE_TOLERANCE = Decimal("0.01")

# Similarity floors for the fuzzy checks.  Tuned to tolerate OCR damage
# (a wrong character or two in a product name) without accepting a
# genuinely different string.
ENTITY_MATCH_THRESHOLD = 0.85
QUOTE_MATCH_THRESHOLD = 0.75

# Numeric field names the arithmetic checks understand.
_LINE_ITEM_FIELDS = ("quantity", "unit_price", "amount")
_TOTALS_FIELDS = ("subtotal", "tax", "total")

# Currency markers stripped before parsing a number.  The original
# implementation stripped only $ £ € — a Western-currency assumption
# these documents do not hold, which is why every RM-denominated claim
# failed.
_CURRENCY_PATTERN = re.compile(
    r"(?:rm|rs\.?|inr|myr|sgd|aed|usd|eur|gbp|[$£€₹¥])",
    re.IGNORECASE,
)

# A token that might be a number, including thousands separators.
_NUMBER_TOKEN = re.compile(r"\d[\d.,]*")

# Characters OCR routinely confuses with digits.  Folded only inside a
# token already believed to be numeric, never across free text.
_OCR_DIGIT_FOLD = str.maketrans({"o": "0", "O": "0", "l": "1", "I": "1"})

_FINAL_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def parse_number(raw):
    """Parse a possibly-messy money or quantity string into a Decimal.

    Handles currency prefixes, thousands separators in either the
    Anglo (``1,234.56``) or European (``1.234,56``) convention,
    parenthesised negatives, and the digit-shaped characters OCR likes
    to emit.  Returns ``None`` if the input is not recognisably numeric.
    """
    if raw is None:
        return None

    # Already a number (the model returns real JSON numbers for fields).
    if isinstance(raw, (int, Decimal)):
        return Decimal(str(raw))
    if isinstance(raw, float):
        # str() first — Decimal(float) would carry binary float noise.
        return Decimal(str(raw))

    text = str(raw).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    text = _CURRENCY_PATTERN.sub("", text)
    text = re.sub(r"[\s_]", "", text)
    text = text.translate(_OCR_DIGIT_FOLD)

    if text.startswith("-"):
        negative = True
        text = text[1:]

    if not text:
        return None

    # Decide which separator is the decimal point.  Whichever appears
    # last is the decimal separator — but a trailing group of three
    # digits is a thousands group, not a fraction.
    last_comma = text.rfind(",")
    last_dot = text.rfind(".")

    if last_comma > last_dot:
        if len(text) - last_comma - 1 == 2:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", "")

    if not _FINAL_NUMBER.match(text):
        return None

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    return -value if negative else value


def extract_numbers(text):
    """Every number appearing anywhere in *text*, as Decimals.

    Used as a presence set: "does this value occur in this document".
    It deliberately also picks up dates and reference numbers, which can
    only make a check more permissive, never less — a value that is
    absent here is genuinely absent.
    """
    if not text:
        return []

    values = []
    for token in _NUMBER_TOKEN.findall(text):
        value = parse_number(token)
        if value is not None:
            values.append(value)
    return values


def value_present(value, candidates, tolerance=VALUE_TOLERANCE):
    """Is *value* among *candidates*, within *tolerance*?"""
    if value is None:
        return False
    return any(abs(value - candidate) <= tolerance for candidate in candidates)


def _normalize_for_match(text):
    """Lowercase, drop punctuation, collapse whitespace."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s.]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def best_window_match(needle, haystack):
    """Find where *needle* best matches inside *haystack*.

    Slides a word window the length of the needle across the haystack and
    keeps the highest similarity ratio.  This is what lets a claim match
    a source whose line breaks and column spacing differ — the failure
    that made plain substring matching reject correctly-cited evidence.

    Returns ``(ratio, matched_text)``, with a ratio of 0.0 when either
    side is empty.
    """
    needle_norm = _normalize_for_match(needle)
    haystack_norm = _normalize_for_match(haystack)

    if not needle_norm or not haystack_norm:
        return 0.0, ""

    # Exact containment is the cheap path and by far the common one for
    # short entity names.
    if needle_norm in haystack_norm:
        return 1.0, needle_norm

    needle_words = needle_norm.split()
    haystack_words = haystack_norm.split()
    window = len(needle_words)

    if window >= len(haystack_words):
        return SequenceMatcher(None, needle_norm, haystack_norm).ratio(), haystack_norm

    matcher = SequenceMatcher()
    matcher.set_seq2(needle_norm)

    best_ratio = 0.0
    best_text = ""

    # A couple of words of slack either side, so a window that clips the
    # phrase boundary still scores fairly.
    for start in range(0, len(haystack_words) - window + 1):
        candidate = " ".join(haystack_words[start : start + window + 2])
        matcher.set_seq1(candidate)

        # quick_ratio is an upper bound — skip the real comparison when
        # it cannot beat what we already have.
        if matcher.quick_ratio() <= best_ratio:
            continue

        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_text = candidate
            if best_ratio >= 0.999:
                break

    return best_ratio, best_text


def check_arithmetic(fields):
    """Check the claim against itself.

    A line item and a totals block each carry a redundant value that must
    follow from the others.  This needs no source document at all, and it
    is the check that catches a fabricated quantity instantly.
    """
    results = []

    quantity = parse_number(fields.get("quantity"))
    unit_price = parse_number(fields.get("unit_price"))
    amount = parse_number(fields.get("amount"))

    if quantity is not None and unit_price is not None and amount is not None:
        expected = quantity * unit_price
        results.append({
            "rule": "quantity x unit_price = amount",
            "expected": expected,
            "stated": amount,
            "ok": abs(expected - amount) <= VALUE_TOLERANCE,
        })

    subtotal = parse_number(fields.get("subtotal"))
    tax = parse_number(fields.get("tax"))
    total = parse_number(fields.get("total"))

    if subtotal is not None and tax is not None and total is not None:
        expected = subtotal + tax
        results.append({
            "rule": "subtotal + tax = total",
            # Documents round the printed total, so allow a cent either way.
            "expected": expected,
            "stated": total,
            "ok": abs(expected - total) <= Decimal("0.02"),
        })

    return results


def verify_evidence(evidence, doc_text):
    """Verify one evidence item against the text of the document it cites.

    *doc_text* must be the cited document only.  Verifying against the
    whole prompt cannot distinguish "the rate agreement says 38.00" from
    "some document somewhere says 38.00", and that distinction is the
    difference between a defensible finding and a guess.
    """
    fields = evidence.get("fields") or {}
    quote = evidence.get("quote") or ""

    if doc_text is None:
        return {
            "status": UNRESOLVED_SOURCE,
            "detail": "Cited document was not part of the retrieved context.",
            "fields": {},
            "quote": None,
            "arithmetic": [],
        }

    doc_numbers = extract_numbers(doc_text)

    # -- 1. numeric fields --------------------------------------------
    field_results = {}
    numeric_found = 0
    numeric_total = 0

    for name, raw in fields.items():
        if name == "item":
            continue
        value = parse_number(raw)
        if value is None:
            continue
        numeric_total += 1
        found = value_present(value, doc_numbers)
        if found:
            numeric_found += 1
        field_results[name] = {"value": value, "found": found}

    # -- 2. entity -----------------------------------------------------
    entity = fields.get("item") or ""
    entity_ratio = None
    if entity:
        entity_ratio, _ = best_window_match(entity, doc_text)
        field_results["item"] = {
            "value": entity,
            "found": entity_ratio >= ENTITY_MATCH_THRESHOLD,
            "ratio": round(entity_ratio, 3),
        }

    # -- 3. quote ------------------------------------------------------
    quote_result = None
    if quote:
        ratio, matched = best_window_match(quote, doc_text)
        quote_result = {
            "ratio": round(ratio, 3),
            "located": ratio >= QUOTE_MATCH_THRESHOLD,
            "matched_text": matched[:300],
        }

    # -- 4. arithmetic --------------------------------------------------
    arithmetic = check_arithmetic(fields)
    arithmetic_ok = all(check["ok"] for check in arithmetic) if arithmetic else None

    status, detail = _grade(
        numeric_found=numeric_found,
        numeric_total=numeric_total,
        entity_ratio=entity_ratio,
        quote_result=quote_result,
        arithmetic=arithmetic,
        arithmetic_ok=arithmetic_ok,
    )

    return {
        "status": status,
        "detail": detail,
        "fields": field_results,
        "quote": quote_result,
        "arithmetic": arithmetic,
    }


def _grade(numeric_found, numeric_total, entity_ratio, quote_result, arithmetic, arithmetic_ok):
    """Turn the individual check results into one graded verdict."""

    # A claim that contradicts itself is contradicted regardless of what
    # the document says — this is the fabricated-quantity case.
    if arithmetic and not arithmetic_ok:
        broken = next(check for check in arithmetic if not check["ok"])
        return CONTRADICTED, (
            f"Claim is internally inconsistent: {broken['rule']} gives "
            f"{broken['expected']} but the claim states {broken['stated']}."
        )

    quote_located = bool(quote_result and quote_result["located"])

    if numeric_total == 0:
        # Nothing numeric to check — fall back to the quote and entity.
        if quote_located:
            return VERIFIED, "Quoted text located in the cited document."
        if entity_ratio is not None and entity_ratio >= ENTITY_MATCH_THRESHOLD:
            return PARTIAL, "Entity found, but no quoted span could be located."
        return UNSUPPORTED, "Nothing in this claim could be located in the cited document."

    if numeric_found == numeric_total:
        if quote_result and not quote_located:
            return PARTIAL, (
                "All values found, but the quoted text does not match the "
                "document closely — the quote was probably paraphrased."
            )
        return VERIFIED, "All cited values found in the cited document."

    # The revealing case: the model quoted a row that genuinely exists,
    # but read numbers off it that the row does not contain.
    if quote_located and numeric_found < numeric_total:
        missing = numeric_total - numeric_found
        return CONTRADICTED, (
            f"The quoted text was located in the cited document, but "
            f"{missing} of {numeric_total} cited values do not appear in it."
        )

    if numeric_found > 0:
        return PARTIAL, (
            f"{numeric_found} of {numeric_total} cited values found in the "
            f"cited document."
        )

    return UNSUPPORTED, "None of the cited values appear in the cited document."


def verify_grounding(findings, docs_by_name):
    """Verify every evidence item in *findings* against its cited document.

    *docs_by_name* maps filename to that document's text, so each claim is
    checked against what it actually cited.

    Mutates and returns *findings*.  Each evidence item gains a
    ``grounding`` block, and the original boolean ``verified`` is kept in
    place — true only for a full pass — so existing consumers keep
    working.
    """
    print("\n--- [VERIFY GROUNDING] Starting evidence verification ---")

    for finding in findings:
        for evidence in finding.get("evidence", []) or []:
            source_doc = evidence.get("source_doc") or ""
            doc_text = _resolve_document(source_doc, docs_by_name)

            result = verify_evidence(evidence, doc_text)

            evidence["grounding"] = result
            evidence["verified"] = result["status"] == VERIFIED

            label = result["status"].upper()
            print(f"[VERIFY GROUNDING] {label}: [{source_doc}] {result['detail']}")

    print("--- [VERIFY GROUNDING] Complete ---\n")
    return findings


def _resolve_document(source_doc, docs_by_name):
    """Look up a cited filename, tolerating small differences.

    The model echoes back the filename it saw in the context header, and
    it does not always reproduce it exactly.
    """
    if not source_doc or not docs_by_name:
        return None

    if source_doc in docs_by_name:
        return docs_by_name[source_doc]

    target = source_doc.strip().lower()
    for name, text in docs_by_name.items():
        if name.strip().lower() == target:
            return text

    # Last resort: basename match, so "March/Acme/inv.pdf" still finds
    # "inv.pdf" and vice versa.
    target_base = target.rsplit("/", 1)[-1]
    for name, text in docs_by_name.items():
        if name.strip().lower().rsplit("/", 1)[-1] == target_base:
            return text

    return None


def apply_grounding_policy(result):
    """Let the verification result constrain the model's own verdict.

    Without this, grounding is decoration: the model decides the status
    before anything is checked, and the check only ever annotates.  The
    rules, in order of severity:

    * any contradicted evidence  -> needs_more_info, always
    * auto_resolved              -> requires *every* evidence item verified
    * flagged with nothing found -> needs_more_info

    The auto-resolution rule is the important one.  Auto-resolved cases
    close without a human ever seeing them and feed the touchless rate,
    so allowing one to rest on unchecked evidence would make that metric
    measure the model's confidence rather than the system's correctness.

    Returns ``(result, notes)``.
    """
    findings = result.get("findings") or []
    statuses = [
        evidence.get("grounding", {}).get("status")
        for finding in findings
        for evidence in (finding.get("evidence") or [])
    ]
    statuses = [status for status in statuses if status]

    original = result.get("status")
    notes = []

    if not statuses:
        return result, notes

    if CONTRADICTED in statuses:
        result["status"] = "needs_more_info"
        notes.append(
            "Downgraded to needs_more_info: at least one cited claim is "
            "contradicted by the document it was attributed to."
        )
    elif original == "auto_resolved" and not all(s == VERIFIED for s in statuses):
        result["status"] = "needs_more_info"
        notes.append(
            "Downgraded to needs_more_info: auto-resolution requires every "
            "piece of evidence to be verified against its source."
        )
    elif original == "flagged" and all(
        s in (UNSUPPORTED, UNRESOLVED_SOURCE) for s in statuses
    ):
        result["status"] = "needs_more_info"
        notes.append(
            "Downgraded to needs_more_info: none of the cited evidence could "
            "be located in the documents it was attributed to."
        )

    for note in notes:
        print(f"[GROUNDING POLICY] {note}")

    return result, notes
