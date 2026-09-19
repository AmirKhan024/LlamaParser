"""Stage 3 evaluation service + API against the real test Postgres, with the
model mocked (policy_select.call_model). Covers idempotency, append-only
re-evaluation, the override path, DB-level immutability, monthly aggregation
across claims, and atomicity when the model is unavailable."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import policy as pol
import policy_llm
import policy_select
import repository
from conftest import APPROVAL_PDF, MOBILE_PDF
from models import PolicyDecision
from policy import Clause, LimitEntry
from test_api import upload, wait_until_processed

D = Decimal
REF = {"grades": ["L1", "L2", "L3", "L4", "L5", "L6"], "city_tiers": {"1": ["mumbai"]}, "zone_a_countries": ["usa"]}


def _clauses():
    phone = Clause("12.1", "12", 0, "12.1. Mobile monthly cap by grade ...", ("phone_internet",), limit_unit="per_month",
                   limit_table=(LimitEntry(D("800"), "INR", {"grade": ("L1", "L2")}), LimitEntry(D("1500"), "INR", {"grade": ("L3", "L4")})))
    broadband = Clause("12.2", "12", 2, "12.2. Home broadband up to 1,500/month ...", ("phone_internet",), limit_unit="per_month",
                       limit_table=(LimitEntry(D("1500"), "INR", {}),))
    fines = Clause("19.1", "19", 1, "Traffic fines are never reimbursable.", ("*",), is_prohibition=True, conditions=(
        {"id": "c1", "text": "the expense is a fine", "kind": "excludes", "check": "judgment", "depends_on": []},))
    return [phone, fines, broadband]


@pytest.fixture
def seeded(db_session):
    return repository.create_policy_version(
        db_session, version="vtest", source_sha256="x", reference_data=REF, build_meta={"human_reviewed": True},
        clauses=[pol.clause_to_dict(c) for c in _clauses()],
    )


class FakeModel:
    """Stands in for policy_select.call_model; records every call."""

    def __init__(self, unit_overrides=None, raw_text=None, error=None):
        self.calls = 0
        self.unit_overrides = unit_overrides or {}
        self.raw_text = raw_text
        self.error = error

    def __call__(self, messages, *, model, scope, cache_dir=None, use_cache=True):
        self.calls += 1
        if self.error:
            raise self.error
        unit = {"line_item_refs": [], "clause_id": "12.1", "clause_confidence": 0.9,
                "amount_refs": [{"path": "total", "sign": 1}], "stated_amount": "1417.18", "amount_reason": "bill total",
                "quantities": {}, "dimensions": {}, "conditions": {}, "documentation": {}, "approval_evidenced": None,
                "missing_fields": [], "explanation": "mobile bill", "confidence": 0.9, **self.unit_overrides}
        return policy_llm.LLMCall(self.raw_text or json.dumps({"units": [unit]}), 1200, 80, 5, False, model)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        m = FakeModel(**kw)
        monkeypatch.setattr(policy_select, "call_model", m)
        return m
    return install


def new_claim_with_bill(client, path=MOBILE_PDF):
    claim = client.post("/api/claims", json={"title": "May"}).json()
    doc = upload(client, claim["id"], path).json()
    wait_until_processed(client, doc["id"])
    return claim["id"], doc["id"]


def rows(db_session):
    db_session.expire_all()
    return list(db_session.scalars(select(PolicyDecision).order_by(PolicyDecision.evaluation_seq, PolicyDecision.unit_index)))


# ------------------------------------------------------------------- basics

def test_evaluate_returns_and_stores_a_cited_decision(client, db_session, seeded, fake):
    fake()
    claim_id, _ = new_claim_with_bill(client)
    r = client.post(f"/api/claims/{claim_id}/evaluate")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "compliant" and body["reused"] is False and body["evaluation_seq"] == 1
    d = body["decisions"][0]
    assert d["clause"]["id"] == "12.1" and d["clause"]["text"].startswith("12.1. Mobile monthly cap")
    assert d["comparison_text"] == "₹1,417.18 vs ₹1,500 limit (per month)"      # L4 seed employee
    assert d["evaluation_mode"] == "document" and d["line_item_ref"] is None
    assert "category_confidence" not in json.dumps(body) and "category_method" not in json.dumps(body)

    stored = rows(db_session)[0]
    assert stored.category_used == "phone_internet" and stored.category_method == "rules"     # the audit columns
    assert stored.raw_request["prompt_version"] == "v1" and stored.raw_response["units"]
    assert stored.prompt_tokens == 1200 and stored.policy_version == "vtest"

    got = client.get(f"/api/claims/{claim_id}/decisions").json()
    assert got["decisions"][0]["id"] == d["id"]


def test_approval_email_is_evidence_not_an_expense(client, seeded, fake):
    model = fake()
    claim_id, _ = new_claim_with_bill(client)
    doc = upload(client, claim_id, APPROVAL_PDF).json()
    wait_until_processed(client, doc["id"])
    body = client.post(f"/api/claims/{claim_id}/evaluate").json()
    assert len(body["decisions"]) == 1 and model.calls == 1
    assert any("supporting document" in s["reason"] for s in body["skipped"])


# --------------------------------------------- idempotency / append-only

def test_evaluate_is_idempotent_and_force_appends_a_new_run(client, db_session, seeded, fake):
    model = fake()
    claim_id, _ = new_claim_with_bill(client)
    first = client.post(f"/api/claims/{claim_id}/evaluate").json()
    again = client.post(f"/api/claims/{claim_id}/evaluate").json()
    assert again["reused"] is True and again["evaluation_seq"] == 1
    assert again["decisions"][0]["id"] == first["decisions"][0]["id"]
    assert model.calls == 1 and len(rows(db_session)) == 1

    forced = client.post(f"/api/claims/{claim_id}/evaluate?force=true").json()
    assert forced["reused"] is False and forced["evaluation_seq"] == 2
    all_rows = rows(db_session)
    assert [r.evaluation_seq for r in all_rows] == [1, 2]                       # the old row is preserved
    assert all_rows[0].id == __import__("uuid").UUID(first["decisions"][0]["id"])
    assert client.get(f"/api/claims/{claim_id}/decisions").json()["evaluation_seq"] == 2


def test_new_policy_or_prompt_version_is_a_new_run_without_force(client, db_session, seeded, fake):
    fake()
    repository.create_policy_version(db_session, version="vtest2", source_sha256="y", reference_data=REF, build_meta={},
                                     clauses=[pol.clause_to_dict(c) for c in _clauses()], activate=False)
    claim_id, _ = new_claim_with_bill(client)
    client.post(f"/api/claims/{claim_id}/evaluate")
    second = client.post(f"/api/claims/{claim_id}/evaluate?policy_version=vtest2").json()
    assert second["reused"] is False and second["policy_version"] == "vtest2" and second["evaluation_seq"] == 2


def test_stale_flag_when_the_extraction_changed_after_evaluation(client, seeded, fake):
    fake()
    claim_id, doc_id = new_claim_with_bill(client)
    assert client.post(f"/api/claims/{claim_id}/evaluate").json()["stale"] is False
    client.put(f"/api/documents/{doc_id}/fields", json={"edits": {"vendor_name": "Other Telecom"}})
    assert client.get(f"/api/claims/{claim_id}/decisions").json()["stale"] is True


# ------------------------------------------------------------ hard failures

def test_invalid_clause_id_is_stored_as_insufficient_information(client, seeded, fake):
    fake(unit_overrides={"clause_id": "77.7"})
    claim_id, _ = new_claim_with_bill(client)
    d = client.post(f"/api/claims/{claim_id}/evaluate").json()["decisions"][0]
    assert d["verdict"] == "insufficient_information"
    assert d["missing_fields"] == ["system:invalid_clause_id"] and "77.7" in d["hard_failure_reason"]


def test_amount_that_does_not_reconcile_is_never_coerced(client, seeded, fake):
    fake(unit_overrides={"stated_amount": "999.00"})
    claim_id, _ = new_claim_with_bill(client)
    d = client.post(f"/api/claims/{claim_id}/evaluate").json()["decisions"][0]
    assert d["verdict"] == "insufficient_information" and d["amount_compared"] is None
    assert "does not reconcile" in d["hard_failure_reason"]


def test_malformed_model_output_is_stored_as_a_document_level_failure(client, seeded, fake):
    fake(raw_text="I think it is fine.")
    claim_id, _ = new_claim_with_bill(client)
    d = client.post(f"/api/claims/{claim_id}/evaluate").json()["decisions"][0]
    assert d["verdict"] == "insufficient_information" and d["missing_fields"] == ["system:invalid_json"]


def test_model_unavailable_stores_nothing_and_does_not_block_a_retry(client, db_session, seeded, fake, monkeypatch):
    fake(error=policy_llm.PolicyModelUnavailable("GROQ_API_KEY is not set"))
    claim_id, _ = new_claim_with_bill(client)
    r = client.post(f"/api/claims/{claim_id}/evaluate")
    assert r.status_code == 503 and rows(db_session) == []
    fake()
    assert client.post(f"/api/claims/{claim_id}/evaluate").json()["reused"] is False


def test_not_seeded_and_foreign_claim(client, fake):
    fake()
    claim_id, _ = new_claim_with_bill(client)
    assert client.post(f"/api/claims/{claim_id}/evaluate").status_code == 409
    assert client.post("/api/claims/00000000-0000-0000-0000-000000000000/evaluate").status_code == 404


# ------------------------------------------------------------- aggregation

def test_monthly_aggregation_across_claims_turns_a_pass_into_a_violation(client, db_session, seeded, fake):
    fake()
    first_claim, first_doc = new_claim_with_bill(client)
    assert client.post(f"/api/claims/{first_claim}/evaluate").json()["verdict"] == "compliant"   # 1,417 <= 1,500 alone
    client.post(f"/api/documents/{first_doc}/confirm", json={"edits": {}})
    assert client.post(f"/api/claims/{first_claim}/submit").status_code == 200

    second_claim, _ = new_claim_with_bill(client)          # a second May bill in a new claim
    body = client.post(f"/api/claims/{second_claim}/evaluate").json()
    d = body["decisions"][0]
    assert body["verdict"] == "violation" and d["amount_compared"] == "2834.36" and d["limit_applied"] == "1500"
    stored = [r for r in rows(db_session) if str(r.claim_id) == second_claim][0]
    assert len(stored.check_detail["aggregation"]["members"]) == 1


# ---------------------------------------------------------------- override

def test_override_records_the_label_without_touching_the_decision(client, db_session, seeded, fake):
    fake()
    claim_id, _ = new_claim_with_bill(client)
    d = client.post(f"/api/claims/{claim_id}/evaluate").json()["decisions"][0]
    before = rows(db_session)[0]
    snapshot = (before.verdict, before.clause_id, before.explanation, before.amount_compared, before.raw_response)

    r = client.post(f"/api/decisions/{d['id']}/override",
                    json={"human_verdict": "needs_approval", "human_clause_id": "19.1", "human_note": "manager exception"})
    assert r.status_code == 200 and r.json()["override"]["human_verdict"] == "needs_approval"
    assert r.json()["verdict"] == "compliant" and r.json()["effective_verdict"] == "needs_approval"

    after = rows(db_session)[0]
    assert (after.verdict, after.clause_id, after.explanation, after.amount_compared, after.raw_response) == snapshot
    assert after.human_verdict == "needs_approval" and after.human_clause_id == "19.1" and after.overridden_by is not None
    assert client.get(f"/api/claims/{claim_id}/decisions").json()["verdict"] == "needs_approval"   # roll-up uses the label


def test_override_validation(client, seeded, fake):
    fake()
    claim_id, _ = new_claim_with_bill(client)
    d = client.post(f"/api/claims/{claim_id}/evaluate").json()["decisions"][0]
    assert client.post(f"/api/decisions/{d['id']}/override", json={"human_verdict": "maybe"}).status_code == 422
    assert client.post(f"/api/decisions/{d['id']}/override", json={"human_verdict": "violation", "human_clause_id": "0.0"}).status_code == 422
    assert client.post("/api/decisions/not-a-uuid/override", json={"human_verdict": "violation"}).status_code == 404


def test_database_refuses_to_mutate_or_delete_a_decision(client, db_session, seeded, fake):
    fake()
    claim_id, _ = new_claim_with_bill(client)
    client.post(f"/api/claims/{claim_id}/evaluate")
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("UPDATE policy_decisions SET verdict = 'violation'"))
    db_session.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("DELETE FROM policy_decisions"))
    db_session.rollback()
    db_session.execute(text("UPDATE policy_decisions SET human_note = 'ok'"))    # the override columns are allowed
    db_session.commit()


# ------------------------------------------------------------- server env

def test_env_file_credentials_beat_a_stale_machine_variable(tmp_path, monkeypatch):
    import server

    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=key-from-dotenv\nPIPELINE_MODE=real\n")
    monkeypatch.setenv("GROQ_API_KEY", "stale-machine-key")
    monkeypatch.setenv("PIPELINE_MODE", "fake")
    server._load_env(env)
    import os
    assert os.environ["GROQ_API_KEY"] == "key-from-dotenv"
    assert os.environ["PIPELINE_MODE"] == "fake"          # only credentials are overridden


def test_monthly_aggregation_is_per_clause_so_mobile_and_broadband_are_not_pooled(client, db_session, seeded, fake):
    # claim 1's bill is judged as BROADBAND (12.2) and submitted
    fake(unit_overrides={"clause_id": "12.2"})
    first_claim, first_doc = new_claim_with_bill(client)
    client.post(f"/api/claims/{first_claim}/evaluate")
    client.post(f"/api/documents/{first_doc}/confirm", json={"edits": {}})
    client.post(f"/api/claims/{first_claim}/submit")

    # claim 2's bill is MOBILE (12.1): 1,417.18 alone is within 1,500; pooled it would be 2,834.36
    fake(unit_overrides={"clause_id": "12.1"})
    second_claim, _ = new_claim_with_bill(client)
    body = client.post(f"/api/claims/{second_claim}/evaluate").json()
    d = body["decisions"][0]
    assert d["verdict"] == "compliant" and d["amount_compared"] == "1417.18"
    stored = [r for r in rows(db_session) if str(r.claim_id) == second_claim][0]
    assert stored.check_detail["aggregation"]["members"] == []


def test_a_never_evaluated_peer_is_reported_not_silently_counted(client, db_session, seeded, fake):
    fake()
    first_claim, first_doc = new_claim_with_bill(client)          # submitted WITHOUT being evaluated
    client.post(f"/api/documents/{first_doc}/confirm", json={"edits": {}})
    client.post(f"/api/claims/{first_claim}/submit")
    second_claim, _ = new_claim_with_bill(client)
    client.post(f"/api/claims/{second_claim}/evaluate")
    stored = [r for r in rows(db_session) if str(r.claim_id) == second_claim][0]
    agg = stored.check_detail["aggregation"]
    assert agg["members"] == [] and agg["unattributed_documents_not_counted"] == [first_doc]
