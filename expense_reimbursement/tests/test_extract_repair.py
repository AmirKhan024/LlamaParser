"""Unit tests for extract.py's self-repair retry (no network, no DB --
the Groq client is mocked). Repro this is built around: "May-26 Local
conveyance.pdf" sometimes extracts total_kms=5200 (should be 981, the
form's own trip kms sum) and total_conveyance_amount=null (should be
5200) -- a field swap. After the first extraction, if any arithmetic
check fails, extract_claim_with_repair makes ONE more Groq call with
the failed checks' names/details and keeps the result only if it
passes strictly more arithmetic checks than the first.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract

CONVEYANCE_MARKDOWN = (
    "Local Conveyance Form\n"
    "Trip 1: 400 km\n"
    "Trip 2: 581 km\n"
    "Total km: 981\n"
    "Conveyance: 5200\n"
    "Daily allowance: 1560\n"
    "Vehicle maintenance: 600\n"
    "Mobile allowance: 750\n"
    "Total claimed: 8110\n"
)

_TRAVEL_ENTRIES = [
    {"date": "26 May 2026", "place": "A", "purpose": "meeting", "client": "x", "kms": "400"},
    {"date": "26 May 2026", "place": "B", "purpose": "meeting", "client": "y", "kms": "581"},
]

# The real repro: total_kms and total_conveyance_amount swapped/missing.
BROKEN_FIELDS = {
    "document_type": "local_conveyance_form",
    "travel_entries": _TRAVEL_ENTRIES,
    "total_kms": "5200",
    "total_conveyance_amount": None,
    "total_claimed": "8110",
}

FIXED_FIELDS = dict(
    BROKEN_FIELDS,
    total_kms="981",
    total_conveyance_amount="5200",
    daily_allowance_amount="1560",
    vehicle_maintenance_amount="600",
    mobile_allowance_amount="750",
)


def _fake_response(fields: dict, prompt_tokens=100, completion_tokens=50) -> SimpleNamespace:
    message = SimpleNamespace(content=json.dumps(fields))
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def _mock_client(*field_dicts):
    client = MagicMock()
    client.chat.completions.create.side_effect = [_fake_response(f) for f in field_dicts]
    return client


def test_no_repair_when_first_extraction_passes_every_check(monkeypatch):
    monkeypatch.setattr(extract, "SELF_REPAIR_ENABLED", True)
    client = _mock_client(FIXED_FIELDS)
    with patch.object(extract, "_get_client", return_value=client):
        outcome = extract.extract_claim_with_repair(CONVEYANCE_MARKDOWN, {"pages": []})

    assert outcome.repair_attempted is False
    assert outcome.repair_accepted is False
    assert outcome.repair_result is None
    assert client.chat.completions.create.call_count == 1
    assert outcome.result.raw_fields["total_kms"] == "981"


def test_repair_attempted_and_accepted_when_it_fixes_the_swap(monkeypatch):
    monkeypatch.setattr(extract, "SELF_REPAIR_ENABLED", True)
    client = _mock_client(BROKEN_FIELDS, FIXED_FIELDS)
    with patch.object(extract, "_get_client", return_value=client):
        outcome = extract.extract_claim_with_repair(CONVEYANCE_MARKDOWN, {"pages": []})

    assert outcome.repair_attempted is True
    assert outcome.repair_accepted is True
    assert outcome.result is outcome.repair_result
    assert outcome.result.raw_fields["total_kms"] == "981"
    assert outcome.result.raw_fields["total_conveyance_amount"] == "5200"
    assert client.chat.completions.create.call_count == 2

    # the retry's own user message names the failed checks by name
    second_call_content = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
    assert "sum(travel_entries.kms) == total_kms" in second_call_content
    assert "YOUR FIRST EXTRACTION" in second_call_content


def test_repair_rejected_when_it_does_not_improve(monkeypatch):
    monkeypatch.setattr(extract, "SELF_REPAIR_ENABLED", True)
    # second attempt is no better -- still both checks failing
    client = _mock_client(BROKEN_FIELDS, BROKEN_FIELDS)
    with patch.object(extract, "_get_client", return_value=client):
        outcome = extract.extract_claim_with_repair(CONVEYANCE_MARKDOWN, {"pages": []})

    assert outcome.repair_attempted is True
    assert outcome.repair_accepted is False
    assert outcome.result is outcome.first_result
    assert outcome.result.raw_fields["total_kms"] == "5200"
    assert client.chat.completions.create.call_count == 2, "the repair call still happens even though it's discarded"


def test_repair_never_retried_more_than_once(monkeypatch):
    """Even a repair result that's WORSE than the first (fewer passing
    checks) must not trigger a second repair attempt."""
    monkeypatch.setattr(extract, "SELF_REPAIR_ENABLED", True)
    worse_fields = dict(BROKEN_FIELDS, total_kms=None, total_conveyance_amount=None)
    client = _mock_client(BROKEN_FIELDS, worse_fields)
    with patch.object(extract, "_get_client", return_value=client):
        outcome = extract.extract_claim_with_repair(CONVEYANCE_MARKDOWN, {"pages": []})

    assert outcome.repair_attempted is True
    assert outcome.repair_accepted is False
    assert outcome.result is outcome.first_result
    assert client.chat.completions.create.call_count == 2


def test_self_repair_can_be_disabled(monkeypatch):
    monkeypatch.setattr(extract, "SELF_REPAIR_ENABLED", False)
    client = _mock_client(BROKEN_FIELDS)
    with patch.object(extract, "_get_client", return_value=client):
        outcome = extract.extract_claim_with_repair(CONVEYANCE_MARKDOWN, {"pages": []})

    assert outcome.repair_attempted is False
    assert client.chat.completions.create.call_count == 1
    assert outcome.result.raw_fields["total_kms"] == "5200", "the (broken) first extraction is kept as-is"


def test_both_attempts_token_counts_are_available():
    client = _mock_client(BROKEN_FIELDS, FIXED_FIELDS)
    with patch.object(extract, "_get_client", return_value=client):
        outcome = extract.extract_claim_with_repair(CONVEYANCE_MARKDOWN, {"pages": []})

    assert outcome.first_result.total_tokens == 150
    assert outcome.repair_result.total_tokens == 150


def test_repair_call_failing_outright_falls_back_to_the_first_result(monkeypatch):
    """Real failure mode hit while gathering item 6's reliability
    numbers: Groq's JSON-mode validation can reject the repair call
    outright ("Failed to validate JSON") and raise, since the repair
    prompt asks the model to reproduce a full JSON object a second time
    under more constraints. That's a failure of the OPTIONAL second
    attempt, not of the (already valid) first one -- it must not crash
    the whole extraction."""
    monkeypatch.setattr(extract, "SELF_REPAIR_ENABLED", True)
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _fake_response(BROKEN_FIELDS),
        RuntimeError("Failed to validate JSON. Please adjust your prompt."),
    ]
    with patch.object(extract, "_get_client", return_value=client):
        outcome = extract.extract_claim_with_repair(CONVEYANCE_MARKDOWN, {"pages": []})

    assert outcome.repair_attempted is True
    assert outcome.repair_accepted is False
    assert outcome.repair_result is None
    assert outcome.result is outcome.first_result
    assert outcome.result.raw_fields["total_kms"] == "5200"
