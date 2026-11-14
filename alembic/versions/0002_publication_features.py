"""Add publication, email-token, and rate-limit schema."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_publication_features"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    return any(
        column["name"] == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )


def _ensure_index(
    index_name: str,
    table_name: str,
    columns: list[str],
    unique: bool = False,
) -> None:
    existing_indexes = {
        index["name"]: index
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    existing_index = existing_indexes.get(index_name)
    if existing_index and existing_index["unique"] != unique:
        op.drop_index(index_name, table_name=table_name)
        existing_index = None
    if not existing_index:
        op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    if not _has_column("users", "email_verified_at"):
        op.add_column(
            "users",
            sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("users", "auth_version"):
        op.add_column(
            "users",
            sa.Column(
                "auth_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    op.execute(
        "UPDATE users SET email_verified_at = CURRENT_TIMESTAMP "
        "WHERE email_verified_at IS NULL"
    )
    null_password_hashes = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM users WHERE password_hash IS NULL")
    ).scalar_one()
    if null_password_hashes:
        raise RuntimeError(
            "Cannot make users.password_hash required while existing users have "
            "no password hash. Resolve those accounts before upgrading."
        )
    user_columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns("users")
    }
    with op.batch_alter_table("users") as batch_op:
        if user_columns["auth_version"]["default"] is not None:
            batch_op.alter_column(
                "auth_version",
                existing_type=sa.Integer(),
                server_default=None,
            )
        if user_columns["password_hash"]["nullable"]:
            batch_op.alter_column(
                "password_hash",
                existing_type=sa.String(length=255),
                nullable=False,
            )

    if not _has_table("categories"):
        op.create_table(
            "categories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=50), nullable=False),
            sa.Column("slug", sa.String(length=60), nullable=False),
        )
    _ensure_index("ix_categories_slug", "categories", ["slug"], unique=True)
    if not _has_table("tags"):
        op.create_table(
            "tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=40), nullable=False),
            sa.Column("slug", sa.String(length=50), nullable=False),
        )
    _ensure_index("ix_tags_slug", "tags", ["slug"], unique=True)
    if not _has_column("posts", "summary"):
        op.add_column(
            "posts",
            sa.Column("summary", sa.String(length=240), nullable=True),
        )
    if not _has_column("posts", "category_id"):
        with op.batch_alter_table("posts") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "category_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "categories.id",
                        ondelete="SET NULL",
                        name="fk_posts_category_id_categories",
                    ),
                    nullable=True,
                )
            )
    _ensure_index("ix_posts_category_id", "posts", ["category_id"])

    if not _has_table("post_tags"):
        op.create_table(
            "post_tags",
            sa.Column("post_id", sa.Integer(), nullable=False),
            sa.Column("tag_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("post_id", "tag_id"),
        )

    if not _has_table("email_action_tokens"):
        op.create_table(
            "email_action_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column("purpose", sa.String(length=30), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
    _ensure_index(
        "ix_email_action_tokens_user_id",
        "email_action_tokens",
        ["user_id"],
    )
    _ensure_index(
        "ix_email_action_tokens_purpose",
        "email_action_tokens",
        ["purpose"],
    )

    if not _has_table("rate_limit_hits"):
        op.create_table(
            "rate_limit_hits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bucket_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _ensure_index(
        "ix_rate_limit_hits_bucket_hash",
        "rate_limit_hits",
        ["bucket_hash"],
    )
    _ensure_index(
        "ix_rate_limit_hits_created_at",
        "rate_limit_hits",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_hits_created_at", table_name="rate_limit_hits")
    op.drop_index("ix_rate_limit_hits_bucket_hash", table_name="rate_limit_hits")
    op.drop_table("rate_limit_hits")
    op.drop_index(
        "ix_email_action_tokens_purpose",
        table_name="email_action_tokens",
    )
    op.drop_index(
        "ix_email_action_tokens_user_id",
        table_name="email_action_tokens",
    )
    op.drop_table("email_action_tokens")
    op.drop_table("post_tags")
    op.drop_index("ix_posts_category_id", table_name="posts")
    with op.batch_alter_table("posts") as batch_op:
        batch_op.drop_column("category_id")
        batch_op.drop_column("summary")
    op.drop_table("tags")
    op.drop_table("categories")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("auth_version")
        batch_op.drop_column("email_verified_at")
