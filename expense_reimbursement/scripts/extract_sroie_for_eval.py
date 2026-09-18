"""Run extract.py's Groq classify+extract step on the 30 SROIE receipts
that already have cached LlamaParse output at the repo root (../data/*.pdf,
../output/*_res.json) -- no LlamaParse call needed, just the one Groq call
per document. Caches each result to outputs/sroie_<stem>_result.json,
same convention run.py/eval_cord.py use, and skips any file that already
has a cached result (never re-spend a Groq call on a document already
extracted).

Usage: python scripts/extract_sroie_for_eval.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from extract import extract_claim

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent
SROIE_DATA_DIR = REPO_ROOT / "data"
SROIE_OUTPUT_DIR = REPO_ROOT / "output"
OUT_DIR = BASE_DIR / "outputs"
MARKDOWN_OUT_DIR = OUT_DIR  # <stem>_raw.md alongside the other cached markdown

# openai/gpt-oss-120b (extract.py's production MODEL) hit its daily
# token quota partway through this eval-set build (Groq free tier,
# 200k TPD) -- these are eval-set documents, not the live pipeline, so
# falling back to the smaller model here is a reasonable substitution;
# noted in SUMMARY.md's Stage 2 section rather than silently switched.
FALLBACK_MODEL = "openai/gpt-oss-20b"


def _extract_markdown(res_json: dict) -> str:
    pages = (res_json.get("markdown") or {}).get("pages") or []
    return "\n\n".join(p.get("markdown", "") for p in pages)


def main() -> None:
    stems = sorted(p.stem for p in SROIE_DATA_DIR.glob("*.pdf"))
    print(f"{len(stems)} SROIE documents found.")
    for stem in stems:
        result_path = OUT_DIR / f"sroie_{stem}_result.json"
        markdown_path = MARKDOWN_OUT_DIR / f"sroie_{stem}_raw.md"
        if result_path.exists():
            print(f"  {stem}: cached, skipping")
            continue

        res_path = SROIE_OUTPUT_DIR / f"{stem}_res.json"
        if not res_path.exists():
            print(f"  {stem}: no cached parse output at {res_path}, skipping")
            continue
        res_json = json.loads(res_path.read_text(encoding="utf-8"))
        markdown = _extract_markdown(res_json)
        if not markdown.strip():
            print(f"  {stem}: empty markdown, skipping")
            continue
        markdown_path.write_text(markdown, encoding="utf-8")

        try:
            result = extract_claim(markdown, {}, model=FALLBACK_MODEL)  # no supporting JSON needed -- markdown alone carries the OCR text
        except Exception as exc:  # noqa: BLE001 -- keep going through the rest of the batch, report at the end
            print(f"  {stem}: FAILED ({exc})")
            continue
        payload = {
            "document_type": result.document_type.value,
            "clean_json": result.raw_fields,
            "model": FALLBACK_MODEL,
            "duration_seconds": result.duration_seconds,
            "tokens": {
                "prompt": result.prompt_tokens,
                "completion": result.completion_tokens,
                "total": result.total_tokens,
            },
        }
        result_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {stem}: {result.document_type.value} ({result.total_tokens} tokens)")


if __name__ == "__main__":
    main()
