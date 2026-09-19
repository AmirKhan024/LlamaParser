"""Stage 3 clause selection: ask the model which clause governs each unit of a
document and what it read off the document, then VALIDATE everything it said
before any of it is used.

The model's answer is treated as untrusted input. Nothing here computes a
verdict (policy_check.py does) and nothing here coerces a bad answer into a
good one: an unknown clause id, an amount that does not reconcile with the
document's extracted fields, a path that does not exist, or a line item left
uncovered is a HARD FAILURE, returned as a `UnitFailure` (which the evaluation
service turns into an insufficient_information decision with the reason
logged) -- never repaired.

The prompt lives in prompts/policy_select_<version>.md so a prompt change is a
new file and a new `prompt_version`, not an edit to application code.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import policy as pol
import policy_check as pc
import policy_llm
from validate import parse_amount

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
_MAX_STR = 160


# ------------------------------------------------------------------- prompt

def prompt_path(version: str) -> Path:
    return PROMPTS_DIR / f"policy_select_{version}.md"


def load_prompt(version: str) -> tuple[str, str]:
    """(text, sha256) of the versioned prompt file. A missing file is an
    error, never a silent fallback to a different prompt."""
    path = prompt_path(version)
    if not path.exists():
        raise FileNotFoundError(f"no prompt file for prompt_version {version!r}: {path}")
    text = path.read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def call_model(messages: list[dict], *, model: str, scope: str, cache_dir: Optional[Path] = None, use_cache: bool = True) -> policy_llm.LLMCall:
    """The single seam through which evaluation talks to the model (tests
    replace it). Temperature 0, JSON mode, cached by request hash."""
    return policy_llm.call_json(messages, model=model, scope=scope, cache_dir=cache_dir, use_cache=use_cache)


# -------------------------------------------------------------- field paths

def flatten_fields(value: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Every scalar leaf of the extraction as {"path", "value"}, e.g.
    {"path": "line_items[0].total", "value": "30985.35"}. These are the ONLY
    paths the model may reference, and resolve_path reads the same structure,
    so a reference the model makes can always be checked against the source."""
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.extend(flatten_fields(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(flatten_fields(v, f"{prefix}[{i}]"))
    elif value is not None:
        text = value if not isinstance(value, str) else (value if len(value) <= _MAX_STR else value[:_MAX_STR] + "...")
        out.append({"path": prefix, "value": text})
    return out


_MISSING = object()


def resolve_path(fields: Any, path: str) -> Any:
    """The value at `path` in `fields`, or the sentinel _MISSING."""
    node = fields
    for m in _PATH_TOKEN.finditer(path or ""):
        key, index = m.group(1), m.group(2)
        try:
            if index is not None:
                node = node[int(index)]
            else:
                node = node[key]
        except (KeyError, IndexError, TypeError):
            return _MISSING
    return node if path else _MISSING


def _to_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        parsed, _warning = parse_amount(value)
        return parsed
    return None


# ------------------------------------------------------------------ request

def candidate_view(clause: pol.Clause) -> dict[str, Any]:
    """A clause as the model sees it: verbatim text, what its limit is per and
    what it varies by, its conditions -- and NOT the numeric limits, approval
    thresholds or numeric-condition values. The model is not told the
    numbers so it cannot pre-judge a comparison the program owns."""
    varies_by = sorted({k for e in clause.limit_table for k in e.when})
    qualifiers = {
        k: sorted({v for e in clause.limit_table for v in e.when.get(k, ())})
        for k in varies_by if k not in pol.CODE_DIMENSIONS
    }

    def cond_view(c: dict) -> dict:
        computed = c.get("check") == "numeric" or (c.get("any_of") and any(s.get("check") == "numeric" for s in c["any_of"]))
        out: dict[str, Any] = {"id": c["id"], "text": c["text"], "kind": c["kind"]}
        if c.get("any_of"):
            out["alternatives"] = [
                {"id": s["id"], "text": s["text"], "answered_by": "program" if s.get("check") == "numeric" else "you"}
                for s in c["any_of"]
            ]
            out["answered_by"] = "you (for the alternatives marked you)"
        else:
            out["answered_by"] = "program" if computed else "you"
        return out

    return {
        "clause_id": clause.clause_id,
        "text": clause.verbatim_text,
        "limit": {
            "unit": clause.limit_unit,
            "kind": clause.limit_kind,
            "varies_by": varies_by,
            "qualifier_values": qualifiers,
        } if clause.limit_table else None,
        "conditions": [cond_view(c) for c in clause.conditions],
        "documentation_required": [d["item"] for d in clause.documentation_required],
        "requires_approval": bool(clause.requires_approval_above),
        "is_prohibition": clause.is_prohibition,
    }


def sibling_summary(document, extraction, category_id: Optional[str]) -> dict[str, Any]:
    f = extraction.fields or {}
    out: dict[str, Any] = {
        "document_type": extraction.document_type,
        "original_name": document.original_name,
        "category": category_id,
        "amount": str(extraction.amount) if extraction.amount is not None else None,
        "currency": extraction.currency,
        "date": f.get("date"),
    }
    for key in ("approval_status", "subject", "sender", "related_form_title"):
        if f.get(key):
            out[key] = f[key]
    return out


def build_user_payload(*, employee, claim, document, extraction, category_id: str, category_label: str,
                       siblings: list[dict], candidates: list[pol.Clause]) -> dict[str, Any]:
    fields = extraction.fields or {}
    line_items = [
        {"index": i, **{k: v for k, v in li.items() if v is not None}}
        for i, li in enumerate(fields.get("line_items") or []) if isinstance(li, dict)
    ]
    return {
        "employee": {"grade": employee.grade, "base_city": employee.base_city},
        "claim": {"title": claim.title, "note_to_approver": claim.note_to_approver},
        "document": {
            "document_type": extraction.document_type,
            "category": {"id": category_id, "label": category_label},
            "currency": extraction.currency,
            "date": fields.get("date"),
            "vendor_name": fields.get("vendor_name"),
        },
        "fields": flatten_fields(fields),
        "line_items": line_items,
        "other_documents": siblings,
        "candidate_clauses": [candidate_view(c) for c in candidates],
    }


def build_messages(prompt_text: str, user_payload: dict) -> list[dict]:
    return [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
    ]


# --------------------------------------------------------------- validation

class SelectionError(ValueError):
    """The response as a whole is unusable (not JSON, no units, line items not
    covered exactly once) -- the document gets ONE insufficient_information
    decision naming this reason."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class UnitFailure:
    unit_index: int
    line_item_refs: list[int]
    clause_id: Optional[str]
    code: str
    reason: str
    raw_unit: dict


@dataclass
class ValidatedUnit:
    unit_index: int
    line_item_refs: list[int]
    clause: pol.Clause
    clause_confidence: Optional[float]
    base_amount: Optional[Decimal]
    amount_derivation: str            # the model's stated reason + how code summed it
    base_of_percent: Optional[Decimal]
    nights: Optional[int]
    days: Optional[int]
    persons: Optional[int]
    distance_km: Optional[Decimal]
    dims: dict[str, Optional[str]]
    judgments: dict[str, Optional[bool]]
    documentation: dict[str, Optional[bool]]
    approval_evidenced: Optional[bool]
    model_missing: list[str]
    explanation: str
    confidence: Optional[float]
    notes: list[str] = field(default_factory=list)   # things the validator saw and did NOT silently fix
    raw_unit: dict = field(default_factory=dict)


def _fail(idx: int, refs: list[int], clause_id: Optional[str], code: str, reason: str, raw: dict) -> UnitFailure:
    return UnitFailure(idx, refs, clause_id, code, reason, raw)


def _sum_refs(refs: Any, fields: Any, what: str) -> tuple[Optional[Decimal], Optional[str], list[str]]:
    """(sum, error, parts). `error` is set when any ref is malformed, points
    at nothing, or points at a non-number -- the caller turns it into a hard
    failure."""
    if not isinstance(refs, list):
        return None, f"{what} must be a list of {{path, sign}}", []
    total = Decimal(0)
    parts: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            return None, f"{what} entry {ref!r} is not {{path, sign}}", []
        sign = ref.get("sign", 1)
        if sign not in (1, -1):
            return None, f"{what} sign must be 1 or -1, got {sign!r}", []
        value = resolve_path(fields, ref["path"])
        if value is _MISSING:
            return None, f"{what} path {ref['path']!r} does not exist in the extracted fields", []
        number = _to_decimal(value)
        if number is None:
            return None, f"{what} path {ref['path']!r} holds {value!r}, which is not an amount", []
        total += sign * number
        parts.append(f"{'+' if sign == 1 else '-'} {number} ({ref['path']})")
    return total, None, parts


def _quantity_paths_exist(spec: Any, fields: Any, key: str) -> Optional[str]:
    """Error text if a quantity spec points at a path that doesn't exist."""
    if not isinstance(spec, dict):
        return f"quantities.{key} must be an object"
    for k, v in spec.items():
        if k.endswith("_path") or k == "path":
            if not isinstance(v, str) or resolve_path(fields, v) is _MISSING:
                return f"quantities.{key}.{k} {v!r} does not exist in the extracted fields"
    return None


def _condition_ids(clause: pol.Clause) -> dict[str, dict]:
    """id -> condition for every condition and any_of alternative the MODEL
    answers (numeric ones are the program's and are excluded)."""
    out: dict[str, dict] = {}
    for c in clause.conditions:
        if c.get("any_of"):
            for sub in c["any_of"]:
                if sub.get("check") != "numeric":
                    out[sub["id"]] = sub
        elif c.get("check") != "numeric":
            out[c["id"]] = c
    return out


def _tri(value: Any) -> tuple[Optional[bool], bool]:
    """(value, ok): the model's answer as True/False/None; ok=False when it
    said something that is not true/false/null (treated as unknown)."""
    if isinstance(value, dict):
        value = value.get("met", value.get("present"))
    if value is None or isinstance(value, bool):
        return value, True
    return None, False


def parse_response(raw_text: str) -> list[dict]:
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SelectionError("invalid_json", f"the model's response is not valid JSON ({exc})")
    units = data.get("units") if isinstance(data, dict) else None
    if not isinstance(units, list) or not units or not all(isinstance(u, dict) for u in units):
        raise SelectionError("no_units", 'the model\'s response has no non-empty "units" list')
    return units


def validate_selection(
    units: list[dict],
    *,
    fields: dict,
    n_line_items: int,
    candidates: list[pol.Clause],
    policy_clause_ids: set[str],
    reference: dict,
    currency: Optional[str],
    has_approval_evidence: bool,
) -> list[ValidatedUnit | UnitFailure]:
    """Strictly validate the model's units. Raises SelectionError if the units
    don't partition the document's line items; otherwise returns one
    ValidatedUnit or UnitFailure per unit, in order."""
    candidate_by_id = {c.clause_id: c for c in candidates}

    # --- the units must partition the line items (or be one whole-document unit)
    covered: list[int] = []
    for u in units:
        refs = u.get("line_item_refs")
        if not isinstance(refs, list) or any(isinstance(r, bool) or not isinstance(r, int) for r in refs):
            raise SelectionError("bad_line_item_refs", f"line_item_refs must be a list of integers, got {refs!r}")
        covered.extend(refs)
    if n_line_items == 0:
        if len(units) != 1 or covered:
            raise SelectionError("bad_partition", "the document has no line items, so exactly one unit with line_item_refs [] was expected")
    else:
        if sorted(covered) != list(range(n_line_items)):
            raise SelectionError(
                "bad_partition",
                f"the document has {n_line_items} line items (0..{n_line_items - 1}); the units cover {sorted(covered)} -- every line item must be covered exactly once",
            )
        if any(not u["line_item_refs"] for u in units):
            raise SelectionError("bad_partition", "a unit with no line items was returned for a document that has line items")

    results: list[ValidatedUnit | UnitFailure] = []
    for idx, u in enumerate(units):
        refs = list(u["line_item_refs"])
        clause_id = u.get("clause_id")

        # --- the clause must be one the policy actually has
        if clause_id is None:
            results.append(_fail(idx, refs, None, "no_clause", f"the model found no governing clause: {u.get('explanation') or 'no reason given'}", u))
            continue
        if not isinstance(clause_id, str) or clause_id not in policy_clause_ids:
            results.append(_fail(idx, refs, str(clause_id), "invalid_clause_id", f"clause id {clause_id!r} does not exist in the policy", u))
            continue
        if clause_id not in candidate_by_id:
            results.append(_fail(idx, refs, clause_id, "not_a_candidate", f"clause {clause_id} exists in the policy but is not a machine-checkable candidate for this category (informational, unstructured, or another category's clause); no other clause is substituted", u))
            continue
        clause = candidate_by_id[clause_id]
        notes: list[str] = []

        # --- the amount must be re-derivable from the extracted fields
        amount_refs = u.get("amount_refs") or []
        stated = u.get("stated_amount")
        base: Optional[Decimal] = None
        derivation = (u.get("amount_reason") or "").strip()
        if amount_refs:
            total, error, parts = _sum_refs(amount_refs, fields, "amount_refs")
            if error:
                results.append(_fail(idx, refs, clause_id, "amount_ref_invalid", error, u))
                continue
            stated_dec = _to_decimal(stated)
            if stated_dec is None:
                results.append(_fail(idx, refs, clause_id, "amount_mismatch", f"stated_amount {stated!r} is not a number, so it cannot be reconciled with the fields", u))
                continue
            if pc.money(total) != pc.money(stated_dec):
                results.append(_fail(
                    idx, refs, clause_id, "amount_mismatch",
                    f"the model stated {stated_dec} but its amount_refs sum to {total} ({' '.join(parts)}); the amount does not reconcile with the extracted fields",
                    u,
                ))
                continue
            base = pc.money(total)
            derivation = f"{derivation} [code: {' '.join(parts)} = {base}]".strip()
        elif stated not in (None, "", 0, "0"):
            results.append(_fail(idx, refs, clause_id, "amount_without_refs", f"the model stated an amount ({stated!r}) but gave no amount_refs to derive it from", u))
            continue

        base_of_percent: Optional[Decimal] = None
        if u.get("percent_base_refs"):
            total, error, parts = _sum_refs(u["percent_base_refs"], fields, "percent_base_refs")
            if error:
                results.append(_fail(idx, refs, clause_id, "amount_ref_invalid", error, u))
                continue
            base_of_percent = pc.money(total)
            derivation += f" [percent base: {' '.join(parts)} = {base_of_percent}]"

        # --- quantities: every path must exist; unparseable/absent -> None (missing)
        quantities = u.get("quantities") or {}
        if not isinstance(quantities, dict):
            results.append(_fail(idx, refs, clause_id, "bad_quantities", "quantities must be an object", u))
            continue
        bad = None
        for key, spec in quantities.items():
            if key in ("nights", "days", "persons", "distance_km"):
                bad = _quantity_paths_exist(spec, fields, key)
                if bad:
                    break
        if bad:
            results.append(_fail(idx, refs, clause_id, "quantity_path_not_found", bad, u))
            continue

        def qty(key: str, *, date_pair: tuple[str, str], counter) -> Optional[int]:
            spec = quantities.get(key)
            if not spec:
                return None
            if spec.get("count_path"):
                return pc.parse_count(resolve_path(fields, spec["count_path"]))
            a, b = date_pair
            if spec.get(a) and spec.get(b):
                return counter(resolve_path(fields, spec[a]), resolve_path(fields, spec[b]))
            return None

        nights = qty("nights", date_pair=("check_in_path", "check_out_path"), counter=pc.nights_between)
        days = qty("days", date_pair=("start_path", "end_path"), counter=pc.inclusive_days)
        persons = None
        if quantities.get("persons"):
            persons = pc.parse_count(resolve_path(fields, quantities["persons"].get("count_path", "")))
        distance: Optional[Decimal] = None
        if quantities.get("distance_km"):
            dist = _to_decimal(resolve_path(fields, quantities["distance_km"].get("path", "")))
            distance = dist if dist is not None and dist > 0 else None
        for key, value in (("nights", nights), ("days", days), ("persons", persons), ("distance_km", distance)):
            if quantities.get(key) and value is None:
                notes.append(f"{key}: the referenced value could not be read as a valid quantity")

        # --- dimensions: the model names a city/country; CODE maps it to a tier/zone
        model_dims = u.get("dimensions") or {}
        dims: dict[str, Optional[str]] = {}
        city, country = model_dims.get("city"), model_dims.get("country")
        dims["city_tier"] = pol.city_tier(reference, city) if (currency == "INR" and isinstance(city, str)) else None
        dims["zone"] = pol.zone_of(reference, country) if isinstance(country, str) else None
        allowed_by_key: dict[str, set[str]] = {}
        for e in clause.limit_table:
            for k, vs in e.when.items():
                allowed_by_key.setdefault(k, set()).update(v.lower() for v in vs)
        vehicle = model_dims.get("vehicle_type")
        if vehicle is not None:
            if "vehicle_type" in allowed_by_key and str(vehicle).lower() not in allowed_by_key["vehicle_type"]:
                results.append(_fail(idx, refs, clause_id, "bad_dimension_value", f"vehicle_type {vehicle!r} is not one of {sorted(allowed_by_key['vehicle_type'])}", u))
                continue
            dims["vehicle_type"] = str(vehicle).lower()
        else:
            dims["vehicle_type"] = None
        qualifier_error = None
        for key, allowed in allowed_by_key.items():
            if key in pol.CODE_DIMENSIONS:
                continue
            value = model_dims.get(key)
            if value is None:
                dims[key] = None
            elif str(value).lower() not in allowed:
                qualifier_error = f"{key} {value!r} is not one of {sorted(allowed)}"
                break
            else:
                dims[key] = str(value).lower()
        if qualifier_error:
            results.append(_fail(idx, refs, clause_id, "bad_dimension_value", qualifier_error, u))
            continue

        # --- conditions / documentation / approval: the model's judgments, tri-state
        answerable = _condition_ids(clause)
        judgments: dict[str, Optional[bool]] = {}
        model_conditions = u.get("conditions") or {}
        for cid in answerable:
            value, ok = _tri(model_conditions.get(cid))
            judgments[cid] = value
            if not ok:
                notes.append(f"condition {cid}: answer {model_conditions.get(cid)!r} is not true/false/null; treated as unknown")
        ignored = sorted(set(model_conditions) - set(answerable))
        if ignored:
            notes.append(f"ignored answers for conditions the model is not asked (or that don't exist): {ignored}")

        documentation: dict[str, Optional[bool]] = {}
        model_docs = u.get("documentation") or {}
        for item in clause.documentation_required:
            value, ok = _tri(model_docs.get(item["item"]))
            documentation[item["item"]] = value
            if not ok:
                notes.append(f"documentation {item['item']}: answer {model_docs.get(item['item'])!r} is not true/false/null; treated as unknown")

        approval = u.get("approval_evidenced")
        if approval is not None and not isinstance(approval, bool):
            notes.append(f"approval_evidenced {approval!r} is not true/false/null; treated as unknown")
            approval = None
        if approval is True and not has_approval_evidence:
            notes.append("the model reported an approval, but the claim has no approval document or note; treated as NOT evidenced")
            approval = False

        confidence = u.get("confidence")
        clause_confidence = u.get("clause_confidence")
        results.append(ValidatedUnit(
            unit_index=idx, line_item_refs=refs, clause=clause,
            clause_confidence=clause_confidence if isinstance(clause_confidence, (int, float)) else None,
            base_amount=base, amount_derivation=derivation, base_of_percent=base_of_percent,
            nights=nights, days=days, persons=persons, distance_km=distance,
            dims=dims, judgments=judgments, documentation=documentation, approval_evidenced=approval,
            model_missing=[str(m) for m in (u.get("missing_fields") or []) if isinstance(m, (str, int))],
            explanation=str(u.get("explanation") or "").strip(),
            confidence=float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
            notes=notes, raw_unit=u,
        ))
    return results
