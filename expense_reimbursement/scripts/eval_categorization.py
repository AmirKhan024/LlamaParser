"""Run categorize.py's methods against eval/categorization/dataset.jsonl
and write a results report: accuracy, macro-F1, per-class precision/
recall, confusion matrix, top confusions, latency, tokens, cost -- reported
three ways (all documents, real-source only, synthetic-only) -- plus the
default-method decision.

Two modes:
- Default: ground truth is each row's silver_label. Writes RESULTS_SILVER.md
  (the interim, pre-review report; kept for the record).
- --final: ground truth is each row's gold_label. REFUSES to run unless
  every row has reviewed=true (scripts/promote_to_gold.py). Writes
  RESULTS.md. Takes --methods (default: rules,classifier -- the free ones)
  and ACCUMULATES results across invocations in
  eval/categorization/_final_results_state.json, so adding a method later
  never re-runs (or re-spends anything on) a method already recorded.

Run mechanics (matter most for `llm`, which spends Groq quota):
- Every method's per-document prediction is cached to
  eval/categorization/_predictions_cache/<method>.jsonl and CHECKPOINTED
  after each document, so an interrupted run resumes instead of restarting.
  A re-run reuses the cache and makes zero API calls unless --no-cache.
- `llm` rows carry llm_config_fingerprint() (model + reasoning effort +
  prompt + few-shot examples + message format); a cached row from a
  different fingerprint is a miss, never silently reused.
- The exact request and raw response for every `llm` call are written to
  eval/categorization/_llm_raw_cache/<doc id>.json.
- If Groq quota runs out mid-run, the script exits with a message saying how
  many documents completed and does NOT touch RESULTS.md or the state file
  for that method -- partial results are never presented as a full run.
  Non-quota failures (network etc.) likewise leave the run incomplete;
  re-run to retry just those documents.

Usage:
  python scripts/eval_categorization.py                            # silver report (all methods)
  python scripts/eval_categorization.py --final                    # gold: rules + classifier -> RESULTS.md
  python scripts/eval_categorization.py --final --methods llm      # gold: adds llm; doesn't touch the others
  python scripts/eval_categorization.py --final --report-only      # re-render RESULTS.md from saved state, no method runs
  python scripts/eval_categorization.py --final --methods llm --no-cache   # ignore cached llm predictions
"""

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from groq import RateLimitError
from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support

import categorize as categorize_module
from categories import CATEGORIES, CATEGORY_IDS
from categorize import CategorizationInput, build_input, categorize, categorize_llm, llm_config_fingerprint

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent  # dataset rows store paths relative to the repo root (SROIE lives outside expense_reimbursement/)
EVAL_DIR = BASE_DIR / "eval" / "categorization"
DATASET_PATH = EVAL_DIR / "dataset.jsonl"
CACHE_DIR = EVAL_DIR / "_predictions_cache"
RAW_CACHE_DIR = EVAL_DIR / "_llm_raw_cache"
STATE_PATH = EVAL_DIR / "_final_results_state.json"
ALL_METHODS = ["rules", "classifier", "llm"]
DEFAULT_FINAL_METHODS = ["rules", "classifier"]  # llm needs a Groq call per row -- run it explicitly
NOT_PURSUED = {"hybrid": "not pursued — decision: two-method comparison was sufficient"}
REAL_SOURCES = {"sroie", "cord", "real"}


class QuotaExhausted(Exception):
    def __init__(self, method: str, completed: int, total: int):
        super().__init__(f"{method}: Groq quota exhausted after {completed}/{total} documents")
        self.method, self.completed, self.total = method, completed, total


class IncompleteRun(Exception):
    def __init__(self, method: str, errors: int, completed: int, total: int):
        super().__init__(f"{method}: {errors} document(s) failed with non-quota errors ({completed}/{total} completed)")
        self.method, self.errors, self.completed, self.total = method, errors, completed, total


# ---------------------------------------------------------------- data + cache

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
        for pred in predictions.values():
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")


def _write_raw(row_id: str, raw_sink: dict, fingerprint: str) -> None:
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"id": row_id, "config_fingerprint": fingerprint, **raw_sink}
    (RAW_CACHE_DIR / f"{row_id}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _row_input(row: dict) -> CategorizationInput:
    fields = row.get("extracted_fields") or {}
    markdown_path = row.get("markdown_path")
    markdown = ""
    if markdown_path:
        full_path = REPO_ROOT / markdown_path
        if full_path.exists():
            markdown = full_path.read_text(encoding="utf-8", errors="replace")
    return build_input(fields, markdown)


def _method_fingerprint(method: str):
    return llm_config_fingerprint() if method == "llm" else None


def _run_method(method: str, inp: CategorizationInput, raw_sink):
    if method == "llm":
        return categorize_llm(inp, raw_sink=raw_sink)
    return categorize(inp, method)


def _is_valid_cached(pred: dict, fingerprint) -> bool:
    return "error" not in pred and pred.get("config_fingerprint") == fingerprint


def _predict_all(rows: list[dict], method: str, *, use_cache: bool = True) -> dict[str, dict]:
    """Predictions for every row. Cached, valid rows are reused; the rest are
    run one at a time with a checkpoint after each. Raises QuotaExhausted
    (Groq rate limit that categorize.py's own backoff couldn't ride out) or
    IncompleteRun (any other per-row failure) -- never returns a result set
    that silently contains failed rows."""
    fingerprint = _method_fingerprint(method)
    cache = _load_cache(method) if use_cache else {}
    predictions = {k: v for k, v in cache.items() if _is_valid_cached(v, fingerprint)}
    row_ids = [r["id"] for r in rows]
    todo = [r for r in rows if r["id"] not in predictions]
    print(f"  {method}: {len(rows) - len(todo)}/{len(rows)} cached, {len(todo)} to run", flush=True)

    def completed() -> int:
        return sum(1 for rid in row_ids if rid in predictions and "error" not in predictions[rid])

    errors = 0
    for i, row in enumerate(todo, 1):
        inp = _row_input(row)
        raw_sink = {} if method == "llm" else None
        try:
            result = _run_method(method, inp, raw_sink)
        except RateLimitError as exc:
            _save_cache(method, predictions)
            raise QuotaExhausted(method, completed(), len(rows)) from exc
        except Exception as exc:  # noqa: BLE001 -- recorded so the row is retried next run; run ends incomplete
            errors += 1
            predictions[row["id"]] = {"id": row["id"], "category": None, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            _save_cache(method, predictions)
            print(f"  [{i}/{len(todo)}] {row['id']}: ERROR {type(exc).__name__}", flush=True)
            continue
        predictions[row["id"]] = {
            "id": row["id"],
            "category": result.category,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "latency_ms": result.latency_ms,
            "estimated_cost_usd": result.estimated_cost_usd,
            "parse_failure": result.parse_failure,
            "parse_failure_reason": result.parse_failure_reason,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "config_fingerprint": fingerprint,
        }
        if raw_sink:
            _write_raw(row["id"], raw_sink, fingerprint)
        _save_cache(method, predictions)  # checkpoint after every document
        note = " PARSE FAILURE" if result.parse_failure else ""
        print(f"  [{i}/{len(todo)}] {row['id']}: {result.category} ({result.latency_ms} ms, {result.total_tokens} tokens){note}", flush=True)

    if errors:
        raise IncompleteRun(method, errors, completed(), len(rows))
    return predictions


# --------------------------------------------------------------------- metrics

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
        latencies.append(pred.get("latency_ms", 0))
        costs.append(pred.get("estimated_cost_usd", 0.0))

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


def _run_stats(rows: list[dict], predictions: dict[str, dict], key: str) -> dict:
    scored = [r for r in rows if r.get(key) is not None]
    preds = [predictions[r["id"]] for r in scored]
    latencies = [p.get("latency_ms", 0) for p in preds]
    tokens = [p["total_tokens"] for p in preds if p.get("total_tokens") is not None]
    failures = [p for p in preds if p.get("parse_failure")]
    return {
        "n_scored": len(scored),
        "parse_failures": len(failures),
        "parse_failure_reasons": sorted({p.get("parse_failure_reason") or "?" for p in failures}),
        "mean_latency_ms": statistics.mean(latencies) if latencies else None,
        "total_tokens": sum(tokens) if tokens else None,
        "total_cost_usd": sum(p.get("estimated_cost_usd", 0.0) for p in preds),
    }


def _wilson(correct: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = correct / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - margin) / denom, (centre + margin) / denom


def _top_confusions(metrics: dict, limit: int = 5) -> list[tuple[str, str, int]]:
    labels, cm = metrics["labels"], metrics["confusion_matrix"]
    pairs = [(labels[i], labels[j], cm[i][j]) for i in range(len(labels)) for j in range(len(labels)) if i != j and cm[i][j] > 0]
    return sorted(pairs, key=lambda p: (-p[2], p[0], p[1]))[:limit]


# ------------------------------------------------------------------- rendering

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
    top = _top_confusions(metrics)
    if top:
        lines.append("- Top confusions (gold → predicted): " + "; ".join(f"{g} → {p} ({n})" for g, p, n in top))
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
            "-- most likely a Groq quota wall. Re-run once quota resets._\n"
        )
    lines = [f"## {method}", ""]
    stats = entry.get("run_stats") or {}
    if method == "llm":
        lines.append(
            f"- Model: `{entry.get('model')}`, temperature 0, reasoning effort: {entry.get('reasoning_effort') or 'provider default'}; "
            f"prompt/config fingerprint `{entry.get('config_fingerprint')}`"
        )
        lines.append(f"- **Parse failures / invalid category returns: {stats.get('parse_failures', 0)}** of {stats.get('n_scored')} scored documents"
                     + (f" ({'; '.join(stats['parse_failure_reasons'])})" if stats.get("parse_failure_reasons") else ""))
    if stats.get("mean_latency_ms") is not None:
        lines.append(f"- Mean latency per doc: {stats['mean_latency_ms']:.0f} ms")
    if stats.get("total_tokens") is not None:
        lines.append(f"- Total tokens for the run: {stats['total_tokens']:,} (est. ${stats['total_cost_usd']:.4f}, unverified Groq rates)")
    lines.append("")
    lines += _render_metrics_block("All documents", entry["metrics_all"])
    lines += _render_metrics_block("Real-source only (sroie + cord + real)", entry["metrics_real"], note_partial_coverage=True)
    lines += _render_metrics_block("Synthetic-only", entry["metrics_synth"])
    return "\n".join(lines)


def _error_analysis(rows: list[dict], predictions: dict[str, dict], ground_truth_key: str, method: str) -> str:
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
        return f"\n## Error analysis ({method})\n\nNo misclassifications -- 100% accuracy on this set.\n"

    lines = [f"\n## Error analysis ({method})\n", f"{sum(len(v) for v in groups.values())} misclassified row(s), grouped by (gold -> predicted):\n"]
    for (truth, predicted), group_rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"### {truth} -> {predicted} ({len(group_rows)})")
        for row in group_rows:
            vendor = (row.get("extracted_fields") or {}).get("vendor_name", "?")
            pred = predictions[row["id"]]
            detail = ""
            if pred.get("parse_failure"):
                detail = f" PARSE FAILURE: {pred.get('parse_failure_reason')}"
            elif method == "llm" and pred.get("rationale"):
                detail = f" model said: \"{pred['rationale']}\""
            lines.append(f"- `{row['id']}` ({row['source']}, vendor: {vendor}):{detail or ' ' + row.get('notes', '')}")
        lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------- decision

DECISION_RULE_TEXT = """## Decision rule

Fixed before the `llm` numbers were looked at.

- **PRIMARY metric:** accuracy on the real-source documents only (sroie + cord + real). Synthetic documents are known to inflate scores, so they don't decide. The real slice scores 43 documents: the 44 real documents minus one approval email that is evidence, not an expense, and has no category.
- **SECONDARY metric:** accuracy on all scored documents (n=133).
- If one method has the strictly highest accuracy on BOTH metrics, it becomes the default.
- If the winners are split (or first place is tied on either metric), the default stays `rules` -- zero cost, deterministic, no quota dependency -- and the split is explained explicitly, along with what would change the call.
- Cost and latency are a tiebreaker consideration only, never the deciding metric.
"""


def _best(state: dict, metric_slice: str) -> tuple[list[str], float]:
    scores = {m: state[m][metric_slice]["accuracy"] for m in ALL_METHODS if m in state and not state[m].get("unavailable") and state[m][metric_slice]["n"] > 0}
    top = max(scores.values())
    return sorted(m for m, s in scores.items() if s == top), top


def _decide(state: dict) -> dict:
    primary, primary_score = _best(state, "metrics_real")
    secondary, secondary_score = _best(state, "metrics_all")
    if len(primary) == 1 and primary == secondary:
        return {"default": primary[0], "split": False, "primary": primary, "secondary": secondary,
                "primary_score": primary_score, "secondary_score": secondary_score}
    return {"default": "rules", "split": True, "primary": primary, "secondary": secondary,
            "primary_score": primary_score, "secondary_score": secondary_score}


def _real_scored(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["source"] in REAL_SOURCES and r.get("gold_label") is not None]


def _paired_counts(rows: list[dict], state: dict, a: str, b: str) -> dict:
    """Same documents scored by both methods: how many both got right, only
    one got right, neither. Sharper than comparing two accuracy intervals."""
    counts = {"both": 0, "only_a": 0, "only_b": 0, "neither": 0}
    for r in rows:
        a_ok = state[a]["predictions"][r["id"]]["category"] == r["gold_label"]
        b_ok = state[b]["predictions"][r["id"]]["category"] == r["gold_label"]
        counts["both" if a_ok and b_ok else "only_a" if a_ok else "only_b" if b_ok else "neither"] += 1
    return counts


def _paired_section(rows: list[dict], state: dict) -> str:
    real = _real_scored(rows)
    lines = []
    for other in ("llm", "classifier"):
        if other in state and "rules" in state and not state[other].get("unavailable"):
            c = _paired_counts(real, state, "rules", other)
            lines.append(
                f"- Real documents, `rules` vs `{other}` (same {len(real)} documents): both right {c['both']}, "
                f"only `rules` right {c['only_a']}, only `{other}` right {c['only_b']}, both wrong {c['neither']}."
            )
    return "\n".join(lines)


def _top_real_confusion(rows: list[dict], predictions: dict[str, dict]):
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in _real_scored(rows):
        predicted = predictions[r["id"]]["category"] or "none"
        if predicted != r["gold_label"]:
            groups[(r["gold_label"], predicted)].append(r["id"])
    if not groups:
        return None
    (gold, predicted), ids = max(groups.items(), key=lambda kv: (len(kv[1]), kv[0]))
    return gold, predicted, ids


def _sensitivity_section(rows: list[dict], state: dict) -> str:
    """What-if only. The gold labels are frozen and are NOT changed; this
    just shows how much the primary-metric ranking leans on the largest
    real-document disagreement, so the decision isn't over-read."""
    llm = state.get("llm")
    if not llm or llm.get("unavailable"):
        return ""
    top = _top_real_confusion(rows, llm["predictions"])
    if top is None or len(top[2]) < 3:
        return ""
    gold, predicted, ids = top
    real = _real_scored(rows)
    idset = set(ids)
    definition = CATEGORIES[predicted].definition if predicted in CATEGORIES else "?"
    lines = [
        "## What-if: how much the primary metric leans on one label boundary (NOT applied)",
        "",
        f"`llm`'s largest real-document disagreement is gold `{gold}` predicted as `{predicted}` on {len(ids)} of {len(real)} real documents "
        f"({', '.join(f'`{i}`' for i in ids)}). `categories.py` defines `{predicted}` as \"{definition}\", and the model's stated reasons "
        "apply that wording literally. Whether these documents are `" + gold + "` or `" + predicted + "` is a label-boundary judgment; the "
        "gold label was `" + gold + "`, approved in review with the label visible (see the anchoring limitation).",
        "",
        "**The gold labels were not changed and the decision above stands.** For scale only: real-only accuracy if those "
        f"{len(ids)} documents had been labeled `{predicted}` instead:",
        "",
        "| method | real-only accuracy as recorded | real-only accuracy under the what-if |",
        "|---|---|---|",
    ]
    for method in ALL_METHODS:
        entry = state.get(method)
        if not entry or entry.get("unavailable"):
            continue
        recorded = sum(1 for r in real if entry["predictions"][r["id"]]["category"] == r["gold_label"])
        flipped = sum(
            1 for r in real
            if entry["predictions"][r["id"]]["category"] == (predicted if r["id"] in idset else r["gold_label"])
        )
        lines.append(f"| {method} | {recorded / len(real):.1%} ({recorded}/{len(real)}) | {flipped / len(real):.1%} ({flipped}/{len(real)}) |")
    lines.append("")
    lines.append(
        "`rules` scores these documents as correct only because its fallback for any generic receipt its keywords don't match is `other`, "
        "not because it recognises them; the ranking on the real slice therefore depends on where this one boundary is drawn. "
        "Fixing the wording in `categories.py` and re-running `llm` (a new prompt fingerprint, so a fresh run) would be a legitimate next step, "
        "but it is tuning on these results and was deliberately not done here."
    )
    lines.append("")
    return "\n".join(lines)


def _fmt_winners(winners: list[str]) -> str:
    return " and ".join(f"`{w}`" for w in winners) + (" (tied)" if len(winners) > 1 else "")


def _decision_outcome_text(state: dict, decision: dict, rows: list[dict]) -> str:
    lines = ["## Decision outcome", ""]
    lines.append(f"- Primary (real-only accuracy): {_fmt_winners(decision['primary'])} at {decision['primary_score']:.1%}.")
    lines.append(f"- Secondary (all-documents accuracy): {_fmt_winners(decision['secondary'])} at {decision['secondary_score']:.1%}.")
    if not decision["split"]:
        lines.append(f"- **One method wins both -> the default is `{decision['default']}`.**")
    else:
        lines.append("- **The winners are split (or first place is tied) -> the default stays `rules`.**")
        lines.append("")
        lines.append(_split_explanation(state, decision))
    paired = _paired_section(rows, state)
    if paired:
        lines.append("")
        lines.append(paired)
    lines.append("")
    lines.append("Cost and latency (tiebreaker consideration only, not what decided this):")
    for m in ALL_METHODS:
        if m in state and not state[m].get("unavailable"):
            stats = state[m].get("run_stats") or {}
            latency = f"{stats['mean_latency_ms']:.0f} ms" if stats.get("mean_latency_ms") is not None else "n/a"
            cost = state[m]["metrics_all"]["cost_per_1000_docs"]
            quota = "needs Groq quota and a network call" if m == "llm" else "no API dependency"
            lines.append(f"- `{m}`: mean {latency} per doc, ~${cost:.4f} per 1,000 docs, {quota}.")
    lines.append("")
    return "\n".join(lines)


def _split_explanation(state: dict, decision: dict) -> str:
    real = {m: state[m]["metrics_real"]["accuracy"] for m in ALL_METHODS if m in state and not state[m].get("unavailable")}
    ordered = ", ".join(f"`{m}` {a:.1%}" for m, a in sorted(real.items(), key=lambda kv: -kv[1]))
    challenger = next((m for m in decision["primary"] + decision["secondary"] if m != "rules"), None)
    parts = [f"Real-only accuracy: {ordered}."]
    if challenger:
        n_real = state[challenger]["metrics_real"]["n"]
        interval = _wilson(round(state[challenger]["metrics_real"]["accuracy"] * n_real), n_real)
        parts.append(
            f"With only {n_real} real documents, one document is worth {100 / n_real:.1f} points and `{challenger}`'s "
            f"real-only 95% interval is {interval[0]:.1%}–{interval[1]:.1%} -- differences of a few points on this slice are not "
            "distinguishable from noise."
        )
        parts.append(
            f"What would change the call: `{challenger}` becomes the default if it strictly beats `rules` on both real-only and "
            "all-documents accuracy -- most convincingly on a larger real-document set that covers more than the 7 categories "
            "this one does, and after checking the labels blind (see limitations)."
        )
    else:
        parts.append("What would change the call: another method strictly beating `rules` on both metrics.")
    return " ".join(parts)


# ----------------------------------------------------------------- comparison

def _comparison_table(state: dict) -> str:
    lines = [
        "## Comparison",
        "",
        "| method | all docs: accuracy | all docs: macro-F1 | real docs: accuracy (95% CI) | real docs: macro-F1† | synthetic: accuracy | parse failures | mean latency / doc | total tokens | est. cost / 1,000 docs |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for method in ALL_METHODS:
        entry = state.get(method)
        if entry is None:
            lines.append(f"| {method} | not yet run | | | | | | | | |")
            continue
        if entry.get("unavailable"):
            lines.append(f"| {method} | unavailable | | | | | | | | |")
            continue
        a, r, s = entry["metrics_all"], entry["metrics_real"], entry["metrics_synth"]
        stats = entry.get("run_stats") or {}
        interval = _wilson(round(r["accuracy"] * r["n"]), r["n"])
        latency = f"{stats['mean_latency_ms']:.0f} ms" if stats.get("mean_latency_ms") is not None else "n/a"
        tokens = f"{stats['total_tokens']:,}" if stats.get("total_tokens") is not None else "n/a"
        failures = str(stats.get("parse_failures", 0)) if method == "llm" else "n/a"
        lines.append(
            f"| {method} | {a['accuracy']:.1%} (n={a['n']}) | {a['macro_f1']:.3f} | "
            f"{r['accuracy']:.1%} (n={r['n']}; {interval[0]:.0%}–{interval[1]:.0%}) | {r['macro_f1']:.3f} | "
            f"{s['accuracy']:.1%} (n={s['n']}) | {failures} | {latency} | {tokens} | ${a['cost_per_1000_docs']:.4f} |"
        )
    for method, reason in NOT_PURSUED.items():
        lines.append(f"| {method} | {reason} | | | | | | | | |")
    lines.append("")
    lines.append(
        "† The real-only macro-F1 is averaged over only the 7 categories that slice contains "
        "(accommodation, local_transport, office_supplies_equipment, other, own_vehicle_mileage, phone_internet, "
        "travel_meals), not all 14. Estimated costs use unverified public Groq rates."
    )
    lines.append("")
    return "\n".join(lines)


def _llm_design_section(entry: dict) -> str:
    return f"""## The `llm` method: prompt design and provenance

- **Model / settings:** Groq `{entry.get('model')}`, temperature 0, reasoning effort {entry.get('reasoning_effort') or 'left at the provider default'}. One request per document; a Groq JSON-validation rejection is retried once, then recorded as a parse failure; 429s back off exponentially.
- **Prompt:** few-shot. The system prompt lists all 14 categories with the one-line definition from `categories.py`, plus the tie-break rules from `categories.py`. Three worked examples follow as user/assistant turns.
- **Where the few-shot examples came from:** hand-written for this prompt (an airline e-ticket, a client dinner, a hotel folio with minibar/laundry lines), using fictional vendors, clients and people. **None is a row of the 134-document eval set** -- `tests/test_llm_categorizer.py` checks that none of their names appear in `dataset.jsonl`, and none of the eval's documents were used to tune the prompt. The prompt was run once against the eval set; it was not iterated on the results.
- **What the model sees:** the Stage 1 extracted fields only -- `document_type`, vendor, date, amount + currency, line-item names, and `additional_fields` -- and never the OCR markdown. `additional_fields` is included on purpose: for the synthetic client/team dinners the distinguishing signal (client name, attendee count, "Team Dinner") lives only there, so a literal vendor/date/amount/line-items input would make `client_entertainment`, `team_events` and `travel_meals` indistinguishable by construction. **`rules` and `classifier` are not given identical information:** they read a 2,000-character OCR markdown excerpt. So an `llm`-vs-`rules` difference reflects input as well as method.
- **Output validation:** strict JSON `{{category, confidence, reason}}`; the category must equal one of the 14 ids exactly. Anything else (not JSON, no category, `null`, an id not on the list, wrong case) is a **parse failure**: stored as no answer, scored as a miss, and counted separately -- never coerced to the nearest category.
- **Caching:** every request/response is in `eval/categorization/_llm_raw_cache/<doc id>.json`; predictions are checkpointed per document in `_predictions_cache/llm.jsonl` with a config fingerprint so a changed prompt can never reuse stale rows.
"""


def _limitations_section(rows: list[dict], state: dict) -> str:
    items = [
        "**Mostly synthetic eval set.** 90 of 134 documents are synthetic, generated and labeled in the same session as the categorizers being measured; all-documents numbers are likely optimistic. That is why real-only accuracy is the primary metric.",
        "**The real slice covers only 7 of the 14 categories** (43 scored documents; 18 are `other`, 18 `travel_meals`). Real-only numbers say nothing about the other 7 categories, and a single document moves real-only accuracy by 2.3 points.",
        "**Label anchoring.** The project owner reviewed silver labels with the label visible and agreed with all 134 (100%). That is not independent confirmation; a blind relabel of a sample is the proper check and has not been done.",
        "**`rules` keywords were written in the same session as the eval set**, so `rules` may be partly tuned to the documents it is scored on.",
        "**`team_events` has 6 examples, not 8** (two deliberately ambiguous synthetic documents were relabeled `travel_meals` on review).",
        "**One category per document.** A hotel folio with a minibar line is filed entirely under `accommodation`.",
        "**Unequal inputs.** `rules` and `classifier` read a 2,000-character OCR markdown excerpt; `llm` reads only the Stage 1 extracted fields (including `additional_fields`). Any `llm`-vs-`rules` gap mixes model and input effects.",
        "**Single run, no variance estimate.** `llm` was run once at temperature 0; gpt-oss is not perfectly deterministic even then (noted in Stage 1's reliability numbers), and no repeat runs were done.",
        "**Same-author prompt.** The category definitions, tie-break rules, few-shot examples, rules keywords and eval labels were all written by the same author in the same project; the prompt was not tuned on eval results, but it was written knowing the shape of the eval.",
    ]
    llm = state.get("llm")
    if llm and not llm.get("unavailable"):
        stats = llm.get("run_stats") or {}
        synth, real = llm["metrics_synth"], llm["metrics_real"]
        if synth["n"] and real["n"]:
            items.append(
                f"**Synthetic inflation is visible in the `llm` numbers.** It scored {synth['accuracy']:.1%} on the {synth['n']} synthetic documents "
                f"but {real['accuracy']:.1%} on the {real['n']} real ones -- a gap of {100 * (synth['accuracy'] - real['accuracy']):.0f} points. "
                "The synthetic receipts are clean template renders whose vendor names, line items and additional fields make their category easy to read, "
                "so the all-documents figure mostly measures how easy they are."
            )
        top = _top_real_confusion(rows, llm["predictions"])
        real_errors = sum(1 for r in _real_scored(rows) if llm["predictions"][r["id"]]["category"] != r["gold_label"])
        if top and len(top[2]) >= 3:
            items.append(
                f"**{len(top[2])} of `llm`'s {real_errors} real-document misses are a single label boundary** (gold `{top[0]}` vs predicted `{top[1]}`), "
                f"caused by the wording of `{top[1]}`'s definition in `categories.py`, not by random error. The ranking on the primary metric is fragile "
                "to how that boundary is drawn -- see the what-if above."
            )
        rules = state.get("rules")
        if rules and not rules.get("unavailable"):
            other = rules["metrics_real"]["per_class"].get("other")
            if other and other["support"]:
                items.append(
                    f"**`rules`' real-only score is propped up by its `other` fallback.** {other['support']} of the {rules['metrics_real']['n']} real "
                    f"documents are `other`, and `rules` predicts `other` for any generic receipt its keywords don't match (recall {other['recall']:.0%}, "
                    f"precision {other['precision']:.0%}), so part of that score is the default rather than recognition."
                )
        items.append(
            f"**Cost and quota.** One `llm` pass over the {llm['metrics_all']['n']} scored documents used {stats.get('total_tokens', 0):,} tokens (the "
            "previous key's daily limit on this model was 200,000; about 1.5-1.9k tokens per document, mostly the fixed few-shot prompt) and averaged "
            f"{(stats.get('mean_latency_ms') or 0) / 1000:.1f} s per document on the shared tier, so an `llm` default would make the pipeline depend on Groq quota "
            "and latency for every upload."
        )
        if stats.get("parse_failures"):
            items.append(f"**`llm` returned {stats['parse_failures']} unusable answer(s)** ({'; '.join(stats.get('parse_failure_reasons', []))}); they are scored as misses, so they lower its accuracy rather than being repaired.")
    return "## Limitations\n\n" + "\n".join(f"- {item}" for item in items) + "\n"


def _not_yet_run_section(state: dict) -> str:
    missing = [m for m in ALL_METHODS if m not in state]
    if not missing:
        return ""
    lines = ["\n## Not yet run\n"]
    for method in missing:
        lines.append(f"- **{method}**: `python scripts/eval_categorization.py --final --methods {method}`")
    lines.append("")
    return "\n".join(lines)


def _render_results(rows: list[dict], state: dict) -> str:
    present = [m for m in ALL_METHODS if m in state and not state[m].get("unavailable")]
    parts = ["# Categorization eval results (FINAL, gold labels)", "", DECISION_RULE_TEXT, _comparison_table(state)]
    decision = _decide(state) if present else None
    if decision:
        parts.append(_decision_outcome_text(state, decision, rows))
    if "llm" in state and not state["llm"].get("unavailable"):
        parts.append(_llm_design_section(state["llm"]))
    for method in ALL_METHODS:
        if method in state:
            parts.append(_render_method_section(method, state[method]))
    if decision:
        analyzed = [decision["default"]]
        if "llm" in present and "llm" not in analyzed:
            analyzed.append("llm")
        for method in analyzed:
            parts.append(_error_analysis(rows, state[method]["predictions"], "gold_label", method))
    parts.append(_sensitivity_section(rows, state))
    parts.append(_limitations_section(rows, state))
    parts.append(_not_yet_run_section(state))
    return "\n".join(parts)


# ---------------------------------------------------------------------- modes

def _build_entry(rows: list[dict], method: str, predictions: dict[str, dict], key: str) -> dict:
    entry = {
        "unavailable": False,
        "metrics_all": _metrics_for(rows, predictions, key),
        "metrics_real": _metrics_for([r for r in rows if r["source"] in REAL_SOURCES], predictions, key),
        "metrics_synth": _metrics_for([r for r in rows if r["source"] == "synthetic_image"], predictions, key),
        "run_stats": _run_stats(rows, predictions, key),
        "predictions": predictions,
    }
    if method == "llm":
        entry.update(model=categorize_module.LLM_MODEL, reasoning_effort=categorize_module.LLM_REASONING_EFFORT,
                     config_fingerprint=llm_config_fingerprint())
    return entry


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _run_final(rows: list[dict], methods: list[str], *, use_cache: bool, report_only: bool) -> None:
    state = _load_state()
    if not report_only:
        for method in methods:
            print(f"Running {method} on {len(rows)} rows...", flush=True)
            predictions = _predict_all(rows, method, use_cache=use_cache)  # raises rather than return a partial run
            state[method] = _build_entry(rows, method, predictions, "gold_label")
            _save_state(state)  # a completed method is recorded even if a later one in this call is interrupted
            m = state[method]["metrics_all"]
            print(f"  {method}: accuracy {m['accuracy']:.1%}, macro-F1 {m['macro_f1']:.3f}", flush=True)
    for method, entry in state.items():
        if not entry.get("unavailable"):
            entry["run_stats"] = _run_stats(rows, entry["predictions"], "gold_label")
    _save_state(state)

    out_path = EVAL_DIR / "RESULTS.md"
    out_path.write_text(_render_results(rows, state), encoding="utf-8")
    print(f"\nWrote {out_path}")


def _run_silver(rows: list[dict]) -> None:
    sections, unavailable = [], {}
    for method in ALL_METHODS:
        print(f"Running {method} on {len(rows)} rows...", flush=True)
        try:
            predictions = _predict_all(rows, method)
        except (QuotaExhausted, IncompleteRun) as exc:
            print(f"  {exc}")
            unavailable[method] = exc
            sections.append(
                f"## {method}\n\n_Unavailable this run: {exc}. Re-run once quota resets._\n"
            )
            continue
        entry = _build_entry(rows, method, predictions, "silver_label")
        sections.append(_render_method_section(method, entry))
    banner = (
        "\n**These numbers use silver labels (Claude Code's own judgment against categories.py's "
        "definitions and tie-break rules -- not the categorizer being evaluated, and not human-"
        "reviewed gold labels). Treat as directional only until RESULTS.md exists.**\n"
    )
    out = "# Categorization eval results (SILVER -- interim, not final)\n" + banner + "\n" + "\n".join(sections)
    out_path = EVAL_DIR / "RESULTS_SILVER.md"
    out_path.write_text(out, encoding="utf-8")
    print(f"\nWrote {out_path}")


def main() -> None:
    # override=True: a stale machine-level GROQ_API_KEY must not beat the project's .env
    load_dotenv(override=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--methods", default=None, help="Comma-separated subset of rules,classifier,llm (--final only).")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached predictions for the requested methods and re-run them.")
    parser.add_argument("--report-only", action="store_true", help="--final only: re-render RESULTS.md from saved state without running any method.")
    args = parser.parse_args()

    if not DATASET_PATH.exists():
        raise SystemExit(f"No dataset at {DATASET_PATH} -- build it first (Part A5).")
    rows = _load_dataset()

    if not args.final:
        if args.methods or args.report_only or args.no_cache:
            raise SystemExit("--methods, --no-cache and --report-only only apply with --final.")
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
        hint = f" ({'; '.join(f'{m}: {NOT_PURSUED[m]}' for m in unknown if m in NOT_PURSUED)})" if unknown & set(NOT_PURSUED) else ""
        raise SystemExit(f"Unknown method(s): {sorted(unknown)}{hint}. Choose from {ALL_METHODS}.")

    try:
        _run_final(rows, methods, use_cache=not args.no_cache, report_only=args.report_only)
    except QuotaExhausted as exc:
        print(
            f"\nGroq quota exhausted: {exc.completed}/{exc.total} documents completed for '{exc.method}'.\n"
            f"Progress is checkpointed in eval/categorization/_predictions_cache/{exc.method}.jsonl (raw responses in "
            "_llm_raw_cache/). Re-run the same command once quota resets to resume -- completed documents are "
            "not re-run. RESULTS.md was NOT updated for this method."
        )
        sys.exit(2)
    except IncompleteRun as exc:
        print(
            f"\n{exc}. Those documents are retried on the next run; completed ones are not re-run. "
            "RESULTS.md was NOT updated for this method."
        )
        sys.exit(3)


if __name__ == "__main__":
    main()
