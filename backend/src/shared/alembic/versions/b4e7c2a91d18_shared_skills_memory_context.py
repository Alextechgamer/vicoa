"""control plane skills, memory, context, and handoff

Revision ID: b4e7c2a91d18
Revises: a8c1e4b72d09
Create Date: 2026-09-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b4e7c2a91d18"
down_revision: Union[str, None] = "a8c1e4b72d09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from shared.control_plane.knowledge import KNOWLEDGE_SCHEMA
    from shared.control_plane.pg import postgres_statements

    for statement in postgres_statements(KNOWLEDGE_SCHEMA):
        op.execute(statement)


def downgrade() -> None:
    from shared.control_plane.knowledge import KNOWLEDGE_TABLES

    for table in KNOWLEDGE_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
