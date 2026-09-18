"""Step 3 -- parse amount strings into Decimal, then run the arithmetic
checks appropriate to each document type. All checks run in plain code,
never asked of the LLM -- the model's job stopped at Step 2.
"""

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple, get_args

from schemas import BaseClaim, DocumentType, GenericClaim, LocalConveyanceForm, RestaurantBill, TelecomBill, schema_for

_CURRENCY_WORDS = re.compile(r"Rs\.?|INR|₹|\$", re.IGNORECASE)

# The currency assumed when a document has no currency marker at all
# (see resolve_currency below) -- read once at import time, same as
# every other env-driven constant in this codebase (e.g. server.py's
# PIPELINE_MODE).
COMPANY_CURRENCY = os.environ.get("COMPANY_CURRENCY", "INR")

# 0.5 was loose enough that a real employee edit (780.75 -> 780.70, a
# $0.05 change) still passed every check -- an absolute 0.5 tolerance is
# right for genuine rupee round-off, but wrong as a default: it also
# hides a $0.05-$0.49 typo or a deliberately padded amount. 0.01 is the
# default; 1.00 is only used when the document itself gives evidence of
# a round-off line (see _has_roundoff_evidence) -- never assumed.
TOLERANCE_DEFAULT = Decimal("0.01")
TOLERANCE_WITH_ROUNDOFF = Decimal("1.00")
TOLERANCE = TOLERANCE_DEFAULT  # kept for anything still importing the old name directly

_ROUNDOFF_PATTERN = re.compile(r"round[\s\-]?off|rounding", re.IGNORECASE)


def _has_roundoff_evidence(markdown_text: str, claim: Optional[BaseClaim] = None) -> bool:
    """True only when the document's own markdown, or a field/
    additional_field the model actually extracted, mentions round-off --
    never inferred from the check itself being off by less than a rupee,
    which would make this circular (any small mismatch "explained" by
    assuming round-off)."""
    if markdown_text and _ROUNDOFF_PATTERN.search(markdown_text):
        return True
    if claim is not None:
        additional = getattr(claim, "additional_fields", None) or {}
        for key in additional:
            if _ROUNDOFF_PATTERN.search(key):
                return True
    return False


def _tolerance_for(markdown_text: str, claim: Optional[BaseClaim] = None) -> Decimal:
    return TOLERANCE_WITH_ROUNDOFF if _has_roundoff_evidence(markdown_text, claim) else TOLERANCE_DEFAULT


def _tolerance_note(tolerance: Decimal) -> str:
    return " (round-off line found on the document)" if tolerance == TOLERANCE_WITH_ROUNDOFF else ""

GSTIN_PATTERN = re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')

# "gstin"/"gst_no" substring match on the field name is deliberately
# loose -- it's what lets this catch a tax-ID field on ANY document
# type, including ones with no dedicated schema (where it would only
# ever show up in additional_fields), not just the types tested so far.
_GSTIN_FIELD_HINT = re.compile(r"gstin|gst_no", re.IGNORECASE)

CURRENCY_MAP = {
    "rs.": "INR", "rs": "INR", "₹": "INR", "inr": "INR", "rupees": "INR",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}

# Used only when the model gave no currency at all (build_claim below) --
# scans the document's own markdown for a symbol/code and infers the
# currency from that instead of silently assuming INR. Order matters
# only in that each pattern maps to exactly one currency; a document
# is ambiguous (currency left as None) if this finds zero or 2+ distinct
# currencies, never guessed at.
_CURRENCY_DETECTION_PATTERNS: list[Tuple[re.Pattern, str]] = [
    (re.compile(r"\$"), "USD"),
    (re.compile(r"\bUSD\b", re.IGNORECASE), "USD"),
    (re.compile(r"€"), "EUR"),
    (re.compile(r"\bEUR\b", re.IGNORECASE), "EUR"),
    (re.compile(r"£"), "GBP"),
    (re.compile(r"\bGBP\b", re.IGNORECASE), "GBP"),
    (re.compile(r"₹"), "INR"),
    (re.compile(r"\bRs\.?\b"), "INR"),
    (re.compile(r"\bINR\b", re.IGNORECASE), "INR"),
]


def _currency_candidates(markdown_text: str) -> set[str]:
    return {code for pattern, code in _CURRENCY_DETECTION_PATTERNS if pattern.search(markdown_text)}


def detect_currency_from_markdown(markdown_text: str) -> Optional[str]:
    """None means "couldn't tell" -- either nothing matched, or more
    than one distinct currency showed up (e.g. an FX conversion note),
    and this deliberately doesn't guess between them. Only used when the
    model gave no currency of its own -- see resolve_currency below for
    what build_claim actually puts on the claim, which treats "nothing
    matched" differently from "several matched"."""
    found = _currency_candidates(markdown_text)
    if len(found) == 1:
        return found.pop()
    return None


def resolve_currency(model_currency: Optional[str], markdown_text: str) -> Optional[str]:
    """The model's own currency wins when it gave one (normalized, e.g.
    a bare "$" becomes "USD"). Otherwise: exactly one currency marker
    in the document's own text -> use it; two or more (e.g. an FX
    conversion note) -> genuinely ambiguous, left None so check_
    completeness warns; none at all -> COMPANY_CURRENCY, not a warning-
    worthy situation -- most bills in the company's own currency never
    print a symbol at all (e.g. a plain INR conveyance form), and
    warning on every one of them was pure noise."""
    if model_currency:
        return normalize_currency(model_currency)
    candidates = _currency_candidates(markdown_text)
    if len(candidates) == 1:
        return candidates.pop()
    if len(candidates) >= 2:
        return None
    return COMPANY_CURRENCY

# Real placeholder values a bill prints when a customer has no GST
# registration -- e.g. "Customer GST No.: -" -- these are legitimately
# absent, not malformed. Without this, "-" fell through to the regex and
# was reported as an invalid GSTIN, which is a false positive (a format
# complaint about a value that was never claiming to be a GSTIN in the
# first place), not a real data quality issue.
_GSTIN_PLACEHOLDER_VALUES = {"-", "n/a", "na", "none", "nil", "null", ""}


def validate_gstin(value: Optional[str]) -> dict:
    """Returns {'valid': bool, 'reason': str | None}. Does not attempt to
    correct a malformed value (e.g. an OCR'd 0/O mix-up) -- there's no
    second source to correct it against here; it only flags the shape
    as wrong so it doesn't silently pass through unnoticed."""
    if not value or value.strip().lower() in _GSTIN_PLACEHOLDER_VALUES:
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


def _preprocess_line_items(raw_items: Any, notes: list) -> Any:
    """`LineItem.unit_price`/`total` are Decimal, but the model is
    instructed to copy every amount exactly as printed (e.g. "24,000"),
    same as every other amount field -- so each item needs the same
    `parse_amount` treatment before validation. Skipping this would let
    a comma-formatted line-item price raise a pydantic decimal_parsing
    error and take the whole document down (verified directly: Pydantic
    rejects "24,000" as an invalid Decimal, it does not strip commas).
    """
    if not isinstance(raw_items, list):
        return raw_items
    processed_items = []
    for item in raw_items:
        if not isinstance(item, dict):
            processed_items.append(item)
            continue
        new_item = dict(item)
        for key in ("unit_price", "total"):
            value = new_item.get(key)
            if isinstance(value, str):
                parsed, warning = parse_amount(value)
                new_item[key] = parsed
                if warning:
                    notes.append(warning)
            elif isinstance(value, (int, float)):
                new_item[key] = Decimal(str(value))
        processed_items.append(new_item)
    return processed_items


def _coerce_additional_fields(value: Any) -> Dict[str, str]:
    """additional_fields is typed dict[str, str], but the model
    occasionally puts a non-string value there (e.g. a nested
    "line_items" list, for a document_type whose schema has no
    line_items field of its own to put it in instead). JSON-encoding
    the value keeps the data instead of crashing validation -- and a
    JSON-stringified list is exactly the shape
    eval_cord.find_extracted_line_items already knows how to recover."""
    if not isinstance(value, dict):
        return {}
    coerced = {}
    for k, v in value.items():
        if v is None:
            continue
        coerced[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return coerced


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


def build_claim(document_type: DocumentType, raw_fields: Dict[str, Any], markdown_text: str = "") -> BaseClaim:
    """Turn the model's raw output (amounts as printed strings, per the
    extraction prompt) into a typed claim: every Decimal-typed field is
    parsed with `parse_amount` first, then the result is validated
    against that document type's schema. A comma-shape warning from
    `parse_amount` is appended to extraction_notes, not silently
    dropped. If validation still fails (e.g. a field with a value that
    genuinely isn't numeric), falls back to GenericClaim rather than
    crashing the run -- additional_fields still preserves everything
    the model reported.

    `markdown_text` (the document's own parsed text) is only consulted
    when the model gave no currency at all -- see resolve_currency: a
    single unambiguous marker in the text wins, conflicting markers
    leave the currency `None` (check_completeness warns on that), and
    no marker at all falls back to COMPANY_CURRENCY with no warning
    (most bills in the company's own currency never print a symbol).
    """
    schema_cls = schema_for(document_type)
    processed = dict(raw_fields)
    notes = _coerce_notes(processed.get("extraction_notes"))
    if "additional_fields" in processed:
        processed["additional_fields"] = _coerce_additional_fields(processed["additional_fields"])

    for field_name in _decimal_field_names(schema_cls):
        value = processed.get(field_name)
        if isinstance(value, str):
            parsed, warning = parse_amount(value)
            processed[field_name] = parsed
            if warning:
                notes.append(warning)
        elif isinstance(value, (int, float)):
            processed[field_name] = Decimal(str(value))

    if "line_items" in processed:
        processed["line_items"] = _preprocess_line_items(processed["line_items"], notes)

    processed["extraction_notes"] = notes
    processed["currency"] = resolve_currency(processed.get("currency"), markdown_text)

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
        is_placeholder = bool(value) and value.strip().lower() in _GSTIN_PLACEHOLDER_VALUES
        detail = outcome["reason"] or (
            f"'{value}' recognized as a placeholder for no GST registration -- not treated as a GSTIN"
            if is_placeholder
            else f"'{value}' matches GSTIN format"
        )
        results.append(CheckResult(f"gstin_format:{field_name}", outcome["valid"], detail))
    return results


def validate_claim(claim: BaseClaim, markdown_text: str = "") -> list[CheckResult]:
    checks: list[CheckResult] = []
    if isinstance(claim, TelecomBill):
        checks.extend(_validate_telecom_bill(claim, markdown_text))
    elif isinstance(claim, RestaurantBill):
        checks.extend(_validate_restaurant_bill(claim, markdown_text))
    elif isinstance(claim, LocalConveyanceForm):
        checks.extend(_validate_local_conveyance_form(claim, markdown_text))
    elif isinstance(claim, GenericClaim):
        # ApprovalCorrespondence is a BaseClaim sibling, not a GenericClaim
        # subclass, so it's naturally excluded here -- an email has no
        # subtotal/tax/line-item totals to check.
        checks.extend(_validate_generic_claim(claim, markdown_text))
    checks.extend(check_gstin_format(claim))
    return checks


def _validate_telecom_bill(claim: TelecomBill, markdown_text: str = "") -> list[CheckResult]:
    if claim.subtotal is None or claim.tax is None or claim.total is None:
        return [CheckResult(
            "subtotal + tax == total", False,
            f"cannot check: subtotal={claim.subtotal}, tax={claim.tax}, total={claim.total} (one or more missing)",
        )]
    tolerance = _tolerance_for(markdown_text, claim)
    computed = claim.subtotal + claim.tax
    passed = _isclose(computed, claim.total, tolerance)
    return [CheckResult(
        "subtotal + tax == total", passed,
        f"{claim.subtotal} + {claim.tax} = {computed}, printed total = {claim.total} "
        f"(diff {abs(computed - claim.total)}, tolerance {tolerance}{_tolerance_note(tolerance)})",
    )]


def _validate_restaurant_bill(claim: RestaurantBill, markdown_text: str = "") -> list[CheckResult]:
    if claim.subtotal is None or claim.cgst is None or claim.sgst is None or claim.grand_total is None:
        return [CheckResult(
            "subtotal + cgst + sgst == grand_total", False,
            f"cannot check: subtotal={claim.subtotal}, cgst={claim.cgst}, "
            f"sgst={claim.sgst}, grand_total={claim.grand_total} (one or more missing)",
        )]
    tolerance = _tolerance_for(markdown_text, claim)
    computed = claim.subtotal + claim.cgst + claim.sgst
    passed = _isclose(computed, claim.grand_total, tolerance)
    return [CheckResult(
        "subtotal + cgst + sgst == grand_total", passed,
        f"{claim.subtotal} + {claim.cgst} + {claim.sgst} = {computed}, printed grand_total = {claim.grand_total} "
        f"(diff {abs(computed - claim.grand_total)}, tolerance {tolerance}{_tolerance_note(tolerance)})",
    )]


def _validate_local_conveyance_form(claim: LocalConveyanceForm, markdown_text: str = "") -> list[CheckResult]:
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

    results.append(_check_conveyance_total(claim, markdown_text))

    return results


def _check_conveyance_total(claim: LocalConveyanceForm, markdown_text: str = "") -> CheckResult:
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
    tolerance = _tolerance_for(markdown_text, claim)
    computed = sum(present, Decimal("0"))
    passed = _isclose(computed, claim.total_claimed, tolerance)
    return CheckResult(
        name, passed,
        f"{' + '.join(str(p) for p in present)} = {computed}, "
        f"printed total_claimed = {claim.total_claimed} "
        f"(diff {abs(computed - claim.total_claimed)}, tolerance {tolerance}{_tolerance_note(tolerance)})",
    )


_SUBTOTAL_FIELD_HINT = re.compile(r"subtotal", re.IGNORECASE)
_TAX_FIELD_HINT = re.compile(r"tax", re.IGNORECASE)
_TOTAL_FIELD_HINT = re.compile(r"total", re.IGNORECASE)


def _find_amount_in_additional_fields(claim: GenericClaim, pattern: re.Pattern) -> Optional[Decimal]:
    """GenericClaim (taxi/hotel/fuel/unstructured/generic receipts) has
    no dedicated subtotal/tax schema fields the way TelecomBill does --
    those values, when present at all, only ever show up in
    additional_fields under whatever label the bill printed. Matched
    case-insensitively on the key, same approach as _find_gstin_like_fields."""
    for key, value in (claim.additional_fields or {}).items():
        if pattern.search(key) and isinstance(value, str):
            parsed, _warning = parse_amount(value)
            if parsed is not None:
                return parsed
    return None


def _validate_generic_claim(claim: GenericClaim, markdown_text: str = "") -> list[CheckResult]:
    """Opportunistic, unlike the other per-type checks: a generic
    receipt may not print a subtotal/tax breakdown at all (a simple taxi
    fare, say), and that's not a data quality problem -- so this returns
    no CheckResult at all for a check whose inputs aren't there, rather
    than a loud "cannot check" failure that would flag every simple
    receipt for review.
    """
    results: list[CheckResult] = []
    tolerance = _tolerance_for(markdown_text, claim)

    subtotal = _find_amount_in_additional_fields(claim, _SUBTOTAL_FIELD_HINT)
    tax = _find_amount_in_additional_fields(claim, _TAX_FIELD_HINT)
    total_field = _find_amount_in_additional_fields(claim, _TOTAL_FIELD_HINT)
    target = claim.amount if claim.amount is not None else total_field

    if subtotal is not None and tax is not None and target is not None:
        computed = subtotal + tax
        passed = _isclose(computed, target, tolerance)
        results.append(CheckResult(
            "subtotal + tax == amount", passed,
            f"{subtotal} + {tax} = {computed}, amount = {target} "
            f"(diff {abs(computed - target)}, tolerance {tolerance}{_tolerance_note(tolerance)})",
        ))

    item_totals = [item.total for item in claim.line_items if item.total is not None]
    if item_totals:
        items_sum = sum(item_totals, Decimal("0"))
        compare_to = subtotal if subtotal is not None else claim.amount
        compare_label = "subtotal" if subtotal is not None else "amount"
        if compare_to is not None:
            passed = _isclose(items_sum, compare_to, tolerance)
            results.append(CheckResult(
                f"sum(line items) == {compare_label}", passed,
                f"sum of {len(item_totals)} item totals = {items_sum}, {compare_label} = {compare_to} "
                f"(diff {abs(items_sum - compare_to)}, tolerance {tolerance}{_tolerance_note(tolerance)})",
            ))

    return results


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


_DATE_LIKE = re.compile(r"\b\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b")


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
    label's line). Two classes of over-flagging are filtered out
    explicitly: a number that's really a component of a date sitting on
    the same line (e.g. "01-06-2026" splits into "01"/"06"/"2026",
    none of which are amounts), and a number that IS captured but under
    different comma grouping than the line prints it in -- comparison
    happens after stripping commas from both sides, not just the
    candidate. Good enough to catch the one class of bug that already
    happened once; a document type without this wired in just gets an
    empty list back.

    Also carries one near-universal, non-local-conveyance-specific
    check: a claim whose currency build_claim couldn't determine (see
    detect_currency_from_markdown) gets a plain-language warning here,
    on every document type that has an amount to be ambiguous about --
    not gated behind the local_conveyance_form check below. Excluded for
    approval_correspondence: it's an email, not a bill, so "which
    currency" is meaningless noise there even though the schema
    technically inherits an (always-empty) currency field from BaseClaim.
    """
    warnings: list[str] = []
    if claim.get("currency") is None and doc_type != DocumentType.APPROVAL_CORRESPONDENCE.value:
        warnings.append("Couldn't tell which currency this bill is in.")

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
    captured_stripped = {v.replace(",", "") for v in captured_values}

    labels = [
        "Total Conveyance Amount", "Daily Allowance", "Vehicle Maintenance",
        "Mobile Allowance", "Total Amount Claimed",
    ]
    for label in labels:
        line = _find_line_with_label(markdown_text, label)
        if not line:
            continue
        date_spans = [m.span() for m in _DATE_LIKE.finditer(line)]
        for match in re.finditer(r"[\d,]+\.?\d*", line):
            if any(match.start() >= start and match.end() <= end for start, end in date_spans):
                continue
            num = match.group()
            stripped = num.replace(",", "")
            if not stripped or not re.search(r"\d", stripped):
                continue
            if stripped not in captured_stripped and num not in captured_values:
                warnings.append(
                    f"Possible dropped value '{num}' near label '{label}' -- "
                    "not found in clean_json or additional_fields"
                )
    return warnings
