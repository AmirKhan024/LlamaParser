"""API tests for Stage 2 category integration: assigned on upload
(PIPELINE_MODE=fake forces the `rules` categorizer, so this needs no
Groq/classifier model), editable like any other field but through its
own top-level `category` key (never `edits`), versioned the same way,
and recorded as a Correction row -- see server.py's _save_edits /
_category_correction and categories.py.
"""

import uuid

from sqlalchemy import select

import repository
from conftest import APPROVAL_PDF, CONVEYANCE_PDF, MOBILE_PDF
from models import Extraction

from test_api import upload, wait_until_processed


def test_telecom_bill_gets_phone_internet_category(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    detail = wait_until_processed(client, doc["id"])

    category = detail["review"]["category"]
    assert category is not None
    assert category["value"] == "phone_internet"
    assert category["label"] == "Phone and Internet"
    assert "options" in category and len(category["options"]) == 14
    assert "needs_check" in category


def test_conveyance_form_gets_own_vehicle_mileage_category(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], CONVEYANCE_PDF).json()
    detail = wait_until_processed(client, doc["id"])
    assert detail["review"]["category"]["value"] == "own_vehicle_mileage"


def test_approval_correspondence_category_is_none(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], APPROVAL_PDF).json()
    detail = wait_until_processed(client, doc["id"])
    assert detail["review"]["category"] is None


def test_category_confidence_method_and_rationale_never_leak(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    detail = wait_until_processed(client, doc["id"])
    blob = str(detail)
    for forbidden in ["category_confidence", "category_method", "rationale", "'method'"]:
        assert forbidden not in blob, f"forbidden category internal leaked into API response: {forbidden}"


def test_edit_category_creates_new_version_and_correction(client, db_session):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {}, "category": "software_subscriptions"})
    assert r.status_code == 200
    updated = r.json()
    assert updated["review"]["category"]["value"] == "software_subscriptions"
    assert updated["extraction_version"] == 2

    doc_uuid = uuid.UUID(doc["id"])
    corrections = repository.list_corrections(db_session, doc_uuid)
    category_corrections = [c for c in corrections if c.field_path == "category"]
    assert len(category_corrections) == 1
    assert category_corrections[0].ai_value == "phone_internet"
    assert category_corrections[0].employee_value == "software_subscriptions"
    assert category_corrections[0].reason is None  # never required for a category edit


def test_category_edit_alone_does_not_require_a_reason_at_confirm(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.post(f"/api/documents/{doc['id']}/confirm", json={"edits": {}, "category": "other"})
    assert r.status_code == 200
    assert r.json()["review"]["category"]["value"] == "other"


def test_setting_an_unknown_category_is_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {}, "category": "not_a_real_category"})
    assert r.status_code == 422


def test_setting_category_on_non_categorizable_document_is_rejected(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], APPROVAL_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {}, "category": "other"})
    assert r.status_code == 422


def test_category_edit_survives_reload(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {}, "category": "other"})
    reloaded = client.get(f"/api/documents/{doc['id']}").json()
    assert reloaded["review"]["category"]["value"] == "other"


def test_revert_document_restores_ai_category(client):
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {}, "category": "other"})
    reverted = client.post(f"/api/documents/{doc['id']}/revert").json()
    assert reverted["review"]["category"]["value"] == "phone_internet"


def test_category_unchanged_when_only_editing_other_fields(client, db_session):
    """A save that doesn't touch category at all must carry the previous
    category forward untouched, and must NOT create a spurious
    field_path="category" correction."""
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    r = client.put(f"/api/documents/{doc['id']}/fields", json={"edits": {"vendor_name": "Jio"}})
    assert r.status_code == 200
    assert r.json()["review"]["category"]["value"] == "phone_internet"

    doc_uuid = uuid.UUID(doc["id"])
    corrections = repository.list_corrections(db_session, doc_uuid)
    assert not any(c.field_path == "category" for c in corrections)


def test_category_method_is_rules_in_fake_pipeline_mode(db_session, client):
    """PIPELINE_MODE=fake must never call the real llm/hybrid categorizer
    (no network, no API key in the test environment) -- category_method
    is forced to "rules" regardless of the CATEGORIZER env var."""
    claim = client.post("/api/claims", json={}).json()
    doc = upload(client, claim["id"], MOBILE_PDF).json()
    wait_until_processed(client, doc["id"])

    doc_uuid = uuid.UUID(doc["id"])
    ai_extraction = db_session.scalars(
        select(Extraction).where(Extraction.document_id == doc_uuid, Extraction.source == "ai")
    ).one()
    assert ai_extraction.category_method == "rules"
    assert ai_extraction.category == "phone_internet"
    assert ai_extraction.category_confidence is not None
