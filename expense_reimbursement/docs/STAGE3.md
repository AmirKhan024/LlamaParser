# Stage 3 — Policy compliance engine

Given a submitted claim and each document's Stage 2 category, decide whether
every expense complies with the company policy, and cite the clause the
decision rests on. Every decision is stored, immutable, with everything needed
to replay it — which makes the table a labelled dataset in waiting.

Nothing in this stage changes Stage 1 extraction or the Stage 2 eval set.

## 1. Split of responsibility: the model selects, code computes

| The model decides | Code decides |
|---|---|
| which clause governs a unit | every amount-vs-limit comparison |
| whether qualitative conditions hold (client present? approval evidenced?) | every date/duration calculation (nights, trip days, days since expense) |
| which document values form the amount to compare, and why | every per-unit division (total ÷ nights, ÷ attendees) and rate (km × rate, % of a bill) |
| where in the document a quantity (nights, attendees, km) is printed | every aggregation (a month's phone bills across claims) |
| what information is missing | the city → tier and country → zone mapping, the limit lookup, and the **verdict** |

**Why the line is here.** A language model reads a receipt well and does
arithmetic unreliably; more importantly its arithmetic cannot be audited.
Everything a person would need to re-check a verdict is a number or a date, so
those live in ordinary functions (`policy_check.py`) with unit tests. The model
never sees the numeric limits (`policy_select.candidate_view` strips them) so it
cannot pre-judge a comparison, and it never states a verdict.

**The model's answer is untrusted input** (`policy_select.validate_selection`):

- a clause id that is not in the policy → `insufficient_information`, reason logged;
- a clause id that exists but was not a candidate for the category → same;
- `amount_refs` must be paths that exist in the extracted fields and hold numbers;
  the model also states the amount it expects, code recomputes the (signed) sum,
  and a mismatch is a hard failure — **never coerced**;
- a quantity path that does not exist is a hard failure; one that exists but
  cannot be read as a count/date is *missing*, not guessed;
- the units must cover every line item exactly once;
- an approval the model reports with no approval document or claim note to back
  it is recorded as *not evidenced*, with a note.

Hard failures are stored (they are data about the model) with
`missing_fields = ["system:<code>"]` and `hard_failure_reason`. The `system:`
prefix marks "not something the employee can answer" for Stage 4's question
generator.

If the model cannot be reached at all (no key, daily quota, network) the whole
evaluation is abandoned and **nothing is stored** (HTTP 503), so a retry is not
blocked by idempotency.

**Explanations are written by code** from the deterministic outcome, so the
sentence can never contradict the verdict. The model's own one-liner and its
amount reasoning are preserved (`check_detail.model_explanation`,
`amount_derivation`, `raw_response`).

## 2. The four verdicts and when each fires

| Verdict | Fires when |
|---|---|
| `compliant` | every check that applies passed and nothing needed is unknown |
| `violation` | a limit is exceeded, a `requires` condition is false, an `excludes` circumstance is true (a prohibition's trigger), a required proof is absent, or the clause is an unconditional prohibition |
| `needs_approval` | no violation, but the clause requires approval above a threshold (compared against the pre-division total, e.g. the whole event, not the per-person share) and none is evidenced |
| `insufficient_information` | something needed is unknown — an attendee count that was never extracted is *unknown*, not "1" — **or** a hard failure. `missing_fields` names it |

Precedence inside a unit and when rolling up: **violation > needs_approval >
insufficient_information > compliant**. A known violation outranks an unknown
elsewhere (the unknown is still reported). A reviewer's label replaces the
model's verdict in the roll-up (`effective_verdict`); both are shown.

**Unknown dimensions are not guessed.** If the city (hence tier) or vehicle type
is unknown, the checker evaluates every limit that could apply and decides only
if they all agree: ₹300 is under even the lowest cap, so the missing city does
not matter; ₹7,000 against caps of ₹6,000 and ₹8,000 is
`insufficient_information` with `missing_fields = ["city_tier"]`. A currency the
clause has no limit in (a USD receipt against an INR-only cap) is
`insufficient_information` / `fx_rate` — no conversion is attempted.

Machine-readable `missing_fields` vocabulary: `stay_nights`, `trip_days`,
`attendee_count`, `distance_km`, `document_amount`, `currency`, `fx_rate`,
`city_tier`, `zone`, `vehicle_type`, `applicable_limit`, `monthly_history`,
`percent_base_amount`, `bill_date`, `employee_grade`, `documentation:<item>`,
`system:<code>`, plus the `depends_on` names from `policy.FIELD_VOCAB` for
judgment conditions (`client_name`, `business_purpose`, …).

## 3. Evaluation unit

Line items exist → the model returns units, each covering one or more line
items (a room line under 8.1, a minibar line under 8.4); grouping is allowed
only when one clause's limit applies to the combined amount (all food lines under
a per-person cap). Code enforces that every line item is covered exactly once. No
line items → one whole-document unit. Each decision records
`evaluation_mode` (`line_item` | `document`) and `line_item_ref` (`"1"` or
`"0,2"`, zero-based indexes into the extraction's `line_items`). The document
verdict is the roll-up of its units.

## 4. Policy representation and versioning

The policy prose (`policy/expense_policy.md`) has 24 numbered *sections* holding
97 numbered *rules*. **A clause is a numbered rule** (`8.4`, `5.3.1`), not a
section: one limit and one unit per row is impossible for §8 alone (a per-night
cap, a flat family allowance, laundry per day, a GST threshold). §19's
unnumbered bullets become `19.1`–`19.8` with generated ids (flagged
`SYNTHESIZED_ID`).

`scripts/build_policy.py`:

1. **Code** cuts the markdown into clauses — ids and `verbatim_text` are never
   model output, so a citation is verbatim by construction. Section 2's grade /
   city-tier / zone tables are parsed by code into `reference_data`.
2. **The model** (one call per section, temperature 0) fills in unit, limit
   table, conditions, documentation, approval threshold and prohibition flag.
   A schema violation is retried on just the failed clauses, then one clause at a
   time; what still fails is stored as an inert stub and reported.
3. **Code validates the model against the text** and prints a review table plus
   a **CONFLICTS** section: `UNIT_UNDETERMINED`, `UNIT_DISAGREES_WITH_TEXT`
   (regex on the wording vs the model's unit), `OVERLAP` (two limits that can
   apply to the same expense), `NUMBER_NOT_IN_TEXT`, `UNCAPTURED_NUMBERS`,
   `LIMIT_EQUALS_APPROVAL_THRESHOLD`, `PROHIBITION_WITHOUT_TRIGGER`,
   `STAGE1_GAP` (a condition depends on something Stage 1 does not reliably
   extract — see `policy.FIELD_VOCAB`), `UNSTRUCTURED`, `TABLE_NOT_STRUCTURED`,
   `CATEGORY_SCOPE`, `SYNTHESIZED_ID`. Nothing is resolved silently.
4. **Nothing is written without `--confirm`.** The JSON records
   `human_reviewed` / `reviewed_by`, and `seed_policy.py` warns if it is false.

### How `policy_v1.json` was actually produced (read this)

The structuring model's answers for sections 1 and 3-23 come from a first pass with
prompt `structure-v1` on `openai/gpt-oss-120b`. Groq's free tier (200k tokens/day on that
model) was then exhausted, so the improved `structure-v2` prompt could not be re-run and
section 24 was never structured. `scripts/build_policy.py --replay structure-v1` rebuilt the
JSON from those cached answers using the current validator, and
`policy/policy_v1.overrides.json` applies **32 manual corrections** (each with its reason, the
replaced value, all copied into `build_meta.manual_edits`): thresholds the model had turned
into limits (10.4, 11.2, 15.1, 15.3, 20.1, 12.3), submission windows (21.x), approval routing
(23.x), quantity misuse (3.2, 21.4), an AND that should be an OR (12.2), unconditional
documentation items that would make every unit "unknown" (5.3, 8.6, 9.1, 10.4, 12.x, 16.x),
and section 24 as informational. **These corrections were made by the Claude session that
built Stage 3, not by a human reviewer; `human_reviewed` is `false`.** The full review table
and CONFLICTS report are in `policy/policy_v1.review.txt`. Read them against the policy
before trusting a verdict; then re-run `scripts/build_policy.py --confirm --reviewed-by NAME`
(the JSON is regenerated deterministically from the cache + overrides, or from a fresh model
run once quota allows) and seed a new version.

Extensions to the spec's clause schema (all forced by the real policy):
`limit_unit` also takes `per_ride`, `per_item`, `per_km`, `per_event`,
`percent_of_amount` (and `unknown`, stored when a limit's unit cannot be
determined — the checker refuses to apply it); `limit_table` holds one entry per
distinct limit (grade × city-tier, INR vs USD…) with a `when` selector, so the
spec's scalar `limit_amount`/`limit_currency` are filled only for a
single-limit clause; `limit_kind` (`amount|percent|count`), `limit_inclusive`
(`under ₹2,000` excludes ₹2,000), conditions can be `numeric` (code computes:
nights, days, amount, persons, km, grade rank, days since expense) or
`judgment` (model answers), with `any_of` for alternatives;
`documentation_required` items carry an optional amount threshold;
`requires_approval_above` is a currency → amount map (0 = always).

**Versioning.** `policy_versions` (label, source hash, reference tables, build
metadata) → `policy_clauses` (immutable once a version exists; a changed policy
is a *new* version, and `seed_policy.py` refuses an existing label). The DB is
the runtime source of truth; `policy/policy_v1.json` is the reviewable seed. A
decision stores `policy_version` and `prompt_version` (the prompt is a file,
`prompts/policy_select_<version>.md`; its sha256 is stored in `raw_request`), so
the exact clause text and instructions behind any verdict can be reproduced.
`policy_clauses.embedding vector(384)` exists for a future semantic stage and is
never populated or queried; retrieval today is `category in applies_to_categories
or "*"`.

## 5. Storage: the capture layer that becomes the eval set

`policy_decisions` — one row per evaluated unit per run, **immutable**: a
database trigger rejects any UPDATE that touches a column other than the five
reviewer columns and rejects every DELETE. Re-evaluating (`?force=true`, a new
policy version, a new prompt version) writes new rows with a higher
`evaluation_seq`; nothing is overwritten. Each row keeps the judged
`extraction_id`, `category_used` + `category_confidence` + `category_method`
(rules | classifier | llm | hybrid | **employee**, i.e. a human-corrected
category), the clause id and verbatim text, limit / unit / amount compared /
derivation / comparison result, `missing_fields`, `check_detail` (dimensions,
quantities, limit candidates, condition results, aggregation members),
`raw_request` (the exact payload the model saw), `raw_response`, latency and
tokens (recorded on the run's first unit only, so a SUM is correct), and
`cache_hit`.

Reviewers label through `POST /api/decisions/{id}/override`
(`human_verdict`, optional `human_clause_id`, `human_note`); each override is
also an `audit_events` row that keeps the previous label.

### From capture layer to eval set

Once real claims have flowed through and been labelled:

1. **Gold set** = decisions with a `human_verdict` (add a sample of unlabelled
   ones the reviewers explicitly agree with). Use the *latest* run per
   (document, unit); use `raw_request` to replay any row.
2. **Score the halves separately**, which is the point of the split: model
   selection (`clause_id` vs `human_clause_id`; validation failures per
   `hard_failure_reason`; `model_confidence` calibration) and the deterministic
   check (recompute `check_unit` from stored `check_detail`; a mismatch is a
   code bug, never a model error).
3. **Attribute errors upstream**: group wrong verdicts by `category_method` and by
   whether a category correction preceded the decision. Bad verdicts concentrated
   in `rules`-labelled rows are Stage 2's problem, not Stage 3's.
4. **Compare versions** with `scripts/reevaluate_claims.py --policy-version v2`
   / `--prompt-version v2`: old and new runs sit side by side and reviewer labels
   on old rows stay valid for the same units.
5. Treat labels made by the same person who wrote the prompt with the anchoring
   caution Stage 2's RESULTS.md applies to its own labels: a blind sample first.

## 6. API and UI

Routes follow the existing `/api` convention and response style (string ids,
string money, `HTTPException` details) and run as the seeded employee behind
`get_current_employee` — there is no auth yet, so **override has no role check**
and `overridden_by` is the seeded employee.

- `POST /api/claims/{id}/evaluate[?force=true&policy_version=&prompt_version=]` —
  idempotent per (claim, policy_version, prompt_version); returns decisions,
  per-document and claim roll-ups, `reused`, `stale` (an extraction changed after
  the run), `skipped` (supporting documents are evidence, not expenses).
- `GET /api/claims/{id}/decisions` — the latest run.
- `POST /api/decisions/{id}/override`.

UI: the claim page shows a read-only summary (overall badge + link). The policy
page (`#/claims/<id>/policy`) shows, per document and per line item, the verdict
badge, **the verbatim clause**, the comparison as `₹1,450 vs ₹1,200 limit (per
trip)`, the explanation, what is missing, how the amount was derived, and the
override control. A decision with no clause says so instead of showing a bare
verdict. The controls are on their own page because Stage 1's e2e suite requires
a submitted claim page to contain no inputs or buttons.

## 7. Known limits (read before building on this)

- **One governing clause per unit.** Secondary rules that also apply — the GST
  invoice threshold (8.6), receipts (20.1), submission windows (21.x), the policy
  exception rule (23.5) — are only checked if the model picks them as *the*
  clause. The checker already supports them (`days_since_expense`, documentation
  thresholds); a small extension is to run a code-selected set of deterministic
  universal clauses on every unit.
- **Monthly aggregation is per employee + category + currency + calendar month + CLAUSE**
  (documents in the current claim plus submitted claims, counted only if their latest
  evaluation used the same clause, so mobile 12.1 and broadband 12.2 are not pooled).
  A peer never evaluated has no clause yet: it is NOT counted and is listed in
  `check_detail.aggregation.unattributed_documents_not_counted`, so evaluate all of an
  employee's claims for the month before trusting a per-month verdict. "Per team" (11.3)
  is not determinable and counts the employee's own team events.
- **A multi-limit clause never compares a null limit.** An unknown dimension (grade, city
  tier, vehicle...) gives `insufficient_information` naming it unless every possible limit
  agrees; a per-unit clause with no limit values gives `policy_limit_missing`.
- **Informational/unstructured clauses (all of section 24, 23.x...) are never candidates.**
  If the model names one, the unit fails loudly (`system:not_a_candidate`,
  `insufficient_information`); no neighbouring clause is substituted.
- For `per_km` and `percent_of_amount` the stored `limit_applied` is the COMPUTED total
  (rate x km, % x bill), and the UI labels it "km rate x distance" / "% of bill amount".
- **Class of travel by grade (3.4, 3.5, 4.1) and vehicle tier (5.1) are judgment
  conditions** read from the clause's table by the model — categorical lookups,
  not arithmetic, but not code-verified either.
- **Approval routing by claim total (23.x)** is informational here (Stage 6).
- **Currency conversion is not attempted** (22.x); a mismatch is `fx_rate`.
- **Stage 1 does not extract** attendee count, client name, business purpose,
  vehicle type, GSTIN on hotel bills, trip dates, flight details, or who a bill is
  billed to; they surface as `missing_fields` or as judgments from
  `additional_fields`/the claim note. Expect many `insufficient_information`
  verdicts until Stage 4+ asks the employee.
- **Free-tier throughput.** Groq's 200k-tokens/day cap on gpt-oss-120b is
  roughly 25–40 documents a day at 4–8k tokens each.
- The model sees the extracted fields, never the OCR markdown.
