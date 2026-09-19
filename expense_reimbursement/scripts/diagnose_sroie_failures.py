"""Follow-up to verify_sroie_retry_fix.py: for the SROIE receipts that still
failed extraction after the JSON-validate retry, capture Groq's actual error
(including `failed_generation`) and check whether the PRODUCTION model
(extract.MODEL, gpt-oss-120b) handles them -- separating "the retry doesn't
work" from "the fallback model can't do this receipt".

Calls Groq directly (one call per model per receipt, no retry) so the error
body is visible. Writes eval/categorization/sroie_failure_diagnosis.json and,
like verify_sroie_retry_fix.py, never touches outputs/sroie_*_result.json
(the eval set is frozen).

Usage: python scripts/diagnose_sroie_failures.py [STEM ...]
       (defaults to the two receipts that still failed in the verification run)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from groq import BadRequestError, Groq

import extract

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "outputs"
REPORT_PATH = BASE_DIR / "eval" / "categorization" / "sroie_failure_diagnosis.json"
FALLBACK_MODEL = "openai/gpt-oss-20b"
DEFAULT_STEMS = ["X51005442322", "X51005442334"]


def _try(client: Groq, model: str, system: str, user: str) -> dict:
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(r.choices[0].message.content or "{}")
        return {"outcome": "success", "total_tokens": r.usage.total_tokens,
                "document_type": parsed.get("document_type") if isinstance(parsed, dict) else None}
    except BadRequestError as exc:
        error = (exc.body or {}).get("error", {}) if isinstance(exc.body, dict) else {}
        return {"outcome": "failed", "code": error.get("code"), "message": (error.get("message") or "")[:200],
                "failed_generation": (error.get("failed_generation") or "")[:300]}


def main() -> None:
    stems = sys.argv[1:] or DEFAULT_STEMS
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    system = extract._build_system_prompt()
    report = []
    for stem in stems:
        markdown = (OUT_DIR / f"sroie_{stem}_raw.md").read_text(encoding="utf-8")
        user = ("=== DOCUMENT MARKDOWN ===\n\n" + markdown +
                "\n\n=== SUPPORTING JSON (layout/image data stripped) ===\n\n" + json.dumps({"pages": []}))
        entry = {"stem": stem, "markdown_chars": len(markdown),
                 "fallback_model": {"model": FALLBACK_MODEL, **_try(client, FALLBACK_MODEL, system, user)},
                 "production_model": {"model": extract.MODEL, **_try(client, extract.MODEL, system, user)}}
        report.append(entry)
        print(f"{stem}: fallback={entry['fallback_model']['outcome']} production={entry['production_model']['outcome']}", flush=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
