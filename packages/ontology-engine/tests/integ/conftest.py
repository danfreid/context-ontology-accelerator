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
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

IMAGE = os.getenv("COA_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")

# The same DDL infra/lib/stacks/foundation/migrations/aurora-init.sql creates for the DynamoDB mirror.
INFRA_ONTOLOGY_ENGINE_DDL = """
CREATE TABLE IF NOT EXISTS ontology_engine (
    pk         TEXT  NOT NULL,
    sk         TEXT  NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (pk, sk)
)
"""


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


@pytest.fixture(scope="session")
def rds_fake(pg_container: str):
    """A ``FakeRdsDataClient`` on the container, shared by the session."""
    from .fake_rds_data import FakeRdsDataClient

    fake = FakeRdsDataClient(pg_container)
    # Created by infra's aurora-init.sql in a real cluster; the container only has extensions we create ourselves.
    fake.execute_statement(sql=INFRA_ONTOLOGY_ENGINE_DDL, parameters=[])
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
    from coa_common.aurora_data_api import DataApiClient
    from coa_ontology.stores.aurora.schema import ensure_schema

    client = DataApiClient(aurora_config, rds_data=rds_fake)
    ensure_schema(client, 8)
    return client


@pytest.fixture
def aurora_client(_aurora_session_client, rds_fake):
    """A schema-ready ``DataApiClient`` over empty tables (vector dimension 8 keeps the data small)."""
    rds_fake.max_response_bytes = None
    _aurora_session_client.execute("TRUNCATE coa_triple, coa_embedding, ontology_engine RESTART IDENTITY")
    return _aurora_session_client
