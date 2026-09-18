"""Quick-fix item 3: consistent inflation loophole. Repro: on the
conveyance form, change travel_entries[0].kms 78->578, total_kms
981->1481, total_conveyance_amount 5200->8200, total_claimed
8110->11110. Every arithmetic check still passes (everything scales
together), so confirm used to succeed with no reason and the claim
grew from Rs.8,110 to Rs.11,110 with nothing flagging it.
"""

import uuid
from decimal import Decimal

import repository
import server

MARKDOWN = (
    "Local Conveyance Form\nTrip 1: 78 km\nTrip 2: 903 km\nTotal Km: 981\n"
    "Conveyance: 5200\nDaily allowance: 2910\nTotal claimed: 8110\n"
)

AI_FIELDS = {
    "document_type": "local_conveyance_form",
    "travel_entries": [
        {"date": "26 May 2026", "place": "A", "purpose": "meeting", "client": "x", "kms": "78"},
        {"date": "26 May 2026", "place": "B", "purpose": "meeting", "client": "y", "kms": "903"},
    ],
    "total_kms": "981",
    "total_conveyance_amount": "5200",
    "daily_allowance_amount": "2910",
    "total_claimed": "8110",
}


def _seed_document(client, db_session):
    claim = client.post("/api/claims", json={}).json()
    employee = repository.get_or_create_seed_employee(db_session)
    document = repository.create_document(
        db_session, claim_id=uuid.UUID(claim["id"]), actor_id=employee.id,
        original_name="conveyance.pdf", file_key=f"test/{uuid.uuid4().hex}.pdf",
        file_sha256=uuid.uuid4().hex, mime_type="application/pdf",
    )
    evaluated = server.evaluate("local_conveyance_form", AI_FIELDS, MARKDOWN)
    repository.add_extraction(
        db_session, document_id=document.id, actor_id=employee.id, source="ai",
        document_type="local_conveyance_form", fields=evaluated["clean_json"],
        confidence=evaluated["claim"].confidence, amount=Decimal("8110"), currency="INR",
        check_results=evaluated["checks"], audit_action="extracted",
    )
    repository.update_document_status(db_session, document.id, status="needs_review", raw_markdown=MARKDOWN)
    return claim, document


def test_consistent_inflation_requires_a_reason_even_though_every_check_passes(client, db_session):
    claim, document = _seed_document(client, db_session)

    edits = {
        "travel_entries": [
            {"date": "26 May 2026", "place": "A", "purpose": "meeting", "client": "x", "kms": "578"},
            {"date": "26 May 2026", "place": "B", "purpose": "meeting", "client": "y", "kms": "903"},
        ],
        "total_kms": "1481",
        "total_conveyance_amount": "8200",
        "total_claimed": "11110",
    }

    # sanity check: every arithmetic check really does pass on the
    # inflated numbers -- this is precisely the loophole
    preview = client.post(f"/api/documents/{document.id}/validate", json={"edits": edits}).json()
    assert preview["review"]["warnings"] == []
    assert preview["review"]["reason_required"] is True, "an increase across the board must require a reason anyway"

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": edits})
    assert r.status_code == 422

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": edits, "reason": "extra client visits added"})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


def test_downward_edit_to_a_value_printed_on_the_document_needs_no_reason(client, db_session):
    """The AI misread total_kms as 1200; the trip row and the document
    itself both say 981. Correcting DOWN to 981 needs no reason."""
    claim = client.post("/api/claims", json={}).json()
    employee = repository.get_or_create_seed_employee(db_session)
    document = repository.create_document(
        db_session, claim_id=uuid.UUID(claim["id"]), actor_id=employee.id,
        original_name="conveyance2.pdf", file_key=f"test/{uuid.uuid4().hex}.pdf",
        file_sha256=uuid.uuid4().hex, mime_type="application/pdf",
    )
    fields = dict(AI_FIELDS, total_kms="1200")
    evaluated = server.evaluate("local_conveyance_form", fields, MARKDOWN)
    repository.add_extraction(
        db_session, document_id=document.id, actor_id=employee.id, source="ai",
        document_type="local_conveyance_form", fields=evaluated["clean_json"],
        confidence=evaluated["claim"].confidence, amount=Decimal("8110"), currency="INR",
        check_results=evaluated["checks"], audit_action="extracted",
    )
    repository.update_document_status(db_session, document.id, status="needs_review", raw_markdown=MARKDOWN)

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {"total_kms": "981"}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"


def test_suggestion_apply_values_need_no_reason(client, db_session):
    """An Apply-filled value (matches what suggest_fixes would offer
    against the AI's own fields) is exempt even though total_kms goes
    up numerically (5200 -> ... no, here total_kms itself is the
    misread field being corrected upward from a wrong low value to the
    real, higher, document-printed one)."""
    claim = client.post("/api/claims", json={}).json()
    employee = repository.get_or_create_seed_employee(db_session)
    document = repository.create_document(
        db_session, claim_id=uuid.UUID(claim["id"]), actor_id=employee.id,
        original_name="conveyance3.pdf", file_key=f"test/{uuid.uuid4().hex}.pdf",
        file_sha256=uuid.uuid4().hex, mime_type="application/pdf",
    )
    markdown = (
        "Local Conveyance Form\nTrip 1: 400 km\nTrip 2: 581 km\nTotal Km: 981\n"
        "Conveyance: 5200\nTotal claimed: 5200\n"
    )
    fields = {
        "document_type": "local_conveyance_form",
        "travel_entries": [
            {"date": "26 May 2026", "place": "A", "purpose": "meeting", "client": "x", "kms": "400"},
            {"date": "26 May 2026", "place": "B", "purpose": "meeting", "client": "y", "kms": "581"},
        ],
        "total_kms": "100",  # wrong -- real sum is 981, and 981 is printed on the doc
        "total_conveyance_amount": "5200",
        "total_claimed": "5200",
    }
    evaluated = server.evaluate("local_conveyance_form", fields, markdown)
    repository.add_extraction(
        db_session, document_id=document.id, actor_id=employee.id, source="ai",
        document_type="local_conveyance_form", fields=evaluated["clean_json"],
        confidence=evaluated["claim"].confidence, amount=Decimal("5200"), currency="INR",
        check_results=evaluated["checks"], audit_action="extracted",
    )
    repository.update_document_status(db_session, document.id, status="needs_review", raw_markdown=markdown)

    detail = client.get(f"/api/documents/{document.id}").json()
    suggestion = next(s for s in detail["review"]["suggestions"] if s["field"] == "total_kms")
    assert suggestion["suggested_value"] == "981"

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {"total_kms": "981"}})
    assert r.status_code == 200, r.text
