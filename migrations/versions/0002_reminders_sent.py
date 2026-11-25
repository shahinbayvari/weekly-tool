"""reminders_sent: one row per reminder email sent (user, week)

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "reminders_sent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.String(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders_sent")),
        sa.UniqueConstraint("user_id", "week", name=op.f("uq_reminders_sent_user_id_week")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_reminders_sent_user_id_users")),
    )


def downgrade() -> None:
    op.drop_table("reminders_sent")
