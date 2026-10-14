"""recruiter searches

Revision ID: 22b1cd8b7dc0
Revises: c2622182dffa
Create Date: 2026-10-13 16:20:30.821716

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '22b1cd8b7dc0'
down_revision: Union[str, Sequence[str], None] = 'c2622182dffa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Reviewed by hand: one new table and its (employer_id, created_at) index;
    # no Computed column, no partial index, no unique fingerprint (on purpose).
    op.create_table('recruiter_searches',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('employer_id', sa.BigInteger(), nullable=False),
    sa.Column('inputs_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('answer', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('llm_call_id', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['employer_id'], ['employers.id'], name=op.f('fk_recruiter_searches_employer_id_employers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['llm_call_id'], ['llm_calls.id'], name=op.f('fk_recruiter_searches_llm_call_id_llm_calls')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recruiter_searches'))
    )
    op.create_index('ix_recruiter_searches_employer_created', 'recruiter_searches', ['employer_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_recruiter_searches_employer_created', table_name='recruiter_searches')
    op.drop_table('recruiter_searches')
