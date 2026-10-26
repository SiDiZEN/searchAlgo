"""Apply SQL migrations in backend/migrations in filename order.

Usage:
    python backend/scripts/migrate.py            # apply pending migrations
    python backend/scripts/migrate.py --status   # list applied / pending
    python backend/scripts/migrate.py --reset --yes   # DEV ONLY: wipe the schema, then migrate

Each file runs in its own transaction and is recorded in schema_migrations.
Never edit a migration that has been applied anywhere shared; add a new file.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from _env import MIGRATIONS_DIR, database_url

LOCK_ID = 7_420_001  # arbitrary; stops two migrators running at once


def migration_files():
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def applied_versions(conn: psycopg.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def reset(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")


def migrate(url: str, *, do_reset: bool = False, verbose: bool = True) -> list[str]:
    """Apply pending migrations; returns the versions applied."""
    applied_now: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            if do_reset:
                reset(conn)
            done = applied_versions(conn)
            for path in migration_files():
                if path.stem in done:
                    continue
                if verbose:
                    print(f"applying {path.name}")
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied_now.append(path.stem)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    if verbose:
        print("up to date" if not applied_now else f"applied {len(applied_now)} migration(s)")
    return applied_now


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="show applied and pending migrations")
    parser.add_argument("--reset", action="store_true", help="DEV ONLY: drop everything and re-run all migrations")
    parser.add_argument("--yes", action="store_true", help="confirm --reset")
    args = parser.parse_args()
    url = database_url()

    if args.status:
        with psycopg.connect(url, autocommit=True) as conn:
            done = applied_versions(conn)
        for path in migration_files():
            print(f"{'applied' if path.stem in done else 'PENDING'}  {path.name}")
        return 0

    if args.reset and not args.yes:
        print("--reset wipes every table. Re-run with --reset --yes if you mean it.", file=sys.stderr)
        return 1

    migrate(url, do_reset=args.reset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
