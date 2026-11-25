# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reciprocal rank fusion and search modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

RRF_K = 60


class SearchMode(StrEnum):
    """How a query is ranked.

    SEMANTIC: vector ranking only. LEXICAL: BM25 ranking only. HYBRID: RRF over the
    vector, BM25 and trigram rankings (an empty ranking contributes nothing).
    HYBRID without a query vector degrades to LEXICAL.
    """

    HYBRID = "hybrid"
    SEMANTIC = "semantic"
    LEXICAL = "lexical"


@dataclass(frozen=True)
class FusedHit:
    """One fused result: key, RRF score and the 1-based rank in each contributing ranking."""

    key: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)


def reciprocal_rank_fusion(rankings: dict[str, list[str]], k: int = RRF_K) -> list[FusedHit]:
    """Fuse ranked key lists: score = sum of ``1 / (k + rank)``, rank starting at 1.

    A key repeated inside one ranking counts once, at its best rank. Ties break by key.
    """
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name, keys in rankings.items():
        seen: set[str] = set()
        rank = 0
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(key, {})[name] = rank
    return [FusedHit(key, scores[key], ranks[key]) for key in sorted(scores, key=lambda x: (-scores[x], x))]
