"""FastAPI backend for the expense reimbursement employee UI.

Pipeline per uploaded document: parse_pdf -> extract_claim -> evaluate()
(build_claim -> validate_claim -> check_completeness -> build_review_view,
the same sequence run.py uses) -> stored as an `extractions` row. Runs in
a FastAPI background task so the upload endpoint returns immediately;
the UI polls GET /api/documents/{id} while status stays "processing".

PIPELINE_MODE=fake (see .env.example) skips LlamaParse/Groq entirely and
replays a cached outputs/*_result.json matched by the upload's sha256,
so the UI and the test suite work with no API keys or credits spent.
"""

import hashlib
import os
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

import json

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

import repository
from db import get_db, get_sessionmaker
from extract import MODEL as GROQ_MODEL
from extract import extract_claim
from models import Claim, Document, Employee, Extraction
from parse import parse_pdf
from review_view import build_review_view
from schemas import DocumentType
from validate import build_claim, check_completeness, validate_claim

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
OUTPUTS_DIR = BASE_DIR / "outputs"
UPLOADS_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

_AMOUNT_FIELD_BY_TYPE = {
    "telecom_bill": "total",
    "restaurant_bill": "grand_total",
    "local_conveyance_form": "total_claimed",
    "generic_receipt": "amount",
}

app = FastAPI(title="Expense Reimbursement")


# ------------------------------------------------------------------ auth

def get_current_employee(session: Session = Depends(get_db)) -> Employee:
    """No login yet (Stage 5 adds real auth) -- every request runs as the
    one seeded employee, behind this single dependency so swapping in
    real auth later only means changing this function."""
    return repository.get_or_create_seed_employee(session)


# --------------------------------------------------------------- helpers

def _money(value: Optional[Decimal]) -> Optional[str]:
    return str(value) if value is not None else None


def _get_owned_claim(session: Session, claim_id: str, employee: Employee) -> Claim:
    try:
        claim_uuid = uuid.UUID(claim_id)
    except ValueError:
        raise HTTPException(404, "Claim not found")
    claim = repository.get_claim(session, claim_uuid)
    if claim is None or claim.employee_id != employee.id:
        raise HTTPException(404, "Claim not found")
    return claim


def _get_owned_document(session: Session, document_id: str, employee: Employee) -> tuple[Document, Claim]:
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(404, "Document not found")
    document = repository.get_document(session, doc_uuid)
    if document is None:
        raise HTTPException(404, "Document not found")
    claim = repository.get_claim(session, document.claim_id)
    if claim is None or claim.employee_id != employee.id:
        raise HTTPException(404, "Document not found")
    return document, claim


def _require_draft(claim: Claim) -> None:
    if claim.status != "draft":
        raise HTTPException(409, "This claim has already been submitted and can no longer be changed.")


def _decimal_default(obj: Any):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj)}")


PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _parse_field_path(path: str) -> list:
    tokens = []
    for name, idx in PATH_TOKEN.findall(path):
        tokens.append(name if name else int(idx))
    if not tokens:
        raise HTTPException(400, f"Invalid field path: {path!r}")
    return tokens


def _get_by_path(data: Any, tokens: list) -> Any:
    cur = data
    for t in tokens:
        try:
            cur = cur[t]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def _set_by_path(data: dict, tokens: list, value: Any) -> None:
    cur = data
    for t in tokens[:-1]:
        if isinstance(t, int):
            cur = cur[t]
        else:
            nxt = cur.get(t)
            if nxt is None:
                nxt = {}
                cur[t] = nxt
            cur = nxt
    last = tokens[-1]
    cur[last] = value


def _values_equal(a: Any, b: Any) -> bool:
    if a == b:
        return True
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _apply_edits(fields: dict, edits: dict[str, Any], editable_fields: set[str]) -> dict:
    new_fields = json.loads(json.dumps(fields, default=_decimal_default))
    for field_path, value in edits.items():
        tokens = _parse_field_path(field_path)
        root = tokens[0]
        if root not in editable_fields:
            raise HTTPException(400, f"'{root}' cannot be edited on this document.")
        _set_by_path(new_fields, tokens, value)
    return new_fields


def _corrections_for_edits(ai_fields: dict, edits: dict[str, Any]) -> list[dict[str, Any]]:
    corrections = []
    for field_path, employee_value in edits.items():
        ai_value = _get_by_path(ai_fields, _parse_field_path(field_path))
        if not _values_equal(ai_value, employee_value):
            corrections.append(
                {"field_path": field_path, "ai_value": ai_value, "employee_value": employee_value}
            )
    return corrections


def _extraction_amount(document_type: str, clean_json: dict) -> Optional[Decimal]:
    field = _AMOUNT_FIELD_BY_TYPE.get(document_type)
    if field is None:
        return None
    value = clean_json.get(field)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def evaluate(document_type_value: str, fields: dict, markdown_text: str) -> dict:
    """build_claim -> validate_claim -> check_completeness ->
    build_review_view, the same sequence run.py's process_one uses.
    Reused for the extraction pipeline, for a dry-run /validate, and for
    re-evaluating after an employee save or revert."""
    doc_type = DocumentType(document_type_value)
    claim = build_claim(doc_type, fields)
    clean_json = claim.model_dump(mode="json")
    checks = validate_claim(claim)
    checks_as_dicts = [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks]
    completeness_warnings = check_completeness(markdown_text or "", clean_json, claim.document_type.value)
    review_input = {**clean_json, "validation": checks_as_dicts, "completeness_warnings": completeness_warnings}
    view = build_review_view(review_input)
    return {"claim": claim, "clean_json": clean_json, "checks": checks_as_dicts, "view": view}


def _document_summary(document: Document) -> dict:
    return {
        "id": str(document.id),
        "claim_id": str(document.claim_id),
        "original_name": document.original_name,
        "mime_type": document.mime_type,
        "status": document.status,
        "error_message": document.error_message,
        "uploaded_at": document.uploaded_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
    }


def _document_detail(session: Session, document: Document) -> dict:
    summary = _document_summary(document)
    extraction = repository.latest_extraction(session, document.id)
    if extraction is None:
        summary["review"] = None
        summary["corrections"] = []
        return summary
    checks_as_dicts = [
        {"name": c.check_name, "passed": c.passed, "detail": c.detail} for c in extraction.check_results
    ]
    completeness_warnings = check_completeness(document.raw_markdown or "", extraction.fields, extraction.document_type)
    review_input = {**extraction.fields, "validation": checks_as_dicts, "completeness_warnings": completeness_warnings}
    summary["review"] = build_review_view(review_input)
    summary["extraction_version"] = extraction.version
    # Only the latest extraction's corrections -- after a revert, the new
    # version has none (fields equal the AI version again), even though
    # older extraction versions' correction rows still exist in history.
    corrections = [c for c in repository.list_corrections(session, document.id) if c.extraction_id == extraction.id]
    summary["corrections"] = [
        {
            "field_path": c.field_path,
            "ai_value": c.ai_value,
            "employee_value": c.employee_value,
        }
        for c in corrections
    ]
    return summary


def _claim_summary(claim: Claim) -> dict:
    return {
        "id": str(claim.id),
        "title": claim.title,
        "status": claim.status,
        "note_to_approver": claim.note_to_approver,
        "document_count": len(claim.documents),
        "total_amount": _money(claim.total_amount),
        "currency": claim.currency,
        "created_at": claim.created_at.isoformat(),
        "updated_at": claim.updated_at.isoformat(),
        "submitted_at": claim.submitted_at.isoformat() if claim.submitted_at else None,
    }


def _claim_detail(session: Session, claim: Claim) -> dict:
    summary = _claim_summary(claim)
    summary["documents"] = [_document_detail(session, d) for d in claim.documents]
    return summary


# ---------------------------------------------------------- fake pipeline

def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_of_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


_FAKE_RESULT_INDEX: Optional[dict[str, Path]] = None
_FALLBACK_FAKE_RESULT = OUTPUTS_DIR / "may26_mobile_result.json"


def _fake_result_index() -> dict[str, Path]:
    global _FAKE_RESULT_INDEX
    if _FAKE_RESULT_INDEX is None:
        index: dict[str, Path] = {}
        if UPLOADS_DIR.is_dir():
            for source_path in UPLOADS_DIR.iterdir():
                if not source_path.is_file():
                    continue
                result_path = OUTPUTS_DIR / f"{source_path.stem}_result.json"
                if result_path.exists():
                    index[_sha256_of_file(source_path)] = result_path
        _FAKE_RESULT_INDEX = index
    return _FAKE_RESULT_INDEX


def _fake_pipeline_result(file_path: Path) -> tuple[str, dict, str, Optional[int], int]:
    """Returns (document_type_value, fields, markdown_text, total_tokens, duration_ms)."""
    sha256 = _sha256_of_file(file_path)
    result_path = _fake_result_index().get(sha256, _FALLBACK_FAKE_RESULT)
    data = json.loads(result_path.read_text(encoding="utf-8"))
    stem = result_path.stem[: -len("_result")]
    markdown_path = OUTPUTS_DIR / f"{stem}_raw.md"
    markdown_text = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
    tokens = data.get("tokens") or {}
    duration_ms = int(float(data.get("duration_seconds") or 0) * 1000)
    return data["document_type"], data["clean_json"], markdown_text, tokens.get("total"), duration_ms


# -------------------------------------------------------------- pipeline

def _run_pipeline(document_id: uuid.UUID, file_path: Path, actor_id: uuid.UUID) -> None:
    session = get_sessionmaker()()
    try:
        pipeline_mode = os.environ.get("PIPELINE_MODE", "real")
        try:
            if pipeline_mode == "fake":
                document_type_value, fields, markdown_text, total_tokens, duration_ms = _fake_pipeline_result(
                    file_path
                )
                model_name = f"fake:{document_type_value}"
            else:
                markdown, raw_json = parse_pdf(file_path)
                result = extract_claim(markdown, raw_json)
                document_type_value = result.document_type.value
                fields = result.raw_fields
                markdown_text = markdown
                total_tokens = result.total_tokens
                duration_ms = int(result.duration_seconds * 1000)
                model_name = GROQ_MODEL

            evaluated = evaluate(document_type_value, fields, markdown_text)
            claim = evaluated["claim"]
            clean_json = evaluated["clean_json"]
            view = evaluated["view"]

            repository.update_document_status(
                session, document_id, status="processing", raw_markdown=markdown_text
            )
            repository.add_extraction(
                session,
                document_id=document_id,
                actor_id=actor_id,
                source="ai",
                document_type=document_type_value,
                fields=clean_json,
                confidence=claim.confidence,
                model=model_name,
                total_tokens=total_tokens,
                duration_ms=duration_ms,
                vendor_name=clean_json.get("vendor_name"),
                bill_date=clean_json.get("date") or clean_json.get("sent_date"),
                amount=_extraction_amount(document_type_value, clean_json),
                check_results=evaluated["checks"],
                audit_action="extracted",
            )
            final_status = "needs_review" if view["needs_review"] else "ready"
            repository.update_document_status(session, document_id, status=final_status)
        except Exception as e:  # noqa: BLE001 -- any pipeline failure must land the document in "failed", not crash the worker
            repository.update_document_status(session, document_id, status="failed", error_message=str(e))
    finally:
        session.close()


# ---------------------------------------------------------------- claims

class CreateClaimBody(BaseModel):
    title: Optional[str] = None


class UpdateClaimBody(BaseModel):
    title: Optional[str] = None
    note_to_approver: Optional[str] = None


class EditsBody(BaseModel):
    edits: dict[str, Any] = {}


@app.post("/api/claims")
def create_claim(
    body: CreateClaimBody,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    title = body.title or f"Expenses – {datetime.now():%B %Y}"
    claim = repository.create_claim(session, employee.id, title)
    return _claim_summary(claim)


@app.get("/api/claims")
def list_claims(
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    return [_claim_summary(c) for c in repository.list_claims(session, employee.id)]


@app.get("/api/claims/{claim_id}")
def get_claim(
    claim_id: str,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    claim = _get_owned_claim(session, claim_id, employee)
    return _claim_detail(session, claim)


@app.patch("/api/claims/{claim_id}")
def update_claim(
    claim_id: str,
    body: UpdateClaimBody,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    claim = _get_owned_claim(session, claim_id, employee)
    _require_draft(claim)
    updated = repository.update_claim(session, claim.id, title=body.title, note_to_approver=body.note_to_approver)
    return _claim_summary(updated)


@app.post("/api/claims/{claim_id}/submit")
def submit_claim(
    claim_id: str,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    claim = _get_owned_claim(session, claim_id, employee)
    _require_draft(claim)
    if not claim.documents:
        raise HTTPException(400, "Add at least one document before submitting.")
    not_confirmed = [d.original_name for d in claim.documents if d.status not in ("confirmed",) and _needs_confirm(session, d)]
    if not_confirmed:
        raise HTTPException(400, f"Confirm every document before submitting: {', '.join(not_confirmed)}")
    submitted = repository.submit_claim(session, claim.id, employee.id)
    return _claim_detail(session, submitted)


def _needs_confirm(session: Session, document: Document) -> bool:
    """Derived from review_view's own needs_confirm flag (e.g. false for
    approval_correspondence -- read-only, no Confirm button in the UI) so
    this doesn't duplicate a document_type check that could drift from
    review_view.py. Falls back to True (safer: block submit) if a
    document has no extraction yet."""
    extraction = repository.latest_extraction(session, document.id)
    if extraction is None:
        return True
    checks_as_dicts = [{"name": c.check_name, "passed": c.passed} for c in extraction.check_results]
    review_input = {**extraction.fields, "validation": checks_as_dicts, "completeness_warnings": []}
    return build_review_view(review_input)["needs_confirm"]


# ------------------------------------------------------------- documents

@app.post("/api/claims/{claim_id}/documents")
async def upload_document(
    claim_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    claim = _get_owned_claim(session, claim_id, employee)
    _require_draft(claim)

    content_type = file.content_type or ""
    ext = ALLOWED_CONTENT_TYPES.get(content_type)
    if ext is None:
        raise HTTPException(400, "Unsupported file type; upload a PDF, PNG, JPG, or WEBP.")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File is too large (max 15 MB).")
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")

    sha256 = _sha256_of_bytes(content)
    duplicate = repository.find_document_by_sha256(session, claim.id, sha256)
    if duplicate is not None:
        raise HTTPException(409, f"'{duplicate.original_name}' has already been uploaded to this claim.")

    document_id = uuid.uuid4()
    file_key = f"{claim.id}/{document_id}{ext}"
    full_path = STORAGE_DIR / file_key
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(content)

    document = repository.create_document(
        session,
        document_id=document_id,
        claim_id=claim.id,
        actor_id=employee.id,
        original_name=file.filename or f"document{ext}",
        file_key=file_key,
        file_sha256=sha256,
        mime_type=content_type,
    )
    background_tasks.add_task(_run_pipeline, document.id, full_path, employee.id)
    return _document_summary(document)


@app.get("/api/documents/{document_id}")
def get_document(
    document_id: str,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, _claim = _get_owned_document(session, document_id, employee)
    return _document_detail(session, document)


@app.get("/api/documents/{document_id}/file")
def get_document_file(
    document_id: str,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, _claim = _get_owned_document(session, document_id, employee)
    full_path = STORAGE_DIR / document.file_key
    if not full_path.exists():
        raise HTTPException(404, "File not found on disk.")
    # content_disposition_type="inline" -- the default ("attachment")
    # makes the browser download the file instead of rendering it in the
    # review screen's <iframe>/<img> preview.
    return FileResponse(
        full_path,
        media_type=document.mime_type,
        filename=document.original_name,
        content_disposition_type="inline",
    )


@app.post("/api/documents/{document_id}/validate")
def validate_document(
    document_id: str,
    body: EditsBody,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, claim = _get_owned_document(session, document_id, employee)
    _require_draft(claim)
    extraction = repository.latest_extraction(session, document.id)
    if extraction is None:
        raise HTTPException(409, "This document is still being processed.")

    current_review = build_review_view(
        {
            **extraction.fields,
            "validation": [{"name": c.check_name, "passed": c.passed} for c in extraction.check_results],
            "completeness_warnings": [],
        }
    )
    editable_fields = set(current_review["editable_fields"])
    new_fields = _apply_edits(extraction.fields, body.edits, editable_fields)

    evaluated = evaluate(extraction.document_type, new_fields, document.raw_markdown or "")

    ai_extraction = _find_ai_extraction(document)
    corrections_preview = _corrections_for_edits(ai_extraction.fields if ai_extraction else extraction.fields, body.edits)

    return {"review": evaluated["view"], "corrections_preview": corrections_preview}


def _find_ai_extraction(document: Document) -> Optional[Extraction]:
    for extraction in document.extractions:
        if extraction.source == "ai":
            return extraction
    return None


def _save_edits(session: Session, document: Document, edits: dict[str, Any], actor_id: uuid.UUID) -> Extraction:
    extraction = repository.latest_extraction(session, document.id)
    if extraction is None:
        raise HTTPException(409, "This document is still being processed.")

    current_review = build_review_view(
        {
            **extraction.fields,
            "validation": [{"name": c.check_name, "passed": c.passed} for c in extraction.check_results],
            "completeness_warnings": [],
        }
    )
    editable_fields = set(current_review["editable_fields"])
    new_fields = _apply_edits(extraction.fields, edits, editable_fields)

    evaluated = evaluate(extraction.document_type, new_fields, document.raw_markdown or "")
    claim = evaluated["claim"]
    clean_json = evaluated["clean_json"]
    view = evaluated["view"]

    ai_extraction = _find_ai_extraction(document)
    corrections = _corrections_for_edits(ai_extraction.fields if ai_extraction else extraction.fields, edits)

    new_extraction = repository.add_extraction(
        session,
        document_id=document.id,
        actor_id=actor_id,
        source="employee",
        document_type=extraction.document_type,
        fields=clean_json,
        confidence=claim.confidence,
        vendor_name=clean_json.get("vendor_name"),
        bill_date=clean_json.get("date") or clean_json.get("sent_date"),
        amount=_extraction_amount(extraction.document_type, clean_json),
        check_results=evaluated["checks"],
        corrections=corrections,
        audit_action="edited",
    )
    final_status = "needs_review" if view["needs_review"] else "ready"
    repository.update_document_status(session, document.id, status=final_status)
    return new_extraction


@app.put("/api/documents/{document_id}/fields")
def save_document_fields(
    document_id: str,
    body: EditsBody,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, claim = _get_owned_document(session, document_id, employee)
    _require_draft(claim)
    if not body.edits:
        raise HTTPException(400, "No edits were sent.")
    _save_edits(session, document, body.edits, employee.id)
    repository.recompute_claim_total(session, claim.id)
    refreshed = repository.get_document(session, document.id)
    return _document_detail(session, refreshed)


@app.post("/api/documents/{document_id}/confirm")
def confirm_document(
    document_id: str,
    body: EditsBody,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, claim = _get_owned_document(session, document_id, employee)
    _require_draft(claim)
    if body.edits:
        _save_edits(session, document, body.edits, employee.id)
    repository.confirm_document(session, document.id, employee.id)
    repository.recompute_claim_total(session, claim.id)
    refreshed = repository.get_document(session, document.id)
    return _document_detail(session, refreshed)


@app.post("/api/documents/{document_id}/revert")
def revert_document(
    document_id: str,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, claim = _get_owned_document(session, document_id, employee)
    _require_draft(claim)
    ai_extraction = _find_ai_extraction(document)
    if ai_extraction is None:
        raise HTTPException(409, "No AI extraction to revert to.")

    evaluated = evaluate(ai_extraction.document_type, ai_extraction.fields, document.raw_markdown or "")
    claim_obj = evaluated["claim"]
    view = evaluated["view"]

    repository.add_extraction(
        session,
        document_id=document.id,
        actor_id=employee.id,
        source="employee",
        document_type=ai_extraction.document_type,
        fields=evaluated["clean_json"],
        confidence=claim_obj.confidence,
        vendor_name=evaluated["clean_json"].get("vendor_name"),
        bill_date=evaluated["clean_json"].get("date") or evaluated["clean_json"].get("sent_date"),
        amount=_extraction_amount(ai_extraction.document_type, evaluated["clean_json"]),
        check_results=evaluated["checks"],
        corrections=[],
        audit_action="edited",
    )
    final_status = "needs_review" if view["needs_review"] else "ready"
    repository.update_document_status(session, document.id, status=final_status)
    repository.recompute_claim_total(session, claim.id)
    refreshed = repository.get_document(session, document.id)
    return _document_detail(session, refreshed)


@app.post("/api/documents/{document_id}/retry")
def retry_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, claim = _get_owned_document(session, document_id, employee)
    _require_draft(claim)
    if document.status != "failed":
        raise HTTPException(409, "Only a failed document can be retried.")
    full_path = STORAGE_DIR / document.file_key
    if not full_path.exists():
        raise HTTPException(404, "The original file is no longer on disk.")
    repository.update_document_status(session, document.id, status="processing", error_message=None)
    background_tasks.add_task(_run_pipeline, document.id, full_path, employee.id)
    return _document_summary(document)


@app.delete("/api/documents/{document_id}")
def delete_document(
    document_id: str,
    session: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    document, claim = _get_owned_document(session, document_id, employee)
    _require_draft(claim)
    full_path = STORAGE_DIR / document.file_key
    repository.delete_document(session, document.id, employee.id)
    if full_path.exists():
        full_path.unlink()
    repository.recompute_claim_total(session, claim.id)
    return {"deleted": True}


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
