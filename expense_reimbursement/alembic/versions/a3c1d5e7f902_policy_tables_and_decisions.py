"""policy tables and decisions (Stage 3)

Revision ID: a3c1d5e7f902
Revises: 9715e7d323b3
Create Date: 2026-09-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a3c1d5e7f902'
down_revision: Union[str, Sequence[str], None] = '9715e7d323b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Columns a reviewer may fill in after the fact; every other column of a
# policy_decisions row is frozen by the trigger below.
_OVERRIDE_COLUMNS = "ARRAY['human_verdict', 'human_clause_id', 'human_note', 'overridden_at', 'overridden_by']"


def upgrade() -> None:
    """Upgrade schema."""
    # pgvector ships in the docker image (pgvector/pgvector:pg16); the
    # embedding column below is reserved and never populated or queried.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        'policy_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('source_sha256', sa.Text(), nullable=False),
        sa.Column('reference_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('build_meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version'),
    )
    op.create_table(
        'policy_clauses',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('policy_version_id', sa.UUID(), nullable=False),
        sa.Column('clause_id', sa.Text(), nullable=False),
        sa.Column('section', sa.Text(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('verbatim_text', sa.Text(), nullable=False),
        sa.Column('applies_to_categories', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('limit_amount', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('limit_currency', sa.Text(), nullable=True),
        sa.Column('limit_unit', sa.Text(), nullable=False),
        sa.Column('limit_kind', sa.Text(), nullable=False),
        sa.Column('limit_inclusive', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('limit_table', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('conditions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('documentation_required', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('requires_approval_above', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('is_prohibition', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['policy_version_id'], ['policy_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('policy_version_id', 'clause_id', name='uq_policy_clause_version_clause'),
    )
    # Not expressible through sa.Column without the pgvector package.
    op.execute("ALTER TABLE policy_clauses ADD COLUMN embedding vector(384)")

    op.create_table(
        'policy_decisions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('claim_id', sa.UUID(), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column('extraction_id', sa.UUID(), nullable=False),
        sa.Column('evaluation_seq', sa.Integer(), nullable=False),
        sa.Column('unit_index', sa.Integer(), nullable=False),
        sa.Column('evaluation_mode', sa.Text(), nullable=False),
        sa.Column('line_item_ref', sa.Text(), nullable=True),
        sa.Column('category_used', sa.Text(), nullable=False),
        sa.Column('category_confidence', sa.Float(), nullable=True),
        sa.Column('category_method', sa.Text(), nullable=False),
        sa.Column('policy_version', sa.Text(), nullable=False),
        sa.Column('prompt_version', sa.Text(), nullable=False),
        sa.Column('model_name', sa.Text(), nullable=False),
        sa.Column('clause_id', sa.Text(), nullable=True),
        sa.Column('clause_text', sa.Text(), nullable=True),
        sa.Column('verdict', sa.Text(), nullable=False),
        sa.Column('limit_applied', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('limit_unit', sa.Text(), nullable=True),
        sa.Column('limit_currency', sa.Text(), nullable=True),
        sa.Column('amount_compared', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('currency', sa.Text(), nullable=True),
        sa.Column('amount_derivation', sa.Text(), nullable=True),
        sa.Column('comparison_result', sa.Text(), nullable=True),
        sa.Column('explanation', sa.Text(), nullable=False),
        sa.Column('model_confidence', sa.Float(), nullable=True),
        sa.Column('missing_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('hard_failure_reason', sa.Text(), nullable=True),
        sa.Column('check_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('raw_request', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('raw_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('cache_hit', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('human_verdict', sa.Text(), nullable=True),
        sa.Column('human_clause_id', sa.Text(), nullable=True),
        sa.Column('human_note', sa.Text(), nullable=True),
        sa.Column('overridden_at', sa.DateTime(), nullable=True),
        sa.Column('overridden_by', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['claim_id'], ['claims.id']),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.ForeignKeyConstraint(['extraction_id'], ['extractions.id']),
        sa.ForeignKeyConstraint(['overridden_by'], ['employees.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('claim_id', 'evaluation_seq', 'document_id', 'unit_index', name='uq_policy_decision_unit'),
    )
    op.create_index('ix_policy_decisions_claim_seq', 'policy_decisions', ['claim_id', 'evaluation_seq'])

    # Decisions are immutable: reject any UPDATE that touches a column other
    # than the reviewer's override columns, and every DELETE. (TRUNCATE is
    # not a row-level event, so the test suite's per-test cleanup still works.)
    op.execute(f"""
        CREATE FUNCTION policy_decisions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'policy_decisions rows are immutable and cannot be deleted';
            END IF;
            IF (to_jsonb(NEW) - {_OVERRIDE_COLUMNS}) IS DISTINCT FROM (to_jsonb(OLD) - {_OVERRIDE_COLUMNS}) THEN
                RAISE EXCEPTION 'policy_decisions rows are immutable; only the human_* / overridden_* columns may change';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER policy_decisions_guard_trg
        BEFORE UPDATE OR DELETE ON policy_decisions
        FOR EACH ROW EXECUTE FUNCTION policy_decisions_guard()
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS policy_decisions_guard_trg ON policy_decisions")
    op.execute("DROP FUNCTION IF EXISTS policy_decisions_guard()")
    op.drop_index('ix_policy_decisions_claim_seq', table_name='policy_decisions')
    op.drop_table('policy_decisions')
    op.drop_table('policy_clauses')
    op.drop_table('policy_versions')
