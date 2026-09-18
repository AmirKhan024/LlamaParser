"""Promote eval/categorization/dataset.jsonl's silver labels to gold,
applying human overrides from gold_overrides.yaml (see that file's header
comment for its format).

For every row: gold_label = override's label if the row's id is in
gold_overrides.yaml, else the row's own silver_label. reviewed becomes
true for every row this touches (i.e. every row in the dataset -- a
review pass covers the whole set, not just the overridden rows). A row
named in gold_overrides.yaml with ambiguous: true also gets its
`ambiguous` flag set.

Writes the updated dataset.jsonl in place (a .bak copy of the previous
version is kept alongside it), prints silver-vs-gold agreement: overall
rate and a per-category breakdown of what silver got wrong most, and
writes eval/categorization/dataset_metadata.json recording who reviewed
it, how, and the agreement rate -- see --review-method's default for why
that field matters (a reviewer who sees the silver label while reviewing
can anchor on it; a blind relabel of a sample is the way to check that
properly later).

Usage: python scripts/promote_to_gold.py [--reviewer NAME] [--review-method DESC]
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATASET_PATH = BASE_DIR / "eval" / "categorization" / "dataset.jsonl"
OVERRIDES_PATH = BASE_DIR / "eval" / "categorization" / "gold_overrides.yaml"
METADATA_PATH = BASE_DIR / "eval" / "categorization" / "dataset_metadata.json"


def _load_dataset() -> list[dict]:
    with DATASET_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    raw = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    return raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer", default="project owner")
    parser.add_argument(
        "--review-method", default="reviewed silver labels with the label visible",
        help="How the review was done -- recorded verbatim in dataset_metadata.json.",
    )
    args = parser.parse_args()

    if not DATASET_PATH.exists():
        raise SystemExit(f"No dataset at {DATASET_PATH} -- build it first (Part A5).")

    rows = _load_dataset()
    overrides = _load_overrides()
    print(f"{len(rows)} dataset rows, {len(overrides)} override(s) in {OVERRIDES_PATH.name}.")

    agreements = 0
    disagreements_by_silver_label: Counter = Counter()
    unreviewed_ids_used = set(overrides.keys())

    for row in rows:
        override = overrides.get(row["id"])
        silver = row.get("silver_label")
        if override is not None:
            gold = override["label"]
            if "ambiguous" in override:
                row["ambiguous"] = bool(override["ambiguous"])
            if override.get("notes"):
                row["notes"] = override["notes"]
            unreviewed_ids_used.discard(row["id"])
        else:
            gold = silver
        row["gold_label"] = gold
        row["reviewed"] = True
        if gold == silver:
            agreements += 1
        else:
            disagreements_by_silver_label[silver] += 1

    if unreviewed_ids_used:
        print(f"WARNING: gold_overrides.yaml has id(s) not found in the dataset: {sorted(unreviewed_ids_used)}")

    if DATASET_PATH.exists():
        shutil.copy(DATASET_PATH, DATASET_PATH.with_suffix(".jsonl.bak"))
    with DATASET_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    total = len(rows)
    agreement_rate = agreements / total if total else 0.0
    print(f"\nSilver -> gold agreement: {agreements}/{total} ({agreement_rate:.1%})")
    if disagreements_by_silver_label:
        print("Silver labels most often overridden (silver_label -> count):")
        for label, count in disagreements_by_silver_label.most_common():
            print(f"  {label}: {count}")
    else:
        print("No disagreements -- every silver label was accepted as gold.")

    metadata = {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewer": args.reviewer,
        "review_method": args.review_method,
        "total_rows": total,
        "overrides_applied": len(overrides),
        "silver_gold_agreement": agreements / total if total else None,
        "silver_gold_agreements": agreements,
        "silver_gold_disagreements": total - agreements,
        "anchoring_risk_note": (
            "The reviewer saw each row's silver label while reviewing it, which can anchor "
            "the reviewer toward agreeing with what's already shown rather than forming an "
            "independent judgment -- a 100% (or near-100%) agreement rate under this method "
            "should not be read as proof the silver labels are error-free. A blind relabel of "
            "a sample (reviewer sees the document only, not the silver label) would test this "
            "properly and is the recommended follow-up before leaning heavily on this eval set."
        ),
    }
    METADATA_PATH.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {METADATA_PATH}")


if __name__ == "__main__":
    main()
