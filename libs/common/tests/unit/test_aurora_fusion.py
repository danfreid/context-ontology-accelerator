# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for reciprocal rank fusion and search modes."""

from __future__ import annotations

from typing import Any

import pytest
from coa_common.aurora_search import index as index_mod
from coa_common.aurora_search.fusion import SearchMode, reciprocal_rank_fusion
from coa_common.aurora_search.index import AuroraSearchIndex, FuzzyHit, SearchHit

pytestmark = pytest.mark.unit


def test_rrf_arithmetic() -> None:
    fused = reciprocal_rank_fusion({"v": ["a", "b"], "k": ["b", "c"]})
    scores = {f.key: f.score for f in fused}
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 62)
    assert [f.key for f in fused] == ["b", "a", "c"]
    assert fused[0].ranks == {"v": 2, "k": 1}


def test_rrf_custom_k() -> None:
    assert reciprocal_rank_fusion({"v": ["a"]}, k=10)[0].score == pytest.approx(1 / 11)


def test_rrf_tie_break_by_key() -> None:
    fused = reciprocal_rank_fusion({"v": ["b"], "k": ["a"]})
    assert [f.key for f in fused] == ["a", "b"]
    assert fused[0].score == fused[1].score


def test_rrf_duplicates_count_once_at_best_rank() -> None:
    fused = reciprocal_rank_fusion({"v": ["a", "a", "b"]})
    scores = {f.key: f.score for f in fused}
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)


def test_rrf_empty_rankings() -> None:
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"v": [], "k": []}) == []
    assert [f.key for f in reciprocal_rank_fusion({"v": [], "k": ["a"]})] == ["a"]


def test_mode_values() -> None:
    assert SearchMode("hybrid") is SearchMode.HYBRID
    assert SearchMode.SEMANTIC == "semantic"
    assert SearchMode.LEXICAL == "lexical"


class StubIndex(AuroraSearchIndex):
    """Index whose three rankings are canned; records which were called."""

    def __init__(self) -> None:
        super().__init__(client=None, dimensions=3)  # type: ignore[arg-type]
        self.called: list[str] = []

    @staticmethod
    def h(key: str) -> SearchHit:
        return SearchHit(key=key, doc_id=f"doc-{key}", score=0.0, body=key)

    def vector_search(self, scope: str, vector: Any, top_k: int, filters: Any = None) -> list[SearchHit]:
        self.called.append("vector")
        return [self.h("a"), self.h("b")]

    def keyword_search(self, scope: str, query: str, top_k: int, **kw: Any) -> list[SearchHit]:
        self.called.append("keyword")
        return [self.h("b"), self.h("c")] if query else []

    def fuzzy_entities(self, scope: str, text: str, limit: int = 50, **kw: Any) -> list[FuzzyHit]:
        self.called.append("fuzzy")
        return [FuzzyHit("d", "Dee", 0.8), FuzzyHit("b", "Bee", 0.5)]

    def _docs_for_entities(self, scope: str, entity_ids: Any, filters: Any) -> list[SearchHit]:
        self.called.append("docs_for_entities")
        return [self.h(k) for k in entity_ids if k == "d"]


def test_semantic_uses_vector_only() -> None:
    idx = StubIndex()
    hits = idx.search("s", "q", vector=[1, 0, 0], mode=SearchMode.SEMANTIC)
    assert [h.key for h in hits] == ["a", "b"]
    assert idx.called == ["vector"]


def test_semantic_without_vector_is_empty() -> None:
    idx = StubIndex()
    assert idx.search("s", "q", mode=SearchMode.SEMANTIC) == []
    assert idx.called == []


def test_lexical_uses_bm25_only() -> None:
    idx = StubIndex()
    hits = idx.search("s", "q", vector=[1, 0, 0], mode=SearchMode.LEXICAL)
    assert [h.key for h in hits] == ["b", "c"]
    assert idx.called == ["keyword"]


def test_lexical_without_tokens_is_empty() -> None:
    assert StubIndex().search("s", "", mode=SearchMode.LEXICAL) == []


def test_hybrid_without_vector_degrades_to_lexical() -> None:
    idx = StubIndex()
    hits = idx.search("s", "q", mode=SearchMode.HYBRID)
    assert [h.key for h in hits] == ["b", "c"]
    assert idx.called == ["keyword"]


def test_hybrid_fuses_three_rankings() -> None:
    idx = StubIndex()
    hits = idx.search("s", "q", vector=[1, 0, 0], mode=SearchMode.HYBRID, top_k=10)
    # b: vector 2 + bm25 1 + trigram 2 ; a: vector 1 ; c: bm25 2 ; d: trigram 1 (resolved by entity lookup)
    assert [h.key for h in hits] == ["b", "a", "d", "c"]
    assert hits[0].ranks == {"vector": 2, "bm25": 1, "trigram": 2}
    assert hits[0].score == pytest.approx(1 / 62 + 1 / 61 + 1 / 62)
    assert "docs_for_entities" in idx.called


def test_hybrid_respects_top_k_and_skips_fuzzy_when_disabled() -> None:
    idx = StubIndex()
    hits = idx.search("s", "q", vector=[1, 0, 0], top_k=1, fuzzy_names=False)
    assert len(hits) == 1
    assert "fuzzy" not in idx.called


def test_hybrid_with_all_empty_rankings() -> None:
    class Empty(StubIndex):
        def vector_search(self, *a: Any, **k: Any) -> list[SearchHit]:
            return []

        def keyword_search(self, *a: Any, **k: Any) -> list[SearchHit]:
            return []

        def fuzzy_entities(self, *a: Any, **k: Any) -> list[FuzzyHit]:
            return []

    assert Empty().search("s", "", vector=[1, 0, 0]) == []


def test_module_exports_helpers() -> None:
    assert hasattr(index_mod, "AuroraSearchIndex")
