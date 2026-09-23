"""control plane tables

Native orchestration tables beside Vicoa's session and human-task tables.
Same columns as shared.control_plane.store.SCHEMA, translated for PostgreSQL.
This revision was not applied before this branch, so the table shape matches
the store instead of a parallel control_* schema.

Revision ID: a8c1e4b72d09
Revises: c3d9e5a7b1f4
Create Date: 2026-09-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a8c1e4b72d09"
down_revision: Union[str, None] = "c3d9e5a7b1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "allow_rules",
    "route_decisions",
    "events",
    "steer_messages",
    "quota_observations",
    "approvals",
    "verifications",
    "task_dependencies",
    "tasks",
    "jobs",
    "accounts",
)


def upgrade() -> None:
    from shared.control_plane.pg import postgres_statements
    from shared.control_plane.store import SCHEMA

    for statement in postgres_statements(SCHEMA):
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
