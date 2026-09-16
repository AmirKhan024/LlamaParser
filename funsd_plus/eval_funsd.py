"""
Score LlamaParse output against FUNSD+ ground-truth key-value pairs.

Generic, regex-based extraction of candidate (key, value) pairs from the
LlamaParse markdown (no per-document hardcoding): bold "**Key**: Value"
lines, plain "Key: Value" lines, and 2-column markdown table rows. A
ground-truth pair counts as correctly identified if some candidate pair
has a fuzzy-matching key AND a fuzzy-matching value.
"""

import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(sys.argv[1])
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"

KEY_MATCH_THRESHOLD = 0.6
VALUE_MATCH_THRESHOLD = 0.75

BOLD_KV_RE = re.compile(r"^\*?\s*\*\*(.+?)\*\*:?\s+(\S.*)")
BOLD_KEY_ONLY_RE = re.compile(r"^\*?\s*\*\*(.+?)\*\*:?\s*$")
PLAIN_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /_.#-]{1,40}):\s+(.+)$")
INLINE_KV_RE = re.compile(r"(?:(?<=\s)|^)([A-Z][A-Za-z][A-Za-z /]{0,20}):\s+(\S.*?)(?=\s{2,}|$)")
TABLE_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|$")


def normalize(s: str) -> str:
    s = re.sub(r"[*_`#]", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def extract_candidate_pairs(markdown: str):
    pairs = []
    pending_key = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()

        if pending_key is not None:
            if not line:
                continue  # blank line between "* **Key**:" and its value
            pairs.append((pending_key, line))
            pending_key = None
            continue

        if not line or line.startswith("|---") or set(line) <= {"|", "-", " "}:
            continue

        m = TABLE_ROW_RE.match(line)
        if m and "---" not in m.group(1):
            pairs.append((m.group(1), m.group(2)))
            continue

        m = BOLD_KV_RE.match(line)
        if m:
            pairs.append((m.group(1), m.group(2)))
            continue

        m = BOLD_KEY_ONLY_RE.match(line)
        if m:
            pending_key = m.group(1)  # value expected on a following line
            continue

        m = PLAIN_KV_RE.match(line)
        if m:
            pairs.append((m.group(1), m.group(2)))
        for m in INLINE_KV_RE.finditer(line):
            pairs.append((m.group(1), m.group(2)))
    return pairs


def score_document(gt_pairs, markdown: str):
    candidates = extract_candidate_pairs(markdown)
    results = []
    for gt in gt_pairs:
        best = max(
            (
                min(similarity(gt["key"], c[0]), similarity(gt["value"], c[1]))
                for c in candidates
            ),
            default=0.0,
        )
        key_val_ok = any(
            similarity(gt["key"], c[0]) >= KEY_MATCH_THRESHOLD
            and similarity(gt["value"], c[1]) >= VALUE_MATCH_THRESHOLD
            for c in candidates
        )
        value_anywhere = normalize(gt["value"]) in normalize(markdown)
        results.append(
            {
                "key": gt["key"],
                "value": gt["value"],
                "key_value_pair_correct": key_val_ok,
                "value_found_anywhere": value_anywhere,
            }
        )
    return results


def main():
    manifest = json.loads((DATA_DIR / "_ground_truth.json").read_text(encoding="utf-8"))

    per_doc = {}
    total_pairs = 0
    total_kv_correct = 0
    total_value_found = 0
    docs_all_correct = 0
    docs_with_pairs = 0

    for doc_id, gt in manifest.items():
        gt_pairs = gt["key_value_pairs"]
        md_path = OUT_DIR / f"{doc_id}.md"
        if not md_path.exists() or not gt_pairs:
            continue
        markdown = md_path.read_text(encoding="utf-8")
        results = score_document(gt_pairs, markdown)

        n = len(results)
        kv_correct = sum(r["key_value_pair_correct"] for r in results)
        value_found = sum(r["value_found_anywhere"] for r in results)

        per_doc[doc_id] = {
            "num_pairs": n,
            "key_value_pairs_correct": kv_correct,
            "key_value_accuracy": round(kv_correct / n, 4),
            "value_found_anywhere": value_found,
            "value_recall": round(value_found / n, 4),
            "pairs": results,
        }

        total_pairs += n
        total_kv_correct += kv_correct
        total_value_found += value_found
        docs_with_pairs += 1
        if kv_correct == n:
            docs_all_correct += 1

    summary = {
        "documents_scored": docs_with_pairs,
        "total_key_value_pairs": total_pairs,
        "key_value_pairs_correct": total_kv_correct,
        "key_value_accuracy_overall": round(total_kv_correct / total_pairs, 4),
        "value_found_anywhere_overall": round(total_value_found / total_pairs, 4),
        "documents_with_all_pairs_correct": docs_all_correct,
        "documents_with_all_pairs_correct_pct": round(docs_all_correct / docs_with_pairs, 4),
    }

    (ROOT / "eval_results.json").write_text(
        json.dumps({"summary": summary, "per_document": per_doc}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))
    print(f"\nWrote {ROOT / 'eval_results.json'}")


if __name__ == "__main__":
    main()
