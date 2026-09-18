"""API tests against a real Postgres (expense_test), PIPELINE_MODE=fake
so extraction replays the cached outputs/*_result.json files instead of
calling LlamaParse/Groq. See conftest.py for the fixtures used below."""

import time

from sqlalchemy import select

import repository
from conftest import APPROVAL_PDF, CONVEYANCE_PDF, MOBILE_PDF
from models import Extraction


def upload(client, claim_id, path, filename=None, content_type="application/pdf"):
    with open(path, "rb") as f:
        return client.post(
            f"/api/claims/{claim_id}/documents",
            files={"file": (filename or path.name, f, content_type)},
        )


def wait_until_processed(client, doc_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/documents/{doc_id}")
        assert r.status_code == 200
        detail = r.json()
        if detail["status"] != "processing":
            return detail
        time.sleep(0.2)
    raise AssertionError(f"document {doc_id} never left 'processing'")


def test_full_flow_create_upload_edit_confirm_submit(client, db_session):
    r = client.post("/api/claims", json={"title": "June trip"})
    assert r.status_code == 200
    claim = r.json()
    assert claim["status"] == "draft"

    r = upload(client, claim["id"], MOBILE_PDF)
    assert r.status_code == 200
    doc = r.json()
    assert doc["status"] == "processing"

    detail = wait_until_processed(client, doc["id"])
    assert detail["status"] == "ready"
    assert detail["review"]["document_type"] == "telecom_bill"
    ai_total = next(f["value"] for f in detail["review"]["fields"] if f["key"] == "total")
    assert ai_total == "1417.18"

    # dry-run validate doesn't persist anything
    r = client.post(f"/api/documents/{doc['id']}/validate", json={"edits": {"total": "1420.00"}})
    assert r.status_code == 200
    dry_run = r.json()
    assert dry_run["corrections_preview"] == [
        {"field_path": "total", "ai_value": "1417.18", "employee_value": "1420.00"}
    ]
    unchanged = client.get(f"/api/documents/{doc['id']}").json()
    assert next(f["value"] for f in unchanged["review"]["fields"] if f["key"] == "total") == "1417.18"

    # save persists a new employee extraction version + a correction row
    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"total": "1420.00"}})
    assert r.status_code == 200
    saved = r.json()
    assert next(f["value"] for f in saved["review"]["fields"] if f["key"] == "total") == "1420.00"
    assert saved["corrections"] == [{"field_path": "total", "ai_value": "1417.18", "employee_value": "1420.00"}]
    assert saved["extraction_version"] == 2

    # revert discards the saved edit and clears the correction
    r = client.post(f"/api/documents/{doc['id']}/revert")
    assert r.status_code == 200
    reverted = r.json()
    assert next(f["value"] for f in reverted["review"]["fields"] if f["key"] == "total") == "1417.18"
    assert reverted["corrections"] == []

    # confirm
    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"

    # submit
    r = client.post(f"/api/claims/{claim['id']}/submit")
    assert r.status_code == 200
    submitted = r.json()
    assert submitted["status"] == "submitted"
    assert submitted["total_amount"] == "1417.18"
    assert submitted["submitted_at"] is not None


def test_duplicate_upload_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    r1 = upload(client, claim["id"], MOBILE_PDF)
    assert r1.status_code == 200
    r2 = upload(client, claim["id"], MOBILE_PDF)
    assert r2.status_code == 409
    assert "already been uploaded" in r2.json()["detail"]
    wait_until_processed(client, r1.json()["id"])  # let the pipeline thread finish before teardown truncates


def test_unsupported_file_type_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    r = client.post(
        f"/api/claims/{claim['id']}/documents",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


def test_edit_non_editable_field_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"document_type": "restaurant_bill"}})
    assert r.status_code == 400
    assert "cannot be edited" in r.json()["detail"]


def test_edit_after_submit_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])
    client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    submitted = client.post(f"/api/claims/{claim['id']}/submit")
    assert submitted.status_code == 200

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"total": "1.00"}})
    assert r.status_code == 409

    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    assert r.status_code == 409

    r = client.delete(f"/api/documents/{doc['id']}")
    assert r.status_code == 409

    r = client.patch(f"/api/claims/{claim['id']}", json={"title": "renamed"})
    assert r.status_code == 409


def test_submit_with_unconfirmed_document_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])  # ready, but never confirmed

    r = client.post(f"/api/claims/{claim['id']}/submit")
    assert r.status_code == 400
    assert "Confirm every document" in r.json()["detail"]


def test_submit_empty_claim_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    r = client.post(f"/api/claims/{claim['id']}/submit")
    assert r.status_code == 400
    assert "at least one document" in r.json()["detail"]


def test_approval_correspondence_does_not_block_submit(client):
    claim = client.post("/api/claims", json={}).json()
    bill = upload(client, claim["id"], MOBILE_PDF).json()
    approval = upload(client, claim["id"], APPROVAL_PDF, filename="approval.pdf").json()
    wait_until_processed(client, bill["id"])
    approval_detail = wait_until_processed(client, approval["id"])
    assert approval_detail["review"]["document_type"] == "approval_correspondence"
    assert approval_detail["review"]["needs_confirm"] is False
    assert approval_detail["status"] != "confirmed"  # never explicitly confirmed

    client.post(f"/api/documents/{bill['id']}/confirm", json={"edits": {}})
    r = client.post(f"/api/claims/{claim['id']}/submit")
    assert r.status_code == 200


def test_corrections_row_has_correct_field_path(client, db_session):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"vendor_name": "Jio", "total": "1500.00"}})
    assert r.status_code == 200

    import uuid

    corrections = repository.list_corrections(db_session, uuid.UUID(doc["id"]))
    by_path = {c.field_path: c for c in corrections}
    assert set(by_path) == {"vendor_name", "total"}
    assert by_path["vendor_name"].ai_value == "Vodafone Idea Limited"
    assert by_path["vendor_name"].employee_value == "Jio"
    assert by_path["total"].ai_value == "1417.18"
    assert by_path["total"].employee_value == "1500.00"


def test_ai_extraction_version_never_modified(client, db_session):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    import uuid

    doc_uuid = uuid.UUID(doc["id"])
    ai_before = db_session.scalars(
        select(Extraction).where(Extraction.document_id == doc_uuid, Extraction.source == "ai")
    ).one()
    original_fields = dict(ai_before.fields)
    original_version = ai_before.version

    client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"total": "1420.00"}})
    client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"vendor_name": "Jio"}})
    client.post(f"/api/documents/{doc['id']}/revert")
    client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {"total": "9.99"}})

    db_session.expire_all()
    ai_rows = db_session.scalars(
        select(Extraction).where(Extraction.document_id == doc_uuid, Extraction.source == "ai")
    ).all()
    assert len(ai_rows) == 1, "there must only ever be one source='ai' extraction row"
    assert ai_rows[0].version == original_version
    assert ai_rows[0].fields == original_fields

    all_versions = db_session.scalars(
        select(Extraction.version).where(Extraction.document_id == doc_uuid).order_by(Extraction.version)
    ).all()
    assert all_versions == [1, 2, 3, 4, 5]  # ai, edit, edit, revert, confirm-with-edit


def test_never_show_forbidden_fields(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], CONVEYANCE_PDF, filename="conveyance.pdf").json()
    detail = wait_until_processed(client, doc["id"])

    # review["document_type"] itself is a legitimate structural key (the
    # client uses it to decide how to render, never prints it) -- what's
    # actually forbidden is any of these appearing as a visible field.
    blob = str(detail)
    for forbidden in ["confidence", "employee_name", "employee_no", "additional_fields", "extraction_notes"]:
        assert forbidden not in blob, f"forbidden field leaked into API response: {forbidden}"
    field_keys = [f["key"] for f in detail["review"]["fields"]]
    assert "employee_name" not in field_keys
    assert "employee_no" not in field_keys


def test_remove_document_from_draft_claim(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.delete(f"/api/documents/{doc['id']}")
    assert r.status_code == 200

    claim_after = client.get(f"/api/claims/{claim['id']}").json()
    assert claim_after["document_count"] == 0

    r = client.get(f"/api/documents/{doc['id']}")
    assert r.status_code == 404


def test_retry_failed_document(client, db_session):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    import uuid

    repository.update_document_status(db_session, uuid.UUID(doc["id"]), status="failed", error_message="boom")

    r = client.post(f"/api/documents/{doc['id']}/retry")
    assert r.status_code == 200
    detail = wait_until_processed(client, doc["id"])
    assert detail["status"] == "ready"
    assert detail["error_message"] is None


def test_confirmed_document_hides_warnings_but_keeps_check_results(client, db_session):
    """Bug 1: warnings were recomputed from the AI's confidence/checks on
    every load and ignored document status, so a confirmed document's
    warning came right back on the next view. Once confirmed, the
    employee-facing `warnings` list must be empty, but the underlying
    check_results rows must survive (finance / later stages need them)."""
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], CONVEYANCE_PDF, filename="conveyance.pdf").json()
    detail = wait_until_processed(client, doc["id"])
    assert detail["review"]["warnings"], "test assumes this cached result has a completeness warning"

    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    assert r.status_code == 200
    confirmed = r.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["review"]["warnings"] == []

    # re-fetching (simulating "navigate away, reopen") must not resurrect it
    refetched = client.get(f"/api/documents/{doc['id']}").json()
    assert refetched["review"]["warnings"] == []

    import uuid

    extraction = repository.latest_extraction(db_session, uuid.UUID(doc["id"]))
    assert len(extraction.check_results) > 0, "check_results must still be in the DB, only the employee view changes"


def test_confirmed_document_is_locked_until_reopened(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])
    client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})

    for method, path, body in [
        ("PUT", f"/api/documents/{doc['id']}/fields", {"edits": {"total": "1.00"}}),
        ("POST", f"/api/documents/{doc['id']}/validate", {"edits": {"total": "1.00"}}),
        ("POST", f"/api/documents/{doc['id']}/revert", None),
        ("POST", f"/api/documents/{doc['id']}/confirm", {"edits": {}}),
    ]:
        r = client.request(method, path, json=body)
        assert r.status_code == 409, f"{method} {path} should be blocked on a confirmed document"

    r = client.post(f"/api/documents/{doc['id']}/reopen")
    assert r.status_code == 200
    reopened = r.json()
    assert reopened["status"] == "needs_review"

    # now editing works again
    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"total": "1420.00"}})
    assert r.status_code == 200


def test_reopen_removes_amount_from_claim_total(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])
    client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    confirmed_claim = client.get(f"/api/claims/{claim['id']}").json()
    assert confirmed_claim["total_amount"] == "1417.18"

    client.post(f"/api/documents/{doc['id']}/reopen")
    reopened_claim = client.get(f"/api/claims/{claim['id']}").json()
    assert reopened_claim["total_amount"] == "0.00"


def test_reopen_on_submitted_claim_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])
    client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    client.post(f"/api/claims/{claim['id']}/submit")

    r = client.post(f"/api/documents/{doc['id']}/reopen")
    assert r.status_code == 409


def test_claim_total_never_sums_different_currencies(client, db_session):
    """Bug 3: the claim total used to add every confirmed document's
    amount together regardless of currency. A USD document and an INR
    document confirmed on the same claim must show a per-currency
    breakdown, never one combined (and currency-blind) number."""
    import uuid
    from decimal import Decimal

    claim = client.post("/api/claims", json={}).json()

    inr_doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, inr_doc["id"])
    client.post(f"/api/documents/{inr_doc['id']}/confirm", json={"edits": {}})

    # a second document, injected directly with a USD extraction --
    # the fake-mode cached results are all INR, so this simulates what
    # a real dollar bill (e.g. Hotel-Receipt.png) would produce.
    usd_doc = upload(client, claim["id"], CONVEYANCE_PDF, filename="hotel.pdf").json()
    wait_until_processed(client, usd_doc["id"])
    repository.add_extraction(
        db_session,
        document_id=uuid.UUID(usd_doc["id"]),
        actor_id=repository.get_or_create_seed_employee(db_session).id,
        source="employee",
        document_type="generic_receipt",
        fields={"document_type": "generic_receipt", "vendor_name": "Grand Plaza Hotel", "amount": "780.75", "currency": "USD"},
        amount=Decimal("780.75"),
        currency="USD",
        audit_action="edited",
    )
    client.post(f"/api/documents/{usd_doc['id']}/confirm", json={"edits": {}})

    claim_detail = client.get(f"/api/claims/{claim['id']}").json()
    assert claim_detail["total_amount"] is None, "a mixed-currency claim has no single total"
    assert claim_detail["currency"] is None
    assert claim_detail["totals_by_currency"] == {"INR": "1417.18", "USD": "780.75"}

    # the claims list must show the same breakdown, not a stale/summed figure
    listed = next(c for c in client.get("/api/claims").json() if c["id"] == claim["id"])
    assert listed["totals_by_currency"] == {"INR": "1417.18", "USD": "780.75"}
