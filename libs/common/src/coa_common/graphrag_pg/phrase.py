# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exact-phrase search over the raw chunk text of the Aurora document graph.

The chunk text lives in ``<schema>.node.value`` for ``label = '__Chunk__'``. Latin-script phrases use
``phraseto_tsquery('simple', ...)`` against ``to_tsvector('simple', value)`` (a partial GIN index serves
it), which matches the OpenSearch ``match_phrase`` semantics: tokens in order and adjacent, case
folded, no stemming. The ``simple`` configuration has no word segmenter for Chinese, Japanese, Thai or
Korean, so a phrase containing such a character is matched with a trigram-backed ``ILIKE`` instead.

Returns the same row shape as ``OpenSearchVectorClient.phrase_search``: ``chunk_id``, ``source_id``,
``source_s3_key``, ``text``, ``score`` and ``matched`` (indexes into the caller's ``phrases``).
"""

from __future__ import annotations

import json
import re
from typing import Any

from coa_common.graphrag_pg.schema import _SCHEMA_RE

MAX_PHRASE_SIZE = 100

# Scripts the ``simple`` text-search configuration cannot split into words.
_UNSEGMENTED = re.compile(
    "[\u0e00-\u0e7f\u1100-\u11ff\u2e80-\u2fdf\u3000-\u303f\u3040-\u30ff\u3100-\u312f\u3130-\u318f\u31f0-\u31ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]"
)

# ``{schema}`` is validated; phrases, tenant and size are bound. ``:phrases`` is a JSON list of
# ``{"idx": int, "ph": str, "cjk": bool}``. ILIKE metacharacters in a phrase are escaped.
_PHRASE_SQL = """
WITH p AS (
  SELECT x.idx, x.ph, x.cjk
  FROM jsonb_to_recordset(CAST(:phrases AS jsonb)) AS x(idx int, ph text, cjk boolean)
),
hit AS (
  SELECT n.id, n.value, p.idx,
         ts_rank(to_tsvector('simple', n.value), phraseto_tsquery('simple', p.ph)) AS rank
  FROM p
  JOIN {schema}.node n ON n.label = '__Chunk__' AND n.tenant = :tenant
   AND to_tsvector('simple', n.value) @@ phraseto_tsquery('simple', p.ph)
  WHERE NOT p.cjk
  UNION ALL
  SELECT n.id, n.value, p.idx, 1.0 AS rank
  FROM p
  JOIN {schema}.node n ON n.label = '__Chunk__' AND n.tenant = :tenant
   AND n.value ILIKE '%' || replace(replace(replace(p.ph, '!', '!!'), '%', '!%'), '_', '!_') || '%' ESCAPE '!'
  WHERE p.cjk
),
agg AS (
  SELECT id, value, sum(rank) AS score, jsonb_agg(idx ORDER BY idx) AS matched
  FROM hit GROUP BY id, value
)
SELECT a.id AS chunk_id, s.id AS source_id, s.props->>'source_s3_key' AS source_s3_key,
       a.value AS text, a.score AS score, a.matched AS matched
FROM agg a
LEFT JOIN {schema}.edge ef ON ef.type = '__EXTRACTED_FROM__' AND ef.src = a.id
LEFT JOIN {schema}.node s ON s.id = ef.dst AND s.label = '__Source__' AND s.tenant = :tenant
ORDER BY a.score DESC, a.id
LIMIT {size}
"""


def has_unsegmented_script(phrase: str) -> bool:
    """True when ``phrase`` contains a character the ``simple`` text-search config cannot tokenize."""
    return bool(_UNSEGMENTED.search(phrase))


def phrase_search(client: Any, schema: str, tenant: str, phrases: list[str], size: int = 50) -> list[dict[str, Any]]:
    """Find chunks of ``tenant`` containing any of ``phrases``; each hit lists the phrase indexes it matched.

    Args:
        client: An object with ``execute(sql, params) -> list[dict]`` (``AuroraDataApiClient``).
        schema: Aurora schema holding the node/edge tables.
        tenant: The toolkit tenant suffix of the chunk labels (``''`` for the default tenant).
        phrases: Phrases to find; blank ones are skipped but keep their index.
        size: Maximum hits (clamped to ``[1, MAX_PHRASE_SIZE]``).
    """
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name {schema!r}")
    wanted = [
        {"idx": i, "ph": " ".join(p.split()), "cjk": has_unsegmented_script(p)}
        for i, p in enumerate(phrases)
        if p and p.strip()
    ]
    if not wanted:
        return []
    limit = max(1, min(int(size), MAX_PHRASE_SIZE))
    sql = _PHRASE_SQL.format(schema=schema, size=limit)
    rows = client.execute(sql, {"phrases": wanted, "tenant": tenant})
    out: list[dict[str, Any]] = []
    for row in rows:
        matched = row.get("matched")
        if isinstance(matched, str):
            matched = json.loads(matched)
        out.append(
            {
                "chunk_id": row.get("chunk_id") or "",
                "source_id": row.get("source_id") or "",
                "source_s3_key": row.get("source_s3_key") or "",
                "text": row.get("text") or "",
                "score": float(row.get("score") or 0.0),
                "matched": [int(i) for i in (matched or [])],
            }
        )
    return out
