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


def _prefix_of(term: str) -> str | None:
    """The prefix of a ``x:*`` / ``x*`` query term, or None for an exact term."""
    if term.endswith(":*"):
        return term[:-2]
    if term.endswith("*"):
        return term[:-1]
    return None


def term_doc_freqs(query_terms: Sequence[str], doc_freqs: Mapping[str, int]) -> dict[str, int]:
    """Document frequency per query term. A prefix term takes the max over its expansions in ``doc_freqs``.

    A term with no row in ``doc_freqs`` gets 0 and contributes no weight.
    """
    out: dict[str, int] = {}
    for term in query_terms:
        prefix = _prefix_of(term)
        if prefix is None:
            out[term] = doc_freqs.get(term, 0)
        else:
            out[term] = max((df for lexeme, df in doc_freqs.items() if lexeme.startswith(prefix)), default=0)
    return out


def _tf(term: str, doc_tf: Mapping[str, int]) -> int:
    prefix = _prefix_of(term)
    if prefix is None:
        return doc_tf.get(term, 0)
    return sum(count for lexeme, count in doc_tf.items() if lexeme.startswith(prefix))


def bm25_score(
    query_terms: Sequence[str],
    doc_tf: Mapping[str, int],
    doc_len: int,
    avg_doc_len: float,
    n_docs: int,
    doc_freqs: Mapping[str, int],
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
    term_dfs: Mapping[str, int] | None = None,
) -> float:
    """BM25 score of one document. Returns 0.0 for an empty corpus or a non-positive average length.

    ``term_dfs`` is the per-query-term document frequency from ``term_doc_freqs``; it is computed
    from ``doc_freqs`` when omitted. A term with no statistics row adds nothing.
    """
    if avg_doc_len <= 0 or n_docs <= 0:
        return 0.0
    dfs = term_dfs if term_dfs is not None else term_doc_freqs(query_terms, doc_freqs)
    score = 0.0
    norm = k1 * (1.0 - b + b * (doc_len / avg_doc_len))
    for term in query_terms:
        tf = _tf(term, doc_tf)
        df = dfs.get(term, 0)
        if tf <= 0 or df <= 0:
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
    dfs = term_doc_freqs(query_terms, stats.doc_freqs)
    scored = [
        (
            c,
            bm25_score(query_terms, c.tf, c.doc_len, stats.avg_doc_len, stats.n_docs, stats.doc_freqs, k1, b, dfs),
        )
        for c in candidates
    ]
    scored.sort(key=lambda item: (-item[1], item[0].key))
    return scored
