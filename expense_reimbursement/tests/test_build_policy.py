"""scripts/build_policy.py and policy.py's segmentation, offline (the model is
a fake): clause ids/text come from code, the model's structure is validated
against the text, and everything doubtful lands in CONFLICTS."""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

import build_policy as bp
import policy as pol

MD = pol.POLICY_MD_PATH.read_text(encoding="utf-8")


def entry(cid, **kw):
    base = {"clause_id": cid, "applies_to_categories": [], "limit_unit": "none", "unit_evidence": None,
            "limit_kind": "amount", "limit_inclusive": True, "limit_table": [], "conditions": [],
            "documentation_required": [], "requires_approval_above": None, "is_prohibition": False, "notes": None}
    base.update(kw)
    return base


def fake_ask(per_clause):
    """ask(messages, scope): answers every clause of the requested section from `per_clause`
    (id -> entry overrides); unlisted clauses are inert."""
    def ask(messages, scope):
        payload = json.loads(messages[1]["content"])
        return json.dumps({"clauses": [entry(c["clause_id"], **per_clause.get(c["clause_id"], {})) for c in payload["clauses"]]})
    return ask


def test_segmentation_finds_every_numbered_clause_and_the_bullets():
    raws = pol.segment_policy(MD)
    ids = [r.clause_id for r in raws]
    assert len(ids) == len(set(ids)) >= 95
    for expected in ("1.1", "5.3.1", "5.3.2", "8.1", "8.6", "10.4", "19.1", "19.8", "24.5"):
        assert expected in ids
    assert "2.1" not in ids and "2.2" not in ids            # section 2 is reference data
    by_id = {r.clause_id: r for r in raws}
    assert by_id["8.1"].text.startswith("8.1. **Nightly caps") and "| L4 | 8,000 | 6,000 | 4,500 |" in by_id["8.1"].text
    assert by_id["19.3"].synthesized_id and "The following are never reimbursable" in by_id["19.3"].text
    assert "8.2." not in by_id["8.1"].text                  # a clause stops where the next starts


def test_clause_text_is_verbatim_from_the_source_file():
    for r in pol.segment_policy(MD):
        first_line = r.text.splitlines()[-1] if r.synthesized_id else r.text.splitlines()[0]
        assert first_line in MD


def test_reference_tables_parse():
    ref = pol.parse_reference_data(MD)
    assert ref["grades"] == ["L1", "L2", "L3", "L4", "L5", "L6"]
    assert "mumbai" in ref["city_tiers"]["1"] and "jaipur" in ref["city_tiers"]["2"]
    assert "singapore" in ref["zone_a_countries"] and ref["zone_a_open_ended"] == ["Western Europe"]


def test_categories_come_from_categories_py_for_mapped_sections():
    clauses, _c, failed = bp.build(fake_ask({"8.1": {"applies_to_categories": ["other"]}}), MD, {"8"})
    c = {x.clause_id: x for x in clauses}["8.1"]
    assert not failed and c.applies_to_categories == ("accommodation", "other")    # mapping kept, model's addition allowed
    plain = {x.clause_id: x for x in clauses}["8.2"]
    assert plain.applies_to_categories == ("accommodation",)                       # model said [] -> mapping still applies


def test_schema_violation_is_retried_then_reported_not_silently_fixed():
    calls = []

    def ask(messages, scope):
        calls.append(len(messages))
        payload = json.loads(messages[1]["content"])
        return json.dumps({"clauses": [entry(c["clause_id"], limit_unit="per_fortnight") for c in payload["clauses"]]})

    clauses, conflicts, failed = bp.build(ask, MD, {"7"})
    # whole section, then the failed subset with the problems fed back, then one clause at a time
    assert len(calls) == 1 + 1 + 3 and calls[1] > calls[0]
    assert set(failed) == {"7.1", "7.2", "7.3"}
    assert any(c.kind == "UNSTRUCTURED" for c in conflicts)
    assert all(c.applies_to_categories == () for c in clauses)    # stubs are never offered as candidates


def test_conflict_unit_undetermined_and_disagreeing_with_the_text():
    ask = fake_ask({
        "8.5": {"limit_unit": "unknown", "limit_table": [{"amount": 300, "currency": "INR", "when": {}}]},
        "9.2": {"limit_unit": "per_trip", "unit_evidence": "Amount per day",
                "limit_table": [{"amount": 1200, "currency": "INR", "when": {"city_tier": ["1"]}}]},
    })
    _clauses, conflicts, _failed = bp.build(ask, MD, {"8", "9"})
    kinds = {(c.kind, c.clause_ids) for c in conflicts}
    assert ("UNIT_UNDETERMINED", ("8.5",)) in kinds
    assert ("UNIT_DISAGREES_WITH_TEXT", ("9.2",)) in kinds        # text says "per day", model said per_trip


def test_conflict_number_not_in_text_and_limit_equal_to_approval_threshold():
    ask = fake_ask({
        "10.4": {"limit_unit": "per_event", "unit_evidence": "single client entertainment event",
                 "limit_table": [{"amount": 15000, "currency": "INR", "when": {}}],
                 "requires_approval_above": {"INR": 15000}},
        "10.2": {"limit_unit": "per_person", "unit_evidence": "Per-person cap",
                 "limit_table": [{"amount": 9999, "currency": "INR", "when": {}}]},
    })
    _c, conflicts, _f = bp.build(ask, MD, {"10"})
    kinds = {(c.kind, c.clause_ids) for c in conflicts}
    assert ("LIMIT_EQUALS_APPROVAL_THRESHOLD", ("10.4",)) in kinds
    assert ("NUMBER_NOT_IN_TEXT", ("10.2",)) in kinds


def test_conflict_overlapping_limits_and_stage1_gaps_and_prohibition_without_trigger():
    ask = fake_ask({
        "8.1": {"limit_unit": "per_night", "unit_evidence": "Nightly caps",
                "limit_table": [{"amount": 6000, "currency": "INR", "when": {"grade": ["L3"]}}]},
        "8.3": {"limit_unit": "per_night", "unit_evidence": "night",
                "limit_table": [{"amount": 1500, "currency": "INR", "when": {}}]},
        "8.4": {"is_prohibition": True},
        "8.6": {"conditions": [{"id": "c1", "text": "GSTIN shown", "kind": "requires", "check": "judgment",
                                "depends_on": ["gstin_on_invoice", "document_amount"], "numeric": None}]},
    })
    _c, conflicts, _f = bp.build(ask, MD, {"8"})
    kinds = {(c.kind, c.clause_ids) for c in conflicts}
    assert ("OVERLAP", ("8.1", "8.3")) in kinds
    assert ("PROHIBITION_WITHOUT_TRIGGER", ("8.4",)) in kinds
    gap = [c for c in conflicts if c.kind == "STAGE1_GAP" and c.clause_ids == ("8.6",)][0]
    assert "gstin_on_invoice (not_extracted)" in gap.message and "document_amount" not in gap.message


def test_any_of_and_numeric_conditions_are_accepted_and_normalised():
    ask = fake_ask({"15.3": {"conditions": [{
        "id": "c1", "text": "bond signed if above 50,000", "kind": "requires", "depends_on": [],
        "any_of": [
            {"id": "c1a", "text": "not above 50,000", "check": "numeric", "numeric": {"quantity": "amount", "op": "<=", "value": 50000}},
            {"id": "c1b", "text": "bond signed", "check": "judgment", "depends_on": ["business_purpose"]},
        ]}]}})
    clauses, _c, failed = bp.build(ask, MD, {"15"})
    cond = {c.clause_id: c for c in clauses}["15.3"].conditions[0]
    assert not failed and [s["id"] for s in cond["any_of"]] == ["c1a", "c1b"] and cond["any_of"][0]["numeric"]["value"] == "50000"


def test_a_clause_without_a_limit_tolerates_a_null_limit_kind():
    clauses, _c, failed = bp.build(fake_ask({"17.1": {"limit_kind": None}, "17.2": {"limit_kind": "none"}}), MD, {"17"})
    assert not failed


def test_review_table_lists_every_clause_and_flags(capsys):
    clauses, conflicts, _f = bp.build(fake_ask({"8.4": {"is_prohibition": True}}), MD, {"8"})
    flagged = {}
    for c in conflicts:
        for cid in c.clause_ids:
            flagged.setdefault(cid, []).append(c.kind)
    table = bp.render_review_table(clauses, flagged)
    for cid in ("8.1", "8.2", "8.3", "8.4", "8.5", "8.6"):
        assert cid in table
    assert "PROHIBITION_WITHOUT_TRIGGER" in table and "CONFLICTS" in bp.render_conflicts(conflicts)


def test_main_writes_nothing_without_confirm(tmp_path, monkeypatch):
    import policy_llm
    out = tmp_path / "policy_v1.json"
    monkeypatch.setattr(policy_llm, "call_json", lambda messages, **kw: policy_llm.LLMCall(
        fake_ask({})(messages, ""), 1, 1, 1, False, "m"))
    monkeypatch.setattr(sys, "argv", ["build_policy.py", "--out", str(out)])
    assert bp.main() == 0 and not out.exists()
    monkeypatch.setattr(sys, "argv", ["build_policy.py", "--out", str(out), "--confirm", "--reviewed-by", "Tester"])
    assert bp.main() == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["build_meta"]["human_reviewed"] is True and doc["build_meta"]["reviewed_by"] == "Tester"
    assert len(doc["clauses"]) >= 95 and doc["reference_data"]["grades"][0] == "L1"
