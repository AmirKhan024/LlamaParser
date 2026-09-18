"""Item 3: warnings fewer and grouped (no network/DB -- build_review_view
called directly with hand-built inputs).

- If all arithmetic checks pass, completeness warnings are hidden from
  the employee entirely (check_completeness itself still runs; only
  what reaches this view narrows).
- A completeness sentence whose number a suggestion already explains is
  dropped, so the same root problem is never said twice.
- A check-driven warning (e.g. "the trip distances don't add up") is
  dropped when a suggestion covers the same field.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review_view import build_review_view


def _local_conveyance_claim(**overrides):
    base = {
        "document_type": "local_conveyance_form",
        "currency": "INR",
        "travel_entries": [{"date": "1", "place": "A", "purpose": "x", "client": "y", "kms": "400"}],
        "total_kms": "400",
        "total_conveyance_amount": "1000",
        "daily_allowance_amount": None,
        "vehicle_maintenance_amount": None,
        "mobile_allowance_amount": None,
        "total_claimed": "1000",
        "confidence": 1.0,
        "validation": [],
        "completeness_warnings": [],
        "suggestions": [],
    }
    base.update(overrides)
    return base


def test_completeness_warnings_hidden_when_all_arithmetic_checks_pass():
    claim = _local_conveyance_claim(
        validation=[
            {"name": "sum(travel_entries.kms) == total_kms", "passed": True},
            {"name": "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", "passed": True},
        ],
        completeness_warnings=["Possible dropped value '5886' near label 'Daily Allowance' -- not found in clean_json or additional_fields"],
    )
    view = build_review_view(claim)
    assert view["warnings"] == []
    assert view["needs_review"] is False


def test_completeness_warnings_shown_when_an_arithmetic_check_fails():
    claim = _local_conveyance_claim(
        validation=[
            {"name": "sum(travel_entries.kms) == total_kms", "passed": False},
            {"name": "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", "passed": True},
        ],
        completeness_warnings=["Possible dropped value '5886' near label 'Daily Allowance' -- not found in clean_json or additional_fields"],
    )
    view = build_review_view(claim)
    assert any("5886" in w for w in view["warnings"])
    assert view["needs_review"] is True


def test_completeness_sentence_dropped_when_a_suggestion_explains_the_same_number():
    claim = _local_conveyance_claim(
        validation=[
            {"name": "sum(travel_entries.kms) == total_kms", "passed": False},
            {"name": "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", "passed": True},
        ],
        completeness_warnings=["Possible dropped value '400' near label 'Total km' -- not found in clean_json or additional_fields"],
        suggestions=[{"field": "total_kms", "current_value": "999", "suggested_value": "400", "reason": "..."}],
    )
    view = build_review_view(claim)
    assert not any("400" in w for w in view["warnings"]), "the completeness sentence about 400 is redundant with the suggestion"
    assert "Total km 400" in view["suggestion_sentence"]


def test_unrelated_completeness_sentence_survives_alongside_a_suggestion():
    claim = _local_conveyance_claim(
        validation=[
            {"name": "sum(travel_entries.kms) == total_kms", "passed": False},
            {"name": "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", "passed": True},
        ],
        completeness_warnings=["Possible dropped value '9999' near label 'Parking' -- not found in clean_json or additional_fields"],
        suggestions=[{"field": "total_kms", "current_value": "999", "suggested_value": "400", "reason": "..."}],
    )
    view = build_review_view(claim)
    assert any("9999" in w for w in view["warnings"]), "an unrelated completeness sentence must not be dropped"


def test_check_driven_warning_dropped_when_a_suggestion_covers_the_same_field():
    claim = _local_conveyance_claim(
        validation=[
            {"name": "sum(travel_entries.kms) == total_kms", "passed": False},
            {"name": "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", "passed": True},
        ],
        suggestions=[{"field": "total_kms", "current_value": "999", "suggested_value": "400", "reason": "..."}],
    )
    view = build_review_view(claim)
    assert not any("trip distances" in w for w in view["warnings"])


def test_check_driven_warning_survives_without_a_suggestion():
    claim = _local_conveyance_claim(
        validation=[
            {"name": "sum(travel_entries.kms) == total_kms", "passed": False},
            {"name": "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", "passed": True},
        ],
        suggestions=[],
    )
    view = build_review_view(claim)
    assert any("trip distances" in w for w in view["warnings"])
