# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the Aurora integration tests.

``pg_container`` starts ``pgvector/pgvector:pg16`` through the ``docker`` CLI on a
random localhost port (override the image with ``COA_PGVECTOR_IMAGE``) and stops it
afterwards. When the Docker daemon does not answer or ``asyncpg`` is missing, every
test that needs it is skipped with the reason. The ``skip`` is real: the run summary
(``-rs``) prints it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

IMAGE = os.getenv("COA_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")

INIT_SQL = Path(__file__).resolve().parents[4] / "infra/lib/stacks/foundation/migrations/aurora-init.sql"
DIMENSIONS = 8
ENGINE_ROLES = ("coa_search_rw", "coa_search_ro", "coa_engine_rw")
ROLE_PASSWORD = "coa-engine-test-pw"  # noqa: S105  throwaway local test database


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _skip_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not found"
    try:
        info = _docker("info", timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"docker daemon not reachable: {exc}"
    if info.returncode != 0:
        return "docker daemon not reachable: " + (info.stderr.strip().splitlines() or ["docker info failed"])[-1]
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return "asyncpg is not installed (run with `uv run --all-packages`)"
    return None


@pytest.fixture(scope="session")
def pg_container() -> Iterator[str]:
    """Start a pgvector container and yield its DSN."""
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    import asyncio

    import asyncpg

    password = secrets.token_hex(12)
    run = _docker(
        "run",
        "-d",
        "--rm",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_PASSWORD={password}",
        IMAGE,
        "-c",
        "fsync=off",
        "-c",
        "max_connections=200",
    )
    if run.returncode != 0:
        pytest.skip(f"could not start {IMAGE}: {run.stderr.strip()[-300:]}")
    container = run.stdout.strip().splitlines()[-1]
    try:
        port_out = _docker("port", container, "5432/tcp").stdout.strip().splitlines()[0]
        port = int(port_out.rsplit(":", 1)[1])
        dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"

        async def ready() -> bool:
            try:
                conn = await asyncpg.connect(dsn, timeout=3)
            except Exception:
                return False
            try:
                await conn.fetchval("SELECT 1")
                return True
            finally:
                await conn.close()

        deadline = time.monotonic() + 90
        streak = 0
        while time.monotonic() < deadline:
            streak = streak + 1 if asyncio.run(ready()) else 0
            if streak >= 3:  # the image restarts Postgres once after initdb
                break
            time.sleep(1)
        else:
            pytest.skip("pgvector container did not become ready in 90 s")
        yield dsn
    finally:
        _docker("rm", "-f", container, timeout=60)


def _init_sql() -> str:
    """``aurora-init.sql`` with only the pgrouting extension line removed (the pgvector image has no pgrouting)."""
    lines = INIT_SQL.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not re.match(r"\s*CREATE EXTENSION IF NOT EXISTS pgrouting\b", ln)]
    assert len(kept) == len(lines) - 1
    return "\n".join(kept)


@pytest.fixture(scope="session")
def engine_dsn(pg_container: str) -> str:
    """Migrate the container as admin, then return the DSN of the ``coa_engine_rw`` role.

    Does what the aurora-migrator does: applies ``aurora-init.sql``, builds the HNSW index as the table owner
    (the rw roles are not owners), and sets each role's password. The tests then connect as ``coa_engine_rw``.
    """
    import asyncio

    import asyncpg

    async def migrate() -> None:
        conn = await asyncpg.connect(pg_container)
        try:
            await conn.execute(_init_sql())
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS search_doc_embedding_hnsw_{DIMENSIONS} ON search_doc "
                f"USING hnsw (CAST(embedding AS vector({DIMENSIONS})) vector_cosine_ops) WHERE embedding IS NOT NULL"
            )
            for role in ENGINE_ROLES:
                await conn.execute(
                    await conn.fetchval(
                        "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", role, ROLE_PASSWORD
                    )
                )
        finally:
            await conn.close()

    asyncio.run(migrate())
    parts = urlsplit(pg_container)
    netloc = f"coa_engine_rw:{ROLE_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="session")
def rds_fake(engine_dsn: str):
    """A ``FakeRdsDataClient`` connected as ``coa_engine_rw``, shared by the session."""
    from .fake_rds_data import FakeRdsDataClient

    fake = FakeRdsDataClient(engine_dsn)
    yield fake
    fake.close()


@pytest.fixture(scope="session")
def aurora_config():
    """A dummy config: the fake ignores ARNs."""
    from coa_common.aurora_data_api import AuroraConfig

    return AuroraConfig(
        resource_arn="arn:aws:rds:us-east-1:000000000000:cluster:fake",
        secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:fake",
        database="postgres",
        resume_timeout_seconds=1,
    )


@pytest.fixture(scope="session")
def _aurora_session_client(rds_fake, aurora_config):
    from coa_common.aurora_data_api import AuroraDataApiClient
    from coa_ontology.stores.aurora.schema import check_schema

    client = AuroraDataApiClient(aurora_config, rds_data=rds_fake)
    check_schema(client, DIMENSIONS)
    return client


# coa_engine_rw has no TRUNCATE privilege, so tests start from empty tables with DELETE.
_CLEAN = (
    "DELETE FROM coa_triple",
    "DELETE FROM ontology_engine",
    "DELETE FROM search_doc",
    "DELETE FROM search_term_stats",
    "DELETE FROM search_corpus_stats",
    "DELETE FROM entity_label",
)


@pytest.fixture
def aurora_client(_aurora_session_client, rds_fake):
    """A schema-ready ``AuroraDataApiClient`` as ``coa_engine_rw`` over empty tables (dimension 8 keeps data small)."""
    rds_fake.max_response_bytes = None
    for statement in _CLEAN:
        _aurora_session_client.execute(statement)
    return _aurora_session_client
