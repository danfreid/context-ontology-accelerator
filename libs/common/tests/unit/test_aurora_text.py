# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Aurora search text helpers."""

from __future__ import annotations

import pytest
from coa_common.aurora_search.text import build_or_prefix_tsquery, build_phrase_query, parse_tsvector_text, tokenize

pytestmark = pytest.mark.unit


def test_tokenize_lowercases_and_splits() -> None:
    assert tokenize("Glencore PLC, Q3-2024 Report!") == ["glencore", "plc", "q3", "2024", "report"]


def test_tokenize_caps_tokens_and_length() -> None:
    assert len(tokenize(" ".join(f"w{i}" for i in range(100)))) == 32
    assert tokenize("a" * 65 + " ok") == ["ok"]
    assert tokenize("a" * 64) == ["a" * 64]


def test_tokenize_empty() -> None:
    assert tokenize("") == []
    assert tokenize("  !!! ... ") == []


def test_prefix_tsquery_or_joined() -> None:
    assert build_or_prefix_tsquery("harb copper") == "harb:* | copper:*"


def test_tsquery_strips_operators() -> None:
    q = build_or_prefix_tsquery("a & b | !c ' ; :* ( ) <-> d\\")
    assert q == "a:* | b:* | c:* | d:*"
    assert set(q) <= set("abcd:* |")


def test_tsquery_empty_input() -> None:
    assert build_or_prefix_tsquery("") == ""
    assert build_or_prefix_tsquery("&|!") == ""


def test_tsquery_unicode_tokens_keep_only_safe_chars() -> None:
    assert build_or_prefix_tsquery("café Zürich") == "caf:* | zrich:*"
    assert build_or_prefix_tsquery("日本") == ""
    assert tokenize("café Zürich") == ["café", "zürich"]


def test_tsquery_token_cap() -> None:
    q = build_or_prefix_tsquery(" ".join(f"w{i}" for i in range(50)))
    assert q.count(":*") == 32


def test_phrase_query_collapses_whitespace_only() -> None:
    assert build_phrase_query("  master   netting\nagreement ") == "master netting agreement"
    assert build_phrase_query("it's; DROP") == "it's; DROP"


def test_parse_tsvector_positions() -> None:
    assert parse_tsvector_text("'copper':1,5,9 'trade':3") == {"copper": 3, "trade": 1}


def test_parse_tsvector_weights_and_no_positions() -> None:
    assert parse_tsvector_text("'alpha':1A,2B 'beta' 'gamma':4C") == {"alpha": 2, "beta": 1, "gamma": 1}


def test_parse_tsvector_quoted_lexemes() -> None:
    assert parse_tsvector_text("'it''s':2 'a b':1,3 'x:y':4") == {"it's": 1, "a b": 2, "x:y": 1}


def test_parse_tsvector_empty() -> None:
    assert parse_tsvector_text("") == {}
