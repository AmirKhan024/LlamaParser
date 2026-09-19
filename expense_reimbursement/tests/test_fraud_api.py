"""Stage 4 assessment service + API against the real test Postgres (narrative model mocked).
Reads Stage 1-3 tables; asserts nothing in them is modified."""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import fraud_narrative
import policy as pol
import policy_llm
import repository
from conftest import MOBILE_PDF
from models import Claim, Correction, Extraction, FraudAssessment
from policy import Clause, LimitEntry
from test_api import upload, wait_until_processed
from test_policy_eval_api import _clauses, REF, FakeModel, new_claim_with_bill  # noqa: F401  (fixture helpers)
import policy_select


class FakeNarrator:
    def __init__(self, summary=None, refs=None, error=None):
        self.calls, self.summary, self.refs, self.error = 0, summary, refs, error

    def __call__(self, messages, *, model, scope, cache_dir=None, use_cache=True):
        self.calls += 1
        if self.error:
            raise self.error
        payload = json.loads(messages[1]["content"])
        fired = [r["rule_id"] for r in payload["fired_rules"]]
        summary = self.summary or "Flagged by the rules listed."
        return policy_llm.LLMCall(json.dumps({"summary": summary, "rules_referenced": self.refs or fired}), 900, 60, 3, False, model)


@pytest.fixture
def narrator(monkeypatch):
    def install(**kw):
        n = FakeNarrator(**kw)
        monkeypatch.setattr(fraud_narrative, "call_model", n)
        return n
    return install


def submit_bill(client):
    claim_id, doc_id = new_claim_with_bill(client)
    client.post(f"/api/documents/{doc_id}/confirm", json={"edits": {}})
    assert client.post(f"/api/claims/{claim_id}/submit").status_code == 200
    return claim_id, doc_id


def test_clean_claim_has_no_fired_rules_and_reports_coverage(client, narrator):
    n = narrator()
    claim_id, _ = submit_bill(client)
    body = client.post(f"/api/claims/{claim_id}/assess-fraud").json()
    assert body["rules_fired"] == [] and body["narrative_status"] == "not_needed" and n.calls == 0
    assert body["assessable_signals"].endswith(" of 9") and body["risk_band"] in ("low", "unassessable")
    # policy_repeat_violation could not assess (claim never evaluated by Stage 3): reported, not scored
    na = {r["rule_id"]: r["reason"] for r in body["rules_not_applicable"]}
    assert "policy_repeat_violation" in na and "not evaluated" in na["policy_repeat_violation"]
    assert client.get(f"/api/claims/{claim_id}/fraud").json()["assessment"]["id"] == body["id"]


def test_duplicate_across_two_claims_fires_with_evidence_and_a_grounded_narrative(client, db_session, narrator):
    n = narrator(summary="The same vendor and amount appear on two claims.")
    first, _ = submit_bill(client)
    second, second_doc = submit_bill(client)                 # same bill again, same employee, same day
    body = client.post(f"/api/claims/{second}/assess-fraud").json()
    fired = {r["rule_id"]: r for r in body["rules_fired"]}
    assert "duplicate_same_employee" in fired and "round_number" not in fired
    ev = fired["duplicate_same_employee"]["evidence"]["matches"][0]
    assert ev["this_claim"]["document_id"] == second_doc and ev["days_apart"] == 0
    assert body["risk_score"] == fired["duplicate_same_employee"]["weight"] == 30 and body["risk_band"] == "medium"
    assert body["narrative_status"] == "ok" and body["narrative"] == "The same vendor and amount appear on two claims." and n.calls == 1
    stored = db_session.scalar(select(FraudAssessment).where(FraudAssessment.id == uuid.UUID(body["id"])))
    assert stored.raw_request["user_payload"]["fired_rules"][0]["rule_id"] == "duplicate_same_employee"
    assert stored.prompt_version == "v1" and stored.prompt_tokens == 900


def test_ungrounded_narrative_is_dropped_but_rules_and_score_remain(client, narrator):
    narrator(summary="Also a round number and weekend claim.")                  # mentions rules that did not fire
    submit_bill(client)
    second, _ = submit_bill(client)
    body = client.post(f"/api/claims/{second}/assess-fraud").json()
    assert body["narrative"] is None and body["narrative_status"] == "rejected"
    assert body["risk_score"] == 30 and [r["rule_id"] for r in body["rules_fired"]] == ["duplicate_same_employee"]


def test_model_unavailable_still_produces_the_full_assessment(client, narrator):
    narrator(error=policy_llm.PolicyModelUnavailable("no key"))
    submit_bill(client)
    second, _ = submit_bill(client)
    body = client.post(f"/api/claims/{second}/assess-fraud").json()
    assert body["narrative_status"] == "unavailable" and body["risk_score"] == 30 and body["rules_fired"]


def test_thresholds_are_read_from_the_policy_tables(client, db_session, narrator):
    narrator()
    limit = Clause("12.9", "12", 5, "12.9. approval above 1,500", ("phone_internet",), requires_approval_above={"INR": pol._dec("1500")})
    repository.create_policy_version(db_session, version="vf", source_sha256="x", reference_data=REF, build_meta={},
                                     clauses=[pol.clause_to_dict(limit)])
    claim_id, _ = submit_bill(client)                        # a 1,417.18 phone bill: 5.5% below 1,500 -> outside the 5% band
    assert client.post(f"/api/claims/{claim_id}/assess-fraud").json()["rules_fired"] == []
    tight = Clause("12.9", "12", 5, "12.9. approval above 1,450", ("phone_internet",), requires_approval_above={"INR": pol._dec("1450")})
    repository.create_policy_version(db_session, version="vf2", source_sha256="y", reference_data=REF, build_meta={},
                                     clauses=[pol.clause_to_dict(tight)])
    body = client.post(f"/api/claims/{claim_id}/assess-fraud").json()      # active policy is now vf2: 2.3% below 1,450
    assert [r["rule_id"] for r in body["rules_fired"]] == ["threshold_gaming"]
    assert body["rules_fired"][0]["evidence"]["hits"][0]["clause_id"] == "12.9"


def test_stage1_corrections_are_read_and_stage1_tables_are_not_modified(client, db_session, narrator):
    narrator()
    claim_id, doc_id = new_claim_with_bill(client)
    r = client.post(f"/api/documents/{doc_id}/confirm", json={"edits": {"total": "1900.00"}, "reason": "Late fee added."})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/claims/{claim_id}/submit").status_code == 200
    before = (db_session.scalar(select(text("count(*)")).select_from(Extraction)), db_session.scalar(select(text("count(*)")).select_from(Correction)))
    body = client.post(f"/api/claims/{claim_id}/assess-fraud").json()
    fired = {r["rule_id"]: r for r in body["rules_fired"]}
    assert "correction_upward" in fired and "correction_guardrail" in fired
    assert fired["correction_guardrail"]["evidence"]["corrections"][0]["reason"] == "Late fee added."
    assert body["risk_band"] == "high"                                       # 20 + 35 = 55
    db_session.expire_all()
    assert before == (db_session.scalar(select(text("count(*)")).select_from(Extraction)), db_session.scalar(select(text("count(*)")).select_from(Correction)))


def test_policy_repeat_violation_reads_stage3_decisions(client, db_session, narrator, monkeypatch):
    narrator()
    repository.create_policy_version(db_session, version="vtest", source_sha256="x", reference_data=REF, build_meta={},
                                     clauses=[pol.clause_to_dict(c) for c in _clauses()])
    m = FakeModel(unit_overrides={"stated_amount": "1417.18"})
    m.unit_overrides = {}
    monkeypatch.setattr(policy_select, "call_model", m)
    # a 800 cap for grade L1 would be violated; make the seed employee L1 so the 1,417.18 bill breaches 12.1
    emp = repository.get_or_create_seed_employee(db_session)
    emp.grade = "L1"
    db_session.commit()
    claims = []
    for _ in range(2):
        cid, doc_id = new_claim_with_bill(client)
        assert client.post(f"/api/claims/{cid}/evaluate").json()["verdict"] == "violation"
        client.post(f"/api/documents/{doc_id}/confirm", json={"edits": {}})
        client.post(f"/api/claims/{cid}/submit")
        claims.append(cid)
    body = client.post(f"/api/claims/{claims[1]}/assess-fraud").json()
    fired = {r["rule_id"]: r for r in body["rules_fired"]}
    assert fired["policy_repeat_violation"]["evidence"]["clauses"][0]["clause_id"] == "12.1"
    assert fired["policy_repeat_violation"]["evidence"]["clauses"][0]["violations"] == 2


def test_review_queue_is_sorted_by_band_and_review_does_not_mutate_the_assessment(client, db_session, narrator):
    narrator()
    clean, _ = submit_bill(client)
    first, _ = submit_bill(client)
    dup, _ = submit_bill(client)
    client.post(f"/api/claims/{clean}/assess-fraud")
    flagged = client.post(f"/api/claims/{dup}/assess-fraud").json()
    queue = client.get("/api/fraud/queue").json()["items"]
    assert [q["claim_id"] for q in queue][0] == dup and queue[0]["risk_band"] == "medium"

    r = client.post(f"/api/fraud/{flagged['id']}/review", json={"human_assessment": "confirmed", "human_note": "genuine duplicate"})
    assert r.status_code == 200 and r.json()["review"]["human_assessment"] == "confirmed"
    stored = db_session.scalar(select(FraudAssessment).where(FraudAssessment.id == uuid.UUID(flagged["id"])))
    assert stored.risk_score == 30 and stored.human_note == "genuine duplicate" and stored.reviewed_by is not None
    assert client.post(f"/api/fraud/{flagged['id']}/review", json={"human_assessment": "maybe"}).status_code == 422
    assert client.post("/api/fraud/not-a-uuid/review", json={"human_assessment": "dismissed"}).status_code == 404


def test_assessments_are_immutable_and_each_run_appends(client, db_session, narrator):
    narrator()
    claim_id, _ = submit_bill(client)
    a = client.post(f"/api/claims/{claim_id}/assess-fraud").json()
    b = client.post(f"/api/claims/{claim_id}/assess-fraud").json()
    assert a["id"] != b["id"] and a["run_id"] != b["run_id"]
    assert len(db_session.scalars(select(FraudAssessment)).all()) == 2
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("UPDATE fraud_assessments SET risk_score = 99"))
    db_session.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        db_session.execute(text("DELETE FROM fraud_assessments"))
    db_session.rollback()
    db_session.execute(text("UPDATE fraud_assessments SET human_note = 'ok'"))
    db_session.commit()
