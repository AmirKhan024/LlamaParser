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
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

# Fields a suggestion (see suggest_fixes, and _suggestion_sentence below)
# can ever target that are money, not a count -- total_kms is the one
# suggested field that's a distance, not an amount, so it's deliberately
# left out (no currency symbol on a km figure).
_MONEY_SUGGESTION_FIELDS = {"total_conveyance_amount", "total", "tax", "grand_total", "amount"}
_SUGGESTION_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}

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


def _fmt_suggestion_number(value: str, is_money: bool, currency: Optional[str]) -> str:
    try:
        num = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    text = f"{int(num):,}" if num == num.to_integral_value() else f"{num:,.2f}"
    if not is_money:
        return text
    return f"{_SUGGESTION_CURRENCY_SYMBOLS.get(currency, '')}{text}"


def _suggestion_sentence(suggestions: List[Dict[str, Any]], label_by_key: Dict[str, str], currency: Optional[str]) -> str:
    """"The numbers on this form don't add up as we read them. Did you
    mean: Total km 981, Conveyance ₹5,200?" -- one sentence covering
    every current suggestion, phrased with the same field labels the
    review screen itself uses (label_by_key, built from this document
    type's own `fields` list) so it never repeats a raw schema key."""
    if not suggestions:
        return ""
    parts = [
        f"{label_by_key.get(s['field'], s['field'])} "
        f"{_fmt_suggestion_number(s['suggested_value'], s['field'] in _MONEY_SUGGESTION_FIELDS, currency)}"
        for s in suggestions
    ]
    return "The numbers on this form don't add up as we read them. Did you mean: " + ", ".join(parts) + "?"


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


def _build_telecom_bill(claim: Dict[str, Any], checks: Dict[str, bool], suggested_fields: set) -> Dict[str, Any]:
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
    # A suggestion already covers this exact failure (with a fix
    # attached) -- showing this sentence too would be the same root
    # problem said twice.
    if not check_ok and not ({"total", "tax"} & suggested_fields):
        warnings.append("The amounts don't add up: subtotal plus tax should equal the total.")
    return {"fields": fields, "collapsible": None, "warnings": warnings, "needs_confirm": True}


def _build_restaurant_bill(claim: Dict[str, Any], checks: Dict[str, bool], suggested_fields: set) -> Dict[str, Any]:
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
    if not check_ok and "grand_total" not in suggested_fields:
        warnings.append("The amounts don't add up: subtotal plus CGST and SGST should equal the total.")
    return {"fields": fields, "collapsible": collapsible, "warnings": warnings, "needs_confirm": True}


# travel_entries is `list[dict]` with no Pydantic-enforced key names
# (schemas.py's comment says "date, place, purpose, client, kms", but
# nothing makes the model actually use those -- real extractions come
# back with e.g. "place_of_visit"/"purpose_of_travel"/"client_name"
# instead, which silently rendered as permanently blank Place/Purpose/
# Client columns until this alias table existed). Not fixed by renaming
# fields in the extraction prompt (out of scope here) -- normalized on
# display instead, consistently reused by server.py's corrections diff
# so an edit's field_path lines up with what's actually in the DB.
TRIP_FIELD_ALIASES = {
    "date": ("date",),
    "place": ("place", "place_of_visit", "location"),
    "purpose": ("purpose", "purpose_of_travel", "reason"),
    "client": ("client", "client_name", "customer"),
    "kms": ("kms", "km", "distance"),
}


def normalize_trip_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {}
    for canonical, aliases in TRIP_FIELD_ALIASES.items():
        normalized[canonical] = next((entry[a] for a in aliases if a in entry), None)
    return normalized


def _build_local_conveyance_form(claim: Dict[str, Any], checks: Dict[str, bool], suggested_fields: set) -> Dict[str, Any]:
    kms_ok = checks.get("sum(travel_entries.kms) == total_kms", True)
    total_ok = checks.get(
        "conveyance_amount + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed", True
    )
    trips = [
        normalize_trip_entry(entry)
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
    if not kms_ok and "total_kms" not in suggested_fields:
        warnings.append("The trip distances don't add up to the total km claimed. Check the trips table.")
    if not total_ok and "total_conveyance_amount" not in suggested_fields:
        warnings.append(
            "The amounts don't add up: conveyance, daily allowance, vehicle maintenance and mobile "
            "allowance should sum to the total claimed."
        )
    return {
        "fields": fields, "collapsible": None, "warnings": warnings, "needs_confirm": True,
        # item 4: highlights the Km column header and the Total km field
        # on the client when the trips themselves are the problem.
        "trips_check_failed": not kms_ok,
    }


def _build_approval_correspondence(claim: Dict[str, Any], checks: Dict[str, bool], suggested_fields: set) -> Dict[str, Any]:
    fields = [
        _field("sender", "From", claim.get("sender"), False),
        _field("sent_date", "Date", claim.get("sent_date"), False),
        _field("approval_status", "Status", _humanize_approval_status(claim.get("approval_status")), False),
    ]
    return {"fields": fields, "collapsible": None, "warnings": [], "needs_confirm": False}


def _build_generic_receipt(claim: Dict[str, Any], checks: Dict[str, bool], suggested_fields: set) -> Dict[str, Any]:
    fields = [
        _field("vendor_name", "Vendor", claim.get("vendor_name"), True),
        _field("date", "Date", claim.get("date"), True),
        _field("amount", "Amount", claim.get("amount"), True),
    ]
    # _validate_generic_claim only ever runs these when it actually found
    # the fields to check (a simple receipt with no subtotal/tax
    # breakdown at all has neither check in `checks`) -- checks.get's
    # True default only matters for a check that DID run and passed.
    subtotal_tax_ok = checks.get("subtotal + tax == amount", True)
    items_sum_ok = checks.get("sum(line items) == subtotal or amount", True)

    line_items = claim.get("line_items") or []
    collapsible = {
        "key": "items",
        "label": f"Show items ({len(line_items)})",
        "auto_expand": not items_sum_ok,
        "line_items": line_items,
        "extra_fields": [],
        "editable": True,
    }
    warnings = []
    if not subtotal_tax_ok and not ({"amount", "tax"} & suggested_fields):
        warnings.append("The amounts don't add up: subtotal plus tax should equal the total.")
    if not items_sum_ok:
        # suggest_fixes has no rule for a line-items-vs-total mismatch --
        # nothing to dedupe against here.
        warnings.append("The amounts don't add up: the items don't add up to the subtotal or total.")
    return {"fields": fields, "collapsible": collapsible, "warnings": warnings, "needs_confirm": True}


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
    suggestions = claim.get("suggestions") or []
    suggested_fields = {s["field"] for s in suggestions}

    builder = _BUILDERS.get(doc_type, _build_generic_receipt)
    built = builder(claim, checks, suggested_fields)

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

    actionable_checks_failed = any(
        not passed for name, passed in checks.items()
        if not any(name.startswith(p) for p in _NON_ACTIONABLE_CHECK_PREFIXES)
    )

    warnings = list(built.get("warnings", []))
    # A document quirk that doesn't affect any number (a dropped value,
    # an unclear currency) is only worth surfacing when there's still an
    # arithmetic reason to look at this document at all -- otherwise
    # it's noise with nothing actionable attached. check_completeness
    # itself is unchanged (still computed and available for a later
    # approver-facing stage); only what reaches the EMPLOYEE view here
    # narrows.
    if actionable_checks_failed:
        completeness_sentences = _completeness_sentences(completeness_warnings)
        suggested_numbers = {str(s["suggested_value"]) for s in suggestions}
        # Never say the same root problem twice: if a suggestion already
        # explains (and offers to fix) the exact number a completeness
        # sentence is about, drop that sentence.
        completeness_sentences = [
            sentence for sentence in completeness_sentences
            if not any(number in sentence for number in suggested_numbers)
        ]
        warnings.extend(completeness_sentences)

    low_confidence = needs_confirm and confidence < 0.85
    if low_confidence:
        warnings.append(_LOW_CONFIDENCE_WARNING)

    needs_review = actionable_checks_failed or low_confidence

    label_by_key = {f["key"]: f["label"] for f in fields}
    suggestion_sentence = _suggestion_sentence(suggestions, label_by_key, currency)

    return {
        "document_type": doc_type,
        "currency": currency,
        "fields": fields,
        "collapsible": collapsible,
        "warnings": warnings,
        "editable_fields": editable_fields,
        "needs_confirm": needs_confirm,
        "needs_review": needs_review,
        "suggestions": suggestions,
        "suggestion_sentence": suggestion_sentence,
        "trips_check_failed": built.get("trips_check_failed", False),
    }
