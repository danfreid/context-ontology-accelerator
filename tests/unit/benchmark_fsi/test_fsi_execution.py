# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Execution phase: Bloomberg shorthand read per trader pair, and ETRM bookings checked against the confirmations.

The checks read the built chat JSON, the RightAngle-style CSV exports and the confirmation PDFs, and recompute the
interpretation confidence and every break from the manifest alone.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from decimal import Decimal
from typing import Any

import pypdfium2 as pdfium
import pytest

from tests.benchmark.fsi_commodity import build as build_mod
from tests.benchmark.fsi_commodity.execution.common import ETRM_COLUMNS, THRESHOLD, breaks, confidence
from tests.benchmark.fsi_commodity.execution.e1_desk_chats_etrm import CURRENT, PLANTED, SCENARIO_ID
from tests.benchmark.fsi_commodity.golden import build_golden
from tests.benchmark.fsi_commodity.scenario import Scenario

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def result() -> build_mod.BuildResult:
    return build_mod.build()


@pytest.fixture(scope="module")
def sc(result: build_mod.BuildResult) -> Scenario:
    return next(s for s in result.scenarios if s.phase == "execution")


@pytest.fixture(scope="module")
def files(sc: Scenario) -> dict[str, bytes]:
    return sc.files()


@pytest.fixture(scope="module")
def ex(sc: Scenario) -> dict[str, Any]:
    return json.loads(sc.files()["manifest.json"])["execution"]


def _answer(sc: Scenario, n: int) -> Any:
    return next(q["expected"] for q in sc.questions if q["id"] == f"{SCENARIO_ID}-q{n}")


def _exports(sc: Scenario, files: dict[str, bytes]) -> list[tuple[str, list[dict[str, str]]]]:
    out = []
    for a in sc.attachments:
        if a["kind"] == "etrm-export":
            out.append((a["message"], list(csv.DictReader(io.StringIO(files[a["path"]].decode("utf-8"))))))
    return out


# --- phase ----------------------------------------------------------------------------------------------
def test_phase_counts_and_byte_identical_rebuild(sc: Scenario, files: dict[str, bytes]) -> None:
    assert build_mod.PHASES.index("execution") == build_mod.PHASES.index("lifecycle") + 1
    assert tuple(sc.counts[k] for k in ("emails", "attachments", "chats")) == build_mod.EXPECTED_TOTALS["execution"]
    assert next(s for s in build_mod.build().scenarios if s.phase == "execution").files() == files


# --- chats ----------------------------------------------------------------------------------------------
def test_chats_are_bloomberg_shorthand_whose_terms_hold_the_meaning(sc: Scenario, files: dict[str, bytes]) -> None:
    assert all(c["channel"] == "bloomberg" for c in sc.chats)
    for c in sc.chats:
        on_disk = json.loads(files[c["file"]])
        enquiry, offer, _, accept = on_disk["lines"]
        assert enquiry["terms"]["product"] not in enquiry["text"] or enquiry["terms"]["product"] == "Mars blend"
        assert "k " in enquiry["text"] and accept["intent"] == "accept" and accept["terms"]["quantity"] % 1000 == 0
        assert offer["text"].startswith(("+", "flat "))


def test_the_same_token_means_different_things_for_different_pairs(ex: dict[str, Any]) -> None:
    k = {dl["pair"]: dl["k_unit"] for dl in ex["dialects"]}
    scale = {dl["pair"]: dl["price_scale"] for dl in ex["dialects"]}
    assert k["hou-vitol"] == "bbl" and k["lon-bp"] == "mt"
    assert scale["hou-vitol"] == 0.01 and scale["lon-bp"] == 1.0
    lon = next(c for c in ex["interpretations"] if c["trade_id"] == "NC-LON-26-0312")
    assert lon["terms"]["premium"] == 12.0 and lon["terms"]["unit"] == "mt"


def test_confidence_recomputes_from_confirmed_history(ex: dict[str, Any]) -> None:
    history = [h for h in ex["history"] if h["status"] == "confirmed"]
    for interp in ex["interpretations"]:
        for tok in interp["tokens"]:
            again = confidence(tok["token"], tok["meaning"], interp["pair"], history)
            assert {k: tok[k] for k in again} == again
            for chat in tok["evidence"]:
                assert next(h for h in history if h["chat"] == chat)["trade_id"]
        assert interp["decision"] == (
            "auto" if all(t["confidence"] >= THRESHOLD for t in interp["tokens"]) else "review"
        )


def test_pair_history_beats_a_desk_wide_majority(ex: dict[str, Any]) -> None:
    history = ex["history"]
    majority = Counter(h["tokens"]["price:+n"] for h in history if "price:+n" in h["tokens"]).most_common(1)[0][0]
    assert majority == 0.01  # most desk history is crude, priced in cents
    lon = next(c for c in ex["interpretations"] if c["trade_id"] == "NC-LON-26-0312")
    naive = 12 * majority
    booked = next(r for r in ex["etrm_rows"] if r["ExternalRef"] == "NC-LON-26-0312" and r["Version"] == 1)
    assert Decimal(booked["Premium"]) == Decimal(str(naive))  # the open break is exactly the desk-wide misreading
    tok = next(t for t in lon["tokens"] if t["token"] == "price:+n")
    assert tok["meaning"] == 1.0 and tok["basis"] == "pair" and tok["decision"] == "auto"


def test_a_new_trader_pair_goes_to_review(ex: dict[str, Any], sc: Scenario) -> None:
    gln = next(c for c in ex["interpretations"] if c["pair"] == "hou-glencore")
    assert gln["decision"] == "review"
    assert {t["token"]: t["basis"] for t in gln["tokens"]}["qty:k"] == "conflict"
    assert all(t["basis"] != "pair" for t in gln["tokens"])
    assert "c06" in _answer(sc, 7)


# --- ETRM against confirmations -------------------------------------------------------------------------
def test_exports_parse_and_match_the_manifest_rows(sc: Scenario, files: dict[str, bytes], ex: dict[str, Any]) -> None:
    parsed = [r for _, rows in _exports(sc, files) for r in rows]
    assert len(parsed) == len(ex["etrm_rows"]) == 12 + 6 + 3
    for got, want in zip(parsed, ex["etrm_rows"], strict=True):
        assert list(got) == list(ETRM_COLUMNS)
        assert got == {k: str(want[k]) for k in ETRM_COLUMNS}


def test_confirmation_pdfs_state_the_binding_side_and_terms(
    sc: Scenario, files: dict[str, bytes], ex: dict[str, Any]
) -> None:
    for a in (x for x in sc.attachments if x["kind"] == "trade-confirmation"):
        pdf = pdfium.PdfDocument(files[a["path"]])
        text = " ".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
        conf = ex["confirmations"][a["facts"]["trade_id"]]
        buyer = a["facts"]["buyer"]
        assert ("buy" if buyer.startswith("newco-") else "sell") == conf["side"]
        assert conf["product"] in text and f"{conf['quantity']:,} {conf['unit']}" in text


def test_breaks_recompute_and_match_the_planted_errors(ex: dict[str, Any]) -> None:
    v1 = [r for r in ex["etrm_rows"] if r["Version"] == 1 and r["DealStatus"] == "ENTERED"]
    found = {r["ExternalRef"]: breaks(ex["confirmations"][r["ExternalRef"]], r, ex["product_codes"]) for r in v1}
    found = {k: v for k, v in found.items() if v}
    assert {b["trade_id"]: b["fields"] for b in ex["breaks"]} == found
    assert set(found) == set(PLANTED)
    assert [f["field"] for f in found["NC-HOU-26-0312"]] == ["side"]
    assert found["NC-LON-26-0311"] == [{"field": "unit", "confirmation": "mt", "etrm": "bbl"}]
    assert len([d for d in CURRENT if d.trade_id not in PLANTED]) == 2  # two clean bookings


def test_remediation_amends_caught_breaks_and_leaves_one_open(sc: Scenario, ex: dict[str, Any]) -> None:
    rows = ex["etrm_rows"]
    for b in ex["breaks"]:
        versions = [r for r in rows if r["ExternalRef"] == b["trade_id"]]
        if b["caught"]:
            assert [r["Version"] for r in versions] == [1, 2] and versions[-1]["DealStatus"] == "AMENDED"
            assert not breaks(ex["confirmations"][b["trade_id"]], versions[-1], ex["product_codes"])
            assert b["status"] == "remediated"
        else:
            assert len(versions) == 1 and b["status"] == "open"
    assert [b["trade_id"] for b in ex["breaks"] if b["status"] == "open"] == _answer(sc, 2) == ["NC-LON-26-0312"]
    caught = {c["id"].split(":", 1)[1]: c["caught"] for c in sc.expected_contradictions}
    assert caught == {f"etrm-{f}-{tid}": c for tid, (_, c, f) in PLANTED.items()}
    flag = next(m for m in sc.messages if m["key"] == "breaks")
    assert flag["direction"] == "internal" and "binding record" in sc.body("breaks")


def test_questions_reach_the_golden_answers(result: build_mod.BuildResult, sc: Scenario) -> None:
    golden = build_golden(result)
    assert {f"{SCENARIO_ID}-q{i}" for i in range(1, 10)} <= set(golden)
    assert _answer(sc, 1) == sorted(PLANTED)
    assert _answer(sc, 4) == "mt" and _answer(sc, 5) == 0.85 and _answer(sc, 8) == 2
