from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

import models
from database import Base


def run_migration(connection, revision: str) -> None:
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def test_migrations_preserve_existing_users_and_posts(tmp_path):
    database_path = tmp_path / "migration-test.db"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            run_migration(connection, "0001_initial")
            connection.execute(
                text(
                    "INSERT INTO users (username, email, password_hash) "
                    "VALUES ('existing', 'existing@example.test', 'hashed')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO posts (title, content, user_id, date_posted) "
                    "VALUES ('Existing post', 'Keep this content', 1, CURRENT_TIMESTAMP)"
                )
            )
            connection.commit()

            run_migration(connection, "head")

            user = connection.execute(
                text(
                    "SELECT username, email_verified_at, auth_version, password_hash "
                    "FROM users WHERE id = 1"
                )
            ).one()
            post = connection.execute(
                text("SELECT title, content, summary FROM posts WHERE id = 1")
            ).one()
            assert user.username == "existing"
            assert user.email_verified_at is not None
            assert user.auth_version == 0
            assert user.password_hash == "hashed"
            password_hash_column = next(
                row
                for row in connection.execute(
                    text("PRAGMA table_info(users)")
                )
                if row.name == "password_hash"
            )
            assert password_hash_column.notnull == 1
            assert post.title == "Existing post"
            assert post.content == "Keep this content"
            assert post.summary is None
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0002_publication_features"
            current_config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            current_config.attributes["connection"] = connection
            command.check(current_config)
    finally:
        engine.dispose()


def test_migration_rejects_users_without_password_hash(tmp_path):
    database_path = tmp_path / "missing-password-hash.db"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            run_migration(connection, "0001_initial")
            connection.execute(
                text(
                    "INSERT INTO users (username, email, password_hash) "
                    "VALUES ('invalid', 'invalid@example.test', NULL)"
                )
            )
            connection.commit()

            with pytest.raises(RuntimeError, match="users.password_hash"):
                run_migration(connection, "head")
    finally:
        engine.dispose()


def test_migration_adopts_feature_tables_created_before_alembic(tmp_path):
    database_path = tmp_path / "partially-initialized.db"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            run_migration(connection, "0001_initial")
            connection.execute(
                text(
                    "INSERT INTO users (username, email, password_hash) "
                    "VALUES ('existing', 'existing@example.test', 'hashed')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO posts (title, content, user_id, date_posted) "
                    "VALUES ('Existing post', 'Keep this content', 1, CURRENT_TIMESTAMP)"
                )
            )
            connection.commit()

            Base.metadata.create_all(connection)
            connection.execute(
                text(
                    "INSERT INTO categories (name, slug) "
                    "VALUES ('Existing category', 'existing-category')"
                )
            )
            connection.execute(
                text("INSERT INTO tags (name, slug) VALUES ('Existing tag', 'existing-tag')")
            )
            connection.execute(
                text("INSERT INTO post_tags (post_id, tag_id) VALUES (1, 1)")
            )
            connection.commit()

            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.stamp(config, "0001_initial")
            run_migration(connection, "head")

            assert connection.execute(
                text("SELECT slug FROM categories WHERE id = 1")
            ).scalar_one() == "existing-category"
            assert connection.execute(
                text("SELECT slug FROM tags WHERE id = 1")
            ).scalar_one() == "existing-tag"
            assert connection.execute(
                text("SELECT COUNT(*) FROM post_tags WHERE post_id = 1 AND tag_id = 1")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT COUNT(*) FROM email_action_tokens")
            ).scalar_one() == 0
            check_config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            check_config.attributes["connection"] = connection
            command.check(check_config)
    finally:
        engine.dispose()
