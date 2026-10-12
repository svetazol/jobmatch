"""llm_calls kind, request and response

Revision ID: a91d9ad9f37f
Revises: 5546e6d9996f
Create Date: 2026-10-12 19:17:08.720887

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a91d9ad9f37f'
down_revision: Union[str, Sequence[str], None] = '5546e6d9996f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `kind` has no default, so existing rows are filled from the data before
    # it becomes NOT NULL: every Jev call asked for a `jev-*` model, and the
    # only other rows (5 on 2026-10-12, gemini-2.5-flash-lite) are the paid
    # runs of the dropped people-search attempt. Verified by hand after
    # applying it on 2026-10-12: 13451 `match` (all `jev-%`), 5 `people` (all
    # google/gemini-2.5-flash-lite), no NULLs. No pytest: tests run against
    # the migrated database and cannot see the before state.
    op.add_column('llm_calls', sa.Column('kind', sa.String(length=16), nullable=True))
    op.execute(
        "UPDATE llm_calls SET kind = CASE WHEN model_requested LIKE 'jev-%' "
        "THEN 'match' ELSE 'people' END"
    )
    op.alter_column('llm_calls', 'kind', nullable=False)
    op.add_column('llm_calls', sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('llm_calls', sa.Column('response', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('llm_calls', 'response')
    op.drop_column('llm_calls', 'request')
    op.drop_column('llm_calls', 'kind')
