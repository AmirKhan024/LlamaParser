"""Accuracy evaluation of the existing extraction pipeline against the
CORD-v2 receipt dataset's ground truth.

NEW, separate script. Does not modify parse.py / extract.py / validate.py
/ schemas.py / run.py -- it only calls them, exactly as they are, and
scores what comes back. If the pipeline does badly on CORD receipts
(e.g. no line-items schema field, India-specific document types being a
poor fit for a Korean/international retail receipt), that is a finding
to report, not something this script works around.

Ground truth structure was NOT guessed -- confirmed by loading and
printing real samples first. Key facts that shaped the mapping below:
  - sample["ground_truth"] is a JSON string; the parsed dict's real
    content lives under a top-level "gt_parse" key.
  - gt_parse["menu"] is a dict when there's exactly one item, and a
    list of dicts when there's more than one -- a real, confirmed CORD
    quirk, not an edge case to shrug off.
  - gt_parse["sub_total"] is entirely absent in some samples (4 of the
    first 15 checked); gt_parse["total"]["total_price"] is absent in at
    least one. Both are handled as "ground truth doesn't have this
    field" (both_absent when the extractor also lacks it), never as an
    extraction error.
  - No sample in the first 15 has any vendor/store-name field anywhere
    in gt_parse -- confirmed, not assumed from the prompt's warning.
    Reported as "not present in ground truth" for every sample, never
    scored as a miss.

Usage:
    python eval_cord.py
"""

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from datasets import load_dataset  # noqa: E402

from extract import extract_claim  # noqa: E402
from parse import parse_pdf  # noqa: E402
from validate import build_claim, parse_amount, validate_claim  # noqa: E402

NUM_SAMPLES = 15
NAME_MATCH_THRESHOLD = 0.6
SLEEP_BETWEEN_SAMPLES_SECONDS = 3  # light throttle against the shared Groq TPM cap

ROOT = Path(__file__).resolve().parent
CORD_EVAL_DIR = ROOT / "outputs" / "cord_eval"
TEMP_IMAGES_DIR = CORD_EVAL_DIR / "images"

# document_types that could plausibly describe a generic retail/restaurant
# receipt image -- used only to flag likely-forced classifications for
# the report; CORD has no ground truth for document_type itself, so this
# is a heuristic aid for a human to skim, not a scored metric.
_PLAUSIBLE_FOR_RECEIPT = {
    "restaurant_bill", "generic_receipt", "taxi_receipt",
    "hotel_invoice", "fuel_receipt", "unstructured_proof",
}


# ---------------------------------------------------------------------------
# Ground truth mapping
# ---------------------------------------------------------------------------
def _as_list(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _gt_line_items(gt_parse: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten gt_parse['menu'] into a flat list of {'nm','price','cnt'}
    dicts. A menu entry's own 'sub' (e.g. a combo add-on, seen in real
    samples) is included as its own line item too -- it has its own
    name/price and could plausibly match something the extractor found
    as a separate item.
    """
    items = []
    for entry in _as_list(gt_parse.get("menu")):
        if entry.get("nm") is not None or entry.get("price") is not None:
            items.append({"nm": entry.get("nm"), "price": entry.get("price"), "cnt": entry.get("cnt")})
        for sub_entry in _as_list(entry.get("sub")):
            items.append({"nm": sub_entry.get("nm"), "price": sub_entry.get("price"), "cnt": sub_entry.get("cnt")})
    return items


@dataclass
class GroundTruth:
    grand_total: Optional[str]
    subtotal: Optional[str]
    tax_total: Optional[str]
    line_items: List[Dict[str, Any]]
    vendor_name: Optional[str]  # always None for CORD -- see module docstring


def map_ground_truth(gt_parse: Dict[str, Any]) -> GroundTruth:
    total = gt_parse.get("total") or {}
    sub_total = gt_parse.get("sub_total") or {}
    return GroundTruth(
        grand_total=total.get("total_price"),
        subtotal=sub_total.get("subtotal_price"),
        tax_total=sub_total.get("tax_price"),
        line_items=_gt_line_items(gt_parse),
        vendor_name=None,
    )


# ---------------------------------------------------------------------------
# Extracted-side field lookup
# ---------------------------------------------------------------------------
def _find_amount_like(clean_json: Dict[str, Any], top_level_names: List[str], substr_hints: List[str]) -> Optional[str]:
    """Check named schema fields first (in priority order), then fall
    back to additional_fields by substring match on the key name -- a
    lot of what a CORD receipt has may not have a named field at all
    (see module docstring), so ignoring additional_fields here would
    unfairly make the pipeline look worse than it is.
    """
    for name in top_level_names:
        value = clean_json.get(name)
        if value not in (None, ""):
            return str(value)
    for key, value in (clean_json.get("additional_fields") or {}).items():
        if value in (None, ""):
            continue
        key_lower = key.lower()
        if any(hint in key_lower for hint in substr_hints):
            return str(value)
    return None


def extracted_grand_total(clean_json: Dict[str, Any]) -> Optional[str]:
    return _find_amount_like(clean_json, ["grand_total", "total", "amount"], ["grand_total", "total"])


def extracted_subtotal(clean_json: Dict[str, Any]) -> Optional[str]:
    return _find_amount_like(clean_json, ["subtotal"], ["subtotal", "sub_total"])


def extracted_tax(clean_json: Dict[str, Any]) -> Optional[str]:
    if clean_json.get("document_type") == "restaurant_bill":
        cgst, _ = parse_amount(str(clean_json["cgst"])) if clean_json.get("cgst") not in (None, "") else (None, None)
        sgst, _ = parse_amount(str(clean_json["sgst"])) if clean_json.get("sgst") not in (None, "") else (None, None)
        if cgst is not None or sgst is not None:
            return str((cgst or Decimal(0)) + (sgst or Decimal(0)))
    return _find_amount_like(clean_json, [], ["tax", "vat", "gst"])


# additional_fields is a flat dict[str, str] (see schemas.py) -- it has
# no way to hold a nested list, so when the model captures receipt line
# items with no schema field to put them in, it does the next best thing
# and flattens them into individually-numbered keys like "item_1_name" /
# "item_1_price" / "item_1_quantity". Confirmed by inspecting real
# sample output (see conversation), not assumed -- this was the
# ACTUAL observed shape, not a guess about what the model might do.
_NUMBERED_ITEM_KEY = re.compile(
    r"^item[_\s]?(\d+)[_\s]?(name|price|amount|quantity|qty|description|desc)$", re.IGNORECASE
)


def _numbered_items_from_additional_fields(additional_fields: Dict[str, str]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, str]] = {}
    for key, value in additional_fields.items():
        m = _NUMBERED_ITEM_KEY.match(key.strip())
        if not m:
            continue
        idx, field_name = m.group(1), m.group(2).lower()
        groups.setdefault(idx, {})[field_name] = value

    items = []
    for idx in sorted(groups, key=int):
        g = groups[idx]
        items.append({
            "nm": g.get("name") or g.get("description") or g.get("desc"),
            "price": g.get("price") or g.get("amount"),
            "cnt": g.get("quantity") or g.get("qty"),
        })
    return items


def find_extracted_line_items(clean_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """No schema currently has a line-items array for restaurant_bill or
    generic_receipt (only LocalConveyanceForm has travel_entries, which
    is unrelated to receipts) -- so there is no guaranteed structured
    home for CORD's menu items. This checks every place one plausibly
    could have landed, in order:
      1. a top-level field that's already a list of dicts (forward-
         compatible with a future schema change, without this script
         needing an update)
      2. a JSON-stringified list inside an additional_fields value
      3. additional_fields keys following the "item_N_<field>" pattern
         the model actually uses today (see _NUMBERED_ITEM_KEY above)
    Returning [] after all three still means nothing was found -- a
    real, reportable finding, not a failure of this script.
    """
    if clean_json.get("document_type") == "local_conveyance_form" and isinstance(clean_json.get("travel_entries"), list):
        return []  # not a receipt-items field in this context

    for key, value in clean_json.items():
        if key == "additional_fields":
            continue
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            return value

    additional_fields = clean_json.get("additional_fields") or {}
    for value in additional_fields.values():
        if isinstance(value, str) and value.strip().startswith("["):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, list):
                return parsed

    numbered = _numbered_items_from_additional_fields(additional_fields)
    if numbered:
        return numbered

    return []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_field(extracted_value: Any, ground_truth_value: Any, field_type: str = "text") -> str:
    """Returns 'match', 'mismatch', or 'both_absent'."""
    if extracted_value is None and ground_truth_value is None:
        return "both_absent"
    if extracted_value is None or ground_truth_value is None:
        return "mismatch"  # one has it, one doesn't
    if field_type == "amount":
        e, _ = parse_amount(str(extracted_value))
        g, _ = parse_amount(str(ground_truth_value).replace(",", ""))
        if e is None or g is None:
            return "mismatch"
        return "match" if abs(e - g) < Decimal("1.0") else "mismatch"
    return "match" if str(extracted_value).strip().lower() == str(ground_truth_value).strip().lower() else "mismatch"


def _item_name(item: Dict[str, Any]) -> str:
    for key in ("nm", "name", "description", "item", "item_name"):
        if item.get(key):
            return str(item[key])
    return ""


def score_line_items(extracted_items: List[Dict[str, Any]], gt_items: List[Dict[str, Any]],
                      name_threshold: float = NAME_MATCH_THRESHOLD) -> Dict[str, Any]:
    """Match each ground-truth item to its best-scoring extracted item by
    name similarity (difflib) above `name_threshold`; a name match alone
    counts as "matched" (price is recorded but not required, since
    receipt line-item prices are frequently ambiguous about
    per-unit-vs-line-total) -- greedy, one extracted item consumed per
    match. Returns matched/missed/extra counts plus detail for
    spot-checking.
    """
    remaining = list(enumerate(extracted_items))
    matched, missed = [], []

    for gt_item in gt_items:
        gt_name = _item_name(gt_item).strip().lower()
        best_pos, best_ratio = None, 0.0
        for pos, (_, ext_item) in enumerate(remaining):
            ext_name = _item_name(ext_item).strip().lower()
            ratio = difflib.SequenceMatcher(None, gt_name, ext_name).ratio() if gt_name and ext_name else 0.0
            if ratio > best_ratio:
                best_ratio, best_pos = ratio, pos
        if best_pos is not None and best_ratio >= name_threshold:
            _, ext_item = remaining.pop(best_pos)
            matched.append({"gt": gt_item, "extracted": ext_item, "name_ratio": round(best_ratio, 2)})
        else:
            missed.append(gt_item)

    extra = [ext_item for _, ext_item in remaining]
    return {
        "matched": len(matched), "missed": len(missed), "extra": len(extra), "total_gt": len(gt_items),
        "matched_detail": matched, "missed_detail": missed, "extra_detail": extra,
    }


# ---------------------------------------------------------------------------
# Per-sample pipeline run
# ---------------------------------------------------------------------------
@dataclass
class SampleResult:
    index: int
    success: bool
    error: Optional[str] = None
    document_type: Optional[str] = None
    clean_json: Optional[Dict[str, Any]] = None
    ground_truth: Optional[Dict[str, Any]] = None
    field_scores: Dict[str, str] = field(default_factory=dict)
    line_item_score: Optional[Dict[str, Any]] = None
    duration_seconds: float = 0.0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


def run_one_sample(index: int, image, gt_parse: Dict[str, Any]) -> SampleResult:
    TEMP_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    image_path = TEMP_IMAGES_DIR / f"cord_{index:02d}.png"
    image.save(image_path)

    try:
        markdown, raw_json = parse_pdf(image_path)
        result = extract_claim(markdown, raw_json)
        claim = build_claim(result.document_type, result.raw_fields, markdown)
        validate_claim(claim)  # exercised for parity with run.py; not scored against CORD
    except Exception as e:
        return SampleResult(index=index, success=False, error=f"{type(e).__name__}: {e}")

    clean_json = claim.model_dump(mode="json")
    gt = map_ground_truth(gt_parse)

    field_scores = {
        "vendor_name": (
            "not_in_ground_truth" if gt.vendor_name is None
            else score_field(clean_json.get("vendor_name"), gt.vendor_name)
        ),
        "grand_total": score_field(extracted_grand_total(clean_json), gt.grand_total, "amount"),
        "subtotal": score_field(extracted_subtotal(clean_json), gt.subtotal, "amount"),
        "tax_total": score_field(extracted_tax(clean_json), gt.tax_total, "amount"),
    }

    extracted_items = find_extracted_line_items(clean_json)
    line_item_score = score_line_items(extracted_items, gt.line_items)

    return SampleResult(
        index=index, success=True, document_type=claim.document_type.value,
        clean_json=clean_json, ground_truth=gt.__dict__,
        field_scores=field_scores, line_item_score=line_item_score,
        duration_seconds=result.duration_seconds,
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _field_accuracy(results: List[SampleResult], field_name: str) -> str:
    scores = [r.field_scores[field_name] for r in results if r.success and field_name in r.field_scores]
    scoreable = [s for s in scores if s in ("match", "mismatch")]
    if not scoreable:
        return "n/a (ground truth never has this field)"
    matched = sum(1 for s in scoreable if s == "match")
    return f"{matched}/{len(scoreable)} ({100 * matched / len(scoreable):.0f}%)"


def print_summary(results: List[SampleResult]) -> None:
    print(f"\n{'=' * 100}\nPer-sample summary\n{'=' * 100}")
    header = f"{'#':>2}  {'doc_type':22s}  {'grand_total':12s}  {'subtotal':12s}  {'tax_total':12s}  {'items (m/gt/extra)':20s}"
    print(header)
    print("-" * len(header))
    for r in results:
        if not r.success:
            print(f"{r.index:>2}  FAILED: {r.error}")
            continue
        li = r.line_item_score
        items_str = f"{li['matched']}/{li['total_gt']}/{li['extra']}"
        print(
            f"{r.index:>2}  {r.document_type:22s}  {r.field_scores['grand_total']:12s}  "
            f"{r.field_scores['subtotal']:12s}  {r.field_scores['tax_total']:12s}  {items_str:20s}"
        )

    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    print(f"\n{'=' * 100}\nAggregate\n{'=' * 100}")
    print(f"Samples: {len(results)} total, {len(successful)} succeeded, {len(failed)} failed (pipeline error, not scored)")
    if failed:
        for r in failed:
            print(f"  sample {r.index}: {r.error}")

    print(f"\nField-level accuracy (match / scoreable, i.e. excluding samples where ground truth lacks the field):")
    for f in ["grand_total", "subtotal", "tax_total"]:
        print(f"  {f:12s}: {_field_accuracy(successful, f)}")
    vendor_scoreable = [r for r in successful if r.field_scores.get("vendor_name") != "not_in_ground_truth"]
    print(f"  vendor_name : ground truth had this field in {len(vendor_scoreable)}/{len(successful)} samples")

    total_matched = sum(r.line_item_score["matched"] for r in successful if r.line_item_score)
    total_missed = sum(r.line_item_score["missed"] for r in successful if r.line_item_score)
    total_extra = sum(r.line_item_score["extra"] for r in successful if r.line_item_score)
    precision = total_matched / (total_matched + total_extra) if (total_matched + total_extra) else None
    recall = total_matched / (total_matched + total_missed) if (total_matched + total_missed) else None
    print(f"\nLine items, combined across all samples:")
    print(f"  matched={total_matched} missed={total_missed} extra={total_extra}")
    print(f"  precision = matched/(matched+extra) = {precision:.2f}" if precision is not None else "  precision = n/a (nothing extracted)")
    print(f"  recall    = matched/(matched+missed) = {recall:.2f}" if recall is not None else "  recall = n/a")
    samples_with_zero_extracted_items = sum(
        1 for r in successful if r.line_item_score and not r.line_item_score.get("extra") and not r.line_item_score.get("matched")
    )
    print(f"  samples with ZERO extracted line items: {samples_with_zero_extracted_items}/{len(successful)}")

    plausible = sum(1 for r in successful if r.document_type in _PLAUSIBLE_FOR_RECEIPT)
    forced = len(successful) - plausible
    print(f"\ndocument_type classification (heuristic -- CORD has no ground truth for this field):")
    print(f"  plausible fit for a retail receipt: {plausible}/{len(successful)}")
    print(f"  looks like a forced/wrong-category fit: {forced}/{len(successful)}")
    type_counts: Dict[str, int] = {}
    for r in successful:
        type_counts[r.document_type] = type_counts.get(r.document_type, 0) + 1
    for t, c in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {t}: {c}")

    if successful:
        avg_duration = sum(r.duration_seconds for r in successful) / len(successful)
        avg_tokens = sum(r.total_tokens or 0 for r in successful) / len(successful)
        print(f"\nAverage per document (Groq call only, successful samples): {avg_duration:.2f}s, {avg_tokens:.0f} tokens")
        print("(LlamaParse time/cost not included above -- see per-sample files for markdown/JSON parse artifacts)")


def main():
    print(f"Loading naver-clova-ix/cord-v2 test split...")
    dataset = load_dataset("naver-clova-ix/cord-v2", split="test")
    print(f"Dataset has {len(dataset)} samples; evaluating the first {NUM_SAMPLES}.")

    CORD_EVAL_DIR.mkdir(parents=True, exist_ok=True)
    results: List[SampleResult] = []

    for i in range(NUM_SAMPLES):
        sample = dataset[i]
        gt_parse = json.loads(sample["ground_truth"])["gt_parse"]
        print(f"\n[{i + 1}/{NUM_SAMPLES}] Processing sample {i}...")

        result = run_one_sample(i, sample["image"], gt_parse)
        results.append(result)

        if result.success:
            print(f"  document_type={result.document_type}  "
                  f"grand_total={result.field_scores['grand_total']}  "
                  f"line_items matched={result.line_item_score['matched']}/{result.line_item_score['total_gt']}")
        else:
            print(f"  FAILED: {result.error}")

        out_path = CORD_EVAL_DIR / f"sample_{i:02d}.json"
        out_path.write_text(
            json.dumps({
                "index": result.index, "success": result.success, "error": result.error,
                "document_type": result.document_type, "clean_json": result.clean_json,
                "ground_truth": result.ground_truth, "field_scores": result.field_scores,
                "line_item_score": result.line_item_score, "duration_seconds": result.duration_seconds,
                "tokens": {"prompt": result.prompt_tokens, "completion": result.completion_tokens, "total": result.total_tokens},
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if i < NUM_SAMPLES - 1:
            time.sleep(SLEEP_BETWEEN_SAMPLES_SECONDS)

    print_summary(results)
    print(f"\nPer-sample raw results saved under: {CORD_EVAL_DIR}")


if __name__ == "__main__":
    main()
