"""All database access goes through these functions. Each function that
writes commits its own transaction; every multi-table write (e.g. a
status change plus its audit event) happens inside that one commit."""

import uuid
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select, update
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
# Senior Manager per policy/company.md -- see policy/expense_policy.md
# section 2.1 for what grade L4 means for travel/accommodation caps.
SEED_EMPLOYEE_GRADE = "L4"
SEED_EMPLOYEE_BASE_CITY = "Mumbai"


# ---------------------------------------------------------------- employees

def get_or_create_seed_employee(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.email == SEED_EMPLOYEE_EMAIL))
    if employee is not None:
        return employee
    employee = Employee(
        name=SEED_EMPLOYEE_NAME,
        email=SEED_EMPLOYEE_EMAIL,
        role="employee",
        grade=SEED_EMPLOYEE_GRADE,
        base_city=SEED_EMPLOYEE_BASE_CITY,
    )
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


def recompute_claim_total(session: Session, claim_id: uuid.UUID, *, commit: bool = True) -> dict[str, Decimal]:
    """Updates claims.total_amount/currency -- a best-effort single
    figure, only meaningful when every confirmed document shares one
    currency. Cleared to None/None when there are none or several;
    server.py's API responses compute the full per-currency breakdown
    live instead of trusting this cached pair for anything but the
    common single-currency case. `commit=False` for composing into a
    larger single-transaction action (item 5d) -- see
    update_document_status's docstring."""
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
    if commit:
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
    """Item 5e: a removed document is excluded, so re-uploading the same
    file after removing it is allowed -- not blocked by a stale
    duplicate that's no longer visible to the employee at all."""
    return session.scalar(
        select(Document).where(
            Document.claim_id == claim_id,
            Document.file_sha256 == file_sha256,
            Document.status != "removed",
        )
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
    commit: bool = True,
) -> Document:
    """`commit=False` lets a caller compose this with other writes into
    one transaction (item 5d) -- e.g. server.confirm_document, which
    saves edits, confirms, and recomputes the claim total as a single
    user action and must not leave the DB half-updated if any step
    after the first raises. The caller is then responsible for calling
    session.commit() itself, exactly once, after every step succeeds."""
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    document.status = status
    document.error_message = error_message
    if raw_markdown is not None:
        document.raw_markdown = raw_markdown
    if commit:
        session.commit()
    else:
        # Without a commit, refresh() below would otherwise re-SELECT
        # and silently discard this change if it were never sent to the
        # DB at all -- flush() sends it within the still-open
        # transaction without ending it.
        session.flush()
    session.refresh(document)
    return document


def update_document_status_if_processing(
    session: Session,
    document_id: uuid.UUID,
    *,
    status: str,
    error_message: Optional[str] = None,
    raw_markdown: Optional[str] = None,
) -> bool:
    """Item 5c: every status write the background pipeline itself makes
    (server._run_pipeline) goes through this instead of the plain
    update above -- an atomic UPDATE ... WHERE status = 'processing',
    not a read-then-write, so a pipeline that's still running when the
    employee confirms or removes the document (or another request
    already failed/finished it) can never overwrite that outcome once
    it finally completes. Returns whether the row was actually updated.
    Callers that already know their own precondition holds (an
    employee save, a revert, a retry -- each behind its own status
    guard in server.py) keep using the plain update_document_status."""
    values: dict[str, Any] = {"status": status, "error_message": error_message}
    if raw_markdown is not None:
        values["raw_markdown"] = raw_markdown
    result = session.execute(
        update(Document).where(Document.id == document_id, Document.status == "processing").values(**values)
    )
    session.commit()
    return result.rowcount > 0


def recover_stuck_processing_documents(session: Session) -> list[Document]:
    """Every document still "processing" has no pipeline task running
    for it anymore -- this is a single-process server, so a restart
    kills every in-flight pipeline unconditionally, not just ones
    stuck past some age. Called once from server.py's startup hook, so
    there's no "legitimately still running" document to exempt by age."""
    stuck = list(session.scalars(select(Document).where(Document.status == "processing")))
    for document in stuck:
        document.status = "failed"
        document.error_message = "Processing was interrupted. Retry."
    if stuck:
        session.commit()
    return stuck


def delete_document(session: Session, document_id: uuid.UUID, actor_id: uuid.UUID, *, commit: bool = True) -> Document:
    """Soft delete (item 5e): sets status="removed" instead of deleting
    the row. Extractions, corrections, the audit trail and the
    uploaded file are all kept -- only what's shown to the employee and
    counted in totals changes (server._visible_documents), and the
    document's file_sha256 stops blocking a re-upload of the same file
    (find_document_by_sha256, and the partial unique index in
    models.py that backs it at the DB level too)."""
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    document.status = "removed"
    session.add(
        AuditEvent(
            claim_id=document.claim_id,
            document_id=document.id,
            actor_id=actor_id,
            action="removed",
            payload={"original_name": document.original_name},
        )
    )
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(document)
    return document


def confirm_document(session: Session, document_id: uuid.UUID, actor_id: uuid.UUID, *, commit: bool = True) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    document.status = "confirmed"
    session.add(
        AuditEvent(claim_id=document.claim_id, document_id=document.id, actor_id=actor_id, action="confirmed")
    )
    if commit:
        session.commit()
    else:
        # Without a commit, refresh() below would otherwise re-SELECT
        # and silently discard this change if it were never sent to the
        # DB at all -- flush() sends it within the still-open
        # transaction without ending it.
        session.flush()
    session.refresh(document)
    return document


def reopen_document(session: Session, document_id: uuid.UUID, actor_id: uuid.UUID, *, commit: bool = True) -> Document:
    """"Edit again": unconditionally back to needs_review (not
    recomputed from checks) so the employee's edit screen reappears."""
    document = session.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    document.status = "needs_review"
    session.add(
        AuditEvent(claim_id=document.claim_id, document_id=document.id, actor_id=actor_id, action="reopened")
    )
    if commit:
        session.commit()
    else:
        # Without a commit, refresh() below would otherwise re-SELECT
        # and silently discard this change if it were never sent to the
        # DB at all -- flush() sends it within the still-open
        # transaction without ending it.
        session.flush()
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
    repair_attempted: bool = False,
    repair_accepted: Optional[bool] = None,
    first_attempt_tokens: Optional[int] = None,
    repair_attempt_tokens: Optional[int] = None,
    commit: bool = True,
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
        repair_attempted=repair_attempted,
        repair_accepted=repair_accepted,
        first_attempt_tokens=first_attempt_tokens,
        repair_attempt_tokens=repair_attempt_tokens,
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
                direction=correction.get("direction"),
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
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(extraction)
    return extraction


def list_corrections(session: Session, document_id: uuid.UUID) -> list[Correction]:
    return list(
        session.scalars(
            select(Correction).where(Correction.document_id == document_id).order_by(Correction.created_at)
        )
    )


def set_reason_for_corrections(session: Session, extraction_id: uuid.UUID, reason: str, *, commit: bool = True) -> int:
    """Attaches `reason` to every correction row belonging to
    `extraction_id` -- called from server.confirm_document when a money
    edit (whether saved in this request or an earlier one, either way
    now part of the latest extraction) leaves an arithmetic check
    failing. Applied to the whole extraction's corrections rather than
    filtered down to just the money field(s): the common case is the one
    field that triggered the requirement, and over-attaching the reason
    to an incidental, non-money edit saved in the same request is an
    acceptable simplification."""
    rows = list(session.scalars(select(Correction).where(Correction.extraction_id == extraction_id)))
    for row in rows:
        row.reason = reason
    if rows and commit:
        session.commit()
    return len(rows)


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
