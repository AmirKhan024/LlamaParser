# Expense reimbursement

An employee uploads receipts/bills to a claim, reviews and corrects the
key details extracted from each one, confirms them, and submits the
claim. See `SUMMARY.md` for the full pipeline/data-model writeup and
what's still out of scope.

## Setup

```bash
# 1. Start Postgres (pgvector/pgvector:pg16) in Docker.
#    Host port 5433, not 5432 -- leaves a native Postgres install on
#    5432 alone if you have one.
docker compose up -d

# 2. Python deps
pip install -r requirements.txt
python -m playwright install chromium   # only needed for tests/test_e2e.py

# 3. Configure
cp .env.example .env
# Fill in POSTGRES_PASSWORD (must match what you put in .env for the
# same variable docker-compose.yml reads) and, for PIPELINE_MODE=real,
# LLAMA_CLOUD_API_KEY / GROQ_API_KEY.

# 4. Create the schema (also enables the pgvector extension, unused
#    until a later stage)
alembic upgrade head

# 5. Seed the one employee Stage 1 runs as (no login yet)
python scripts/seed_employee.py

# 6. Optional: seed a demo claim with the 3 real documents in uploads/,
#    using their cached extraction results (no API keys needed)
python scripts/seed_demo.py

# 7. Run the server
uvicorn server:app --reload
# -> http://127.0.0.1:8000
```

`PIPELINE_MODE` in `.env` controls what an upload does:
- `real` (default): calls LlamaParse + Groq. Needs both API keys.
- `fake`: replays the cached `outputs/*_result.json` matching the
  upload's sha256 (falls back to the phone-bill result). No keys
  needed -- used by the UI when iterating without spending credits,
  and by the test suite.

## Tests

```bash
# Point TEST_DATABASE_URL at a *separate* database (docker-compose's
# init script already creates expense_test on first startup) -- the
# suite TRUNCATEs every table in it before each test.
pytest tests/test_api.py -v

# End-to-end (Playwright): starts its own uvicorn process against
# expense_test, drives every button on every screen, and saves
# screenshots at 1280px and 390px to test_screenshots/.
pytest tests/test_e2e.py -v
```

Both need Postgres running (`docker compose up -d`) and use
`PIPELINE_MODE=fake` regardless of what's in `.env`, so no API keys are
required to run them.

## CLI tools (unchanged, JSON-file based, no database)

```bash
python run.py          # parse -> extract -> validate every PDF in uploads/
python eval_cord.py     # accuracy check against the CORD benchmark set
```

## Project layout

- `parse.py`, `extract.py`, `validate.py`, `review_view.py` -- the
  extraction pipeline and its employee-facing view logic. `run.py` and
  `eval_cord.py` are unchanged CLI entry points into the same functions
  `server.py` calls for the API.
- `models.py`, `db.py`, `repository.py`, `alembic/` -- the database
  layer. All reads/writes to Postgres go through `repository.py`.
- `server.py` -- the FastAPI app (`/api/...`) plus the background
  extraction pipeline.
- `static/index.html` -- the employee UI (plain HTML/CSS/JS, no build
  step, hash-routed).
- `scripts/seed_employee.py`, `scripts/seed_demo.py` -- one-off setup
  scripts, see Setup above.
- `tests/` -- pytest API tests (`test_api.py`) and the Playwright
  end-to-end suite (`test_e2e.py`); `conftest.py` points both at
  `expense_test`.
- `storage/` -- uploaded files on disk, gitignored. Never in the
  database.
