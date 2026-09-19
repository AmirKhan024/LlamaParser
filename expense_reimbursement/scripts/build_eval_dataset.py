"""Assembles eval/categorization/dataset.jsonl from every source built so
far: the 25 usable SROIE receipts (scripts/extract_sroie_for_eval.py; 5
of the original 30 failed extraction, see SUMMARY.md), the 15 cached CORD
samples, the 4 real Stage 1 documents, and the 90 synthetic images
(scripts/generate_synthetic_eval_images.py). Silver labels are added by
a separate pass (scripts/generate_silver_labels.py) -- this script only
fills the fields that don't need an LLM call.

Row schema (one JSON object per line):
  id, source, file, markdown_path, extracted_fields, silver_label,
  silver_rationale, silver_model, gold_label, reviewed, ambiguous, notes

Usage: python scripts/build_eval_dataset.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent
OUT_DIR = BASE_DIR / "outputs"
DATASET_PATH = BASE_DIR / "eval" / "categorization" / "dataset.jsonl"
SYNTHETIC_MANIFEST_PATH = BASE_DIR / "eval" / "categorization" / "synthetic_manifest.jsonl"

# Non-expense document types (categories.NON_EXPENSE_DOCUMENT_TYPES)
# aren't imported directly to avoid a hard dependency here -- kept as a
# literal set, cross-checked by tests/test_eval_dataset.py against
# categories.py itself.
_APPROVAL_TYPE = "approval_correspondence"


def _rel(path: Path) -> str:
    # Relative to the git repo root, not expense_reimbursement/ -- SROIE's
    # source files live at the repo root's data/ and output/, one level
    # above expense_reimbursement/, so a single consistent base is needed
    # for every row regardless of source.
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


def _row(row_id: str, source: str, file_path: Path, markdown_path: Path, fields: dict, notes: str = "", ambiguous: bool = False) -> dict:
    return {
        "id": row_id,
        "source": source,
        "file": _rel(file_path) if file_path else None,
        "markdown_path": _rel(markdown_path) if markdown_path and markdown_path.exists() else None,
        "extracted_fields": fields,
        "silver_label": None,
        "silver_rationale": None,
        "silver_model": None,
        "gold_label": None,
        "reviewed": False,
        "ambiguous": ambiguous,
        "notes": notes,
    }


def _sroie_rows() -> list[dict]:
    rows = []
    for i, result_path in enumerate(sorted(OUT_DIR.glob("sroie_*_result.json"))):
        stem = result_path.stem[: -len("_result")]  # sroie_<original_stem>
        original_stem = stem[len("sroie_"):]
        data = json.loads(result_path.read_text(encoding="utf-8"))
        fields = dict(data["clean_json"])
        fields.setdefault("document_type", data["document_type"])
        file_path = REPO_ROOT / "data" / f"{original_stem}.pdf"
        markdown_path = OUT_DIR / f"{stem}_raw.md"
        rows.append(_row(f"cat-sroie-{i:03d}", "sroie", file_path, markdown_path, fields))
    return rows


def _cord_rows() -> list[dict]:
    rows = []
    for i, sample_path in enumerate(sorted((OUT_DIR / "cord_eval").glob("sample_*.json"))):
        data = json.loads(sample_path.read_text(encoding="utf-8"))
        if not data.get("success", True):
            continue
        idx = data["index"]
        fields = dict(data["clean_json"])
        fields.setdefault("document_type", data["document_type"])
        file_path = OUT_DIR / "cord_eval" / "images" / f"cord_{idx:02d}.png"
        markdown_path = OUT_DIR / f"cord_{idx:02d}_raw.md"
        rows.append(_row(f"cat-cord-{i:03d}", "cord", file_path, markdown_path, fields))
    return rows


# original filename stem -> (image/pdf path relative to expense_reimbursement/, is upload dir)
_REAL_DOCS = [
    ("may26_mobile", "uploads/may26_mobile.pdf"),
    ("May-26 Local conveyance", "uploads/May-26 Local conveyance.pdf"),
    ("May-26 mail approval", "uploads/May-26 mail approval.pdf"),
    ("Hotel-Receipt", "test_documents/Hotel-Receipt.png"),
]


def _real_rows() -> list[dict]:
    rows = []
    for i, (stem, rel_file) in enumerate(_REAL_DOCS):
        result_path = OUT_DIR / f"{stem}_result.json"
        if not result_path.exists():
            continue
        data = json.loads(result_path.read_text(encoding="utf-8"))
        fields = dict(data["clean_json"])
        fields.setdefault("document_type", data["document_type"])
        file_path = BASE_DIR / rel_file
        markdown_path = OUT_DIR / f"{stem}_raw.md"
        notes = "Not an expense -- approval/forwarding email, evidence not a claim." if fields["document_type"] == _APPROVAL_TYPE else ""
        rows.append(_row(f"cat-real-{i:03d}", "real", file_path, markdown_path, fields, notes=notes))
    return rows


def _synthetic_rows() -> list[dict]:
    if not SYNTHETIC_MANIFEST_PATH.exists():
        return []
    rows = []
    with SYNTHETIC_MANIFEST_PATH.open(encoding="utf-8") as f:
        manifest_rows = [json.loads(line) for line in f if line.strip()]
    for i, manifest_row in enumerate(manifest_rows):
        result_path = BASE_DIR / manifest_row["result_path"]
        if not result_path.exists():
            continue
        data = json.loads(result_path.read_text(encoding="utf-8"))
        fields = dict(data["clean_json"])
        fields.setdefault("document_type", data["document_type"])
        file_path = BASE_DIR / manifest_row["image_path"]
        markdown_path = BASE_DIR / manifest_row["markdown_path"]
        note = manifest_row.get("notes", "")
        expected = manifest_row.get("expected_category")
        if expected and note:
            note = f"expected category: {expected}. {note}"
        elif expected:
            note = f"expected category: {expected}"
        rows.append(_row(
            manifest_row["id"], "synthetic_image", file_path, markdown_path, fields,
            notes=note, ambiguous=manifest_row.get("ambiguous", False),
        ))
    return rows


def _is_frozen() -> bool:
    """True once any row has been reviewed (silver -> gold). The eval set is
    frozen from that point: rebuilding would re-glob outputs/ (which can
    now contain extra sroie_* results, e.g. the receipts re-extracted while
    verifying the retry fix), renumber the cat-sroie-NNN ids, and silently
    change what every recorded result was scored against."""
    if not DATASET_PATH.exists():
        return False
    with DATASET_PATH.open(encoding="utf-8") as f:
        return any(json.loads(line).get("reviewed") for line in f if line.strip())


def main() -> None:
    if _is_frozen() and "--force" not in sys.argv[1:]:
        raise SystemExit(
            f"{DATASET_PATH} has reviewed rows -- the eval set is frozen and rebuilding it would "
            "renumber ids and invalidate recorded results. Pass --force only if you really intend a new eval set."
        )
    rows = _sroie_rows() + _cord_rows() + _real_rows() + _synthetic_rows()
    print(f"sroie={len(_sroie_rows())} cord={len(_cord_rows())} real={len(_real_rows())} synthetic={len(_synthetic_rows())}")
    print(f"Total rows: {len(rows)}")

    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATASET_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Wrote {DATASET_PATH}")


if __name__ == "__main__":
    main()
