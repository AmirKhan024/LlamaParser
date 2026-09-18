"""Renders synthetic receipt/invoice images for categories the real
document pool (SROIE + CORD + the 4 real uploads) doesn't cover well
enough, then runs each one through the REAL pipeline (LlamaParse +
Groq) exactly like a real upload would -- so the Stage 2 eval set is
scored on the same pipeline path as production, not a shortcut.

Counts below were set from the real-source category distribution
(computed with the free `rules` categorizer -- see the Stage 2 report):
real coverage was 25x other, 12x travel_meals, 3x office_supplies_
equipment, 1x each phone_internet/own_vehicle_mileage/accommodation, and
0 for the rest. Each category here is topped up to a floor of 8 total
(real + synthetic); the total below is exactly 90 -- the agreed cap on
new LlamaParse parses for this eval set.

Caches every render + parse + extract result keyed by doc id, so a
re-run never re-spends a LlamaParse or Groq call for an id already done.

Usage: python scripts/generate_synthetic_eval_images.py
"""

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

random.seed(20260918)

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright

from groq import RateLimitError

from extract import extract_claim
from parse import parse_pdf
from synth_content import GENERATORS
from synth_html import render

BASE_DIR = Path(__file__).resolve().parent.parent
IMAGES_DIR = BASE_DIR / "eval" / "categorization" / "synthetic_images"
OUT_DIR = BASE_DIR / "outputs"
MANIFEST_PATH = BASE_DIR / "eval" / "categorization" / "synthetic_manifest.jsonl"

# Both openai/gpt-oss-120b AND openai/gpt-oss-20b hit their daily Groq
# TPD quota while building this eval set (see SUMMARY.md's Stage 2
# section) -- qwen/qwen3.8-27b is the one model left with headroom.
FALLBACK_MODEL = "qwen/qwen3.8-27b"

# Floor-8-per-category top-up counts (see docstring). Sums to 90.
SYNTHETIC_COUNTS = {
    "intercity_travel": 8,
    "local_transport": 8,
    "fuel": 8,
    "client_entertainment": 8,
    "team_events": 8,  # 2 of these are deliberately ambiguous, see below
    "software_subscriptions": 8,
    "training_conferences": 8,
    "travel_documents_fees": 8,
    "phone_internet": 7,
    "own_vehicle_mileage": 7,
    "accommodation": 7,
    "office_supplies_equipment": 5,
}
AMBIGUOUS_TEAM_EVENTS = 2  # out of team_events' 8


def _build_docs() -> list:
    docs = []
    for category, count in SYNTHETIC_COUNTS.items():
        generators = GENERATORS[category]
        for i in range(count):
            doc_id = f"synth-{category}-{i:02d}"
            if category == "team_events":
                doc = generators[0](doc_id, ambiguous=(i < AMBIGUOUS_TEAM_EVENTS))
            else:
                doc = generators[i % len(generators)](doc_id)
            docs.append(doc)
    return docs


def _extract_with_retry(markdown: str, raw_json: dict, model: str, max_retries: int = 3):
    """A daily-quota 429 won't be fixed by a short retry, but a
    transient burst-rate 429 will -- honors Groq's own Retry-After
    header when present, otherwise a fixed short backoff."""
    attempt = 0
    while True:
        try:
            return extract_claim(markdown, raw_json, model=model)
        except RateLimitError as exc:
            attempt += 1
            if attempt > max_retries:
                raise
            wait_seconds = 5.0
            response = getattr(exc, "response", None)
            if response is not None:
                header_value = response.headers.get("retry-after")
                if header_value:
                    try:
                        wait_seconds = min(float(header_value), 30.0)
                    except ValueError:
                        pass
            time.sleep(wait_seconds)


def _render_png(html: str, out_path: Path, browser) -> None:
    page = browser.new_page()
    page.set_content(html)
    page.wait_for_timeout(50)
    page.screenshot(path=str(out_path), full_page=True)
    page.close()


def main() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    docs = _build_docs()
    print(f"{len(docs)} synthetic documents to build.")

    # Phase 1: render every PNG with Playwright, then fully close it
    # before touching LlamaParse. Sharing a process with an open
    # sync_playwright() context is exactly the "nested async" trap
    # SUMMARY.md's known-risks section already documents for parse.py's
    # sync .parse() inside a running uvicorn event loop -- Playwright's
    # sync API runs its own event loop under the hood, and the two don't
    # coexist in one process even outside of uvicorn.
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for doc in docs:
            png_path = IMAGES_DIR / f"{doc.doc_id}.png"
            if not png_path.exists():
                _render_png(render(doc), png_path, browser)
        browser.close()
    print("All images rendered. Playwright closed -- starting parse+extract phase.")

    # Phase 2: parse + extract, Playwright fully out of the process by now.
    manifest = []
    for doc in docs:
        png_path = IMAGES_DIR / f"{doc.doc_id}.png"
        result_path = OUT_DIR / f"{doc.doc_id}_result.json"

        if not result_path.exists():
            markdown_path = OUT_DIR / f"{doc.doc_id}_raw.md"
            json_path = OUT_DIR / f"{doc.doc_id}_raw.json"
            if markdown_path.exists() and json_path.exists():
                # Already parsed on an earlier run (extract_claim must
                # have failed that time, e.g. a Groq quota hit) -- reuse
                # it rather than spending another LlamaParse credit on a
                # document already successfully parsed.
                markdown = markdown_path.read_text(encoding="utf-8")
                raw_json = json.loads(json_path.read_text(encoding="utf-8"))
            else:
                try:
                    # parse_pdf works for images too (same as server.py's
                    # real pipeline) and already writes outputs/<doc_id>_
                    # raw.md/.json itself (OUTPUTS_DIR == OUT_DIR here).
                    markdown, raw_json = parse_pdf(png_path)
                except Exception as exc:  # noqa: BLE001 -- keep going, report at the end
                    print(f"  {doc.doc_id}: PARSE FAILED ({exc})")
                    continue
            try:
                extraction = _extract_with_retry(markdown, raw_json, FALLBACK_MODEL)
            except Exception as exc:  # noqa: BLE001
                print(f"  {doc.doc_id}: EXTRACT FAILED ({exc})")
                continue
            payload = {
                "document_type": extraction.document_type.value,
                "clean_json": extraction.raw_fields,
                "model": FALLBACK_MODEL,
                "tokens": {"total": extraction.total_tokens},
            }
            result_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  {doc.doc_id}: {extraction.document_type.value} (expected {doc.category})")
        else:
            print(f"  {doc.doc_id}: cached, skipping")

        manifest.append({
            "id": doc.doc_id,
            "expected_category": doc.category,
            "vendor": doc.vendor,
            "ambiguous": doc.ambiguous,
            "notes": doc.notes,
            "image_path": f"eval/categorization/synthetic_images/{doc.doc_id}.png",
            "markdown_path": f"outputs/{doc.doc_id}_raw.md",
            "result_path": f"outputs/{doc.doc_id}_result.json",
        })

        # Write after every doc -- an interruption partway through loses
        # nothing already in the manifest (re-run picks up the rest via
        # the cache checks above).
        with MANIFEST_PATH.open("w", encoding="utf-8") as f:
            for row in manifest:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\nWrote manifest for {len(manifest)} documents to {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
