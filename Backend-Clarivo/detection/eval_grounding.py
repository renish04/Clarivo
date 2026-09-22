"""
Evaluation harness for grounding verification.

A verifier is a classifier, so it has a precision and a recall, and
tuning it without measuring them is guesswork.  This scores the verifier
against labelled fixtures and prints a confusion matrix.

Run it directly — it imports nothing from Django, AWS or Gemini::

    python detection/eval_grounding.py

Watch precision on the ``verified`` class in particular.  A verifier
that passes everything is exactly as useless as one that fails
everything, and it is the direction you drift while loosening
thresholds to stop false negatives.

The fixtures below are seeds, not a benchmark.  They cover the failure
modes that matter and include the real case from the WELDRO invoice,
but a real measurement needs labelled evidence from your own synthetic
document set.  Add cases to FIXTURES as you meet them — a verifier bug
that does not have a fixture will come back.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detection.grounding import (  # noqa: E402
    CONTRADICTED,
    PARTIAL,
    UNRESOLVED_SOURCE,
    UNSUPPORTED,
    VERIFIED,
    verify_evidence,
)

# A realistic OCR rendering of the invoice, columns and all.
INVOICE_TEXT = """TAX INVOICE
No  Description               Qty  U/Price  Amount
1   WELDRO PICKLING GEL 1KG    2    43.00    86.00
2   ARGON GAS REFILL           5    65.10   325.50

Sub Total (RM)        411.50
Total GST (RM)         24.69
Total (RM)            436.20"""

ORDER_TEXT = """PURCHASE ORDER PO-4471
Item                        Qty   Rate    Amount
WELDRO PICKLING GEL 1KG      2    38.00    76.00
ARGON GAS REFILL             5    65.10   325.50

Subtotal          401.50
Add GST (6%)       24.09
Total (RM)        425.59"""

DOCS = {"Invoice.jpg": INVOICE_TEXT, "PO-4471.pdf": ORDER_TEXT}


FIXTURES = [
    # -- correctly cited, should verify -----------------------------
    {
        "label": "line item cited verbatim from the invoice",
        "source_doc": "Invoice.jpg",
        "quote": "WELDRO PICKLING GEL 1KG    2    43.00    86.00",
        "fields": {"item": "WELDRO PICKLING GEL 1KG", "quantity": 2,
                   "unit_price": 43.00, "amount": 86.00},
        "expected": VERIFIED,
    },
    {
        "label": "same values, reflowed spacing (OCR column drift)",
        "source_doc": "Invoice.jpg",
        "quote": "WELDRO PICKLING GEL 1KG 2 43.00 86.00",
        "fields": {"item": "WELDRO PICKLING GEL 1KG", "quantity": 2,
                   "unit_price": 43.00, "amount": 86.00},
        "expected": VERIFIED,
    },
    {
        "label": "totals block from the order",
        "source_doc": "PO-4471.pdf",
        "quote": "Subtotal          401.50",
        "fields": {"subtotal": 401.50, "tax": 24.09, "total": 425.59},
        "expected": VERIFIED,
    },
    {
        "label": "item name with OCR damage still matches",
        "source_doc": "Invoice.jpg",
        "quote": "WELDR0 PICKLING GEL 1KG    2    43.00    86.00",
        "fields": {"item": "WELDR0 PICKLING GEL 1KG", "quantity": 2,
                   "unit_price": 43.00, "amount": 86.00},
        "expected": VERIFIED,
    },
    {
        "label": "currency-prefixed values parse and verify",
        "source_doc": "Invoice.jpg",
        "quote": "Total (RM)            436.20",
        "fields": {"total": "RM 436.20"},
        "expected": VERIFIED,
    },

    # -- the real failure from the console output --------------------
    {
        "label": "fabricated quantity (5 x 38.00 != 76.00)",
        "source_doc": "PO-4471.pdf",
        "quote": "WELDRO PICKLING GEL 1KG      2    38.00    76.00",
        "fields": {"item": "WELDRO PICKLING GEL 1KG", "quantity": 5,
                   "unit_price": 38.00, "amount": 76.00},
        "expected": CONTRADICTED,
    },

    # -- hallucination and misattribution ----------------------------
    {
        "label": "real row quoted, but a value misread off it",
        "source_doc": "Invoice.jpg",
        "quote": "WELDRO PICKLING GEL 1KG    2    43.00    86.00",
        "fields": {"item": "WELDRO PICKLING GEL 1KG", "quantity": 2,
                   "unit_price": 47.00},
        "expected": CONTRADICTED,
    },
    {
        "label": "invoice rate attributed to the order",
        "source_doc": "PO-4471.pdf",
        "quote": "WELDRO PICKLING GEL 1KG      2    43.00    86.00",
        "fields": {"item": "WELDRO PICKLING GEL 1KG", "quantity": 2,
                   "unit_price": 43.00, "amount": 86.00},
        "expected": CONTRADICTED,
    },
    {
        "label": "values invented entirely",
        "source_doc": "Invoice.jpg",
        "quote": "SUPER WIDGET DELUXE   9   11.11   99.99",
        "fields": {"item": "SUPER WIDGET DELUXE", "quantity": 9,
                   "unit_price": 11.11, "amount": 99.99},
        "expected": UNSUPPORTED,
    },
    {
        "label": "cites a document not in the context",
        "source_doc": "DeliveryNote-99.pdf",
        "quote": "WELDRO PICKLING GEL 1KG 2 43.00 86.00",
        "fields": {"quantity": 2, "unit_price": 43.00, "amount": 86.00},
        "expected": UNRESOLVED_SOURCE,
    },

    # -- partial ------------------------------------------------------
    {
        "label": "one real value, one invented, quote not locatable",
        "source_doc": "Invoice.jpg",
        "quote": "some paraphrase that appears nowhere in the document at all",
        "fields": {"amount": 86.00, "unit_price": 12.34},
        "expected": PARTIAL,
    },
]


def run():
    results = []
    confusion = Counter()

    print("=" * 74)
    print("GROUNDING VERIFIER EVALUATION")
    print("=" * 74)

    for fixture in FIXTURES:
        evidence = {
            "source_doc": fixture["source_doc"],
            "quote": fixture["quote"],
            "fields": fixture["fields"],
        }
        doc_text = DOCS.get(fixture["source_doc"])
        actual = verify_evidence(evidence, doc_text)["status"]
        expected = fixture["expected"]
        ok = actual == expected

        results.append((ok, fixture["label"], expected, actual))
        confusion[(expected, actual)] += 1

        mark = "PASS" if ok else "FAIL"
        print(f"\n[{mark}] {fixture['label']}")
        print(f"       expected={expected}  actual={actual}")

    passed = sum(1 for ok, *_ in results if ok)
    total = len(results)

    print("\n" + "=" * 74)
    print(f"ACCURACY: {passed}/{total}")

    # Precision and recall for the class that actually gates behaviour:
    # a false 'verified' is what lets a hallucinated case auto-resolve.
    true_pos = sum(1 for ok, _, exp, act in results if exp == VERIFIED and act == VERIFIED)
    false_pos = sum(1 for ok, _, exp, act in results if exp != VERIFIED and act == VERIFIED)
    false_neg = sum(1 for ok, _, exp, act in results if exp == VERIFIED and act != VERIFIED)

    precision = true_pos / (true_pos + false_pos) if (true_pos + false_pos) else 0.0
    recall = true_pos / (true_pos + false_neg) if (true_pos + false_neg) else 0.0

    print(f"\n'verified' class — precision: {precision:.2f}   recall: {recall:.2f}")
    print(f"  true positives {true_pos}   false positives {false_pos}   false negatives {false_neg}")

    if false_pos:
        print("\n  WARNING: a false 'verified' is the dangerous direction — it is what")
        print("  lets an unchecked claim auto-resolve and inflate the touchless rate.")

    print("\nConfusion (expected -> actual):")
    for (expected, actual), count in sorted(confusion.items()):
        flag = "" if expected == actual else "   <-- mismatch"
        print(f"  {expected:>18} -> {actual:<18} {count}{flag}")

    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(run())
