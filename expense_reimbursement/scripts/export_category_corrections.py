"""Export every employee category correction (Correction.field_path ==
"category") as a training-data row -- real signal for a future retrain of
the classifier, once there's enough volume to matter.

Usage: python scripts/export_category_corrections.py [output.jsonl]
Defaults to eval/categorization/employee_corrections.jsonl.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select

from categorize import MARKDOWN_EXCERPT_CHARS
from db import get_sessionmaker
from models import Correction, Document, Extraction

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "eval" / "categorization" / "employee_corrections.jsonl"


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = get_sessionmaker()()
    try:
        corrections = session.scalars(
            select(Correction).where(Correction.field_path == "category")
        ).all()
        rows = []
        for correction in corrections:
            extraction = session.get(Extraction, correction.extraction_id)
            document = session.get(Document, correction.document_id) if extraction else None
            if extraction is None or document is None:
                continue
            rows.append({
                "id": f"corr-{correction.id}",
                "document_id": str(document.id),
                "document_type": extraction.document_type,
                "vendor_name": extraction.fields.get("vendor_name"),
                "markdown_excerpt": (document.raw_markdown or "")[:MARKDOWN_EXCERPT_CHARS],
                "ai_category": correction.ai_value,
                "employee_category": correction.employee_value,
                "corrected_at": correction.created_at.isoformat(),
                "source": "employee_correction",
            })
        with out_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"Wrote {len(rows)} correction row(s) to {out_path}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
