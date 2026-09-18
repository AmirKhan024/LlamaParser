"""Run all four categorizers (categorize.py) against eval/categorization/
dataset.jsonl and write a results report: accuracy, macro-F1, per-class
precision/recall, confusion matrix, latency p50, cost per 1,000 docs, and
the same split by source (real: sroie/cord/real vs synthetic_image).

Two modes:
- Default: ground truth is each row's silver_label. Writes RESULTS_SILVER.md.
  Every row need not be reviewed -- this is the interim, "before my
  review" number.
- --final: ground truth is each row's gold_label. REFUSES to run unless
  every row has reviewed=true (i.e. scripts/promote_to_gold.py has been
  run) -- the final number must never be quietly computed against
  unreviewed silver labels. Writes RESULTS.md, with an added error
  analysis section (every misclassified example of the best-macro-F1
  method, grouped by (gold, predicted) pair).

Caches each method's per-row prediction to eval/categorization/
_predictions_cache/<method>.jsonl, keyed by row id -- re-running this
script (e.g. after adding rows) never re-spends a Groq call (llm/hybrid)
for a row already predicted by that method.

Usage:
  python scripts/eval_categorization.py             # writes RESULTS_SILVER.md
  python scripts/eval_categorization.py --final      # writes RESULTS.md
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support

from categories import CATEGORY_IDS
from categorize import CategorizationInput, categorize

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent  # dataset rows store paths relative to the repo root (SROIE lives outside expense_reimbursement/)
DATASET_PATH = BASE_DIR / "eval" / "categorization" / "dataset.jsonl"
CACHE_DIR = BASE_DIR / "eval" / "categorization" / "_predictions_cache"
METHODS = ["rules", "llm", "classifier", "hybrid"]
REAL_SOURCES = {"sroie", "cord", "real"}


def _load_dataset() -> list[dict]:
    with DATASET_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_cache(method: str) -> dict[str, dict]:
    path = CACHE_DIR / f"{method}.jsonl"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {(row := json.loads(line))["id"]: row for line in f if line.strip()}


def _save_cache(method: str, predictions: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with (CACHE_DIR / f"{method}.jsonl").open("w", encoding="utf-8") as f:
        for row_id, pred in predictions.items():
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")


def _row_input(row: dict) -> CategorizationInput:
    fields = row.get("extracted_fields") or {}
    markdown_path = row.get("markdown_path")
    markdown = ""
    if markdown_path:
        full_path = REPO_ROOT / markdown_path
        if full_path.exists():
            markdown = full_path.read_text(encoding="utf-8", errors="replace")
    from categorize import build_input
    return build_input(fields, markdown)


def _predict_all(rows: list[dict], method: str) -> dict[str, dict]:
    cache = _load_cache(method)
    predictions = dict(cache)
    for row in rows:
        if row["id"] in predictions:
            continue
        inp = _row_input(row)
        result = categorize(inp, method)
        predictions[row["id"]] = {
            "id": row["id"],
            "category": result.category,
            "confidence": result.confidence,
            "latency_ms": result.latency_ms,
            "estimated_cost_usd": result.estimated_cost_usd,
        }
    if len(predictions) > len(cache):
        _save_cache(method, predictions)
    return predictions


def _metrics_for(rows: list[dict], predictions: dict[str, dict], ground_truth_key: str) -> dict:
    y_true, y_pred = [], []
    latencies, costs = [], []
    for row in rows:
        truth = row.get(ground_truth_key)
        if truth is None:
            continue  # non-categorizable document (e.g. approval_correspondence) -- excluded from accuracy
        pred = predictions[row["id"]]
        y_true.append(truth)
        y_pred.append(pred["category"] or "none")
        latencies.append(pred["latency_ms"])
        costs.append(pred["estimated_cost_usd"])

    if not y_true:
        return {"n": 0}

    labels = sorted(set(y_true) | set(y_pred))
    accuracy = sum(t == p for t, p in zip(y_true, y_pred)) / len(y_true)
    macro_f1 = f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0
    )
    cm = confusion_matrix(y_true, y_pred, labels=labels)

    return {
        "n": len(y_true),
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "labels": labels,
        "per_class": {
            label: {"precision": precision[i], "recall": recall[i], "support": int(support[i])}
            for i, label in enumerate(labels)
        },
        "confusion_matrix": cm.tolist(),
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "cost_per_1000_docs": (statistics.mean(costs) * 1000) if costs else 0.0,
    }


def _format_confusion_matrix(labels: list[str], cm: list[list[int]]) -> str:
    header = "| true \\ pred | " + " | ".join(labels) + " |"
    sep = "|---" * (len(labels) + 1) + "|"
    lines = [header, sep]
    for label, row in zip(labels, cm):
        lines.append(f"| {label} | " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def _render_method_section(method: str, overall: dict, by_source: dict[str, dict]) -> str:
    lines = [f"## {method}", ""]
    if overall["n"] == 0:
        lines.append("_No categorizable rows._\n")
        return "\n".join(lines)
    lines.append(f"- n = {overall['n']}")
    lines.append(f"- Accuracy: {overall['accuracy']:.1%}")
    lines.append(f"- Macro-F1: {overall['macro_f1']:.3f}")
    lines.append(f"- Latency p50: {overall['latency_p50_ms']:.0f} ms" if overall["latency_p50_ms"] is not None else "- Latency p50: n/a")
    lines.append(f"- Estimated cost per 1,000 docs: ${overall['cost_per_1000_docs']:.4f}")
    lines.append("")
    lines.append("### Per-class precision/recall")
    lines.append("| category | precision | recall | support |")
    lines.append("|---|---|---|---|")
    for label, stats in overall["per_class"].items():
        lines.append(f"| {label} | {stats['precision']:.2f} | {stats['recall']:.2f} | {stats['support']} |")
    lines.append("")
    lines.append("### Confusion matrix")
    lines.append(_format_confusion_matrix(overall["labels"], overall["confusion_matrix"]))
    lines.append("")
    lines.append("### By source")
    lines.append("| source | n | accuracy | macro-F1 |")
    lines.append("|---|---|---|---|")
    for source_name, stats in by_source.items():
        if stats["n"] == 0:
            lines.append(f"| {source_name} | 0 | n/a | n/a |")
        else:
            lines.append(f"| {source_name} | {stats['n']} | {stats['accuracy']:.1%} | {stats['macro_f1']:.3f} |")
    lines.append("")
    return "\n".join(lines)


def _error_analysis(rows: list[dict], predictions: dict[str, dict], ground_truth_key: str, best_method: str) -> str:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        truth = row.get(ground_truth_key)
        if truth is None:
            continue
        pred = predictions[row["id"]]
        predicted = pred["category"] or "none"
        if predicted != truth:
            groups[(truth, predicted)].append(row)

    if not groups:
        return f"\n## Error analysis ({best_method})\n\nNo misclassifications -- 100% accuracy on this set.\n"

    lines = [f"\n## Error analysis ({best_method})\n", f"{sum(len(v) for v in groups.values())} misclassified row(s), grouped by (gold -> predicted):\n"]
    for (truth, predicted), group_rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {truth} -> {predicted} ({len(group_rows)})")
        for row in group_rows:
            vendor = (row.get("extracted_fields") or {}).get("vendor_name", "?")
            lines.append(f"- `{row['id']}` ({row['source']}, vendor: {vendor}): {row.get('notes', '')}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    if not DATASET_PATH.exists():
        raise SystemExit(f"No dataset at {DATASET_PATH} -- build it first (Part A5).")
    rows = _load_dataset()

    ground_truth_key = "gold_label" if args.final else "silver_label"
    if args.final and not all(row.get("reviewed") for row in rows):
        unreviewed = [row["id"] for row in rows if not row.get("reviewed")]
        raise SystemExit(
            f"--final requires every row reviewed -- {len(unreviewed)} row(s) aren't: "
            f"{unreviewed[:10]}{'...' if len(unreviewed) > 10 else ''}. "
            "Run scripts/promote_to_gold.py first."
        )

    all_predictions = {}
    for method in METHODS:
        print(f"Running {method} on {len(rows)} rows...")
        all_predictions[method] = _predict_all(rows, method)

    sections = []
    best_method, best_f1 = None, -1.0
    for method in METHODS:
        predictions = all_predictions[method]
        overall = _metrics_for(rows, predictions, ground_truth_key)
        by_source = {
            "real (sroie+cord+real)": _metrics_for([r for r in rows if r["source"] in REAL_SOURCES], predictions, ground_truth_key),
            "synthetic_image": _metrics_for([r for r in rows if r["source"] == "synthetic_image"], predictions, ground_truth_key),
        }
        sections.append(_render_method_section(method, overall, by_source))
        if overall["n"] > 0 and overall["macro_f1"] > best_f1:
            best_f1, best_method = overall["macro_f1"], method

    title = "# Categorization eval results (FINAL, gold labels)" if args.final else "# Categorization eval results (SILVER -- interim, not final)"
    banner = "" if args.final else (
        "\n**These numbers use silver labels (one LLM's own judgment), not human-reviewed gold "
        "labels. Treat as directional only until RESULTS.md exists.**\n"
    )
    body = "\n".join(sections)
    out = f"{title}\n{banner}\n{body}"
    if args.final:
        out += _error_analysis(rows, all_predictions[best_method], ground_truth_key, best_method)
        out += f"\n\n_Best method by macro-F1: **{best_method}** ({best_f1:.3f})._\n"

    out_path = BASE_DIR / "eval" / "categorization" / ("RESULTS.md" if args.final else "RESULTS_SILVER.md")
    out_path.write_text(out, encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
