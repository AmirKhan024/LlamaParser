"""Structure policy/expense_policy.md into machine-readable clauses and write
policy/policy_v1.json (the reviewable seed scripts/seed_policy.py loads into
the DB).

What is code and what is the model:
  * CODE cuts the markdown into clauses (policy.segment_policy) and parses the
    section-2 reference tables. Clause ids and verbatim_text therefore come
    straight from the file -- the model never writes either.
  * The MODEL (Groq, temperature 0, one call per policy section; see --model)
    fills in the structured fields for text code already cut out: unit, limit
    table, conditions, documentation, approval threshold, prohibition flag.
  * CODE then checks the model's answer against the clause text (every number
    must appear in it, every unit is compared with what the words say...) and
    reports everything it can't settle in a CONFLICTS section. Nothing is
    resolved silently.

Nothing is written unless you pass --confirm. Without it the script structures
the policy, prints the full review table and the CONFLICTS report, and exits.
A unit misread ("per trip" as "per day") corrupts every downstream verdict, so
read the table against the policy before confirming.

Usage:
  python scripts/build_policy.py                       # structure + print review, write nothing
  python scripts/build_policy.py --detail 8.1 6.3      # also print those clauses in full
  python scripts/build_policy.py --confirm --reviewed-by "Your Name"
  python scripts/build_policy.py --no-cache            # re-ask the model (default reuses cached answers)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import dotenv_values, load_dotenv

import policy as pol
from categories import CATEGORIES, CATEGORY_IDS

STRUCTURING_PROMPT_VERSION = "structure-v2"
DEFAULT_OUT = BASE_DIR / "policy" / "policy_v1.json"
BUILD_CACHE_DIR = BASE_DIR / "storage" / "policy_cache" / "build"

MODEL_UNITS = pol.LIMIT_UNITS + ("unknown",)

# Words in a clause that name its unit. Used ONLY to cross-check the model's
# answer (a disagreement is reported, never auto-corrected).
UNIT_HINTS: dict[str, re.Pattern] = {
    "per_night": re.compile(r"per night|/\s?night|nightly", re.I),
    "per_day": re.compile(r"per day|/\s?day|per diem|daily|amount per day", re.I),
    "per_ride": re.compile(r"per ride|per-ride", re.I),
    "per_trip": re.compile(r"per trip|/\s?trip|\bper-trip", re.I),
    "per_month": re.compile(r"per month|/\s?month|monthly|calendar month", re.I),
    "per_person": re.compile(r"per person|per-person|per head|per-head|per recipient|per-recipient|per attendee", re.I),
    "per_km": re.compile(r"/\s?km|per km|per-km|per kilomet", re.I),
    "per_item": re.compile(r"per item", re.I),
    "percent_of_amount": re.compile(r"\d\s?%|per ?cent", re.I),
}


SYSTEM_PROMPT = """You convert clauses of a corporate expense policy into a strict JSON structure that a program will apply to expense claims.

You are given one policy section: a list of clauses, each with an id and its exact text. Return ONE JSON object: {"clauses": [ ... ]} with EXACTLY one entry per given clause id (same ids, same order). Do not add, drop, merge or rename clauses. Do not copy the clause text back.

Each entry has these keys (all required; use null / [] where nothing applies):

  "clause_id": the given id.
  "applies_to_categories": array of category ids from the CATEGORY LIST below, or ["*"] if the rule applies to every kind of expense (receipts, prohibitions, submission windows), or [] if the clause is guidance/definition that cannot be checked against a single expense (principles, audit statements, approval routing by claim total).
  "limit_unit": one of per_trip | per_night | per_day | per_month | per_person | per_claim | per_ride | per_item | per_km | per_event | percent_of_amount | none | unknown.
       none = the clause states no numeric limit. unknown = it states a numeric limit but the words do not say what it is per; NEVER guess a unit. per_person also covers "per head" / "per recipient". per_km is a rate per kilometre. percent_of_amount is a cap expressed as a percentage of another amount (a tip as 10% of the bill).
  "unit_evidence": the exact words copied from the clause that state the unit (e.g. "Nightly caps", "per ride", "Monthly cap", "/night"), or null if none. Must be a substring of the clause text.
  "limit_kind": "amount" (money; also use it when there is no limit), "percent" (a % of another amount), or "count" (a number of occurrences, e.g. events per month).
  "limit_inclusive": true when the text says "up to", "cap", "maximum", "no more than", "at most" (the limit itself is allowed); false when it says "under X" (X itself is not allowed).
  "limit_table": array of {"amount": <number, no commas>, "currency": "INR"|"USD"|... or null for percent/count, "when": {...}} -- one entry per distinct limit. Every "amount" MUST be a number that literally appears in the clause text; never compute, convert or invent one. For a grade x city-tier table, emit one entry per cell.
       "when" narrows an entry. Allowed keys: "grade" (list of "L1".."L6"; expand "L1 - L2" to ["L1","L2"], "L5 and above" to ["L5","L6"]), "city_tier" (list of "1","2","3"), "zone" (list of "A","B"), "vehicle_type" (list of "two_wheeler","four_wheeler"), or a qualifier key you name in snake_case ONLY when the clause distinguishes limits by something else (e.g. "occasion": ["routine"] vs ["celebration"]). Use {} when an entry always applies. Domestic (INR) limits and international (USD) limits are separate entries distinguished by currency, not by a key.
  "conditions": array of conditions that decide whether THIS expense is acceptable under the clause, beyond the amount comparison, the documentation list and the approval threshold (do not restate those). Each:
       {"id": "c1", "text": "...", "kind": "requires"|"excludes", "check": "judgment"|"numeric", "depends_on": [...], "numeric": {...}|null}
       kind "requires": must be true for the expense to be acceptable. kind "excludes": if true, the expense is not acceptable.
       check "numeric" when the condition is a comparison of a quantity: numeric = {"quantity": "nights"|"days"|"amount"|"unit_amount"|"persons"|"distance_km"|"grade_rank"|"days_since_expense", "op": ">"|">="|"<"|"<="|"==", "value": <number that appears in the clause>}. Use grade_rank for "L5 or above" (L5 -> value 5, op ">="). Everything else is check "judgment" with numeric null.
       "depends_on": one or more names from the FIELD LIST below naming what information the condition needs (for a numeric condition, use the field that supplies the quantity).
       A condition that offers alternatives ("A or B") is one condition; put the alternatives in "text" -- unless one alternative is a numeric comparison, then use "any_of" (see below).
  "documentation_required": array of {"item": short_snake_case_name, "min_amount": <number or null>} -- proof the clause demands (itemised_receipt, gst_invoice, trip_log, ...). min_amount is the amount at or above which it is demanded when the clause sets such a threshold, else null.
  "requires_approval_above": null if the clause has no approval requirement; otherwise {"<currency>": <amount>} -- the amount ABOVE which approval is needed. Use 0 when approval is required whatever the amount.
  "is_prohibition": true if the clause says something is never / not reimbursable or prohibited. A prohibition MUST have at least one "excludes" condition stating the factual trigger (e.g. "the expense is a traffic or parking fine"), even when the clause is unconditional.
  "notes": one short sentence on anything the structure cannot capture (an alternative the employee chooses, a scope the system cannot determine, a cross-reference), or null.

A threshold is not a limit:
- "limit_table" holds ONLY amounts the expense may not exceed. A threshold above which approval, proof or a further step is required is NOT a limit: "pre-approval above Rs 15,000" -> requires_approval_above {"INR": 15000} and limit_table []; "an itemised receipt is required for Rs 500 or more" -> documentation_required item itemised_receipt with min_amount 500 and limit_table []. A step required only above an amount (a service bond above Rs 50,000) is a "requires" condition with any_of: [{"id","text","check":"numeric","numeric":{"quantity":"amount","op":"<=","value":50000}}, {"id","text","check":"judgment","depends_on":[...]}] -- i.e. either the threshold isn't reached OR the step was done. (In an any_of, the alternatives carry id, text, check, depends_on, numeric; the outer condition carries kind.)
- A time window ("submit within 30 days of the expense date") is a numeric condition on days_since_expense (days from the document date to the submission date): kind "requires", numeric {"quantity":"days_since_expense","op":"<=","value":30}. "Claims older than 90 days are rejected outright" is a prohibition with an "excludes" numeric condition days_since_expense > 90. A window measured from anything else (return from a trip) is a judgment depending on trip_dates. Never express a time window as a limit_unit.
- Approval routing by CLAIM TOTAL (which approvers a claim of a given size needs), audit and penalty statements, the currency-conversion method, and general principles cannot be checked against one expense: applies_to_categories [] and no limit.

Rules:
- Use ONLY what the clause text says. Do not import numbers or rules from other clauses.
- If a clause is a table, structure the whole table.
- Return JSON only.

CATEGORY LIST (id: meaning):
{categories}

FIELD LIST (what a condition's "depends_on" may name):
{fields}
"""


# ------------------------------------------------------------------- prompts

def _category_list() -> str:
    return "\n".join(f"- {c.id}: {c.definition} (policy sections {', '.join(c.policy_clauses)})" for c in CATEGORIES.values())


def _field_list() -> str:
    return "\n".join(f"- {name}: {desc}" for name, (_status, desc) in pol.FIELD_VOCAB.items())


def build_messages(section: str, raws: list[pol.RawClause]) -> list[dict]:
    system = SYSTEM_PROMPT.replace("{categories}", _category_list()).replace("{fields}", _field_list())
    payload = {"section": section, "clauses": [{"clause_id": r.clause_id, "text": r.text} for r in raws]}
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


# --------------------------------------------------------------- validation

class StructureError(ValueError):
    """The model's answer violated the schema -- retried once with the
    problems listed, then the section is reported as unstructured."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _num(value: Any, where: str, problems: list[str]) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        problems.append(f"{where}: expected a number, got {value!r}")
        return None
    try:
        return pol._dec(value)
    except ValueError:
        problems.append(f"{where}: expected a number, got {value!r}")
        return None


def _section_categories(section: str) -> set[str]:
    return {c.id for c in CATEGORIES.values() if section in c.policy_clauses}


def normalize_entry(raw: pol.RawClause, entry: dict, order: int) -> tuple[pol.Clause, dict[str, Any]]:
    """Schema-validate one model entry and build the Clause. Raises
    StructureError listing EVERY schema problem found. Returns the clause
    plus `meta` (facts about the model's answer the CONFLICTS pass needs:
    unit evidence, the model's own category list, ...) that isn't part of
    the stored clause."""
    problems: list[str] = []
    cid = raw.clause_id

    unit = entry.get("limit_unit")
    if unit not in MODEL_UNITS:
        problems.append(f"{cid}: limit_unit {unit!r} is not one of {MODEL_UNITS}")
    kind = entry.get("limit_kind") or "amount"
    if kind == "none" or (not entry.get("limit_table") and kind not in pol.LIMIT_KINDS):
        kind = "amount"   # a clause with no limit has nothing to be a "kind" of
    if kind not in pol.LIMIT_KINDS:
        problems.append(f"{cid}: limit_kind {kind!r} is not one of {pol.LIMIT_KINDS}")

    model_cats = entry.get("applies_to_categories")
    if not isinstance(model_cats, list) or any(c not in pol.valid_category_ids() for c in model_cats):
        problems.append(f"{cid}: applies_to_categories must be a list of category ids or '*', got {model_cats!r}")
        model_cats = []

    table: list[pol.LimitEntry] = []
    for i, e in enumerate(entry.get("limit_table") or []):
        where = f"{cid}.limit_table[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{where}: not an object")
            continue
        amount = _num(e.get("amount"), f"{where}.amount", problems)
        when = e.get("when") or {}
        if not isinstance(when, dict) or any(not isinstance(v, (list, str)) for v in when.values()):
            problems.append(f"{where}.when must map keys to lists of values")
            when = {}
        if amount is not None:
            table.append(pol.LimitEntry(
                amount=amount,
                currency=e.get("currency") or None,
                when={str(k): tuple(str(x) for x in (v if isinstance(v, list) else [v])) for k, v in when.items()},
            ))

    conditions: list[dict] = []
    seen_ids: set[str] = set()

    def parse_condition(c: Any, where: str, top_level: bool) -> Optional[dict]:
        if not isinstance(c, dict) or not c.get("id") or not c.get("text"):
            problems.append(f"{where}: needs id and text")
            return None
        if c["id"] in seen_ids:
            problems.append(f"{where}: duplicate condition id {c['id']!r}")
        seen_ids.add(c["id"])
        if top_level and c.get("kind") not in pol.CONDITION_KINDS:
            problems.append(f"{where}.kind must be one of {pol.CONDITION_KINDS}")
        depends = c.get("depends_on") or []
        bad = [d for d in depends if d not in pol.FIELD_VOCAB]
        if bad:
            problems.append(f"{where}.depends_on names unknown fields {bad}")
        cond: dict[str, Any] = {"id": c["id"], "text": c["text"], "depends_on": [d for d in depends if d in pol.FIELD_VOCAB]}
        if top_level:
            cond["kind"] = c.get("kind")
        if top_level and c.get("any_of"):
            subs = [parse_condition(sub, f"{where}.any_of[{j}]", False) for j, sub in enumerate(c["any_of"])]
            cond["check"] = "judgment"
            cond["numeric"] = None
            cond["any_of"] = [x for x in subs if x]
            return cond
        check = c.get("check")
        if check not in ("judgment", "numeric"):
            problems.append(f"{where}.check must be judgment or numeric")
        cond["check"] = check
        cond["numeric"] = None
        if check == "numeric":
            n = c.get("numeric") or {}
            if n.get("quantity") not in pol.NUMERIC_QUANTITIES or n.get("op") not in pol.NUMERIC_OPS:
                problems.append(f"{where}.numeric needs quantity in {pol.NUMERIC_QUANTITIES} and op in {pol.NUMERIC_OPS}")
            else:
                value = _num(n.get("value"), f"{where}.numeric.value", problems)
                if value is not None:
                    cond["numeric"] = {"quantity": n["quantity"], "op": n["op"], "value": str(value)}
        return cond

    for i, c in enumerate(entry.get("conditions") or []):
        parsed = parse_condition(c, f"{cid}.conditions[{i}]", True)
        if parsed:
            conditions.append(parsed)

    docs: list[dict] = []
    for i, d in enumerate(entry.get("documentation_required") or []):
        where = f"{cid}.documentation_required[{i}]"
        if not isinstance(d, dict) or not d.get("item"):
            problems.append(f"{where}: needs item")
            continue
        min_amount = d.get("min_amount")
        if isinstance(min_amount, dict):
            parsed = {str(k): str(_num(v, where + f".min_amount.{k}", problems)) for k, v in min_amount.items()}
        else:
            parsed = str(_num(min_amount, where + ".min_amount", problems)) if min_amount is not None else None
        docs.append({"item": str(d["item"]), "min_amount": parsed})

    approval = entry.get("requires_approval_above")
    approval_map: Optional[dict[str, Decimal]] = None
    if approval is not None:
        if not isinstance(approval, dict) or not approval:
            problems.append(f"{cid}.requires_approval_above must be null or {{currency: amount}}")
        else:
            approval_map = {}
            for cur, amt in approval.items():
                value = _num(amt, f"{cid}.requires_approval_above.{cur}", problems)
                if value is not None:
                    approval_map[str(cur)] = value

    if problems:
        raise StructureError(problems)

    # applies_to: for a section some category maps to (categories.py's
    # policy_clauses), that mapping is authoritative and the model may only
    # ADD categories. For an unmapped section (1, 19-24) the model decides.
    section_cats = _section_categories(raw.section)
    if section_cats:
        applies = sorted(section_cats | {c for c in model_cats if c != "*"}, key=CATEGORY_IDS.index)
    else:
        applies = list(model_cats)

    clause = pol.Clause(
        clause_id=cid,
        section=raw.section,
        sort_order=order,
        verbatim_text=raw.text,
        applies_to_categories=tuple(applies),
        limit_unit=unit,
        limit_kind=kind,
        limit_inclusive=bool(entry.get("limit_inclusive", True)),
        limit_table=tuple(table),
        conditions=tuple(conditions),
        documentation_required=tuple(docs),
        requires_approval_above=approval_map,
        is_prohibition=bool(entry.get("is_prohibition", False)),
        notes=(entry.get("notes") or None),
    )
    meta = {
        "unit_evidence": entry.get("unit_evidence"),
        "model_categories": list(model_cats),
        "dropped_by_section_mapping": sorted(set(model_cats) - set(applies) - {"*"} if section_cats else set()),
        "model_said_star": "*" in model_cats and bool(section_cats),
    }
    return clause, meta


def parse_section_response(raw_text: str, raws: list[pol.RawClause]) -> dict[str, dict]:
    """The model's JSON -> {clause_id: entry}, insisting on exactly the ids
    that were asked for."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StructureError([f"response is not valid JSON: {exc}"])
    entries = data.get("clauses") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise StructureError(['response must be {"clauses": [...]}'])
    by_id: dict[str, dict] = {}
    for e in entries:
        if isinstance(e, dict) and e.get("clause_id") in {r.clause_id for r in raws}:
            by_id[e["clause_id"]] = e
    missing = [r.clause_id for r in raws if r.clause_id not in by_id]
    extra = [e.get("clause_id") for e in entries if isinstance(e, dict) and e.get("clause_id") not in {r.clause_id for r in raws}]
    problems = []
    if missing:
        problems.append(f"missing clause ids {missing}")
    if extra:
        problems.append(f"unexpected clause ids {extra}")
    if problems:
        raise StructureError(problems)
    return by_id


def structure_section(
    section: str,
    raws: list[pol.RawClause],
    ask: Callable[[list[dict], str], str],
    first_order: int,
) -> tuple[dict[str, tuple[pol.Clause, dict]], dict[str, list[str]]]:
    """Structure one section. `ask(messages, scope) -> raw_text` is the model
    call (injected so tests run offline). One retry, with the problems fed
    back, on a schema violation. Returns (structured, failed) where `failed`
    maps a clause id to why it could not be structured."""
    import policy_llm

    scope = f"build__{STRUCTURING_PROMPT_VERSION}__s{section}"
    out: dict[str, tuple[pol.Clause, dict]] = {}
    failed: dict[str, list[str]] = {}
    order = {r.clause_id: first_order + i for i, r in enumerate(raws)}
    todo = list(raws)
    failed_hint: list[str] = []
    # Attempt 1 asks for the whole section. Later attempts ask ONLY for the
    # clauses that failed validation or were missing (a smaller request keeps a
    # weaker model from dropping clauses), feeding the problems back, and keep
    # everything that already validated. The last attempt goes one clause at a time.
    for attempt in range(3):
        batches = [todo] if attempt < 2 else [[r] for r in todo]
        still: list[pol.RawClause] = []
        failed = {}
        for batch in batches:
            messages = build_messages(section, batch)
            if attempt and failed_hint:
                messages = messages + [{"role": "user", "content": "A previous answer for these clauses was rejected: " + "; ".join(failed_hint[:4]) + ". Return a complete, valid answer for every clause listed."}]
            try:
                raw_text = ask(messages, f"{scope}__a{attempt}")
                by_id = parse_section_response(raw_text, batch)
            except policy_llm.PolicyModelError as exc:
                for r in batch:
                    failed[r.clause_id] = [str(exc)]
                still.extend(batch)
                continue
            except StructureError as exc:
                # keep whatever entries did come back for the requested ids
                try:
                    by_id = {e["clause_id"]: e for e in json.loads(raw_text).get("clauses", [])
                             if isinstance(e, dict) and e.get("clause_id") in {r.clause_id for r in batch}}
                except (json.JSONDecodeError, AttributeError, TypeError):
                    by_id = {}
                for r in batch:
                    if r.clause_id not in by_id:
                        failed[r.clause_id] = exc.problems
            for r in batch:
                if r.clause_id in by_id:
                    try:
                        out[r.clause_id] = normalize_entry(r, by_id[r.clause_id], order[r.clause_id])
                        failed.pop(r.clause_id, None)
                        continue
                    except StructureError as exc:
                        failed[r.clause_id] = exc.problems
                still.append(r)
        todo = still
        failed_hint = [p for ps_ in failed.values() for p in ps_]
        if not todo:
            return out, {}
    return out, failed


def stub_clause(raw: pol.RawClause) -> pol.Clause:
    """Placeholder for a clause the model could not structure: no category,
    so it is never offered to the selection model, and loudly reported."""
    return pol.Clause(
        clause_id=raw.clause_id, section=raw.section, sort_order=raw.sort_order, verbatim_text=raw.text,
        applies_to_categories=(), notes="UNSTRUCTURED: the model's answer failed validation twice",
    )


# ---------------------------------------------------------------- conflicts

@dataclass(frozen=True)
class Conflict:
    kind: str
    clause_ids: tuple[str, ...]
    message: str


def _overlap_conflicts(clauses: list[pol.Clause]) -> list[Conflict]:
    out: list[Conflict] = []

    def compatible(a: dict, b: dict) -> bool:
        return all(set(a[k]) & set(b[k]) for k in set(a) & set(b))

    def entries(c: pol.Clause):
        return [(e, c) for e in c.limit_table]

    # within one clause: two entries that can apply together but differ.
    for c in clauses:
        for i, ea in enumerate(c.limit_table):
            for eb in c.limit_table[i + 1:]:
                if ea.currency == eb.currency and compatible(ea.when, eb.when) and ea.amount != eb.amount:
                    out.append(Conflict("OVERLAP", (c.clause_id,),
                        f"{c.clause_id}: two limits ({ea.amount} and {eb.amount} {ea.currency or ''}) can apply to the same expense "
                        f"(when={ea.when} vs {eb.when})"))
                    break
            else:
                continue
            break

    # across clauses: same category, same unit and currency, overlapping scope, different amount.
    for cat in CATEGORY_IDS:
        in_cat = [c for c in clauses if cat in c.applies_to_categories and c.limit_table and c.limit_unit not in ("none", "unknown")]
        for i, a in enumerate(in_cat):
            for b in in_cat[i + 1:]:
                if a.limit_unit != b.limit_unit or a.limit_kind != b.limit_kind:
                    continue
                hit = next(
                    ((ea, eb) for ea, _ in entries(a) for eb, _ in entries(b)
                     if ea.currency == eb.currency and compatible(ea.when, eb.when) and ea.amount != eb.amount),
                    None,
                )
                if hit:
                    out.append(Conflict("OVERLAP", (a.clause_id, b.clause_id),
                        f"[{cat}] {a.clause_id} and {b.clause_id} both set a {a.limit_unit} limit that can apply to the same expense "
                        f"({hit[0].amount} vs {hit[1].amount} {hit[0].currency or ''}); the text may make one an alternative to the other"))
    seen: set[tuple] = set()
    unique = []
    for c in out:
        key = (c.kind, c.clause_ids)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def detect_conflicts(
    clauses: list[pol.Clause],
    metas: dict[str, dict],
    raws: list[pol.RawClause],
    failed: dict[str, list[str]],
) -> list[Conflict]:
    out: list[Conflict] = []
    raw_by_id = {r.clause_id: r for r in raws}
    for c in clauses:
        text = c.verbatim_text
        meta = metas.get(c.clause_id, {})
        if c.clause_id in failed:
            out.append(Conflict("UNSTRUCTURED", (c.clause_id,), f"{c.clause_id}: could not be structured -- {'; '.join(failed[c.clause_id][:3])}"))
            continue
        if raw_by_id[c.clause_id].synthesized_id:
            out.append(Conflict("SYNTHESIZED_ID", (c.clause_id,), f"{c.clause_id}: the policy prints this as an unnumbered bullet under section {c.section}; the id is generated"))

        # A. unit undeterminable
        has_limit = bool(c.limit_table)
        if c.limit_unit == "unknown":
            out.append(Conflict("UNIT_UNDETERMINED", (c.clause_id,), f"{c.clause_id}: states a limit but the text does not say what it is per"))
        elif has_limit and c.limit_unit == "none":
            out.append(Conflict("UNIT_UNDETERMINED", (c.clause_id,), f"{c.clause_id}: has a limit table but limit_unit is 'none'"))
        elif has_limit:
            evidence = meta.get("unit_evidence")
            if meta.get("manual"):
                pass   # a manual correction: its unit was set by a person, with a logged reason
            elif not evidence or evidence.strip().lower() not in text.lower():
                out.append(Conflict("UNIT_UNDETERMINED", (c.clause_id,), f"{c.clause_id}: unit {c.limit_unit} has no supporting quote in the clause (model quoted {evidence!r})"))

        # B. unit disagreement with the words
        if has_limit and c.limit_unit not in ("unknown", "none"):
            hinted = {u for u, rx in UNIT_HINTS.items() if rx.search(text)}
            if hinted and c.limit_unit not in hinted:
                out.append(Conflict("UNIT_DISAGREES_WITH_TEXT", (c.clause_id,),
                    f"{c.clause_id}: structured as {c.limit_unit} but the text's wording suggests {sorted(hinted)}"))
            if not hinted and c.limit_unit in ("per_night", "per_day", "per_month", "per_person", "per_ride", "per_km", "percent_of_amount"):
                out.append(Conflict("UNIT_DISAGREES_WITH_TEXT", (c.clause_id,),
                    f"{c.clause_id}: structured as {c.limit_unit} but no wording in the clause names that unit"))

        # E. numbers not in the text
        text_numbers = pol.numbers_in(text)
        structured_numbers = {e.amount for e in c.limit_table}
        if c.requires_approval_above:
            structured_numbers |= {v for v in c.requires_approval_above.values()}
        for cond in c.conditions:
            if cond.get("numeric"):
                structured_numbers.add(Decimal(cond["numeric"]["value"]))
        for d in c.documentation_required:
            m = d.get("min_amount")
            if isinstance(m, dict):
                structured_numbers |= {Decimal(v) for v in m.values()}
            elif m is not None:
                structured_numbers.add(Decimal(m))
        invented = {n for n in structured_numbers if n not in text_numbers and n != 0}
        if invented:
            out.append(Conflict("NUMBER_NOT_IN_TEXT", (c.clause_id,), f"{c.clause_id}: structured number(s) {sorted(invented)} do not appear in the clause text"))

        # F. numbers in the text the structure did not capture
        cleaned = re.sub(r"§\s?\d+(?:\.\d+)*(?:\s?[–-]\s?\d+(?:\.\d+)*)?", " ", text)
        cleaned = re.sub(r"^\s*\d+(?:\.\d+)+\.", " ", cleaned)               # the clause's own number
        cleaned = re.sub(r"\bL\d\b|\bTier \d\b|\bZone [AB]\b", " ", cleaned)    # grade/tier labels
        cleaned = re.sub(r"\b\d{1,2}(?::\d{2})?\s?(?:AM|PM)\b", " ", cleaned)   # clock times
        mentioned = " ".join(cond["text"] for cond in c.conditions) + " " + (c.notes or "") + " " + " ".join(d["item"] for d in c.documentation_required)
        uncaptured = {n for n in pol.numbers_in(cleaned) if n not in structured_numbers and n not in pol.numbers_in(mentioned)}
        if uncaptured:
            out.append(Conflict("UNCAPTURED_NUMBERS", (c.clause_id,), f"{c.clause_id}: number(s) {sorted(uncaptured)} in the text appear nowhere in the structure"))

        # J. a limit that duplicates an approval threshold (a threshold is not a limit)
        if c.requires_approval_above and c.limit_table:
            for e in c.limit_table:
                if e.currency in c.requires_approval_above and c.requires_approval_above[e.currency] == e.amount:
                    out.append(Conflict("LIMIT_EQUALS_APPROVAL_THRESHOLD", (c.clause_id,),
                        f"{c.clause_id}: the {e.amount} {e.currency} limit equals the approval threshold -- exceeding it would read as a violation, not as needing approval"))
                    break

        # G. prohibition without a trigger
        if c.is_prohibition and not any(x.get("kind") == "excludes" for x in c.conditions):
            out.append(Conflict("PROHIBITION_WITHOUT_TRIGGER", (c.clause_id,), f"{c.clause_id}: is_prohibition but no 'excludes' condition states what triggers it"))

        # D. conditions / documentation that depend on data Stage 1 does not reliably extract
        gaps: dict[str, str] = {}
        for cond in c.conditions:
            for token in cond.get("depends_on", []):
                status = pol.FIELD_VOCAB[token][0]
                if status != "extracted":
                    gaps[token] = status
        if gaps:
            out.append(Conflict("STAGE1_GAP", (c.clause_id,), f"{c.clause_id}: conditions depend on " + ", ".join(f"{t} ({s})" for t, s in sorted(gaps.items()))))

        # H. the model's categories disagreed with categories.py's section mapping
        if meta.get("dropped_by_section_mapping"):
            out.append(Conflict("CATEGORY_SCOPE", (c.clause_id,), f"{c.clause_id}: model also proposed {meta['dropped_by_section_mapping']}; kept (added to section mapping)"))

        # I. tables the structure may not have fully captured
        if "|" in text and has_limit is False and not c.conditions and c.applies_to_categories:
            out.append(Conflict("TABLE_NOT_STRUCTURED", (c.clause_id,), f"{c.clause_id}: the clause contains a table but no limit or condition was structured (e.g. class of travel by grade is a judgment against the clause text)"))

    out.extend(_overlap_conflicts(clauses))
    return out


# ------------------------------------------------------------------- output

def _limit_summary(c: pol.Clause) -> str:
    if not c.limit_table:
        return "-"
    if len(c.limit_table) == 1 and not c.limit_table[0].when:
        e = c.limit_table[0]
        return f"{e.amount:g} {e.currency or ''}".strip() + ("" if c.limit_inclusive else " (excl)")
    amounts = [e.amount for e in c.limit_table]
    curs = sorted({e.currency or "" for e in c.limit_table})
    keys = sorted({k for e in c.limit_table for k in e.when})
    return f"{len(c.limit_table)} entries {min(amounts):g}-{max(amounts):g} {'/'.join(x for x in curs if x)} by {'x'.join(keys) or 'n/a'}"


def render_review_table(clauses: list[pol.Clause], flagged: dict[str, list[str]]) -> str:
    header = f"{'id':<7} {'categories':<26} {'unit':<15} {'limit':<44} {'cond':<4} {'doc':<3} {'appr':<12} {'proh':<4} flags"
    lines = [header, "-" * len(header)]
    for c in clauses:
        cats = ",".join(c.applies_to_categories) or "(informational)"
        cats = cats if len(cats) <= 26 else cats[:23] + "..."
        appr = ",".join(f"{k}>{v:g}" for k, v in (c.requires_approval_above or {}).items()) or "-"
        lines.append(
            f"{c.clause_id:<7} {cats:<26} {c.limit_unit:<15} {_limit_summary(c)[:44]:<44} "
            f"{len(c.conditions):<4} {len(c.documentation_required):<3} {appr[:12]:<12} {'Y' if c.is_prohibition else '-':<4} "
            f"{','.join(sorted(set(flagged.get(c.clause_id, [])))) or ''}"
        )
    return "\n".join(lines)


def render_detail(c: pol.Clause) -> str:
    return json.dumps(pol.clause_to_dict(c), indent=2, ensure_ascii=False)


def render_conflicts(conflicts: list[Conflict]) -> str:
    if not conflicts:
        return "CONFLICTS: none"
    lines = [f"CONFLICTS ({len(conflicts)}) -- none of these were resolved automatically:"]
    order = ["UNSTRUCTURED", "LIMIT_EQUALS_APPROVAL_THRESHOLD", "UNIT_UNDETERMINED", "UNIT_DISAGREES_WITH_TEXT", "OVERLAP", "NUMBER_NOT_IN_TEXT",
             "UNCAPTURED_NUMBERS", "PROHIBITION_WITHOUT_TRIGGER", "STAGE1_GAP", "TABLE_NOT_STRUCTURED", "CATEGORY_SCOPE", "SYNTHESIZED_ID"]
    for kind in order + sorted({c.kind for c in conflicts} - set(order)):
        group = [c for c in conflicts if c.kind == kind]
        if group:
            lines.append(f"\n[{kind}] ({len(group)})")
            lines.extend(f"  - {c.message}" for c in group)
    return "\n".join(lines)


def apply_overrides(clauses: list[pol.Clause], overrides: dict) -> tuple[list[pol.Clause], list[dict]]:
    """Apply manual corrections (clause_id -> {field: value, "_reason": text}) on top of the
    model's structure by round-tripping through the JSON form. Each is logged with its
    reason; an unknown clause id or field is an error, never ignored."""
    by_id = {c.clause_id: pol.clause_to_dict(c) for c in clauses}
    log: list[dict] = []
    for cid, patch in overrides.items():
        if cid.startswith("_"):
            continue
        if cid not in by_id:
            raise SystemExit(f"--overrides names unknown clause {cid}")
        reason = patch.get("_reason")
        if not reason:
            raise SystemExit(f"--overrides for {cid} needs a _reason")
        before = {}
        for field_name, value in patch.items():
            if field_name == "_reason":
                continue
            if field_name not in by_id[cid]:
                raise SystemExit(f"--overrides for {cid}: unknown field {field_name}")
            before[field_name] = by_id[cid][field_name]
            by_id[cid][field_name] = value
        # keep the spec's scalar limit fields consistent with the table
        table = by_id[cid]["limit_table"]
        by_id[cid]["limit_amount"] = table[0]["amount"] if len(table) == 1 else None
        by_id[cid]["limit_currency"] = table[0]["currency"] if len(table) == 1 else None
        log.append({"clause_id": cid, "reason": reason, "changed": sorted(before), "before": before})
    return [pol.clause_from_dict(by_id[c.clause_id]) for c in clauses], log


# --------------------------------------------------------------------- main

def build(ask: Callable[[list[dict], str], str], markdown: str, only_sections: Optional[set[str]] = None):
    raws = pol.segment_policy(markdown)
    grouped = pol.sections_of(raws)
    clauses: list[pol.Clause] = []
    metas: dict[str, dict] = {}
    failed: dict[str, list[str]] = {}
    for section, section_raws in grouped.items():
        if only_sections and section not in only_sections:
            continue
        structured, section_failed = structure_section(section, section_raws, ask, first_order=section_raws[0].sort_order)
        failed.update(section_failed)
        for r in section_raws:
            if r.clause_id in structured:
                clause, meta = structured[r.clause_id]
                metas[r.clause_id] = meta
                clauses.append(clause)
            else:
                clauses.append(stub_clause(r))
    used_raws = [r for r in raws if not only_sections or r.section in only_sections]
    conflicts = detect_conflicts(clauses, metas, used_raws, failed)
    return clauses, conflicts, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=pol.POLICY_MD_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--version", default="v1")
    ap.add_argument("--sections", nargs="*", help="only structure these section numbers (a partial run never writes)")
    ap.add_argument("--detail", nargs="*", default=[], help="print these clause ids in full")
    ap.add_argument("--model", default=None, help="default: $POLICY_BUILD_MODEL, else extract.MODEL")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached model answers")
    ap.add_argument("--replay", metavar="PROMPT_VERSION", help="make NO model call: re-use the newest cached raw answer of an "
                    "earlier prompt version (e.g. structure-v1) for each section, then validate it with the current code")
    ap.add_argument("--overrides", type=Path, help="JSON {clause_id: {field: value}} of MANUAL corrections applied after "
                    "structuring; every one is recorded in build_meta.manual_edits with its reason")
    ap.add_argument("--confirm", action="store_true", help="REQUIRED to write the JSON: you have read the review table")
    ap.add_argument("--reviewed-by", default=None, help="name recorded in the JSON; omit if no human has reviewed the table")
    ap.add_argument("--allow-unstructured", action="store_true", help="write even if some clause could not be structured")
    args = ap.parse_args()

    # A stale machine-level GROQ_API_KEY must not beat the key in .env -- but
    # only the credential is overridden (never DATABASE_URL etc., which the
    # test suite sets and this module is imported by tests).
    load_dotenv(BASE_DIR / ".env")
    for key, value in dotenv_values(BASE_DIR / ".env").items():
        if key in ("GROQ_API_KEY", "LLAMA_CLOUD_API_KEY") and value:
            os.environ[key] = value

    import policy_llm
    from extract import MODEL

    model = args.model or os.environ.get("POLICY_BUILD_MODEL") or MODEL
    markdown = args.source.read_text(encoding="utf-8")

    def ask(messages: list[dict], scope: str) -> str:
        if args.replay:
            m = re.search(r"__s(\d+)", scope)
            files = sorted(BUILD_CACHE_DIR.glob(f"build__{args.replay}__s{m.group(1)}__*.json"), key=lambda f: f.stat().st_mtime)
            if not files:
                raise policy_llm.PolicyModelError(f"no cached {args.replay} answer for section {m.group(1)}")
            return json.loads(files[-1].read_text(encoding="utf-8"))["raw_text"]
        return policy_llm.call_json(messages, model=model, scope=scope, cache_dir=BUILD_CACHE_DIR, use_cache=not args.no_cache).raw_text

    try:
        clauses, conflicts, failed = build(ask, markdown, set(args.sections) if args.sections else None)
    except policy_llm.PolicyModelUnavailable as exc:
        print(f"Cannot reach the model: {exc}", file=sys.stderr)
        return 2

    manual_edits: list[dict] = []
    if args.overrides:
        clauses, manual_edits = apply_overrides(clauses, json.loads(args.overrides.read_text(encoding="utf-8")))
        overridden = {e["clause_id"] for e in manual_edits}
        failed = {k: v for k, v in failed.items() if k not in overridden}
        edited = [c for c in clauses if c.clause_id in overridden]
        # Keep the model-pass conflicts for untouched clauses; re-derive them for the edited ones.
        conflicts = [c for c in conflicts if not (set(c.clause_ids) & overridden)] + detect_conflicts(
            edited, {c.clause_id: {"manual": True} for c in edited}, pol.segment_policy(markdown), {})
        conflicts += [c for c in _overlap_conflicts(clauses) if set(c.clause_ids) & overridden and c not in conflicts]
        print(f"\nApplied {len(manual_edits)} manual correction(s) from {args.overrides} (recorded in the JSON).")

    flagged: dict[str, list[str]] = {}
    for c in conflicts:
        for cid in c.clause_ids:
            flagged.setdefault(cid, []).append(c.kind)
    print(f"\nPolicy structured: {len(clauses)} clauses from {len({c.section for c in clauses})} sections "
          f"(model {model}, prompt {STRUCTURING_PROMPT_VERSION})\n")
    print(render_review_table(clauses, flagged))
    print()
    print(render_conflicts(conflicts))
    by_id = {c.clause_id: c for c in clauses}
    for cid in args.detail:
        print(f"\n--- {cid} ---")
        print(render_detail(by_id[cid]) if cid in by_id else "not found")

    if args.sections:
        print("\nPartial run (--sections): nothing written.")
        return 0
    if failed and not args.allow_unstructured:
        print(f"\n{len(failed)} clause(s) could not be structured; nothing written (--allow-unstructured to override).")
        return 1
    if not args.confirm:
        print("\nNothing written. Review the table and CONFLICTS above against the policy, then re-run with --confirm.")
        return 0

    reference = pol.parse_reference_data(markdown)
    document = {
        "version": args.version,
        "source": str(args.source.relative_to(BASE_DIR)).replace("\\", "/"),
        "source_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "reference_data": reference,
        "build_meta": {
            "structuring_model": model,
            "structuring_prompt_version": STRUCTURING_PROMPT_VERSION,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_flag_passed": True,
            "human_reviewed": bool(args.reviewed_by),
            "reviewed_by": args.reviewed_by,
            "unstructured_clause_ids": sorted(failed),
            "replayed_from": args.replay,
            "manual_edits": manual_edits,
            "conflicts": [{"kind": c.kind, "clause_ids": list(c.clause_ids), "message": c.message} for c in conflicts],
        },
        "clauses": [pol.clause_to_dict(c) for c in clauses],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {args.out} ({len(clauses)} clauses; human_reviewed={document['build_meta']['human_reviewed']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
