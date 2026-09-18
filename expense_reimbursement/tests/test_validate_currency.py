"""Unit tests for validate.py's currency handling (no network, no DB).

Bug 3: BaseClaim.currency defaulted to "INR" whenever the model gave
none at all, so a dollar bill silently became a rupee claim with no
warning. A missing currency is inferred from the document's own
markdown (detect_currency_from_markdown) when unambiguous.

Prior-prompt item 3: the fix above was itself too noisy -- a document
with NO currency marker at all (e.g. a plain INR conveyance form that
never prints "Rs." or "INR") also came out None and warned on every
such document. resolve_currency now treats that case as "the company's
own currency, no warning" (COMPANY_CURRENCY, default INR) and reserves
the None-plus-warning outcome for a genuine conflict: two or more
distinct currency markers found in the same document.
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import DocumentType
from validate import build_claim, check_completeness, detect_currency_from_markdown, resolve_currency


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


def test_build_claim_defaults_to_company_currency_when_no_marker_found():
    """No currency marker anywhere in the document (e.g. a plain INR
    conveyance form) is not ambiguous -- it's the common case, and must
    default to COMPANY_CURRENCY with no warning, not null."""
    raw_fields = {"document_type": "generic_receipt", "amount": "780.75"}
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "no currency symbols in here at all")
    assert claim.currency == "INR"


def test_build_claim_leaves_currency_null_only_on_a_genuine_conflict():
    raw_fields = {"document_type": "generic_receipt", "amount": "780.75"}
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "Was $50, now Rs. 4000 after conversion")
    assert claim.currency is None


def test_resolve_currency_model_value_wins_over_markdown():
    assert resolve_currency("$", "Total: Rs. 500") == "USD"


def test_resolve_currency_respects_company_currency_env_var(monkeypatch):
    import validate

    monkeypatch.setenv("COMPANY_CURRENCY", "EUR")
    importlib.reload(validate)
    try:
        assert validate.resolve_currency(None, "no currency markers here") == "EUR"
    finally:
        monkeypatch.delenv("COMPANY_CURRENCY", raising=False)
        importlib.reload(validate)


def test_conveyance_form_markdown_has_no_currency_marker_and_gets_no_warning():
    """The real repro: a plain INR conveyance form with zero currency
    symbols anywhere must show no currency warning at all."""
    markdown = "Local Conveyance Form\nEmployee: Nasir Khan\nTotal Claimed: 8110\nDate: 26 May 2026"
    raw_fields = {"document_type": "local_conveyance_form", "total_claimed": "8110"}
    claim = build_claim(DocumentType.LOCAL_CONVEYANCE_FORM, raw_fields, markdown)
    assert claim.currency == "INR"
    warnings = check_completeness(markdown, claim.model_dump(mode="json"), "local_conveyance_form")
    assert "Couldn't tell which currency this bill is in." not in warnings


def test_currency_renormalized_on_re_evaluation():
    """A stored raw "$" (from before normalization existed, or from an
    employee-saved field) must come out normalized every time build_claim
    runs again -- re-evaluation is not a one-time migration."""
    raw_fields = {"document_type": "generic_receipt", "amount": "780.75", "currency": "$"}
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "")
    assert claim.currency == "USD"


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
