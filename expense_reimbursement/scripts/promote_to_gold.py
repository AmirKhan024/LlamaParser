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
version is kept alongside it) and prints silver-vs-gold agreement: overall
rate and a per-category breakdown of what silver got wrong most.

Usage: python scripts/promote_to_gold.py
"""

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATASET_PATH = BASE_DIR / "eval" / "categorization" / "dataset.jsonl"
OVERRIDES_PATH = BASE_DIR / "eval" / "categorization" / "gold_overrides.yaml"


def _load_dataset() -> list[dict]:
    with DATASET_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    raw = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    return raw


def main() -> None:
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


if __name__ == "__main__":
    main()
