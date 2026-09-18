"""Run categorize.py's methods against eval/categorization/dataset.jsonl
and write a results report: accuracy, macro-F1, per-class precision/
recall, confusion matrix, latency p50, cost per 1,000 docs -- reported
three ways (all documents, real-source only, synthetic-only).

Two modes:
- Default: ground truth is each row's silver_label. Writes RESULTS_SILVER.md.
  Every row need not be reviewed -- this is the interim, "before my
  review" number. Always attempts all four methods.
- --final: ground truth is each row's gold_label. REFUSES to run unless
  every row has reviewed=true (i.e. scripts/promote_to_gold.py has been
  run). Writes RESULTS.md. Takes --methods (default: rules,classifier --
  llm/hybrid need a Groq call per row and are left for a separate run,
  see below) and ACCUMULATES results across separate invocations in
  eval/categorization/_final_results_state.json, so running one more
  method later never re-runs (or re-spends a Groq call for) a method
  already recorded. Adds an error-analysis section for the best-so-far
  method and a "NOT YET RUN" section listing the exact command for any
  method missing from the state.

Caches each method's per-row prediction to eval/categorization/
_predictions_cache/<method>.jsonl, keyed by row id -- re-running this
script (e.g. after adding rows) never re-spends a Groq call (llm/hybrid)
for a row already predicted by that method. A cached row that failed
last time (e.g. a quota wall) is retried, not treated as permanent.

Usage:
  python scripts/eval_categorization.py                          # silver, all 4 methods -> RESULTS_SILVER.md
  python scripts/eval_categorization.py --final                  # gold, rules+classifier -> RESULTS.md
  python scripts/eval_categorization.py --final --methods llm     # adds llm to RESULTS.md, doesn't touch the rest
  python scripts/eval_categorization.py --final --methods hybrid  # same, for hybrid
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
from categorize import CategorizationInput, build_input, categorize

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent  # dataset rows store paths relative to the repo root (SROIE lives outside expense_reimbursement/)
DATASET_PATH = BASE_DIR / "eval" / "categorization" / "dataset.jsonl"
CACHE_DIR = BASE_DIR / "eval" / "categorization" / "_predictions_cache"
STATE_PATH = BASE_DIR / "eval" / "categorization" / "_final_results_state.json"
ALL_METHODS = ["rules", "llm", "classifier", "hybrid"]
DEFAULT_FINAL_METHODS = ["rules", "classifier"]  # llm/hybrid need a Groq call/row -- run separately, see docstring
REAL_SOURCES = {"sroie", "cord", "real"}
MAX_ERROR_RATE = 0.2  # above this, a method's numbers this run aren't meaningful (e.g. a Groq quota wall)


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
    return build_input(fields, markdown)


def _predict_all(rows: list[dict], method: str) -> tuple[dict[str, dict], int]:
    """Returns (predictions, error_count). A per-row failure (e.g. a
    Groq daily quota exhausted) is recorded as category=None with
    confidence 0.0 rather than crashing the whole eval run -- the
    caller decides whether too many failures means this method's
    numbers for this run aren't meaningful (see MAX_ERROR_RATE)."""
    cache = _load_cache(method)
    # A cached row with an "error" key failed last run (e.g. a quota
    # wall) -- retry it rather than treating that failure as permanent.
    predictions = {k: v for k, v in cache.items() if "error" not in v}
    errors = 0
    for row in rows:
        if row["id"] in predictions:
            continue
        inp = _row_input(row)
        try:
            result = categorize(inp, method)
            predictions[row["id"]] = {
                "id": row["id"],
                "category": result.category,
                "confidence": result.confidence,
                "latency_ms": result.latency_ms,
                "estimated_cost_usd": result.estimated_cost_usd,
            }
        except Exception as exc:  # noqa: BLE001 -- keep going, the caller reports the error rate
            errors += 1
            predictions[row["id"]] = {
                "id": row["id"], "category": None, "confidence": 0.0,
                "latency_ms": 0, "estimated_cost_usd": 0.0, "error": str(exc)[:200],
            }
    if len(predictions) > len(cache):
        _save_cache(method, predictions)
    return predictions, errors


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
    categories_covered = sorted(set(y_true))  # categories gold actually contains in this slice

    return {
        "n": len(y_true),
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "labels": labels,
        "categories_covered": categories_covered,
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


def _render_metrics_block(heading: str, metrics: dict, note_partial_coverage: bool = False) -> list[str]:
    lines = [f"### {heading}", ""]
    if metrics["n"] == 0:
        lines.append("_No categorizable rows in this slice._\n")
        return lines
    lines.append(f"- n = {metrics['n']}")
    lines.append(f"- Accuracy: {metrics['accuracy']:.1%}")
    lines.append(f"- Macro-F1: {metrics['macro_f1']:.3f}")
    if note_partial_coverage:
        covered = metrics["categories_covered"]
        lines.append(
            f"- **Categories covered: {len(covered)}/{len(CATEGORY_IDS)}** ({', '.join(covered)}) -- "
            "macro-F1 above is only averaged over these categories, not all 14. Treat it as a read on "
            "this slice's categories, not overall category coverage."
        )
    lines.append(f"- Latency p50: {metrics['latency_p50_ms']:.0f} ms" if metrics["latency_p50_ms"] is not None else "- Latency p50: n/a")
    lines.append(f"- Estimated cost per 1,000 docs: ${metrics['cost_per_1000_docs']:.4f}")
    lines.append("")
    lines.append("| category | precision | recall | support |")
    lines.append("|---|---|---|---|")
    for label, stats in metrics["per_class"].items():
        lines.append(f"| {label} | {stats['precision']:.2f} | {stats['recall']:.2f} | {stats['support']} |")
    lines.append("")
    lines.append("Confusion matrix:")
    lines.append("")
    lines.append(_format_confusion_matrix(metrics["labels"], metrics["confusion_matrix"]))
    lines.append("")
    return lines


def _render_method_section(method: str, entry: dict) -> str:
    if entry.get("unavailable"):
        return (
            f"## {method}\n\n_Unavailable: {entry['errors']}/{entry['total_rows']} calls failed "
            f"(see the per-row `error` field in eval/categorization/_predictions_cache/{method}.jsonl) "
            "-- most likely every usable Groq model's daily quota was exhausted while this eval set "
            "was being built. Re-run once quota resets._\n"
        )
    lines = [f"## {method}", ""]
    lines += _render_metrics_block("All documents", entry["metrics_all"])
    lines += _render_metrics_block("Real-source only (sroie + cord + real)", entry["metrics_real"], note_partial_coverage=True)
    lines += _render_metrics_block("Synthetic-only", entry["metrics_synth"])
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


def _not_yet_run_section(missing_methods: list[str]) -> str:
    if not missing_methods:
        return ""
    lines = ["\n## Not yet run\n"]
    for method in missing_methods:
        lines.append(
            f"- **{method}**: `python scripts/eval_categorization.py --final --methods {method}` -- "
            f"standalone (doesn't touch {', '.join(m for m in ALL_METHODS if m != method)}), uses "
            f"eval/categorization/_predictions_cache/{method}.jsonl for any row already predicted "
            "(nothing already done gets re-spent), and appends its section into this file via "
            "_final_results_state.json without rerunning anything else."
        )
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ silver

def _run_silver(rows: list[dict]) -> None:
    all_predictions = {}
    unavailable_methods = {}
    for method in ALL_METHODS:
        print(f"Running {method} on {len(rows)} rows...")
        predictions, errors = _predict_all(rows, method)
        all_predictions[method] = predictions
        if errors > 0:
            error_rate = errors / len(rows)
            print(f"  {errors}/{len(rows)} rows failed ({error_rate:.0%})")
            if error_rate > MAX_ERROR_RATE:
                unavailable_methods[method] = errors

    sections = []
    for method in ALL_METHODS:
        if method in unavailable_methods:
            sections.append(
                f"## {method}\n\n_Unavailable this run: {unavailable_methods[method]}/{len(rows)} calls "
                "failed (see the per-row `error` field in eval/categorization/_predictions_cache/"
                f"{method}.jsonl) -- most likely every usable Groq model's daily quota was exhausted "
                "while this eval set was being built. Re-run once quota resets._\n"
            )
            continue
        predictions = all_predictions[method]
        metrics_all = _metrics_for(rows, predictions, "silver_label")
        metrics_real = _metrics_for([r for r in rows if r["source"] in REAL_SOURCES], predictions, "silver_label")
        metrics_synth = _metrics_for([r for r in rows if r["source"] == "synthetic_image"], predictions, "silver_label")
        sections.append(_render_method_section(method, {
            "metrics_all": metrics_all, "metrics_real": metrics_real, "metrics_synth": metrics_synth,
        }))

    title = "# Categorization eval results (SILVER -- interim, not final)"
    banner = (
        "\n**These numbers use silver labels (Claude Code's own judgment against categories.py's "
        "definitions and tie-break rules -- not the categorizer being evaluated, and not human-"
        "reviewed gold labels). Treat as directional only until RESULTS.md exists.**\n"
    )
    if unavailable_methods:
        banner += f"\n**{', '.join(unavailable_methods)} could not be evaluated this run.**\n"
    out = f"{title}\n{banner}\n" + "\n".join(sections)
    out_path = BASE_DIR / "eval" / "categorization" / "RESULTS_SILVER.md"
    out_path.write_text(out, encoding="utf-8")
    print(f"\nWrote {out_path}")


# ------------------------------------------------------------------- final

def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _run_final(rows: list[dict], methods: list[str]) -> None:
    state = _load_state()

    for method in methods:
        print(f"Running {method} on {len(rows)} rows...")
        predictions, errors = _predict_all(rows, method)
        error_rate = errors / len(rows) if rows else 0.0
        if error_rate > MAX_ERROR_RATE:
            print(f"  {errors}/{len(rows)} rows failed ({error_rate:.0%}) -- marking unavailable")
            state[method] = {"unavailable": True, "errors": errors, "total_rows": len(rows)}
            continue

        metrics_all = _metrics_for(rows, predictions, "gold_label")
        metrics_real = _metrics_for([r for r in rows if r["source"] in REAL_SOURCES], predictions, "gold_label")
        metrics_synth = _metrics_for([r for r in rows if r["source"] == "synthetic_image"], predictions, "gold_label")
        state[method] = {
            "unavailable": False,
            "metrics_all": metrics_all, "metrics_real": metrics_real, "metrics_synth": metrics_synth,
            # predictions kept for the error-analysis section if this method turns out best -- small
            # dataset (134 rows), fine to keep inline rather than re-deriving from the cache file.
            "predictions": predictions,
        }
        print(f"  {method}: accuracy {metrics_all['accuracy']:.1%}, macro-F1 {metrics_all['macro_f1']:.3f}")

    _save_state(state)

    sections = []
    best_method, best_f1 = None, -1.0
    for method in ALL_METHODS:
        entry = state.get(method)
        if entry is None:
            continue
        sections.append(_render_method_section(method, entry))
        if not entry.get("unavailable") and entry["metrics_all"]["n"] > 0 and entry["metrics_all"]["macro_f1"] > best_f1:
            best_f1, best_method = entry["metrics_all"]["macro_f1"], method

    title = "# Categorization eval results (FINAL, gold labels)"
    out = f"{title}\n\n" + "\n".join(sections)
    if best_method is not None:
        out += _error_analysis(rows, state[best_method]["predictions"], "gold_label", best_method)
        out += f"\n\n_Best method by macro-F1 (all documents) among those run: **{best_method}** ({best_f1:.3f})._\n"
    else:
        out += "\n\n_No method produced usable results yet._\n"

    missing = [m for m in ALL_METHODS if m not in state]
    out += _not_yet_run_section(missing)

    out_path = BASE_DIR / "eval" / "categorization" / "RESULTS.md"
    out_path.write_text(out, encoding="utf-8")
    print(f"\nWrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--methods", default=None, help="Comma-separated subset of rules,llm,classifier,hybrid (--final only).")
    args = parser.parse_args()

    if not DATASET_PATH.exists():
        raise SystemExit(f"No dataset at {DATASET_PATH} -- build it first (Part A5).")
    rows = _load_dataset()

    if not args.final:
        if args.methods:
            raise SystemExit("--methods only applies with --final.")
        _run_silver(rows)
        return

    if not all(row.get("reviewed") for row in rows):
        unreviewed = [row["id"] for row in rows if not row.get("reviewed")]
        raise SystemExit(
            f"--final requires every row reviewed -- {len(unreviewed)} row(s) aren't: "
            f"{unreviewed[:10]}{'...' if len(unreviewed) > 10 else ''}. "
            "Run scripts/promote_to_gold.py first."
        )
    methods = args.methods.split(",") if args.methods else DEFAULT_FINAL_METHODS
    unknown = set(methods) - set(ALL_METHODS)
    if unknown:
        raise SystemExit(f"Unknown method(s): {unknown}. Choose from {ALL_METHODS}.")
    _run_final(rows, methods)


if __name__ == "__main__":
    main()
