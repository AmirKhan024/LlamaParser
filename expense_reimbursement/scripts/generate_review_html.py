"""Generates eval/categorization/review.html: a standalone page (open
directly in a browser, no server needed) for reviewing every dataset row's
silver label before promoting it to gold. One card per document -- image/
PDF preview (or a link for a PDF, which won't inline-preview from a plain
file:// page), key extracted fields, the silver label + rationale, and the
id -- grouped by silver label, with a per-category count at the top and
client-side filters for source and ambiguous.

Usage: python scripts/generate_review_html.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from categories import CATEGORIES

BASE_DIR = Path(__file__).resolve().parent.parent
DATASET_PATH = BASE_DIR / "eval" / "categorization" / "dataset.jsonl"
OUT_PATH = BASE_DIR / "eval" / "categorization" / "review.html"
REVIEW_DIR = OUT_PATH.parent  # paths in the HTML are relative to this


def _load_rows() -> list[dict]:
    with DATASET_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _rel_from_review_dir(path_from_repo_root: str) -> str:
    """Dataset rows store paths relative to the git repo root (SROIE's
    source files live outside expense_reimbursement/); the review page
    lives at expense_reimbursement/eval/categorization/, three levels
    below the repo root."""
    return "../../../" + path_from_repo_root


def _esc(s) -> str:
    if s is None:
        return ""
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _card_html(row: dict) -> str:
    fields = row.get("extracted_fields") or {}
    file_rel = _rel_from_review_dir(row["file"]) if row.get("file") else None
    is_pdf = file_rel and file_rel.lower().endswith(".pdf")
    if file_rel and not is_pdf:
        preview = f'<img loading="lazy" src="{_esc(file_rel)}" alt="preview">'
    elif file_rel:
        preview = f'<a href="{_esc(file_rel)}" target="_blank">Open PDF &rarr;</a>'
    else:
        preview = '<div class="no-preview">No file</div>'

    amount = fields.get("amount") or fields.get("total") or fields.get("grand_total") or fields.get("total_claimed")
    ambiguous_badge = '<span class="badge badge-ambiguous">ambiguous</span>' if row.get("ambiguous") else ""

    return f"""
    <div class="card" data-source="{_esc(row['source'])}" data-ambiguous="{'1' if row.get('ambiguous') else '0'}" data-id="{_esc(row['id'])}">
      <div class="preview">{preview}</div>
      <div class="body">
        <div class="id-row"><code>{_esc(row['id'])}</code><span class="badge badge-source">{_esc(row['source'])}</span>{ambiguous_badge}</div>
        <div class="field"><b>document_type:</b> {_esc(fields.get('document_type'))}</div>
        <div class="field"><b>vendor:</b> {_esc(fields.get('vendor_name'))}</div>
        <div class="field"><b>amount:</b> {_esc(amount)} {_esc(fields.get('currency'))}</div>
        <div class="silver"><b>silver_label:</b> <span class="silver-label">{_esc(row.get('silver_label') or '(none)')}</span></div>
        <div class="rationale">{_esc(row.get('silver_rationale'))}</div>
        {f'<div class="notes">note: {_esc(row["notes"])}</div>' if row.get('notes') else ''}
      </div>
    </div>"""


def main() -> None:
    if not DATASET_PATH.exists():
        raise SystemExit(f"No dataset at {DATASET_PATH} -- build it and run silver labeling first.")
    rows = _load_rows()

    groups: dict[str, list[dict]] = {}
    for row in rows:
        label = row.get("silver_label") or "(no category)"
        groups.setdefault(label, []).append(row)

    category_order = list(CATEGORIES.keys()) + ["(no category)"]
    ordered_labels = [c for c in category_order if c in groups] + [g for g in groups if g not in category_order]

    counts_html = "".join(
        f'<span class="count-pill">{_esc(label)}: {len(groups[label])}</span>' for label in ordered_labels
    )

    sections = []
    for label in ordered_labels:
        cat_label = CATEGORIES[label].label if label in CATEGORIES else label
        cards = "".join(_card_html(row) for row in groups[label])
        sections.append(f"""
        <section class="group" data-group="{_esc(label)}">
          <h2>{_esc(cat_label)} <span class="group-count">({len(groups[label])})</span></h2>
          <div class="cards">{cards}</div>
        </section>""")

    sources = sorted({row["source"] for row in rows})
    source_filters = "".join(
        f'<label><input type="checkbox" class="source-filter" value="{_esc(s)}" checked> {_esc(s)}</label>'
        for s in sources
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Categorization silver-label review</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; padding: 20px; background: #f7f7f9; color: #1a1a1a; }}
  h1 {{ margin: 0 0 8px; }}
  .toolbar {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; }}
  .counts {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
  .count-pill {{ background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 999px; padding: 3px 10px; font-size: 12px; }}
  .filters label {{ margin-right: 14px; font-size: 13px; }}
  .group h2 {{ font-size: 16px; border-bottom: 2px solid #333; padding-bottom: 4px; margin-top: 28px; }}
  .group-count {{ color: #666; font-weight: normal; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; margin-top: 10px; }}
  .card {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; overflow: hidden; font-size: 12px; }}
  .card .preview {{ height: 140px; background: #fafafa; display: flex; align-items: center; justify-content: center; border-bottom: 1px solid #eee; overflow: hidden; }}
  .card .preview img {{ max-width: 100%; max-height: 140px; object-fit: contain; }}
  .card .body {{ padding: 8px 10px; }}
  .id-row {{ display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }}
  .id-row code {{ font-size: 11px; color: #444; }}
  .badge {{ font-size: 10px; padding: 1px 6px; border-radius: 999px; }}
  .badge-source {{ background: #f0f0f0; border: 1px solid #ccc; }}
  .badge-ambiguous {{ background: #fef3c7; border: 1px solid #fde68a; color: #92400e; }}
  .field {{ margin-bottom: 2px; }}
  .silver {{ margin-top: 6px; }}
  .silver-label {{ font-weight: bold; color: #1d4ed8; }}
  .rationale {{ color: #555; margin-top: 3px; font-style: italic; }}
  .notes {{ color: #b45309; margin-top: 4px; }}
  .no-preview {{ color: #999; font-size: 11px; }}
  .hidden {{ display: none !important; }}
</style>
</head>
<body>
  <h1>Categorization silver-label review</h1>
  <div class="toolbar">
    <div class="counts">{counts_html}</div>
    <div class="filters">
      <b>Source:</b> {source_filters}
      &nbsp;&nbsp; <label><input type="checkbox" id="ambiguous-only"> ambiguous only</label>
    </div>
  </div>
  {"".join(sections)}
  <script>
    function applyFilters() {{
      const checked = new Set(Array.from(document.querySelectorAll('.source-filter:checked')).map(el => el.value));
      const ambiguousOnly = document.getElementById('ambiguous-only').checked;
      document.querySelectorAll('.card').forEach(card => {{
        const sourceOk = checked.has(card.dataset.source);
        const ambiguousOk = !ambiguousOnly || card.dataset.ambiguous === '1';
        card.classList.toggle('hidden', !(sourceOk && ambiguousOk));
      }});
      document.querySelectorAll('.group').forEach(group => {{
        const visible = group.querySelectorAll('.card:not(.hidden)').length;
        group.classList.toggle('hidden', visible === 0);
      }});
    }}
    document.querySelectorAll('.source-filter, #ambiguous-only').forEach(el => el.addEventListener('change', applyFilters));
  </script>
</body></html>"""

    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(rows)} rows, {len(ordered_labels)} groups)")


if __name__ == "__main__":
    main()
