# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trade phase (t1..t6): counts, planted problems, links to onboarding, figures recomputed from inputs, class URIs."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from tests.benchmark.fsi_commodity.build import BuildResult, build, render_files
from tests.benchmark.fsi_commodity.calendars import CALENDARS, in_working_hours
from tests.benchmark.fsi_commodity.trade import TRADE
from tests.benchmark.fsi_commodity.trade.common import credit_check, latest_ticket, onboarded, traded

pytestmark = pytest.mark.unit

PACK = Path(__file__).resolve().parents[3] / "packages" / "ontology-engine" / "demo" / "config-fsi-commodity.yaml"

COUNTS = {
    "t1-houston-vitol-wti": (7, 3, 3),
    "t2-london-kestrel-vlsfo": (7, 3, 2),
    "t3-sydney-harbourline-ulp": (8, 3, 3),
    "t4-newyork-glencore-isda": (7, 3, 2),
    "t5-sydney-bp-jet": (7, 3, 2),
    "t6-london-northgate-gbp-fwd": (6, 3, 1),
}
ONBOARDING_OF = {
    "t1-houston-vitol-wti": "s1-houston-vitol-wti",
    "t2-london-kestrel-vlsfo": "s2-london-kestrel-vlsfo",
    "t3-sydney-harbourline-ulp": "s3-sydney-harbourline-ulp",
    "t4-newyork-glencore-isda": "s4-newyork-glencore-isda",
    "t5-sydney-bp-jet": "s5-sydney-bp-jet",
}
# scenario -> {mismatch id: caught}
PLANTED = {
    "t1-houston-vitol-wti": {"recap-differs-from-chat": False},
    "t2-london-kestrel-vlsfo": {"wrong-currency-requested": True},
    "t3-sydney-harbourline-ulp": {},
    "t4-newyork-glencore-isda": {"ticket-transposed-digits": False},
    "t5-sydney-bp-jet": {"wrong-incoterm": False},
    "t6-london-northgate-gbp-fwd": {"wrong-value-date": True},
}


@pytest.fixture(scope="module")
def result() -> BuildResult:
    return build()


@pytest.fixture(scope="module")
def sc(result: BuildResult) -> dict[str, Any]:
    return {s.id: s for s in result.scenarios}


@pytest.fixture(scope="module")
def onb(result: BuildResult, sc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return onboarded(result.universe, sc)


@pytest.fixture(scope="module")
def deals(sc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["trade_id"]: d for sid in COUNTS for d in sc[sid].deals}


def _pack_uris() -> set[str]:
    """Every class_uri under sample_classes, read straight from the pack."""
    with PACK.open(encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    uris: set[str] = set()
    for group in ("foundational_ontologies", "domain_ontologies", "customer_ontologies"):
        for onto in pack.get(group) or []:
            uris.update(c["class_uri"] for c in onto.get("sample_classes") or [])
    return uris


def _uris(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node] if node.startswith("http") else []
    if isinstance(node, dict):
        return [u for v in node.values() for u in _uris(v)]
    if isinstance(node, list):
        return [u for v in node for u in _uris(v)]
    return []


def _attachment(s: Any, filename: str) -> dict[str, Any]:
    return next(a for a in s.attachments if a["filename"] == filename)


def _message(s: Any, key: str) -> dict[str, Any]:
    return next(m for m in s.messages if m["key"] == key)


def _chat(s: Any, key: str) -> dict[str, Any]:
    return next(c for c in s.chats if c["key"] == key)


# --- counts -------------------------------------------------------------------------------------


def test_registry_has_six_scenarios() -> None:
    assert len(TRADE) == 6


@pytest.mark.parametrize(("sid", "expected"), sorted(COUNTS.items()))
def test_scenario_counts(sc: dict[str, Any], sid: str, expected: tuple[int, int, int]) -> None:
    assert sc[sid].counts == {"emails": expected[0], "attachments": expected[1], "chats": expected[2]}


def test_phase_totals_match_brief(result: BuildResult) -> None:
    built = [s for s in result.scenarios if s.phase == "trade"]
    assert [s.id for s in built] == list(COUNTS)
    assert sum(s.counts["emails"] for s in built) == 42
    assert sum(s.counts["attachments"] for s in built) == 18
    assert sum(s.counts["chats"] for s in built) == 13


def test_seven_deals_t3_has_two(deals: dict[str, dict[str, Any]], sc: dict[str, Any]) -> None:
    assert len(deals) == 7
    assert len(sc["t3-sydney-harbourline-ulp"].deals) == 2
    assert all(len(sc[sid].deals) == 1 for sid in COUNTS if not sid.startswith("t3"))


def test_files_on_disk_match_counts(result: BuildResult) -> None:
    files = render_files(result)
    trade = {f: b for f, b in files.items() if f.startswith("trade/")}
    eml = [f for f in trade if "/messages/" in f and f.endswith(".eml")]
    md = [f for f in trade if "/messages/" in f and f.endswith(".md")]
    att = [f for f in trade if "/attachments/" in f]
    chats = [f for f in trade if "/chats/" in f]
    assert (len(eml), len(md), len(att), len(chats)) == (42, 42, 18, 13)
    for f in chats:
        record = json.loads(trade[f])
        assert record["lines"]
        assert all({"intent", "terms", "text", "timestamp", "speaker"} <= set(line) for line in record["lines"])


def test_attachment_hashes_match_files(result: BuildResult) -> None:
    files = render_files(result)
    for s in result.scenarios:
        if s.phase != "trade":
            continue
        for a in s.attachments:
            assert hashlib.sha256(files[f"trade/{s.id}/{a['path']}"]).hexdigest() == a["sha256"]


def test_trade_files_are_byte_identical_on_rebuild(result: BuildResult) -> None:
    first = {k: v for k, v in render_files(result).items() if k.startswith("trade/")}
    again = {k: v for k, v in render_files(build()).items() if k.startswith("trade/")}
    assert first == again


# --- planted problems ---------------------------------------------------------------------------


@pytest.mark.parametrize(("sid", "planted"), sorted(PLANTED.items()))
def test_planted_contradictions(sc: dict[str, Any], sid: str, planted: dict[str, bool]) -> None:
    found = {c["id"].split(":", 1)[1]: c["caught"] for c in sc[sid].expected_contradictions}
    assert found == planted
    on_deals = {m["id"].split(":", 1)[1]: m["caught"] for deal in sc[sid].deals for m in deal["mismatches"]}
    assert on_deals == planted


def test_caught_problems_name_the_catching_message(sc: dict[str, Any]) -> None:
    for sid in COUNTS:
        keys = {m["key"] for m in sc[sid].messages}
        for c in sc[sid].expected_contradictions:
            assert (c["caught_by"] in keys) == c["caught"]


def test_t1_recap_premium_differs_from_chat(sc: dict[str, Any], deals: dict[str, dict[str, Any]]) -> None:
    s = sc["t1-houston-vitol-wti"]
    deal = next(iter(s.deals))
    agree = [ln for ln in _chat(s, "premium")["lines"] if ln["intent"] == "agree"]
    assert [ln["terms"]["premium_usd_per_bbl"] for ln in agree] == [0.85]
    chat_premiums = [
        ln["terms"]["premium_usd_per_bbl"]
        for ln in _chat(s, "premium")["lines"]
        if "premium_usd_per_bbl" in ln["terms"]
    ]
    assert chat_premiums == [0.75, 0.95, 0.85, 0.85]
    assert _chat(s, "enquiry")["lines"][1]["terms"]["premium_usd_per_bbl"] == 1.10
    recap = _attachment(s, f"{deal['trade_id']}-recap-vitol.pdf")["facts"]
    ticket = deal["tickets"][-1]["facts"]
    assert recap["premium_usd_per_bbl"] == 0.95
    assert ticket["premium_usd_per_bbl"] == 0.85
    assert deal["mismatches"][0]["agreed"] == 0.85
    assert deal["mismatches"][0]["found"] == 0.95


def test_t2_prepayment_and_wrong_currency(sc: dict[str, Any], onb: dict[str, dict[str, Any]]) -> None:
    s = sc["t2-london-kestrel-vlsfo"]
    deal = s.deals[0]
    assert onb["s2-london-kestrel-vlsfo"]["prepayment_only"] is True
    assert deal["credit_check"]["status"] == "prepayment_required"
    assert deal["credit_check"]["credit_used"] == 0
    assert deal["prepayment"]["amount"] == deal["terms"]["quantity"] * deal["terms"]["price_usd_per_mt"] == 4_360_000
    first = _attachment(s, f"{deal['trade_id']}-prepayment-request.pdf")["facts"]
    assert (first["currency"], first["amount"]) == ("GBP", 4_360_000)
    fixed = _message(s, "prepay-corrected")["facts"]
    assert (fixed["currency"], fixed["amount"]) == ("USD", 4_360_000)
    assert deal["terms"]["currency"] == "USD"


def test_t3_second_cargo_declined_then_resized(sc: dict[str, Any], onb: dict[str, dict[str, Any]]) -> None:
    s = sc["t3-sydney-harbourline-ulp"]
    a, b = s.deals
    price = Decimal(str(a["terms"]["price_usd_per_mt"]))
    limit = onb["s3-sydney-harbourline-ulp"]["credit"]["limit"]
    declined = b["declined_checks"][0]
    assert b["status"] == "resized"
    assert b["requested_quantity"] == 20_000
    assert b["terms"]["quantity"] == 18_000
    assert declined["status"] == "declined"
    assert declined["exposure_if_booked"] == a["notional"] + int(20_000 * price) > limit
    assert declined["credit_used"] == 0
    max_qty = b["max_quantity"]
    assert max_qty * price <= declined["headroom_before"] < (max_qty + 1) * price
    assert b["terms"]["quantity"] <= max_qty
    assert b["credit_check"]["status"] == "approved"
    assert b["credit_check"]["exposure_after"] == a["notional"] + b["notional"] <= limit
    assert _attachment(s, f"{b['trade_id']}-credit-check-declined.pdf")["facts"]["status"] == "declined"
    assert [m for m in s.messages if "decline" in m["tags"]]


def test_t4_newco_pays_fixed(sc: dict[str, Any]) -> None:
    s = sc["t4-newyork-glencore-isda"]
    deal = s.deals[0]
    assert deal["side"] == "pay-fixed"
    assert deal["terms"]["fixed_payer"].startswith("NewCo")
    assert "Glencore" in deal["terms"]["fixed_receiver"]
    assert "you pay 68.40 fixed" in _chat(s, "execution")["lines"][-1]["text"]


def test_t4_ticket_digits_transposed(sc: dict[str, Any], result: BuildResult) -> None:
    s = sc["t4-newyork-glencore-isda"]
    deal = s.deals[0]
    agreed = f"{deal['terms']['fixed_price_usd_per_bbl']:.2f}"
    v1, v2 = (t["facts"]["fixed_price_usd_per_bbl"] for t in deal["tickets"])
    assert f"{v1:.2f}" == agreed == "68.40"
    typed = f"{v2:.2f}"
    assert typed == "68.04"
    assert typed != agreed and sorted(typed) == sorted(agreed) and typed[:-2] == agreed[:-2]
    last = latest_ticket(deal["trade_id"], result.universe, sc)
    assert last["version"] == 2 and last["facts"]["fixed_price_usd_per_bbl"] == 68.04
    glencore = _message(s, "glencore-note")["facts"]
    assert glencore["fixed_price_usd_per_bbl"] == 68.40


def test_t5_wrong_incoterm_and_group_exposure(sc: dict[str, Any], onb: dict[str, dict[str, Any]]) -> None:
    s = sc["t5-sydney-bp-jet"]
    deal = s.deals[0]
    o = onb["s5-sydney-bp-jet"]
    assert deal["terms"]["incoterm"] == "CIF"
    assert _chat(s, "agreement")["lines"][-1]["terms"]["incoterm"] == "CIF"
    assert deal["tickets"][-1]["facts"]["incoterm"] == "CIF"
    assert _attachment(s, f"{deal['trade_id']}-recap-bp.pdf")["facts"]["incoterm"] == "CFR"
    check = deal["credit_check"]
    assert check["other_group_exposure"] == o["london_exposure"] == 62_000_000
    assert check["group_exposure_after"] == 62_000_000 + deal["notional"]
    assert check["group_exposure_after"] <= o["group_limit"]
    assert check["group_headroom_after"] == o["group_limit"] - check["group_exposure_after"]
    assert _message(s, "london-reply")["facts"]["london_exposure"] == o["london_exposure"]


def test_t6_forward_amount_and_value_date(sc: dict[str, Any], result: BuildResult) -> None:
    s = sc["t6-london-northgate-gbp-fwd"]
    deal = s.deals[0]
    t = deal["terms"]
    assert Decimal(t["buy_amount"]) * Decimal(str(t["rate"])) == Decimal(t["sell_amount"]) == Decimal(5_138_000)
    agreed = t["value_date"]
    assert [x["facts"]["value_date"] for x in deal["tickets"]] != [agreed] * 3
    first, wrong, fixed = (x["facts"]["value_date"] for x in deal["tickets"])
    assert first == fixed == agreed and wrong != agreed
    cal = CALENDARS["GB"]
    assert date.fromisoformat(wrong) > date.fromisoformat(agreed)
    assert date.fromisoformat(agreed).weekday() < 5 and date.fromisoformat(agreed) not in cal.holidays
    assert _chat(s, "dealing")["lines"][-1]["terms"] == t
    assert latest_ticket(deal["trade_id"], result.universe, sc)["facts"]["value_date"] == agreed
    assert deal["credit_check"] is None


# --- links to onboarding ------------------------------------------------------------------------


@pytest.mark.parametrize(("tid", "sid"), sorted(ONBOARDING_OF.items()))
def test_deal_reads_onboarded_values(sc: dict[str, Any], onb: dict[str, dict[str, Any]], tid: str, sid: str) -> None:
    o = onb[sid]
    for deal in sc[tid].deals:
        assert deal["onboarding_scenario"] == sid
        assert deal["agreement"] == o["agreement"]
        assert deal["payment_days"] == o["payment_days"]
        assert deal["tolerance_pct"] == o["tolerance_pct"]
        assert deal["governing_law"] == o["governing_law"]
        assert (deal["entity"], deal["counterparty"]) == (o["entity"], o["counterparty"])
        first_message = date.fromisoformat(sc[tid].messages[0]["timestamp"][:10])
        assert first_message > date.fromisoformat(o["effective"])
        tickets = [a for a in sc[tid].attachments if a["kind"] == "deal-ticket" and deal["trade_id"] in a["filename"]]
        for t in tickets:
            if "agreement" in t["facts"]:
                assert t["facts"]["agreement"] == o["agreement"]
                assert t["facts"]["governing_law"] == o["governing_law"]
            if "payment_days" in t["facts"]:
                assert t["facts"]["payment_days"] == o["payment_days"]


def test_t3_uses_the_buried_payment_change(sc: dict[str, Any], onb: dict[str, dict[str, Any]]) -> None:
    assert onb["s3-sydney-harbourline-ulp"]["payment_days"] == 45
    assert {d["payment_days"] for d in sc["t3-sydney-harbourline-ulp"].deals} == {45}


def test_onboarded_from_memory_equals_upstream(result: BuildResult, sc: dict[str, Any]) -> None:
    assert onboarded(result.universe) == onboarded(result.universe, sc)


# --- figures recomputed from inputs -------------------------------------------------------------


def test_notionals_recompute_from_inputs(deals: dict[str, dict[str, Any]]) -> None:
    for deal in deals.values():
        i, t = deal["inputs"], deal["terms"]
        if deal["scenario"].startswith("t1"):
            expected = Decimal(i["quantity"]) * (Decimal(str(i["reference_price"])) + Decimal(str(i["premium"])))
        elif deal["kind"] == "physical":
            expected = Decimal(i["quantity"]) * Decimal(str(i["price"]))
            assert i["quantity"] == t["quantity"]
        elif deal["kind"] == "swap":
            expected = Decimal(i["notional_bbl_per_month"] * i["months"]) * Decimal(str(i["fixed_price"]))
        else:
            expected = Decimal(i["buy_amount"]) * Decimal(str(i["rate"]))
        assert Decimal(deal["notional"]) == expected


def test_credit_checks_recompute(deals: dict[str, dict[str, Any]], onb: dict[str, dict[str, Any]]) -> None:
    for deal in deals.values():
        check = deal["credit_check"]
        if check is None:
            continue
        o = onb[deal["onboarding_scenario"]]
        assert check["limit"] == o["credit"]["limit"]
        assert check["headroom_before"] == check["limit"] - check["exposure_before"]
        assert check["exposure_after"] == check["exposure_before"] + check["credit_used"]
        assert check["headroom_after"] == check["limit"] - check["exposure_after"]
        if check["status"] == "approved":
            assert check["exposure_after"] <= check["limit"]
            assert check["credit_used"] == check["deal_value"]
        if deal["kind"] == "swap":
            pfe = deal["notional"] * deal["inputs"]["pfe_pct"] // 100
            assert check["deal_value"] == pfe
        else:
            assert check["deal_value"] == deal["notional"]


def test_credit_check_function_decisions(onb: dict[str, dict[str, Any]]) -> None:
    s1 = onb["s1-houston-vitol-wti"]
    limit = s1["credit"]["limit"]
    assert credit_check(s1, limit)["status"] == "approved"
    assert credit_check(s1, limit + 1)["status"] == "declined"
    assert credit_check(s1, 1, existing=limit)["status"] == "declined"
    assert credit_check(onb["s2-london-kestrel-vlsfo"], 1)["status"] == "prepayment_required"
    s5 = onb["s5-sydney-bp-jet"]
    assert (
        credit_check(s5, 1_000_000, group_limit=s5["group_limit"], other_group_exposure=s5["group_limit"])["status"]
        == "declined"
    )


def test_attached_credit_checks_repeat_the_deal_check(sc: dict[str, Any], deals: dict[str, dict[str, Any]]) -> None:
    for s in (sc[i] for i in COUNTS):
        for a in s.attachments:
            if a["kind"] == "credit-check":
                facts = a["facts"]
                deal = deals[facts["trade_id"]]
                shown = deal["declined_checks"][0] if "declined" in a["filename"] else deal["credit_check"]
                assert {k: facts[k] for k in shown} == shown


# --- agreed terms, chats, ground truth ----------------------------------------------------------


def test_agreement_line_states_the_deal_terms_exactly(sc: dict[str, Any], deals: dict[str, dict[str, Any]]) -> None:
    for deal in deals.values():
        chat = _chat(sc[deal["scenario"]], deal["agreed_in"])
        agree = [ln for ln in chat["lines"] if ln["intent"] == "agree"]
        assert len(agree) == 1, deal["trade_id"]
        assert agree[0]["terms"] == deal["terms"]
        assert chat["lines"][-1] is agree[0] or chat["lines"][-1]["intent"] == "agree"


def test_every_chat_line_has_intent_terms_and_a_known_deal(
    sc: dict[str, Any], deals: dict[str, dict[str, Any]]
) -> None:
    for sid in COUNTS:
        for chat in sc[sid].chats:
            assert chat["deal"] in deals
            assert chat["phase"] == "trade"
            assert chat["class_uri"]
            for ln in chat["lines"]:
                assert ln["intent"] and isinstance(ln["terms"], dict)


def test_every_deal_chat_is_recorded(sc: dict[str, Any], deals: dict[str, dict[str, Any]]) -> None:
    for deal in deals.values():
        assert {c["key"] for c in sc[deal["scenario"]].chats if c["deal"] == deal["trade_id"]} == set(deal["chats"])


def test_trade_ids_are_unique_and_follow_the_pattern(deals: dict[str, dict[str, Any]]) -> None:
    assert all(tid.startswith("NC-") and tid.endswith(tuple("0123456789")) for tid in deals)
    assert sorted(deals) == sorted(set(deals))


def test_message_times_fall_in_working_hours(result: BuildResult, sc: dict[str, Any]) -> None:
    u = result.universe
    from datetime import datetime

    for sid in COUNTS:
        for m in sc[sid].messages:
            org = u["orgs"][m["from"]["org"]]
            moment = datetime.fromisoformat(m["timestamp"]).astimezone(ZoneInfo(CALENDARS[org["calendar"]].tz))
            assert in_working_hours(CALENDARS[org["calendar"]], moment), (sid, m["key"])


def test_messages_carry_ground_truth(sc: dict[str, Any]) -> None:
    for sid in COUNTS:
        for m in sc[sid].messages:
            assert m["phase"] == "trade"
            assert m["direction"] in {"inbound", "outbound", "internal"}
            assert m["thread"] and m["from"]["org"] and m["from"]["role"]
            assert m["class_uri"].startswith("http")
        for a in sc[sid].attachments:
            assert a["provenance"]["wasDerivedFrom"] and a["provenance"]["wasAttributedTo"]


def test_questions_have_ids_types_and_expected(sc: dict[str, Any]) -> None:
    for sid in COUNTS:
        qs = sc[sid].questions
        assert len(qs) == 3
        assert [q["id"] for q in qs] == [f"{sid}-q{i}" for i in (1, 2, 3)]
        assert {q["type"] for q in qs} <= {"graph_traversal", "hybrid_retrieval", "reconciliation", "metadata"}
        assert all(q["expected"] not in (None, "") for q in qs)


def test_question_answers_match_the_deals(sc: dict[str, Any], deals: dict[str, dict[str, Any]]) -> None:
    q = {s: {x["id"]: x["expected"] for x in sc[s].questions} for s in COUNTS}
    t1 = sc["t1-houston-vitol-wti"].deals[0]
    assert q["t1-houston-vitol-wti"]["t1-houston-vitol-wti-q3"] == t1["credit_check"]["headroom_after"]
    assert (
        q["t3-sydney-harbourline-ulp"]["t3-sydney-harbourline-ulp-q2"] == deals["NC-SYD-26-0002"]["terms"]["quantity"]
    )
    assert q["t6-london-northgate-gbp-fwd"]["t6-london-northgate-gbp-fwd-q3"] == 5_138_000


# --- traded() and latest_ticket() ---------------------------------------------------------------


def test_traded_returns_every_deal(result: BuildResult, sc: dict[str, Any], deals: dict[str, dict[str, Any]]) -> None:
    from_upstream = traded(result.universe, sc)
    assert set(from_upstream) == set(deals)
    assert traded(result.universe) == from_upstream


def test_latest_ticket_is_the_last_version(
    result: BuildResult, sc: dict[str, Any], deals: dict[str, dict[str, Any]]
) -> None:
    for tid, deal in deals.items():
        last = latest_ticket(tid, result.universe, sc)
        assert last == deal["tickets"][-1]
        assert last["version"] == max(t["version"] for t in deal["tickets"])


# --- class URIs ---------------------------------------------------------------------------------


def test_every_class_uri_is_in_sample_classes(result: BuildResult) -> None:
    allowed = _pack_uris()
    seen: set[str] = set()
    for s in result.scenarios:
        if s.phase == "trade":
            uris = _uris(s.manifest())
            assert uris
            seen.update(uris)
            assert set(uris) <= allowed, set(uris) - allowed
    assert len(seen) >= 8


def test_deal_classes_follow_the_brief(deals: dict[str, dict[str, Any]]) -> None:
    for deal in deals.values():
        c = deal["class_uris"]
        if deal["kind"] == "physical":
            assert c["transaction"].endswith(("/TradeTransaction", "/TradeAgreement"))
            assert c["line_item"].endswith(("/SupplyChainTradeLineItem", "/TradeItem"))
            assert c["order"].endswith("/Order")
        else:
            assert c["instrument"].endswith("/DerivativeInstrument")
            assert c["settlement_terms"].endswith(("/SettlementTerms", "/DerivativeSettlementTerms"))
    assert deals["NC-NYC-26-0001"]["class_uris"]["contract_type"].endswith(
        ("#SWAPS", "/OverTheCounterDerivativeInstrument")
    )
    assert deals["NC-NYC-26-0001"]["class_uris"]["underlier"].endswith("/Underlier")
    assert deals["NC-LON-26-0002"]["class_uris"]["contract_type"].endswith(
        ("#FXOUT", "/OverTheCounterDerivativeInstrument")
    )


def test_no_real_addresses_in_markdown(result: BuildResult) -> None:
    for path, data in render_files(result).items():
        if path.startswith("trade/") and path.endswith(".md"):
            text = data.decode("utf-8")
            for token in text.split():
                if "@" in token:
                    assert token.strip("<>(),.;:").endswith(".example"), token
