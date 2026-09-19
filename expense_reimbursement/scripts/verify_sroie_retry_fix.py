"""Empirically check the extract.py JSON-validate retry fix against the 5
SROIE receipts that originally failed extraction (see SUMMARY.md's Stage 2
section and the commit that added the retry).

Re-runs extract_claim on each receipt's cached markdown (no LlamaParse
call) with the SAME model the originals failed on (openai/gpt-oss-20b),
through a counting wrapper so we can tell whether the retry was actually
exercised: attempts=1 means the first call worked (the original failure
didn't reproduce -- transient), attempts=2 with success means the retry
recovered it, and a failure means the fix doesn't cover it.

Writes eval/categorization/sroie_retry_verification.json -- and, on
purpose, NOT outputs/sroie_*_result.json: build_eval_dataset.py globs
those, and the eval set is frozen at 134 rows (re-running it with 5 more
SROIE results would renumber ids and silently change the set).

Usage: python scripts/verify_sroie_retry_fix.py
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(override=True)  # a stale machine-level GROQ_API_KEY must not beat the project's .env

import extract
from groq import Groq

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "outputs"
REPORT_PATH = BASE_DIR / "eval" / "categorization" / "sroie_retry_verification.json"
MODEL = "openai/gpt-oss-20b"  # what the originals failed on
STEMS = ["X51005433518", "X51005433556", "X51005442322", "X51005442334", "X51005442382"]


class _CountingClient:
    def __init__(self, real: Groq):
        self._real = real
        self.calls: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        try:
            response = self._real.chat.completions.create(**kwargs)
            self.calls.append("ok")
            return response
        except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised for extract.py to handle
            body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
            self.calls.append(f"{type(exc).__name__}:{(body.get('error') or {}).get('code')}")
            raise


def main() -> None:
    import os

    real = Groq(api_key=os.environ["GROQ_API_KEY"])
    report = []
    for stem in STEMS:
        markdown = (OUT_DIR / f"sroie_{stem}_raw.md").read_text(encoding="utf-8")
        counting = _CountingClient(real)
        entry = {"stem": stem, "model": MODEL, "markdown_chars": len(markdown)}
        with patch.object(extract, "_get_client", return_value=counting):
            try:
                result = extract.extract_claim(markdown, {}, model=MODEL)
                entry.update(
                    outcome="success", document_type=result.document_type.value,
                    vendor_name=result.raw_fields.get("vendor_name"),
                    amount=result.raw_fields.get("amount") or result.raw_fields.get("grand_total"),
                )
            except Exception as exc:  # noqa: BLE001
                entry.update(outcome="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        entry["groq_calls"] = counting.calls
        report.append(entry)
        print(f"{stem}: {entry['outcome']} (calls: {counting.calls})", flush=True)

    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
