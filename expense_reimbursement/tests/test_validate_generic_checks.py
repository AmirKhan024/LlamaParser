"""Unit tests for validate.py's generic-receipt arithmetic checks (bug
4, no network/DB): GenericClaim (the fallback schema for hotel_invoice/
taxi_receipt/fuel_receipt/unstructured_proof/generic_receipt) previously
had no arithmetic check at all -- an obviously wrong amount on a hotel
bill, taxi fare, etc. would sail through with nothing flagged.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review_view import build_review_view
from schemas import DocumentType
from validate import build_claim, validate_claim

HOTEL_RAW_FIELDS = {
    "document_type": "hotel_invoice",
    "vendor_name": "GRAND PLAZA HOTEL",
    "date": "03/18/2026",
    "amount": "780.75",
    "line_items": [],
    "additional_fields": {
        "tax": "$86.75",
        "total": "$780.75",
        "parking": "$50.00",
        "check_in": "11/15/2024",
        "mini_bar": "$32.00",
        "subtotal": "$694.00",
        "check_out": "11/18/2024",
        "guest_name": "John Smith",
        "card_number": "****9876",
        "room_charge": "$567.00",
        "room_number": "412",
        "room_service": "$45.00",
        "payment_method": "AMEX",
    },
}
HOTEL_MARKDOWN = "Grand Plaza Hotel\nInvoice\nSubtotal $694.00\nTax $86.75\nTotal $780.75"


def test_hotel_receipt_numbers_pass_the_check():
    claim = build_claim(DocumentType.HOTEL_INVOICE, HOTEL_RAW_FIELDS, HOTEL_MARKDOWN)
    checks = {c.name: c for c in validate_claim(claim)}
    assert "subtotal + tax == amount" in checks
    check = checks["subtotal + tax == amount"]
    assert check.passed is True
    assert "694.00 + 86.75 = 780.75" in check.detail


def test_mismatched_subtotal_and_tax_fails_the_check():
    raw = dict(HOTEL_RAW_FIELDS, amount="999.00")
    raw["additional_fields"] = dict(HOTEL_RAW_FIELDS["additional_fields"], total="$999.00")
    claim = build_claim(DocumentType.HOTEL_INVOICE, raw, HOTEL_MARKDOWN)
    checks = {c.name: c for c in validate_claim(claim)}
    assert checks["subtotal + tax == amount"].passed is False


def test_no_subtotal_or_tax_fields_means_no_check_at_all():
    """A simple taxi fare with no breakdown isn't a data quality
    problem -- must not be flagged as a failed/uncheckable check."""
    raw_fields = {
        "document_type": "taxi_receipt",
        "vendor_name": "City Cabs",
        "date": "2026-03-01",
        "amount": "250.00",
        "additional_fields": {"driver_name": "Ramesh"},
    }
    claim = build_claim(DocumentType.TAXI_RECEIPT, raw_fields, "City Cabs receipt, fare 250.00")
    checks = validate_claim(claim)
    assert not any(c.name == "subtotal + tax == amount" for c in checks)


def test_line_items_sum_checked_against_subtotal_when_present():
    raw_fields = dict(HOTEL_RAW_FIELDS)
    raw_fields["line_items"] = [
        {"name": "Room charge", "total": "567.00"},
        {"name": "Mini bar", "total": "32.00"},
        {"name": "Room service", "total": "45.00"},
        {"name": "Parking", "total": "50.00"},
    ]
    claim = build_claim(DocumentType.HOTEL_INVOICE, raw_fields, HOTEL_MARKDOWN)
    checks = {c.name: c for c in validate_claim(claim)}
    assert "sum(line items) == subtotal" in checks
    assert checks["sum(line items) == subtotal"].passed is True


def test_line_items_sum_checked_against_amount_when_no_subtotal_found():
    raw_fields = {
        "document_type": "generic_receipt",
        "vendor_name": "Corner Store",
        "amount": "30.00",
        "line_items": [{"name": "Widget", "total": "20.00"}, {"name": "Gadget", "total": "10.00"}],
    }
    claim = build_claim(DocumentType.GENERIC_RECEIPT, raw_fields, "Corner Store receipt")
    checks = {c.name: c for c in validate_claim(claim)}
    assert "sum(line items) == amount" in checks
    assert checks["sum(line items) == amount"].passed is True


def test_review_view_shows_plain_english_warning_and_expands_items_on_failure():
    raw = dict(HOTEL_RAW_FIELDS, amount="999.00")
    raw["additional_fields"] = dict(HOTEL_RAW_FIELDS["additional_fields"], total="$999.00")
    raw["line_items"] = [{"name": "Room", "total": "567.00"}]
    claim = build_claim(DocumentType.HOTEL_INVOICE, raw, HOTEL_MARKDOWN)
    checks_as_dicts = [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in validate_claim(claim)]
    clean_json = claim.model_dump(mode="json")
    view = build_review_view({**clean_json, "validation": checks_as_dicts, "completeness_warnings": []})

    assert "The amounts don't add up: subtotal plus tax should equal the total." in view["warnings"]
    # GSTIN/raw check names/formulas never reach the employee view
    for warning in view["warnings"]:
        assert "subtotal + tax == amount" not in warning
