"""All database access goes through these functions. Each function that
writes commits its own transaction; every multi-table write (e.g. a
status change plus its audit event) happens inside that one commit."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from models import (
    AuditEvent,
    CheckResultRow,
    Claim,
    Correction,
    Document,
    Employee,
    Extraction,
)

SEED_EMPLOYEE_NAME = "Nasir Ahmed Khan"
SEED_EMPLOYEE_EMAIL = "nasir.khan@example.com"


# ---------------------------------------------------------------- employees

def get_or_create_seed_employee(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.email == SEED_EMPLOYEE_EMAIL))
    if employee is not None:
        return employee
    employee = Employee(name=SEED_EMPLOYEE_NAME, email=SEED_EMPLOYEE_EMAIL, role="employee")
    session.add(employee)
    session.commit()
    session.refresh(employee)
    return employee


def get_employee(session: Session, employee_id: uuid.UUID) -> Optional[Employee]:
    return session.get(Employee, employee_id)


# ------------------------------------------------------------------ claims

def create_claim(session: Session, employee_id: uuid.UUID, title: str) -> Claim:
    claim = Claim(employee_id=employee_id, title=title, status="draft")
    session.add(claim)
    session.flush()
    session.add(AuditEvent(claim_id=claim.id, actor_id=employee_id, action="created"))
    session.commit()
    session.refresh(claim)
    return claim


def list_claims(session: Session, employee_id: uuid.UUID) -> list[Claim]:
    return list(
        session.scalars(
            select(Claim)
            # extractions eager-loaded too (not just documents): the list
            # view computes each claim's per-currency totals live from
            # them (server._claim_totals_by_currency), not from the
            # possibly-stale total_amount/currency columns.
            .options(selectinload(Claim.documents).selectinload(Document.extractions))
            .where(Claim.employee_id == employee_id)
            .order_by(Claim.updated_at.desc())
        )
    )


def get_claim(session: Session, claim_id: uuid.UUID) -> Optional[Claim]:
    return session.scalar(
        select(Claim)
        .options(selectinload(Claim.documents).selectinload(Document.extractions).selectinload(Extraction.check_results))
        .where(Claim.id == claim_id)
    )


def update_claim(
    session: Session,
    claim_id: uuid.UUID,
    *,
    title: Optional[str] = None,
    note_to_approver: Optional[str] = None,
) -> Claim:
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    if title is not None:
        claim.title = title
    if note_to_approver is not None:
        claim.note_to_approver = note_to_approver
    session.commit()
    session.refresh(claim)
    return claim


def compute_claim_totals(session: Session, claim_id: uuid.UUID) -> dict[str, Decimal]:
    """Per-currency totals across confirmed documents only -- never
    summed together, since a Decimal can't represent "$10 + Rs 10".
    A document whose currency couldn't be determined (see
    validate.check_completeness's currency warning) is bucketed under
    "unknown" rather than dropped or guessed into an existing bucket."""
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    totals: dict[str, Decimal] = {}
    for document in claim.documents:
        if document.status != "confirmed":
            continue
        extraction = latest_extraction(session, document.id)
        if extraction is None or extraction.amount is None:
            continue
        currency = extraction.currency or "unknown"
        totals[currency] = totals.get(currency, Decimal("0")) + extraction.amount
    return totals


def recompute_claim_total(session: Session, claim_id: uuid.UUID) -> dict[str, Decimal]:
    """Updates claims.total_amount/currency -- a best-effort single
    figure, only meaningful when every confirmed document shares one
    currency. Cleared to None/None when there are none or several;
    server.py's API responses compute the full per-currency breakdown
    live instead of trusting this cached pair for anything but the
    common single-currency case."""
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    totals = compute_claim_totals(session, claim_id)
    if len(totals) == 1:
        currency, amount = next(iter(totals.items()))
        claim.total_amount = amount
        claim.currency = currency
    elif len(totals) == 0:
        claim.total_amount = Decimal("0")
        claim.currency = "INR"
    else:
        claim.total_amount = None
        claim.currency = None
    session.commit()
    return totals


def submit_claim(session: Session, claim_id: uuid.UUID, actor_id: uuid.UUID) -> Claim:
    claim = session.get(Claim, claim_id)
    if claim is None:
        raise LookupError(f"claim {claim_id} not found")
    from sqlalchemy import func as sa_func

    claim.status = "submitted"
    claim.submitted_at = sa_func.now()
    totals = compute_claim_totals(session, claim_id)
    if len(totals) == 1:
        currency, amount = next(iter(totals.items()))
        claim.total_amount = amount
        claim.currency = currency
    elif len(totals) == 0:
        claim.total_amount = Decimal("0")
        claim.currency = "INR"
    else:
        claim.total_amount = None
        claim.currency = None
    session.add(
        AuditEvent(
            claim_id=claim.id,
            actor_id=actor_id,
            action="submitted",
            payload={"totals_by_currency": {k: str(v) for k, v in totals.items()}},
        )
    )
    session.commit()
    session.refresh(claim)
    return claim


# --------------------------------------------------------------- documents

def create_document(
    session: Session,
    *,
    claim_id: uuid.UUID,
    actor_id: uuid.UUID,
    original_name: str,
    file_key: str,
    file_sha256: str,
    mime_type: str,
    document_id: Optional[uuid.UUID] = None,
) -> Document:
    """`document_id` can be supplied by the caller so the storage path
    (storage/<claim_id>/<document_id><ext>) can be computed and the file
    written to disk before this row exists."""
    document = Document(
        id=document_id or uuid.uuid4(),
        claim_id=claim_id,
        original_name=original_name,
        file_key=file_key,
        file_sha256=file_sha256,
        mime_type=mime_type,
        status="processing",
    )
    session.add(document)
    session.flush()
    session.add(
        AuditEvent(
            claim_id=claim_id,
            document_id=document.id,
            actor_id=actor_id,
            action="uploaded",
            payload={"original_name": original_name},
        )
    )
    session.commit()
    session.refresh(document)
    return document


def find_document_by_sha256(session: Session, claim_id: uuid.UUID, file_sha256: str) -> Optional[Document]:
    return session.scalar(
        select(Document).where(Document.claim_id == claim_id, Document.file_sha256 == file_sha256)
    )


def get_document(session: Session, document_id: uuid.UUID) -> Optional[Document]:
    return session.scalar(
        select(Document)
        .options(selectinload(Document.extractions).selectinload(Extraction.check_results))
        .where(Document.id == document_id)
    )


def update_document_status(
    session: Session,
    document_id: uuid.UUID,
    *,
    status: str,
    error_message: Optional[str] = None,
    raw_markdown: Optional[str] = None,
) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    document.status = status
    document.error_message = error_message
    if raw_markdown is not None:
        document.raw_markdown = raw_markdown
    session.commit()
    session.refresh(document)
    return document


def recover_stuck_processing_documents(session: Session, cutoff: datetime) -> list[Document]:
    """A document still "processing" from before a server restart has
    no pipeline task running for it anymore -- it would otherwise show
    "Reading..." forever. Called on startup for anything last updated
    before `cutoff` (the caller decides the age threshold)."""
    stuck = list(
        session.scalars(
            select(Document).where(Document.status == "processing", Document.updated_at < cutoff)
        )
    )
    for document in stuck:
        document.status = "failed"
        document.error_message = "Processing was interrupted. Retry."
    if stuck:
        session.commit()
    return stuck


def delete_document(session: Session, document_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    claim_id = document.claim_id
    session.add(
        AuditEvent(
            claim_id=claim_id,
            document_id=None,
            actor_id=actor_id,
            action="removed",
            payload={"document_id": str(document_id), "original_name": document.original_name},
        )
    )
    session.delete(document)
    session.commit()


def confirm_document(session: Session, document_id: uuid.UUID, actor_id: uuid.UUID) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    document.status = "confirmed"
    session.add(
        AuditEvent(claim_id=document.claim_id, document_id=document.id, actor_id=actor_id, action="confirmed")
    )
    session.commit()
    session.refresh(document)
    return document


def reopen_document(session: Session, document_id: uuid.UUID, actor_id: uuid.UUID) -> Document:
    """"Edit again": unconditionally back to needs_review (not
    recomputed from checks) so the employee's edit screen reappears."""
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    document.status = "needs_review"
    session.add(
        AuditEvent(claim_id=document.claim_id, document_id=document.id, actor_id=actor_id, action="reopened")
    )
    session.commit()
    session.refresh(document)
    return document


# -------------------------------------------------------------- extractions

def latest_extraction(session: Session, document_id: uuid.UUID) -> Optional[Extraction]:
    return session.scalar(
        select(Extraction)
        .options(selectinload(Extraction.check_results))
        .where(Extraction.document_id == document_id)
        .order_by(Extraction.version.desc())
        .limit(1)
    )


def next_extraction_version(session: Session, document_id: uuid.UUID) -> int:
    current = latest_extraction(session, document_id)
    return 1 if current is None else current.version + 1


def add_extraction(
    session: Session,
    *,
    document_id: uuid.UUID,
    actor_id: uuid.UUID,
    source: str,
    document_type: str,
    fields: dict[str, Any],
    confidence: Optional[float] = None,
    model: Optional[str] = None,
    total_tokens: Optional[int] = None,
    duration_ms: Optional[int] = None,
    vendor_name: Optional[str] = None,
    bill_date: Optional[str] = None,
    amount: Optional[Decimal] = None,
    currency: Optional[str] = None,
    check_results: Optional[list[dict[str, Any]]] = None,
    corrections: Optional[list[dict[str, Any]]] = None,
    audit_action: str = "edited",
) -> Extraction:
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")

    version = next_extraction_version(session, document_id)
    extraction = Extraction(
        document_id=document_id,
        version=version,
        source=source,
        document_type=document_type,
        fields=fields,
        confidence=confidence,
        model=model,
        total_tokens=total_tokens,
        duration_ms=duration_ms,
        vendor_name=vendor_name,
        bill_date=bill_date,
        amount=amount,
        currency=currency,
    )
    session.add(extraction)
    session.flush()

    for result in check_results or []:
        session.add(
            CheckResultRow(
                extraction_id=extraction.id,
                check_name=result["name"],
                passed=result["passed"],
                detail=result.get("detail"),
            )
        )

    for correction in corrections or []:
        session.add(
            Correction(
                document_id=document_id,
                extraction_id=extraction.id,
                field_path=correction["field_path"],
                ai_value=correction.get("ai_value"),
                employee_value=correction.get("employee_value"),
                change_type=correction.get("change_type"),
            )
        )

    session.add(
        AuditEvent(
            claim_id=document.claim_id,
            document_id=document_id,
            actor_id=actor_id,
            action=audit_action,
            payload={"extraction_id": str(extraction.id), "version": version, "source": source},
        )
    )
    session.commit()
    session.refresh(extraction)
    return extraction


def list_corrections(session: Session, document_id: uuid.UUID) -> list[Correction]:
    return list(
        session.scalars(
            select(Correction).where(Correction.document_id == document_id).order_by(Correction.created_at)
        )
    )


# --------------------------------------------------------------------- misc

def add_audit_event(
    session: Session,
    *,
    claim_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    document_id: Optional[uuid.UUID] = None,
    payload: Optional[dict[str, Any]] = None,
) -> AuditEvent:
    event = AuditEvent(claim_id=claim_id, document_id=document_id, actor_id=actor_id, action=action, payload=payload)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event
