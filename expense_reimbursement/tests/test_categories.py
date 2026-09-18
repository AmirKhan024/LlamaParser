"""categories.py <-> policy/expense_policy.md consistency: every category
must cite a clause that actually exists in the policy, and every top-level
clause number the policy defines must be referenced by at least one
category (so a policy section never silently has no category mapped to
it, and no category cites a clause number that was renamed/removed).
"""

import re
from pathlib import Path

import pytest

from categories import CATEGORIES, CATEGORY_IDS, DOCUMENT_TYPE_DEFAULT_CATEGORY

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "expense_policy.md"

# Top-level clause headings look like "## 7. Fuel" -- capture the number.
_CLAUSE_HEADING = re.compile(r"^## (\d+)\. ", re.MULTILINE)


def _policy_clause_numbers() -> set[str]:
    text = POLICY_PATH.read_text(encoding="utf-8")
    return set(_CLAUSE_HEADING.findall(text))


def test_every_category_has_at_least_one_policy_clause():
    for category in CATEGORIES.values():
        assert category.policy_clauses, f"{category.id} has no policy_clauses"


def test_every_category_clause_exists_in_policy():
    clause_numbers = _policy_clause_numbers()
    for category in CATEGORIES.values():
        for clause in category.policy_clauses:
            assert clause in clause_numbers, (
                f"{category.id} cites policy clause {clause}, which doesn't exist "
                f"as a top-level '## {clause}. ...' heading in expense_policy.md"
            )


# Clauses 1-2 (scope/definitions) and 19-24 (not-reimbursable, receipts,
# submission, currency, approval, compliance) are cross-cutting rules that
# apply across every category rather than belonging to one -- only the
# per-expense-type clauses (3-18) are expected to have a category.
_ADMINISTRATIVE_CLAUSES = {"1", "2", "19", "20", "21", "22", "23", "24"}


def test_every_category_specific_policy_clause_is_referenced_by_some_category():
    clause_numbers = _policy_clause_numbers() - _ADMINISTRATIVE_CLAUSES
    referenced = {clause for category in CATEGORIES.values() for clause in category.policy_clauses}
    missing = clause_numbers - referenced
    assert not missing, f"policy clauses with no category mapped to them: {sorted(missing, key=int)}"


def test_categories_have_required_shape():
    for category_id, category in CATEGORIES.items():
        assert category.id == category_id
        assert category.label
        assert category.definition
        assert len(category.include_examples) >= 3, f"{category_id} needs 3+ include examples"
        assert len(category.exclude_examples) >= 3, f"{category_id} needs 3+ exclude examples"
        for example, other_category in category.exclude_examples:
            assert example
            assert other_category


def test_category_ids_match_dict_keys():
    assert CATEGORY_IDS == list(CATEGORIES.keys())


def test_document_type_defaults_only_reference_real_categories_or_none():
    for doc_type, category_id in DOCUMENT_TYPE_DEFAULT_CATEGORY.items():
        assert doc_type
        if category_id is not None:
            assert category_id in CATEGORIES, f"{doc_type} maps to unknown category {category_id}"


EXPECTED_CATEGORY_IDS = {
    "intercity_travel", "local_transport", "own_vehicle_mileage", "fuel",
    "accommodation", "travel_meals", "client_entertainment", "team_events",
    "phone_internet", "software_subscriptions", "office_supplies_equipment",
    "training_conferences", "travel_documents_fees", "other",
}


def test_all_14_categories_present():
    assert set(CATEGORY_IDS) == EXPECTED_CATEGORY_IDS
