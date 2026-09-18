"""SQLAlchemy 2.0 typed models for the expense reimbursement database.

A claim is not the same as a document: one claim holds several documents
(e.g. a conveyance form + a phone bill + a manager's approval email).
Each document's extracted fields are append-only versions in `extractions`
-- the AI version (source="ai") is never updated; every employee save adds
a new row (source="employee"). `corrections` records the per-field diff
between the AI version and what the employee actually saved.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="employee")
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True
    )
    department: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    claims: Mapped[list["Claim"]] = relationship(back_populates="employee")


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    note_to_approver: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    # Nullable: a claim whose confirmed documents mix currencies has no
    # single currency to report -- see repository.compute_claim_totals.
    currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="INR")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    employee: Mapped["Employee"] = relationship(back_populates="claims")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="claim",
        cascade="all, delete-orphan",
        order_by="Document.uploaded_at",
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("claim_id", "file_sha256", name="uq_document_claim_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_key: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    raw_markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="processing")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    claim: Mapped["Claim"] = relationship(back_populates="documents")
    extractions: Mapped[list["Extraction"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="Extraction.version",
    )


class Extraction(Base):
    """Append-only. One row per save; the AI version (source='ai',
    version=1) is never mutated. vendor_name/bill_date/amount are
    denormalized queryable copies of `fields` for Stage 4 fraud checks."""

    __tablename__ = "extractions"
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_extraction_document_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    vendor_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bill_date: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    # Null when the model gave none and exactly one couldn't be detected
    # from the raw markdown either -- see validate.build_claim.
    currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="extractions")
    check_results: Mapped[list["CheckResultRow"]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan"
    )


class CheckResultRow(Base):
    __tablename__ = "check_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False
    )
    check_name: Mapped[str] = mapped_column(Text, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    extraction: Mapped["Extraction"] = relationship(back_populates="check_results")


class Correction(Base):
    """Replaces the old corrections.jsonl file. One row per changed field
    per save, diffing the new employee extraction against the AI one."""

    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False
    )
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    ai_value: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    employee_value: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class AuditEvent(Base):
    """Append-only. One row per state change: created, uploaded,
    extracted, edited, confirmed, removed, submitted, failed."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False)
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
