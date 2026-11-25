# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Query-text helpers: tokenizer, tsquery builders and tsvector parsing."""

from __future__ import annotations

import re

MAX_QUERY_TOKENS = 32
MAX_TOKEN_LEN = 64

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercased unicode alphanumeric tokens: at most 32, each 1-64 characters."""
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text or ""):
        token = match.group(0).lower()
        if 1 <= len(token) <= MAX_TOKEN_LEN:
            tokens.append(token)
            if len(tokens) >= MAX_QUERY_TOKENS:
                break
    return tokens


def build_or_prefix_tsquery(text: str) -> str:
    """Build ``'a':* | 'b':*`` for ``to_tsquery``.

    Tokens are Unicode word characters only, and each lexeme is single-quoted with quote and
    backslash escaped, so no operator can be injected.
    """
    parts: list[str] = []
    for token in tokenize(text):
        quoted = token.replace("\\", "\\\\").replace("'", "''")
        parts.append(f"'{quoted}':*")
    return " | ".join(parts)


def build_phrase_query(phrase: str) -> str:
    """Return the phrase to bind to ``phraseto_tsquery('english', :p)``; Postgres parses it safely."""
    return " ".join((phrase or "").split())


def parse_tsvector_text(text: str) -> dict[str, int]:
    """Parse ``tsvector::text`` (e.g. ``'foo':1,4 'it''s':2A``) to ``{lexeme: term frequency}``.

    The term frequency is the number of positions, at least 1.
    """
    result: dict[str, int] = {}
    i = 0
    n = len(text or "")
    while i < n:
        if text[i] != "'":
            i += 1
            continue
        i += 1
        lexeme: list[str] = []
        while i < n:
            ch = text[i]
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    lexeme.append("'")
                    i += 2
                    continue
                i += 1
                break
            lexeme.append(ch)
            i += 1
        positions = 0
        if i < n and text[i] == ":":
            i += 1
            start = i
            while i < n and text[i] != " ":
                i += 1
            positions = len([p for p in text[start:i].split(",") if p])
        word = "".join(lexeme)
        result[word] = result.get(word, 0) + max(1, positions)
    return result
