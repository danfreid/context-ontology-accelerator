# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for pure BM25 scoring."""

from __future__ import annotations

import math

import pytest
from coa_common.aurora_search.bm25 import Candidate, CorpusStats, bm25_score, idf, rank_bm25

pytestmark = pytest.mark.unit


def test_idf_known_answer() -> None:
    assert idf(10, 2) == pytest.approx(math.log(1 + 8.5 / 2.5))


def test_idf_non_negative_for_common_term() -> None:
    assert idf(10, 10) > 0


def test_prefix_df_is_one_value_per_term_across_disjoint_expansions() -> None:
    # "cop:*" expands to "copper" (df 2) in one document and "copperfield" (df 9) in another.
    doc_freqs = {"copper": 2, "copperfield": 9}
    a = Candidate("a", {"copper": 1}, 5)
    b = Candidate("b", {"copperfield": 1}, 5)
    stats = CorpusStats(20, 5.0, doc_freqs)
    scores = {c.key: s for c, s in rank_bm25([a, b], stats, ["cop:*"])}
    assert scores["a"] == pytest.approx(scores["b"])  # same df (max) for both candidates
    assert scores["a"] == pytest.approx(bm25_score(["cop:*"], a.tf, 5, 5.0, 20, doc_freqs))
    assert scores["a"] == pytest.approx(idf(20, 9) * 2.2 / 2.2)


def test_term_without_stats_row_contributes_zero() -> None:
    assert bm25_score(["ghost"], {"ghost": 3}, 5, 5.0, 10, {}) == 0.0
    assert bm25_score(["gho:*"], {"ghost": 3}, 5, 5.0, 10, {"other": 4}) == 0.0
    both = bm25_score(["ghost", "real"], {"ghost": 3, "real": 1}, 5, 5.0, 10, {"real": 2})
    assert both == pytest.approx(bm25_score(["real"], {"real": 1}, 5, 5.0, 10, {"real": 2}))


def test_single_term_known_answer() -> None:
    score = bm25_score(["a"], {"a": 1}, 10, 10.0, 10, {"a": 2})
    # doc_len == avg -> norm = k1; score = idf * (k1+1) / (1 + k1)
    assert score == pytest.approx(idf(10, 2) * 2.2 / 2.2)


def test_tf_saturates() -> None:
    def s(tf: int) -> float:
        return bm25_score(["a"], {"a": tf}, 10, 10.0, 10, {"a": 2})

    assert s(1) < s(2) < s(10) < s(100)
    assert s(100) < idf(10, 2) * (1.2 + 1)
    assert (s(2) - s(1)) > (s(100) - s(50))


def test_length_normalisation() -> None:
    def s(doc_len: int) -> float:
        return bm25_score(["a"], {"a": 2}, doc_len, 10.0, 10, {"a": 2})

    assert s(20) < s(10) < s(5)


def test_rare_term_outranks_common_term() -> None:
    freqs = {"rare": 1, "common": 9}
    rare = bm25_score(["rare"], {"rare": 1}, 10, 10.0, 10, freqs)
    common = bm25_score(["common"], {"common": 1}, 10, 10.0, 10, freqs)
    assert rare > common


def test_missing_term_scores_zero() -> None:
    assert bm25_score(["zzz"], {"a": 3}, 10, 10.0, 10, {"a": 2}) == 0.0


def test_prefix_expansion_sums_tf_and_takes_max_df() -> None:
    doc_tf = {"harbor": 2, "harbour": 1, "other": 5}
    freqs = {"harbor": 3, "harbour": 7, "other": 1}
    got = bm25_score(["harb:*"], doc_tf, 10, 10.0, 20, freqs)
    expected = bm25_score(["x"], {"x": 3}, 10, 10.0, 20, {"x": 7})
    assert got == pytest.approx(expected)
    star = bm25_score(["harb*"], doc_tf, 10, 10.0, 20, freqs)
    assert star == pytest.approx(expected)


def test_empty_corpus_returns_zero() -> None:
    assert bm25_score(["a"], {"a": 1}, 10, 10.0, 0, {"a": 0}) == 0.0
    assert bm25_score(["a"], {"a": 1}, 10, 0.0, 10, {"a": 1}) == 0.0
    assert bm25_score(["a"], {"a": 1}, 10, -1.0, 10, {"a": 1}) == 0.0


def test_rank_bm25_orders_and_breaks_ties_by_key() -> None:
    stats = CorpusStats(n_docs=10, avg_doc_len=10.0, doc_freqs={"a": 2})
    cands = [
        Candidate("c", {"a": 1}, 10),
        Candidate("b", {"a": 1}, 10),
        Candidate("z", {"a": 5}, 10),
        Candidate("none", {"q": 1}, 10),
    ]
    ranked = rank_bm25(cands, stats, ["a"])
    assert [c.key for c, _ in ranked] == ["z", "b", "c", "none"]
    assert ranked[-1][1] == 0.0


def test_rank_bm25_empty_corpus_is_deterministic() -> None:
    stats = CorpusStats(0, 0.0, {})
    ranked = rank_bm25([Candidate("b", {"a": 1}, 1), Candidate("a", {"a": 1}, 1)], stats, ["a"])
    assert [c.key for c, _ in ranked] == ["a", "b"]
    assert all(score == 0.0 for _, score in ranked)
