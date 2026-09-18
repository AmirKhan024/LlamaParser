"""Unit tests for validate.py's arithmetic-check tolerance (no network,
no DB). The user's own repro: editing Hotel-Receipt.png's amount from
780.75 to 780.70 (a $0.05 change) still passed subtotal+tax==amount,
because the old tolerance was an absolute 0.5 -- right for genuine
rupee round-off, wrong as a blanket default since it also hides a
$0.05-$0.49 typo or a deliberately padded amount.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import DocumentType
from validate import (
    TOLERANCE_DEFAULT,
    TOLERANCE_WITH_ROUNDOFF,
    _has_roundoff_evidence,
    _isclose,
    _tolerance_for,
    build_claim,
    validate_claim,
)


def test_default_tolerance_is_tight():
    assert TOLERANCE_DEFAULT == Decimal("0.01")
    assert TOLERANCE_WITH_ROUNDOFF == Decimal("1.00")


def test_users_exact_repro_now_fails():
    """780.70 vs 694.00 + 86.75 (= 780.75): a $0.05 gap that the old
    0.5 absolute tolerance silently accepted."""
    raw_fields = {
        "document_type": "generic_receipt",
        "amount": "780.70",
        "additional_fields": {"subtotal": "$694.00", "tax": "$86.75"},
    }
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "Hotel bill, Subtotal $694.00 Tax $86.75")
    checks = {c.name: c for c in validate_claim(claim)}
    check = checks["subtotal + tax == amount"]
    assert check.passed is False, "a $0.05 mismatch must now fail, not pass silently"
    assert "0.01" in check.detail


def test_diff_within_new_tight_tolerance_still_passes():
    raw_fields = {
        "document_type": "generic_receipt",
        "amount": "780.75",
        "additional_fields": {"subtotal": "$694.00", "tax": "$86.75"},
    }
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "Hotel bill")
    checks = {c.name: c for c in validate_claim(claim)}
    assert checks["subtotal + tax == amount"].passed is True


def test_has_roundoff_evidence_detects_markdown_mention():
    assert _has_roundoff_evidence("Subtotal 100.00\nRound off -0.30\nTotal 99.70") is True
    assert _has_roundoff_evidence("Subtotal 100.00\nRounding adjustment 0.30") is True
    assert _has_roundoff_evidence("Subtotal 100.00\nTotal 99.70") is False
    assert _has_roundoff_evidence("") is False


def test_tolerance_for_widens_only_with_roundoff_evidence():
    assert _tolerance_for("no mention of it here") == TOLERANCE_DEFAULT
    assert _tolerance_for("Round Off: 0.40") == TOLERANCE_WITH_ROUNDOFF


def test_roundoff_line_allows_up_to_one_rupee_diff_with_note_in_detail():
    raw_fields = {
        "document_type": "generic_receipt",
        "amount": "100.60",  # 694.00-style subtotal+tax example, adapted: subtotal+tax off by 0.60
        "additional_fields": {"subtotal": "$90.00", "tax": "$10.00"},  # sums to 100.00, printed amount 100.60
    }
    claim = build_claim(
        DocumentType.GENERIC_RECEIPT, raw_fields, "Bill\nSubtotal $90.00\nTax $10.00\nRound off $0.60\nTotal $100.60"
    )
    checks = {c.name: c for c in validate_claim(claim, "Bill\nSubtotal $90.00\nTax $10.00\nRound off $0.60\nTotal $100.60")}
    check = checks["subtotal + tax == amount"]
    assert check.passed is True, "a 0.60 diff must pass when the document itself shows a round-off line"
    assert "1.00" in check.detail
    assert "round-off" in check.detail.lower()


def test_roundoff_without_matching_evidence_is_not_silently_widened():
    """The same 0.60 diff, but nothing on the document mentions
    round-off -- must still fail at the tight default tolerance."""
    raw_fields = {
        "document_type": "generic_receipt",
        "amount": "100.60",
        "additional_fields": {"subtotal": "$90.00", "tax": "$10.00"},
    }
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "Bill\nSubtotal $90.00\nTax $10.00\nTotal $100.60")
    checks = {c.name: c for c in validate_claim(claim, "Bill\nSubtotal $90.00\nTax $10.00\nTotal $100.60")}
    assert checks["subtotal + tax == amount"].passed is False


def test_isclose_respects_explicit_tolerance_argument():
    assert _isclose(Decimal("10.00"), Decimal("10.005")) is True  # within 0.01 default (rounds to same cent boundary is not exact, but diff 0.005 < 0.01)
    assert _isclose(Decimal("10.00"), Decimal("10.05")) is False  # 0.05 > 0.01 default
    assert _isclose(Decimal("10.00"), Decimal("10.05"), Decimal("1.00")) is True
