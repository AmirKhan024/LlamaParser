"""Stage 4 assessment service: build the rule context from the Stage 1-3 tables
(read-only -- nothing in Stages 1-3 is modified), run the deterministic rules,
score in code, optionally ask the model for a grounded narrative, and store an
immutable `fraud_assessments` row.

Rules and score never depend on the model: if the narrative call fails or is
rejected the assessment is still complete and the UI shows the raw rules.
"""

import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import groq
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

import fraud_config as cfg
import fraud_narrative as narr
import fraud_rules as fr
import policy_check as pc
import policy_llm
import repository
from categories import is_categorizable
from extract import MODEL as DEFAULT_MODEL
from models import Claim, Document, Employee, FraudAssessment

FRAUD_MODEL = os.environ.get("FRAUD_MODEL") or DEFAULT_MODEL
COMPANY_CURRENCY = os.environ.get("COMPANY_CURRENCY") or "INR"
_EVALUABLE = ("ready", "needs_review", "confirmed")
BAND_ORDER = {"high": 0, "medium": 1, "unassessable": 2, "low": 3}


# ------------------------------------------------------------------ context

def _doc_infos(claims: list[Claim]) -> list[fr.DocInfo]:
    out = []
    for claim in claims:
        for doc in claim.documents:
            if doc.status not in _EVALUABLE or not doc.extractions:
                continue
            ext = doc.extractions[-1]
            if not is_categorizable(ext.document_type):
                continue          # supporting evidence (approval emails), not an expense
            fields = ext.fields or {}
            out.append(fr.DocInfo(
                document_id=str(doc.id), claim_id=str(claim.id), employee_id=str(claim.employee_id),
                employee_name=claim.employee.name, name=doc.original_name, vendor=ext.vendor_name,
                date=pc.parse_date(ext.bill_date or fields.get("date")), amount=ext.amount, currency=ext.currency,
                category=ext.category,
            ))
    return out


def _thresholds(session: Session) -> dict[str, list[fr.Threshold]]:
    """Approval and documentation thresholds READ from the active policy's clauses
    (requires_approval_above, documentation_required.min_amount) -- nothing hardcoded."""
    pv = repository.get_policy_version(session)
    out: dict[str, list[fr.Threshold]] = {}
    if pv is None:
        return out
    for c in pv.clauses:
        found: list[fr.Threshold] = []
        for cur, amount in (c.requires_approval_above or {}).items():
            if Decimal(str(amount)) > 0:
                found.append(fr.Threshold(c.clause_id, "approval", cur, Decimal(str(amount))))
        for d in c.documentation_required or []:
            m = d.get("min_amount")
            if isinstance(m, dict):
                found += [fr.Threshold(c.clause_id, "documentation", cur, Decimal(str(v))) for cur, v in m.items()]
            elif m is not None:
                found.append(fr.Threshold(c.clause_id, "documentation", COMPANY_CURRENCY, Decimal(str(m))))
        for cat in c.applies_to_categories:
            out.setdefault(cat, []).extend(found)
    return out


def build_context(session: Session, claim: Claim) -> fr.Context:
    claims = list(session.scalars(
        select(Claim).options(selectinload(Claim.documents).selectinload(Document.extractions), selectinload(Claim.employee))
        .where((Claim.status == "submitted") | (Claim.id == claim.id))
    ))
    universe = _doc_infos(claims)
    this = [d for d in universe if d.claim_id == str(claim.id)]

    latest_ext = {}
    with_ai: set[str] = set()
    for doc in claim.documents:
        if doc.status == "removed" or not doc.extractions:
            continue
        latest_ext[doc.id] = doc.extractions[-1].id
        if any(e.source == "ai" for e in doc.extractions):
            with_ai.add(str(doc.id))
    corrections = []
    for doc_id, ext_id in latest_ext.items():
        for c in repository.list_corrections(session, doc_id):
            if c.extraction_id == ext_id:
                corrections.append(fr.CorrectionInfo(str(doc_id), c.field_path, c.direction, c.reason, c.ai_value, c.employee_value))

    def decisions(claim_id: uuid.UUID) -> list[fr.DecisionInfo]:
        return [fr.DecisionInfo(str(d.claim_id), str(d.document_id), d.unit_index, d.clause_id, d.human_verdict or d.verdict)
                for d in repository.latest_decision_run(session, claim_id)]

    mine = [c for c in claims if c.employee_id == claim.employee_id]
    return fr.Context(
        claim_id=str(claim.id), employee_id=str(claim.employee_id), docs=this, universe=universe,
        thresholds=_thresholds(session), corrections=corrections, docs_with_ai_extraction=with_ai,
        decisions_this_claim=decisions(claim.id), decisions_employee=[x for c in mine for x in decisions(c.id)],
    )


# --------------------------------------------------------------- assessment

def _rule_json(r: fr.RuleResult) -> dict[str, Any]:
    return {"rule_id": r.rule_id, "description": r.description, "weight": r.weight, "status": r.status,
            "reason": r.reason, "evidence": r.evidence}


def assess_claim(
    session: Session, claim: Claim, *, run_id: Optional[uuid.UUID] = None, narrate: bool = True,
    prompt_version: Optional[str] = None, use_cache: bool = True, cache_dir: Optional[Path] = None,
) -> FraudAssessment:
    ctx = build_context(session, claim)
    result = fr.run_rules(ctx)
    fired = [r for r in result.results if r.status == "fired"]
    prompt_version = prompt_version or cfg.NARRATIVE_PROMPT_VERSION

    row = FraudAssessment(
        claim_id=claim.id, employee_id=claim.employee_id, run_id=run_id or uuid.uuid4(), ruleset_version=cfg.RULESET_VERSION,
        rules_fired=[_rule_json(r) for r in fired],
        rules_not_applicable=[_rule_json(r) for r in result.results if r.status == "not_applicable"],
        rules_clear=[_rule_json(r) for r in result.results if r.status == "clear"],
        risk_score=result.risk_score, risk_band=result.risk_band,
        assessable_signal_count=result.assessable, total_signal_count=result.total,
        narrative_status="not_needed",
    )
    if fired and narrate:
        prompt_text, prompt_sha = narr.load_prompt(prompt_version)
        payload = narr.build_payload(claim.title, claim.employee.name, result.risk_band, fired)
        row.model_name, row.prompt_version = FRAUD_MODEL, prompt_version
        row.raw_request = {"model": FRAUD_MODEL, "prompt_version": prompt_version, "prompt_sha256": prompt_sha, "user_payload": payload}
        try:
            call = narr.call_model(narr.build_messages(prompt_text, payload), model=FRAUD_MODEL,
                                   scope=f"fraud__{claim.id}__{cfg.RULESET_VERSION}__{prompt_version}",
                                   cache_dir=cache_dir, use_cache=use_cache)
            row.latency_ms, row.prompt_tokens, row.completion_tokens = call.latency_ms, call.prompt_tokens, call.completion_tokens
            text, rejection = narr.ground(call.raw_text, result.results)
            row.raw_response = {"raw_text": call.raw_text, "rejection": rejection}
            row.narrative, row.narrative_status = (text, "ok") if text else (None, "rejected")
        except (policy_llm.PolicyModelUnavailable, groq.RateLimitError, groq.APIConnectionError, groq.APIStatusError) as exc:
            row.narrative_status, row.raw_response = "unavailable", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        except policy_llm.PolicyModelError as exc:
            row.narrative_status, row.raw_response = "error", {"error": str(exc)}
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# -------------------------------------------------------------------- views

def assessment_view(a: FraudAssessment, claim: Optional[Claim] = None) -> dict[str, Any]:
    return {
        "id": str(a.id), "claim_id": str(a.claim_id), "employee_id": str(a.employee_id), "run_id": str(a.run_id),
        "claim_title": claim.title if claim else None, "employee_name": claim.employee.name if claim else None,
        "ruleset_version": a.ruleset_version, "risk_score": a.risk_score, "risk_band": a.risk_band,
        "assessable_signals": f"{a.assessable_signal_count} of {a.total_signal_count}",
        "assessable_signal_count": a.assessable_signal_count, "total_signal_count": a.total_signal_count,
        "rules_fired": a.rules_fired, "rules_not_applicable": a.rules_not_applicable, "rules_clear": a.rules_clear,
        "narrative": a.narrative, "narrative_status": a.narrative_status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "review": {"human_assessment": a.human_assessment, "human_note": a.human_note,
                   "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None} if a.human_assessment else None,
    }


def latest_assessment(session: Session, claim_id: uuid.UUID) -> Optional[FraudAssessment]:
    return session.scalar(select(FraudAssessment).where(FraudAssessment.claim_id == claim_id)
                          .order_by(FraudAssessment.created_at.desc()).limit(1))


def review_queue(session: Session) -> list[dict[str, Any]]:
    """Latest assessment per claim, riskiest band first (then score)."""
    seen: set[uuid.UUID] = set()
    latest: list[FraudAssessment] = []
    for a in session.scalars(select(FraudAssessment).order_by(FraudAssessment.created_at.desc())):
        if a.claim_id not in seen:
            seen.add(a.claim_id)
            latest.append(a)
    claims = {c.id: c for c in session.scalars(
        select(Claim).options(selectinload(Claim.employee)).where(Claim.id.in_(seen)))} if seen else {}
    latest.sort(key=lambda a: (BAND_ORDER.get(a.risk_band, 9), -a.risk_score))
    return [assessment_view(a, claims.get(a.claim_id)) for a in latest]


def review_assessment(session: Session, a: FraudAssessment, *, decision: str, note: Optional[str], actor_id: uuid.UUID) -> FraudAssessment:
    a.human_assessment, a.human_note = decision, note
    a.reviewed_at, a.reviewed_by = datetime.utcnow(), actor_id
    session.add(repository_audit(a, actor_id, decision))
    session.commit()
    session.refresh(a)
    return a


def repository_audit(a: FraudAssessment, actor_id: uuid.UUID, decision: str):
    from models import AuditEvent

    return AuditEvent(claim_id=a.claim_id, actor_id=actor_id, action="fraud_reviewed",
                      payload={"assessment_id": str(a.id), "decision": decision, "risk_band": a.risk_band})
