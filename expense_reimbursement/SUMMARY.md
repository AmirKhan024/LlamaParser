# Stage 1 closeout: expense reimbursement extraction pipeline + claim app

This is the final record of what stage 1 actually is, as of the commit that
added this file. It exists so "what does stage 1 do" has one place to check
instead of drifting across conversation history. If the code changes,
update this file in the same commit -- don't let it go stale.

Stage 1 is now the full employee-facing loop, not just the extraction
pipeline: create a claim, upload documents to it, review/correct each
one's extracted fields, confirm, submit. Everything persists in
Postgres. See `README.md` for setup.

## Extraction pipeline

```
PDF --[parse.py: LlamaParse, tier=agentic]--> markdown + raw JSON
    --[extract.py: one Groq call, gpt-oss-120b, JSON mode]--> document_type + raw fields
    --[validate.py: build_claim]--> typed Pydantic claim (schema chosen by document_type)
    --[validate.py: validate_claim]--> arithmetic checks + GSTIN format checks
    --[validate.py: check_completeness]--> completeness warnings (local_conveyance_form only)
    --[review_view.py: build_review_view]--> short employee-facing view
```

Output per document: `clean_json` (the full typed claim), `additional_fields`
(anything that didn't fit a named schema field -- never silently dropped),
`extraction_notes` (any label/value fix the model made, logged), a list of
validation results, completeness warnings, and a `review_view` (per-type
editable fields with plain labels, an optional collapsible items/trips
section, plain-language warnings, and `needs_review`/`needs_confirm` flags
-- see "Data model, API, and UI" below for what actually consumes this now).

Document types with a dedicated schema: `telecom_bill`, `restaurant_bill`
(now with a `line_items` array), `local_conveyance_form`,
`approval_correspondence`. Everything else falls back to `generic_receipt`
via `GenericClaim` (which also has `line_items`).

Validation performed in code, never asked of the LLM:
- Amount-string parsing (`parse_amount`) handling both Indian and
  international comma grouping, flagging (not guessing at) ambiguous
  grouping.
- Arithmetic identities: `subtotal + tax == total` (telecom),
  `subtotal + cgst + sgst == grand_total` (restaurant),
  `sum(kms) == total_kms` and
  `conveyance + daily_allowance + vehicle_maintenance + mobile_allowance == total_claimed`
  (local conveyance).
- GSTIN format (`validate_gstin`), run generically on any field whose name
  contains "gstin"/"gst_no" across any document type -- not hardcoded to one
  schema. Placeholder values ("-", "N/A", "none", "nil", "null", "") are
  treated as legitimately absent, not a format error.
- Completeness check (`local_conveyance_form` only, intentionally narrow
  pattern-matching, not a general solution): flags a number sitting on a
  known totals-section label's markdown line that never made it into the
  final output.

## Data model, API, and UI

Postgres (`pgvector/pgvector:pg16` in Docker, `docker-compose.yml`),
SQLAlchemy 2.0 typed models (`models.py`) + Alembic (`alembic/`). A
claim is not a document: one claim holds several documents (a
conveyance form + a phone bill + a manager's approval email, say).

- `employees` / `claims` / `documents` -- straightforward, one row per
  entity. `claims.total_amount` is recomputed (from confirmed
  documents' latest extraction amount) after every save/confirm/
  revert/delete/submit, not derived on read.
- `extractions` -- **append-only** versions of a document's fields. The
  AI version (`source="ai"`, always version 1) is never updated; every
  employee save adds a new row (`source="employee"`). Also carries
  denormalized `vendor_name`/`bill_date`/`amount` columns, unused until
  a later stage's fraud checks.
- `check_results` -- one row per `validate_claim()` check, per
  extraction version.
- `corrections` -- one row per changed field per save
  (`field_path`/`ai_value`/`employee_value`), diffed against the AI
  version specifically (not the previous version) -- replaces the old
  `corrections.jsonl` idea entirely.
- `audit_events` -- append-only, one row per state change (created,
  uploaded, extracted, edited, confirmed, removed, submitted, failed).

All DB access goes through `repository.py`; nothing else touches
SQLAlchemy sessions directly. No login yet -- every request runs as one
seeded employee ("Nasir Ahmed Khan") via `get_current_employee()` in
`server.py`, a single dependency stage 5 can swap for real auth without
touching anything else.

`server.py` (FastAPI) exposes claim/document CRUD, upload (sha256
duplicate detection, 15 MB / PDF+PNG+JPG+WEBP limit), a background
extraction pipeline per upload, a dry-run `/validate`, save/confirm/
revert/retry/delete, and submit (blocked unless every document that
actually needs confirming -- i.e. not a read-only approval email -- is
confirmed; every mutation on a submitted claim returns 409).
`PIPELINE_MODE=fake` replays a cached `outputs/*_result.json` matched
by sha256 instead of calling LlamaParse/Groq, for local dev and tests.

`static/index.html` is the employee UI: plain HTML/CSS/JS, no build
step, hash-routed (`#/`, `#/claims/<id>`, `#/documents/<id>`). Each
document type shows only what an employee needs (see `review_view.py`'s
per-type field builders) -- confidence scores, `document_type` codes,
GSTIN, account/invoice numbers, `additional_fields`, `extraction_notes`,
raw check formulas, and tokens/cost never reach it, because
`review_view.py`'s output doesn't include them in the first place, not
because the UI happens to skip rendering them.

## Tested, with results

**App (`tests/`)**: 13 pytest API tests against a real Postgres
(`expense_test`, `PIPELINE_MODE=fake`) -- full create-upload-edit-
confirm-submit flow, duplicate upload, editing a non-editable field,
every mutation rejected on a submitted claim, submitting with an
unconfirmed or zero documents, a correction row's exact field_path, the
AI extraction row never mutating across several saved versions, and
retry after a forced failure. Plus a Playwright end-to-end suite that
clicks every button on all three screens in a real browser against a
real `uvicorn` process, reloading after each change to confirm it
persisted, with screenshots at 1280px and 390px saved to
`test_screenshots/` (gitignored) and reviewed directly. All 15 tests
green as of this commit. The real (non-`fake`) pipeline was also run
once end to end against `uploads/may26_mobile.pdf` with real LlamaParse
+ Groq keys, which is what surfaced the nested-event-loop bug below.

**3 real documents** (`uploads/`, re-run in full for this closeout, fresh
LlamaParse + Groq calls, not cached):

| Document | document_type | Checks | Notes |
|---|---|---|---|
| `may26_mobile.pdf` | `telecom_bill` | 2/2 PASS | `subtotal+tax==total`; `gstin_format:customer_gst_no` now passes ("-" correctly treated as absent) |
| `May-26 Local conveyance.pdf` | `local_conveyance_form` | 2/2 PASS | Both arithmetic identities hold; completeness check still correctly flags a `'5886'` value on the Daily Allowance row that never made it into any output field |
| `May-26 mail approval.pdf` | `approval_correspondence` | 0/0 (none defined) | Sender/recipient/subject/approval_status/related_form_title all populated, not forced into a form schema it isn't |

**15 CORD-v2 samples** (`outputs/cord_eval/`), final committed numbers:

| Field | Accuracy |
|---|---|
| grand_total | 14/15 (93%) |
| subtotal | 9-10/11 (82-91%, varies run to run -- gpt-oss isn't perfectly deterministic even at temperature 0) |
| tax_total | 3/6 (50%) |
| vendor_name | ground truth has this field in 0/15 samples (not scored) |
| line items | precision 1.00, recall 1.00 (matched=32, missed=0, extra=0) |

document_type classification: 15/15 landed on a plausible retail-receipt
type (`generic_receipt` or `restaurant_bill`); 0 forced into an
India-specific type. CORD has no ground truth for document_type itself,
so this is a heuristic read, not a scored metric.

## Real bugs found and fixed during development

All found by actually running the pipeline on real or real-shaped data, not
by inspection alone:

1. **kms/amount swap** -- `total_kms` and `total_conveyance_amount` got
   swapped by the model on one run; caught by an arithmetic check
   specifically designed to detect a value matching the wrong total.
2. **`extraction_notes` string -> character-array corruption** -- the model
   sometimes returned one string instead of a JSON array; Python/Pydantic
   silently treats a string as an iterable of its own characters
   (`list("Extracted X")` -> `["E","x","t",...]`), not an error. Fixed with
   explicit type coercion before validation.
3. **Dropped `'5886'` value** -- a real number on the Daily Allowance row of
   the conveyance form that ended up in neither `clean_json` nor
   `additional_fields`. Motivated the completeness check.
4. **Comma-in-Decimal crash** -- `Decimal("24,000")` raises; the model is
   (correctly) instructed to keep amounts exactly as printed, commas
   included, so every Decimal-typed field (later extended to `LineItem`'s
   `unit_price`/`total` too) needs comma-stripping before Pydantic sees it.
5. **GSTIN placeholder false positive** -- fixed in this pass. `"-"`, the
   real value a bill prints for "no GST registration," was being reported
   as an invalid GSTIN format instead of recognized as legitimately absent.
6. **`currency: null` crash** -- a required `currency: str` field broke
   when the model returned an explicit `null`; fixed by treating an
   explicit null the same as an absent key so the schema default applies.
7. **Non-string values in `additional_fields`** -- typed `dict[str, str]`,
   but the model occasionally put a raw list there (e.g. line items, for a
   document type with no `line_items` field of its own); now JSON-encoded
   defensively instead of crashing validation.
8. **`GenericClaim` missing from the prompt's visible schema list** -- it
   gained a `line_items` field, but the prompt-building code only described
   fields for document types with an entry in `SCHEMA_BY_TYPE`, and
   `generic_receipt` wasn't in it -- so the model was never told the field
   existed for that type. Silently defeated line-item extraction for every
   `generic_receipt`-classified document until fixed.
   **This first fix was incomplete**: it added `generic_receipt` to
   `SCHEMA_BY_TYPE`'s iteration, but `taxi_receipt`, `hotel_invoice`,
   `fuel_receipt`, and `unstructured_proof` -- every other type that
   falls back to `GenericClaim` via `schema_for()` without its own
   `SCHEMA_BY_TYPE` entry -- had the exact same bug, undetected until a
   real hotel receipt (4 line items, $780.75) came back with `Items (0)`.
   Properly fixed by building the prompt's field list from
   `schema_for(t)` for every `DocumentType`, not from `SCHEMA_BY_TYPE`'s
   own entries -- see `tests/test_extract_prompt.py`, which would have
   caught both the original bug and this incomplete fix.

Found while building the app layer (all caught by actually driving the
running app -- browser or a live `uvicorn` process -- not by inspection):

9. **Corrections read across all extraction versions, not just the
   latest** -- after a revert, the (now-superseded) correction from the
   edit before it was still shown, so "Undo my changes" didn't visibly
   clear the changed-field marker.
10. **`check_completeness` over-flagged** -- a date's day/month/year
    digits sitting on the same markdown line as a totals label got
    treated as dropped amounts, and a captured value was only compared
    to a flagged number after stripping the number's commas, not both
    sides -- so a captured `"1417.18"` didn't match a printed `"1,417.18"`.
11. **`GET .../file` defaulted to `Content-Disposition: attachment`** --
    silently blocked every inline document preview in the review screen,
    in any browser, not just headless test Chromium.
12. **The changed-field UI marker only checked in-session unsaved
    edits** -- the blue highlight and struck-through AI value vanished
    on reload after a real save, because nothing checked the document's
    persisted `corrections` too.
13. **A CSS Grid `min-width: auto` overflow** -- widening a table
    column to stop it truncating a date leaked through the review
    layout's grid columns and forced the whole page to scroll
    horizontally at 390px.
14. **LlamaParse's sync `.parse()` detects "nested async"** when called
    from a background thread in a running `uvicorn` process, even a
    thread the app owns outright -- not a `parse.py` bug, but a real
    production blocker for the real (non-`fake`) pipeline path that only
    showed up when it was actually run against a live server rather
    than the CLI (`run.py`), which never has this problem since nothing
    else's event loop is running alongside it.

## Second bug-fix pass: found by using the app after that

**Tolerance was too loose.** `TOLERANCE = Decimal("0.5")` was right for
genuine rupee round-off but meant any employee edit within ±0.49 of the
AI value passed every check silently -- reported directly: editing
Hotel-Receipt.png's amount from 780.75 to 780.70 (a $0.05 change) went
through with no flag at all. Default tightened to 0.01; 1.00 is only
used when the document's own markdown (or an extracted field) actually
mentions "round off"/"rounding" -- never inferred from the check itself
being off by less than a rupee, which would make the exception
circular. Verified before changing anything else, per instruction:
re-ran the CORD eval and all 4 real documents fresh.

- **CORD eval**: `grand_total 14/15 (93%)`, unchanged from the
  documented baseline -- expected, since `eval_cord.py`'s field-value
  scoring never calls `_isclose`/`TOLERANCE` at all (confirmed by
  reading it), so this tolerance change literally cannot affect it. The
  subtotal (64% vs the previous run's 82-91%) and tax_total (33% vs 50%)
  numbers moved, but that's the same run-to-run LLM variance already
  documented above, not this change -- nothing here is tolerance-driven.
- **The 4 real documents**: no previously-passing check now fails.
  `may26_mobile.pdf` (diff 0.00) and `May-26 Local conveyance.pdf`
  (diff 0) both still pass at the tight 0.01 tolerance because they were
  already exact matches. `Hotel-Receipt.png`'s one failing check
  (items-sum vs the tax-inclusive amount, diff 86.75) was already
  failing by two orders of magnitude more than even the *old* 0.5
  tolerance allowed -- unrelated to this change, same limitation
  documented in the first bug-fix pass above.

**Employee money edits need a reason when they contradict the bill.**
A tighter tolerance alone still let an employee silently save a money
edit that broke the bill's own arithmetic, as long as they didn't mind
the warning staying up -- nothing stopped them from confirming anyway.
Confirm now requires a >=5 character reason whenever a money-field
edit (the same allowlist used for display formatting, plus line-item
`unit_price`/`total`) leaves an arithmetic check failing (gstin format
checks excluded -- those aren't arithmetic). An edit that instead FIXES
a check that was already failing on the AI version needs no reason --
that's a correction, not a contradiction. Enforced server-side (422,
`server._reason_required_for`), not just in the UI: `Correction` gained
`reason` (nullable) and `direction` (increase/decrease/none) columns.
One inline reason box appears under the warnings; Confirm stays
disabled until the reason clears 5 characters; no browser dialogs.

**Currency warning was noise.** The tolerance/reason work above didn't
touch this, but it was reported in the same pass: a document with NO
currency marker at all (e.g. a plain INR conveyance form) was treated
the same as a genuine conflict -- both left currency `null` and warned.
Most bills in the company's own currency never print a symbol, so this
warned on nearly every one of them. `validate.resolve_currency` now
only leaves currency null (with a warning) on an actual conflict -- two
or more distinct currency markers in the same document. No marker at
all falls back to the new `COMPANY_CURRENCY` env var (default `INR`),
no warning. Verified against the real conveyance form's own markdown
(zero currency markers) end-to-end through the API: currency comes
back `"INR"`, no currency warning.

**Generic checks (subtotal + tax == amount) were non-deterministic.**
`GenericClaim` (the fallback schema for hotel/taxi/fuel/unstructured/
generic receipts) had no dedicated `subtotal`/`tax` schema fields --
those values, when the model found them at all, only ever landed in
`additional_fields` under whatever label it felt like using that run,
so the same document sometimes produced a check and sometimes didn't.
Added explicit optional `subtotal`/`tax` fields to `GenericClaim`
(`extract.py`'s prompt already lists every schema field by name for
`generic_receipt`, so this alone gets the model asked for them
directly); `_validate_generic_claim` now reads `claim.subtotal`/
`claim.tax` first, falling back to `additional_fields` only when those
are missing (older extractions, or a model that still buries the value
despite being asked). Verified with 3 fresh real-pipeline runs of
`test_documents/Hotel-Receipt.png` (`PIPELINE_MODE=real`, no cache):

  | Run | subtotal | tax | amount | check present | check passed |
  |-----|----------|-----|--------|---------------|--------------|
  | 1   | 694.00   | 86.75 | 780.75 | yes | yes |
  | 2   | 694.00   | 86.75 | 780.75 | yes | yes |
  | 3   | 694.00   | 86.75 | 780.75 | yes | yes |

  `subtotal`/`tax`/`amount` and the check itself were identical and
  present in all 3 runs -- fully deterministic. `additional_fields`
  still varied run to run (key casing, which extra fields like
  address/phone showed up) exactly as before, which is expected and
  harmless now that the arithmetic check no longer depends on it.

## Third pass: self-repair, suggested fixes, grouped warnings

**Self-repair retry.** `extract.extract_claim_with_repair`: after the
first extraction, run `validate_claim`. If any arithmetic check fails
(`gstin_format` excluded), make ONE more Groq call with the same
markdown, the first JSON, and the failed checks' names/details, asking
the model to re-read only the fields involved and return the full
corrected JSON. Kept only if it passes strictly more arithmetic checks
than the first; never retried more than once regardless. `Extraction`
gained `repair_attempted`/`repair_accepted`/`first_attempt_tokens`/
`repair_attempt_tokens` columns. `SELF_REPAIR_ENABLED` env var (default
true) turns it off for the reliability comparison below.

**Suggested fix when checks still fail.** `validate.suggest_fixes`
offers a fix only when it's both implied by the document's own
arithmetic AND the exact number is printed somewhere on the document --
never invented. Implemented for the three swap/misread patterns this
project has actually seen: local_conveyance_form's total_kms/
total_conveyance_amount, telecom_bill/generic's total-vs-subtotal+tax,
restaurant_bill's grand_total. One "Did you mean: ...?" box with an
Apply button; Apply is a normal employee edit (still shown as changed,
still re-validated), never automatic.

**Warnings: fewer and grouped.** All employee warnings already lived in
one box (`#warnings-container`); this pass added the deduplication:
a check-driven or completeness sentence about the exact same field/
number a suggestion already covers is dropped, and completeness
warnings are hidden from the employee entirely once every arithmetic
check passes (still computed -- `check_completeness` is unchanged --
just not shown; reserved for a later approver-facing stage). See the
corrected note on the conveyance form's real completeness-warning
example ("Real-document check" section above): "5886" is very likely
`981 x 6`, a per-km rate calculation printed on the form, not a
dropped second daily-allowance figure -- a Stage 3 policy question,
not an extraction miss, and exactly the kind of non-actionable warning
this pass stops surfacing once the real (arithmetic) checks pass.

**Review page layout.** The document review page opts into a wider
page (up to 1600px, vs. the site-wide 900px) with the document at
~55% (sticky, full viewport height minus the action bar) and the form
at ~45%. The trips table now shows every column, including Km, with
no horizontal scrollbar at >=1280px wide (`table-layout:fixed` with
explicit column widths, instead of scrolling by design as before); its
cells are plain text until clicked, focused, or activated with Enter,
swapping to a real `<input>` only then -- not a permanently bordered
input per cell. A failing trips check highlights the Km column header
and the Total km field together. Verified with real screenshots at
1440px and 390px, before (the pre-change layout, checked out briefly)
and after: before, Km was cut off the trips table entirely at both
widths and ~700px sat unused at 1440px; after, every column fits with
no scroll at both widths.

**Code issues found in review.**
- Restart recovery now marks EVERY document still "processing" as
  failed at startup, not just ones stuck past a 5-minute age
  threshold -- a single-process server has no in-flight pipeline that
  legitimately survives a restart. Switched to timezone-aware UTC
  datetimes at the touched call sites.
- Confirm now requires status in (ready, needs_review) -- previously
  it only rejected an already-confirmed document, so a `processing` or
  `failed` document (no real extraction behind it) could be silently
  marked confirmed.
- The pipeline's own terminal status writes go through a new atomic
  `UPDATE ... WHERE status = 'processing'`, not a read-then-write, so
  a slow LlamaParse/Groq call finishing after the employee already
  confirmed or removed the document can never overwrite that outcome.
- Save+confirm+recompute (and reopen+recompute, revert+save+recompute,
  remove+recompute) are each now one transaction, one commit, instead
  of 2-5 separate commits for one user action -- a failure partway
  through now leaves no trace at all. Building this surfaced a real
  bug: `session.refresh()` right after a not-yet-committed change
  silently discarded it (nothing had been flushed to the DB for the
  refresh's SELECT to see) -- fixed by flushing first.
- "Remove document" is now a soft delete (`status='removed'`):
  extractions, corrections, the audit trail and the uploaded file are
  all kept; the document disappears from the claim's list/count/
  totals and from the duplicate-sha check (backed by a partial unique
  index at the DB level, not just an application check, so a removed
  document's sha256 can't block a fresh re-upload either).

**Reliability numbers: self-repair retry, "May-26 Local conveyance.pdf",
5 real-pipeline runs each way (`PIPELINE_MODE=real`, no cache):**

  | Repair | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | Correct |
  |---|---|---|---|---|---|---|
  | Disabled | 981 / 5200 (yes) | 981 / 5200 (yes) | 5200 / 981 (no -- the swap) | 981 / 5200 (yes) | 981 / 5200 (yes) | 4/5 |
  | Enabled | 981 / 5200 (yes) | 981 / 5200 (yes, repaired) | 981 / 5200 (yes) | 981 / 5200 (yes, repaired) | 981 / 5200 (yes) | 5/5 |

  ("Correct" = both `total_kms == 981` and `total_conveyance_amount ==
  5200`, the values the form itself prints.) With repair disabled, the
  known total_kms/total_conveyance_amount swap reproduced once in 5
  runs, exactly as expected from earlier observations. With repair
  enabled, the first attempt still failed an arithmetic check twice
  (runs 2 and 4) -- but the repair call caught and corrected both,
  landing at 5/5 correct. Gathering this also surfaced and fixed a real
  bug: on one repair attempt, Groq's JSON-mode validation rejected the
  retry call outright ("Failed to validate JSON") and raised, which
  would have crashed the entire extraction over a failure in the
  OPTIONAL second attempt -- `extract_claim_with_repair` now catches
  that and falls back to the (already valid) first result instead.

## Bug-fix pass: 8 bugs found by actually using the app

After stage 1 shipped, using it for real (uploading a real dollar hotel
receipt among other things) surfaced 8 more bugs, fixed in this order,
each with its own commit and test: confirmed documents still showing AI
warnings and staying editable; line items never extracted for the
`GenericClaim` fallback types (the same class of bug as #2/#8 above, now
for `hotel_invoice`/`taxi_receipt`/`fuel_receipt`/`unstructured_proof`);
currency silently defaulting to INR; no arithmetic check at all for
generic receipts; an empty/non-fitting items table; corrections
collapsed to one row per whole array instead of per cell; background
processing not surviving a server restart; and this section itself --
a broader real-document check.

Two more real bugs surfaced incidentally while fixing the above, fixed
in the same commits: the Trips table's Place/Purpose/Client columns
have been silently blank for every `local_conveyance_form` document
ever shown, because the model's actual field names
(`place_of_visit`/`purpose_of_travel`/`client_name`) never matched what
`review_view.py` assumed (`place`/`purpose`/`client`) -- fixed by
normalizing known aliases on display and before diffing corrections,
not by changing the extraction prompt. And the money-vs-plain-value
display regex matched any field key containing "total", so a confirmed
conveyance form showed "Total km: 981" as "₹981.00" -- fixed with an
explicit allowlist of money field keys.

### Real-document check: the real pipeline, run fresh, on all 4 documents

`test_documents/Hotel-Receipt.png` (copied from `storage/` -- it's the
exact file uploaded through the running app earlier, sha256-verified
against the `documents` row already in the database) plus the 3 PDFs in
`uploads/`, all four run through the real (non-`fake`) pipeline in one
sitting -- fresh LlamaParse + Groq calls, not the cached results:

| Document | document_type | Currency | Amount | Line items | Checks |
|---|---|---|---|---|---|
| `may26_mobile.pdf` | `telecom_bill` | INR | 1417.18 | n/a | 2/3 |
| `May-26 Local conveyance.pdf` | `local_conveyance_form` | **null** | 8110 | n/a | **0/2** |
| `May-26 mail approval.pdf` | `approval_correspondence` | n/a | n/a | n/a | 0/0 (none apply) |
| `Hotel-Receipt.png` | `hotel_invoice` | USD | 780.75 | **4** | 0/1 |

Line items for the hotel receipt: 4/4 extracted (Room King Suite ×3
nights $567.00, Room Service $45.00, Parking ×2 $50.00, Mini Bar
$32.00) -- confirms bug 2's fix; this document type returned `Items (0)`
before it.

### Honest state after this run

- **The local conveyance form's real extraction problem, corrected**:
  earlier notes here mischaracterized this as "no schema field to put a
  second number in." That's wrong on both counts:
  - "Total Conveyance Amount (5200 and 981)" is not two conveyance
    amounts -- 981 is the trip km total, and `LocalConveyanceForm`
    already has a dedicated field for it (`total_kms`). The real
    problem is the model sometimes files 981 under the wrong field
    (`total_kms` <-> `total_conveyance_amount` swapped, or one of them
    left null) -- an extraction miss, not a missing field. This is what
    items 1 (self-repair retry) and 2 (suggest_fixes) above now catch
    and, in item 2's case, offer a one-click fix for.
  - "Daily Allowance (1560 and 5886)" is likely not a second daily
    allowance at all: 5886 = 981 x 6, almost certainly a per-km rate
    calculation (Rs 6/km) printed on the form near that row, not a
    dropped allowance figure. Whether the claim should even account for
    a per-km rate is a Stage 3 reimbursement-policy question, not an
    extraction gap -- `check_completeness` flagging it as a "possible
    dropped value" is a reasonable, honest guess from a generic
    heuristic (a number near a label that isn't in the output), not
    evidence that a field is missing. This is the completeness-warning
    case item 3 above addresses: it's the kind of warning that's only
    worth showing when there's still an arithmetic reason to look at
    the document at all, and is now suppressed once the real (kms/
    conveyance) checks pass.
- **This exposed a real trade-off in the currency fix (bug 3) worth
  flagging, not just a bug**: the conveyance form's raw markdown has *no*
  currency symbol anywhere -- it's an internal Indian company form that
  prints bare numbers on the assumption everyone reading it knows it's
  rupees. `detect_currency_from_markdown` correctly returns null here
  (nothing to detect), exactly as designed -- but the practical
  consequence is that this exact, very ordinary style of document will
  show "Couldn't tell which currency this bill is in." on every single
  submission, not just the rare genuinely-ambiguous one. The alternative
  (assume INR when nothing else is found) is exactly the silent-default
  behavior bug 3 was written to eliminate, so this wasn't changed without
  asking -- flagging it as a real UX cost of the fix as specified.
- **The extraction is not perfectly reproducible run to run**, even at
  temperature 0. Re-running `may26_mobile.pdf` fresh surfaced a `gst_no`
  additional_field (a likely OCR "O"-for-"0" misread, `27AAACB210OP1ZX`)
  that an earlier run never extracted at all, failing a `gstin_format`
  check that previously didn't exist for this document -- invisible to
  the employee either way (GSTIN is never shown, and non-actionable
  checks don't drive `needs_review`), but a genuine difference in what's
  in the database depending on which run produced it. Likewise,
  Hotel-Receipt.png's subtotal/tax additional_fields (present in the
  cached result from the original bug report, used throughout this
  summary's other examples) were simply absent from this run's
  extraction -- which is why the items-sum check compared against
  `amount` (the tax-inclusive total) instead of `subtotal`, and legitimately
  failed (items sum to $694.00, pre-tax; amount is $780.75). **This is a
  real limitation of bug 4's "opportunistic" design**: when the model
  doesn't happen to extract a subtotal into `additional_fields` on a
  given run, the items-sum check falls back to comparing against the
  tax-inclusive total and will read as "wrong" for any receipt with tax,
  even though nothing is actually wrong. Not fixed here -- flagging it
  honestly rather than papering over it with a heuristic guess at what
  the tax rate might be.

## Explicitly NOT done -- out of scope for stage 1

- **No auth.** Every request runs as one seeded employee
  (`get_current_employee()` in `server.py`); anyone who can reach the
  server can see and edit everything. Fine for local/demo use, not for
  anything else.
- **No manager approval.** A submitted claim just sits at
  `status="submitted"` -- no notification, no approve/reject action, no
  approver-facing view of `note_to_approver` or the corrections list
  beyond what the employee themself can already see.
- **No policy engine.** Nothing checks a claim against spending limits,
  per-category rules, or receipt requirements.
- **No fraud or duplicate-claim detection.** `extractions.vendor_name` /
  `bill_date` / `amount` are denormalized specifically to make this
  possible later; nothing queries them yet, and `vector` is enabled in
  the first migration but no column uses it.
- No testing on scanned, blurry, rotated, or handwritten documents. All
  test documents (3 real bills, 15 CORD samples) are clean digital or
  well-scanned printed receipts.
- No testing at scale. A handful of claims/documents have gone through
  the app, ever. Nothing about failure modes, cost, or latency at 100+
  claims or concurrent uploads is known.
- No dynamic field-name generation, canonicalization/ontology layer, or
  fuzzy cross-schema matching -- deliberately out of scope from stage 1's
  original brief, still true.
- Editing a table field (a trip row, a line item) still only supports
  replacing the whole array in one PUT -- there's no per-cell field_path
  edit endpoint. Corrections are per-cell now (diff_values), but only
  because the edited array is diffed against the AI version server-side;
  there's still no way for the client to send a single-cell edit
  directly.
- No stable identity (id/UUID) per trip or line item, only array index.
  Editing row 2 of a 3-row array and appending a 4th produces sensible
  per-cell/row_added corrections (see diff_values); replacing row 2 with
  an entirely different row while the array stays the same length is
  indistinguishable, from the server's side, from editing every field of
  row 2 in place -- there's no "this row was swapped for a different
  one" signal, since nothing tracks row identity across a save.

## Known risks

- `llama-cloud-services` (the LlamaParse SDK `parse.py` imports) is deprecated, with maintenance ending 2026-05-01; migration to the `llama-cloud` package is deferred.

## Current cost and latency (Groq call only; LlamaParse not included)

From the most recent real runs:

| | Duration | Tokens |
|---|---|---|
| `may26_mobile.pdf` (telecom_bill) | 2.64s | 3702 |
| `May-26 Local conveyance.pdf` (local_conveyance_form) | 6.84s | 4429 |
| `May-26 mail approval.pdf` (approval_correspondence) | 2.09s | 2399 |
| CORD sample, average of 15 | 6.41s | 2196 |

Dollar estimates in `run.py`'s output use an unverified, approximate Groq
per-token rate (see the comment there) -- treat as order-of-magnitude, not
a billing figure.

# Stage 2: Expense categorization

**Resume later.** Once Groq's daily quota has reset, run:
```bash
python scripts/eval_categorization.py --final --methods llm
python scripts/eval_categorization.py --final --methods hybrid
```
Each is standalone (doesn't touch `rules`/`classifier`/each other), uses its own
`eval/categorization/_predictions_cache/<method>.jsonl` for any row already
predicted (nothing already done gets re-spent), and appends its section into
`RESULTS.md` via `_final_results_state.json` without rerunning anything already
there. Then redo Part B step 4's comparison across all four methods -- the
current default (`rules`) was picked from only two of the four, and may change.

## What was built

- **Fictional company + policy** (`policy/company.md`, `policy/expense_policy.md`):
  Konkan Digital Systems Private Limited, an Indian IT services company, HQ
  Mumbai. 24 numbered clauses with tables by grade x city-tier x zone, written
  so later clauses cite by number. Employee grades (`employees.grade`) and base
  city (`employees.base_city`) added to the schema; the seeded employee is L4
  (Senior Manager), Mumbai.
- **14 expense categories** (`categories.py`): id, label, definition, 3+
  include/exclude examples, policy-clause mapping, and explicit tie-break
  rules for the ambiguous cases (restaurant bill vs client/team, hotel folio
  extras, fuel vs mileage, airport cab vs flight, etc.) -- a different axis
  from `document_type` (what kind of paper it is, Stage 1) vs category (what
  the money was for).
- **Four categorizer implementations** (`categorize.py`): `rules`
  (document_type default + keyword tie-break, free), `llm` (Groq
  `openai/gpt-oss-20b`, zero-shot, retry-with-backoff), `classifier`
  (sentence-transformers `all-MiniLM-L6-v2` embeddings + scikit-learn
  LogisticRegression, trained on 540 synthetic examples generated from the
  category definitions alone), `hybrid` (classifier first, falls back to
  `llm` below a confidence threshold tuned on a held-out synthetic split).
- **Product integration**: `extractions.category`/`category_confidence`/
  `category_method` columns; the pipeline categorizes every new document
  (`CATEGORIZER` env var, falls back to `rules` on any failure -- never fails
  the document itself); a Category dropdown on the review page and on the
  claim page's document rows; `scripts/backfill_categories.py` and
  `scripts/export_category_corrections.py` for existing documents and future
  retraining data.
- **Eval pipeline**: `scripts/build_eval_dataset.py`,
  `scripts/generate_silver_labels.py` (Groq-based, batched, model-agnostic --
  kept for when quota allows), `scripts/apply_manual_silver_labels.py` (the
  one actually used this time, see below), `scripts/promote_to_gold.py`,
  `scripts/eval_categorization.py`, `scripts/generate_review_html.py`.

## Eval methodology

**Silver labels, by hand, not by another LLM call.** Every reachable Groq
model on this account (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`,
`qwen/qwen3.8-27b`, and `groq/compound-mini`'s underlying
`llama-3.3-70b-versatile`) hit its own daily token quota in turn while
building this eval set -- a real constraint from one day's heavy usage, not
a design choice (see "Limitations" below). Per instruction, all 134 rows
were labeled directly: reading each document's `extracted_fields` and full
markdown against `categories.py`'s definitions and tie-break rules,
`silver_model="claude-code"` -- which incidentally serves the eval's own
goal better than a Groq-based labeler would have, since the categorizers
being measured are themselves Groq-based.

**Silver -> gold.** The project owner reviewed all 134 rows in
`review.html` and agreed with every label, including the ones flagged
ambiguous -- 100% agreement, 0 overrides (`gold_overrides.yaml` empty).
`dataset_metadata.json` records this, including an explicit anchoring-risk
note: the reviewer saw each row's silver label while reviewing it, which
can pull the reviewer toward agreeing with what's already shown rather than
an independent judgment. 100% here is not proof the labels are error-free;
a blind relabel of a sample (document only, no visible silver label) is the
documented follow-up to test that properly.

**Leakage rule.** The `classifier`'s training data (540 synthetic examples,
generated from category definitions alone, before the eval set existed) was
checked against the eval set by embedding cosine similarity
(`scripts/train_categorizer.py`, threshold 0.95) and retrained through the
filter once the eval set existed -- 0 training rows were within threshold of
an eval document, so nothing was dropped, but the check is real and reruns
automatically if training data changes.

**Synthetic-train / real-test split.** The classifier never trains on eval
documents (enforced by the leakage check above); the eval set itself mixes
25 SROIE + 15 CORD + 4 real Stage 1 documents (44 real, minus 1
non-categorizable) with 90 synthetic images, reported separately in
`RESULTS.md` specifically so a synthetic-heavy overall number is never
mistaken for a real-world one.

## RESULTS.md (gold labels, `rules` and `classifier` -- see "Resume later")

| method | all (n=133) | real-only (n=43, 7/14 categories) | synthetic-only (n=90) |
|---|---|---|---|
| `rules` | 87.2% acc, macro-F1 0.894 | 83.7% acc, macro-F1 0.805 | 88.9% acc, macro-F1 0.856 |
| `classifier` | 70.7% acc, macro-F1 0.706 | 46.5% acc, macro-F1 0.357 | 82.2% acc, macro-F1 0.776 |

`llm`/`hybrid`: not yet run (Groq quota). The real-only macro-F1 above is
averaged over only the 7 categories that slice actually contains
(accommodation, local_transport, office_supplies_equipment, other,
own_vehicle_mileage, phone_internet, travel_meals) -- not all 14.

`rules`' 17 misclassifications (its error-analysis section in `RESULTS.md`)
are mostly `other` swallowing two categories its keyword list doesn't cover
yet (`training_conferences`, `travel_documents_fees` vendor names) and one
real document (`cat-sroie-011`, a parking receipt) where the keyword list
has `"parking receipt"` but the actual text is `"Parking Fee"` -- a literal
string-match gap, not a conceptual one.

## Production default

`CATEGORIZER=rules`. Rule as specified: pick the higher macro-F1 on all
documents unless its real-only accuracy is more than 5 points worse than
the other's. `rules` has both the higher macro-F1 (0.894 vs 0.706) and the
better real-only accuracy (83.7% vs 46.5%) -- it wins outright on the
numbers actually measured, no tiebreak needed. This is a two-method
comparison, not a four-method one, and may change once `llm`/`hybrid` are
benchmarked (see "Resume later" above).

## Limitations, stated plainly

- **`llm` and `hybrid` are built and cached-ready but not yet benchmarked**
  (Groq daily quota exhausted while building this eval set) -- this is the
  single biggest open item before Stage 2's comparison is actually complete.
  The production default above reflects only `rules` vs `classifier`.
- **90 of 134 eval documents are synthetic**, generated and labeled in the
  same session as the categorizers being measured -- results are likely
  optimistic versus real-world documents the categorizers have never seen
  described in their own construction.
- **The real-only slice covers only 7 of the 14 categories** -- there is no
  real-document evidence at all for `client_entertainment`, `fuel`,
  `intercity_travel`, `software_subscriptions`, `team_events`,
  `training_conferences`, or `travel_documents_fees`.
- **`rules`' keywords were written in the same session as the eval set** --
  it may be partly tuned to documents it's now being scored against, rather
  than independently validated.
- **100% silver -> gold agreement with the label visible to the reviewer**
  -- a real risk of anchoring, not independent confirmation; see "Silver ->
  gold" above.
- **`team_events` has 6 examples, not 8** -- 2 of its synthetic documents
  were deliberately ambiguous (2 diners, no client/team wording) and were
  honestly relabeled `travel_meals` on review rather than kept as
  `team_events` to hit the floor, which is itself informative about how
  blurry that category boundary is.
- **Single category per document** -- a hotel bill with a minibar line item
  is filed entirely under `accommodation`; there is no per-line-item
  categorization.
