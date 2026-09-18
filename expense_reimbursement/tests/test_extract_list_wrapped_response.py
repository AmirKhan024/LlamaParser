"""Regression test: found while extracting the 30 SROIE receipts for the
Stage 2 eval set (scripts/extract_sroie_for_eval.py) -- Groq's JSON mode
occasionally wraps the response in a one-element array instead of
returning the object directly, crashing extract_claim with
AttributeError: 'list' object has no attribute 'get'. extract._call_groq
now unwraps a single-element array of dicts; anything else malformed
falls back to an empty dict rather than crashing.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract


def _response_with_raw_json(raw_text: str) -> SimpleNamespace:
    message = SimpleNamespace(content=raw_text)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_list_wrapped_json_object_is_unwrapped():
    fields = {"document_type": "generic_receipt", "vendor_name": "Corner Store", "amount": "60.00"}
    client = MagicMock()
    client.chat.completions.create.return_value = _response_with_raw_json(json.dumps([fields]))
    with patch.object(extract, "_get_client", return_value=client):
        result = extract.extract_claim("markdown", {})
    assert result.document_type.value == "generic_receipt"
    assert result.raw_fields["vendor_name"] == "Corner Store"


def test_empty_list_response_falls_back_to_generic_receipt_not_a_crash():
    client = MagicMock()
    client.chat.completions.create.return_value = _response_with_raw_json(json.dumps([]))
    with patch.object(extract, "_get_client", return_value=client):
        result = extract.extract_claim("markdown", {})
    assert result.document_type.value == "generic_receipt"


def test_list_of_non_dicts_falls_back_to_generic_receipt_not_a_crash():
    client = MagicMock()
    client.chat.completions.create.return_value = _response_with_raw_json(json.dumps(["not", "a", "dict"]))
    with patch.object(extract, "_get_client", return_value=client):
        result = extract.extract_claim("markdown", {})
    assert result.document_type.value == "generic_receipt"
