"""initial schema: users, project, invites, feedback_items

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.CheckConstraint("role IN ('owner', 'member')", name=op.f("ck_users_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "project",
        sa.Column("id", sa.Integer(), autoincrement=False, server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_project_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project")),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("invited_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invites_token_hash")),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], name=op.f("fk_invites_invited_by_users")),
    )
    op.create_table(
        "feedback_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("anonymous", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.CheckConstraint("category IN ('start', 'stop', 'continue')", name=op.f("ck_feedback_items_category")),
        sa.CheckConstraint("anonymous IN (0, 1)", name=op.f("ck_feedback_items_anonymous")),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], name=op.f("fk_feedback_items_author_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback_items")),
    )


def downgrade() -> None:
    op.drop_table("feedback_items")
    op.drop_table("invites")
    op.drop_table("project")
    op.drop_table("users")
