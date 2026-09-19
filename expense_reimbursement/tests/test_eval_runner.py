"""Tests for scripts/eval_categorization.py's run mechanics: the disk
cache, per-document checkpointing, resume after a quota error, cache
invalidation when the prompt changes, --no-cache, the clean exit on quota
exhaustion (RESULTS.md must NOT be written), and the default-method
decision rule. The categorizer call itself is faked -- no network.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from groq import RateLimitError

import eval_categorization as ev
from categorize import CategorizationResult

CATS = ["fuel", "other", "travel_meals", "accommodation", "local_transport"]


def _rows(n=5, reviewed=True):
    return [
        {"id": f"doc-{i}", "source": "synthetic_image" if i % 2 else "sroie", "gold_label": CATS[i % len(CATS)],
         "silver_label": CATS[i % len(CATS)], "reviewed": reviewed, "notes": "",
         "extracted_fields": {"document_type": "generic_receipt", "vendor_name": f"V{i}"}, "markdown_path": None}
        for i in range(n)
    ]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "CACHE_DIR", tmp_path / "_predictions_cache")
    monkeypatch.setattr(ev, "RAW_CACHE_DIR", tmp_path / "_llm_raw_cache")
    monkeypatch.setattr(ev, "STATE_PATH", tmp_path / "_state.json")
    monkeypatch.setattr(ev, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(ev, "DATASET_PATH", tmp_path / "dataset.jsonl")
    return tmp_path


class FakeLLM:
    """Stands in for eval_categorization._run_method; records every call."""

    def __init__(self, fail_on=None, exc=None):
        self.calls = []
        self.fail_on = fail_on  # 1-based call number that raises exc
        self.exc = exc

    def __call__(self, method, inp, raw_sink):
        self.calls.append(inp.vendor_name)
        if self.fail_on == len(self.calls):
            raise self.exc
        if raw_sink is not None:
            raw_sink.update({"request": {"messages": ["m"]}, "response": '{"category": "other"}', "usage": {"total_tokens": 1500}})
        return CategorizationResult("other", 0.9, "because", "llm", 700, 0.0003, total_tokens=1500, prompt_tokens=1440, completion_tokens=60)


def _quota_error():
    return RateLimitError("Rate limit reached ... on tokens per day (TPD)", response=MagicMock(), body=None)


# ----------------------------------------------------------------------- cache

def test_second_run_reuses_the_cache_and_makes_zero_calls(paths, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(ev, "_run_method", fake)
    ev._predict_all(_rows(), "llm")
    assert len(fake.calls) == 5
    ev._predict_all(_rows(), "llm")
    assert len(fake.calls) == 5  # nothing re-spent


def test_no_cache_flag_re_runs_everything(paths, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(ev, "_run_method", fake)
    ev._predict_all(_rows(), "llm")
    ev._predict_all(_rows(), "llm", use_cache=False)
    assert len(fake.calls) == 10


def test_raw_request_and_response_are_written_per_document(paths, monkeypatch):
    monkeypatch.setattr(ev, "_run_method", FakeLLM())
    ev._predict_all(_rows(3), "llm")
    files = sorted(p.name for p in (paths / "_llm_raw_cache").iterdir())
    assert files == ["doc-0.json", "doc-1.json", "doc-2.json"]
    raw = json.loads((paths / "_llm_raw_cache" / "doc-1.json").read_text())
    assert raw["id"] == "doc-1" and raw["response"] == '{"category": "other"}'
    assert raw["config_fingerprint"] and raw["request"]["messages"] == ["m"]


def test_cached_rows_from_a_different_prompt_are_not_reused(paths, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(ev, "_run_method", fake)
    ev._predict_all(_rows(3), "llm")
    cache_file = paths / "_predictions_cache" / "llm.jsonl"
    stale = [dict(json.loads(line), config_fingerprint="an-older-prompt") for line in cache_file.read_text().splitlines()]
    cache_file.write_text("\n".join(json.dumps(r) for r in stale) + "\n")
    ev._predict_all(_rows(3), "llm")
    assert len(fake.calls) == 6  # every stale row re-run rather than silently trusted


def test_non_llm_methods_are_unaffected_by_fingerprints(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(ev, "_run_method", lambda m, inp, sink: calls.append(m) or CategorizationResult("other", 0.5, "", m))
    ev._predict_all(_rows(3), "rules")
    ev._predict_all(_rows(3), "rules")
    assert len(calls) == 3


# ------------------------------------------------------ checkpoint and resume

def test_quota_error_mid_run_raises_with_a_count_and_leaves_a_checkpoint(paths, monkeypatch):
    fake = FakeLLM(fail_on=3, exc=_quota_error())
    monkeypatch.setattr(ev, "_run_method", fake)
    with pytest.raises(ev.QuotaExhausted) as info:
        ev._predict_all(_rows(5), "llm")
    assert (info.value.completed, info.value.total) == (2, 5)
    cached = [json.loads(line)["id"] for line in (paths / "_predictions_cache" / "llm.jsonl").read_text().splitlines()]
    assert cached == ["doc-0", "doc-1"]  # every finished document was checkpointed


def test_resume_only_runs_the_documents_that_are_left(paths, monkeypatch):
    monkeypatch.setattr(ev, "_run_method", FakeLLM(fail_on=3, exc=_quota_error()))
    with pytest.raises(ev.QuotaExhausted):
        ev._predict_all(_rows(5), "llm")

    resumed = FakeLLM()
    monkeypatch.setattr(ev, "_run_method", resumed)
    predictions = ev._predict_all(_rows(5), "llm")
    assert resumed.calls == ["V2", "V3", "V4"]  # not restarted from V0
    assert sorted(predictions) == [f"doc-{i}" for i in range(5)]


def test_non_quota_failure_leaves_the_run_incomplete_and_is_retried_next_time(paths, monkeypatch):
    monkeypatch.setattr(ev, "_run_method", FakeLLM(fail_on=2, exc=ConnectionError("network blip")))
    with pytest.raises(ev.IncompleteRun) as info:
        ev._predict_all(_rows(4), "llm")
    assert (info.value.errors, info.value.completed, info.value.total) == (1, 3, 4)

    retry = FakeLLM()
    monkeypatch.setattr(ev, "_run_method", retry)
    ev._predict_all(_rows(4), "llm")
    assert retry.calls == ["V1"]  # only the document that errored


# ------------------------------------------------------ clean exit, RESULTS.md

def _write_dataset(paths, rows):
    (paths / "dataset.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_quota_exhaustion_exits_cleanly_and_does_not_write_results(paths, monkeypatch, capsys):
    _write_dataset(paths, _rows(5))
    monkeypatch.setattr(ev, "_run_method", FakeLLM(fail_on=4, exc=_quota_error()))
    monkeypatch.setattr(ev, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["eval_categorization.py", "--final", "--methods", "llm"])
    with pytest.raises(SystemExit) as info:
        ev.main()
    assert info.value.code == 2
    out = capsys.readouterr().out
    assert "3/5 documents completed" in out and "RESULTS.md was NOT updated" in out
    assert not (paths / "RESULTS.md").exists()
    assert not (paths / "_state.json").exists() or "llm" not in json.loads((paths / "_state.json").read_text())


def test_a_completed_run_writes_results_and_records_the_method(paths, monkeypatch):
    _write_dataset(paths, _rows(5))
    monkeypatch.setattr(ev, "_run_method", FakeLLM())
    monkeypatch.setattr(ev, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["eval_categorization.py", "--final", "--methods", "llm"])
    ev.main()
    assert "llm" in json.loads((paths / "_state.json").read_text())
    text = (paths / "RESULTS.md").read_text(encoding="utf-8")
    assert "## Decision rule" in text and "## Comparison" in text and "not pursued" in text


def test_final_refuses_unreviewed_rows(paths, monkeypatch):
    _write_dataset(paths, _rows(3, reviewed=False))
    monkeypatch.setattr(ev, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["eval_categorization.py", "--final"])
    with pytest.raises(SystemExit) as info:
        ev.main()
    assert "reviewed" in str(info.value)


def test_hybrid_is_not_selectable(paths, monkeypatch):
    _write_dataset(paths, _rows(3))
    monkeypatch.setattr(ev, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["eval_categorization.py", "--final", "--methods", "hybrid"])
    with pytest.raises(SystemExit) as info:
        ev.main()
    assert "not pursued" in str(info.value)


# ------------------------------------------------------------- decision rule

def _entry(real_acc, all_acc):
    metrics = lambda acc: {"n": 40, "accuracy": acc, "macro_f1": acc}
    return {"unavailable": False, "metrics_real": metrics(real_acc), "metrics_all": metrics(all_acc)}


def test_one_method_winning_both_metrics_becomes_the_default():
    decision = ev._decide({"rules": _entry(0.80, 0.85), "classifier": _entry(0.50, 0.70), "llm": _entry(0.90, 0.92)})
    assert decision["default"] == "llm" and not decision["split"]


def test_split_winners_keep_rules_as_the_default():
    decision = ev._decide({"rules": _entry(0.80, 0.90), "classifier": _entry(0.50, 0.70), "llm": _entry(0.90, 0.85)})
    assert decision["default"] == "rules" and decision["split"]
    assert decision["primary"] == ["llm"] and decision["secondary"] == ["rules"]


def test_a_tie_for_first_is_not_a_win_so_rules_stays():
    decision = ev._decide({"rules": _entry(0.80, 0.85), "classifier": _entry(0.50, 0.70), "llm": _entry(0.80, 0.90)})
    assert decision["default"] == "rules" and decision["split"]
    assert decision["primary"] == ["llm", "rules"]


def test_rules_winning_both_stays_rules_without_a_split():
    decision = ev._decide({"rules": _entry(0.90, 0.90), "classifier": _entry(0.50, 0.70), "llm": _entry(0.80, 0.85)})
    assert decision["default"] == "rules" and not decision["split"]


def test_wilson_interval_brackets_the_estimate():
    low, high = ev._wilson(36, 43)
    assert low < 36 / 43 < high
    assert ev._wilson(0, 0) is None


# ------------------------------------------------------- frozen eval set guard

def test_build_eval_dataset_refuses_to_rebuild_a_reviewed_set(tmp_path, monkeypatch):
    import build_eval_dataset as build

    dataset = tmp_path / "dataset.jsonl"
    original = json.dumps({"id": "cat-sroie-000", "reviewed": True}) + "\n"
    dataset.write_text(original)
    monkeypatch.setattr(build, "DATASET_PATH", dataset)
    monkeypatch.setattr(sys, "argv", ["build_eval_dataset.py"])
    with pytest.raises(SystemExit) as info:
        build.main()
    assert "frozen" in str(info.value)
    assert dataset.read_text() == original  # untouched


def test_an_unreviewed_dataset_is_not_frozen(tmp_path, monkeypatch):
    import build_eval_dataset as build

    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps({"id": "x", "reviewed": False}) + "\n")
    monkeypatch.setattr(build, "DATASET_PATH", dataset)
    assert build._is_frozen() is False
