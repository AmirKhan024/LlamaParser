"""Employee review UI: a small local web app for approving/editing
extracted claims.

Runs entirely on your machine, reads and writes the same
`outputs/*_result.json` files `run.py` already produces -- no new
storage, no database, no auth. `review_view.py`'s `build_review_view()`
(REVIEW_CONFIG) is the single source of truth for which fields an
employee sees per document_type and which of those are editable; this
app doesn't duplicate that decision, it renders it.

Scope: the 3 real documents in outputs/*_result.json (NOT
outputs/cord_eval/ -- those are benchmark receipts, not real employee
claims, and don't belong in a review queue).

An edit is applied to `clean_json`, then re-validated through the exact
same `build_claim`/`validate_claim` machinery the extraction pipeline
itself uses -- an edited total is re-checked against its arithmetic
identity, not just stored blindly. Every edit is appended to
`employee_edits` (old value, new value, timestamp) rather than silently
overwriting history. Approving sets `review_status` + `reviewed_at` and
is a separate action from saving an edit -- you can save edits without
approving, and (today) you can approve without editing, matching "edit
and send" and "approve" as the two distinct actions asked for.

Usage:
    python review_app.py
    -> http://127.0.0.1:8000
"""

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from review_view import build_review_view
from schemas import DocumentType, schema_for
from validate import build_claim, validate_claim

OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"

app = FastAPI()


# ---------------------------------------------------------------------------
# Data access -- read/write the same *_result.json files run.py produces
# ---------------------------------------------------------------------------
def _list_result_files():
    return sorted(p for p in OUTPUTS_DIR.glob("*_result.json"))


def _load(doc_id: str) -> Dict[str, Any]:
    path = OUTPUTS_DIR / f"{doc_id}_result.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("review_status", "pending")
    data.setdefault("reviewed_at", None)
    data.setdefault("employee_edits", [])
    return data


def _save(doc_id: str, data: Dict[str, Any]) -> None:
    path = OUTPUTS_DIR / f"{doc_id}_result.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _rebuild(data: Dict[str, Any]) -> Dict[str, Any]:
    """Re-run build_claim/validate_claim/build_review_view on the current
    clean_json -- the exact same machinery the extraction pipeline uses,
    so an employee-edited field is re-validated, not just stored as-is.
    """
    doc_type = DocumentType(data["document_type"])
    claim = build_claim(doc_type, dict(data["clean_json"]))
    checks = validate_claim(claim)

    data["clean_json"] = claim.model_dump(mode="json")
    data["additional_fields"] = claim.additional_fields
    data["extraction_notes"] = claim.extraction_notes
    data["validation"] = [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks]

    review_input = {**data["clean_json"], "validation": data["validation"], "completeness_warnings": data.get("completeness_warnings", [])}
    data["review_view"] = build_review_view(review_input)
    return data


# ---------------------------------------------------------------------------
# Tiny HTML helpers -- no template engine dependency, just escaped f-strings
# ---------------------------------------------------------------------------
_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 780px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #ddd; }
  a { color: #2563eb; text-decoration: none; } a:hover { text-decoration: underline; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }
  .badge-review { background: #fef3c7; color: #92400e; }
  .badge-ok { background: #dcfce7; color: #166534; }
  .badge-approved { background: #dbeafe; color: #1e40af; }
  .field-row { margin-bottom: 0.9rem; }
  .field-row label { display: block; font-weight: 600; font-size: 0.85rem; color: #444; margin-bottom: 0.2rem; }
  .field-row input, .field-row textarea { width: 100%; padding: 0.45rem; border: 1px solid #ccc; border-radius: 4px; font-family: inherit; font-size: 0.95rem; box-sizing: border-box; }
  .field-row textarea { min-height: 5rem; font-family: ui-monospace, monospace; font-size: 0.85rem; }
  .readonly { padding: 0.45rem 0; color: #333; }
  .checks li.fail { color: #b91c1c; } .checks li.pass { color: #15803d; }
  .actions { margin-top: 1.5rem; display: flex; gap: 0.75rem; }
  button { padding: 0.55rem 1.1rem; border: none; border-radius: 4px; font-size: 0.95rem; cursor: pointer; }
  .btn-save { background: #2563eb; color: white; }
  .btn-approve { background: #16a34a; color: white; }
  .btn-approve:disabled { background: #9ca3af; cursor: not-allowed; }
  .notice { padding: 0.6rem 0.9rem; border-radius: 6px; margin: 1rem 0; font-size: 0.9rem; }
  .notice-warn { background: #fef3c7; color: #92400e; } .notice-ok { background: #dcfce7; color: #166534; }
  .history { font-size: 0.85rem; color: #555; }
</style>
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<html><head><title>{html.escape(title)}</title>{_STYLE}</head><body>{body}</body></html>")


def _fmt_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    rows = []
    for path in _list_result_files():
        doc_id = path.name[: -len("_result.json")]
        data = _load(doc_id)
        rv = data.get("review_view", {})
        status = data["review_status"]
        badge = (
            '<span class="badge badge-approved">Approved</span>' if status == "approved"
            else '<span class="badge badge-review">Needs review</span>' if rv.get("_needs_review")
            else '<span class="badge badge-ok">OK</span>'
        )
        rows.append(
            f'<tr><td><a href="/review/{quote(doc_id, safe="")}">{html.escape(data["file"])}</a></td>'
            f'<td>{html.escape(data["document_type"])}</td><td>{badge}</td></tr>'
        )
    body = f"""
    <h1>Claims for review</h1>
    <table><tr><th>Document</th><th>Type</th><th>Status</th></tr>{''.join(rows) or '<tr><td colspan=3>No results found in outputs/</td></tr>'}</table>
    """
    return _page("Claims for review", body)


@app.get("/review/{doc_id}", response_class=HTMLResponse)
def review(doc_id: str):
    data = _load(doc_id)
    rv = data["review_view"]
    show_fields = [k for k in rv.keys() if not k.startswith("_")]
    editable = set(rv["_editable_fields"])

    field_rows = []
    for field in show_fields:
        value = rv.get(field)
        label = field.replace("_", " ").title()
        if field in editable:
            if isinstance(value, (list, dict)):
                field_rows.append(
                    f'<div class="field-row"><label>{html.escape(label)}</label>'
                    f'<textarea name="{html.escape(field)}">{html.escape(json.dumps(value, indent=2, ensure_ascii=False))}</textarea></div>'
                )
            else:
                field_rows.append(
                    f'<div class="field-row"><label>{html.escape(label)}</label>'
                    f'<input type="text" name="{html.escape(field)}" value="{html.escape(_fmt_value(value))}"></div>'
                )
        else:
            field_rows.append(
                f'<div class="field-row"><label>{html.escape(label)}</label>'
                f'<div class="readonly">{html.escape(_fmt_value(value))}</div></div>'
            )

    checks_html = "".join(
        f'<li class="{"pass" if c["passed"] else "fail"}">[{"PASS" if c["passed"] else "FAIL"}] {html.escape(c["name"])} -- {html.escape(c["detail"])}</li>'
        for c in data["validation"]
    ) or "<li>No checks defined for this document type.</li>"

    completeness_html = "".join(f"<li>{html.escape(w)}</li>" for w in data.get("completeness_warnings", []))
    corrections_html = "".join(f"<li>{html.escape(n)}</li>" for n in rv.get("_ai_corrections", []))

    notice = ""
    if data["review_status"] == "approved":
        notice = f'<div class="notice notice-ok">Approved at {html.escape(data["reviewed_at"] or "")}</div>'
    elif rv.get("_needs_review"):
        notice = '<div class="notice notice-warn">This claim is flagged for review -- check the notes below before approving.</div>'

    history_html = ""
    if data["employee_edits"]:
        items = "".join(
            f'<li>{html.escape(e["timestamp"])}: <b>{html.escape(e["field"])}</b> '
            f'changed from <code>{html.escape(_fmt_value(e["old_value"]))}</code> '
            f'to <code>{html.escape(_fmt_value(e["new_value"]))}</code></li>'
            for e in data["employee_edits"]
        )
        history_html = f'<h2>Edit history</h2><ul class="history">{items}</ul>'

    approve_disabled = "disabled" if data["review_status"] == "approved" else ""

    body = f"""
    <p><a href="/">&larr; back to list</a></p>
    <h1>{html.escape(data["file"])}</h1>
    <p>document_type: <b>{html.escape(data["document_type"])}</b></p>
    {notice}

    <h2>Details</h2>
    <form method="post" action="/review/{quote(doc_id, safe='')}/save">
      {''.join(field_rows)}
      <div class="actions"><button type="submit" class="btn-save">Save changes</button></div>
    </form>

    <h2>Validation</h2>
    <ul class="checks">{checks_html}</ul>

    {f'<h2>Completeness warnings</h2><ul>{completeness_html}</ul>' if completeness_html else ''}
    {f'<h2>AI corrections made during extraction</h2><ul>{corrections_html}</ul>' if corrections_html else ''}

    <form method="post" action="/review/{quote(doc_id, safe='')}/approve">
      <div class="actions">
        <button type="submit" class="btn-approve" {approve_disabled}>
          {"Approved" if data["review_status"] == "approved" else "Approve"}
        </button>
      </div>
    </form>

    {history_html}
    """
    return _page(data["file"], body)


@app.post("/review/{doc_id}/save")
async def save(doc_id: str, request: Request):
    data = _load(doc_id)
    form = await request.form()
    rv = data["review_view"]
    editable = set(rv["_editable_fields"])
    now = datetime.now(timezone.utc).isoformat()

    for field, raw_new_value in form.multi_items():
        if field not in editable:
            continue  # never trust the client to edit a field the config didn't mark editable
        old_value = data["clean_json"].get(field)

        if isinstance(old_value, (list, dict)) or field in ("travel_entries", "line_items"):
            try:
                new_value = json.loads(raw_new_value)
            except json.JSONDecodeError:
                continue  # invalid JSON typed into a list field -- skip rather than corrupt the record
        else:
            new_value = raw_new_value

        if new_value == old_value:
            continue

        data["clean_json"][field] = new_value
        data["employee_edits"].append({
            "timestamp": now, "field": field,
            "old_value": old_value, "new_value": new_value,
        })

    data = _rebuild(data)
    _save(doc_id, data)
    return RedirectResponse(f"/review/{quote(doc_id, safe='')}", status_code=303)


@app.post("/review/{doc_id}/approve")
def approve(doc_id: str):
    data = _load(doc_id)
    data["review_status"] = "approved"
    data["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    _save(doc_id, data)
    return RedirectResponse(f"/review/{quote(doc_id, safe='')}", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
