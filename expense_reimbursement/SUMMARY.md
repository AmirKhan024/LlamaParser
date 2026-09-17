# Stage 1 closeout: expense reimbursement extraction pipeline

This is the final record of what stage 1 actually is, as of the commit that
added this file. It exists so "what does stage 1 do" has one place to check
instead of drifting across conversation history. If the code changes,
update this file in the same commit -- don't let it go stale.

## What it does

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
validation results, completeness warnings, and a `review_view` (a handful of
fields plus `_needs_review`, `_flagged_checks`, `_ai_corrections`).

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

## Tested, with results

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

## Explicitly NOT done -- out of scope for stage 1

- No policy engine, no fraud/duplicate detection, no payment-time
  validation.
- No human review UI. `review_view.py` produces a data projection
  (`build_review_view`) for a future UI to consume -- no UI exists.
- No testing on scanned, blurry, rotated, or handwritten documents. All
  test documents (3 real bills, 15 CORD samples) are clean digital or
  well-scanned printed receipts.
- No testing at scale. 18 documents total have gone through this pipeline,
  ever. Nothing about failure modes, cost, or latency at 100+ or 1000+
  documents is known.
- No dynamic field-name generation, canonicalization/ontology layer, or
  fuzzy cross-schema matching -- deliberately out of scope from stage 1's
  original brief, still true.

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
