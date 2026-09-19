"""SQLAlchemy 2.0 typed models for the expense reimbursement database.

A claim is not the same as a document: one claim holds several documents
(e.g. a conveyance form + a phone bill + a manager's approval email).
Each document's extracted fields are append-only versions in `extractions`
-- the AI version (source="ai") is never updated; every employee save adds
a new row (source="employee"). `corrections` records the per-field diff
between the AI version and what the employee actually saved.

Stage 3 adds the policy tables (`policy_versions`, `policy_clauses`) and
`policy_decisions`: one immutable row per evaluated unit of a claim's
documents -- see policy_eval.py and docs/STAGE3.md.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType


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
    # Grade (L1-L6) and base city, per policy/expense_policy.md section 2 --
    # unused by Stage 1/2 logic, provisioned for Stage 3's policy engine
    # (per-grade/city caps need to know who's claiming).
    grade: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_city: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
    __table_args__ = (
        # Item 5e: a partial index, not a plain UniqueConstraint -- a
        # soft-removed document (status="removed") keeps its row (and
        # file_sha256) forever, so re-uploading the same file after
        # removing it must not collide with the old, now-invisible row.
        # Only ever one non-removed document per (claim, sha256).
        Index(
            "uq_document_claim_sha256_active",
            "claim_id", "file_sha256",
            unique=True,
            postgresql_where=text("status != 'removed'"),
        ),
    )

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
    # Self-repair retry (extract.extract_claim_with_repair): only ever
    # true/non-null for source="ai" rows -- an employee save never calls
    # Groq at all. repair_accepted is null when no repair was attempted,
    # and false when it was attempted but discarded (didn't pass strictly
    # more arithmetic checks than the first attempt).
    repair_attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    repair_accepted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    first_attempt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    repair_attempt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    vendor_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bill_date: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    # Null when the model gave none and exactly one couldn't be detected
    # from the raw markdown either -- see validate.build_claim.
    currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # What the expense was FOR (categories.py), separate from
    # `document_type` (what kind of paper it is) above. Null for a
    # document_type that isn't an expense at all (approval_correspondence)
    # -- see categories.is_categorizable. category_method is "rules" /
    # "llm" / "classifier" / "hybrid" (categorize.py) for an AI-assigned
    # category, or "employee" once the employee has edited it; never
    # shown to the employee (see review_view/server.py).
    category: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category_confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    category_method: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
    # None for an ordinary per-cell value change; "row_added"/"row_removed"
    # for a whole array element (a trip, a line item) added/removed
    # wholesale rather than edited -- see server.diff_values.
    change_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Employee's own explanation, only ever required (and collected) for
    # a money-field edit that leaves an arithmetic check failing -- see
    # server._reason_required_for. Null otherwise.
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "increase"/"decrease"/"none", computed from ai_value vs
    # employee_value at correction-creation time (numeric comparison;
    # "none" when either side isn't a number, e.g. a text field).
    direction: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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


# ---------------------------------------------------------------- Stage 3

class Vector(UserDefinedType):
    """pgvector's `vector(n)` column type, declared by hand because the
    `pgvector` Python package isn't a dependency (Stage 3 adds none). The
    column exists for a future semantic-retrieval stage and is never
    populated or queried today -- see PolicyClause.embedding."""

    cache_ok = True

    def __init__(self, dim: int):
        self.dim = dim

    def get_col_spec(self, **kw) -> str:
        return f"vector({self.dim})"


# all-MiniLM-L6-v2's output size -- the embedder categorize.py already uses.
EMBEDDING_DIM = 384


class PolicyVersion(Base):
    """One published version of the expense policy. Clauses are immutable
    once a version exists; a policy change is a new version (v2) whose
    clauses sit alongside v1's, so a historical decision's `policy_version`
    always resolves to the exact clause text it was judged against."""

    __tablename__ = "policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    source_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    # Grades / city tiers / zones parsed from the policy's section 2 --
    # the reference tables the deterministic checker resolves dimensions
    # from (policy.py).
    reference_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Who/what structured it, the CONFLICTS report, and whether a human
    # reviewed it -- see scripts/build_policy.py.
    build_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    clauses: Mapped[list["PolicyClause"]] = relationship(
        back_populates="policy_version", cascade="all, delete-orphan", order_by="PolicyClause.sort_order"
    )


class PolicyClause(Base):
    __tablename__ = "policy_clauses"
    __table_args__ = (UniqueConstraint("policy_version_id", "clause_id", name="uq_policy_clause_version_clause"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_versions.id", ondelete="CASCADE"), nullable=False
    )
    clause_id: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    # Cut from the policy markdown by code, never written by the model.
    verbatim_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Category ids from categories.py, or ["*"] for a rule that applies to
    # every category (receipts, prohibitions, submission windows).
    applies_to_categories: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    # Scalar copy of the limit when the clause has exactly one (else null);
    # `limit_table` is what the checker actually reads.
    limit_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    limit_currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # per_trip | per_night | per_day | per_month | per_person | per_claim |
    # none, plus per_ride | per_item | per_km | per_event | percent_of_amount
    # (the policy prices things the spec's seven values can't express).
    limit_unit: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    # amount | percent | count
    limit_kind: Mapped[str] = mapped_column(Text, nullable=False, default="amount")
    # false for "under X" (X itself is not allowed), true for "up to X".
    limit_inclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # [{"amount": "6000", "currency": "INR", "when": {"grade": ["L3"], "city_tier": ["1"]}}]
    limit_table: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    documentation_required: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # {"INR": "15000", "USD": "200"}; "0" means approval is always required.
    requires_approval_above: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    is_prohibition: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # RESERVED for future semantic retrieval. Deliberately never populated
    # and never queried: 24 sections retrieve by category filter.
    embedding: Mapped[Optional[Any]] = mapped_column(Vector(EMBEDDING_DIM), nullable=True, deferred=True)

    policy_version: Mapped["PolicyVersion"] = relationship(back_populates="clauses")


class PolicyDecision(Base):
    """Immutable: one row per evaluated unit (a line item, a group of line
    items, or a whole document) per evaluation run. Re-evaluating writes new
    rows with a higher `evaluation_seq`; nothing here is ever updated except
    the five human_* / overridden_* columns a reviewer fills in (a database
    trigger rejects any other UPDATE and every DELETE -- see the migration).
    Every input the verdict depended on is recorded, so this table is the
    labeled-data capture layer a future eval set is built from."""

    __tablename__ = "policy_decisions"
    __table_args__ = (
        UniqueConstraint("claim_id", "evaluation_seq", "document_id", "unit_index", name="uq_policy_decision_unit"),
        Index("ix_policy_decisions_claim_seq", "claim_id", "evaluation_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    # The exact extraction version that was judged (employee edits create
    # new versions), so a decision can always be replayed.
    extraction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("extractions.id"), nullable=False)
    evaluation_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # "line_item" (line_item_ref names line items) | "document" (whole document).
    evaluation_mode: Mapped[str] = mapped_column(Text, nullable=False)
    # Null for a document-mode unit; "2" or "0,1" (line-item indexes in the
    # extraction's line_items array) for a line-item-mode unit.
    line_item_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Stage 2's label at evaluation time. Required, so a bad verdict can be
    # traced to a bad category rather than to Stage 3. category_method is
    # rules | classifier | llm | hybrid | employee (an employee edit).
    category_used: Mapped[str] = mapped_column(Text, nullable=False)
    category_confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    category_method: Mapped[str] = mapped_column(Text, nullable=False)

    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)

    clause_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    clause_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    limit_applied: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    limit_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    limit_currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amount_compared: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amount_derivation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    comparison_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    model_confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Set when the unit failed strict validation (bad clause id, amount
    # that doesn't reconcile...) -- the verdict is then always
    # insufficient_information.
    hard_failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Full deterministic trace: dimensions, denominators, limit candidates,
    # condition results, aggregation members.
    check_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    raw_request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    # The reviewer's label -- the only columns ever updated on this row.
    human_verdict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    human_clause_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    human_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    overridden_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    overridden_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True
    )
