"""fraud assessments (Stage 4)

Revision ID: b7d2e4f6a813
Revises: a3c1d5e7f902
Create Date: 2026-09-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b7d2e4f6a813'
down_revision: Union[str, Sequence[str], None] = 'a3c1d5e7f902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REVIEW_COLUMNS = "ARRAY['human_assessment', 'human_note', 'reviewed_at', 'reviewed_by']"


def upgrade() -> None:
    op.create_table(
        'fraud_assessments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('claim_id', sa.UUID(), nullable=False),
        sa.Column('employee_id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('ruleset_version', sa.Text(), nullable=False),
        sa.Column('rules_fired', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('rules_not_applicable', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('rules_clear', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('risk_score', sa.Integer(), nullable=False),
        sa.Column('risk_band', sa.Text(), nullable=False),
        sa.Column('assessable_signal_count', sa.Integer(), nullable=False),
        sa.Column('total_signal_count', sa.Integer(), nullable=False),
        sa.Column('narrative_status', sa.Text(), nullable=False),
        sa.Column('narrative', sa.Text(), nullable=True),
        sa.Column('model_name', sa.Text(), nullable=True),
        sa.Column('prompt_version', sa.Text(), nullable=True),
        sa.Column('raw_request', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('raw_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('human_assessment', sa.Text(), nullable=True),
        sa.Column('human_note', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('reviewed_by', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['claim_id'], ['claims.id']),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['employees.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_fraud_assessments_claim_created', 'fraud_assessments', ['claim_id', 'created_at'])
    # Immutable except the reviewer columns; no deletes (TRUNCATE is not a row event).
    op.execute(f"""
        CREATE FUNCTION fraud_assessments_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'fraud_assessments rows are immutable and cannot be deleted';
            END IF;
            IF (to_jsonb(NEW) - {_REVIEW_COLUMNS}) IS DISTINCT FROM (to_jsonb(OLD) - {_REVIEW_COLUMNS}) THEN
                RAISE EXCEPTION 'fraud_assessments rows are immutable; only the human_* / reviewed_* columns may change';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER fraud_assessments_guard_trg
        BEFORE UPDATE OR DELETE ON fraud_assessments
        FOR EACH ROW EXECUTE FUNCTION fraud_assessments_guard()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS fraud_assessments_guard_trg ON fraud_assessments")
    op.execute("DROP FUNCTION IF EXISTS fraud_assessments_guard()")
    op.drop_index('ix_fraud_assessments_claim_created', table_name='fraud_assessments')
    op.drop_table('fraud_assessments')
