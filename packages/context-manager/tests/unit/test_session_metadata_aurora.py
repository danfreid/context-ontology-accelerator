# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Session metadata with ``METADATA_BACKEND=aurora`` over the in-memory Aurora metadata store."""

from __future__ import annotations

import pytest
from coa_common.testing_aurora import install
from coa_serve.session_metadata import SessionMetadataStore

pytestmark = pytest.mark.unit


@pytest.fixture
def sessions(monkeypatch):
    install(monkeypatch)
    return SessionMetadataStore("coa-dev-session-metadata", region="us-east-1")


async def test_create_list_update_and_delete(sessions):
    a = await sessions.create("u1", namespace_id="ns1", session_id="s-a", title="first")
    b = await sessions.create("u1", namespace_id="ns2", session_id="s-b")
    await sessions.create("u2", namespace_id="ns1", session_id="s-c")

    assert await sessions.validate_ownership("u1", a.session_id)
    assert not await sessions.validate_ownership("u2", a.session_id)
    assert await sessions.get_session_namespace("u1", b.session_id) == "ns2"

    await sessions.update_activity("u1", "s-a", "ns1")
    listed, token = await sessions.list_sessions("u1")
    assert token is None
    assert {s.session_id for s in listed} == {"s-a", "s-b"}
    assert next(s for s in listed if s.session_id == "s-a").message_count == 1

    in_ns, _ = await sessions.list_for_namespace("u1", "ns1")
    assert [s.session_id for s in in_ns] == ["s-a"]
    recent = await sessions.get_recent_for_namespace("u1", "ns1")
    assert recent is not None and recent.session_id == "s-a"

    await sessions.update_title("u1", "s-b", "named")
    await sessions.update_title("u1", "s-b", "ignored")  # first title wins
    assert (await sessions.list_for_namespace("u1", "ns2"))[0][0].title == "named"

    await sessions.delete("u1", "s-a")
    assert not await sessions.validate_ownership("u1", "s-a")


async def test_pagination_token_round_trips(sessions):
    for i in range(3):
        await sessions.create("u1", session_id=f"s-{i}")
    first, token = await sessions.list_sessions("u1", limit=2)
    assert len(first) == 2 and token
    rest, token2 = await sessions.list_sessions("u1", limit=2, page_token=token)
    assert len(rest) == 1 and token2 is None
    assert {s.session_id for s in first + rest} == {"s-0", "s-1", "s-2"}
