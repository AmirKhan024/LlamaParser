"""Unit tests for validate.py's currency handling (no network, no DB).

Bug 3: BaseClaim.currency defaulted to "INR" whenever the model gave
none at all, so a dollar bill silently became a rupee claim with no
warning. Now a missing currency is inferred from the document's own
markdown (detect_currency_from_markdown) when unambiguous, and left
None -- with a warning, via check_completeness -- when it can't be
told (zero or several currency symbols found).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import DocumentType
from validate import build_claim, check_completeness, detect_currency_from_markdown


def test_detect_currency_single_symbol_or_code():
    assert detect_currency_from_markdown("Total: $780.75") == "USD"
    assert detect_currency_from_markdown("Total due: 780.75 USD") == "USD"
    assert detect_currency_from_markdown("Total: €45.00") == "EUR"
    assert detect_currency_from_markdown("Total: £10.00") == "GBP"
    assert detect_currency_from_markdown("Total: Rs. 500") == "INR"
    assert detect_currency_from_markdown("Total: ₹500") == "INR"
    assert detect_currency_from_markdown("Total: 500 INR") == "INR"


def test_detect_currency_none_found_is_ambiguous():
    assert detect_currency_from_markdown("Total: 500 quatloos") is None
    assert detect_currency_from_markdown("") is None


def test_detect_currency_multiple_found_is_ambiguous():
    # e.g. an FX conversion note mentioning two currencies -- never guess
    assert detect_currency_from_markdown("Was $50, now Rs. 4000 after conversion") is None


def test_build_claim_infers_currency_from_markdown_when_model_gives_none():
    raw_fields = {"document_type": "generic_receipt", "amount": "780.75"}  # no "currency" key at all
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "Grand Plaza Hotel\nTotal: $780.75")
    assert claim.currency == "USD"


def test_build_claim_leaves_currency_null_when_markdown_is_ambiguous_too():
    raw_fields = {"document_type": "generic_receipt", "amount": "780.75"}
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "no currency symbols in here at all")
    assert claim.currency is None


def test_build_claim_normalizes_an_explicit_dollar_sign_from_the_model():
    raw_fields = {"document_type": "generic_receipt", "amount": "780.75", "currency": "$"}
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "")
    assert claim.currency == "USD"  # via the extended CURRENCY_MAP, not left as the literal "$"


def test_check_completeness_warns_when_currency_is_null():
    claim_dict = {"currency": None, "document_type": "generic_receipt"}
    warnings = check_completeness("", claim_dict, "generic_receipt")
    assert "Couldn't tell which currency this bill is in." in warnings


def test_check_completeness_no_currency_warning_once_known():
    claim_dict = {"currency": "USD", "document_type": "generic_receipt"}
    warnings = check_completeness("", claim_dict, "generic_receipt")
    assert warnings == []


def test_check_completeness_skips_currency_warning_for_approval_correspondence():
    # an email has no amount to be ambiguous about -- would otherwise
    # warn on every approval email, since none of them mention currency
    claim_dict = {"currency": None, "document_type": "approval_correspondence"}
    warnings = check_completeness("", claim_dict, "approval_correspondence")
    assert warnings == []
