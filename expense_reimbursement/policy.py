"""Stage 3 policy structures: the shape of a structured clause, the code that
cuts policy/expense_policy.md into clauses, and the reference tables
(grades, city tiers, zones) parsed from its section 2.

Nothing here calls a model. The clause TEXT and IDS come from this module's
segmentation of the markdown -- never from an LLM -- so a verbatim citation
is verbatim by construction. scripts/build_policy.py asks a model only to
fill in the structured fields for text this module already cut out, and
checks its answer against the text before anything is written.

`Clause` is used three ways: built from the reviewable JSON seed
(policy/policy_v1.json), built from a policy_clauses DB row (the runtime
source of truth), and handed to the deterministic checker (policy_check.py).
"""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from categories import CATEGORY_IDS

BASE_DIR = Path(__file__).resolve().parent
POLICY_MD_PATH = BASE_DIR / "policy" / "expense_policy.md"

# The spec's seven units, plus five the real policy needs and the seven
# can't express: a per-ride cap (5.3), a per-item cap (14.1), a per-km rate
# (6.2), a per-event cap (10.4 / 11.2 style) and a percentage of another
# amount (alcohol <= 30% of the bill, tips <= 10%, forex <= 2%).
LIMIT_UNITS = (
    "per_trip", "per_night", "per_day", "per_month", "per_person", "per_claim", "none",
    "per_ride", "per_item", "per_km", "per_event", "percent_of_amount",
)
# amount: a money limit; percent: a % of another amount; count: a number of
# occurrences (the team-event frequency limit).
LIMIT_KINDS = ("amount", "percent", "count")

# Dimensions the CODE resolves from the employee / expense location; any
# other key in a limit entry's `when` is a "qualifier" the selection model
# names (e.g. occasion: routine|celebration) and code validates against the
# values the table actually contains.
CODE_DIMENSIONS = ("grade", "city_tier", "zone", "vehicle_type")

CONDITION_KINDS = ("requires", "excludes")
NUMERIC_QUANTITIES = (
    "nights", "days", "amount", "unit_amount", "persons", "distance_km", "grade_rank", "days_since_expense",
)
NUMERIC_OPS = (">", ">=", "<", "<=", "==")

# What a condition can depend on, and whether Stage 1 (or the DB) actually
# supplies it. "partial" = only sometimes: a free-form additional_fields
# key, or one document type. Anything not "extracted" is reported in the
# build_policy CONFLICTS section, and at evaluation time an unknown value
# becomes a `missing_fields` entry instead of a guess.
FIELD_VOCAB: dict[str, tuple[str, str]] = {
    "document_amount": ("extracted", "the document's total / a line item's total"),
    "currency": ("extracted", "the document's currency"),
    "bill_date": ("extracted", "the document's date"),
    "vendor_name": ("extracted", "vendor / merchant name"),
    "line_item_names": ("extracted", "line-item descriptions (alcohol, minibar, laundry...)"),
    "employee_grade": ("extracted", "employees.grade"),
    "employee_base_city": ("extracted", "employees.base_city"),
    "claim_note": ("extracted", "the claim's free-text note to the approver"),
    "approval_email_in_claim": ("extracted", "an approval_correspondence document in the same claim"),
    "trip_log_rows": ("partial", "travel_entries -- local_conveyance_form documents only"),
    "distance_km": ("partial", "total_kms -- local_conveyance_form documents only"),
    "stay_dates": ("partial", "check-in/out or nights, only if the hotel invoice printed them into additional_fields"),
    "attendee_count": ("partial", "only if printed on the bill and captured in additional_fields"),
    "expense_city": ("partial", "only if the document's text/additional_fields name a city"),
    "expense_country": ("partial", "only if the document's text/additional_fields name a country"),
    "client_name": ("partial", "only if printed on the bill and captured in additional_fields"),
    "quantity": ("partial", "line-item quantity is a free string"),
    "business_purpose": ("not_extracted", "no Stage 1 field; at best the claim note"),
    "vehicle_type": ("not_extracted", "two- vs four-wheeler is not extracted"),
    "vehicle_ownership": ("not_extracted", "company-leased vs own vehicle is not extracted"),
    "gstin_on_invoice": ("not_extracted", "GSTIN is extracted for restaurant bills only"),
    "billed_to_name": ("not_extracted", "whose name a bill is in is not extracted"),
    "trip_dates": ("not_extracted", "trip start/end (for trip length) is not extracted"),
    "trip_id": ("not_extracted", "which trip an expense belongs to is not extracted"),
    "flight_details": ("not_extracted", "flight duration / class / booking date are not extracted"),
    "travel_class": ("not_extracted", "class of travel is not extracted"),
    "booking_lead_time": ("not_extracted", "booking date vs travel date is not extracted"),
    "recipient_identity": ("not_extracted", "who a gift/entertainment was for is not extracted"),
    "preapproval_evidence": ("not_extracted", "pre-approval is only visible if an approval email was uploaded"),
    "team_membership": ("not_extracted", "which team an event belongs to is not extracted"),
    "employee_designation": ("not_extracted", "remote/field-based designation, company-car policy: not in HRMS data"),
    "submission_date": ("extracted", "claims.submitted_at / created_at vs the document date"),
    "other_claims_history": ("extracted", "the employee's other documents (used for monthly aggregation)"),
    "time_of_day": ("not_extracted", "time of the expense (late-night travel) is not extracted"),
    "service_bond": ("not_extracted", "whether a service retention bond was signed is not extracted"),
    "reimbursed_elsewhere": ("not_extracted", "whether a client/employer already paid is not extracted"),
    "receipt_itemised": ("partial", "whether the document shows line items"),
}

# City tiers: the policy names Tier 1 and 2 explicitly; everything else in
# India is Tier 3 by the policy's own definition. These aliases are CODE
# knowledge (not policy text): alternate spellings and the towns that make
# up "Delhi NCR". They only widen matching for names the policy lists.
CITY_ALIASES: dict[str, str] = {
    "bangalore": "bengaluru", "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata",
    "poona": "pune", "baroda": "vadodara", "cochin": "kochi",
    "delhi": "delhi ncr", "new delhi": "delhi ncr", "gurgaon": "delhi ncr", "gurugram": "delhi ncr",
    "noida": "delhi ncr", "greater noida": "delhi ncr", "ghaziabad": "delhi ncr", "faridabad": "delhi ncr",
    "ncr": "delhi ncr",
}
COUNTRY_ALIASES: dict[str, str] = {
    "us": "usa", "u.s.": "usa", "u.s.a.": "usa", "united states": "usa", "united states of america": "usa",
    "america": "usa", "uk": "united kingdom", "u.k.": "united kingdom", "great britain": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom", "wales": "united kingdom",
    "the netherlands": "netherlands", "holland": "netherlands",
}


# ---------------------------------------------------------------- data types

@dataclass(frozen=True)
class LimitEntry:
    amount: Decimal
    currency: Optional[str]  # "INR" / "USD" ...; None for a percent or a count
    when: dict[str, tuple[str, ...]] = field(default_factory=dict)  # dimension -> allowed values


@dataclass(frozen=True)
class Clause:
    clause_id: str
    section: str
    sort_order: int
    verbatim_text: str
    applies_to_categories: tuple[str, ...]
    limit_unit: str = "none"
    limit_kind: str = "amount"
    limit_inclusive: bool = True
    limit_table: tuple[LimitEntry, ...] = ()
    conditions: tuple[dict, ...] = ()
    documentation_required: tuple[dict, ...] = ()
    # currency -> amount; Decimal("0") means approval is always required.
    requires_approval_above: Optional[dict[str, Decimal]] = None
    is_prohibition: bool = False
    notes: Optional[str] = None

    @property
    def limit_amount(self) -> Optional[Decimal]:
        return self.limit_table[0].amount if len(self.limit_table) == 1 else None

    @property
    def limit_currency(self) -> Optional[str]:
        return self.limit_table[0].currency if len(self.limit_table) == 1 else None


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a number: {value!r}")


def limit_entry_from_dict(d: dict) -> LimitEntry:
    when = {str(k): tuple(str(x) for x in (v if isinstance(v, list) else [v])) for k, v in (d.get("when") or {}).items()}
    return LimitEntry(amount=_dec(d["amount"]), currency=d.get("currency"), when=when)


def limit_entry_to_dict(e: LimitEntry) -> dict:
    return {"amount": str(e.amount), "currency": e.currency, "when": {k: list(v) for k, v in e.when.items()}}


def clause_from_dict(d: dict) -> Clause:
    approval = d.get("requires_approval_above")
    return Clause(
        clause_id=d["clause_id"],
        section=d["section"],
        sort_order=int(d["sort_order"]),
        verbatim_text=d["verbatim_text"],
        applies_to_categories=tuple(d["applies_to_categories"]),
        limit_unit=d.get("limit_unit", "none"),
        limit_kind=d.get("limit_kind", "amount"),
        limit_inclusive=bool(d.get("limit_inclusive", True)),
        limit_table=tuple(limit_entry_from_dict(e) for e in d.get("limit_table") or []),
        conditions=tuple(d.get("conditions") or []),
        documentation_required=tuple(d.get("documentation_required") or []),
        requires_approval_above={k: _dec(v) for k, v in approval.items()} if approval else None,
        is_prohibition=bool(d.get("is_prohibition", False)),
        notes=d.get("notes"),
    )


def clause_to_dict(c: Clause) -> dict:
    """The reviewable JSON form -- also what the DB row is seeded from.
    Carries the spec's scalar limit_amount/limit_currency for a clause with
    exactly one limit, alongside the full limit_table."""
    return {
        "clause_id": c.clause_id,
        "section": c.section,
        "sort_order": c.sort_order,
        "verbatim_text": c.verbatim_text,
        "applies_to_categories": list(c.applies_to_categories),
        "limit_amount": str(c.limit_amount) if c.limit_amount is not None else None,
        "limit_currency": c.limit_currency,
        "limit_unit": c.limit_unit,
        "limit_kind": c.limit_kind,
        "limit_inclusive": c.limit_inclusive,
        "limit_table": [limit_entry_to_dict(e) for e in c.limit_table],
        "conditions": list(c.conditions),
        "documentation_required": list(c.documentation_required),
        "requires_approval_above": (
            {k: str(v) for k, v in c.requires_approval_above.items()} if c.requires_approval_above else None
        ),
        "is_prohibition": c.is_prohibition,
        "notes": c.notes,
    }


def clause_from_row(row) -> Clause:
    """A policy_clauses ORM row -> Clause. Reads the same JSON shape the
    seed script wrote, so the DB and the seed can't drift apart in meaning."""
    return clause_from_dict({
        "clause_id": row.clause_id,
        "section": row.section,
        "sort_order": row.sort_order,
        "verbatim_text": row.verbatim_text,
        "applies_to_categories": list(row.applies_to_categories),
        "limit_unit": row.limit_unit,
        "limit_kind": row.limit_kind,
        "limit_inclusive": row.limit_inclusive,
        "limit_table": row.limit_table,
        "conditions": row.conditions,
        "documentation_required": row.documentation_required,
        "requires_approval_above": row.requires_approval_above,
        "is_prohibition": row.is_prohibition,
        "notes": row.notes,
    })


# ------------------------------------------------------------- segmentation

@dataclass(frozen=True)
class RawClause:
    clause_id: str
    section: str
    sort_order: int
    text: str
    synthesized_id: bool = False  # True when the id isn't printed in the policy (a bullet)


_SECTION_HEADING = re.compile(r"^## (\d+)\. (.+?)\s*$")
_CLAUSE_START = re.compile(r"^(\d+\.\d+(?:\.\d+)?)\.\s")
_BULLET = re.compile(r"^- (.+)$")


def segment_policy(markdown: str) -> list[RawClause]:
    """Cut the policy into clauses: every numbered paragraph ("8.4. ...",
    "5.3.1. ...") together with the table/lines that follow it. Section 2
    (definitions of grades/tiers/zones) is reference data, not a clause.
    Section 19 is a bullet list with no numbers of its own: each bullet
    becomes its own clause with a SYNTHESIZED id (19.1, 19.2, ...) carrying
    the section's intro sentence, so a citation still reads as a rule."""
    lines = markdown.splitlines()
    clauses: list[RawClause] = []
    section: Optional[str] = None
    current_id: Optional[str] = None
    current_lines: list[str] = []
    intro_lines: list[str] = []
    bullet_count = 0

    def flush() -> None:
        nonlocal current_id, current_lines
        if current_id is not None and section is not None:
            text = "\n".join(current_lines).strip()
            text = re.sub(r"\n---\s*$", "", text).strip()
            clauses.append(RawClause(current_id, section, len(clauses), text))
        current_id, current_lines = None, []

    for line in lines:
        heading = _SECTION_HEADING.match(line)
        if heading:
            flush()
            section = heading.group(1)
            intro_lines, bullet_count = [], 0
            continue
        if section is None or section == "2":
            continue
        if line.strip() == "---":
            flush()
            continue
        start = _CLAUSE_START.match(line)
        if start and start.group(1).split(".")[0] == section:
            flush()
            current_id = start.group(1)
            current_lines = [line]
            continue
        bullet = _BULLET.match(line)
        if bullet and current_id is None and section == "19":
            bullet_count += 1
            intro = " ".join(intro_lines).strip()
            text = f"{intro}\n{line}" if intro else line
            clauses.append(RawClause(f"19.{bullet_count}", section, len(clauses), text, synthesized_id=True))
            continue
        if current_id is not None:
            current_lines.append(line)
        elif section == "19" and line.strip():
            intro_lines.append(line.strip())
    flush()
    ids = [c.clause_id for c in clauses]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate clause ids after segmentation: {dupes}")
    return clauses


def sections_of(raw_clauses: list[RawClause]) -> dict[str, list[RawClause]]:
    grouped: dict[str, list[RawClause]] = {}
    for c in raw_clauses:
        grouped.setdefault(c.section, []).append(c)
    return grouped


# ---------------------------------------------------------- reference tables

def _table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("|") and not re.match(r"^\|[\s\-|]+\|?$", line):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def _split_top_level(text: str) -> list[str]:
    parts, depth, buf = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def parse_reference_data(markdown: str) -> dict[str, Any]:
    """Grades, city tiers and zones from section 2, parsed deterministically
    (they're plain tables -- no model needed, and a model can't misread
    what code parses). Returns:
      grades: ordered ["L1", ...] (rank = position + 1)
      city_tiers: {"1": ["mumbai", ...], "2": [...]}; anything else in India is "3"
      zone_a_countries: explicitly listed Zone A countries; every other
        country is UNKNOWN, not Zone B, because the policy's Zone A list is
        open-ended ("Western Europe ... etc.")
      zone_a_open_ended: the open-ended group names
    """
    start = markdown.index("## 2. ")
    end = markdown.index("## 3. ")
    section2 = markdown[start:end]

    grades = []
    for row in _table_rows(section2.split("### 2.2")[0]):
        if re.fullmatch(r"L\d", row[0]):
            grades.append(row[0])

    city_tiers: dict[str, list[str]] = {}
    tier_block = section2.split("### 2.2")[1].split("### 2.3")[0]
    for row in _table_rows(tier_block):
        m = re.fullmatch(r"Tier (\d)", row[0])
        if m and m.group(1) in ("1", "2"):
            city_tiers[m.group(1)] = [c.strip().lower() for c in row[1].split(",") if c.strip()]

    zone_a: list[str] = []
    open_ended: list[str] = []
    zone_block = section2.split("### 2.3")[1]
    for row in _table_rows(zone_block):
        if row[0] == "Zone A":
            for part in _split_top_level(row[1]):
                m = re.match(r"^(.*?)\s*\((.*)\)\s*$", part)
                if m:
                    members = [x.strip() for x in m.group(2).split(",")]
                    if any(x.lower().rstrip(".") == "etc" for x in members):
                        open_ended.append(m.group(1).strip())
                    zone_a.extend(x for x in members if x.lower().rstrip(".") != "etc")
                else:
                    zone_a.append(part)
    return {
        "grades": grades,
        "city_tiers": city_tiers,
        "zone_a_countries": [c.lower() for c in zone_a],
        "zone_a_open_ended": open_ended,
    }


def grade_rank(ref: dict, grade: Optional[str]) -> Optional[int]:
    if not grade:
        return None
    grades = ref.get("grades", [])
    g = grade.strip().upper()
    return grades.index(g) + 1 if g in grades else None


def city_tier(ref: dict, city: Optional[str]) -> Optional[str]:
    """"1"/"2" for a city the policy lists, "3" for any other Indian city
    (the policy's own definition), None when no city was given. The caller
    is responsible for only asking about Indian locations (INR documents)."""
    if not city or not city.strip():
        return None
    name = city.strip().lower()
    name = CITY_ALIASES.get(name, name)
    for tier, cities in ref.get("city_tiers", {}).items():
        if name in cities:
            return tier
    return "3"


def zone_of(ref: dict, country: Optional[str]) -> Optional[str]:
    """"A" for a country the policy explicitly lists, otherwise None
    (unknown). Deliberately never returns "B": the policy's Zone A includes
    "Western Europe ... etc.", so "not in the listed names" doesn't prove
    Zone B -- the checker treats an unknown zone as an unresolved dimension
    (decides only if every possible limit gives the same verdict)."""
    if not country or not country.strip():
        return None
    name = country.strip().lower()
    name = COUNTRY_ALIASES.get(name, name)
    return "A" if name in ref.get("zone_a_countries", []) else None


# ----------------------------------------------------------------- utilities

def category_clauses(clauses: list[Clause], category: str) -> list[Clause]:
    """Retrieval is a category filter, nothing smarter (24 sections):
    clauses tagged for this category plus the cross-cutting "*" clauses."""
    return [c for c in clauses if category in c.applies_to_categories or "*" in c.applies_to_categories]


def numbers_in(text: str) -> set[Decimal]:
    """Every number written in `text` (commas stripped: 1,00,000 -> 100000),
    used to prove a structured limit really appears in its clause."""
    out: set[Decimal] = set()
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            out.add(Decimal(m.group(0).replace(",", "").rstrip(".")))
        except InvalidOperation:
            continue
    return out


def load_policy_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def valid_category_ids() -> set[str]:
    return set(CATEGORY_IDS) | {"*"}
