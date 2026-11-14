# Production deployment

The application is configured for PostgreSQL, SMTP delivery, and persistent upload storage in production. Keep `.env` out of source control and set its values through the hosting provider's secret manager whenever possible.

## Docker Compose / VPS

1. Copy `.env.production.example` to `.env`.
2. Set a unique random `JWT_SECRET_KEY` (at least 32 characters), a strong database password, the public HTTPS `SITE_URL`, the public host in `TRUSTED_HOSTS`, and real SMTP credentials.
3. Configure a TLS-terminating reverse proxy for the domain. The application binds to `127.0.0.1` by default; do not expose the unencrypted application port directly to the internet.
4. Build and start the services with `docker compose up --build -d`.
5. Confirm `http://127.0.0.1:8000/health/ready` returns success through the host or proxy.

The Compose file keeps PostgreSQL data and uploaded profile pictures in named persistent volumes. Back up both volumes, and test restoring them before relying on them. Do not remove volumes during routine deployments.

The web container applies Alembic migrations before starting. This is appropriate for the included single-web-container Compose setup. On platforms that run multiple web replicas, configure `alembic upgrade head` as a single release/pre-deploy command instead of running migrations concurrently in every replica.

## Existing SQLite database

The first migration describes the original `users` and `posts` tables; the next migration adds the publication features and marks existing accounts as verified so their current logins continue to work. It also adopts feature tables that may already have been created by an earlier application startup, while adding missing columns and indexes. Back up `blog.db` before adopting Alembic. For an existing database, first mark the original baseline and then apply later migrations:

```text
alembic stamp 0001_initial
alembic upgrade head
```

If any existing user has a missing password hash, the migration stops instead of making that account unusable; resolve those accounts before retrying. New databases should run `alembic upgrade head` directly. Migrations never copy the local SQLite database into PostgreSQL; use a separate, verified data migration when moving existing content to a hosted database.

## Environment and email

`APP_ENV=production` requires PostgreSQL, an HTTPS `SITE_URL`, trusted hosts, a non-placeholder signing key, and SMTP host/from-address settings. Verification and password-reset links are single-use, expire, and are stored hashed in the database. SMTP username/password are optional only for relays that explicitly do not require authentication.

The application sends email synchronously with a connection timeout. Configure a working SMTP relay before accepting sign-ups. A durable external job queue is recommended if email delivery volume grows or must be retried after process restarts.

## Release checks

- Install development dependencies with `python -m pip install -r requirements-dev.txt`.
- Run `python -m pytest`.
- Review the Alembic revision with `alembic current` and `alembic history`.
- Confirm health checks, TLS, trusted hosts, SMTP delivery, and database/media backups in the actual hosting environment.

This repository provides a portable deployment baseline; a real production launch still requires a domain, hosting account, TLS, SMTP credentials, and a tested backup/restore procedure.
