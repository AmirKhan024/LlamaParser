"""Item 2: suggested fix when checks still fail.

Unit tests for validate.suggest_fixes (no network/DB) plus an
integration test reproducing the exact swap bug ("May-26 Local
conveyance.pdf" sometimes extracts total_kms=5200/
total_conveyance_amount=null instead of 981/5200): suggestion shown ->
Apply (sent as a normal PUT /fields edit, same as the real UI) -> both
checks pass -> Confirm works without needing a reason.
"""

import sys
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repository
import server
from schemas import DocumentType
from validate import build_claim, suggest_fixes

CONVEYANCE_MARKDOWN = (
    "Local Conveyance Form\n"
    "Trip 1: 400 km\n"
    "Trip 2: 581 km\n"
    "Total Km: 981\n"
    "Conveyance: 5200\n"
    "Daily allowance: 1560\n"
    "Vehicle maintenance: 600\n"
    "Mobile allowance: 750\n"
    "Total claimed: 8110\n"
)

_TRAVEL_ENTRIES = [
    {"date": "26 May 2026", "place": "A", "purpose": "meeting", "client": "x", "kms": "400"},
    {"date": "26 May 2026", "place": "B", "purpose": "meeting", "client": "y", "kms": "581"},
]

SWAPPED_FIELDS = {
    "document_type": "local_conveyance_form",
    "travel_entries": _TRAVEL_ENTRIES,
    "total_kms": "5200",
    "total_conveyance_amount": None,
    "daily_allowance_amount": "1560",
    "vehicle_maintenance_amount": "600",
    "mobile_allowance_amount": "750",
    "total_claimed": "8110",
}


def test_local_conveyance_form_suggests_the_swap_fix():
    claim = build_claim(DocumentType.LOCAL_CONVEYANCE_FORM, SWAPPED_FIELDS, CONVEYANCE_MARKDOWN)
    suggestions = {s["field"]: s for s in suggest_fixes(claim, CONVEYANCE_MARKDOWN)}
    assert suggestions["total_kms"]["suggested_value"] == "981"
    assert suggestions["total_kms"]["current_value"] == "5200"
    assert suggestions["total_conveyance_amount"]["suggested_value"] == "5200"
    assert suggestions["total_conveyance_amount"]["current_value"] is None


def test_no_suggestion_once_the_swap_is_fixed():
    fixed = dict(SWAPPED_FIELDS, total_kms="981", total_conveyance_amount="5200")
    claim = build_claim(DocumentType.LOCAL_CONVEYANCE_FORM, fixed, CONVEYANCE_MARKDOWN)
    assert suggest_fixes(claim, CONVEYANCE_MARKDOWN) == []


def test_no_suggestion_when_the_number_does_not_appear_on_the_document():
    """The core "never invent a value" rule: a computed fix is only
    offered when that exact number is actually printed on the document."""
    fields = dict(SWAPPED_FIELDS)
    markdown_without_the_real_numbers = "Local Conveyance Form\nSome unrelated text.\n"
    claim = build_claim(DocumentType.LOCAL_CONVEYANCE_FORM, fields, markdown_without_the_real_numbers)
    assert suggest_fixes(claim, markdown_without_the_real_numbers) == []


def test_telecom_bill_suggests_total_from_subtotal_and_tax():
    markdown = "Phone bill\nSubtotal 1200.00\nTax 217.18\nTotal 1417.18\n"
    fields = {"document_type": "telecom_bill", "subtotal": "1200.00", "tax": "217.18", "total": "1400.00"}
    claim = build_claim(DocumentType.TELECOM_BILL, fields, markdown)
    suggestions = suggest_fixes(claim, markdown)
    assert suggestions == [{
        "field": "total", "current_value": "1400.00", "suggested_value": "1417.18",
        "reason": "Subtotal (1200.00) plus tax (217.18) is 1417.18, which also appears on the document.",
    }]


def test_telecom_bill_suggests_tax_when_total_is_correct_but_tax_is_not():
    """The "or" case: subtotal+tax != total, but total-subtotal (not
    subtotal+tax) is the number actually printed -- suggest fixing tax,
    not total. The document itself prints the correct tax (217.18); the
    model just misread it as 300.00."""
    markdown = "Phone bill\nSubtotal 1200.00\nTax 217.18\nTotal 1417.18\n"
    fields = {"document_type": "telecom_bill", "subtotal": "1200.00", "tax": "300.00", "total": "1417.18"}
    claim = build_claim(DocumentType.TELECOM_BILL, fields, markdown)
    suggestions = suggest_fixes(claim, markdown)
    assert suggestions == [{
        "field": "tax", "current_value": "300.00", "suggested_value": "217.18",
        "reason": "Total (1417.18) minus subtotal (1200.00) is 217.18, which also appears on the document.",
    }]


def test_restaurant_bill_suggests_grand_total():
    markdown = "Restaurant\nSubtotal 500.00\nCGST 45.00\nSGST 45.00\nGrand Total 590.00\n"
    fields = {
        "document_type": "restaurant_bill", "subtotal": "500.00", "cgst": "45.00", "sgst": "45.00",
        "grand_total": "500.00",
    }
    claim = build_claim(DocumentType.RESTAURANT_BILL, fields, markdown)
    suggestions = suggest_fixes(claim, markdown)
    assert suggestions == [{
        "field": "grand_total", "current_value": "500.00", "suggested_value": "590.00",
        "reason": "Subtotal (500.00) plus CGST (45.00) and SGST (45.00) is 590.00, which also appears on the document.",
    }]


def test_generic_receipt_suggests_amount_from_subtotal_and_tax():
    markdown = "Hotel bill\nSubtotal $694.00\nTax $86.75\nTotal $780.75\n"
    fields = {"document_type": "generic_receipt", "subtotal": "694.00", "tax": "86.75", "amount": "700.00"}
    claim = build_claim(DocumentType.GENERIC_RECEIPT, fields, markdown)
    suggestions = suggest_fixes(claim, markdown)
    assert suggestions[0]["field"] == "amount"
    assert suggestions[0]["suggested_value"] == "780.75"


# --------------------------------------------------------- integration


def _seed_swapped_conveyance_document(db_session, claim_id):
    employee = repository.get_or_create_seed_employee(db_session)
    document = repository.create_document(
        db_session,
        claim_id=claim_id,
        actor_id=employee.id,
        original_name="conveyance.pdf",
        file_key=f"test/{uuid.uuid4().hex}.pdf",
        file_sha256=uuid.uuid4().hex,
        mime_type="application/pdf",
    )
    evaluated = server.evaluate("local_conveyance_form", SWAPPED_FIELDS, CONVEYANCE_MARKDOWN)
    repository.add_extraction(
        db_session,
        document_id=document.id,
        actor_id=employee.id,
        source="ai",
        document_type="local_conveyance_form",
        fields=evaluated["clean_json"],
        confidence=evaluated["claim"].confidence,
        amount=Decimal("8110"),
        currency="INR",
        check_results=evaluated["checks"],
        audit_action="extracted",
    )
    repository.update_document_status(db_session, document.id, status="needs_review", raw_markdown=CONVEYANCE_MARKDOWN)
    return document


def test_suggestion_shown_apply_fixes_both_checks_then_confirm_needs_no_reason(client, db_session):
    claim = client.post("/api/claims", json={}).json()
    document = _seed_swapped_conveyance_document(db_session, uuid.UUID(claim["id"]))

    detail = client.get(f"/api/documents/{document.id}").json()
    suggestions = {s["field"]: s for s in detail["review"]["suggestions"]}
    assert suggestions["total_kms"]["suggested_value"] == "981"
    assert suggestions["total_conveyance_amount"]["suggested_value"] == "5200"
    assert "Total km 981" in detail["review"]["suggestion_sentence"]
    # item 3: the check-driven warning for a field a suggestion already
    # covers is suppressed -- the suggestion sentence is the one thing
    # shown, not both saying the same thing.
    assert detail["review"]["warnings"] == []

    # "Apply": the exact edits the button sends -- a normal PUT /fields
    r = client.put(
        f"/api/documents/{document.id}/fields",
        json={"edits": {"total_kms": suggestions["total_kms"]["suggested_value"],
                         "total_conveyance_amount": suggestions["total_conveyance_amount"]["suggested_value"]}},
    )
    assert r.status_code == 200
    saved = r.json()
    assert saved["review"]["suggestions"] == [], "the suggestion box must disappear once the checks pass"
    assert saved["status"] in ("ready", "needs_review")

    corrections_by_path = {c["field_path"]: c for c in saved["corrections"]}
    assert corrections_by_path["total_kms"]["change_type"] == "suggestion_applied"
    assert corrections_by_path["total_conveyance_amount"]["change_type"] == "suggestion_applied"

    # Confirm must succeed with NO reason -- the edit fixed failing
    # checks, it didn't contradict the bill.
    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


def test_manual_edit_matching_a_suggestion_is_also_tagged_suggestion_applied(client, db_session):
    """The server infers this from the value matching, not a
    client-supplied flag -- confirmed by feeding the exact suggested
    value through PUT /fields directly, same as the test above but
    documenting that this is value-based, not click-based."""
    claim = client.post("/api/claims", json={}).json()
    document = _seed_swapped_conveyance_document(db_session, uuid.UUID(claim["id"]))

    r = client.put(f"/api/documents/{document.id}/fields", json={"edits": {"total_kms": "981"}})
    assert r.status_code == 200
    corrections_by_path = {c["field_path"]: c for c in r.json()["corrections"]}
    assert corrections_by_path["total_kms"]["change_type"] == "suggestion_applied"


def test_unrelated_manual_edit_is_not_tagged_suggestion_applied(client, db_session):
    claim = client.post("/api/claims", json={}).json()
    document = _seed_swapped_conveyance_document(db_session, uuid.UUID(claim["id"]))

    r = client.put(f"/api/documents/{document.id}/fields", json={"edits": {"daily_allowance_amount": "2000"}})
    assert r.status_code == 200
    corrections_by_path = {c["field_path"]: c for c in r.json()["corrections"]}
    assert corrections_by_path["daily_allowance_amount"]["change_type"] is None
