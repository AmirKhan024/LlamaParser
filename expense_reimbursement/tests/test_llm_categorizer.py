"""Tests for the few-shot `llm` categorizer (categorize.py): the prompt
builder, the strict response validator, categorize_llm's handling of
failures, and the guarantee that none of the few-shot examples come from
the frozen eval set. No network -- the Groq client is mocked.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from groq import BadRequestError

import categorize
from categories import CATEGORIES, CATEGORY_IDS, TIE_BREAK_RULES
from categorize import CategorizationInput, build_llm_messages, categorize_llm, parse_llm_response

DATASET_PATH = Path(__file__).resolve().parent.parent / "eval" / "categorization" / "dataset.jsonl"


def _inp(**overrides) -> CategorizationInput:
    base = dict(
        document_type="restaurant_bill", vendor_name="Test Bistro", line_item_names=["Dosa", "Filter coffee"],
        amount="450.00", markdown_excerpt="OCR-ONLY-MARKER should never reach the llm prompt",
        date="2026-04-01", currency="INR", additional_fields={"table": "7"},
    )
    base.update(overrides)
    return CategorizationInput(**base)


def _response(content: str, prompt_tokens=1400, completion_tokens=60):
    message = SimpleNamespace(content=content)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                            total_tokens=prompt_tokens + completion_tokens)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _json_error(code="json_validate_failed") -> BadRequestError:
    return BadRequestError("bad", response=MagicMock(), body={"error": {"code": code, "message": "x"}})


# ------------------------------------------------------------- prompt builder

def test_messages_are_system_then_three_worked_examples_then_the_document():
    messages = build_llm_messages(_inp())
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user", "assistant", "user", "assistant", "user"]


def test_system_prompt_has_every_category_with_its_one_line_definition_and_the_tie_breaks():
    system = build_llm_messages(_inp())[0]["content"]
    for category in CATEGORIES.values():
        assert f"- {category.id}: {category.definition}" in system
    for rule in TIE_BREAK_RULES:
        assert rule in system


def test_worked_examples_are_valid_answers_in_the_output_format():
    messages = build_llm_messages(_inp())
    answers = [json.loads(m["content"]) for m in messages if m["role"] == "assistant"]
    assert len(answers) >= 2
    for answer in answers:
        assert set(answer) == {"category", "confidence", "reason"}
        assert answer["category"] in CATEGORY_IDS
        assert 0.0 <= answer["confidence"] <= 1.0


def test_document_message_carries_the_extracted_fields_and_no_ocr_text():
    last = build_llm_messages(_inp())[-1]["content"]
    for expected in ["restaurant_bill", "Test Bistro", "2026-04-01", "450.00 INR", "Dosa; Filter coffee", '"table": "7"']:
        assert expected in last
    assert "OCR-ONLY-MARKER" not in json.dumps(build_llm_messages(_inp()))


def test_missing_fields_are_marked_not_dropped():
    last = build_llm_messages(_inp(vendor_name=None, date=None, amount=None, line_item_names=[], additional_fields={}))[-1]["content"]
    assert "vendor_name: (none)" in last
    assert "date: (none)" in last
    assert "amount: (none)" in last
    assert "line_items: (none)" in last
    assert "additional_fields: {}" in last


def test_long_additional_fields_and_line_items_are_capped():
    huge = {"notes": "x" * 5000}
    many = [f"item {i}" for i in range(100)]
    last = build_llm_messages(_inp(additional_fields=huge, line_item_names=many))[-1]["content"]
    assert "...(truncated)" in last
    assert "item 19" in last and "item 20" not in last  # first 20 line items only


def test_prompt_is_deterministic():
    assert build_llm_messages(_inp()) == build_llm_messages(_inp())


# ----------------------------------------------------------------- provenance

def test_few_shot_examples_are_not_drawn_from_the_eval_set():
    if not DATASET_PATH.exists():
        pytest.skip("eval dataset not built")
    corpus = DATASET_PATH.read_text(encoding="utf-8").lower()
    for example_input, _ in categorize._FEW_SHOT_EXAMPLES:
        names = [example_input.vendor_name]
        names += [v for k, v in example_input.additional_fields.items() if k not in ("route", "attendees")]
        for name in names:
            assert name.lower() not in corpus, f"few-shot example name {name!r} appears in the eval set"


# ---------------------------------------------------------------- fingerprint

def test_fingerprint_is_stable_and_tracks_model_effort_and_examples(monkeypatch):
    base = categorize.llm_config_fingerprint()
    assert base == categorize.llm_config_fingerprint()
    assert categorize.llm_config_fingerprint("openai/gpt-oss-20b") != base
    monkeypatch.setattr(categorize, "LLM_REASONING_EFFORT", "low")
    assert categorize.llm_config_fingerprint() != base
    monkeypatch.setattr(categorize, "LLM_REASONING_EFFORT", None)
    monkeypatch.setattr(categorize, "_FEW_SHOT_EXAMPLES", categorize._FEW_SHOT_EXAMPLES[:2])
    assert categorize.llm_config_fingerprint() != base


# ------------------------------------------------------------------- validator

def test_valid_response_parses():
    parsed = parse_llm_response('{"category": "fuel", "confidence": 0.9, "reason": "petrol pump"}')
    assert (parsed.category, parsed.confidence, parsed.reason, parsed.failure_reason) == ("fuel", 0.9, "petrol pump", None)


@pytest.mark.parametrize("raw, why", [
    ("not json at all", "not valid JSON"),
    ("", "not valid JSON"),
    ("[1, 2]", "not an object"),
    ('{"confidence": 0.9}', "no 'category' key"),
    ('{"category": null}', "not in the allowed list"),
    ('{"category": "petrol"}', "not in the allowed list"),
    ('{"category": "Fuel"}', "not in the allowed list"),      # wrong case is NOT coerced
    ('{"category": " fuel"}', "not in the allowed list"),     # nor is whitespace trimmed
    ('{"category": ["fuel"]}', "not in the allowed list"),
    ('{"category": 3}', "not in the allowed list"),
])
def test_anything_but_an_exact_allowed_id_is_a_parse_failure(raw, why):
    parsed = parse_llm_response(raw)
    assert parsed.category is None
    assert why in parsed.failure_reason


@pytest.mark.parametrize("confidence", ['"high"', "1.5", "-0.2", "null"])
def test_bad_confidence_does_not_fail_an_otherwise_valid_answer(confidence):
    parsed = parse_llm_response('{"category": "other", "confidence": %s, "reason": "r"}' % confidence)
    assert parsed.category == "other"
    assert parsed.confidence == 0.0
    assert parsed.failure_reason is None


def test_legacy_rationale_key_is_still_read():
    assert parse_llm_response('{"category": "other", "confidence": 0.5, "rationale": "old key"}').reason == "old key"


# --------------------------------------------------------------- categorize_llm

def _client_returning(*outcomes):
    client = MagicMock()
    client.chat.completions.create.side_effect = list(outcomes)
    return client


def test_successful_call_records_tokens_cost_and_raw_exchange():
    client = _client_returning(_response('{"category": "travel_meals", "confidence": 0.8, "reason": "meal"}'))
    sink = {}
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize_llm(_inp(), raw_sink=sink)
    assert result.category == "travel_meals" and not result.parse_failure
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (1400, 60, 1460)
    assert result.estimated_cost_usd > 0
    assert sink["request"]["messages"][-1]["role"] == "user"
    assert json.loads(sink["response"])["category"] == "travel_meals"
    assert sink["usage"]["total_tokens"] == 1460
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["temperature"] == 0 and kwargs["model"] == categorize.LLM_MODEL


def test_invalid_category_is_a_parse_failure_not_a_silent_coercion():
    client = _client_returning(_response('{"category": "meals", "confidence": 0.9, "reason": "r"}'))
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize_llm(_inp())
    assert result.category is None and result.parse_failure
    assert "meals" in result.parse_failure_reason


def test_unparseable_json_is_a_parse_failure_not_a_crash():
    client = _client_returning(_response("Sure! The category is fuel."))
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize_llm(_inp())
    assert result.category is None and result.parse_failure


def test_groq_json_rejection_is_retried_once_then_succeeds():
    client = _client_returning(_json_error(), _response('{"category": "fuel", "confidence": 0.7, "reason": "r"}'))
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize_llm(_inp())
    assert result.category == "fuel" and not result.parse_failure
    assert client.chat.completions.create.call_count == 2


def test_groq_json_rejected_twice_becomes_a_parse_failure():
    client = _client_returning(_json_error(), _json_error("json_generate_failed"))
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize_llm(_inp())
    assert result.category is None and result.parse_failure
    assert "twice" in result.parse_failure_reason
    assert client.chat.completions.create.call_count == 2


def test_an_unrelated_bad_request_still_propagates():
    client = _client_returning(_json_error("something_else"))
    with patch.object(categorize, "_get_client", return_value=client):
        with pytest.raises(BadRequestError):
            categorize_llm(_inp())


def test_daily_quota_429_is_not_retried():
    from groq import RateLimitError

    client = _client_returning(RateLimitError("Rate limit ... on tokens per day (TPD) ...", response=MagicMock(), body=None))
    with patch.object(categorize, "_get_client", return_value=client):
        with pytest.raises(RateLimitError):
            categorize_llm(_inp())
    assert client.chat.completions.create.call_count == 1


def test_non_expense_document_makes_no_api_call():
    with patch.object(categorize, "_get_client") as get_client:
        result = categorize_llm(_inp(document_type="approval_correspondence"))
    get_client.assert_not_called()
    assert result.category is None and not result.parse_failure
