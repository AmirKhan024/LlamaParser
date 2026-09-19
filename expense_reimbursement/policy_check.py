"""Stage 3's deterministic checker: given ONE structured clause and the facts
of ONE evaluated unit, decide the verdict. No model, no database, no network.

This is the "code computes" half of the design. Every numeric comparison
(amount vs limit), every date calculation (nights, trip days), every per-unit
division (total / nights, total / attendees), every rate calculation (km x
rate, % of a bill) and every aggregation (a month's phone bills) happens here.
The model's job ended earlier: it chose the clause, judged the qualitative
conditions, and named which amount to compare and why -- those arrive here as
plain inputs (`UnitFacts`), and nothing in this module trusts an amount it
cannot re-derive.

Verdicts:
  compliant                  every check that applies passed, nothing unknown
  violation                  a limit is exceeded, a condition fails, a
                             prohibition is triggered, or required proof is absent
  needs_approval             within the rules, but the clause requires an
                             approval above a threshold and none is evidenced
  insufficient_information   something needed to decide is unknown; the
                             machine-readable `missing_fields` names what
Roll-up of several units: violation > needs_approval > insufficient_information > compliant.

When a dimension the limit depends on is unknown (which city tier? which
vehicle?), the checker does NOT guess: it evaluates every limit that could
apply and decides only if they all agree (e.g. Rs 300 is under even the
lowest possible cap, so the missing city doesn't matter).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional

import policy as pol

VERDICTS = ("compliant", "violation", "needs_approval", "insufficient_information")
_SEVERITY = {"compliant": 0, "insufficient_information": 1, "needs_approval": 2, "violation": 3}

CENT = Decimal("0.01")

# What a missing quantity is called in `missing_fields` (Stage 4+ turns these
# into questions for the employee, so they are stable machine names).
MISSING_NAME = {
    "nights": "stay_nights",
    "days": "trip_days",
    "amount": "document_amount",
    "unit_amount": "document_amount",
    "persons": "attendee_count",
    "distance_km": "distance_km",
    "grade_rank": "employee_grade",
    "days_since_expense": "bill_date",
}


def rollup(verdicts: Iterable[str]) -> str:
    """Worst outcome wins. An empty set (nothing was evaluable) is
    insufficient_information -- never a silent pass."""
    verdicts = list(verdicts)
    if not verdicts:
        return "insufficient_information"
    return max(verdicts, key=lambda v: _SEVERITY[v])


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------- dates

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
)


def parse_date(text: Any) -> Optional[date]:
    """Tolerant date parse. A numeric day/month pair is read DAY-first
    (dd/mm/yyyy -- the company is Indian); an all-numeric date that would be
    a different day under month-first is therefore read one way only. An
    unparseable string is None, never a guess."""
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"\s+", " ", text.strip())
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", cleaned)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ]", cleaned)
    if m:
        return parse_date(m.group(1))
    return None


def nights_between(check_in: Any, check_out: Any) -> Optional[int]:
    """Whole nights between two dates; None if either doesn't parse or the
    stay isn't at least one night."""
    a, b = parse_date(check_in), parse_date(check_out)
    if a is None or b is None:
        return None
    nights = (b - a).days
    return nights if nights >= 1 else None


def inclusive_days(start: Any, end: Any) -> Optional[int]:
    """Calendar days from start to end counting both ends (a Monday-Wednesday
    trip is 3 days); None if either doesn't parse or end < start."""
    a, b = parse_date(start), parse_date(end)
    if a is None or b is None:
        return None
    days = (b - a).days + 1
    return days if days >= 1 else None


def parse_count(value: Any) -> Optional[int]:
    """A whole number written in a document field ("4", "4 pax", 4). A
    non-integer, negative or non-numeric value is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, float):
        return int(value) if value >= 1 and value == int(value) else None
    if isinstance(value, str):
        m = re.match(r"^\s*(\d+)(?:\.0+)?\b", value)
        if m:
            n = int(m.group(1))
            return n if n >= 1 else None
    return None


# ------------------------------------------------------------------- types

@dataclass
class UnitFacts:
    """Everything the checker needs about one unit, already resolved by code
    (policy_eval.py) from the extraction, the employee row and the database.
    The `judgments`, `documentation` and `approval_evidenced` fields are the
    model's qualitative answers; everything numeric is code's."""

    currency: Optional[str]
    base_amount: Optional[Decimal]          # the amount before any per-unit division
    base_of_percent: Optional[Decimal] = None  # percent units: the bill the % is of
    nights: Optional[int] = None
    days: Optional[int] = None
    persons: Optional[int] = None
    distance_km: Optional[Decimal] = None
    # Days from the document date to the submission date (claims.submitted_at,
    # or the evaluation date for a draft) -- for submission-window rules.
    days_since_expense: Optional[int] = None
    grade: Optional[str] = None
    grade_rank: Optional[int] = None
    # dimension -> resolved value ("city_tier": "1", "zone": None = unknown)
    dims: dict[str, Optional[str]] = field(default_factory=dict)
    # Other amounts (or one 1 per occurrence for count-kind limits) falling in
    # the same calendar month for this employee/category; None = the
    # aggregation could not be gathered.
    period_amounts: Optional[list[Decimal]] = None
    judgments: dict[str, Optional[bool]] = field(default_factory=dict)
    documentation: dict[str, Optional[bool]] = field(default_factory=dict)
    approval_evidenced: Optional[bool] = None


@dataclass
class CheckOutcome:
    verdict: str
    limit_applied: Optional[Decimal] = None
    limit_unit: Optional[str] = None
    limit_currency: Optional[str] = None
    amount_compared: Optional[Decimal] = None
    comparison_result: Optional[str] = None   # within_limit | at_limit | over_limit | no_limit | not_evaluated
    missing_fields: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)   # human-readable, one per finding
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------- conditions

def _compare(op: str, left: Decimal, right: Decimal) -> bool:
    return {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right, "==": left == right}[op]


def _quantity(name: str, facts: UnitFacts, unit_amount: Optional[Decimal]) -> Optional[Decimal]:
    raw = {
        "nights": facts.nights, "days": facts.days, "amount": facts.base_amount, "unit_amount": unit_amount,
        "persons": facts.persons, "distance_km": facts.distance_km, "grade_rank": facts.grade_rank,
        "days_since_expense": facts.days_since_expense,
    }[name]
    return None if raw is None else Decimal(raw)


def evaluate_condition(cond: dict, facts: UnitFacts, unit_amount: Optional[Decimal]) -> Optional[bool]:
    """True / False / None (unknown). A numeric condition is computed here;
    a judgment condition is the model's recorded answer. `any_of` is
    three-valued OR: any True wins, all False is False, otherwise unknown."""
    if cond.get("any_of"):
        results = [evaluate_condition(sub, facts, unit_amount) for sub in cond["any_of"]]
        if any(r is True for r in results):
            return True
        return False if all(r is False for r in results) else None
    if cond.get("check") == "numeric" and cond.get("numeric"):
        n = cond["numeric"]
        actual = _quantity(n["quantity"], facts, unit_amount)
        if actual is None:
            return None
        return _compare(n["op"], actual, Decimal(str(n["value"])))
    return facts.judgments.get(cond["id"])


def _missing_for_condition(cond: dict, facts: UnitFacts, unit_amount: Optional[Decimal]) -> list[str]:
    """What is unknown about an unresolved condition. For an `any_of`, only
    the alternatives that are themselves unresolved -- a known-false
    alternative is not a missing field."""
    if cond.get("any_of"):
        out: list[str] = []
        for sub in cond["any_of"]:
            if evaluate_condition(sub, facts, unit_amount) is None:
                out.extend(_missing_for_condition(sub, facts, unit_amount))
        return out
    if cond.get("check") == "numeric" and cond.get("numeric"):
        return [MISSING_NAME[cond["numeric"]["quantity"]]]
    depends = cond.get("depends_on") or []
    unreliable = [d for d in depends if d in pol.FIELD_VOCAB and pol.FIELD_VOCAB[d][0] != "extracted"]
    return unreliable or depends or [f"condition:{cond['id']}"]


# ------------------------------------------------------------------- limits

def _dim_status(when: dict[str, tuple[str, ...]], dims: dict[str, Optional[str]]) -> tuple[bool, set[str]]:
    """(possible, unresolved): can an entry with this `when` apply given the
    known dims, and which of its dimension keys are still unknown."""
    unresolved: set[str] = set()
    for key, allowed in when.items():
        value = dims.get(key)
        if value is None:
            unresolved.add(key)
        elif str(value).strip().lower() not in {a.strip().lower() for a in allowed}:
            return False, set()
    return True, unresolved


def _per_unit_value(unit: str, facts: UnitFacts, limit_amount: Decimal, kind: str) -> tuple[Optional[Decimal], Optional[Decimal], list[str], str]:
    """(amount to compare, limit to compare against, missing, derivation).
    Returns amount None when a needed quantity is unavailable."""
    base = facts.base_amount
    if base is None:
        return None, None, ["document_amount"], ""
    if kind == "count":
        if facts.period_amounts is None:
            return None, None, ["monthly_history"], ""
        return Decimal(1 + len(facts.period_amounts)), limit_amount, [], f"{1 + len(facts.period_amounts)} occurrence(s) this month"
    if unit == "per_night":
        if not facts.nights:
            return None, None, ["stay_nights"], ""
        return money(base / facts.nights), limit_amount, [], f"{base} / {facts.nights} night(s)"
    if unit == "per_day":
        if not facts.days:
            return None, None, ["trip_days"], ""
        return money(base / facts.days), limit_amount, [], f"{base} / {facts.days} day(s)"
    if unit == "per_person":
        if not facts.persons:
            return None, None, ["attendee_count"], ""
        return money(base / facts.persons), limit_amount, [], f"{base} / {facts.persons} person(s)"
    if unit == "per_month":
        if facts.period_amounts is None:
            return None, None, ["monthly_history"], ""
        total = base + sum(facts.period_amounts, Decimal(0))
        return money(total), limit_amount, [], f"{base} + {len(facts.period_amounts)} other same-month amount(s) = {total}"
    if unit == "per_km":
        if facts.distance_km is None:
            return None, None, ["distance_km"], ""
        limit = money(limit_amount * facts.distance_km)
        return money(base), limit, [], f"claimed {base} vs {limit_amount}/km x {facts.distance_km} km = {limit}"
    if unit == "percent_of_amount":
        if facts.base_of_percent is None:
            return None, None, ["percent_base_amount"], ""
        limit = money(limit_amount / Decimal(100) * facts.base_of_percent)
        return money(base), limit, [], f"{limit_amount}% of {facts.base_of_percent} = {limit}"
    # per_trip, per_claim, per_ride, per_event, per_item: the amount itself.
    return money(base), limit_amount, [], f"{base}"


def _resolve_limit(clause: pol.Clause, facts: UnitFacts) -> dict[str, Any]:
    """Pick the limit entries that can apply, then compare against each.
    Returns a dict with candidates' outcomes; the caller reduces it."""
    result: dict[str, Any] = {"status": "ok", "missing": [], "reasons": []}
    entries = list(clause.limit_table)

    if clause.limit_kind == "amount":
        # A money limit is in a currency; without the document's currency (or
        # an FX rate, which Stage 1 doesn't extract) nothing can be compared.
        if facts.currency is None:
            result.update(status="unknown", missing=["currency"], reasons=["the document's currency is unknown"])
            return result
        in_currency = [e for e in entries if e.currency == facts.currency]
        if not in_currency:
            have = sorted({e.currency for e in entries if e.currency})
            result.update(
                status="unknown", missing=["fx_rate"],
                reasons=[f"the document is in {facts.currency} but {clause.clause_id} sets its limit in {', '.join(have)}; "
                         f"no exchange rate is available to convert"],
            )
            result["currency_mismatch"] = True
            return result
        entries = in_currency

    candidates: list[tuple[pol.LimitEntry, set[str]]] = []
    for e in entries:
        possible, unresolved = _dim_status(e.when, facts.dims)
        if possible:
            candidates.append((e, unresolved))
    if not candidates:
        result.update(status="unknown", missing=["applicable_limit"],
                      reasons=[f"no limit in {clause.clause_id} matches this employee/expense (dimensions {facts.dims})"])
        return result

    unresolved_all = sorted({k for _, u in candidates for k in u})
    outcomes = []
    for entry, unresolved in candidates:
        amount, limit, missing, derivation = _per_unit_value(clause.limit_unit, facts, entry.amount, clause.limit_kind)
        if amount is None:
            result.update(status="unknown", missing=missing, reasons=[f"cannot compute the amount per {clause.limit_unit}: missing {', '.join(missing)}"])
            return result
        within = amount <= limit if clause.limit_inclusive else amount < limit
        outcomes.append({"limit": limit, "amount": amount, "within": within, "derivation": derivation, "entry_amount": entry.amount, "unresolved": sorted(unresolved)})
    result["outcomes"] = outcomes

    if not unresolved_all:
        distinct = {o["limit"] for o in outcomes}
        if len(distinct) > 1:
            result.update(status="unknown", missing=["applicable_limit"],
                          reasons=[f"{clause.clause_id} has {len(distinct)} different limits that all match ({sorted(distinct)}); the policy text does not say which applies"])
            return result
        result["chosen"] = outcomes[0]
        return result

    # Some dimension is unknown: decide only if every possible limit agrees.
    if all(o["within"] for o in outcomes) or not any(o["within"] for o in outcomes):
        # Report the limit that produced the tightest/loosest decisive figure.
        chosen = min(outcomes, key=lambda o: o["limit"]) if outcomes[0]["within"] else max(outcomes, key=lambda o: o["limit"])
        result["chosen"] = chosen
        result["decided_despite_unknown"] = unresolved_all
        return result
    result.update(
        status="unknown", missing=unresolved_all,
        reasons=[f"the amount is within some possible limits ({sorted(o['limit'] for o in outcomes if o['within'])}) and over others "
                 f"({sorted(o['limit'] for o in outcomes if not o['within'])}); it depends on {', '.join(unresolved_all)}"],
    )
    return result


# ------------------------------------------------------------------- check

def check_unit(clause: pol.Clause, facts: UnitFacts) -> CheckOutcome:
    """The verdict for one unit under one clause. Pure function."""
    violations: list[str] = []
    approvals: list[str] = []
    missing: list[str] = []
    reasons: list[str] = []
    detail: dict[str, Any] = {"clause_id": clause.clause_id, "limit_unit": clause.limit_unit}

    outcome = CheckOutcome(verdict="compliant", limit_unit=clause.limit_unit, comparison_result="no_limit")
    unit_amount: Optional[Decimal] = None

    # --- the limit
    has_limit = bool(clause.limit_table) and clause.limit_unit != "none"
    if clause.limit_unit == "unknown" and clause.limit_table:
        missing.append("policy_limit_unit")
        reasons.append(f"{clause.clause_id} states a limit whose unit the policy text does not make clear; it cannot be applied")
        outcome.comparison_result = "not_evaluated"
    elif clause.limit_unit not in ("none", "unknown") and not clause.limit_table:
        # A clause that says it has a per-<unit> limit but carries no limit values
        # must never read as "no limit": that would be a silent pass.
        missing.append("policy_limit_missing")
        reasons.append(f"{clause.clause_id} is a {clause.limit_unit} clause but has no limit values structured")
        outcome.comparison_result = "not_evaluated"
    elif has_limit:
        resolved = _resolve_limit(clause, facts)
        detail["limit_resolution"] = {k: v for k, v in resolved.items() if k in ("status", "missing", "decided_despite_unknown", "currency_mismatch")}
        detail["limit_candidates"] = [
            {"limit": str(o["limit"]), "amount": str(o["amount"]), "within": o["within"], "derivation": o["derivation"], "unresolved": o["unresolved"]}
            for o in resolved.get("outcomes", [])
        ]
        if resolved["status"] == "unknown":
            missing.extend(resolved["missing"])
            reasons.extend(resolved["reasons"])
            outcome.comparison_result = "not_evaluated"
        else:
            chosen = resolved["chosen"]
            unit_amount = chosen["amount"]
            outcome.limit_applied = chosen["limit"]
            outcome.limit_currency = facts.currency if clause.limit_kind == "amount" else None
            outcome.amount_compared = chosen["amount"]
            detail["derivation"] = chosen["derivation"]
            if chosen["within"]:
                outcome.comparison_result = "within_limit" if chosen["amount"] < chosen["limit"] else "at_limit"
            else:
                outcome.comparison_result = "over_limit"
                over = chosen["amount"] - chosen["limit"]
                violations.append(f"{chosen['amount']} exceeds the {chosen['limit']} limit ({clause.limit_unit}) by {over}")

    # --- conditions
    condition_results: dict[str, Optional[bool]] = {}
    for cond in clause.conditions:
        value = evaluate_condition(cond, facts, unit_amount)
        condition_results[cond["id"]] = value
        if value is None:
            missing.extend(_missing_for_condition(cond, facts, unit_amount))
            reasons.append(f"cannot tell whether: {cond['text']}")
        elif cond["kind"] == "requires" and value is False:
            violations.append(f"required condition not met: {cond['text']}")
        elif cond["kind"] == "excludes" and value is True:
            violations.append(f"excluded circumstance applies: {cond['text']}")
    detail["conditions"] = condition_results
    if clause.is_prohibition and not clause.conditions:
        violations.append(f"{clause.clause_id} is an unconditional prohibition")

    # --- documentation
    doc_results: dict[str, Optional[bool]] = {}
    for item in clause.documentation_required:
        threshold = item.get("min_amount")
        if isinstance(threshold, dict):          # {"INR": "500", "USD": "10"}
            if facts.currency is None:
                missing.append("currency")
                continue
            if facts.currency not in threshold:
                missing.append("fx_rate")
                continue
            threshold = threshold[facts.currency]
        if threshold is not None:
            if facts.base_amount is None:
                missing.append("document_amount")
                continue
            if facts.base_amount < Decimal(str(threshold)):
                doc_results[item["item"]] = None  # not required at this amount
                continue
        present = facts.documentation.get(item["item"])
        doc_results[item["item"]] = present
        if present is False:
            violations.append(f"required proof missing: {item['item']}")
        elif present is None:
            missing.append(f"documentation:{item['item']}")
            reasons.append(f"cannot tell whether {item['item']} is provided")
    detail["documentation"] = doc_results

    # --- approval (compares the pre-division amount: the event/purchase total)
    if clause.requires_approval_above:
        thresholds = clause.requires_approval_above
        if facts.currency is None:
            missing.append("currency")
        elif facts.currency not in thresholds:
            missing.append("fx_rate")
            reasons.append(f"{clause.clause_id}'s approval threshold is in {', '.join(sorted(thresholds))}, the document is in {facts.currency}")
        elif facts.base_amount is None:
            missing.append("document_amount")
        else:
            threshold = thresholds[facts.currency]
            detail["approval_threshold"] = str(threshold)
            needed = facts.base_amount > threshold if threshold > 0 else facts.base_amount > 0
            detail["approval_needed"] = needed
            if needed and facts.approval_evidenced is not True:
                approvals.append(
                    f"{facts.base_amount} exceeds the {threshold} approval threshold and no approval is evidenced"
                    if threshold > 0 else f"{clause.clause_id} requires approval and none is evidenced"
                )

    missing = list(dict.fromkeys(missing))
    outcome.missing_fields = missing
    reasons = violations + approvals + reasons
    outcome.reasons = reasons
    outcome.detail = detail
    if violations:
        outcome.verdict = "violation"
    elif approvals:
        outcome.verdict = "needs_approval"
    elif missing:
        outcome.verdict = "insufficient_information"
    else:
        outcome.verdict = "compliant"
    if outcome.verdict != "insufficient_information" and outcome.comparison_result == "not_evaluated":
        # a limit that could not be evaluated is still an unknown even when
        # another finding decided the verdict
        detail["limit_not_evaluated"] = True
    return outcome


# --------------------------------------------------------------- formatting

def _group_inr(integer_part: str) -> str:
    if len(integer_part) <= 3:
        return integer_part
    head, tail = integer_part[:-3], integer_part[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"{head},{tail}"


_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def format_money(amount: Optional[Decimal], currency: Optional[str]) -> str:
    """₹1,450 / ₹1,00,000 / $1,450.50 -- whole amounts without decimals."""
    if amount is None:
        return "-"
    amount = money(amount)
    whole = amount == amount.to_integral_value()
    text = f"{amount:.0f}" if whole else f"{amount:.2f}"
    integer, _, frac = text.partition(".")
    grouped = _group_inr(integer) if currency == "INR" else f"{int(integer):,}"
    symbol = _SYMBOLS.get(currency or "", "")
    prefix = symbol if symbol else (f"{currency} " if currency else "")
    return f"{prefix}{grouped}{'.' + frac if frac else ''}"


_UNIT_LABEL = {
    "per_trip": "per trip", "per_night": "per night", "per_day": "per day", "per_month": "per month",
    "per_person": "per person", "per_claim": "per claim", "per_ride": "per ride", "per_item": "per item",
    # For per_km / percent_of_amount the "limit" shown is the COMPUTED total (rate x
    # distance, % x bill), not a rate -- label it as such.
    "per_km": "km rate x distance", "per_event": "per event", "percent_of_amount": "% of bill amount",
}


def format_comparison(amount: Optional[Decimal], limit: Optional[Decimal], unit: Optional[str], currency: Optional[str]) -> Optional[str]:
    """"₹1,450 vs ₹1,200 limit (per trip)" -- None when there was no comparison."""
    if amount is None or limit is None:
        return None
    label = _UNIT_LABEL.get(unit or "", unit or "")
    return f"{format_money(amount, currency)} vs {format_money(limit, currency)} limit ({label})"
