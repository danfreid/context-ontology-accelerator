# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Aurora SPARQL client — GraphClient implementation for ``GRAPH_BACKEND=aurora``.

Counterpart of :class:`~coa_serve.clients.neptune.NeptuneGraphClient`. It answers the same
read-only SPARQL through :class:`coa_common.aurora_sparql.AuroraSparql` (rdflib over the
``coa_triple`` table), makes no HTTP call, and returns the same shapes. The blocking Data API
work runs in a thread executor, like the Aurora vector client.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from coa_common.aurora_sparql import DEFAULT_MAX_RESULTS, AuroraSparql, shared_sparql

from .base import instrumented
from .neptune import _SPARQL_DATA_TOKEN, _SPARQL_WRITE_PATTERN, SPARQLValidationError

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aurora-sparql")


class AuroraSparqlGraphClient:
    """Read-only SPARQL over Aurora with the ``NeptuneGraphClient`` surface."""

    def __init__(self, sparql: AuroraSparql | None = None, *, enforce_read_only: bool = True) -> None:
        """Bind an :class:`AuroraSparql` (the process-wide one by default)."""
        self._sparql = sparql or shared_sparql()
        self._enforce_read_only = enforce_read_only

    def _validate(self, sparql: str) -> None:
        if self._enforce_read_only and _SPARQL_WRITE_PATTERN.search(_SPARQL_DATA_TOKEN.sub(" ", sparql)):
            raise SPARQLValidationError("Only read-only SPARQL queries are allowed (SELECT, ASK, DESCRIBE, CONSTRUCT)")

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))

    @instrumented("aurora_graph")
    async def query(self, sparql: str, *, max_results: int = DEFAULT_MAX_RESULTS) -> list[dict[str, Any]]:
        """Execute a read-only SELECT; flat ``{var: value}`` dicts (a LIMIT is appended if none)."""
        self._validate(sparql)
        return await self._run(self._sparql.query, sparql, max_results=max_results)

    @instrumented("aurora_graph")
    async def ask(self, sparql: str) -> bool:
        """Execute a read-only ASK."""
        self._validate(sparql)
        return await self._run(self._sparql.ask, sparql)

    @instrumented("aurora_graph")
    async def construct(self, sparql: str, *, max_triples: int = DEFAULT_MAX_RESULTS) -> list[dict[str, str]]:
        """Execute a CONSTRUCT or DESCRIBE; ``{subject, predicate, object}`` dicts, at most ``max_triples``."""
        self._validate(sparql)
        return await self._run(self._sparql.construct, sparql, max_triples=max_triples)

    async def health_check(self) -> dict[str, Any]:
        """Probe the Data API; returns a status dict, never raises."""
        try:
            return await self._run(self._sparql.health_check)
        except Exception as e:  # noqa: BLE001 - health checks report, never raise
            return {"status": "error", "detail": str(e)}

    async def close(self) -> None:
        """Nothing is pooled per client; the shared Data API client is process-wide."""
