"""recruiter contacts

Revision ID: e509ae4f2d7d
Revises: 22b1cd8b7dc0
Create Date: 2026-10-15 14:59:50.846325

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e509ae4f2d7d'
down_revision: Union[str, Sequence[str], None] = '22b1cd8b7dc0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Reviewed by hand: one new table keyed by the URL; no FK, no Computed column, no partial index.
    op.create_table('recruiter_contacts',
    sa.Column('linkedin_url', sa.Text(), nullable=False),
    sa.Column('written_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('linkedin_url', name=op.f('pk_recruiter_contacts'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('recruiter_contacts')
