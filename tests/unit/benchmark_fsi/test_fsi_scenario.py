# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Scenario API, the build registry and the CLI, using a throwaway two-email scenario."""

from __future__ import annotations

import email
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.benchmark.fsi_commodity import build as build_mod
from tests.benchmark.fsi_commodity.calendars import CALENDARS, in_working_hours
from tests.benchmark.fsi_commodity.cli import main
from tests.benchmark.fsi_commodity.ontology import cls
from tests.benchmark.fsi_commodity.scenario import Attachment, ChatLine, Scenario
from tests.benchmark.fsi_commodity.writers.pdf_writer import Text, build_pdf

pytestmark = pytest.mark.unit

START = datetime(2026, 3, 6, 23, 50, tzinfo=UTC)  # Friday 17:50 in Houston: after close
TRADER_H = "{}"


def _addr(u: dict, org: str, role: str) -> str:
    return next(a for a, e in u["directory"].items() if e["org"] == org and e["role"] == role)


def demo_builder(universe: dict, upstream: dict) -> Scenario:
    sc = Scenario("t0-demo", "trade", universe, START, expect={"emails": 2, "attachments": 1, "chats": 1}, title="demo")
    ours = _addr(universe, "newco-hou", "trader")
    theirs = _addr(universe, "cp-vitol", "trader")
    pdf = build_pdf("Recap", [Text("250,000 bbl WTI")])
    sc.send(
        "m1",
        ours,
        theirs,
        "Recap attached.\n",
        subject="Recap WTI",
        doc_class=cls("Order"),
        attachments=[Attachment("recap.pdf", pdf, "recap", cls("Order"), {"quantity": 250000})],
        tags=["recap"],
        facts={"quantity": 250000},
    )
    sc.send("m2", theirs, ours, "Agreed.\n", reply_to="m1", after_business_days=1)
    sc.chat(
        "c1",
        [ChatLine(ours, "bid 0.85", "offer", {"premium": 0.85}), ChatLine(theirs, "done", "accept", {"premium": 0.85})],
    )
    return sc


REGISTRY = {"trade": (demo_builder,)}


@pytest.fixture(scope="module")
def result():
    return build_mod.build(registry=REGISTRY)


def test_send_threads_headers_and_ground_truth(result) -> None:
    sc = result.scenarios[0]
    m1, m2 = sc.messages
    assert m2["thread"] == "m1"
    assert m2["in_reply_to"] == "m1"
    assert m2["subject"] == "Re: Recap WTI"
    assert m1["direction"] == "outbound" and m2["direction"] == "inbound"
    assert m1["from"]["org"] == "newco-hou" and m1["from"]["role"] == "trader"
    assert m1["to"][0]["org"] == "cp-vitol"
    assert m1["provenance"]["wasAttributedTo"] == m1["from"]["address"]
    (att,) = sc.attachments
    assert att["sha256"] and att["provenance"]["wasDerivedFrom"] == m1["message_id"]
    eml = email.message_from_bytes(sc.files()["messages/002-m2.eml"])
    assert eml["In-Reply-To"] == m1["message_id"]
    assert eml["References"] == m1["message_id"]


def test_send_times_follow_sender_calendar_and_hours(result) -> None:
    sc = result.scenarios[0]
    stamps = [datetime.fromisoformat(m["timestamp"]) for m in sc.messages]
    assert stamps == sorted(stamps) and len(set(stamps)) == 2
    assert in_working_hours(CALENDARS["US-TX"], stamps[0])
    assert in_working_hours(CALENDARS["US-TX"], stamps[1])
    assert stamps[0].weekday() == 0  # Friday-evening start rolls to Monday morning in Houston
    assert stamps[1].weekday() == 1  # after_business_days=1


def test_chat_lines_carry_intent_and_terms(result) -> None:
    (chat,) = result.scenarios[0].chats
    assert [ln["intent"] for ln in chat["lines"]] == ["offer", "accept"]
    assert chat["lines"][1]["terms"] == {"premium": 0.85}
    assert chat["file"] == "chats/001-c1.json"


def test_duplicate_keys_and_unknown_mailboxes_fail(result) -> None:
    sc = build_mod.build(registry=REGISTRY).scenarios[0]
    with pytest.raises(ValueError):
        sc.send("m1", next(iter(sc.u["directory"])), next(iter(sc.u["directory"])), "x", subject="s")
    with pytest.raises(KeyError):
        sc.send("zz", "nobody@nowhere.example", next(iter(sc.u["directory"])), "x", subject="s")


def test_expected_counts_are_enforced() -> None:
    def wrong(universe: dict, upstream: dict) -> Scenario:
        return Scenario("t0-wrong", "trade", universe, START, expect={"emails": 1, "attachments": 0, "chats": 0})

    with pytest.raises(build_mod.BuildError):
        build_mod.build(registry={"trade": (wrong,)})


def test_build_twice_is_byte_identical(result) -> None:
    first = build_mod.render_files(result)
    second = build_mod.render_files(build_mod.build(registry=REGISTRY))
    assert first == second
    assert set(first) >= {
        "universe.json",
        "trade/t0-demo/manifest.json",
        "trade/t0-demo/messages/001-m1.eml",
        "trade/t0-demo/messages/001-m1.md",
        "trade/t0-demo/attachments/001-recap.pdf",
        "trade/t0-demo/chats/001-c1.json",
    }
    assert build_mod.render_files(result, only="t0-demo").keys() == {
        k for k in first if not k.startswith("structured/")
    }
    with pytest.raises(KeyError):
        build_mod.render_files(result, only="nope")


def test_different_seed_changes_output() -> None:
    a = build_mod.render_files(build_mod.build(1, REGISTRY))
    b = build_mod.render_files(build_mod.build(2, REGISTRY))
    assert a["universe.json"] != b["universe.json"]
    assert a["trade/t0-demo/messages/001-m1.eml"] != b["trade/t0-demo/messages/001-m1.eml"]


def test_every_email_has_a_markdown_copy(result) -> None:
    files = result.scenarios[0].files()
    emls = {p[:-4] for p in files if p.endswith(".eml")}
    mds = {p[:-3] for p in files if p.endswith(".md")}
    assert emls == mds and len(emls) == 2


def test_cli_writes_checks_and_detects_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_mod, "_registry", dict)  # empty registries keep this test about the CLI only
    out = tmp_path / "corpus"
    assert main(["--out", str(out)]) == 0  # empty registries: universe.json and empty structured tables
    assert (out / "universe.json").is_file()
    assert not list(out.glob("*/*/manifest.json"))
    assert main(["--check", "--out", str(out)]) == 0
    (out / "universe.json").write_bytes(b"{}\n")
    assert main(["--check", "--out", str(out)]) == 1
    assert "changed: universe.json" in capsys.readouterr().out
    assert main(["--scenario", "nope", "--out", str(out)]) == 2
    assert main(["--check", "--seed", "7", "--out", str(tmp_path / "missing")]) == 1
