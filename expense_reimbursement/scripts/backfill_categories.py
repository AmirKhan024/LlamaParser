"""One-off backfill: categorize every document whose latest extraction
predates the category column (category IS NULL) and whose document_type
is actually an expense (categories.is_categorizable).

Updates the existing latest extraction row IN PLACE -- this is filling in
metadata that didn't exist yet when the row was written, not a new
employee-sourced version (compare server.py's _save_edits, which always
appends a new version for a real edit).

Usage: python scripts/backfill_categories.py [--method rules|llm|classifier|hybrid]
Defaults to the CATEGORIZER env var (same default as the live pipeline).
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select

import repository
from categories import is_categorizable
from categorize import build_input, categorize
from db import get_sessionmaker
from models import Document, Extraction


def _latest_extraction_ids_missing_category(session) -> list[tuple[Document, Extraction]]:
    pairs = []
    for document in session.scalars(select(Document)).all():
        extraction = repository.latest_extraction(session, document.id)
        if extraction is None or extraction.category is not None:
            continue
        if not is_categorizable(extraction.document_type):
            continue
        pairs.append((document, extraction))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", default=os.environ.get("CATEGORIZER", "llm"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session = get_sessionmaker()()
    try:
        pairs = _latest_extraction_ids_missing_category(session)
        print(f"{len(pairs)} document(s) need a category (method={args.method})")
        for document, extraction in pairs:
            inp = build_input(extraction.fields, document.raw_markdown or "")
            try:
                result = categorize(inp, args.method)
            except Exception as exc:  # noqa: BLE001 -- fall back to rules, never crash the whole backfill
                print(f"  {document.id}: {args.method} failed ({exc}), falling back to rules")
                result = categorize(inp, "rules")
            print(f"  {document.id} ({extraction.document_type}): {result.category} (confidence={result.confidence:.2f}, method={result.method})")
            if not args.dry_run:
                extraction.category = result.category
                extraction.category_confidence = result.confidence
                extraction.category_method = result.method
        if not args.dry_run:
            session.commit()
            print("Committed.")
        else:
            print("Dry run -- nothing written.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
