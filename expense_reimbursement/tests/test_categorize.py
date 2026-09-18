"""Unit tests for categorize.py's four methods -- no network for rules/
classifier (classifier needs the trained model file, see
scripts/train_categorizer.py); llm/hybrid mock the Groq client the same
way tests/test_extract_repair.py does.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import categorize
from categories import CATEGORY_IDS
from categorize import CategorizationInput, categorize as run_categorizer


def _fake_llm_response(category, confidence=0.9, rationale="test"):
    message = SimpleNamespace(content=json.dumps({"category": category, "confidence": confidence, "rationale": rationale}))
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    return SimpleNamespace(choices=[choice], usage=usage)


# ------------------------------------------------------------------ rules

def test_rules_approval_correspondence_has_no_category():
    inp = CategorizationInput("approval_correspondence", None, [], None, "")
    result = categorize.categorize_rules(inp)
    assert result.category is None
    assert result.method == "rules"


def test_rules_direct_document_type_mapping():
    inp = CategorizationInput("telecom_bill", "Airtel", [], "500", "")
    result = categorize.categorize_rules(inp)
    assert result.category == "phone_internet"


def test_rules_fuel_keyword_on_generic_receipt():
    inp = CategorizationInput("generic_receipt", "Highway Fuel Stop", [], "1000", "Petrol 40 litres")
    result = categorize.categorize_rules(inp)
    assert result.category == "fuel"


def test_rules_restaurant_bill_client_tie_break():
    inp = CategorizationInput("restaurant_bill", "Spice Route", [], "3000", "Client: Meridian Retail, 4 attendees")
    result = categorize.categorize_rules(inp)
    assert result.category == "client_entertainment"


def test_rules_restaurant_bill_team_tie_break():
    inp = CategorizationInput("restaurant_bill", "Spice Route", [], "1500", "Team dinner celebration")
    result = categorize.categorize_rules(inp)
    assert result.category == "team_events"


def test_rules_restaurant_bill_default_is_travel_meals():
    inp = CategorizationInput("restaurant_bill", "Spice Route", [], "400", "Dinner for one")
    result = categorize.categorize_rules(inp)
    assert result.category == "travel_meals"


def test_rules_never_raises_on_empty_input():
    inp = CategorizationInput("generic_receipt", None, [], None, "")
    result = categorize.categorize_rules(inp)
    assert result.category in CATEGORY_IDS


# -------------------------------------------------------------------- llm

def test_llm_approval_correspondence_skips_the_api_call_entirely():
    inp = CategorizationInput("approval_correspondence", None, [], None, "")
    with patch.object(categorize, "_get_client") as mock_get_client:
        result = categorize.categorize_llm(inp)
    mock_get_client.assert_not_called()
    assert result.category is None


def test_llm_parses_a_valid_response():
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response("fuel", 0.95, "petrol receipt")
    inp = CategorizationInput("generic_receipt", "Some Fuel Co", [], "1000", "Petrol 40L")
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize.categorize_llm(inp)
    assert result.category == "fuel"
    assert result.confidence == 0.95
    assert result.method == "llm"
    assert result.estimated_cost_usd > 0


def test_llm_rejects_a_category_not_in_the_fixed_list():
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response("not_a_real_category", 0.9)
    inp = CategorizationInput("generic_receipt", "Vendor", [], "100", "")
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize.categorize_llm(inp)
    assert result.category is None


def test_llm_retries_on_rate_limit_then_succeeds(monkeypatch):
    from groq import RateLimitError

    monkeypatch.setattr(categorize.time, "sleep", lambda *_args, **_kwargs: None)
    rate_limit_response = MagicMock()
    rate_limit_response.headers = {}
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        RateLimitError("rate limited", response=rate_limit_response, body=None),
        _fake_llm_response("other", 0.5),
    ]
    inp = CategorizationInput("generic_receipt", "Vendor", [], "100", "")
    with patch.object(categorize, "_get_client", return_value=client):
        result = categorize.categorize_llm(inp)
    assert result.category == "other"
    assert client.chat.completions.create.call_count == 2


# ------------------------------------------------------------- classifier

def test_classifier_raises_clearly_when_model_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(categorize, "_classifier_bundle", None)
    monkeypatch.setattr(categorize, "MODEL_DIR", tmp_path / "does_not_exist")
    inp = CategorizationInput("generic_receipt", "Vendor", [], "100", "some text")
    with pytest.raises(categorize.CategorizerUnavailable):
        categorize.categorize_classifier(inp)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "models" / "categorizer" / "classifier.joblib").exists(),
    reason="classifier not trained yet -- run scripts/train_categorizer.py",
)
def test_classifier_returns_a_real_category():
    inp = CategorizationInput("generic_receipt", "Highway Fuel Stop", [], "1000", "Petrol 40 litres, cash payment")
    result = categorize.categorize_classifier(inp)
    assert result.category in CATEGORY_IDS
    assert 0.0 <= result.confidence <= 1.0
    assert result.method == "classifier"


# ----------------------------------------------------------------- hybrid

def test_hybrid_uses_classifier_when_confident(monkeypatch):
    confident_result = categorize.CategorizationResult("fuel", 0.9, "confident", "classifier", 5)
    monkeypatch.setattr(categorize, "categorize_classifier", lambda inp: confident_result)
    llm_spy = MagicMock()
    monkeypatch.setattr(categorize, "categorize_llm", llm_spy)
    inp = CategorizationInput("generic_receipt", "Vendor", [], "100", "")
    result = categorize.categorize_hybrid(inp)
    assert result.category == "fuel"
    assert result.method == "hybrid"
    llm_spy.assert_not_called()


def test_hybrid_falls_back_to_llm_when_classifier_unsure(monkeypatch):
    unsure_result = categorize.CategorizationResult("other", 0.1, "unsure", "classifier", 5)
    llm_result = categorize.CategorizationResult("fuel", 0.9, "confident llm call", "llm", 200, 0.001)
    monkeypatch.setattr(categorize, "categorize_classifier", lambda inp: unsure_result)
    monkeypatch.setattr(categorize, "categorize_llm", lambda inp: llm_result)
    inp = CategorizationInput("generic_receipt", "Vendor", [], "100", "")
    result = categorize.categorize_hybrid(inp)
    assert result.category == "fuel"
    assert result.method == "hybrid"
    assert result.estimated_cost_usd == 0.001


def test_hybrid_approval_correspondence_skips_everything(monkeypatch):
    classifier_spy = MagicMock()
    llm_spy = MagicMock()
    monkeypatch.setattr(categorize, "categorize_classifier", classifier_spy)
    monkeypatch.setattr(categorize, "categorize_llm", llm_spy)
    inp = CategorizationInput("approval_correspondence", None, [], None, "")
    result = categorize.categorize_hybrid(inp)
    assert result.category is None
    classifier_spy.assert_not_called()
    llm_spy.assert_not_called()


# --------------------------------------------------------------- dispatch

def test_dispatch_unknown_method_raises():
    inp = CategorizationInput("generic_receipt", None, [], None, "")
    with pytest.raises(ValueError):
        run_categorizer(inp, "not_a_real_method")


def test_build_input_extracts_expected_fields():
    fields = {
        "document_type": "restaurant_bill",
        "vendor_name": "Spice Route",
        "grand_total": "1500.00",
        "line_items": [{"name": "Butter Chicken"}, {"name": "Naan"}],
    }
    inp = categorize.build_input(fields, "Some markdown " * 500)
    assert inp.document_type == "restaurant_bill"
    assert inp.vendor_name == "Spice Route"
    assert inp.amount == "1500.00"
    assert inp.line_item_names == ["Butter Chicken", "Naan"]
    assert len(inp.markdown_excerpt) == categorize.MARKDOWN_EXCERPT_CHARS
