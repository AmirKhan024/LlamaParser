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
    assert saved["corrections"] == [
        {
            "field_path": "total", "ai_value": "1417.18", "employee_value": "1420.00",
            "change_type": None, "reason": None, "direction": "increase",
        }
    ]
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

    r = client.patch(f"/api/claims/{claim['id']}", json={"note_to_approver": "too late"})
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
    # item 5d: confirm+save is now one transaction -- this edit breaks
    # the subtotal+tax==total check with no reason given, so the whole
    # attempt (including the would-be new extraction version) rolls
    # back, not just the confirm step.
    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {"total": "9.99"}})
    assert r.status_code == 422

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
    assert all_versions == [1, 2, 3, 4]  # ai, edit, edit, revert -- the rejected confirm added nothing

    # the same edit succeeds, and creates exactly one new version, once
    # a reason is actually given
    r = client.post(
        f"/api/documents/{doc['id']}/confirm", json={"edits": {"total": "9.99"}, "reason": "corrected per manager"}
    )
    assert r.status_code == 200
    all_versions = db_session.scalars(
        select(Extraction.version).where(Extraction.document_id == doc_uuid).order_by(Extraction.version)
    ).all()
    assert all_versions == [1, 2, 3, 4, 5]


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


def test_removed_document_is_soft_deleted_not_actually_deleted(client, db_session):
    """Item 5e: "Remove document" sets status='removed' -- extractions,
    corrections and the uploaded file are all kept, not deleted, and
    the document is excluded from the claim's document list/count/
    totals and from the duplicate-sha check (re-uploading the exact
    same file afterward is allowed)."""
    import uuid

    import server
    from models import Document, Extraction

    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    doc_uuid = uuid.UUID(doc["id"])
    wait_until_processed(client, doc["id"])
    client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"vendor_name": "Jio"}})

    document = db_session.get(Document, doc_uuid)
    full_path = server.STORAGE_DIR / document.file_key
    assert full_path.exists(), "sanity check: the uploaded file is really on disk"

    r = client.delete(f"/api/documents/{doc['id']}")
    assert r.status_code == 200

    db_session.expire_all()
    document = db_session.get(Document, doc_uuid)
    assert document is not None, "the row itself must still exist -- this is a soft delete"
    assert document.status == "removed"

    extractions = db_session.scalars(select(Extraction).where(Extraction.document_id == doc_uuid)).all()
    assert len(extractions) == 2, "the AI extraction and the employee edit must both still be there"
    corrections = repository.list_corrections(db_session, doc_uuid)
    assert len(corrections) == 1, "the correction row must still be there"
    assert full_path.exists(), "the uploaded file itself must be kept, not unlinked"

    # excluded from the claim's document list and totals
    claim_detail = client.get(f"/api/claims/{claim['id']}").json()
    assert claim_detail["document_count"] == 0
    assert claim_detail["documents"] == []

    # excluded from the duplicate-sha check -- the exact same file can
    # be uploaded again
    r = upload(client, claim["id"], MOBILE_PDF)
    assert r.status_code == 200, "re-uploading the same file after removing it must be allowed"


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
    check_results rows must survive (finance / later stages need them).

    Seeded directly (not via the cached fake-pipeline fixtures) with a
    failing check whose implied fix ISN'T printed on the document, so
    item 3's suggestion-based warning dedup doesn't suppress it too --
    this test needs a warning that's genuinely still visible pre-confirm."""
    import uuid
    from decimal import Decimal

    import server

    claim = client.post("/api/claims", json={}).json()
    employee = repository.get_or_create_seed_employee(db_session)
    document = repository.create_document(
        db_session, claim_id=uuid.UUID(claim["id"]), actor_id=employee.id,
        original_name="receipt.png", file_key="test/receipt.png",
        file_sha256=uuid.uuid4().hex, mime_type="image/png",
    )
    markdown = "Receipt\nSubtotal 100.00\nTax 20.00\nTotal 150.00\n"  # 999.00 (the "fix") is nowhere on the page
    fields = {"document_type": "generic_receipt", "amount": "150.00", "subtotal": "100.00", "tax": "20.00"}
    evaluated = server.evaluate("generic_receipt", fields, markdown)
    repository.add_extraction(
        db_session, document_id=document.id, actor_id=employee.id, source="ai",
        document_type="generic_receipt", fields=evaluated["clean_json"],
        confidence=evaluated["claim"].confidence, amount=Decimal("150.00"), currency="INR",
        check_results=evaluated["checks"], audit_action="extracted",
    )
    repository.update_document_status(db_session, document.id, status="needs_review", raw_markdown=markdown)
    doc = {"id": str(document.id)}

    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert detail["review"]["warnings"] == [
        "The amounts don't add up: subtotal plus tax should equal the total."
    ]
    assert detail["review"]["suggestions"] == [], "no suggestion possible: 120.00 isn't printed anywhere"

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


def test_confirm_only_allowed_on_ready_or_needs_review(client, db_session):
    """Item 5b: Confirm must 409 on a document that's processing, failed,
    or already confirmed -- not silently confirm a document with no real
    extraction on it (the previous code only ever checked for
    'confirmed', so a 'processing' or 'failed' document could be marked
    confirmed with nothing behind it)."""
    import uuid

    from models import Document

    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    doc_uuid = uuid.UUID(doc["id"])
    wait_until_processed(client, doc["id"])

    document = db_session.get(Document, doc_uuid)
    for forced_status in ("processing", "failed"):
        document.status = forced_status
        document.error_message = "boom" if forced_status == "failed" else None
        db_session.commit()
        r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
        assert r.status_code == 409, f"confirm must reject a '{forced_status}' document"
        assert forced_status in r.json()["detail"]

    # back to a real, confirmable state -- confirm now succeeds
    document.status = "ready"
    document.error_message = None
    db_session.commit()
    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"

    # and now that it's confirmed, confirming again also 409s
    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})
    assert r.status_code == 409


def test_late_finishing_pipeline_cannot_overwrite_confirmed_or_removed(client, db_session):
    """Item 5c: repository.update_document_status_if_processing is what
    server._run_pipeline uses for its own terminal status writes -- an
    atomic UPDATE ... WHERE status = 'processing', so a pipeline that's
    still running when the employee confirms or removes the document
    (slow LlamaParse/Groq call racing a fast employee click) can never
    flip a 'confirmed' or 'removed' document back to 'ready'/
    'needs_review'/'failed' once it finally completes."""
    import uuid

    from models import Document

    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    doc_uuid = uuid.UUID(doc["id"])
    wait_until_processed(client, doc["id"])
    client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}})

    document = db_session.get(Document, doc_uuid)
    assert document.status == "confirmed"

    updated = repository.update_document_status_if_processing(db_session, doc_uuid, status="ready")
    assert updated is False
    db_session.refresh(document)
    assert document.status == "confirmed", "a late-finishing pipeline must not un-confirm the document"

    updated = repository.update_document_status_if_processing(db_session, doc_uuid, status="failed", error_message="late")
    assert updated is False
    db_session.refresh(document)
    assert document.status == "confirmed"

    # the positive case: it DOES write when the document really is
    # still "processing"
    document.status = "processing"
    document.error_message = None
    db_session.commit()
    updated = repository.update_document_status_if_processing(db_session, doc_uuid, status="ready")
    assert updated is True
    db_session.refresh(document)
    assert document.status == "ready"


def test_confirm_edits_and_recompute_are_one_transaction(client, db_session):
    """Item 5d: a rejected confirm (missing reason) must leave NOTHING
    behind -- not a half-saved extraction, not a bumped extraction
    version, not a stale claim total -- because save+confirm+recompute
    now share one session and commit exactly once."""
    import uuid

    from models import Document, Extraction

    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    doc_uuid = uuid.UUID(doc["id"])
    wait_until_processed(client, doc["id"])

    before_versions = db_session.scalars(
        select(Extraction.version).where(Extraction.document_id == doc_uuid)
    ).all()
    before_claim = client.get(f"/api/claims/{claim['id']}").json()

    # a money edit that breaks the arithmetic check, no reason given
    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {"total": "1.00"}})
    assert r.status_code == 422

    db_session.expire_all()
    after_versions = db_session.scalars(
        select(Extraction.version).where(Extraction.document_id == doc_uuid)
    ).all()
    assert after_versions == before_versions, "the rejected confirm must not have created a new extraction version"

    after_claim = client.get(f"/api/claims/{claim['id']}").json()
    assert after_claim["total_amount"] == before_claim["total_amount"], "claim total must not have moved either"

    document = db_session.get(Document, doc_uuid)
    assert document.status != "confirmed"


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


def test_travel_entries_correction_is_per_cell_not_a_whole_array_blob(client, db_session):
    """Bug 6: array-field edits used to produce one coarse correction
    row for the entire travel_entries array. Restored per-cell diffing
    (server.diff_values): editing one trip's km must show up as exactly
    one travel_entries[i].kms correction, and appending a new trip row
    must show up as a row_added correction carrying the whole new row --
    not 13 rows' worth of noise for a 1-cell edit.

    Also exercises the real-world key-alias bug found while building
    this: the model's actual travel_entries dicts use place_of_visit/
    purpose_of_travel/client_name, not review_view's canonical place/
    purpose/client -- diffing the raw AI value against a canonical-key
    edit without normalizing first would show every field as changed.
    """
    import uuid

    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], CONVEYANCE_PDF, filename="conveyance.pdf").json()
    detail = wait_until_processed(client, doc["id"])
    trips_field = next(f for f in detail["review"]["fields"] if f["key"] == "travel_entries")
    trips = trips_field["value"]
    original_count = len(trips)
    assert trips[0]["place"], "sanity check: place must already be populated (the alias bug this guards against)"

    edited_trips = [dict(t) for t in trips]
    edited_trips[0]["kms"] = "999"
    edited_trips.append({"date": "30 May 2026", "place": "ANDHERI", "purpose": "MEETING", "client": "ACME", "kms": "20"})

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"travel_entries": edited_trips}})
    assert r.status_code == 200

    corrections = repository.list_corrections(db_session, uuid.UUID(doc["id"]))
    by_path = {c.field_path: c for c in corrections}

    assert "travel_entries[0].kms" in by_path
    assert by_path["travel_entries[0].kms"].ai_value == "78"
    assert by_path["travel_entries[0].kms"].employee_value == "999"
    assert by_path["travel_entries[0].kms"].change_type is None

    added_path = f"travel_entries[{original_count}]"
    assert added_path in by_path
    assert by_path[added_path].change_type == "row_added"
    assert by_path[added_path].ai_value is None
    assert by_path[added_path].employee_value["place"] == "ANDHERI"

    # only the one genuinely-changed field plus the one added row -- not
    # a correction per field per row (13 unrelated trips untouched)
    unrelated = [p for p in by_path if p not in ("travel_entries[0].kms", added_path)]
    assert unrelated == [], f"unexpected corrections for unchanged rows: {unrelated}"


def test_diff_values_line_items_per_cell_not_a_whole_array_blob():
    """Pure unit coverage of server.diff_values (bug 6): editing one
    cell in one row of a 3-row array must produce exactly one
    correction, not a blob for the whole array, and an unrelated
    unchanged row must produce nothing at all."""
    import server

    ai_items = [
        {"name": "Coffee", "quantity": "1", "unit_price": "150.00", "total": "150.00"},
        {"name": "Tea", "quantity": "1", "unit_price": "150.00", "total": "150.00"},
        {"name": "Cake", "quantity": "1", "unit_price": "200.00", "total": "200.00"},
    ]
    employee_items = [dict(item) for item in ai_items]
    employee_items[0]["quantity"] = "2"
    employee_items[0]["total"] = "300.00"

    corrections = server.diff_values(ai_items, employee_items, "line_items")
    by_path = {c["field_path"]: c for c in corrections}

    assert by_path.keys() == {"line_items[0].quantity", "line_items[0].total"}
    assert by_path["line_items[0].quantity"] == {"field_path": "line_items[0].quantity", "ai_value": "1", "employee_value": "2"}
    assert by_path["line_items[0].total"] == {"field_path": "line_items[0].total", "ai_value": "150.00", "employee_value": "300.00"}


def test_diff_values_row_added_and_row_removed():
    """change_type distinguishes a whole row added/removed (index-based:
    the employee array's/AI array's trailing elements once the shared
    prefix is exhausted) from an ordinary per-cell value edit."""
    import server

    ai_items = [
        {"name": "Coffee", "total": "150.00"},
        {"name": "Tea", "total": "150.00"},
    ]

    appended = ai_items + [{"name": "Muffin", "total": "60.00"}]
    added = server.diff_values(ai_items, appended, "line_items")
    assert len(added) == 1
    assert added[0] == {
        "field_path": "line_items[2]", "ai_value": None,
        "employee_value": {"name": "Muffin", "total": "60.00"}, "change_type": "row_added",
    }

    truncated = ai_items[:1]
    removed = server.diff_values(ai_items, truncated, "line_items")
    assert len(removed) == 1
    assert removed[0] == {
        "field_path": "line_items[1]", "ai_value": {"name": "Tea", "total": "150.00"},
        "employee_value": None, "change_type": "row_removed",
    }


def test_stuck_processing_document_recovered_on_startup(client, db_session):
    """Bug 7 (item 5a): a document still "processing" from before a
    restart had nothing left to resume it and stayed "Reading..."
    forever. This is a single-process server, so EVERY document still
    "processing" at startup gets failed out unconditionally -- not just
    ones stuck past some age -- with a message pointing at Retry, and
    Retry must then actually work."""
    import uuid
    from datetime import UTC, datetime

    from models import Document

    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    doc_uuid = uuid.UUID(doc["id"])
    wait_until_processed(client, doc["id"])  # let the real pipeline finish first

    # simulate a server restart mid-pipeline: forced back to processing,
    # with an updated_at just now -- no age threshold exempts it anymore
    document = db_session.get(Document, doc_uuid)
    document.status = "processing"
    document.error_message = None
    document.updated_at = datetime.now(UTC)
    db_session.commit()

    recovered = repository.recover_stuck_processing_documents(db_session)
    assert [d.id for d in recovered] == [doc_uuid]

    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert detail["status"] == "failed"
    assert detail["error_message"] == "Processing was interrupted. Retry."

    # a document NOT "processing" must not be touched
    document.status = "ready"
    document.error_message = None
    db_session.commit()
    untouched = repository.recover_stuck_processing_documents(db_session)
    assert untouched == []

    # Retry must actually work afterward, not just flip the status
    document.status = "failed"
    document.error_message = "Processing was interrupted. Retry."
    db_session.commit()
    r = client.post(f"/api/documents/{doc['id']}/retry")
    assert r.status_code == 200
    final = wait_until_processed(client, doc["id"])
    assert final["status"] == "ready"
    assert final["error_message"] is None


def test_fake_pipeline_extraction_has_no_repair_attempted(client, db_session):
    """PIPELINE_MODE=fake replays a cached result and never calls Groq at
    all, so self-repair (extract.extract_claim_with_repair) never runs --
    the new columns must default sanely, not error out or stay unset."""
    import uuid

    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    extraction = db_session.scalars(
        select(Extraction).where(Extraction.document_id == uuid.UUID(doc["id"]), Extraction.source == "ai")
    ).one()
    assert extraction.repair_attempted is False
    assert extraction.repair_accepted is None
    assert extraction.first_attempt_tokens is None
    assert extraction.repair_attempt_tokens is None


def test_add_extraction_stores_repair_columns(db_session):
    """Direct repository-level check that the new columns round-trip --
    server._run_pipeline is the real caller, exercised end-to-end in
    tests/test_extract_repair.py's mocked-Groq tests instead (no live
    API calls there either)."""
    import uuid
    from decimal import Decimal

    employee = repository.get_or_create_seed_employee(db_session)
    claim = repository.create_claim(db_session, employee_id=employee.id, title="repair test")
    document = repository.create_document(
        db_session,
        claim_id=claim.id,
        actor_id=employee.id,
        original_name="conveyance.pdf",
        file_key="test/conveyance.pdf",
        file_sha256=uuid.uuid4().hex,
        mime_type="application/pdf",
    )
    extraction = repository.add_extraction(
        db_session,
        document_id=document.id,
        actor_id=employee.id,
        source="ai",
        document_type="local_conveyance_form",
        fields={"document_type": "local_conveyance_form", "total_claimed": "8110"},
        amount=Decimal("8110"),
        audit_action="extracted",
        repair_attempted=True,
        repair_accepted=True,
        first_attempt_tokens=1200,
        repair_attempt_tokens=1350,
    )
    db_session.refresh(extraction)
    assert extraction.repair_attempted is True
    assert extraction.repair_accepted is True
    assert extraction.first_attempt_tokens == 1200
    assert extraction.repair_attempt_tokens == 1350
