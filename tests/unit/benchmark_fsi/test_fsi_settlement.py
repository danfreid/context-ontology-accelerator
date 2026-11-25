# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Settlement phase (x1..x6): counts, planted problems, class URIs, post-trade links, recomputed figures."""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.benchmark.fsi_commodity.build import BuildResult, build, render_files
from tests.benchmark.fsi_commodity.post_trade.common import last_doc, posted
from tests.benchmark.fsi_commodity.settlement import SETTLEMENT
from tests.benchmark.fsi_commodity.trade.common import traded

pytestmark = pytest.mark.unit

PACK = Path(__file__).resolve().parents[3] / "packages" / "ontology-engine" / "demo" / "config-fsi-commodity.yaml"
X1, X2, X3, X4, X5, X6 = (
    "x1-houston-vitol-wti",
    "x2-london-kestrel-vlsfo",
    "x3-sydney-harbourline-ulp",
    "x4-newyork-glencore-isda",
    "x5-sydney-bp-jet",
    "x6-london-northgate-gbp-fwd",
)
COUNTS = {X1: (5, 3, 0), X2: (6, 3, 0), X3: (8, 4, 0), X4: (6, 4, 0), X5: (6, 3, 0), X6: (3, 2, 0)}
TRADE_OF = {
    X1: "t1-houston-vitol-wti",
    X2: "t2-london-kestrel-vlsfo",
    X3: "t3-sydney-harbourline-ulp",
    X4: "t4-newyork-glencore-isda",
    X5: "t5-sydney-bp-jet",
    X6: "t6-london-northgate-gbp-fwd",
}
# scenario -> {contradiction id suffix: caught}
PLANTED = {
    X1: {},
    X2: {"late-payment": True},
    X3: {"bank-charge-short-payment": True},
    X4: {"statement-wrong-prices": True},
    X5: {"invoice-wrong-entity": True},
    X6: {},
}
CENT = Decimal("0.01")


def d(value: Any) -> Decimal:
    return Decimal(str(value))


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@pytest.fixture(scope="module")
def result() -> BuildResult:
    return build()


@pytest.fixture(scope="module")
def sc(result: BuildResult) -> dict[str, Any]:
    return {s.id: s for s in result.scenarios if s.phase == "settlement"}


@pytest.fixture(scope="module")
def upstream(result: BuildResult) -> dict[str, Any]:
    return {s.id: s for s in result.scenarios}


def _pack_uris() -> set[str]:
    """Every class_uri under sample_classes, read straight from the pack."""
    with PACK.open(encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    uris: set[str] = set()
    for group in ("foundational_ontologies", "domain_ontologies", "customer_ontologies"):
        for onto in pack.get(group) or []:
            uris.update(c["class_uri"] for c in onto.get("sample_classes") or [])
    return uris


def _class_values(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, val in node.items():
            if key in ("class_uri", "bibliographic_class_uri") and isinstance(val, str):
                out.append(val)
            elif key == "class_uris" and isinstance(val, dict):
                out.extend(v for v in val.values() if isinstance(v, str))
            else:
                out.extend(_class_values(val))
    elif isinstance(node, list):
        for item in node:
            out.extend(_class_values(item))
    return out


def _att(s: Any, kind: str) -> list[dict[str, Any]]:
    return [a for a in s.attachments if a["kind"] == kind]


def _msg(s: Any, key: str) -> dict[str, Any]:
    return next(m for m in s.messages if m["key"] == key)


def _pay(result: BuildResult, upstream: dict[str, Any], sid: str) -> dict[str, Any]:
    """The post-trade document the payment of ``sid`` must follow, read through last_doc()."""
    deal = next(x for x in traded(result.universe, upstream).values() if x["scenario"] == TRADE_OF[sid])
    return last_doc(deal["trade_id"], result.universe, upstream)


# --- counts ----------------------------------------------------------------------------------------
def test_registry_has_six_builders() -> None:
    assert len(SETTLEMENT) == 6


@pytest.mark.parametrize("sid", COUNTS)
def test_scenario_counts(sc: dict[str, Any], sid: str) -> None:
    c = sc[sid].counts
    assert (c["emails"], c["attachments"], c["chats"]) == COUNTS[sid]
    files = sc[sid].files()
    assert sum(1 for f in files if f.endswith(".eml")) == COUNTS[sid][0]
    assert sum(1 for f in files if f.endswith(".md") and f.startswith("messages/")) == COUNTS[sid][0]
    assert sum(1 for f in files if f.startswith("attachments/")) == COUNTS[sid][1]


def test_phase_totals(sc: dict[str, Any]) -> None:
    assert len(sc) == 6
    assert sum(s.counts["emails"] for s in sc.values()) == 34
    assert sum(s.counts["attachments"] for s in sc.values()) == 19
    assert sum(s.counts["chats"] for s in sc.values()) == 0


def test_whole_corpus_totals(result: BuildResult) -> None:
    assert len(result.scenarios) == 24
    assert sum(s.counts["emails"] for s in result.scenarios) == 269
    assert sum(s.counts["attachments"] for s in result.scenarios) == 171
    assert sum(s.counts["chats"] for s in result.scenarios) == 13


# --- planted problems ------------------------------------------------------------------------------
@pytest.mark.parametrize("sid", COUNTS)
def test_planted_problems(sc: dict[str, Any], sid: str) -> None:
    got = {c["id"].split(":", 1)[1]: c["caught"] for c in sc[sid].expected_contradictions}
    assert got == PLANTED[sid]
    for c in sc[sid].expected_contradictions:
        assert c["where"] and c["values"] and c["description"]
        assert c["caught_by"] in {m["key"] for m in sc[sid].messages}


def test_control_cases_plant_nothing(sc: dict[str, Any]) -> None:
    for sid in (X1, X6):
        assert sc[sid].expected_contradictions == []
        assert sc[sid].outcome["result"] == "settled"


def test_every_scenario_has_questions(sc: dict[str, Any]) -> None:
    for s in sc.values():
        assert len(s.questions) == 3
        assert {q["id"] for q in s.questions} == {f"{s.id}-q{i}" for i in (1, 2, 3)}


# --- class URIs ------------------------------------------------------------------------------------
def test_every_class_uri_is_in_sample_classes(sc: dict[str, Any]) -> None:
    allowed = _pack_uris()
    seen: set[str] = set()
    for s in sc.values():
        seen.update(_class_values(s.manifest()))
    assert seen
    assert seen <= allowed, sorted(seen - allowed)


@pytest.mark.parametrize(
    ("sid", "kind", "expected"),
    [
        (X1, "debit-advice", "https://schema.org/MonetaryAmount"),
        (X1, "payment-notice", "https://schema.org/MonetaryAmount"),
        (X2, "account-statement", "https://schema.org/Invoice"),
        (X3, "cash-reconciliation", "https://schema.org/MonetaryAmount"),
        (X4, "swap-statement", None),
        (X4, "collateral-transfer-notice", None),
        (X5, "netting-statement", "https://schema.org/MonetaryAmount"),
        (X5, "supplier-invoice", "https://schema.org/Invoice"),
        (X6, "fx-settlement-advice", None),
    ],
)
def test_document_class(sc: dict[str, Any], sid: str, kind: str, expected: str | None) -> None:
    atts = _att(sc[sid], kind)
    assert atts
    for a in atts:
        assert a["class_uri"] in _pack_uris()
        if expected:
            assert a["class_uri"] == expected


def test_attachments_carry_provenance(sc: dict[str, Any]) -> None:
    for s in sc.values():
        ids = {m["message_id"] for m in s.messages}
        for a in s.attachments:
            assert a["provenance"]["wasDerivedFrom"] in ids
            assert a["provenance"]["wasAttributedTo"].endswith(".example")


# --- links to post-trade ---------------------------------------------------------------------------
@pytest.mark.parametrize("sid", [X1, X2, X3, X4, X5, X6])
def test_payment_follows_the_last_post_trade_document(
    sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any], sid: str
) -> None:
    doc = _pay(result, upstream, sid)
    s = sc[sid]
    assert s.outcome["trade_id"] == doc["trade_id"]
    with_ids = [m["facts"]["trade_id"] for m in s.messages if m["facts"].get("trade_id")]
    allowed = {doc["trade_id"], s.outcome.get("london_trade_id")}  # x5 also cites the trade London dealt
    assert with_ids and set(with_ids) <= allowed
    assert s.data["payments"][0]["trade_id"] == doc["trade_id"]
    # every settlement scenario starts after the post-trade scenario that produced the document
    post = next(p for p in upstream.values() if p.phase == "post_trade" and doc in p.data.get("documents", []))
    first_x = min(datetime.fromisoformat(m["timestamp"]) for m in s.messages)
    assert first_x > max(datetime.fromisoformat(m["timestamp"]) for m in post.messages)


def test_posted_covers_every_settled_trade(result: BuildResult, upstream: dict[str, Any]) -> None:
    docs = posted(result.universe, upstream)
    assert {_pay(result, upstream, sid)["trade_id"] for sid in COUNTS} <= set(docs)


# --- figures that recompute from manifest inputs ---------------------------------------------------
def test_x1_pays_the_credit_note_net(sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]) -> None:
    s, doc = sc[X1], _pay(result, upstream, X1)
    assert d(doc["payable"]) == Decimal("16805084.20")
    paid = d(s.outcome["amount_paid"])
    assert paid == d(doc["payable"]) == d(doc["inputs"]["invoice_total"]) - d(doc["inputs"]["credit"])
    release = _att(s, "payment-release-request")[0]["facts"]
    assert d(release["amount"]) == paid > d(release["release_limit"])
    assert release["second_approval_required"] is True
    debit = _att(s, "debit-advice")[0]["facts"]
    notice = _att(s, "payment-notice")[0]["facts"]
    assert d(debit["amount"]) == d(notice["amount"]) == paid
    assert debit["value_date"] == notice["value_date"] == doc["due"]
    assert debit["account"]["account_id"] == doc["payer_account"] == notice["from_account"]
    assert notice["to_account"]["account_id"] == doc["payee_account"]


def test_x2_balance_days_late_and_interest(sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]) -> None:
    s, doc = sc[X2], _pay(result, upstream, X2)
    assert d(doc["payable"]) == Decimal("46886.40")
    stmt = _att(s, "account-statement")[0]["facts"]
    assert d(stmt["balance"]) == d(doc["payable"]) and stmt["due"] == doc["due"]
    credit = _att(s, "credit-advice")[0]["facts"]
    assert d(credit["amount"]) == d(credit["instructed"]) - d(credit["charges"]) == d(doc["payable"])
    late = (date.fromisoformat(credit["value_date"]) - date.fromisoformat(doc["due"])).days
    assert late == 2 == s.outcome["days_late"]
    interest = _att(s, "late-interest-statement")[0]["facts"]
    assert interest["days_late"] == late and interest["waived"] is True and interest["interest_payable"] == 0
    assert d(interest["interest"]) == q2(d(interest["balance"]) * d(interest["rate_pct"]) / 100 * late / 365)
    assert d(s.outcome["interest_accrued"]) == d(interest["interest"])


def test_x3_short_payment_and_top_up(sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]) -> None:
    s, doc = sc[X3], _pay(result, upstream, X3)
    first = _att(s, "credit-advice")
    assert len(first) == 2
    advice, top_up = (a["facts"] for a in first)
    assert d(advice["instructed"]) == d(doc["payable"])
    assert d(advice["amount"]) == d(doc["payable"]) - Decimal("45.00")
    assert d(advice["charges"]) == Decimal("45.00")
    recon = _att(s, "cash-reconciliation")[0]["facts"]
    assert (
        d(recon["unreconciled"])
        == Decimal("45.00")
        == d(recon["rows"][0]["expected"]) - d(recon["rows"][0]["received"])
    )
    assert d(top_up["amount"]) == d(recon["unreconciled"])
    assert d(advice["amount"]) + d(top_up["amount"]) == d(doc["payable"]) == d(s.outcome["net_payable"])
    assert _msg(s, "short-payment-query")["facts"] == {
        "trade_id": doc["trade_id"],
        "shortfall": 45,
        "clause": "no_deductions",
    }


def test_x4_statement_recomputes_from_the_prices(
    sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]
) -> None:
    s, doc = sc[X4], _pay(result, upstream, X4)
    deal = next(x for x in traded(result.universe, upstream).values() if x["scenario"] == TRADE_OF[X4])
    first, second = (a["facts"] for a in _att(s, "swap-statement"))
    assert (first["version"], second["version"]) == (1, 2)
    assert len(first["prices"]) == len(second["prices"]) == first["price_count"] == 23
    assert [p["date"] for p in first["prices"]] == [p["date"] for p in second["prices"]]
    for facts in (first, second):
        avg = (sum(d(p["price"]) for p in facts["prices"]) / len(facts["prices"])).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        assert d(facts["average"]) == avg
        assert facts["fixed_price"] == deal["terms"]["fixed_price_usd_per_bbl"]
        assert d(facts["amount"]) == q2((d(facts["fixed_price"]) - avg) * facts["notional"])
    # the first statement carries each previous day's assessment: shifted by one day
    assert [p["price"] for p in first["prices"]][1:] == [p["price"] for p in second["prices"]][:-1]
    assert first["amount"] != second["amount"]
    pay = _att(s, "payment-notice")[0]["facts"]
    assert d(pay["amount"]) == d(second["amount"]) == d(s.outcome["settled_amount"])
    collateral = _att(s, "collateral-transfer-notice")[0]["facts"]
    assert d(collateral["amount"]) == d(doc["payable"]) == Decimal(620000)
    assert collateral["from_account"] == doc["payer_account"] and collateral["to_account"] == doc["payee_account"]
    assert pay["to_account"]["account_id"] == doc["payee_account"]
    assert pay["from_account"] != doc["payer_account"]  # the swap pays from the operating account


def test_x5_netting_recomputes(sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]) -> None:
    s, doc = sc[X5], _pay(result, upstream, X5)
    inv = _att(s, "supplier-invoice")[0]["facts"]
    qty, price = d(inv["inputs"]["quantity"]), d(inv["inputs"]["price"])
    total = q2(qty * price)
    assert d(inv["total_due"]) == total == d(s.outcome["payable"])
    net = _att(s, "netting-statement")[0]["facts"]
    amounts = {i["direction"]: d(i["amount"]) for i in net["items"]}
    assert amounts["receivable"] == d(doc["payable"]) and amounts["payable"] == total
    assert d(net["net"]) == amounts["receivable"] - amounts["payable"]
    credit = _att(s, "credit-advice")[0]["facts"]
    assert d(credit["amount"]) == d(net["net"]) == d(s.outcome["net_settled"])
    assert credit["value_date"] == doc["due"] == net["value_date"]
    # Sydney has no record of the trade London dealt
    london_tid = s.outcome["london_trade_id"]
    assert london_tid not in traded(result.universe, upstream)
    assert inv["billed_to"] == "NewCo Trading Sydney Pty Ltd"
    assert (
        net["agreement"]
        == next(x for x in traded(result.universe, upstream).values() if x["scenario"] == TRADE_OF[X5])["agreement"]
    )


def test_x6_forward_settles_at_the_agreed_rate(
    sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]
) -> None:
    s, doc = sc[X6], _pay(result, upstream, X6)
    adv = _att(s, "fx-settlement-advice")[0]["facts"]
    assert d(adv["buy_amount"]) == Decimal(4_000_000) and d(adv["rate"]) == Decimal("1.2845")
    assert q2(d(adv["buy_amount"]) * d(adv["rate"])) == d(adv["sell_amount"]) == Decimal(5_138_000) == d(doc["payable"])
    assert adv["debit_account"] == doc["payer_account"]
    instruction = _att(s, "fx-settlement-instruction")[0]["facts"]
    assert d(instruction["pay_amount"]) == d(adv["sell_amount"])
    assert d(instruction["receive_amount"]) == d(adv["buy_amount"])
    assert adv["value_date"] == instruction["value_date"] == doc["due"]


# --- determinism -----------------------------------------------------------------------------------
def test_settlement_files_are_byte_identical_on_rebuild(result: BuildResult) -> None:
    first = {k: v for k, v in render_files(result).items() if k.startswith("settlement/")}
    second = {k: v for k, v in render_files(build()).items() if k.startswith("settlement/")}
    assert first and first == second
    assert sum(1 for k in first if k.endswith("manifest.json")) == 6
