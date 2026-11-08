"""Opt-in restricted-login cache probe for an existing Supabase Session Pooler database.

Run manually only. This script never initializes or clears the shared cache.
"""
import getpass
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from urllib.parse import quote, urlencode
import uuid

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database import open_cache  # noqa: E402
from database_config import PostgresConfig  # noqa: E402
from embeddings import DIMENSIONS  # noqa: E402
import postgres_cache as pg  # noqa: E402
from semantic_cache import QueryEligibility, ResponseEligibility  # noqa: E402

OPT_IN = "LCR_TASK8C_RUNTIME_PROBE"


class ProbeError(RuntimeError):
    """A probe invariant failed; never include connection details."""


def _settings(environ):
    """Reject unexpected destinations and TLS settings before prompting."""
    host = environ.get("PGHOST", "")
    user = environ.get("PGUSER", "")
    certificate = environ.get("PGSSLROOTCERT", "")
    if (
        environ.get(OPT_IN) != "1"
        or re.fullmatch(r"aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com", host) is None
        or environ.get("PGPORT") != "5432"
        or environ.get("PGDATABASE") != "postgres"
        or re.fullmatch(r"lcr_app_login\.[a-z0-9]+", user) is None
        or environ.get("PGSSLMODE") != "verify-full"
        or not certificate
        or not Path(certificate).is_file()
        or environ.get("PGHOSTADDR")
        or environ.get("PGSERVICE")
        or environ.get("PGOPTIONS")
    ):
        raise ProbeError("Explicit opt-in and verified Session Pooler settings are required.")
    return host, user, certificate


def _config(host, user, certificate, password):
    if not password:
        raise ProbeError("A nonempty password is required.")
    # PostgresConfig requires a URI. Keep it in memory and never display it.
    options = urlencode({"sslmode": "verify-full", "sslrootcert": certificate})
    dsn = (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:5432/postgres?{options}"
    )
    return PostgresConfig(dsn)


def _vector():
    vector = np.zeros(DIMENSIONS, dtype=np.float64)
    vector[0] = 1.0
    return vector


def _cleanup(cache, namespace, query, response, model, entry_id):
    """Remove only a row proven to be this probe's synthetic entry."""
    with pg._transaction(cache._db()) as cursor:
        cursor.execute(
            f"SELECT id, query_key, cached_response, response_model FROM {pg.TABLE} "
            "WHERE namespace = %s",
            (namespace,),
        )
        rows = cursor.fetchall()
        if len(rows) > 1 or any(
            row["query_key"] != query
            or row["cached_response"] != response
            or row["response_model"] != model
            or (entry_id is not None and row["id"] != entry_id)
            for row in rows
        ):
            raise ProbeError("Probe namespace contains an unexpected row; no cleanup attempted.")
        if rows:
            cursor.execute(
                f"DELETE FROM {pg.TABLE} WHERE id = %s AND namespace = %s "
                "AND query_key = %s AND cached_response = %s AND response_model = %s",
                (rows[0]["id"], namespace, query, response, model),
            )
            if cursor.rowcount != 1:
                raise ProbeError("Exact probe-row cleanup did not delete one row.")
    with pg._transaction(cache._db()) as cursor:
        cursor.execute(
            f"SELECT count(*) AS count FROM {pg.TABLE} WHERE namespace = %s",
            (namespace,),
        )
        if cursor.fetchone()["count"] != 0:
            raise ProbeError("Probe namespace is not empty after cleanup.")


def _probe(config, token):
    namespace = f"task8c-probe-{token}"
    query = f"Define the synthetic cache marker {token}."
    response = f"Synthetic cache answer for marker {token}."
    model = "task8c-synthetic"
    query_review = QueryEligibility(
        query=query,
        context_independent=True,
        non_sensitive=True,
        time_independent=True,
        external_state_independent=True,
    )
    response_review = ResponseEligibility(
        response=response,
        successful=True,
        not_safety_refusal=True,
        non_sensitive=True,
    )
    embedder = SimpleNamespace(embed_query=lambda _text: _vector())
    entry_id = None
    preflight_passed = False

    with open_cache(embedder, database_config=config, namespace=namespace, ttl_seconds=120) as cache:
        try:
            with pg._transaction(cache._db()) as cursor:
                cursor.execute("SELECT current_user AS role")
                if cursor.fetchone()["role"] != "lcr_app_login":
                    raise ProbeError("Connection did not assume the restricted login.")
                cursor.execute(
                    f"SELECT count(*) AS count FROM {pg.TABLE} WHERE namespace = %s",
                    (namespace,),
                )
                if cursor.fetchone()["count"] != 0:
                    raise ProbeError("Probe namespace is not empty.")
            preflight_passed = True

            entry_id = cache.save(
                query, response, model,
                eligibility=query_review,
                response_eligibility=response_review,
            )
            if not entry_id:
                raise ProbeError("Synthetic cache entry was not saved.")

            hit = cache.lookup(query, eligibility=query_review)
            if not (
                hit.hit and hit.entry_id == entry_id
                and hit.response == response and hit.matched_query == query
            ):
                raise ProbeError("Synthetic cache lookup did not return the saved answer.")

            with pg._transaction(cache._db()) as cursor:
                locked = cache._exact(cursor, query, lock=True)
                if locked is None or locked["id"] != entry_id:
                    raise ProbeError("Restricted-login row lock failed.")

            if cache.invalidate(entry_id=entry_id) != 1:
                raise ProbeError("Synthetic cache invalidation did not delete one entry.")
        finally:
            if preflight_passed:
                _cleanup(cache, namespace, query, response, model, entry_id)


def main():
    try:
        host, user, certificate = _settings(os.environ)
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise ProbeError("Run from a private interactive terminal.")
        password = getpass.getpass("Restricted database password: ")
        config = _config(host, user, certificate, password)
        del password
    except Exception:
        print("Probe not started: check opt-in, Session Pooler settings and private terminal.", file=sys.stderr)
        return 2

    token = uuid.uuid4().hex
    print(f"Probe namespace: task8c-probe-{token}", flush=True)
    try:
        _probe(config, token)
    except Exception:
        print(
            f"Probe failed. Use task8c-probe-{token} for narrowly scoped cleanup review.",
            file=sys.stderr,
        )
        return 1
    print("Restricted-login cache probe passed; synthetic entry removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
