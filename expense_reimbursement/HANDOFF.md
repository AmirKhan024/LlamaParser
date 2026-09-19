# Handoff: Expense Reimbursement AI Pipeline

Written for: the next Claude Code session picking this up (and for Amir,
who is building this to show an interviewer).

## Where this lives

- **Local path**: `C:\Users\Amir Khan\Desktop\ds_Amir\LlamaParser\expense_reimbursement`
  (a subfolder of the `LlamaParser` repo, not its own repo)
- **GitHub**: https://github.com/AmirKhan024/LlamaParser (public)
- **Branch**: `main` — this is the only branch in use; every session so
  far has committed directly to it and pushed at the end
- **Latest commit as of this handoff**: `a6ffd7e` (34 commits total).
  Working tree is clean, everything is pushed.
- **To resume in a fresh Claude Code session**: open this folder (or the
  parent `LlamaParser` folder) in VS Code, then point Claude at this
  file and `SUMMARY.md` (see "What to paste into a new session" at the
  bottom).

## What "the project" actually is

The user's long-term vision (pasted into a recent session, worth reading
in full if you have it) is a 9-stage **"AI expense system that reads a
receipt, checks it against company policy, catches fraud, and routes it
for approval, with a human only stepping in when the AI is uncertain."**

**Stages 1 and 2 are built.** Stage 1 is Intelligent Document Processing;
Stage 2 (expense categorization) is CLOSED -- see `SUMMARY.md`'s Stage 2 section
and `eval/categorization/RESULTS.md`. Default categorizer is `rules`
(`CATEGORIZER=rules`): 83.7% on the 43 real eval documents vs `llm` 69.8% and
`classifier` 46.5%; `llm` wins only on the synthetic-heavy all-documents metric
(90.2% vs 87.2%), so the pre-agreed decision rule keeps `rules`. That ranking
leans on one label boundary (10 hardware receipts) -- read RESULTS.md's what-if
and SUMMARY.md's "What Stage 3 should know" before building on it. Stages 3-9
(policy RAG, fraud/anomaly detection, agentic approval workflow, conversational
assistant, continuous learning, explainability dashboard, analytics) do not
exist yet: no policy engine, no fraud model, no agents, no auth, no chat
interface. See "What's realistically next" below.

## What's built and working (stage 1)

**Stack**: FastAPI + Postgres (pgvector image, unused yet) + SQLAlchemy
2.0 + Alembic migrations + vanilla JS/HTML frontend (no framework) +
LlamaParse (OCR/parsing) + Groq (`openai/gpt-oss-120b`, classification +
structured extraction).

**Pipeline**: upload (PDF/PNG/JPG/WEBP) → LlamaParse → Groq classifies
into one of 8 document types (telecom_bill, restaurant_bill,
local_conveyance_form, approval_correspondence, hotel_invoice,
taxi_receipt, fuel_receipt, generic_receipt/unstructured_proof) and
extracts a flat JSON schema → **self-repair retry** (one extra Groq call
if the first extraction fails an arithmetic check, kept only if it
strictly improves) → deterministic arithmetic validation (no LLM in this
step) → **suggest_fixes** (offers a one-click fix, but only for a number
that's both arithmetically implied AND literally printed on the
document) → employee review UI (edit, confirm, submit).

**Data model**: `Claim` → `Document` → `Extraction` (append-only,
versioned: the AI's own extraction, `source="ai"`, is never mutated;
every employee save adds a new version) → `Correction` (per-field diff
between AI and employee value, with `reason`/`direction` columns) →
`AuditEvent` (append-only action log). `CheckResultRow` stores every
arithmetic check's pass/fail per extraction version.

**Real reliability numbers already in hand** (useful interview ammo,
see `SUMMARY.md`'s "Third pass" and "Known risks" sections):
- CORD eval baseline: `grand_total` 14/15 (93%) field accuracy.
- Self-repair retry measured on 5 real runs each way: **4/5 correct
  without repair, 5/5 with it** (the known total_kms/
  total_conveyance_amount field-swap bug, caught and fixed live).
- A genuinely closed exploit chain, each with its own repro test: a
  loose arithmetic tolerance that let a typo through, a money edit that
  contradicted the bill with no reason required, and a "consistent
  inflation" loophole (raise every related number by the same story and
  every check still balances) — all three closed, each with a test that
  reproduces the exact user-reported bug first.

**Safety/correctness properties already built** (this is what makes it
demo-safe, not just demo-pretty):
- Soft delete (`status='removed'`) — nothing is ever actually deleted,
  including the uploaded file.
- Atomic transactions — save+confirm+recompute is one commit, not
  several; a failure partway through leaves no trace.
- Restart recovery — every `processing` document is failed out on
  startup (single-process server, nothing survives a restart).
- A late-finishing background pipeline can never overwrite a document
  the employee already confirmed or removed (atomic conditional UPDATE).
- Money-edit reason gate: an edit that contradicts the bill, increases a
  number unverifiably, or isn't printed on the document at all requires
  a typed reason, enforced server-side (422), not just in the UI.

**Tests**: 104, all passing — unit tests (validation logic, no
network/DB), API tests (real Postgres, `PIPELINE_MODE=fake` replays
cached LLM output, no API keys needed), and Playwright e2e tests (real
browser, real uvicorn process, screenshots saved to
`test_screenshots/`). `tests/conftest.py` now runs
`alembic upgrade head` against the test DB itself once per session, so
a fresh clone + `docker compose up -d` is enough to run the suite.

**Explicitly NOT done, even within stage 1** (from `SUMMARY.md`):
no auth (one hardcoded seeded employee for every request), no manager
approval action (a submitted claim just sits there), no testing on
real-world-bad scans (everything tested is clean digital/well-scanned),
no testing at any real scale, no per-cell edit endpoint (whole-array PUT
only), no stable row identity for a trip/line-item array.

## How to run it (from a clean checkout)

```bash
docker compose up -d                      # Postgres (pgvector/pgvector:pg16), port 5433
pip install -r requirements.txt
python -m playwright install chromium     # only for tests/test_e2e.py
cp .env.example .env                      # fill in POSTGRES_PASSWORD; LLAMA_CLOUD_API_KEY/GROQ_API_KEY for real mode
alembic upgrade head
python scripts/seed_employee.py
python scripts/seed_demo.py               # optional: seed a demo claim from uploads/, no API keys needed
uvicorn server:app --reload               # -> http://127.0.0.1:8000
```

```bash
pytest tests/ -v                          # full suite, PIPELINE_MODE=fake regardless of .env, no API keys needed
```

Full detail: `README.md` (setup/tests) and `SUMMARY.md` (everything
about the pipeline, data model, bug-fix history, and eval numbers — this
is the single most information-dense file in the repo, worth reading
before changing anything).

## What's realistically next, if there's time before the interview

Applying my own judgement here, not just the user's wishlist as written
— the wishlist is a full enterprise system; nobody builds all 9 stages
solo before an interview. Rank by **interview-impact per hour of work**,
building on what already exists:

1. **Auth + roles** (employee/manager/finance). Cheap, and "every
   request runs as one hardcoded employee" is the first thing a sharp
   interviewer will poke at if they read the code.
2. **Policy Compliance Engine (RAG)** — the highest-leverage addition.
   Stage 1 already produces a clean structured `Claim` object; chunk a
   company policy doc into pgvector (already provisioned, unused),
   retrieve relevant clauses for the claim's category/amount, have the
   LLM reason "compliant or not, citing the clause." This is what turns
   "OCR app" into "intelligent system" in a 5-minute demo.
3. **Agentic approval decision** on top of the policy engine — a single
   Decision step (doesn't need a heavyweight multi-agent framework to be
   credible) that auto-approves low-risk/low-amount, routes the rest to
   a manager, and always writes a plain-English rationale. This is the
   most interview-impressive artifact per the user's own framing
   ("agentic reasoning with explainable decisions").
4. **Duplicate/fraud signal** — cheap version: the sha256 exact-dup
   check already exists (`find_document_by_sha256`); extend to
   perceptual image hashing for a cropped/rotated re-submission, and a
   simple Isolation Forest or z-score on amount-per-category as an
   anomaly signal. Report precision/recall on a small labeled set —
   that's the "real ML, not just an LLM call" evidence interviewers ask
   for.
5. **Explainability surface** — mostly already exists as data
   (`AuditEvent`, `Correction.reason`, check results); needs a UI page
   that renders it as a timeline, not new backend work.
6. **A small analytics view** — spend by category/department, cheap to
   build once claims exist, high visual payoff live.

Lower priority for interview purposes (real work, less payoff per
hour): the conversational/chat assistant, continuous-learning
retraining loop, multi-currency/tax handling, mobile polish, a
full LangGraph/CrewAI multi-agent framework (a single well-reasoned
Decision step demonstrates the same idea with far less infrastructure
risk before a deadline).

Whatever gets built next, keep doing what stage 1 already did well:
**write down the eval, not just the feature** — a labeled sample, a
precision/recall or accuracy number, a stated tradeoff, a documented
failure mode. That's what SUMMARY.md already has for stage 1 and it's
exactly what an interviewer will probe for.

## What to paste into a new session

```
Read HANDOFF.md and SUMMARY.md in this repo for full context. Stage 1
(document extraction/validation/review) is done and pushed to
github.com/AmirKhan024/LlamaParser (main, expense_reimbursement/
subfolder). 104 tests pass. I'm building this for a job interview --
help me with: <whatever you want to do next>.
```
