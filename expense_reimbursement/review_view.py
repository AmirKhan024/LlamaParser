"""Employee-facing projection of a validated claim.

Pure deterministic projection -- no LLM call. Takes the same combined
dict run.py already builds for the audit report (clean_json fields plus
`validation` and `completeness_warnings`) and returns a short view: only
the fields an employee needs to see, which ones they can edit, and
whether anything needs human attention before approval.

GSTIN and other tax-registration fields are deliberately excluded from
every "show" list -- audit-only, not employee-facing, per the earlier
decision that GST compliance isn't something the employee needs to
confirm.

Add a document_type to REVIEW_CONFIG only once it has its own schema in
schemas.py; anything else already falls through to DEFAULT_CONFIG, so a
brand-new, unanticipated document type never crashes this -- it just
gets the generic four-field view until someone decides it deserves more.
"""

from typing import Any, Dict

REVIEW_CONFIG = {
    "telecom_bill": {
        "show": ["vendor_name", "date", "billing_period", "subtotal", "tax", "total", "currency"],
        "editable": ["date", "total"],
    },
    "local_conveyance_form": {
        "show": ["employee_name", "travel_entries", "total_kms", "total_conveyance_amount",
                 "daily_allowance_amount", "vehicle_maintenance_amount", "mobile_allowance_amount",
                 "total_claimed"],
        "editable": ["travel_entries", "total_claimed"],
    },
    "restaurant_bill": {
        "show": ["vendor_name", "date", "subtotal", "cgst", "sgst", "grand_total",
                 "currency", "line_items"],
        "editable": ["date", "grand_total", "line_items"],
    },
    "approval_correspondence": {
        "show": ["sender", "recipient", "sent_date", "approval_status", "related_form_title"],
        "editable": [],
    },
    "generic_receipt": {
        "show": ["vendor_name", "date", "amount", "currency", "line_items"],
        "editable": ["amount", "line_items"],
    },
}
DEFAULT_CONFIG = {"show": ["vendor_name", "date", "amount", "currency"], "editable": []}


def build_review_view(claim: Dict[str, Any]) -> Dict[str, Any]:
    doc_type = claim.get("document_type")
    cfg = REVIEW_CONFIG.get(doc_type, DEFAULT_CONFIG)

    view = {k: claim.get(k) for k in cfg["show"]}
    view["_document_type"] = doc_type
    view["_editable_fields"] = cfg["editable"]

    failed_checks = [c["name"] for c in claim.get("validation", []) if not c.get("passed", True)]
    view["_flagged_checks"] = failed_checks

    # any AI-made correction (swap fix, etc.) must force human review,
    # regardless of whether the arithmetic check passed afterward
    correction_notes = [n for n in claim.get("extraction_notes", [])
                         if any(kw in n.lower() for kw in ["swap", "corrected", "fixed", "inferred"])]
    view["_ai_corrections"] = correction_notes

    view["_needs_review"] = (
        claim.get("confidence", 1.0) < 0.85
        or bool(failed_checks)
        or bool(correction_notes)
        or bool(claim.get("completeness_warnings"))
    )
    return view
