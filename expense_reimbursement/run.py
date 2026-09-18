"""Step 4 -- run the pipeline (parse -> extract -> validate) over every
PDF in uploads/, printing and saving a full report per document.
"""

import json
import sys
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from extract import ExtractResult, extract_claim
from parse import OUTPUTS_DIR, parse_pdf
from review_view import build_review_view
from validate import CheckResult, build_claim, check_completeness, validate_claim

UPLOADS_DIR = Path(__file__).resolve().parent / "uploads"

# Approximate, publicly-listed Groq per-token rates for openai/gpt-oss-120b
# at the time this was written -- NOT verified against a live pricing API.
# Check https://console.groq.com/settings/billing for current rates before
# treating this as authoritative; it's a rough order-of-magnitude estimate,
# not a billing figure.
PRICE_PER_M_PROMPT_TOKENS = Decimal("0.15")
PRICE_PER_M_COMPLETION_TOKENS = Decimal("0.75")


def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj)}")


def _estimate_cost(result: ExtractResult) -> Decimal:
    prompt = Decimal(result.prompt_tokens or 0)
    completion = Decimal(result.completion_tokens or 0)
    return (
        prompt / Decimal(1_000_000) * PRICE_PER_M_PROMPT_TOKENS
        + completion / Decimal(1_000_000) * PRICE_PER_M_COMPLETION_TOKENS
    )


def _print_checks(checks: list[CheckResult]) -> None:
    if not checks:
        print("  (no arithmetic checks defined for this document_type)")
        return
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        print(f"  [{status}] {c.name}")
        print(f"         {c.detail}")


def process_one(pdf_path: Path) -> dict:
    print(f"\n{'=' * 70}\n{pdf_path.name}\n{'=' * 70}")

    print(f"Parsing via LlamaParse (tier=agentic)...")
    markdown, raw_json = parse_pdf(pdf_path)
    print(f"  Raw markdown -> {OUTPUTS_DIR / (pdf_path.stem + '_raw.md')}")
    print(f"  Raw JSON     -> {OUTPUTS_DIR / (pdf_path.stem + '_raw.json')}")

    print("Classifying + extracting via Groq...")
    result = extract_claim(markdown, raw_json)
    claim = build_claim(result.document_type, result.raw_fields, markdown)

    print(f"\ndocument_type: {claim.document_type.value}")

    print("\nClean extracted JSON:")
    clean_json = claim.model_dump(mode="json")
    print(json.dumps(clean_json, indent=2, ensure_ascii=False, default=_decimal_default))

    print("\nadditional_fields (didn't fit the schema):")
    if claim.additional_fields:
        for k, v in claim.additional_fields.items():
            print(f"  {k}: {v}")
    else:
        print("  (none)")

    print("\nextraction_notes:")
    if claim.extraction_notes:
        for note in claim.extraction_notes:
            print(f"  - {note}")
    else:
        print("  (none)")

    print("\nValidation:")
    checks = validate_claim(claim, markdown)
    _print_checks(checks)
    checks_as_dicts = [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks]

    completeness_warnings = check_completeness(markdown, clean_json, claim.document_type.value)
    print("\nCompleteness check (local_conveyance_form only):")
    if completeness_warnings:
        for w in completeness_warnings:
            print(f"  [WARN] {w}")
    else:
        print("  (none)" if claim.document_type.value == "local_conveyance_form" else "  (not applicable to this document_type)")

    cost = _estimate_cost(result)
    print(f"\nTime taken: {result.duration_seconds:.2f}s")
    print(
        f"Tokens: prompt={result.prompt_tokens} completion={result.completion_tokens} "
        f"total={result.total_tokens}  (~${cost:.5f} estimated, unverified pricing -- see note in run.py)"
    )

    # build_review_view expects one dict with the claim's own fields plus
    # "validation" -- the same shape as clean_json, with the checks
    # merged in (validation isn't itself a claim field, it's this step's
    # own output, so it's added here rather than living on the schema).
    review_input = {**clean_json, "validation": checks_as_dicts, "completeness_warnings": completeness_warnings}
    review_view = build_review_view(review_input)
    print("\n" + "=" * 70)
    print("EMPLOYEE REVIEW VIEW")
    print("=" * 70)
    print(json.dumps(review_view, indent=2, ensure_ascii=False, default=_decimal_default))

    report = {
        "file": pdf_path.name,
        "document_type": claim.document_type.value,
        "clean_json": clean_json,
        "additional_fields": claim.additional_fields,
        "extraction_notes": claim.extraction_notes,
        "validation": checks_as_dicts,
        "completeness_warnings": completeness_warnings,
        "review_view": review_view,
        "duration_seconds": result.duration_seconds,
        "tokens": {
            "prompt": result.prompt_tokens,
            "completion": result.completion_tokens,
            "total": result.total_tokens,
        },
        "estimated_cost_usd": str(cost),
    }
    out_path = OUTPUTS_DIR / f"{pdf_path.stem}_result.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=_decimal_default), encoding="utf-8")
    print(f"\nFull report saved to: {out_path}")

    return report


def main():
    pdfs = sorted(UPLOADS_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {UPLOADS_DIR}")
        sys.exit(1)

    print(f"Found {len(pdfs)} PDF(s) in {UPLOADS_DIR}")
    reports = []
    for pdf_path in pdfs:
        reports.append(process_one(pdf_path))

    print(f"\n{'=' * 70}\nSummary\n{'=' * 70}")
    for r in reports:
        total_checks = len(r["validation"])
        passed = sum(1 for c in r["validation"] if c["passed"])
        print(f"  {r['file']:40s} type={r['document_type']:25s} checks={passed}/{total_checks} passed")


if __name__ == "__main__":
    main()
