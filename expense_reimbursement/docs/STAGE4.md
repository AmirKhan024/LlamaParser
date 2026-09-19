# Stage 4 — Fraud & anomaly detection (v1 demo)

Reads Stage 1–3 tables and produces, per claim, a risk score and band **with the rules
and evidence that produced them**. Nothing in Stages 1–3 is modified.

```
Stage 1-3 tables (read-only) ──> fraud_eval.build_context ──> fraud_rules (9 deterministic rules)
                                                               │
                                       score + band in code ◄──┘
                                                               │
                        fired rules + evidence ──> ONE model call (narrative only) ──> grounding check
                                                               │
                                          immutable fraud_assessments row ──> API ──> review queue
```

Files: `fraud_config.py` (all weights/bands/thresholds), `fraud_rules.py` (rules + scoring, pure),
`fraud_narrative.py` + `prompts/fraud_narrative_v1.md`, `fraud_eval.py` (DB + storage),
`server.py` (endpoints), `static/index.html` (`#/fraud`).

## 1. The absent-signal policy (the hard constraint)

Every rule returns one of three statuses, and they are never blurred:

| status | meaning | contributes to score |
|---|---|---|
| `fired` | the data was present and the pattern was found | its weight |
| `clear` | the data was present and nothing was found — **"no risk found"** | 0 |
| `not_applicable` | the data the rule needs is absent — **"could not assess"**, with the reason and the missing fields per document | 0, and it is not evidence of safety |

Rules never guess across a gap. A document missing a field a rule depends on is *skipped by that
rule* and named in the evidence/reason; if no document can be assessed the rule is
`not_applicable`. Examples: duplicates need vendor + amount + currency + date; velocity needs a
date; the policy-repeat rule needs Stage 3 decisions, and if every unit of the claim is
`insufficient_information` (no verdict to build on) it is `not_applicable`; the correction rules
need an AI extraction to compare against.

Every assessment carries `assessable_signals: n of m` (fired + clear out of all 9 rules). If fewer
than `MIN_ASSESSABLE_SIGNALS` (3) rules could look at the claim **and nothing fired**, the band is
**`unassessable`**, not `low`. A fired rule is always reported even on thin coverage.

Not assessed at all in v1, because Stage 1 does not extract the data: out-of-hours (time of day),
who a bill is billed to, the trip a claim belongs to. They are absent from the ruleset rather than
faked.

## 2. Rules and weights (`fraud_config.py`, ruleset `v1`)

| rule | weight | fires when | data it needs |
|---|---|---|---|
| `duplicate_same_employee` | 30 | same (normalised) vendor + exact amount + currency, another document of the same employee within 14 days (any claim) | vendor, amount, currency, date |
| `duplicate_cross_employee` | 25 | same, but a different employee, within 14 days | same |
| `threshold_gaming` | 20 | amount is < a policy threshold and within 5% below it | amount, currency, category; thresholds **read from `policy_clauses`** (`requires_approval_above`, `documentation_required.min_amount`) for the category and `*` clauses in the same currency |
| `velocity` | 15 | > 5 documents, or > ₹25,000, from one employee in one ISO week (by document date) | document dates |
| `round_number` | 8 | amount is a multiple of ₹500 and ≥ ₹1,000 | amount, currency |
| `weekend_business` | 15 | Sat/Sun date on `client_entertainment` / `team_events` | date (docs in other categories: `clear`) |
| `policy_repeat_violation` | 15 | this claim violates a clause the same employee has violated ≥ 2 times (latest Stage 3 run per claim; a reviewer label wins) | Stage 3 decisions with a known verdict |
| `correction_upward` | 20 | the employee edited a **money** field upward vs the AI extraction | corrections table |
| `correction_guardrail` | 35 | an edit carries a reason, i.e. Stage 1's guardrail fired (contradicts the bill, increases a value unverifiably, or the value isn't printed on the document) | corrections table |

The correction rules are the heaviest inputs by design: the employee's own edit of a number is the
strongest available signal. Each rule result carries `id`, `description`, `weight`, `status`,
`reason` and `evidence` (the specific documents — vendor, date, amount, file, employee — and
values that triggered it).

## 3. Scoring

`risk_score = min(100, sum(weight of fired rules))`; band `medium` ≥ 25, `high` ≥ 50, otherwise
`low` (or `unassessable`, above). Computed in `fraud_rules.score`, from rule outputs only. Change a
weight → change `fraud_config.py` and bump `RULESET_VERSION`. A model never produces, adjusts or
sees a score before the narrative is written (it is told the band only as context).

## 4. The narrative (explanation only)

One call per claim with at least one fired rule (temperature 0, JSON mode, prompt in
`prompts/fraud_narrative_v1.md`), fed only the fired rules and their evidence. The reply is
accepted only if it is **grounded** (`fraud_narrative.ground`):

- it declares `rules_referenced`, all of which fired;
- it does not mention an unfired rule (by id, or by that rule's distinctive wording);
- every number in it appears in the fired rules' evidence (or is the length of a list in it) — it
  cannot introduce a sum or figure.

Otherwise the narrative is dropped (`narrative_status = rejected`, reason in `raw_response`) and the
UI shows the raw rules. If the model is unavailable the assessment still completes
(`unavailable`). Rules and score never depend on it.

## 5. Storage, API, UI

`fraud_assessments` — immutable (a DB trigger allows only the `human_assessment`, `human_note`,
`reviewed_at`, `reviewed_by` columns to change; no deletes), one row per claim per run: `rules_fired`
(with evidence), `rules_not_applicable` (with reasons), `rules_clear`, score, band,
`assessable_signal_count` / `total_signal_count`, `narrative_status` + `narrative`, model, prompt
version, `raw_request` / `raw_response`, latency, tokens, `ruleset_version`, `run_id`. Every call
writes a new row (append-only; there is no idempotency short-circuit in v1).

- `POST /api/claims/{id}/assess-fraud` · `GET /api/claims/{id}/fraud`
- `GET /api/fraud/queue` — latest assessment per claim, riskiest band first
- `POST /api/fraud/{id}/review` — `human_assessment: confirmed | dismissed`, `human_note`

These are **reviewer-facing and not scoped to the claim's owner**, and there is no role check:
Stage 5 owns auth. `#/fraud` shows each claim's band, score, coverage, narrative (or an explicit
"no narrative" note), every fired rule with its evidence, what could not be assessed, and
confirm/dismiss controls. A band is never shown without its triggering rules.

## 6. Assessments as labelled data later

Every `confirmed` / `dismissed` label sits next to the exact rule outputs and evidence that
prompted the review, immutable and versioned by `ruleset_version`. With real volume:

1. **Per-rule precision**: for each rule, `confirmed / (confirmed + dismissed)` over reviewed
   assessments where it fired. Rules that are mostly dismissed get a lower weight or a tighter
   parameter; a new ruleset is a new version and old rows stay attributable.
2. **Coverage before accuracy**: report `not_applicable` rates per rule first — a rule that is
   `not_applicable` on most claims is a Stage 1 extraction gap, not a detection result.
3. **Weight fitting**: with enough labels, fit weights (e.g. logistic regression over fired/clear
   flags) and replace the hand-set table — still computed in code, still a weighted sum.
4. **Unlabelled negatives are not negatives.** Only reviewed claims are labels; the queue's
   sort order is a selection bias (reviewers see high bands first), so estimate recall with a random
   sample of low-band claims reviewed blind.
5. **Replay**: `raw_request` holds the exact evidence the narrative saw; the rules can be re-run on
   stored data with a new ruleset for a side-by-side comparison.

## 7. Known limits

- Vendor matching is normalised exact match, not fuzzy; amounts must match exactly (a duplicate
  submitted with a different amount is not caught).
- The universe for duplicates/velocity is *submitted* claims plus the claim being assessed;
  drafts of other claims are ignored.
- `threshold_gaming` needs only the category and amount (it does not depend on which clause Stage 3
  selected), so a category with several thresholds can fire on the one that is not the governing rule.
- Round-number fires on naturally fixed prices (a plan or course fee); it is deliberately low weight.
- Weights and bands are hand-set for the demo, not calibrated.

## 8. Demo run (see `demo/RUN_REPORT.md`; a smoke test, not an eval)

48 synthetic receipts (`demo/receipts/*.png`, 8 employees, Jun-Aug 2026) went through the real
Stage 1 pipeline (LlamaParse + Groq), the employee-correction path, confirm and submit, then
Stage 3 and Stage 4. `demo/manifest.json` holds who submits what and no expected outcomes.
Stage 3 reached 17 of 23 claims (42 units) before Groq's daily token limit; Stage 4 assessed all 23,
reporting `policy_repeat_violation` as `not_applicable` where Stage 3 data was missing. Re-run:
`python demo/generate_demo_data.py`, `python demo/run_demo.py stage1|stage3`,
`python demo/assess_all.py`, `python demo/report.py`.
