"""Unit test for extract.py's prompt-building (no network, no DB).

Bug 2: only document types with their own entry in SCHEMA_BY_TYPE were
described with their schema's fields in the prompt. A type that falls
back to GenericClaim (which HAS line_items) -- hotel_invoice,
taxi_receipt, fuel_receipt, unstructured_proof -- was never told that
field exists, so line items never got extracted for them even though
the schema they're validated against supports it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extract import _build_system_prompt
from schemas import DocumentType, schema_for


def _field_list_lines(prompt: str) -> dict[str, str]:
    lines_by_type = {}
    for line in prompt.splitlines():
        line = line.strip()
        if line.startswith("- ") and ":" in line:
            type_name, _, fields = line[2:].partition(":")
            lines_by_type[type_name.strip()] = fields
    return lines_by_type


def test_prompt_describes_every_document_type_exactly_once():
    prompt = _build_system_prompt()
    for doc_type in DocumentType:
        count = prompt.count(f"- {doc_type.value}:")
        assert count == 1, f"{doc_type.value} should appear exactly once in the prompt, found {count}"


def test_prompt_mentions_line_items_for_every_type_whose_schema_has_it():
    prompt = _build_system_prompt()
    lines_by_type = _field_list_lines(prompt)

    fallback_types_with_line_items = []
    for doc_type in DocumentType:
        schema_cls = schema_for(doc_type)
        line = lines_by_type.get(doc_type.value)
        assert line is not None, f"{doc_type.value} missing from the prompt's per-type field list"
        if "line_items" in schema_cls.model_fields:
            assert "line_items" in line, (
                f"{doc_type.value} (schema {schema_cls.__name__}) has line_items "
                "but the prompt's field list for it doesn't mention line_items"
            )
            fallback_types_with_line_items.append(doc_type.value)
        else:
            assert "line_items" not in line, (
                f"{doc_type.value} (schema {schema_cls.__name__}) has no line_items "
                "but the prompt's field list for it mentions line_items anyway"
            )

    # The exact regression this bug was about: these fall back to
    # GenericClaim (never their own SCHEMA_BY_TYPE entry) but still have
    # line_items via that fallback.
    for doc_type in ("hotel_invoice", "taxi_receipt", "fuel_receipt", "unstructured_proof"):
        assert doc_type in fallback_types_with_line_items, (
            f"{doc_type} falls back to GenericClaim (has line_items) but wasn't confirmed present above"
        )
