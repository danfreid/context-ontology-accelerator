# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Okapi BM25 over a recalled candidate set, using corpus statistics from Postgres."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_K1 = 1.2
DEFAULT_B = 0.75


def idf(n_docs: int, doc_freq: int) -> float:
    """Non-negative Lucene IDF: ``ln(1 + (N - df + 0.5) / (df + 0.5))``."""
    return math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def _match(term: str, doc_tf: Mapping[str, int], doc_freqs: Mapping[str, int]) -> tuple[int, int]:
    """Return (tf, df) for a query term. A term ending in ``:*`` or ``*`` is a prefix."""
    if term.endswith(":*") or term.endswith("*"):
        prefix = term[:-2] if term.endswith(":*") else term[:-1]
        tf = 0
        df = 0
        for lexeme, count in doc_tf.items():
            if lexeme.startswith(prefix):
                tf += count
                df = max(df, doc_freqs.get(lexeme, 0))
        return tf, df
    return doc_tf.get(term, 0), doc_freqs.get(term, 0)


def bm25_score(
    query_terms: Sequence[str],
    doc_tf: Mapping[str, int],
    doc_len: int,
    avg_doc_len: float,
    n_docs: int,
    doc_freqs: Mapping[str, int],
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
) -> float:
    """BM25 score of one document. Returns 0.0 for an empty corpus or a non-positive average length."""
    if avg_doc_len <= 0 or n_docs <= 0:
        return 0.0
    score = 0.0
    norm = k1 * (1.0 - b + b * (doc_len / avg_doc_len))
    for term in query_terms:
        tf, df = _match(term, doc_tf, doc_freqs)
        if tf <= 0:
            continue
        score += idf(n_docs, df) * (tf * (k1 + 1.0)) / (tf + norm)
    return score


@dataclass(frozen=True)
class Candidate:
    """A recalled document with its term frequencies."""

    key: str
    tf: Mapping[str, int]
    doc_len: int
    payload: object = field(default=None, compare=False)


@dataclass(frozen=True)
class CorpusStats:
    """Corpus statistics for one namespace."""

    n_docs: int
    avg_doc_len: float
    doc_freqs: Mapping[str, int]


def rank_bm25(
    candidates: Sequence[Candidate],
    stats: CorpusStats,
    query_terms: Sequence[str],
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
) -> list[tuple[Candidate, float]]:
    """Score candidates and sort by score descending, then key ascending."""
    scored = [
        (
            c,
            bm25_score(query_terms, c.tf, c.doc_len, stats.avg_doc_len, stats.n_docs, stats.doc_freqs, k1, b),
        )
        for c in candidates
    ]
    scored.sort(key=lambda item: (-item[1], item[0].key))
    return scored
