"""Step 1 -- Parse: send a PDF to LlamaParse, save the raw markdown and
JSON to disk untouched, return them for the extraction step.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

from llama_cloud_services import LlamaParse

OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"

# "agentic" gave meaningfully better OCR accuracy than the cheaper tier
# in prior testing on documents like these (correctly reading amounts,
# GSTINs, dates that a lighter tier misread) -- worth the extra cost for
# a reimbursement pipeline where a misread amount is the whole problem.
PARSE_TIER = "agentic"
PARSE_VERSION = "latest"


def _get_parser() -> LlamaParse:
    api_key = os.environ.get("LLAMA_CLOUD_API_KEY")
    if not api_key:
        raise RuntimeError("LLAMA_CLOUD_API_KEY is not set")
    return LlamaParse(api_key=api_key, tier=PARSE_TIER, version=PARSE_VERSION)


def parse_pdf(pdf_path: Path) -> Tuple[str, Dict[str, Any]]:
    """Parse one PDF. Returns (markdown, raw_json) and writes both to
    outputs/<stem>_raw.md and outputs/<stem>_raw.json.
    """
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem

    parser = _get_parser()
    result = parser.parse(str(pdf_path))

    markdown = result.get_markdown()
    raw_json = result.get_json()

    (OUTPUTS_DIR / f"{stem}_raw.md").write_text(markdown, encoding="utf-8")
    (OUTPUTS_DIR / f"{stem}_raw.json").write_text(
        json.dumps(raw_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return markdown, raw_json


async def aparse_pdf(pdf_path: Path) -> Tuple[str, Dict[str, Any]]:
    """Async sibling of parse_pdf -- same behavior, same on-disk output,
    via LlamaParse's own aparse() instead of parse(). Used by server.py's
    background pipeline so it can run as a real asyncio task on the same
    event loop uvicorn already owns, instead of a separate thread.

    Uses JobResult.aget_markdown()/aget_json(), not the sync
    get_markdown()/get_json() the rest of this module uses -- those are
    thin wrappers that call asyncio_run() internally, which is exactly
    the "nested async" trap this function exists to avoid; confirmed
    directly (this raised the exact same "Detected nested async" error
    parse_pdf's sync path did, even though the parse itself already
    correctly went through aparse()) before switching to these.
    """
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem

    parser = _get_parser()
    result = await parser.aparse(str(pdf_path))

    markdown = await result.aget_markdown()
    raw_json = await result.aget_json()

    (OUTPUTS_DIR / f"{stem}_raw.md").write_text(markdown, encoding="utf-8")
    (OUTPUTS_DIR / f"{stem}_raw.json").write_text(
        json.dumps(raw_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return markdown, raw_json
