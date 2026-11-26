"""users.display_name: a name chosen at sign-up, shown instead of the email

Existing users are backfilled from their email (see app.users.default_display_name).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


NOW = sa.text("CURRENT_TIMESTAMP")


def _create_users(name: str, *, with_display_name: bool) -> None:
    columns = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
    ]
    if with_display_name:
        columns.append(sa.Column("display_name", sa.String(), nullable=False))
    op.create_table(
        name,
        *columns,
        sa.CheckConstraint("role IN ('owner', 'member')", name=op.f("ck_users_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )


def _fk_off() -> None:
    """Must run before the migration's first data change: SQLite ignores this inside a transaction,
    and the connection (made by app.db.make_engine) turns it on again for the next connection."""
    op.get_bind().exec_driver_sql("PRAGMA foreign_keys=OFF")


def _rebuild_users(*, with_display_name: bool, name_expression: str) -> None:
    """SQLite cannot add a NOT NULL column without a default (or drop one portably), so the
    table is rebuilt in the same shape as the model. Other tables reference users, so
    foreign key enforcement is off while the old table is dropped (see _fk_off)."""
    conn = op.get_bind()
    _create_users("users_new", with_display_name=with_display_name)
    conn.exec_driver_sql(
        "INSERT INTO users_new (id, email, password_hash, role, created_at"
        + (", display_name" if with_display_name else "")
        + ") SELECT id, email, password_hash, role, created_at"
        + (f", {name_expression}" if with_display_name else "")
        + " FROM users"
    )
    op.drop_table("users")
    op.rename_table("users_new", "users")
    if conn.exec_driver_sql("PRAGMA foreign_key_check").all():
        raise RuntimeError("foreign key check failed after rebuilding users")


def upgrade() -> None:
    from app.users import default_display_name

    _fk_off()
    # Add nullable, backfill every existing user, then rebuild with NOT NULL and no default.
    op.add_column("users", sa.Column("display_name", sa.String(), nullable=True))
    conn = op.get_bind()
    for user_id, email in conn.execute(sa.text("SELECT id, email FROM users")).all():
        conn.execute(
            sa.text("UPDATE users SET display_name = :name WHERE id = :id"),
            {"name": default_display_name(email, user_id), "id": user_id},
        )
    _rebuild_users(with_display_name=True, name_expression="display_name")


def downgrade() -> None:
    _fk_off()
    _rebuild_users(with_display_name=False, name_expression="")
