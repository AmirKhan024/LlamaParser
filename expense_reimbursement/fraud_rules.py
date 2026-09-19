"""Stage 4 deterministic fraud/anomaly rules. Pure functions over a `Context`
(no DB, no model). Each rule returns a `RuleResult` with one of three statuses:

  fired           the rule's data was present and the pattern was found
  clear           the rule's data was present and NOTHING was found ("no risk found")
  not_applicable  the data the rule needs is absent -- it could not assess
                  (reason says exactly what is missing); never a low score

A claim's risk output keeps "no risk found" and "could not assess" apart:
`assessable_signals` = fired + clear rules out of all rules. Rules never guess
across a gap: a document lacking the field a rule depends on is skipped by that
rule and named in the evidence/reason.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

import fraud_config as cfg


# ------------------------------------------------------------------- inputs

@dataclass(frozen=True)
class DocInfo:
    document_id: str
    claim_id: str
    employee_id: str
    employee_name: str
    name: str                     # original file name
    vendor: Optional[str]
    date: Optional[date]
    amount: Optional[Decimal]
    currency: Optional[str]
    category: Optional[str]


@dataclass(frozen=True)
class Threshold:
    clause_id: str
    kind: str                     # "approval" | "documentation"
    currency: str
    amount: Decimal


@dataclass(frozen=True)
class CorrectionInfo:
    document_id: str
    field_path: str
    direction: Optional[str]
    reason: Optional[str]
    ai_value: Any
    employee_value: Any


@dataclass(frozen=True)
class DecisionInfo:
    claim_id: str
    document_id: str
    unit_index: int
    clause_id: Optional[str]
    verdict: str                  # the EFFECTIVE verdict (a reviewer label wins)


@dataclass
class Context:
    claim_id: str
    employee_id: str
    docs: list[DocInfo]                                   # this claim's expense documents
    universe: list[DocInfo]                               # every considered document (incl. this claim's)
    thresholds: dict[str, list[Threshold]] = field(default_factory=dict)   # category -> thresholds ("*" = every category)
    corrections: list[CorrectionInfo] = field(default_factory=list)        # this claim's docs' latest-version corrections
    docs_with_ai_extraction: set[str] = field(default_factory=set)
    decisions_this_claim: list[DecisionInfo] = field(default_factory=list)
    decisions_employee: list[DecisionInfo] = field(default_factory=list)   # latest run per claim, incl. this claim


@dataclass
class RuleResult:
    rule_id: str
    description: str
    weight: int
    status: str                   # fired | clear | not_applicable
    reason: str                   # why fired / why clear / why not applicable
    evidence: dict[str, Any] = field(default_factory=dict)


def _ref(d: DocInfo) -> dict[str, Any]:
    return {
        "document_id": d.document_id, "claim_id": d.claim_id, "employee": d.employee_name, "file": d.name,
        "vendor": d.vendor, "date": d.date.isoformat() if d.date else None,
        "amount": str(d.amount) if d.amount is not None else None, "currency": d.currency,
    }


def _norm_vendor(v: Optional[str]) -> Optional[str]:
    if not v or not v.strip():
        return None
    out = re.sub(r"[^a-z0-9 ]", " ", v.lower())
    out = re.sub(r"\b(pvt|ltd|limited|private|llp|inc|the)\b", " ", out)
    return re.sub(r"\s+", " ", out).strip() or None


def _result(rule_id: str, description: str, status: str, reason: str, evidence: Optional[dict] = None) -> RuleResult:
    return RuleResult(rule_id, description, cfg.WEIGHTS[rule_id], status, reason, evidence or {})


def _missing(docs: list[DocInfo], needs: list[str]) -> dict[str, list[str]]:
    """document_id -> the needed fields it lacks."""
    out = {}
    for d in docs:
        gaps = [n for n in needs if getattr(d, n) in (None, "")]
        if gaps:
            out[d.document_id] = gaps
    return out


# --------------------------------------------------------------- duplicates

def _duplicates(ctx: Context, rule_id: str, description: str, same_employee: bool, window: int) -> RuleResult:
    needs = ["vendor", "amount", "date", "currency"]
    usable = [d for d in ctx.docs if not _missing([d], needs) and _norm_vendor(d.vendor)]
    if not usable:
        gaps = _missing(ctx.docs, needs) or {d.document_id: ["vendor"] for d in ctx.docs}
        return _result(rule_id, description, "not_applicable",
                       "no document of this claim has vendor, amount, currency and date", {"missing_fields_by_document": gaps})
    pairs: dict[frozenset, tuple[DocInfo, DocInfo]] = {}
    for d in usable:
        for o in ctx.universe:
            if o.document_id == d.document_id or (o.employee_id == d.employee_id) != same_employee:
                continue
            if _norm_vendor(o.vendor) != _norm_vendor(d.vendor) or o.amount != d.amount or o.currency != d.currency or o.date is None:
                continue
            if abs((o.date - d.date).days) <= window:
                pairs[frozenset((d.document_id, o.document_id))] = (d, o)
    if not pairs:
        return _result(rule_id, description, "clear", f"no same-vendor, same-amount document within {window} days",
                       {"documents_checked": len(usable)})
    matches = [{"this_claim": _ref(a), "matches": _ref(b), "days_apart": abs((a.date - b.date).days)} for a, b in pairs.values()]
    return _result(rule_id, description, "fired",
                   f"{len(matches)} same-vendor, same-amount pair(s) within {window} days", {"matches": matches})


def rule_duplicate_same_employee(ctx: Context) -> RuleResult:
    return _duplicates(ctx, "duplicate_same_employee", "Same vendor and amount claimed again by the same employee within a short window",
                       True, cfg.DUP_WINDOW_DAYS_SAME_EMPLOYEE)


def rule_duplicate_cross_employee(ctx: Context) -> RuleResult:
    return _duplicates(ctx, "duplicate_cross_employee", "Same vendor and amount claimed by two different employees within a short window",
                       False, cfg.DUP_WINDOW_DAYS_CROSS_EMPLOYEE)


# ---------------------------------------------------------- threshold gaming

def rule_threshold_gaming(ctx: Context) -> RuleResult:
    rid, desc = "threshold_gaming", "Amount just below an approval or receipt threshold from the policy"
    needs = ["amount", "currency", "category"]
    usable = [d for d in ctx.docs if not _missing([d], needs)]
    if not usable:
        return _result(rid, desc, "not_applicable", "no document of this claim has amount, currency and category",
                       {"missing_fields_by_document": _missing(ctx.docs, needs)})
    margin = Decimal(cfg.THRESHOLD_MARGIN_PCT) / 100
    hits = []
    for d in usable:
        for t in ctx.thresholds.get(d.category, []) + ctx.thresholds.get("*", []):
            if t.currency != d.currency or t.amount <= 0:
                continue
            if t.amount * (1 - margin) <= d.amount < t.amount:
                hits.append({"document": _ref(d), "threshold": str(t.amount), "threshold_kind": t.kind, "clause_id": t.clause_id,
                             "percent_below": str(((t.amount - d.amount) / t.amount * 100).quantize(Decimal("0.1")))})
    if not hits:
        return _result(rid, desc, "clear", f"no amount within {cfg.THRESHOLD_MARGIN_PCT}% below a policy threshold", {"documents_checked": len(usable)})
    return _result(rid, desc, "fired", f"{len(hits)} amount(s) within {cfg.THRESHOLD_MARGIN_PCT}% below a policy threshold", {"hits": hits})


# ------------------------------------------------------------------ velocity

def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def rule_velocity(ctx: Context) -> RuleResult:
    rid, desc = "velocity", "Unusually many documents, or unusually high spend, from one employee in one week"
    dated = [d for d in ctx.docs if d.date]
    if not dated:
        return _result(rid, desc, "not_applicable", "no document of this claim has a date",
                       {"missing_fields_by_document": _missing(ctx.docs, ["date"])})
    weeks = {_week_start(d.date) for d in dated}
    hits = []
    for w in sorted(weeks):
        same = [o for o in ctx.universe if o.employee_id == ctx.employee_id and o.date and _week_start(o.date) == w]
        totals: dict[str, Decimal] = {}
        for o in same:
            if o.amount is not None and o.currency in cfg.VELOCITY_MAX_TOTAL_PER_WEEK:
                totals[o.currency] = totals.get(o.currency, Decimal(0)) + o.amount
        over_count = len(same) > cfg.VELOCITY_MAX_DOCS_PER_WEEK
        # A weekly TOTAL only signals velocity when there are several documents; one big invoice
        # (a hotel stay, a course fee) is not a burst of claims.
        over_total = {c: str(t) for c, t in totals.items()
                      if len(same) >= 2 and t > cfg.VELOCITY_MAX_TOTAL_PER_WEEK[c]}
        if over_count or over_total:
            hits.append({"week_starting": w.isoformat(), "document_count": len(same), "count_limit": cfg.VELOCITY_MAX_DOCS_PER_WEEK,
                         "totals_over_limit": over_total, "documents": [_ref(o) for o in same]})
    if not hits:
        return _result(rid, desc, "clear", "weekly document count and spend are within the configured band", {"weeks_checked": len(weeks)})
    return _result(rid, desc, "fired", f"{len(hits)} week(s) above the configured band", {"weeks": hits})


# -------------------------------------------------------------- round number

def rule_round_number(ctx: Context) -> RuleResult:
    rid, desc = "round_number", "Amount is a suspiciously round number"
    usable = [d for d in ctx.docs if d.amount is not None and d.currency in cfg.ROUND_STEP]
    if not usable:
        return _result(rid, desc, "not_applicable", "no document of this claim has an amount in a currency with a configured round-number step",
                       {"missing_fields_by_document": _missing(ctx.docs, ["amount", "currency"])})
    hits = [_ref(d) for d in usable if d.amount >= cfg.ROUND_MIN[d.currency] and d.amount % cfg.ROUND_STEP[d.currency] == 0]
    if not hits:
        return _result(rid, desc, "clear", "no amount is a round multiple", {"documents_checked": len(usable)})
    return _result(rid, desc, "fired", f"{len(hits)} amount(s) are exact multiples of the configured step", {"documents": hits})


# ------------------------------------------------------------------ weekend

def rule_weekend_business(ctx: Context) -> RuleResult:
    rid, desc = "weekend_business", "Weekend date on a category that implies business activity"
    scope = [d for d in ctx.docs if d.category in cfg.WEEKEND_CATEGORIES]
    if not scope:
        return _result(rid, desc, "clear", "no document is in a category where a weekend date is suspicious", {})
    dated = [d for d in scope if d.date]
    if not dated:
        return _result(rid, desc, "not_applicable", "documents in scope have no date", {"missing_fields_by_document": _missing(scope, ["date"])})
    hits = [dict(_ref(d), weekday=d.date.strftime("%A"), category=d.category) for d in dated if d.date.weekday() >= 5]
    if not hits:
        return _result(rid, desc, "clear", "all in-scope documents are dated on weekdays", {"documents_checked": len(dated)})
    return _result(rid, desc, "fired", f"{len(hits)} in-scope document(s) dated on a weekend", {"documents": hits})


# -------------------------------------------------- policy: repeat violations

def rule_policy_repeat_violation(ctx: Context) -> RuleResult:
    rid, desc = "policy_repeat_violation", "The same policy clause repeatedly violated by one employee"
    if not ctx.decisions_this_claim:
        return _result(rid, desc, "not_applicable", "the claim has no Stage 3 policy decisions (not evaluated)", {})
    known = [x for x in ctx.decisions_this_claim if x.verdict != "insufficient_information"]
    if not known:
        return _result(rid, desc, "not_applicable", "every Stage 3 unit of this claim is insufficient_information; no verdict to build on",
                       {"units": len(ctx.decisions_this_claim)})
    hits = []
    for clause_id in sorted({x.clause_id for x in known if x.verdict == "violation" and x.clause_id}):
        history = [x for x in ctx.decisions_employee if x.verdict == "violation" and x.clause_id == clause_id]
        if len(history) >= cfg.POLICY_REPEAT_MIN_VIOLATIONS:
            hits.append({"clause_id": clause_id, "violations": len(history),
                         "claims": sorted({x.claim_id for x in history}), "documents": sorted({x.document_id for x in history})})
    if not hits:
        return _result(rid, desc, "clear", f"no clause violated at least {cfg.POLICY_REPEAT_MIN_VIOLATIONS} times", {"units_assessed": len(known)})
    return _result(rid, desc, "fired", f"{len(hits)} clause(s) violated repeatedly by this employee", {"clauses": hits})


# -------------------------------------------------------- Stage 1 corrections

def _is_money(field_path: str) -> bool:
    leaf = re.sub(r"\[\d+\]", "", field_path).split(".")[-1]
    return leaf in cfg.MONEY_FIELD_KEYS


def _corr_ref(c: CorrectionInfo, docs: dict[str, DocInfo]) -> dict[str, Any]:
    return {"document": _ref(docs[c.document_id]) if c.document_id in docs else {"document_id": c.document_id},
            "field": c.field_path, "ai_value": c.ai_value, "employee_value": c.employee_value, "direction": c.direction,
            "reason": c.reason}


def rule_correction_upward(ctx: Context) -> RuleResult:
    rid, desc = "correction_upward", "The employee edited a money amount UPWARD from what the document extraction read"
    if not ctx.docs_with_ai_extraction:
        return _result(rid, desc, "not_applicable", "no document has an AI extraction to compare against", {})
    docs = {d.document_id: d for d in ctx.docs}
    hits = [_corr_ref(c, docs) for c in ctx.corrections if _is_money(c.field_path) and c.direction == "increase"]
    if not hits:
        return _result(rid, desc, "clear", "no upward money edits", {"documents_checked": len(ctx.docs_with_ai_extraction)})
    return _result(rid, desc, "fired", f"{len(hits)} upward money edit(s)", {"corrections": hits})


def rule_correction_guardrail(ctx: Context) -> RuleResult:
    rid, desc = "correction_guardrail", "An edit tripped Stage 1's fraud guardrail (value contradicts the bill or isn't printed on it) and needed a stated reason"
    if not ctx.docs_with_ai_extraction:
        return _result(rid, desc, "not_applicable", "no document has an AI extraction to compare against", {})
    docs = {d.document_id: d for d in ctx.docs}
    hits = [_corr_ref(c, docs) for c in ctx.corrections if c.reason]
    if not hits:
        return _result(rid, desc, "clear", "no edit needed a guardrail reason", {"documents_checked": len(ctx.docs_with_ai_extraction)})
    return _result(rid, desc, "fired", f"{len(hits)} edit(s) carried a guardrail reason", {"corrections": hits})


RULES: list[Callable[[Context], RuleResult]] = [
    rule_duplicate_same_employee, rule_duplicate_cross_employee, rule_threshold_gaming, rule_velocity, rule_round_number,
    rule_weekend_business, rule_policy_repeat_violation, rule_correction_upward, rule_correction_guardrail,
]
RULE_IDS = list(cfg.WEIGHTS)
assert [f.__name__[5:] for f in RULES] == RULE_IDS, "RULES and fraud_config.WEIGHTS must list the same rules in the same order"


# -------------------------------------------------------------------- scoring

@dataclass
class Assessment:
    results: list[RuleResult]
    risk_score: int
    risk_band: str
    assessable: int
    total: int


def score(results: list[RuleResult]) -> tuple[int, str, int, int]:
    """(risk_score, band, assessable, total). Computed here, in code, from the
    fired rules' weights -- never by a model. Band `unassessable` = too few
    rules could look at this claim AND none fired: that is "could not assess",
    not "low"."""
    fired = [r for r in results if r.status == "fired"]
    assessable = sum(1 for r in results if r.status in ("fired", "clear"))
    total_score = min(100, sum(r.weight for r in fired))
    if not fired and assessable < cfg.MIN_ASSESSABLE_SIGNALS:
        band = "unassessable"
    elif total_score >= cfg.BAND_HIGH:
        band = "high"
    elif total_score >= cfg.BAND_MEDIUM:
        band = "medium"
    else:
        band = "low"
    return total_score, band, assessable, len(results)


def run_rules(ctx: Context) -> Assessment:
    results = [rule(ctx) for rule in RULES]
    s, band, assessable, total = score(results)
    return Assessment(results, s, band, assessable, total)
