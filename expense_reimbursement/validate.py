"""Step 3 -- parse amount strings into Decimal, then run the arithmetic
checks appropriate to each document type. All checks run in plain code,
never asked of the LLM -- the model's job stopped at Step 2.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple, get_args

from schemas import BaseClaim, DocumentType, GenericClaim, LocalConveyanceForm, RestaurantBill, TelecomBill, schema_for

_CURRENCY_WORDS = re.compile(r"Rs\.?|INR|₹|\$", re.IGNORECASE)
TOLERANCE = Decimal("0.5")

GSTIN_PATTERN = re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')

# "gstin"/"gst_no" substring match on the field name is deliberately
# loose -- it's what lets this catch a tax-ID field on ANY document
# type, including ones with no dedicated schema (where it would only
# ever show up in additional_fields), not just the types tested so far.
_GSTIN_FIELD_HINT = re.compile(r"gstin|gst_no", re.IGNORECASE)

CURRENCY_MAP = {"rs.": "INR", "rs": "INR", "₹": "INR", "inr": "INR", "rupees": "INR"}


def validate_gstin(value: Optional[str]) -> dict:
    """Returns {'valid': bool, 'reason': str | None}. Does not attempt to
    correct a malformed value (e.g. an OCR'd 0/O mix-up) -- there's no
    second source to correct it against here; it only flags the shape
    as wrong so it doesn't silently pass through unnoticed."""
    if not value:
        return {"valid": True, "reason": None}  # absence isn't a format error
    cleaned = value.replace(" ", "").upper()
    if not GSTIN_PATTERN.match(cleaned):
        return {
            "valid": False,
            "reason": (
                f"'{value}' does not match GSTIN format (14 chars: 2 digits, 5 letters, "
                "4 digits, 1 letter, 1 alphanumeric, 'Z', 1 alphanumeric)"
            ),
        }
    return {"valid": True, "reason": None}


def normalize_currency(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return CURRENCY_MAP.get(value.strip().lower(), value)


def parse_amount(raw: Optional[str]) -> Tuple[Optional[Decimal], Optional[str]]:
    """Parse a printed amount string into Decimal.

    Comma-stripping alone gives the correct value under BOTH the Indian
    convention (lakh/crore grouping, e.g. "1,20,100.00") and the
    international one (e.g. "120,100.00") -- commas are purely visual
    separators in both. So this never has to "pick" a convention to
    compute the right number. What it DOES do is sanity-check that the
    comma grouping actually matches one of those two conventions; if it
    matches neither (e.g. "12,3,456"), that's a sign of OCR/extraction
    noise, so it's flagged as a warning rather than silently trusted.

    Returns (value, warning). `value` is None only when the string is
    empty or genuinely unparseable (warning explains why); comma-shape
    ambiguity does not prevent parsing, it only adds a warning.
    """
    if raw is None:
        return None, None
    s = raw.strip()
    if not s:
        return None, None

    s = _CURRENCY_WORDS.sub("", s).strip()

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    elif s.startswith("-"):
        negative = True
        s = s[1:].strip()

    if not s:
        return None, None

    int_part, _, dec_part = s.partition(".")
    if not re.fullmatch(r"[\d,]*", int_part) or not re.fullmatch(r"\d*", dec_part):
        return None, f"could not parse amount {raw!r} (unexpected characters)"

    warning = None
    groups = int_part.split(",") if int_part else [""]
    if len(groups) > 1:
        rest = groups[1:]
        is_international = all(len(g) == 3 for g in rest)
        is_indian = len(rest) >= 1 and len(rest[-1]) == 3 and all(len(g) == 2 for g in rest[:-1])
        if not (is_international or is_indian):
            warning = (
                f"amount {raw!r} has an unusual comma grouping "
                "(matches neither Indian nor international convention) -- verify manually"
            )

    digits = int_part.replace(",", "") or "0"
    try:
        value = Decimal(digits + (f".{dec_part}" if dec_part else ""))
    except InvalidOperation:
        return None, f"could not parse amount {raw!r}"

    return (-value if negative else value), warning


def _decimal_field_names(schema_cls: type[BaseClaim]) -> set[str]:
    """Every field on `schema_cls` typed `Decimal` or `Optional[Decimal]`
    -- found generically from the model definition so this doesn't need
    a hand-maintained list that drifts from schemas.py as fields change.
    """
    names = set()
    for name, field in schema_cls.model_fields.items():
        annotation = field.annotation
        candidates = get_args(annotation) or (annotation,)
        if Decimal in candidates:
            names.add(name)
    return names


def _coerce_notes(value: Any) -> list[str]:
    """`extraction_notes` is typed `list[str]`, but the model sometimes
    returns one long string instead of a JSON array despite the prompt
    asking for a list. That matters here specifically because Python
    (and Pydantic's coercion) will happily treat a plain string as an
    iterable of 1-character strings -- `list("Extracted X")` silently
    becomes `["E", "x", "t", ...]` rather than raising. Any non-list,
    non-empty value is wrapped as a single note instead of being
    iterated character-by-character."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def build_claim(document_type: DocumentType, raw_fields: Dict[str, Any]) -> BaseClaim:
    """Turn the model's raw output (amounts as printed strings, per the
    extraction prompt) into a typed claim: every Decimal-typed field is
    parsed with `parse_amount` first, then the result is validated
    against that document type's schema. A comma-shape warning from
    `parse_amount` is appended to extraction_notes, not silently
    dropped. If validation still fails (e.g. a field with a value that
    genuinely isn't numeric), falls back to GenericClaim rather than
    crashing the run -- additional_fields still preserves everything
    the model reported.
    """
    schema_cls = schema_for(document_type)
    processed = dict(raw_fields)
    notes = _coerce_notes(processed.get("extraction_notes"))

    for field_name in _decimal_field_names(schema_cls):
        value = processed.get(field_name)
        if isinstance(value, str):
            parsed, warning = parse_amount(value)
            processed[field_name] = parsed
            if warning:
                notes.append(warning)
        elif isinstance(value, (int, float)):
            processed[field_name] = Decimal(str(value))

    processed["extraction_notes"] = notes
    if "currency" in processed:
        processed["currency"] = normalize_currency(processed["currency"])

    try:
        return schema_cls.model_validate(processed)
    except Exception as e:
        notes.append(f"failed to validate against {schema_cls.__name__} ({e}); fell back to GenericClaim")
        fallback = {k: v for k, v in processed.items() if k in GenericClaim.model_fields}
        fallback["extraction_notes"] = notes
        return GenericClaim.model_validate(fallback)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def _isclose(a: Decimal, b: Decimal, tolerance: Decimal = TOLERANCE) -> bool:
    return abs(a - b) <= tolerance


def _find_gstin_like_fields(claim: BaseClaim) -> list[Tuple[str, str]]:
    """(field_name, value) for every string field -- top-level or inside
    additional_fields -- whose name contains "gstin" or "gst_no". Checks
    additional_fields too so this works for document types with no
    dedicated schema field for a tax ID, not just RestaurantBill's
    `restaurant_gstin`.
    """
    found = []
    for name, value in claim.model_dump().items():
        if name == "additional_fields":
            continue
        if isinstance(value, str) and _GSTIN_FIELD_HINT.search(name):
            found.append((name, value))
    for name, value in (claim.additional_fields or {}).items():
        if isinstance(value, str) and _GSTIN_FIELD_HINT.search(name):
            found.append((name, value))
    return found


def check_gstin_format(claim: BaseClaim) -> list[CheckResult]:
    """Runs on every document type (not just ones with a dedicated GSTIN
    field) since a tax ID can show up in additional_fields on anything.
    """
    results = []
    for field_name, value in _find_gstin_like_fields(claim):
        outcome = validate_gstin(value)
        results.append(CheckResult(
            f"gstin_format:{field_name}",
            outcome["valid"],
            outcome["reason"] or f"'{value}' matches GSTIN format",
        ))
    return results


def validate_claim(claim: BaseClaim) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if isinstance(claim, TelecomBill):
        checks.extend(_validate_telecom_bill(claim))
    elif isinstance(claim, RestaurantBill):
        checks.extend(_validate_restaurant_bill(claim))
    elif isinstance(claim, LocalConveyanceForm):
        checks.extend(_validate_local_conveyance_form(claim))
    checks.extend(check_gstin_format(claim))
    return checks


def _validate_telecom_bill(claim: TelecomBill) -> list[CheckResult]:
    if claim.subtotal is None or claim.tax is None or claim.total is None:
        return [CheckResult(
            "subtotal + tax == total", False,
            f"cannot check: subtotal={claim.subtotal}, tax={claim.tax}, total={claim.total} (one or more missing)",
        )]
    computed = claim.subtotal + claim.tax
    passed = _isclose(computed, claim.total)
    return [CheckResult(
        "subtotal + tax == total", passed,
        f"{claim.subtotal} + {claim.tax} = {computed}, printed total = {claim.total} "
        f"(diff {abs(computed - claim.total)}, tolerance {TOLERANCE})",
    )]


def _validate_restaurant_bill(claim: RestaurantBill) -> list[CheckResult]:
    if claim.subtotal is None or claim.cgst is None or claim.sgst is None or claim.grand_total is None:
        return [CheckResult(
            "subtotal + cgst + sgst == grand_total", False,
            f"cannot check: subtotal={claim.subtotal}, cgst={claim.cgst}, "
            f"sgst={claim.sgst}, grand_total={claim.grand_total} (one or more missing)",
        )]
    computed = claim.subtotal + claim.cgst + claim.sgst
    passed = _isclose(computed, claim.grand_total)
    return [CheckResult(
        "subtotal + cgst + sgst == grand_total", passed,
        f"{claim.subtotal} + {claim.cgst} + {claim.sgst} = {computed}, printed grand_total = {claim.grand_total} "
        f"(diff {abs(computed - claim.grand_total)}, tolerance {TOLERANCE})",
    )]


def _validate_local_conveyance_form(claim: LocalConveyanceForm) -> list[CheckResult]:
    results = []

    kms_values = []
    for entry in claim.travel_entries:
        value, _ = parse_amount(str(entry.get("kms"))) if entry.get("kms") is not None else (None, None)
        if value is not None:
            kms_values.append(value)
    entries_kms_sum = sum(kms_values, Decimal("0")) if kms_values else None

    if entries_kms_sum is None or not claim.travel_entries:
        results.append(CheckResult(
            "sum(travel_entries.kms) computed", False,
            f"cannot check: {len(claim.travel_entries)} travel_entries, no parseable kms values",
        ))
        return results

    matches_total_kms = claim.total_kms is not None and _isclose(entries_kms_sum, claim.total_kms, Decimal("1"))
    matches_total_amount = (
        claim.total_conveyance_amount is not None
        and _isclose(entries_kms_sum, claim.total_conveyance_amount, Decimal("1"))
    )

    if matches_total_kms and not matches_total_amount:
        results.append(CheckResult(
            "sum(travel_entries.kms) == total_kms", True,
            f"sum of {len(kms_values)} entries = {entries_kms_sum}, printed total_kms = {claim.total_kms}",
        ))
    elif matches_total_amount and not matches_total_kms:
        results.append(CheckResult(
            "sum(travel_entries.kms) == total_kms", False,
            f"SWAP SUSPECTED: sum of entries ({entries_kms_sum}) matches total_conveyance_amount "
            f"({claim.total_conveyance_amount}) instead of total_kms ({claim.total_kms}) -- "
            "total_kms and total_conveyance_amount may have been swapped during extraction",
        ))
    elif matches_total_kms and matches_total_amount:
        results.append(CheckResult(
            "sum(travel_entries.kms) == total_kms", True,
            f"sum of entries ({entries_kms_sum}) matches BOTH total_kms and total_conveyance_amount "
            f"({claim.total_kms} == {claim.total_conveyance_amount}?) -- unusual but not a swap",
        ))
    else:
        results.append(CheckResult(
            "sum(travel_entries.kms) == total_kms", False,
            f"sum of {len(kms_values)} entries = {entries_kms_sum} matches NEITHER "
            f"total_kms ({claim.total_kms}) NOR total_conveyance_amount ({claim.total_conveyance_amount})",
        ))

    results.append(_check_conveyance_total(claim))

    return results


def _check_conveyance_total(claim: LocalConveyanceForm) -> CheckResult:
    """The real balancing identity (verified manually against the source
    PDF), now that vehicle/mobile amounts are named schema fields rather
    than living in additional_fields -- replaces the old loose ">="
    sanity floor this check used before those fields existed."""
    name = (
        "conveyance_amount + daily_allowance + vehicle_maintenance + "
        "mobile_allowance == total_claimed"
    )
    parts = [
        claim.total_conveyance_amount,
        claim.daily_allowance_amount,
        claim.vehicle_maintenance_amount,
        claim.mobile_allowance_amount,
    ]
    present = [p for p in parts if p is not None]
    if not present or claim.total_claimed is None:
        return CheckResult(
            name, False,
            f"cannot check: total_conveyance_amount={claim.total_conveyance_amount}, "
            f"daily_allowance_amount={claim.daily_allowance_amount}, "
            f"vehicle_maintenance_amount={claim.vehicle_maintenance_amount}, "
            f"mobile_allowance_amount={claim.mobile_allowance_amount}, "
            f"total_claimed={claim.total_claimed} (nothing to sum, or total_claimed missing)",
        )
    computed = sum(present, Decimal("0"))
    passed = _isclose(computed, claim.total_claimed)
    return CheckResult(
        name, passed,
        f"{' + '.join(str(p) for p in present)} = {computed}, "
        f"printed total_claimed = {claim.total_claimed} "
        f"(diff {abs(computed - claim.total_claimed)}, tolerance {TOLERANCE})",
    )


def _find_line_with_label(markdown_text: str, label: str) -> Optional[str]:
    """First line containing `label`, matched loosely: case-insensitive
    and whitespace-collapsed, so it survives the OCR/markdown-table
    artifacts that routinely add or drop stray spaces around a label."""
    target = re.sub(r"\s+", "", label).lower()
    for line in markdown_text.splitlines():
        collapsed = re.sub(r"\s+", "", line).lower()
        if target in collapsed:
            return line
    return None


def check_completeness(markdown_text: str, claim: Dict[str, Any], doc_type: str) -> list[str]:
    """Local-conveyance-specific, intentionally crude pattern match: for
    each known totals-section label, does every number sitting on that
    label's markdown line actually show up somewhere in the final
    output? Catches a table row with more than one number in it (a
    current-period figure and a running total, say) where only the
    first got captured -- the exact bug this was written after (a
    "5886" that appeared on the Daily Allowance row and ended up
    nowhere in clean_json or additional_fields).

    Not a general solution -- it doesn't understand table structure, so
    it can both miss things (numbers on a different line than the
    label) and over-flag (an unrelated number that happens to share the
    label's line). Good enough to catch the one class of bug that
    already happened once; a document type without this wired in just
    gets an empty list back.
    """
    warnings: list[str] = []
    if doc_type != DocumentType.LOCAL_CONVEYANCE_FORM.value:
        return warnings

    captured_values = set()
    for key, value in claim.items():
        if key == "additional_fields":
            continue
        if isinstance(value, (int, float, str)):
            captured_values.add(str(value))
    for value in (claim.get("additional_fields") or {}).values():
        captured_values.add(str(value))
    for entry in claim.get("travel_entries") or []:
        if isinstance(entry, dict):
            for value in entry.values():
                captured_values.add(str(value))

    labels = [
        "Total Conveyance Amount", "Daily Allowance", "Vehicle Maintenance",
        "Mobile Allowance", "Total Amount Claimed",
    ]
    for label in labels:
        line = _find_line_with_label(markdown_text, label)
        if not line:
            continue
        numbers_on_line = re.findall(r"[\d,]+\.?\d*", line)
        for num in numbers_on_line:
            stripped = num.replace(",", "")
            if not stripped or not re.search(r"\d", stripped):
                continue
            if stripped not in captured_values and num not in captured_values:
                warnings.append(
                    f"Possible dropped value '{num}' near label '{label}' -- "
                    "not found in clean_json or additional_fields"
                )
    return warnings
