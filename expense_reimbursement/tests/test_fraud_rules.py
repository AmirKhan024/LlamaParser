"""Stage 4 deterministic rules, scoring and narrative grounding -- pure, no DB, no model.
Each rule: fires / does not fire (clear) / not_applicable when its data is missing."""

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fraud_config as cfg
import fraud_narrative as narr
import fraud_rules as fr
from fraud_rules import Context, CorrectionInfo, DecisionInfo, DocInfo, Threshold

D = Decimal


def doc(i, *, emp="e1", claim="c1", vendor="Toit Brewpub", when=date(2026, 7, 8), amount="3240", cur="INR", cat="client_entertainment"):
    return DocInfo(f"d{i}", claim, emp, f"Emp {emp}", f"r{i}.png", vendor, when, D(amount) if amount is not None else None, cur, cat)


def ctx(docs, universe=None, **kw):
    return Context(claim_id="c1", employee_id="e1", docs=docs, universe=universe if universe is not None else docs, **kw)


def status(rule, c):
    r = rule(c)
    return r.status, r


# ------------------------------------------------------------------ duplicates

def test_duplicate_same_employee_fires_clear_and_not_applicable():
    a, b = doc(1), doc(2, claim="c2", when=date(2026, 7, 11))
    s, r = status(fr.rule_duplicate_same_employee, ctx([a], [a, b]))
    assert s == "fired" and r.evidence["matches"][0]["days_apart"] == 3
    assert r.evidence["matches"][0]["matches"]["document_id"] == "d2"
    far = doc(3, claim="c3", when=date(2026, 9, 1))
    assert status(fr.rule_duplicate_same_employee, ctx([a], [a, far]))[0] == "clear"          # outside the window
    assert status(fr.rule_duplicate_same_employee, ctx([a], [a, doc(4, amount="3241")]))[0] == "clear"   # different amount
    assert status(fr.rule_duplicate_same_employee, ctx([doc(1, vendor=None)], []))[0] == "not_applicable"
    assert status(fr.rule_duplicate_same_employee, ctx([doc(1, when=None)], []))[0] == "not_applicable"


def test_vendor_names_are_normalised_but_amounts_must_match_exactly():
    a, b = doc(1, vendor="Toit Brewpub Pvt. Ltd."), doc(2, claim="c2", vendor="TOIT BREWPUB", when=date(2026, 7, 9))
    assert status(fr.rule_duplicate_same_employee, ctx([a], [a, b]))[0] == "fired"


def test_duplicate_cross_employee_only_matches_a_different_employee():
    a = doc(1)
    other = doc(2, emp="e2", claim="c9", when=date(2026, 7, 10))
    s, r = status(fr.rule_duplicate_cross_employee, ctx([a], [a, other]))
    assert s == "fired" and r.evidence["matches"][0]["matches"]["employee"] == "Emp e2"
    same_emp = doc(3, emp="e1", claim="c9", when=date(2026, 7, 10))
    assert status(fr.rule_duplicate_cross_employee, ctx([a], [a, same_emp]))[0] == "clear"
    assert status(fr.rule_duplicate_cross_employee, ctx([doc(1, amount=None)], []))[0] == "not_applicable"


# ------------------------------------------------------------ threshold gaming

def test_threshold_gaming_reads_thresholds_from_the_context_not_from_code():
    t = {"client_entertainment": [Threshold("10.4", "approval", "INR", D("15000"))]}
    s, r = status(fr.rule_threshold_gaming, ctx([doc(1, amount="14850")], thresholds=t))
    assert s == "fired" and r.evidence["hits"][0]["clause_id"] == "10.4" and r.evidence["hits"][0]["percent_below"] == "1.0"
    assert status(fr.rule_threshold_gaming, ctx([doc(1, amount="15000")], thresholds=t))[0] == "clear"    # at, not below
    assert status(fr.rule_threshold_gaming, ctx([doc(1, amount="13000")], thresholds=t))[0] == "clear"    # more than 5% below
    assert status(fr.rule_threshold_gaming, ctx([doc(1, amount="14850")], thresholds={}))[0] == "clear"   # no threshold to game
    # a different threshold in the same context changes the outcome (nothing hardcoded)
    t2 = {"client_entertainment": [Threshold("x", "approval", "INR", D("20000"))]}
    assert status(fr.rule_threshold_gaming, ctx([doc(1, amount="14850")], thresholds=t2))[0] == "clear"
    assert status(fr.rule_threshold_gaming, ctx([doc(1, amount="14850", cat=None)], thresholds=t))[0] == "not_applicable"
    assert status(fr.rule_threshold_gaming, ctx([doc(1, amount="14850", cur="USD")], thresholds=t))[0] == "clear"


def test_universal_thresholds_apply_to_every_category():
    t = {"*": [Threshold("20.1", "documentation", "INR", D("500"))]}
    assert status(fr.rule_threshold_gaming, ctx([doc(1, amount="495", cat="local_transport")], thresholds=t))[0] == "fired"


# -------------------------------------------------------------------- velocity

def test_velocity_by_count_by_total_and_not_applicable_without_dates():
    week = [doc(i, claim="c1", when=date(2026, 8, 10 + (i % 5)), amount="300", vendor=f"v{i}") for i in range(1, 8)]   # 7 docs, Mon-Fri
    s, r = status(fr.rule_velocity, ctx(week[:1], week))
    assert s == "fired" and r.evidence["weeks"][0]["document_count"] == 7
    assert status(fr.rule_velocity, ctx(week[:1], week[:4]))[0] == "clear"
    big = [doc(1, amount="14000", when=date(2026, 8, 10)), doc(2, amount="12000", when=date(2026, 8, 11), vendor="b")]
    s, r = status(fr.rule_velocity, ctx(big[:1], big))
    assert s == "fired" and r.evidence["weeks"][0]["totals_over_limit"] == {"INR": "26000"}
    single = [doc(1, amount="38000", when=date(2026, 8, 10))]
    assert status(fr.rule_velocity, ctx(single, single))[0] == "clear"          # one big invoice is not a burst
    assert status(fr.rule_velocity, ctx([doc(1, when=None)], []))[0] == "not_applicable"
    other_emp = [doc(i, emp="e2", when=date(2026, 8, 10), vendor=f"x{i}") for i in range(10, 20)]
    assert status(fr.rule_velocity, ctx(week[:1], week[:1] + other_emp))[0] == "clear"     # other people's docs don't count


# ---------------------------------------------------------------- round number

def test_round_number():
    assert status(fr.rule_round_number, ctx([doc(1, amount="5000")]))[0] == "fired"
    assert status(fr.rule_round_number, ctx([doc(1, amount="5001")]))[0] == "clear"
    assert status(fr.rule_round_number, ctx([doc(1, amount="500")]))[0] == "clear"              # below the minimum
    assert status(fr.rule_round_number, ctx([doc(1, amount=None)]))[0] == "not_applicable"
    assert status(fr.rule_round_number, ctx([doc(1, amount="5000", cur="USD")]))[0] == "not_applicable"   # no step configured


# --------------------------------------------------------------------- weekend

def test_weekend_business():
    sat, mon = date(2026, 7, 4), date(2026, 7, 6)
    s, r = status(fr.rule_weekend_business, ctx([doc(1, when=sat)]))
    assert s == "fired" and r.evidence["documents"][0]["weekday"] == "Saturday"
    assert status(fr.rule_weekend_business, ctx([doc(1, when=mon)]))[0] == "clear"
    assert status(fr.rule_weekend_business, ctx([doc(1, when=sat, cat="local_transport")]))[0] == "clear"   # out of scope
    assert status(fr.rule_weekend_business, ctx([doc(1, when=None)]))[0] == "not_applicable"


# ------------------------------------------------------------ repeat violation

def dec(claim, doc_id, verdict, clause="12.1", unit=0):
    return DecisionInfo(claim, doc_id, unit, clause, verdict)


def test_policy_repeat_violation_needs_known_verdicts_and_history():
    mine = [dec("c1", "d1", "violation")]
    hist = mine + [dec("c0", "d0", "violation")]
    s, r = status(fr.rule_policy_repeat_violation, ctx([doc(1)], decisions_this_claim=mine, decisions_employee=hist))
    assert s == "fired" and r.evidence["clauses"][0] == {"clause_id": "12.1", "violations": 2, "claims": ["c0", "c1"], "documents": ["d0", "d1"]}
    assert status(fr.rule_policy_repeat_violation, ctx([doc(1)], decisions_this_claim=mine, decisions_employee=mine))[0] == "clear"
    other_clause = hist[:1] + [dec("c0", "d0", "violation", clause="8.1")]
    assert status(fr.rule_policy_repeat_violation, ctx([doc(1)], decisions_this_claim=mine, decisions_employee=other_clause))[0] == "clear"


def test_policy_repeat_violation_is_not_applicable_on_absent_signal():
    assert status(fr.rule_policy_repeat_violation, ctx([doc(1)]))[0] == "not_applicable"       # never evaluated
    unknown = [dec("c1", "d1", "insufficient_information")]
    s, r = status(fr.rule_policy_repeat_violation, ctx([doc(1)], decisions_this_claim=unknown, decisions_employee=unknown))
    assert s == "not_applicable" and "insufficient_information" in r.reason


# ----------------------------------------------------------------- corrections

def corr(field="amount", direction="increase", reason=None):
    return CorrectionInfo("d1", field, direction, reason, "420.00", "640.00")


def test_correction_upward_and_guardrail():
    base = dict(docs_with_ai_extraction={"d1"})
    s, r = status(fr.rule_correction_upward, ctx([doc(1)], corrections=[corr()], **base))
    assert s == "fired" and r.evidence["corrections"][0]["employee_value"] == "640.00"
    assert status(fr.rule_correction_upward, ctx([doc(1)], corrections=[corr(direction="decrease")], **base))[0] == "clear"
    assert status(fr.rule_correction_upward, ctx([doc(1)], corrections=[corr(field="vendor_name")], **base))[0] == "clear"   # not money
    assert status(fr.rule_correction_upward, ctx([doc(1)], corrections=[], **base))[0] == "clear"
    assert status(fr.rule_correction_upward, ctx([doc(1)]))[0] == "not_applicable"                                      # nothing to compare to

    assert status(fr.rule_correction_guardrail, ctx([doc(1)], corrections=[corr(reason="toll")], **base))[0] == "fired"
    assert status(fr.rule_correction_guardrail, ctx([doc(1)], corrections=[corr()], **base))[0] == "clear"
    assert status(fr.rule_correction_guardrail, ctx([doc(1)]))[0] == "not_applicable"


# --------------------------------------------------------------------- scoring

def test_score_arithmetic_bands_and_unassessable():
    def r(rid, st):
        return fr.RuleResult(rid, "d", cfg.WEIGHTS[rid], st, "why")
    results = [r(rid, "clear") for rid in cfg.WEIGHTS]
    assert fr.score(results) == (0, "low", 9, 9)
    results[0], results[7] = r("duplicate_same_employee", "fired"), r("correction_upward", "fired")
    score, band, assessable, total = fr.score(results)
    assert score == cfg.WEIGHTS["duplicate_same_employee"] + cfg.WEIGHTS["correction_upward"] == 50 and band == "high"
    one = [r(rid, "clear") for rid in cfg.WEIGHTS]
    one[4] = r("round_number", "fired")
    assert fr.score(one)[:2] == (8, "low")
    medium = [r(rid, "clear") for rid in cfg.WEIGHTS]
    medium[2], medium[3] = r("threshold_gaming", "fired"), r("velocity", "fired")      # 20 + 15 = 35
    assert fr.score(medium)[:2] == (35, "medium")
    # capped at 100
    every = [r(rid, "fired") for rid in cfg.WEIGHTS]
    assert fr.score(every)[0] == 100
    # too little to assess and nothing fired: "unassessable", NOT "low"
    blind = [r(rid, "not_applicable") for rid in cfg.WEIGHTS]
    blind[0] = r("duplicate_same_employee", "clear")
    assert fr.score(blind) == (0, "unassessable", 1, 9)
    # ...but a fired rule is reported even with thin coverage
    blind[1] = r("duplicate_cross_employee", "fired")
    assert fr.score(blind)[1] == "medium" and fr.score(blind)[2] == 2   # 25 points; coverage 2 of 9 is still reported


def test_run_rules_reports_coverage_and_never_guesses_across_a_gap():
    c = ctx([doc(1, vendor=None, when=None, amount=None, cat=None)])
    a = fr.run_rules(c)
    statuses = {r.rule_id: r.status for r in a.results}
    assert statuses["duplicate_same_employee"] == statuses["velocity"] == statuses["round_number"] == "not_applicable"
    assert statuses["policy_repeat_violation"] == "not_applicable" and a.risk_band == "unassessable" and a.risk_score == 0
    assert a.total == 9 and a.assessable < cfg.MIN_ASSESSABLE_SIGNALS


# ------------------------------------------------------------------- narrative

def fired_results():
    a, b = doc(1), doc(2, claim="c2", when=date(2026, 7, 11))
    dup = fr.rule_duplicate_same_employee(ctx([a], [a, b]))
    weekend = fr.rule_weekend_business(ctx([doc(1, when=date(2026, 7, 4))]))
    clear = fr.rule_round_number(ctx([doc(1, amount="3241")]))
    return [dup, weekend, clear]


def narrative(summary, refs):
    return json.dumps({"summary": summary, "rules_referenced": refs})


def test_grounded_narrative_is_accepted():
    text, why = narr.ground(narrative("Toit Brewpub receipts of 3240 dated 2026-07-08 and 2026-07-11, 3 days apart, on this claim and another.",
                                       ["duplicate_same_employee"]), fired_results())
    assert why is None and "Toit Brewpub" in text


def test_narrative_referencing_an_unfired_rule_is_rejected():
    results = fired_results()
    assert narr.ground(narrative("Same vendor twice.", ["duplicate_same_employee", "round_number"]), results)[0] is None
    text, why = narr.ground(narrative("Same vendor twice, and the amount is a round number.", ["duplicate_same_employee"]), results)
    assert text is None and "round_number" in why
    text, why = narr.ground(narrative("Also flagged by the velocity rule.", ["duplicate_same_employee"]), results)
    assert text is None and "velocity" in why
    text, why = narr.ground(narrative("Amount is just under the approval threshold.", ["duplicate_same_employee"]), results)
    assert text is None and "threshold_gaming" in why


def test_narrative_with_an_invented_number_is_rejected():
    text, why = narr.ground(narrative("Toit Brewpub billed 9999 twice.", ["duplicate_same_employee"]), fired_results())
    assert text is None and "9999" in why
    assert narr.ground(narrative("A total of 6480 was claimed.", ["duplicate_same_employee"]), fired_results())[0] is None   # a sum the model made up


def test_narrative_shape_errors_are_rejected():
    results = fired_results()
    assert narr.ground("not json", results)[0] is None
    assert narr.ground(json.dumps({"summary": "", "rules_referenced": ["x"]}), results)[0] is None
    assert narr.ground(json.dumps({"summary": "ok", "rules_referenced": []}), results)[0] is None
    assert narr.ground(json.dumps({"summary": "ok", "rules_referenced": ["velocity"]}), results)[0] is None


def test_narrative_prompt_is_a_versioned_file():
    text, sha = narr.load_prompt("v1")
    assert "You explain; you do not detect" in text and len(sha) == 64
