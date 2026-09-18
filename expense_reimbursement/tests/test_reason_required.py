"""Prior-prompt item 2: employee money edits need a reason when they
contradict the bill. The user's own repro: editing Hotel-Receipt.png's
amount from 780.75 to 780.70 went through with no flag at all, because
nothing forced them to explain a money edit that leaves the bill's own
arithmetic broken.

These tests build the document/extraction rows directly via
repository.py (mirroring test_api.py's test_claim_total_never_sums_
different_currencies pattern) instead of going through the real/fake
pipeline, so each test can set up an exact subtotal/tax/amount
combination without depending on which cached fake-mode fixture happens
to match a given upload's sha256.
"""

import uuid
from decimal import Decimal

import repository
import server

HOTEL_MARKDOWN = "Grand Plaza Hotel\nInvoice\nSubtotal {subtotal}\nTax {tax}\nTotal {amount}"


def _create_generic_document(client, db_session, claim_id, *, amount, subtotal, tax, markdown_amount=None):
    employee = repository.get_or_create_seed_employee(db_session)
    document = repository.create_document(
        db_session,
        claim_id=uuid.UUID(claim_id),
        actor_id=employee.id,
        original_name="hotel.png",
        file_key=f"test/{uuid.uuid4().hex}.png",
        file_sha256=uuid.uuid4().hex,
        mime_type="image/png",
    )
    markdown = HOTEL_MARKDOWN.format(subtotal=subtotal, tax=tax, amount=markdown_amount or amount)
    fields = {
        "document_type": "generic_receipt",
        "vendor_name": "Grand Plaza Hotel",
        "amount": amount,
        "currency": "USD",
        "additional_fields": {"subtotal": subtotal, "tax": tax},
    }
    evaluated = server.evaluate("generic_receipt", fields, markdown)
    repository.add_extraction(
        db_session,
        document_id=document.id,
        actor_id=employee.id,
        source="ai",
        document_type="generic_receipt",
        fields=evaluated["clean_json"],
        confidence=evaluated["claim"].confidence,
        vendor_name="Grand Plaza Hotel",
        amount=Decimal(amount),
        currency="USD",
        check_results=evaluated["checks"],
        audit_action="extracted",
    )
    repository.update_document_status(db_session, document.id, status="ready", raw_markdown=markdown)
    return document


def _new_claim_and_doc(client, db_session, *, amount, subtotal, tax, markdown_amount=None):
    claim = client.post("/api/claims", json={}).json()
    document = _create_generic_document(
        client, db_session, claim["id"], amount=amount, subtotal=subtotal, tax=tax, markdown_amount=markdown_amount
    )
    return claim, document


def test_exact_repro_780_75_to_780_70_requires_reason(client, db_session):
    """694.00 + 86.75 = 780.75 (the AI reading, check passes). Editing
    amount down to 780.70 breaks the check by 0.05 -- must now be
    blocked at Confirm without an explanation."""
    claim, document = _new_claim_and_doc(client, db_session, amount="780.75", subtotal="694.00", tax="86.75")

    r = client.put(f"/api/documents/{document.id}/fields", json={"edits": {"amount": "780.70"}})
    assert r.status_code == 200, "saving the edit itself is never blocked -- only Confirm is"

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}})
    assert r.status_code == 422
    assert "doesn't match the bill's own numbers" in r.json()["detail"]

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}, "reason": "typo, rechecked receipt"})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"

    corrections = repository.list_corrections(db_session, document.id)
    amount_correction = next(c for c in corrections if c.field_path == "amount")
    assert amount_correction.reason == "typo, rechecked receipt"
    assert amount_correction.direction == "decrease"


def test_upward_money_edit_that_breaks_check_also_requires_reason(client, db_session):
    claim, document = _new_claim_and_doc(client, db_session, amount="780.75", subtotal="694.00", tax="86.75")

    r = client.put(f"/api/documents/{document.id}/fields", json={"edits": {"amount": "800.00"}})
    assert r.status_code == 200

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}})
    assert r.status_code == 422

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}, "reason": "found an extra line item"})
    assert r.status_code == 200

    corrections = repository.list_corrections(db_session, document.id)
    amount_correction = next(c for c in corrections if c.field_path == "amount")
    assert amount_correction.direction == "increase"


def test_edit_that_fixes_a_failing_check_needs_no_reason(client, db_session):
    """The AI over-read the total as 800.00, but 694.00 + 86.75 = 780.75
    -- the printed total -- and the check already fails on the AI
    version. The employee correcting amount DOWN to 780.75 (the value
    the document itself prints) fixes the check, so no reason is
    needed: a decrease, and the new value is on the document."""
    claim, document = _new_claim_and_doc(
        client, db_session, amount="800.00", subtotal="694.00", tax="86.75", markdown_amount="780.75"
    )

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {"amount": "780.75"}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"

    corrections = repository.list_corrections(db_session, document.id)
    amount_correction = next(c for c in corrections if c.field_path == "amount")
    assert amount_correction.reason is None


def test_server_enforces_reason_not_just_the_ui(client, db_session):
    """The 422 must come from the server regardless of what the UI
    sends -- posting no `reason` key at all must still be rejected."""
    claim, document = _new_claim_and_doc(client, db_session, amount="780.75", subtotal="694.00", tax="86.75")
    client.put(f"/api/documents/{document.id}/fields", json={"edits": {"amount": "700.00"}})

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}})
    assert r.status_code == 422

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}, "reason": "   "})
    assert r.status_code == 422, "whitespace-only reason must not count"

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {}, "reason": "ok"})
    assert r.status_code == 422, "a reason under 5 characters must still be rejected"


def test_non_money_edit_never_requires_a_reason(client, db_session):
    claim, document = _new_claim_and_doc(client, db_session, amount="780.75", subtotal="694.00", tax="86.75")

    r = client.post(f"/api/documents/{document.id}/confirm", json={"edits": {"vendor_name": "Different Hotel"}})
    assert r.status_code == 200
