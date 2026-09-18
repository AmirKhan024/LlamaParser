"""Employee-facing view of an extracted claim: only the fields an
employee needs to see, in plain language, each marked editable or not.
Confidence scores, document_type codes, GSTIN, account/invoice numbers,
additional_fields, extraction_notes and raw check formulas never reach
this output -- they're either dropped entirely or translated into a
plain warning sentence.

`editable_fields` on the returned view is the source of truth the
server enforces PUT /documents/{id}/fields against: an edit to any
field whose root key isn't in that list is rejected.
"""

import re
from typing import Any, Dict, List, Optional

_APPROVAL_STATUS_LABELS = {
    "approved": "Approved",
    "pending": "Approval requested",
    "requested": "Approval requested",
    "awaiting approval": "Approval requested",
    "rejected": "Not approved",
    "denied": "Not approved",
}

_LOW_CONFIDENCE_WARNING = "Parts of this document were hard to read. Check the values against the document."

_COMPLETENESS_PATTERN = re.compile(r"Possible dropped value '([^']*)' near label '([^']*)'")


def _humanize_approval_status(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _APPROVAL_STATUS_LABELS.get(value.strip().lower(), value)


def _field(key: str, label: str, value: Any, editable: bool) -> Dict[str, Any]:
    return {"key": key, "label": label, "value": value, "editable": editable}


_CURRENCY_OPTIONS = ["INR", "USD", "EUR", "GBP"]


def _currency_select_field(value: Optional[str]) -> Dict[str, Any]:
    return {
        "key": "currency",
        "label": "Currency",
        "value": value,
        "editable": True,
        "type": "select",
        "options": _CURRENCY_OPTIONS,
    }


def _checks_by_name(validation: List[Dict[str, Any]]) -> Dict[str, bool]:
    return {c["name"]: c.get("passed", True) for c in validation}


def _completeness_sentences(completeness_warnings: List[str]) -> List[str]:
    sentences = []
    for warning in completeness_warnings:
        match = _COMPLETENESS_PATTERN.search(warning)
        if match:
            value, label = match.group(1), match.group(2)
            sentences.append(
                f"The bill shows {value} next to {label} but it isn't in your claim. "
                "Check it, or explain in the note to approver."
            )
        else:
            sentences.append(warning)
    return sentences


def _build_telecom_bill(claim: Dict[str, Any], checks: Dict[str, bool]) -> Dict[str, Any]:
    check_ok = checks.get("subtotal + tax == total", True)
    fields = [
        _field("vendor_name", "Provider", claim.get("vendor_name"), True),
        _field("date", "Bill date", claim.get("date"), True),
        _field("billing_period", "Billing period", claim.get("billing_period"), True),
        _field("total", "Amount", claim.get("total"), True),
    ]
    if not check_ok:
        fields.append(_field("subtotal", "Subtotal", claim.get("subtotal"), True))
        fields.append(_field("tax", "Tax", claim.get("tax"), True))
    warnings = []
    if not check_ok:
        warnings.append("The amounts don't add up: subtotal plus tax should equal the total.")
    return {"fields": fields, "collapsible": None, "warnings": warnings, "needs_confirm": True}


def _build_restaurant_bill(claim: Dict[str, Any], checks: Dict[str, bool]) -> Dict[str, Any]:
    check_ok = checks.get("subtotal + cgst + sgst == grand_total", True)
    fields = [
        _field("vendor_name", "Restaurant", claim.get("vendor_name"), True),
        _field("date", "Date", claim.get("date"), True),
        _field("grand_total", "Amount", claim.get("grand_total"), True),
    ]
    line_items = claim.get("line_items") or []
    collapsible = {
        "key": "items",
        "label": f"Show items ({len(line_items)})",
        "auto_expand": not check_ok,
        "line_items": line_items,
        "extra_fields": [
            _field("subtotal", "Subtotal", claim.get("subtotal"), True),
            _field("cgst", "CGST", claim.get("cgst"), True),
            _field("sgst", "SGST", claim.get("sgst"), True),
        ],
        "editable": True,
    }
    warnings = []
    if not check_ok:
        warnings.append("The amounts don't add up: subtotal plus CGST and SGST should equal the total.")
    return {"fields": fields, "collapsible": collapsible, "warnings": warnings, "needs_confirm": True}


_TRIP_ENTRY_KEYS = ("date", "place", "purpose", "client", "kms")


def _build_local_conveyance_form(claim: Dict[str, Any], checks: Dict[str, bool]) -> Dict[str, Any]:
    kms_ok = checks.get("sum(travel_entries.kms) == total_kms", True)
    total_ok = checks.get(
        "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", True
    )
    trips = [
        {k: entry.get(k) for k in _TRIP_ENTRY_KEYS}
        for entry in (claim.get("travel_entries") or [])
        if isinstance(entry, dict)
    ]
    fields = [
        _field("travel_entries", "Trips", trips, True),
        _field("total_kms", "Total km", claim.get("total_kms"), True),
        _field("total_conveyance_amount", "Conveyance", claim.get("total_conveyance_amount"), True),
        _field("daily_allowance_amount", "Daily allowance", claim.get("daily_allowance_amount"), True),
        _field("vehicle_maintenance_amount", "Vehicle maintenance", claim.get("vehicle_maintenance_amount"), True),
        _field("mobile_allowance_amount", "Mobile allowance", claim.get("mobile_allowance_amount"), True),
        _field("total_claimed", "Total claimed", claim.get("total_claimed"), True),
    ]
    warnings = []
    if not kms_ok:
        warnings.append("The trip distances don't add up to the total km claimed. Check the trips table.")
    if not total_ok:
        warnings.append(
            "The amounts don't add up: conveyance, daily allowance, vehicle maintenance and mobile "
            "allowance should sum to the total claimed."
        )
    return {"fields": fields, "collapsible": None, "warnings": warnings, "needs_confirm": True}


def _build_approval_correspondence(claim: Dict[str, Any], checks: Dict[str, bool]) -> Dict[str, Any]:
    fields = [
        _field("sender", "From", claim.get("sender"), False),
        _field("sent_date", "Date", claim.get("sent_date"), False),
        _field("approval_status", "Status", _humanize_approval_status(claim.get("approval_status")), False),
    ]
    return {"fields": fields, "collapsible": None, "warnings": [], "needs_confirm": False}


def _build_generic_receipt(claim: Dict[str, Any], checks: Dict[str, bool]) -> Dict[str, Any]:
    fields = [
        _field("vendor_name", "Vendor", claim.get("vendor_name"), True),
        _field("date", "Date", claim.get("date"), True),
        _field("amount", "Amount", claim.get("amount"), True),
    ]
    line_items = claim.get("line_items") or []
    collapsible = {
        "key": "items",
        "label": f"Show items ({len(line_items)})",
        "auto_expand": False,
        "line_items": line_items,
        "extra_fields": [],
        "editable": True,
    }
    return {"fields": fields, "collapsible": collapsible, "warnings": [], "needs_confirm": True}


_BUILDERS = {
    "telecom_bill": _build_telecom_bill,
    "restaurant_bill": _build_restaurant_bill,
    "local_conveyance_form": _build_local_conveyance_form,
    "approval_correspondence": _build_approval_correspondence,
}

# Check names that don't correspond to anything the employee can see or
# fix (a tax-ID field is never shown -- see NEVER-show list in SUMMARY.md)
# -- excluded from driving needs_review so a document doesn't get stuck
# in "check this" over something the employee has no way to act on.
_NON_ACTIONABLE_CHECK_PREFIXES = ("gstin_format:",)


def build_review_view(claim: Dict[str, Any]) -> Dict[str, Any]:
    doc_type = claim.get("document_type")
    checks = _checks_by_name(claim.get("validation", []))
    completeness_warnings = claim.get("completeness_warnings") or []
    confidence = claim.get("confidence", 1.0)

    builder = _BUILDERS.get(doc_type, _build_generic_receipt)
    built = builder(claim, checks)

    fields = list(built["fields"])
    collapsible = built.get("collapsible")
    needs_confirm = built.get("needs_confirm", True)
    currency = claim.get("currency")

    # Currency is only ever a distinct, editable field when it's genuinely
    # ambiguous (build_claim couldn't determine it) -- otherwise it's
    # conveyed by how amounts are formatted (fmtMoney on the client),
    # not a separate row. Doesn't apply to read-only types (approval
    # correspondence has no amount to be ambiguous about).
    if currency is None and needs_confirm:
        fields.append(_currency_select_field(currency))

    editable_fields = [f["key"] for f in fields if f["editable"]]
    if collapsible and collapsible.get("editable"):
        editable_fields.append(collapsible["key"] if collapsible["key"] != "items" else "line_items")
        editable_fields.extend(f["key"] for f in collapsible.get("extra_fields", []) if f["editable"])

    warnings = list(built.get("warnings", []))
    warnings.extend(_completeness_sentences(completeness_warnings))

    actionable_checks_failed = any(
        not passed for name, passed in checks.items()
        if not any(name.startswith(p) for p in _NON_ACTIONABLE_CHECK_PREFIXES)
    )
    low_confidence = needs_confirm and confidence < 0.85
    if low_confidence:
        warnings.append(_LOW_CONFIDENCE_WARNING)

    needs_review = actionable_checks_failed or bool(completeness_warnings) or low_confidence

    return {
        "document_type": doc_type,
        "currency": currency,
        "fields": fields,
        "collapsible": collapsible,
        "warnings": warnings,
        "editable_fields": editable_fields,
        "needs_confirm": needs_confirm,
        "needs_review": needs_review,
    }
