"""Clause selection validation (policy_select.py), with the model mocked: the
model's answer is untrusted, and every failure below must come back as a
hard failure -- never a repaired or coerced unit."""

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import policy as pol
import policy_select as ps
from policy import Clause, LimitEntry

D = Decimal
REF = {"grades": ["L1", "L2", "L3", "L4", "L5", "L6"], "city_tiers": {"1": ["mumbai"], "2": ["jaipur"]}, "zone_a_countries": ["usa"]}

FIELDS = {
    "vendor_name": "Palm Court", "date": "2026-05-24", "amount": "32181.59", "currency": "INR",
    "additional_fields": {"nights": "3", "check_in_date": "2026-05-24", "check_out_date": "2026-05-27", "guests": "four"},
    "line_items": [
        {"name": "Room x3", "quantity": "3", "unit_price": "10328.45", "total": "30985.35"},
        {"name": "Minibar", "quantity": "1", "unit_price": "1196.24", "total": "1196.24"},
    ],
}


def clause(cid, unit="per_night", table=None, **kw):
    table = table if table is not None else [LimitEntry(D("6000"), "INR", {"city_tier": ("1",)})]
    return Clause(cid, cid.split(".")[0], 0, f"text of {cid}", ("accommodation",), limit_unit=unit, limit_table=tuple(table), **kw)


CANDIDATES = [clause("8.1"), clause("8.4", "none", [], is_prohibition=True, conditions=(
    {"id": "c1", "text": "minibar", "kind": "excludes", "check": "judgment", "depends_on": []},))]
POLICY_IDS = {"8.1", "8.4", "9.2"}   # 9.2 exists in the policy but is not a candidate here


def unit(**kw):
    base = {"line_item_refs": [0], "clause_id": "8.1", "clause_confidence": 0.9,
            "amount_refs": [{"path": "line_items[0].total", "sign": 1}], "stated_amount": "30985.35",
            "amount_reason": "room line", "quantities": {"nights": {"count_path": "additional_fields.nights"}},
            "dimensions": {"city": "Mumbai"}, "conditions": {}, "documentation": {}, "approval_evidenced": None,
            "missing_fields": [], "explanation": "room", "confidence": 0.8}
    base.update(kw)
    return base


MINIBAR = lambda **kw: unit(line_item_refs=[1], clause_id="8.4", amount_refs=[{"path": "line_items[1].total", "sign": 1}],
                            stated_amount="1196.24", quantities={}, conditions={"c1": {"met": True, "evidence": "Minibar"}}, **kw)


def validate(units, fields=FIELDS, n_lines=2, approval_evidence=False):
    return ps.validate_selection(units, fields=fields, n_line_items=n_lines, candidates=CANDIDATES,
                                 policy_clause_ids=POLICY_IDS, reference=REF, currency="INR",
                                 has_approval_evidence=approval_evidence)


def test_valid_units_are_resolved_by_code_not_taken_on_trust():
    room, minibar = validate([unit(), MINIBAR()])
    assert isinstance(room, ps.ValidatedUnit)
    assert room.base_amount == D("30985.35") and room.nights == 3
    assert room.dims["city_tier"] == "1"               # code mapped "Mumbai" -> Tier 1
    assert minibar.judgments == {"c1": True}


# ------------------------------------------------------------- clause ids

def test_clause_id_not_in_policy_is_a_hard_failure():
    result = validate([unit(clause_id="99.9"), MINIBAR()])[0]
    assert isinstance(result, ps.UnitFailure) and result.code == "invalid_clause_id"
    assert "99.9" in result.reason


def test_clause_in_policy_but_not_a_candidate_is_a_hard_failure():
    result = validate([unit(clause_id="9.2"), MINIBAR()])[0]
    assert isinstance(result, ps.UnitFailure) and result.code == "not_a_candidate"


def test_null_clause_is_a_failure_with_the_models_reason():
    result = validate([unit(clause_id=None, explanation="nothing fits"), MINIBAR()])[0]
    assert result.code == "no_clause" and "nothing fits" in result.reason


# ------------------------------------------------------- amount reconciliation

def test_amount_that_does_not_reconcile_is_a_hard_failure_not_a_coercion():
    result = validate([unit(stated_amount="30000.00"), MINIBAR()])[0]
    assert isinstance(result, ps.UnitFailure) and result.code == "amount_mismatch"
    assert "30985.35" in result.reason and "30000" in result.reason


def test_amount_ref_to_a_path_that_does_not_exist_is_a_hard_failure():
    result = validate([unit(amount_refs=[{"path": "line_items[7].total", "sign": 1}]), MINIBAR()])[0]
    assert result.code == "amount_ref_invalid"


def test_amount_ref_to_a_non_number_is_a_hard_failure():
    result = validate([unit(amount_refs=[{"path": "vendor_name", "sign": 1}], stated_amount="1"), MINIBAR()])[0]
    assert result.code == "amount_ref_invalid"


def test_signed_refs_are_summed_by_code():
    refs = [{"path": "amount", "sign": 1}, {"path": "line_items[1].total", "sign": -1}]
    ok = validate([unit(amount_refs=refs, stated_amount="30985.35"), MINIBAR()])[0]
    assert isinstance(ok, ps.ValidatedUnit) and ok.base_amount == D("30985.35")


def test_stated_amount_without_refs_is_a_hard_failure():
    result = validate([unit(amount_refs=[], stated_amount="100"), MINIBAR()])[0]
    assert result.code == "amount_without_refs"


# -------------------------------------------------------------- quantities

def test_quantity_path_that_does_not_exist_is_a_hard_failure():
    result = validate([unit(quantities={"nights": {"count_path": "additional_fields.nope"}}), MINIBAR()])[0]
    assert result.code == "quantity_path_not_found"


def test_unreadable_quantity_is_missing_not_guessed():
    result = validate([unit(quantities={"persons": {"count_path": "additional_fields.guests"}}), MINIBAR()])[0]
    assert isinstance(result, ps.ValidatedUnit) and result.persons is None     # "four" is not read as 4
    assert any("persons" in n for n in result.notes)


def test_nights_from_dates_are_computed_by_code():
    q = {"nights": {"check_in_path": "additional_fields.check_in_date", "check_out_path": "additional_fields.check_out_date"}}
    assert validate([unit(quantities=q), MINIBAR()])[0].nights == 3


# ------------------------------------------------------------- partitioning

def test_units_must_cover_every_line_item_exactly_once():
    with pytest.raises(ps.SelectionError) as e:
        validate([unit()])                                   # line item 1 never covered
    assert e.value.code == "bad_partition"
    with pytest.raises(ps.SelectionError):
        validate([unit(), unit(), MINIBAR()])                # line item 0 twice


def test_document_without_line_items_needs_exactly_one_whole_document_unit():
    plain = {**FIELDS, "line_items": []}
    ok = validate([unit(line_item_refs=[], amount_refs=[{"path": "amount", "sign": 1}], stated_amount="32181.59")], fields=plain, n_lines=0)
    assert isinstance(ok[0], ps.ValidatedUnit)
    with pytest.raises(ps.SelectionError):
        validate([unit(line_item_refs=[0])], fields=plain, n_lines=0)


def test_bad_json_and_missing_units_are_structural_failures():
    with pytest.raises(ps.SelectionError) as e:
        ps.parse_response("not json")
    assert e.value.code == "invalid_json"
    with pytest.raises(ps.SelectionError):
        ps.parse_response(json.dumps({"units": []}))


# ------------------------------------------------- dimensions / judgments

def test_unlisted_city_is_tier_three_and_no_city_is_unknown():
    assert validate([unit(dimensions={"city": "Shillong"}), MINIBAR()])[0].dims["city_tier"] == "3"
    assert validate([unit(dimensions={"city": None}), MINIBAR()])[0].dims["city_tier"] is None


def test_non_boolean_judgment_is_treated_as_unknown():
    bad = ps.validate_selection(
        [unit(), {**MINIBAR(), "conditions": {"c1": {"met": "probably"}}}], fields=FIELDS, n_line_items=2,
        candidates=CANDIDATES, policy_clause_ids=POLICY_IDS, reference=REF, currency="INR", has_approval_evidence=False)[1]
    assert bad.judgments == {"c1": None}


def test_reported_approval_without_any_approval_evidence_is_rejected():
    u = unit(approval_evidenced=True)
    assert validate([u, MINIBAR()], approval_evidence=False)[0].approval_evidenced is False
    assert validate([u, MINIBAR()], approval_evidence=True)[0].approval_evidenced is True


def test_the_prompt_never_shows_the_model_the_numbers():
    view = json.dumps(ps.candidate_view(CANDIDATES[0]))
    assert "6000" not in view and "per_night" in view and "city_tier" in view


def test_prompt_file_is_versioned_and_hashed():
    text, sha = ps.load_prompt("v1")
    assert "You NEVER decide the verdict" in text and len(sha) == 64
    with pytest.raises(FileNotFoundError):
        ps.load_prompt("v999")


def test_informational_clause_like_section_24_fails_loudly_and_is_not_substituted():
    result = validate([unit(clause_id="9.2"), MINIBAR()])[0]        # in the policy, not a candidate (like 24.1)
    assert isinstance(result, ps.UnitFailure) and result.code == "not_a_candidate"
    assert "not a machine-checkable candidate" in result.reason and "no other clause is substituted" in result.reason
