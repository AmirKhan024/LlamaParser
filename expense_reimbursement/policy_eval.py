"""Stage 3 evaluation service: claim -> per-unit policy decisions, stored.

Linear flow, one function (no agent framework):
  for each expense document of the claim:
    candidates = clauses for the document's category           (policy.category_clauses)
    model call: which clause per unit, what it read             (policy_select)
    strict validation of that answer                            (policy_select.validate_selection)
    facts = model's readings + code's resolutions               (this module)
    verdict = deterministic check                               (policy_check.check_unit)
  store every unit as an immutable policy_decisions row.

The model selects and interprets; code computes and decides. A unit whose model
answer fails validation (unknown clause id, amount that doesn't reconcile...)
is stored as insufficient_information with the reason -- never repaired.
If the model cannot be reached at all nothing is stored (the run is atomic and
a later retry isn't blocked by idempotency).
"""

import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import groq
from sqlalchemy.orm import Session

import policy as pol
import policy_check as pc
import policy_llm
import policy_select as ps
import repository
from categories import CATEGORIES, is_categorizable
from extract import MODEL as DEFAULT_MODEL
from models import Claim, Document, Employee, Extraction, PolicyDecision

# Read once at import time (same convention as CATEGORIZER in server.py).
POLICY_MODEL = os.environ.get("POLICY_MODEL") or DEFAULT_MODEL
PROMPT_VERSION = os.environ.get("POLICY_PROMPT_VERSION") or "v1"

_EVALUABLE_STATUSES = ("ready", "needs_review", "confirmed")


class PolicyNotSeeded(RuntimeError):
    pass


class PolicyModelDown(RuntimeError):
    """The model could not be reached (no key, quota, network). Nothing was stored."""


@dataclass
class EvaluationResult:
    decisions: list[PolicyDecision]
    evaluation_seq: int
    reused: bool
    policy_version: str
    prompt_version: str
    skipped: list[dict[str, str]] = field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _explanation(verdict: str, outcome: Optional[pc.CheckOutcome], currency: Optional[str], clause_id: str) -> str:
    """One sentence, composed by code from the deterministic outcome so it can
    never contradict the verdict (the model never states a verdict)."""
    if outcome is None:
        return "Could not be evaluated."
    comparison = pc.format_comparison(outcome.amount_compared, outcome.limit_applied, outcome.limit_unit, currency)
    if verdict == "compliant":
        if comparison:
            return f"Within the limit under {clause_id}: {comparison}."
        return f"No limit is exceeded and no condition of {clause_id} is violated."
    if verdict == "insufficient_information":
        missing = ", ".join(outcome.missing_fields)
        return f"Cannot be decided under {clause_id}: missing {missing}."
    first = outcome.reasons[0] if outcome.reasons else verdict
    text = first[0].upper() + first[1:]
    if comparison and outcome.comparison_result == "over_limit":
        return f"Over the limit under {clause_id}: {comparison}."
    return f"{text} ({clause_id})."


def _parse_year_month(text: Optional[str]) -> Optional[tuple[int, int]]:
    d = pc.parse_date(text)
    return (d.year, d.month) if d else None


def _has_approval_evidence(claim: Claim, siblings: list[dict]) -> bool:
    if (claim.note_to_approver or "").strip():
        return True
    return any(s.get("document_type") == "approval_correspondence" for s in siblings)


def _facts_for(unit: ps.ValidatedUnit, *, session: Session, claim: Claim, employee: Employee, document: Document,
               extraction: Extraction, reference: dict, now: datetime) -> tuple[pc.UnitFacts, dict[str, Any]]:
    clause = unit.clause
    detail: dict[str, Any] = {}
    dims = dict(unit.dims)
    dims["grade"] = employee.grade.strip().upper() if employee.grade else None

    bill = pc.parse_date(extraction.bill_date or (extraction.fields or {}).get("date"))
    reference_date = claim.submitted_at.date() if claim.submitted_at else now.date()
    days_since = (reference_date - bill).days if bill else None

    period: Optional[list[Decimal]] = None
    if clause.limit_unit == "per_month":
        ym = _parse_year_month(extraction.bill_date or (extraction.fields or {}).get("date"))
        if ym and extraction.category:
            peers, unattributed = repository.monthly_peer_amounts(
                session, employee_id=employee.id, category=extraction.category, currency=extraction.currency,
                clause_id=clause.clause_id, year=ym[0], month=ym[1], exclude_document_id=document.id,
                current_claim_id=claim.id,
            )
            period = [Decimal(1)] * len(peers) if clause.limit_kind == "count" else [a for _, a in peers]
            detail["aggregation"] = {
                "month": f"{ym[0]}-{ym[1]:02d}",
                "scope": f"same employee, category, currency and clause ({clause.clause_id})",
                "members": [{"document_id": str(i), "amount": str(a)} for i, a in peers],
                "unattributed_documents_not_counted": [str(i) for i in unattributed],
            }

    facts = pc.UnitFacts(
        currency=extraction.currency, base_amount=unit.base_amount, base_of_percent=unit.base_of_percent,
        nights=unit.nights, days=unit.days, persons=unit.persons, distance_km=unit.distance_km,
        days_since_expense=days_since, grade=employee.grade, grade_rank=pol.grade_rank(reference, employee.grade),
        dims=dims, period_amounts=period, judgments=unit.judgments, documentation=unit.documentation,
        approval_evidenced=unit.approval_evidenced,
    )
    return facts, detail


def _line_ref(refs: list[int]) -> Optional[str]:
    return ",".join(str(r) for r in refs) if refs else None


# ---------------------------------------------------------------- evaluation

def evaluate_claim(
    session: Session,
    claim: Claim,
    employee: Employee,
    *,
    actor_id: uuid.UUID,
    policy_version: Optional[str] = None,
    prompt_version: Optional[str] = None,
    force: bool = False,
    use_cache: bool = True,
    cache_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> EvaluationResult:
    pv = repository.get_policy_version(session, policy_version)
    if pv is None:
        raise PolicyNotSeeded("no policy version is loaded; run scripts/build_policy.py then scripts/seed_policy.py")
    prompt_version = prompt_version or PROMPT_VERSION
    prompt_text, prompt_sha = ps.load_prompt(prompt_version)
    now = now or datetime.now()

    if not force:
        existing = repository.latest_decision_run(session, claim.id, policy_version=pv.version, prompt_version=prompt_version)
        if existing:
            return EvaluationResult(existing, existing[0].evaluation_seq, True, pv.version, prompt_version)

    clauses = [pol.clause_from_row(r) for r in pv.clauses]
    policy_ids = {c.clause_id for c in clauses}
    reference = pv.reference_data

    documents = [d for d in claim.documents if d.status != "removed"]
    latest = {d.id: (d.extractions[-1] if d.extractions else None) for d in documents}
    skipped: list[dict[str, str]] = []
    pending: list[dict[str, Any]] = []   # rows built but not yet given a seq

    for document in documents:
        extraction = latest[document.id]
        if extraction is None or document.status not in _EVALUABLE_STATUSES:
            skipped.append({"document_id": str(document.id), "reason": f"document is {document.status}"})
            continue
        if not is_categorizable(extraction.document_type):
            skipped.append({"document_id": str(document.id), "reason": "supporting document, not an expense"})
            continue
        if not extraction.category or extraction.category not in CATEGORIES:
            skipped.append({"document_id": str(document.id), "reason": "no category assigned"})
            continue

        category = extraction.category
        candidates = pol.category_clauses(clauses, category)
        siblings = [
            ps.sibling_summary(d, latest[d.id], latest[d.id].category)
            for d in documents if d.id != document.id and latest[d.id] is not None
        ]
        payload = ps.build_user_payload(
            employee=employee, claim=claim, document=document, extraction=extraction, category_id=category,
            category_label=CATEGORIES[category].label, siblings=siblings, candidates=candidates,
        )
        messages = ps.build_messages(prompt_text, payload)
        raw_request = {"model": POLICY_MODEL, "prompt_version": prompt_version, "prompt_sha256": prompt_sha, "user_payload": payload}
        scope = f"{claim.id}__{pv.version}__{prompt_version}__{document.id}"

        call = None
        call_error: Optional[str] = None
        try:
            call = ps.call_model(messages, model=POLICY_MODEL, scope=scope, cache_dir=cache_dir, use_cache=use_cache)
        except policy_llm.PolicyModelError as exc:
            call_error = str(exc)
        except policy_llm.PolicyModelUnavailable as exc:
            raise PolicyModelDown(str(exc)) from exc
        except (groq.RateLimitError, groq.APIConnectionError, groq.APIStatusError) as exc:
            raise PolicyModelDown(f"{type(exc).__name__}: {str(exc)[:300]}") from exc

        common = dict(
            claim_id=claim.id, document_id=document.id, extraction_id=extraction.id, category_used=category,
            category_confidence=extraction.category_confidence, category_method=extraction.category_method or "unknown",
            policy_version=pv.version, prompt_version=prompt_version, model_name=POLICY_MODEL,
            currency=extraction.currency, raw_request=raw_request,
        )
        parsed: Any = None
        results: list[Any] = []
        structural_failure: Optional[tuple[str, str]] = call_error and ("model_error", call_error)
        if call is not None:
            try:
                units = ps.parse_response(call.raw_text)
                parsed = {"units": units}
                results = ps.validate_selection(
                    units, fields=extraction.fields or {}, n_line_items=len((extraction.fields or {}).get("line_items") or []),
                    candidates=candidates, policy_clause_ids=policy_ids, reference=reference,
                    currency=extraction.currency, has_approval_evidence=_has_approval_evidence(claim, siblings),
                )
            except ps.SelectionError as exc:
                structural_failure = (exc.code, exc.reason)
        raw_response = parsed if parsed is not None else ({"raw_text": call.raw_text} if call is not None else {"error": call_error})
        call_fields = dict(
            raw_response=raw_response, cache_hit=bool(call and call.cache_hit),
            latency_ms=call.latency_ms if call else None,
            prompt_tokens=call.prompt_tokens if call else None,
            completion_tokens=call.completion_tokens if call else None,
        )
        no_call = dict(raw_response=raw_response, cache_hit=False, latency_ms=None, prompt_tokens=None, completion_tokens=None)

        if structural_failure:
            code, reason = structural_failure
            pending.append(dict(
                common, **call_fields, unit_index=0, evaluation_mode="document", line_item_ref=None, clause_id=None,
                clause_text=None, verdict="insufficient_information", missing_fields=[f"system:{code}"],
                hard_failure_reason=reason, explanation=f"Could not be evaluated automatically: {reason}.",
                check_detail={"failure_code": code}, model_confidence=None, comparison_result="not_evaluated",
            ))
            continue

        n_lines = len((extraction.fields or {}).get("line_items") or [])
        for r in results:
            mode = "line_item" if n_lines else "document"
            fields_for_row = call_fields if r.unit_index == 0 else dict(no_call, raw_response=raw_response)
            base = dict(common, **fields_for_row, unit_index=r.unit_index, evaluation_mode=mode, line_item_ref=_line_ref(r.line_item_refs))
            if isinstance(r, ps.UnitFailure):
                clause = next((c for c in clauses if c.clause_id == r.clause_id), None)
                pending.append(dict(
                    base, clause_id=r.clause_id, clause_text=clause.verbatim_text if clause else None,
                    verdict="insufficient_information", missing_fields=[f"system:{r.code}"], hard_failure_reason=r.reason,
                    explanation=f"Could not be evaluated automatically: {r.reason}.", comparison_result="not_evaluated",
                    check_detail={"failure_code": r.code, "raw_unit": r.raw_unit}, model_confidence=None,
                ))
                continue
            facts, extra = _facts_for(r, session=session, claim=claim, employee=employee, document=document,
                                      extraction=extraction, reference=reference, now=now)
            outcome = pc.check_unit(r.clause, facts)
            missing = list(dict.fromkeys(outcome.missing_fields + (r.model_missing if outcome.verdict == "insufficient_information" else [])))
            detail = {
                **outcome.detail, **extra, "reasons": outcome.reasons, "validator_notes": r.notes,
                "dims": facts.dims, "nights": facts.nights, "days": facts.days, "persons": facts.persons,
                "distance_km": str(facts.distance_km) if facts.distance_km is not None else None,
                "days_since_expense": facts.days_since_expense, "model_explanation": r.explanation,
                "clause_confidence": r.clause_confidence, "model_missing_fields": r.model_missing,
                "call_shared_with_unit_0": r.unit_index != 0,
            }
            pending.append(dict(
                base, clause_id=r.clause.clause_id, clause_text=r.clause.verbatim_text, verdict=outcome.verdict,
                limit_applied=outcome.limit_applied, limit_unit=outcome.limit_unit, limit_currency=outcome.limit_currency,
                amount_compared=outcome.amount_compared if outcome.amount_compared is not None else r.base_amount,
                amount_derivation=r.amount_derivation or None, comparison_result=outcome.comparison_result,
                explanation=_explanation(outcome.verdict, outcome, extraction.currency, r.clause.clause_id),
                model_confidence=r.confidence, missing_fields=missing if outcome.verdict == "insufficient_information" else outcome.missing_fields,
                hard_failure_reason=None, check_detail=detail,
            ))

    # Persist atomically. Lock the claim, re-check idempotency (a concurrent
    # request may have finished first), then take the next sequence number.
    repository.lock_claim(session, claim.id)
    if not force:
        existing = repository.latest_decision_run(session, claim.id, policy_version=pv.version, prompt_version=prompt_version)
        if existing:
            session.rollback()
            return EvaluationResult(existing, existing[0].evaluation_seq, True, pv.version, prompt_version, skipped)
    seq = repository.next_evaluation_seq(session, claim.id)
    rows = [PolicyDecision(evaluation_seq=seq, **{"missing_fields": [], "check_detail": {}, **p}) for p in pending]
    repository.add_policy_decisions(
        session, rows, claim_id=claim.id, actor_id=actor_id,
        payload={"evaluation_seq": seq, "policy_version": pv.version, "prompt_version": prompt_version,
                 "units": len(rows), "forced": force, "skipped": skipped},
    )
    return EvaluationResult(rows, seq, False, pv.version, prompt_version, skipped)


# -------------------------------------------------------------------- views

def effective_verdict(d: PolicyDecision) -> str:
    return d.human_verdict or d.verdict


def decision_view(d: PolicyDecision) -> dict[str, Any]:
    """API shape. Like the Stage 2 category view, never exposes the
    categorizer's confidence/method (kept in the row for analysis)."""
    def money(v): return str(v) if v is not None else None
    cat = CATEGORIES.get(d.category_used)
    return {
        "id": str(d.id), "claim_id": str(d.claim_id), "document_id": str(d.document_id),
        "evaluation_seq": d.evaluation_seq, "unit_index": d.unit_index,
        "evaluation_mode": d.evaluation_mode, "line_item_ref": d.line_item_ref,
        "category": {"id": d.category_used, "label": cat.label if cat else d.category_used},
        "clause": {"id": d.clause_id, "text": d.clause_text},
        "verdict": d.verdict, "effective_verdict": effective_verdict(d),
        "limit_applied": money(d.limit_applied), "limit_unit": d.limit_unit, "limit_currency": d.limit_currency,
        "amount_compared": money(d.amount_compared), "currency": d.currency,
        "comparison_text": pc.format_comparison(d.amount_compared, d.limit_applied, d.limit_unit, d.currency)
        if d.comparison_result in ("within_limit", "at_limit", "over_limit") else None,
        "comparison_result": d.comparison_result, "amount_derivation": d.amount_derivation,
        "explanation": d.explanation, "confidence": d.model_confidence, "missing_fields": d.missing_fields,
        "hard_failure_reason": d.hard_failure_reason,
        "policy_version": d.policy_version, "prompt_version": d.prompt_version, "model": d.model_name,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "override": {
            "human_verdict": d.human_verdict, "human_clause_id": d.human_clause_id, "human_note": d.human_note,
            "overridden_at": d.overridden_at.isoformat() if d.overridden_at else None,
            "overridden_by": str(d.overridden_by) if d.overridden_by else None,
        } if d.human_verdict else None,
    }


def decisions_payload(claim: Claim, decisions: list[PolicyDecision], *, reused: bool = False,
                      skipped: Optional[list[dict]] = None) -> dict[str, Any]:
    """Decisions plus per-document and claim roll-ups (worst outcome wins,
    over the EFFECTIVE verdicts: a reviewer override replaces the model's)."""
    by_doc: dict[str, list[PolicyDecision]] = {}
    for d in decisions:
        by_doc.setdefault(str(d.document_id), []).append(d)
    documents = {
        doc_id: {"verdict": pc.rollup(effective_verdict(d) for d in rows), "model_verdict": pc.rollup(d.verdict for d in rows)}
        for doc_id, rows in by_doc.items()
    }
    current_extractions = {
        str(doc.id): doc.extractions[-1].id for doc in claim.documents if doc.status != "removed" and doc.extractions
    }
    stale = bool(decisions) and any(
        current_extractions.get(str(d.document_id)) not in (None, d.extraction_id) for d in decisions
    )
    return {
        "claim_id": str(claim.id),
        "evaluation_seq": decisions[0].evaluation_seq if decisions else None,
        "policy_version": decisions[0].policy_version if decisions else None,
        "prompt_version": decisions[0].prompt_version if decisions else None,
        "reused": reused, "stale": stale,
        "verdict": pc.rollup(effective_verdict(d) for d in decisions) if decisions else None,
        "documents": documents,
        "decisions": [decision_view(d) for d in decisions],
        "skipped": skipped or [],
    }
