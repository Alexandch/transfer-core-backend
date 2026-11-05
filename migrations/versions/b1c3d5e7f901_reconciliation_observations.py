"""Durable reconciliation evidence and explicit review reasons."""

import sqlalchemy as sa
from alembic import op

revision = "b1c3d5e7f901"
down_revision = "e4b448218b82"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("transfers", sa.Column("review_reason", sa.String(150), nullable=True))
    op.add_column(
        "attempts",
        sa.Column("failure_confirmations", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "attempts", sa.Column("conflict", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("attempts", "failure_confirmations", server_default=None)
    op.alter_column("attempts", "conflict", server_default=None)
    op.create_table(
        "attempt_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", sa.String(150), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("payout_id", sa.String(150), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attempt_observations_attempt_id", "attempt_observations", ["attempt_id"])


def downgrade():
    op.drop_table("attempt_observations")
    op.drop_column("attempts", "conflict")
    op.drop_column("attempts", "failure_confirmations")
    op.drop_column("transfers", "review_reason")
