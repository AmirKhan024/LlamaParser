"""Regression test: found while extracting the 30 SROIE receipts for the
Stage 2 eval set -- 5 clean, well-formed receipts (see SUMMARY.md's
Stage 2 section for the full list) failed extraction outright with
Groq's own JSON-mode validator rejecting the generation ("Failed to
validate JSON" / "Failed to generate JSON"), and extract_claim had no
retry at all for this. _call_groq now retries once, same input, on a
BadRequestError whose error code is json_validate_failed or
json_generate_failed -- any other 400 still propagates immediately.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract
from groq import BadRequestError


def _fake_response(fields: dict) -> SimpleNamespace:
    message = SimpleNamespace(content=json.dumps(fields))
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    return SimpleNamespace(choices=[choice], usage=usage)


def _json_validate_failed_error(code: str = "json_validate_failed") -> BadRequestError:
    return BadRequestError(
        "Failed to validate JSON. Please adjust your prompt.",
        response=MagicMock(),
        body={"error": {"message": "Failed to validate JSON.", "type": "invalid_request_error", "code": code, "failed_generation": ""}},
    )


def test_retries_once_on_json_validate_failed_then_succeeds():
    fields = {"document_type": "generic_receipt", "vendor_name": "Bar Wang Rice", "amount": "8.20"}
    client = MagicMock()
    client.chat.completions.create.side_effect = [_json_validate_failed_error(), _fake_response(fields)]
    with patch.object(extract, "_get_client", return_value=client):
        result = extract.extract_claim("markdown", {})
    assert result.raw_fields["vendor_name"] == "Bar Wang Rice"
    assert client.chat.completions.create.call_count == 2


def test_retries_once_on_json_generate_failed_then_succeeds():
    fields = {"document_type": "restaurant_bill", "vendor_name": "Tony Roma's", "amount": "269.40"}
    client = MagicMock()
    client.chat.completions.create.side_effect = [_json_validate_failed_error("json_generate_failed"), _fake_response(fields)]
    with patch.object(extract, "_get_client", return_value=client):
        result = extract.extract_claim("markdown", {})
    assert result.raw_fields["vendor_name"] == "Tony Roma's"


def test_never_retried_more_than_once():
    client = MagicMock()
    client.chat.completions.create.side_effect = [_json_validate_failed_error(), _json_validate_failed_error()]
    with patch.object(extract, "_get_client", return_value=client):
        try:
            extract.extract_claim("markdown", {})
            assert False, "expected BadRequestError to propagate after one failed retry"
        except BadRequestError:
            pass
    assert client.chat.completions.create.call_count == 2


def test_unrelated_400_error_is_not_retried():
    client = MagicMock()
    other_error = BadRequestError(
        "Some other bad request", response=MagicMock(),
        body={"error": {"message": "Some other bad request", "code": "something_else"}},
    )
    client.chat.completions.create.side_effect = [other_error, _fake_response({"document_type": "generic_receipt"})]
    with patch.object(extract, "_get_client", return_value=client):
        try:
            extract.extract_claim("markdown", {})
            assert False, "expected the unrelated BadRequestError to propagate immediately"
        except BadRequestError:
            pass
    assert client.chat.completions.create.call_count == 1
