"""The deterministic checker (policy_check.py) in isolation: no model, no
database. Every numeric comparison, division, rate, aggregation and date
calculation of Stage 3 is tested here, so that when a verdict is wrong later
the question "which half is at fault -- the model's selection or the
arithmetic?" has an answer: this file passing rules out the arithmetic.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import policy as pol
import policy_check as pc
from policy import Clause, LimitEntry
from policy_check import UnitFacts, check_unit

D = Decimal


def clause(unit="per_trip", limit=1200, currency="INR", *, table=None, kind="amount", inclusive=True,
           conditions=(), documentation=(), approval=None, prohibition=False, cid="X.1") -> Clause:
    if table is None:
        table = [LimitEntry(D(str(limit)), currency)] if limit is not None else []
    return Clause(
        clause_id=cid, section=cid.split(".")[0], sort_order=0, verbatim_text="text",
        applies_to_categories=("other",), limit_unit=unit, limit_kind=kind, limit_inclusive=inclusive,
        limit_table=tuple(table), conditions=tuple(conditions), documentation_required=tuple(documentation),
        requires_approval_above=approval, is_prohibition=prohibition,
    )


def facts(amount, **kw) -> UnitFacts:
    kw.setdefault("currency", "INR")
    return UnitFacts(base_amount=D(str(amount)) if amount is not None else None, **kw)


# ------------------------------------------------------- over / under / at

@pytest.mark.parametrize("amount, verdict, result", [
    ("1199.99", "compliant", "within_limit"),
    ("1200", "compliant", "at_limit"),       # a cap is a ceiling: exactly at it is allowed
    ("1200.01", "violation", "over_limit"),
    ("1450", "violation", "over_limit"),
])
def test_per_trip_boundaries(amount, verdict, result):
    out = check_unit(clause("per_trip", 1200), facts(amount))
    assert (out.verdict, out.comparison_result) == (verdict, result)
    assert out.limit_applied == D("1200") and out.amount_compared == D(amount).quantize(D("0.01"))


def test_exclusive_limit_at_the_limit_is_over():
    # "under Rs 2,000 per item": Rs 2,000 itself is not allowed.
    c = clause("per_item", 2000, inclusive=False)
    assert check_unit(c, facts("1999.99")).verdict == "compliant"
    assert check_unit(c, facts("2000")).verdict == "violation"


def test_missing_amount_is_insufficient_not_compliant():
    out = check_unit(clause("per_trip", 1200), facts(None))
    assert out.verdict == "insufficient_information"
    assert out.missing_fields == ["document_amount"]


# ----------------------------------------------------------------- each unit

def test_per_night_divides_total_by_nights():
    c = clause("per_night", 6000)
    assert check_unit(c, facts(17_999, nights=3)).verdict == "compliant"      # 5,999.67 / night
    over = check_unit(c, facts(18_003, nights=3))                            # 6,001 / night
    assert over.verdict == "violation" and over.amount_compared == D("6001.00")


def test_multi_night_hotel_within_cap_only_because_of_division():
    # 3 nights at Rs 5,500 = Rs 16,500 total: over a Rs 6,000 cap if (wrongly) compared undivided.
    assert check_unit(clause("per_night", 6000), facts(16_500, nights=3)).verdict == "compliant"
    assert check_unit(clause("per_trip", 6000), facts(16_500)).verdict == "violation"


def test_per_night_without_nights_names_the_missing_field():
    out = check_unit(clause("per_night", 6000), facts(16_500))
    assert out.verdict == "insufficient_information" and out.missing_fields == ["stay_nights"]


def test_per_day_and_per_person():
    assert check_unit(clause("per_day", 1200), facts(3_600, days=3)).verdict == "compliant"
    assert check_unit(clause("per_day", 1200), facts(3_700, days=3)).verdict == "violation"
    assert check_unit(clause("per_person", 2500), facts(10_000, persons=4)).verdict == "compliant"
    out = check_unit(clause("per_person", 2500), facts(10_100, persons=4))
    assert out.verdict == "violation" and out.amount_compared == D("2525.00")
    unknown = check_unit(clause("per_person", 2500), facts(10_000))
    assert unknown.verdict == "insufficient_information" and unknown.missing_fields == ["attendee_count"]


def test_per_claim_per_ride_per_event_use_the_amount_itself():
    for unit in ("per_claim", "per_ride", "per_event", "per_item"):
        assert check_unit(clause(unit, 800), facts(800)).verdict == "compliant"
        assert check_unit(clause(unit, 800), facts(801)).verdict == "violation"


def test_unit_none_with_no_limit_and_no_findings_is_compliant():
    out = check_unit(clause("none", None), facts(50_000))
    assert out.verdict == "compliant" and out.comparison_result == "no_limit"


def test_unknown_unit_is_never_guessed():
    out = check_unit(clause("unknown", 500), facts(100))
    assert out.verdict == "insufficient_information" and "policy_limit_unit" in out.missing_fields


# ------------------------------------------------------------ mileage / %

def test_mileage_rate_times_distance():
    c = clause("per_km", 4)
    assert check_unit(c, facts(1_200, distance_km=D("300"))).verdict == "compliant"        # exactly 300 x 4
    out = check_unit(c, facts(1_201, distance_km=D("300")))
    assert out.verdict == "violation" and out.limit_applied == D("1200.00")
    assert check_unit(c, facts(1_200)).missing_fields == ["distance_km"]


def test_mileage_with_unknown_vehicle_decides_only_when_both_rates_agree():
    c = clause("per_km", table=[
        LimitEntry(D("4"), "INR", {"vehicle_type": ("two_wheeler",)}),
        LimitEntry(D("9"), "INR", {"vehicle_type": ("four_wheeler",)}),
    ])
    f = lambda amt: facts(amt, distance_km=D("100"), dims={"vehicle_type": None})
    assert check_unit(c, f(300)).verdict == "compliant"        # within even the lowest rate (400)
    assert check_unit(c, f(1_000)).verdict == "violation"      # over even the highest rate (900)
    mid = check_unit(c, f(600))
    assert mid.verdict == "insufficient_information" and mid.missing_fields == ["vehicle_type"]
    assert check_unit(c, facts(600, distance_km=D("100"), dims={"vehicle_type": "four_wheeler"})).verdict == "compliant"
    assert check_unit(c, facts(600, distance_km=D("100"), dims={"vehicle_type": "two_wheeler"})).verdict == "violation"


def test_percent_of_amount():
    c = clause("percent_of_amount", 30, currency=None, kind="percent")
    ok = check_unit(c, facts(3_000, base_of_percent=D("10000")))                 # alcohol 3,000 of a 10,000 bill
    assert ok.verdict == "compliant" and ok.limit_applied == D("3000.00")
    assert check_unit(c, facts(3_001, base_of_percent=D("10000"))).verdict == "violation"
    assert check_unit(c, facts(3_000)).missing_fields == ["percent_base_amount"]


# --------------------------------------------------------------- aggregation

def test_monthly_aggregation_across_claims():
    c = clause("per_month", 1500)
    # this bill 600 + two others in the month (500 + 400) = 1,500 -> at the cap
    at = check_unit(c, facts(600, period_amounts=[D("500"), D("400")]))
    assert at.verdict == "compliant" and at.amount_compared == D("1500.00") and at.comparison_result == "at_limit"
    over = check_unit(c, facts(600, period_amounts=[D("500"), D("401")]))
    assert over.verdict == "violation" and over.amount_compared == D("1501.00")
    # ...but alone it would have passed: the aggregation is what makes it a violation.
    assert check_unit(c, facts(600, period_amounts=[])).verdict == "compliant"
    none = check_unit(c, facts(600))
    assert none.verdict == "insufficient_information" and none.missing_fields == ["monthly_history"]


def test_count_kind_limit_counts_occurrences():
    c = clause("per_month", 2, currency=None, kind="count")   # max 2 team events a month
    assert check_unit(c, facts(4_000, period_amounts=[D("1")])).verdict == "compliant"       # 2nd event
    out = check_unit(c, facts(4_000, period_amounts=[D("1"), D("1")]))                        # 3rd event
    assert out.verdict == "violation" and out.amount_compared == D("3.00")


# ---------------------------------------------------------- limit tables/dims

GRADE_TIER = [
    LimitEntry(D("4500"), "INR", {"grade": ("L2",), "city_tier": ("1",)}),
    LimitEntry(D("3500"), "INR", {"grade": ("L2",), "city_tier": ("2",)}),
    LimitEntry(D("8000"), "INR", {"grade": ("L4",), "city_tier": ("1",)}),
    LimitEntry(D("6000"), "INR", {"grade": ("L4",), "city_tier": ("2",)}),
    LimitEntry(D("120"), "USD", {"grade": ("L4",), "zone": ("A",)}),
]


def test_limit_table_lookup_by_grade_and_tier():
    c = clause("per_night", table=GRADE_TIER)
    l4_tier1 = facts(8_000, nights=1, grade="L4", dims={"grade": "L4", "city_tier": "1"})
    assert check_unit(c, l4_tier1).verdict == "compliant"
    l4_tier2 = facts(8_000, nights=1, grade="L4", dims={"grade": "L4", "city_tier": "2"})
    out = check_unit(c, l4_tier2)
    assert out.verdict == "violation" and out.limit_applied == D("6000")


def test_unknown_city_tier_is_decided_only_when_every_tier_agrees():
    c = clause("per_night", table=GRADE_TIER)
    dims = {"grade": "L4", "city_tier": None}
    assert check_unit(c, facts(5_000, nights=1, dims=dims)).verdict == "compliant"      # under Tier 2's 6,000 too
    assert check_unit(c, facts(9_000, nights=1, dims=dims)).verdict == "violation"      # over Tier 1's 8,000 too
    between = check_unit(c, facts(7_000, nights=1, dims=dims))
    assert between.verdict == "insufficient_information" and between.missing_fields == ["city_tier"]
    assert between.limit_applied is None


def test_no_matching_limit_is_insufficient_never_a_pass():
    c = clause("per_night", table=GRADE_TIER)
    out = check_unit(c, facts(100, nights=1, dims={"grade": "L6", "city_tier": "1"}))
    assert out.verdict == "insufficient_information" and out.missing_fields == ["applicable_limit"]


def test_qualifier_dimension_selects_between_limits():
    c = clause("per_person", table=[
        LimitEntry(D("800"), "INR", {"occasion": ("routine",)}),
        LimitEntry(D("1500"), "INR", {"occasion": ("celebration",)}),
    ])
    assert check_unit(c, facts(6_000, persons=5, dims={"occasion": "celebration"})).verdict == "compliant"
    assert check_unit(c, facts(6_000, persons=5, dims={"occasion": "routine"})).verdict == "violation"


# -------------------------------------------------------------------- currency

def test_currency_mismatch_is_insufficient_and_asks_for_an_fx_rate():
    c = clause("per_night", table=GRADE_TIER)   # has INR and USD entries
    out = check_unit(c, facts(500, currency="EUR", nights=1, dims={"grade": "L4", "city_tier": "1", "zone": "A"}))
    assert out.verdict == "insufficient_information" and out.missing_fields == ["fx_rate"]
    inr_only = clause("per_month", 3000)
    out = check_unit(inr_only, facts(50, currency="USD", period_amounts=[]))
    assert out.verdict == "insufficient_information" and out.missing_fields == ["fx_rate"]


def test_usd_document_uses_the_usd_limit():
    c = clause("per_night", table=GRADE_TIER)
    dims = {"grade": "L4", "zone": "A"}
    assert check_unit(c, facts(240, currency="USD", nights=2, dims=dims)).verdict == "compliant"   # 120 / night
    assert check_unit(c, facts(242, currency="USD", nights=2, dims=dims)).verdict == "violation"


def test_unknown_currency_is_insufficient():
    out = check_unit(clause("per_trip", 1200), facts(100, currency=None))
    assert out.verdict == "insufficient_information" and out.missing_fields == ["currency"]


# ------------------------------------------------------------------ conditions

def cond(id, kind="requires", text="t", **kw):
    return {"id": id, "text": text, "kind": kind, "check": kw.pop("check", "judgment"),
            "depends_on": kw.pop("depends_on", []), "numeric": kw.pop("numeric", None), **kw}


def test_requires_condition_false_is_a_violation_and_unknown_is_insufficient():
    c = clause("none", None, conditions=[cond("c1", text="a client is named", depends_on=["client_name"])])
    assert check_unit(c, facts(100, judgments={"c1": True})).verdict == "compliant"
    assert check_unit(c, facts(100, judgments={"c1": False})).verdict == "violation"
    unknown = check_unit(c, facts(100, judgments={"c1": None}))
    assert unknown.verdict == "insufficient_information" and unknown.missing_fields == ["client_name"]
    # an unanswered condition is unknown, never assumed true
    assert check_unit(c, facts(100)).verdict == "insufficient_information"


def test_excludes_condition_true_is_a_violation():
    c = clause("none", None, conditions=[cond("c1", "excludes", "alcohol is on the bill")])
    assert check_unit(c, facts(100, judgments={"c1": True})).verdict == "violation"
    assert check_unit(c, facts(100, judgments={"c1": False})).verdict == "compliant"


def test_numeric_condition_is_computed_by_code_not_the_model():
    # Laundry: only on trips longer than 5 nights.
    laundry = cond("c1", text="trip longer than 5 nights", check="numeric", numeric={"quantity": "nights", "op": ">", "value": "5"})
    c = clause("per_day", 300, conditions=[laundry])
    ok = check_unit(c, facts(200, days=1, nights=6))
    assert ok.verdict == "compliant"
    exactly = check_unit(c, facts(200, days=1, nights=5))                # 5 nights is not "longer than 5"
    assert exactly.verdict == "violation"
    # the model's judgment for a numeric condition is ignored entirely
    assert check_unit(c, facts(200, days=1, nights=5, judgments={"c1": True})).verdict == "violation"
    unknown = check_unit(c, facts(200, days=1))
    assert unknown.verdict == "insufficient_information" and unknown.missing_fields == ["stay_nights"]


def test_grade_rank_condition_and_any_of():
    l5_or_leased = {
        "id": "c1", "text": "company-leased vehicle or L5+", "kind": "requires", "check": "judgment", "depends_on": [],
        "any_of": [
            {"id": "c1a", "text": "leased", "check": "judgment", "depends_on": ["vehicle_ownership"]},
            {"id": "c1b", "text": "L5 or above", "check": "numeric", "numeric": {"quantity": "grade_rank", "op": ">=", "value": "5"}},
        ],
    }
    c = clause("none", None, conditions=[l5_or_leased])
    assert check_unit(c, facts(100, grade_rank=5)).verdict == "compliant"                       # rank alone satisfies it
    assert check_unit(c, facts(100, grade_rank=4, judgments={"c1a": True})).verdict == "compliant"
    assert check_unit(c, facts(100, grade_rank=4, judgments={"c1a": False})).verdict == "violation"
    unknown = check_unit(c, facts(100, grade_rank=4))
    assert unknown.verdict == "insufficient_information" and unknown.missing_fields == ["vehicle_ownership"]


# ------------------------------------------------------------- prohibitions

def test_unconditional_prohibition_is_a_violation():
    assert check_unit(clause("none", None, prohibition=True), facts(100)).verdict == "violation"


def test_prohibition_fires_only_when_its_trigger_is_true():
    c = clause("none", None, prohibition=True, conditions=[cond("t", "excludes", "the expense is a traffic fine")])
    assert check_unit(c, facts(500, judgments={"t": True})).verdict == "violation"
    assert check_unit(c, facts(500, judgments={"t": False})).verdict == "compliant"
    assert check_unit(c, facts(500)).verdict == "insufficient_information"


def test_a_known_violation_outranks_an_unknown_elsewhere_in_the_unit():
    c = clause("per_trip", 100, conditions=[cond("c1", depends_on=["client_name"])])
    out = check_unit(c, facts(500, judgments={"c1": None}))
    assert out.verdict == "violation"                 # over the limit regardless of the unknown client
    assert out.missing_fields == ["client_name"]      # ...and the unknown is still reported


# ------------------------------------------------------------- documentation

def test_documentation_threshold_and_presence():
    c = clause("none", None, documentation=[{"item": "gst_invoice", "min_amount": "7500"}])
    assert check_unit(c, facts(7_499)).verdict == "compliant"                           # not required below the threshold
    assert check_unit(c, facts(7_500, documentation={"gst_invoice": True})).verdict == "compliant"
    assert check_unit(c, facts(7_500, documentation={"gst_invoice": False})).verdict == "violation"
    unknown = check_unit(c, facts(7_500))
    assert unknown.verdict == "insufficient_information" and unknown.missing_fields == ["documentation:gst_invoice"]


# ----------------------------------------------------------------- approval

def test_approval_threshold_uses_the_pre_division_total():
    c = clause("per_person", 2500, approval={"INR": D("15000")})
    # Rs 16,000 for 8 people = Rs 2,000 pp (within the cap) but the EVENT exceeds Rs 15,000.
    out = check_unit(c, facts(16_000, persons=8))
    assert out.verdict == "needs_approval"
    assert check_unit(c, facts(16_000, persons=8, approval_evidenced=True)).verdict == "compliant"
    assert check_unit(c, facts(15_000, persons=8)).verdict == "compliant"               # not above the threshold


def test_zero_threshold_means_approval_always_required():
    c = clause("none", None, approval={"INR": D("0")})
    assert check_unit(c, facts(10)).verdict == "needs_approval"
    assert check_unit(c, facts(10, approval_evidenced=True)).verdict == "compliant"


def test_over_limit_outranks_needs_approval():
    c = clause("per_person", 2500, approval={"INR": D("15000")})
    assert check_unit(c, facts(40_000, persons=8)).verdict == "violation"


# ------------------------------------------------------------------- rollup

def test_rollup_precedence():
    assert pc.rollup(["compliant", "insufficient_information"]) == "insufficient_information"
    assert pc.rollup(["compliant", "insufficient_information", "needs_approval"]) == "needs_approval"
    assert pc.rollup(["needs_approval", "violation", "compliant"]) == "violation"
    assert pc.rollup(["insufficient_information", "violation"]) == "violation"
    assert pc.rollup(["compliant", "compliant"]) == "compliant"
    assert pc.rollup([]) == "insufficient_information"


# ------------------------------------------------------------ dates & counts

def test_nights_and_days():
    assert pc.nights_between("2026-04-01", "2026-04-04") == 3
    assert pc.nights_between("01/04/2026", "04/04/2026") == 3          # day-first
    assert pc.nights_between("1 Apr 2026", "4 April 2026") == 3
    assert pc.nights_between("2026-04-01", "2026-04-01") is None       # a same-day stay isn't a night
    assert pc.nights_between("2026-04-04", "2026-04-01") is None
    assert pc.nights_between("garbage", "2026-04-01") is None
    assert pc.inclusive_days("2026-04-06", "2026-04-08") == 3


def test_parse_count():
    assert pc.parse_count("4") == 4 and pc.parse_count("4 pax") == 4 and pc.parse_count(4) == 4
    assert pc.parse_count("four") is None and pc.parse_count("0") is None and pc.parse_count(True) is None


# --------------------------------------------------------------- formatting

def test_format_comparison_matches_the_ui_string():
    assert pc.format_comparison(D("1450"), D("1200"), "per_trip", "INR") == "₹1,450 vs ₹1,200 limit (per trip)"
    assert pc.format_money(D("100000"), "INR") == "₹1,00,000"
    assert pc.format_money(D("1450.5"), "USD") == "$1,450.50"
    assert pc.format_comparison(None, D("1"), "per_trip", "INR") is None


# ----------------------------------------------------------- reference data

def test_reference_lookups():
    ref = pol.parse_reference_data(pol.POLICY_MD_PATH.read_text(encoding="utf-8"))
    assert pol.grade_rank(ref, "L4") == 4 and pol.grade_rank(ref, "L9") is None
    assert pol.city_tier(ref, "Bangalore") == "1" and pol.city_tier(ref, "Gurugram") == "1"
    assert pol.city_tier(ref, "Jaipur") == "2" and pol.city_tier(ref, "Shillong") == "3"
    assert pol.city_tier(ref, None) is None
    assert pol.zone_of(ref, "United States") == "A" and pol.zone_of(ref, "UAE") is None   # never guessed as B


# ------------------------------------------- pre-Stage-4 fixes: no silent garbage

def test_grade_varying_clause_with_unknown_grade_is_insufficient_naming_grade():
    c = clause("per_month", table=[LimitEntry(D("800"), "INR", {"grade": ("L1", "L2")}), LimitEntry(D("1500"), "INR", {"grade": ("L3", "L4")})])
    out = check_unit(c, facts(1_000, period_amounts=[], dims={"grade": None}))
    assert out.verdict == "insufficient_information" and out.missing_fields == ["grade"] and out.limit_applied is None
    # ...but decisive when every possible grade's limit agrees on the outcome
    assert check_unit(c, facts(700, period_amounts=[], dims={"grade": None})).verdict == "compliant"
    assert check_unit(c, facts(2_000, period_amounts=[], dims={"grade": None})).verdict == "violation"
    assert check_unit(c, facts(1_000, period_amounts=[], dims={"grade": "L4"})).verdict == "compliant"
    assert check_unit(c, facts(1_000, period_amounts=[], dims={"grade": "L2"})).verdict == "violation"


def test_limit_unit_without_limit_values_never_reads_as_no_limit():
    out = check_unit(clause("per_night", None), facts(99_999, nights=1))
    assert out.verdict == "insufficient_information" and out.missing_fields == ["policy_limit_missing"]
    assert out.limit_applied is None and out.comparison_result == "not_evaluated"


def test_computed_totals_are_not_labelled_as_per_km_or_percent_limits():
    assert pc.format_comparison(D("8110"), D("8829"), "per_km", "INR") == "₹8,110 vs ₹8,829 limit (km rate x distance)"
    assert "per km" not in pc.format_comparison(D("1"), D("2"), "per_km", "INR")
    assert pc.format_comparison(D("3000"), D("3000"), "percent_of_amount", "INR").endswith("(% of bill amount)")
