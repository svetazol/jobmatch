"""employer identities

Revision ID: c2622182dffa
Revises: a91d9ad9f37f
Create Date: 2026-10-12 19:35:22.086431

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c2622182dffa'
down_revision: Union[str, Sequence[str], None] = 'a91d9ad9f37f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Reviewed by hand: one new table, no Computed column, no partial index.
    op.create_table('employer_identities',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=False),
    sa.Column('inputs_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('answer', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('homepage_ok', sa.Boolean(), nullable=True),
    sa.Column('careers_ok', sa.Boolean(), nullable=True),
    sa.Column('llm_call_id', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['llm_call_id'], ['llm_calls.id'], name=op.f('fk_employer_identities_llm_call_id_llm_calls')),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_employer_identities_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_employer_identities')),
    sa.UniqueConstraint('inputs_fingerprint', name=op.f('uq_employer_identities_inputs_fingerprint'))
    )
    op.create_index(op.f('ix_employer_identities_vacancy_id'), 'employer_identities', ['vacancy_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_employer_identities_vacancy_id'), table_name='employer_identities')
    op.drop_table('employer_identities')
