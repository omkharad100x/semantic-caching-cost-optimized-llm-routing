"""Read-only Supabase cache schema verification via the Session Pooler."""

import getpass
import os
from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row

# Running this file from scripts/ still needs access to the repository modules.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database import DatabaseError  # noqa: E402
from postgres_catalog import verify_catalog  # noqa: E402


def main() -> int:
    settings = os.environ
    if (
        settings.get("PGSSLMODE") != "verify-full"
        or not settings.get("PGSSLROOTCERT")
        or settings.get("PGDATABASE") != "postgres"
        or settings.get("PGPORT") != "5432"
        or not settings.get("PGHOST", "").lower().endswith(".pooler.supabase.com")
        or not settings.get("PGUSER")
    ):
        print("Expected verified Supabase Session Pooler settings.", file=sys.stderr)
        return 1

    try:
        password = getpass.getpass("Database password: ")
        with psycopg.connect(
            host=settings["PGHOST"],
            port=settings["PGPORT"],
            dbname=settings["PGDATABASE"],
            user=settings["PGUSER"],
            password=password,
            sslmode="verify-full",
            sslrootcert=settings["PGSSLROOTCERT"],
            gssencmode="disable",
            connect_timeout=5,
            prepare_threshold=None,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            del password
            with connection.cursor() as cursor:
                cursor.execute("BEGIN READ ONLY")
                try:
                    cursor.execute("SET LOCAL statement_timeout = '5s'")
                    cursor.execute("SET LOCAL lock_timeout = '2s'")
                    cursor.execute("SELECT version FROM lcr_cache.schema_version")
                    if [row["version"] for row in cursor.fetchall()] != [2]:
                        raise DatabaseError("Unexpected cache schema version.")
                    verify_catalog(cursor, 2)
                finally:
                    cursor.execute("ROLLBACK")
    except Exception:
        print("Cache catalog verification failed.", file=sys.stderr)
        return 1

    print("Cache schema version 2 and full catalog verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
