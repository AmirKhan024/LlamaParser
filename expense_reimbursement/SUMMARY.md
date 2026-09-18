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

- **The local conveyance form's real, still-unfixed extraction problem**:
  its markdown table has *two* number columns for both "Total Conveyance
  Amount" (5200 and 981) and "Daily Allowance" (1560 and 5886), and the
  model still only ever captures the first one. `check_completeness`
  correctly caught *both* drops this run (not just the original 5886) --
  but catching it isn't fixing it, and there's no schema field to put a
  second, unexplained number in. This is the same limitation
  `check_completeness` was always documented as having (a warning the
  employee explains via `note_to_approver`, not a correction), just now
  visibly happening twice on one document instead of once.
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
