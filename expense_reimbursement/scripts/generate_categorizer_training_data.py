"""Generate synthetic training data for the `classifier` categorizer.

For each category in categories.py, asks Groq (openai/gpt-oss-20b, higher
temperature for variety) for short receipt-like texts built ONLY from that
category's own definition/examples -- never from an eval document, so this
script must run and be reviewed before eval/categorization/dataset.jsonl
(Part A5) exists, and never reads that file.

Caches each batch's raw response to eval/categorization/_gen_cache/ so a
re-run after an interruption doesn't re-spend Groq credits on batches
already generated. Output: eval/categorization/train_synthetic.jsonl,
~50 rows per category (id, category, text, vendor, source).

Usage: python scripts/generate_categorizer_training_data.py
"""

import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from groq import BadRequestError, Groq, RateLimitError

from categories import CATEGORIES

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_PATH = BASE_DIR / "eval" / "categorization" / "train_synthetic.jsonl"
CACHE_DIR = BASE_DIR / "eval" / "categorization" / "_gen_cache"
MODEL = "openai/gpt-oss-20b"
BATCHES_PER_CATEGORY = 2
ROWS_PER_BATCH = 25  # 2 x 25 = 50 per category


def _client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")
    return Groq(api_key=api_key)


def _call_with_retry(client: Groq, **kwargs):
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError:
            attempt += 1
            if attempt > 5:
                raise
            time.sleep((2 ** attempt) + random.uniform(0, 1))


def _prompt_for(category_id: str, batch_index: int, n: int) -> tuple[str, str]:
    cat = CATEGORIES[category_id]
    system = (
        "You generate SHORT, REALISTIC-LOOKING synthetic training examples for an "
        "expense-categorization model. Each example is the kind of text you'd get after "
        "OCR/parsing a real receipt, invoice, or claim form -- a vendor line, a date, a "
        "line item or two, an amount, maybe a GSTIN. Use entirely FICTIONAL vendor and "
        "person names (never a real company). Mix Indian (INR, GSTIN-style) and "
        "international (USD/EUR/GBP) examples. Make some of them slightly messy, the way "
        "real OCR output is -- an odd line break, a misread character, inconsistent "
        "spacing -- but still clearly readable. Vary length and structure across examples; "
        "do not reuse the same template repeatedly."
    )
    user = (
        f"Category: {cat.label}\n"
        f"Definition: {cat.definition}\n"
        f"Example expenses in this category: {'; '.join(cat.include_examples)}\n\n"
        f"Generate {n} DIFFERENT short synthetic receipt/invoice/form texts (batch {batch_index}) "
        f"that all belong to this category. Each should be 2-6 lines of text, standalone "
        "(as if it were the whole parsed document). Return ONLY a JSON object: "
        '{"examples": [{"text": "...", "vendor": "..."}, ...]} with exactly '
        f"{n} entries in the array. No markdown fences, no commentary."
    )
    return system, user


def _generate_chunk(client: Groq, category_id: str, cache_key: str, n: int) -> list[dict]:
    cache_path = CACHE_DIR / f"{category_id}_{cache_key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))["examples"]

    system, user = _prompt_for(category_id, cache_key, n)
    try:
        response = _call_with_retry(
            client,
            model=MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.9,
        )
        raw = json.loads(response.choices[0].message.content or "{}")
    except (BadRequestError, json.JSONDecodeError):
        # The model occasionally produces JSON that fails Groq's own
        # schema validation (or truncates) at higher n -- halving the ask
        # keeps the response well under whatever length limit is involved.
        if n <= 5:
            print(f"  {category_id} {cache_key}: giving up at n={n}, skipping")
            return []
        half = n // 2
        return (
            _generate_chunk(client, category_id, f"{cache_key}a", half)
            + _generate_chunk(client, category_id, f"{cache_key}b", n - half)
        )

    examples = raw.get("examples", [])
    cache_path.write_text(json.dumps({"examples": examples}, indent=2, ensure_ascii=False), encoding="utf-8")
    return examples


def _generate_batch(client: Groq, category_id: str, batch_index: int) -> list[dict]:
    return _generate_chunk(client, category_id, f"batch{batch_index}", ROWS_PER_BATCH)


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = _client()
    rows = []
    for category_id in CATEGORIES:
        for batch_index in range(BATCHES_PER_CATEGORY):
            examples = _generate_batch(client, category_id, batch_index)
            for i, example in enumerate(examples):
                rows.append({
                    "id": f"syn-{category_id}-{batch_index}-{i:02d}",
                    "category": category_id,
                    "text": example.get("text", ""),
                    "vendor": example.get("vendor", ""),
                    "source": "synthetic_groq",
                })
            print(f"{category_id} batch {batch_index}: {len(examples)} examples")

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(rows)} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
