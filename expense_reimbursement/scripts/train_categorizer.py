"""Train the `classifier` categorizer: embed eval/categorization/
train_synthetic.jsonl with sentence-transformers (all-MiniLM-L6-v2) and
fit a scikit-learn LogisticRegression on top, saved to
models/categorizer/classifier.joblib.

Leakage guard: if eval/categorization/dataset.jsonl already exists (Part
A5), any synthetic training row whose embedding has cosine similarity
> 0.95 to an eval document's text is dropped before training -- printed,
not silent. Safe to run before the eval set exists too (nothing to check
against yet, so nothing is dropped) -- re-run this script after building
the eval set to enforce the guard for real.

Also holds out 20% of the (post-filter) synthetic data, stratified by
category, to sweep the hybrid categorizer's confidence threshold -- NEVER
the eval set itself. Prints the sweep so HYBRID_CONFIDENCE_THRESHOLD in
categorize.py can be set from it by hand (not auto-written, so the choice
stays a reviewed, deliberate one).

Usage: python scripts/train_categorizer.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

from categories import CATEGORY_IDS

BASE_DIR = Path(__file__).resolve().parent.parent
TRAIN_PATH = BASE_DIR / "eval" / "categorization" / "train_synthetic.jsonl"
EVAL_PATH = BASE_DIR / "eval" / "categorization" / "dataset.jsonl"
MODEL_DIR = BASE_DIR / "models" / "categorizer"
LEAKAGE_THRESHOLD = 0.95


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _row_text(row: dict) -> str:
    return " ".join(x for x in [row.get("vendor", ""), row.get("text", "")] if x)


def _eval_doc_text(row: dict) -> str:
    fields = row.get("extracted_fields") or {}
    vendor = fields.get("vendor_name", "")
    return " ".join(x for x in [vendor, row.get("markdown_excerpt", "")] if x)


def main() -> None:
    from sentence_transformers import SentenceTransformer

    train_rows = _load_jsonl(TRAIN_PATH)
    if not train_rows:
        raise SystemExit(f"No training data at {TRAIN_PATH} -- run generate_categorizer_training_data.py first")
    print(f"Loaded {len(train_rows)} synthetic training rows.")

    embedder = SentenceTransformer("all-MiniLM-L6-v2")

    eval_rows = _load_jsonl(EVAL_PATH)
    if eval_rows:
        eval_texts = [_eval_doc_text(r) for r in eval_rows if _eval_doc_text(r).strip()]
        print(f"Checking against {len(eval_texts)} eval document(s) for leakage...")
        eval_embeddings = embedder.encode(eval_texts, normalize_embeddings=True)
        train_texts = [_row_text(r) for r in train_rows]
        train_embeddings = embedder.encode(train_texts, normalize_embeddings=True)
        similarities = train_embeddings @ eval_embeddings.T  # cosine, since both are normalized
        max_similarity = similarities.max(axis=1) if similarities.size else np.zeros(len(train_rows))
        keep_mask = max_similarity <= LEAKAGE_THRESHOLD
        dropped = [r["id"] for r, keep in zip(train_rows, keep_mask) if not keep]
        if dropped:
            print(f"Dropping {len(dropped)} training row(s) too similar to an eval document: {dropped}")
        train_rows = [r for r, keep in zip(train_rows, keep_mask) if keep]
        train_texts = [t for t, keep in zip(train_texts, keep_mask) if keep]
    else:
        print(f"No eval dataset at {EVAL_PATH} yet -- skipping the leakage check (re-run after Part A5).")
        train_texts = [_row_text(r) for r in train_rows]

    labels = [r["category"] for r in train_rows]
    missing = set(labels) - set(CATEGORY_IDS)
    if missing:
        raise SystemExit(f"Training data has unknown category ids: {missing}")

    embeddings = embedder.encode(train_texts)
    X_train, X_holdout, y_train, y_holdout = train_test_split(
        embeddings, labels, test_size=0.2, stratify=labels, random_state=42
    )

    clf = LogisticRegression(class_weight="balanced", max_iter=2000)
    clf.fit(X_train, y_train)

    print("\n--- Held-out split classification report ---")
    print(classification_report(y_holdout, clf.predict(X_holdout), zero_division=0))

    print("--- Confidence threshold sweep (held-out split) ---")
    print("threshold | coverage | accuracy on confident subset")
    probs = clf.predict_proba(X_holdout)
    preds = clf.classes_[probs.argmax(axis=1)]
    confidences = probs.max(axis=1)
    y_holdout_arr = np.array(y_holdout)
    for threshold in [0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8]:
        confident = confidences >= threshold
        coverage = confident.mean()
        accuracy = (preds[confident] == y_holdout_arr[confident]).mean() if confident.any() else float("nan")
        print(f"  {threshold:.2f}      | {coverage:6.1%}  | {accuracy:6.1%}")

    # Retrain on ALL post-filter data (train + holdout) for the shipped
    # model -- the holdout split above is only for the threshold sweep
    # and the printed report, never for picking what ships.
    clf_final = LogisticRegression(class_weight="balanced", max_iter=2000)
    clf_final.fit(embeddings, labels)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": clf_final, "labels": clf_final.classes_.tolist()}, MODEL_DIR / "classifier.joblib")
    print(f"\nSaved model to {MODEL_DIR / 'classifier.joblib'} (trained on {len(train_rows)} rows).")


if __name__ == "__main__":
    main()
