"""control plane tables

Orchestration state for multi-account routing, verification-gated DAGs,
approvals, and protected tasks. Plain DDL. The application store in
shared.control_plane uses the same column names and can run on SQLite for
tests; this migration is the Postgres form inside Vicoa's schema history.

Revision ID: a8c1e4b72d09
Revises: c3d9e5a7b1f4
Create Date: 2026-09-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a8c1e4b72d09"
down_revision: Union[str, None] = "c3d9e5a7b1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "control_accounts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("profile", sa.String(length=64), nullable=False),
        sa.Column("runtime_home", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("drained", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("constrained", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("constraint_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("max_workers", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_workers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auth_state", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("manager_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("vicoa_project_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("import_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_system", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("plan_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("worker_status", sa.String(length=32), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("owner_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("import_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("vicoa_task_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_system", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.ForeignKeyConstraint(["job_id"], ["control_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "plan_key"),
        sa.UniqueConstraint("session_id"),
    )
    op.create_table(
        "control_dependencies",
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("depends_on", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["depends_on"], ["control_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["control_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "depends_on"),
    )
    op.create_table(
        "control_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("permanent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["task_id"], ["control_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_quota_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("pool", sa.Text(), nullable=False),
        sa.Column("window_name", sa.Text(), nullable=False),
        sa.Column("value_pct", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("control_quota_observations")
    op.drop_table("control_approvals")
    op.drop_table("control_dependencies")
    op.drop_table("control_tasks")
    op.drop_table("control_jobs")
    op.drop_table("control_accounts")
