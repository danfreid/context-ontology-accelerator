# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Post-trade phase (p1..p6): counts, planted problems, class URIs, trade links, recomputed figures."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.benchmark.fsi_commodity.build import BuildResult, build, render_files
from tests.benchmark.fsi_commodity.post_trade import POST_TRADE
from tests.benchmark.fsi_commodity.post_trade.common import last_doc, posted
from tests.benchmark.fsi_commodity.trade.common import latest_ticket, traded

pytestmark = pytest.mark.unit

PACK = Path(__file__).resolve().parents[3] / "packages" / "ontology-engine" / "demo" / "config-fsi-commodity.yaml"
COUNTS = {
    "p1-houston-vitol-wti": (12, 9, 0),
    "p2-london-kestrel-vlsfo": (10, 8, 0),
    "p3-sydney-harbourline-ulp": (10, 7, 0),
    "p4-newyork-glencore-isda": (7, 4, 0),
    "p5-sydney-bp-jet": (10, 7, 0),
    "p6-london-northgate-gbp-fwd": (6, 4, 0),
}
TRADE_OF = {
    "p1-houston-vitol-wti": "t1-houston-vitol-wti",
    "p2-london-kestrel-vlsfo": "t2-london-kestrel-vlsfo",
    "p3-sydney-harbourline-ulp": "t3-sydney-harbourline-ulp",
    "p4-newyork-glencore-isda": "t4-newyork-glencore-isda",
    "p5-sydney-bp-jet": "t5-sydney-bp-jet",
    "p6-london-northgate-gbp-fwd": "t6-london-northgate-gbp-fwd",
}
# scenario -> {contradiction id suffix: caught}
PLANTED = {
    "p1-houston-vitol-wti": {"invoice-premium-overcharge": True},
    "p2-london-kestrel-vlsfo": {},
    "p3-sydney-harbourline-ulp": {"invoice-wrong-quantity-basis": True},
    "p4-newyork-glencore-isda": {"confirmation-on-mistyped-ticket": True, "margin-call-ignores-threshold": True},
    "p5-sydney-bp-jet": {"fx-rate-date-dispute": True},
    "p6-london-northgate-gbp-fwd": {"swapped-account-digits": True},
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
    return {s.id: s for s in result.scenarios if s.phase == "post_trade"}


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


# --- counts ----------------------------------------------------------------------------------------
def test_registry_has_six_builders() -> None:
    assert len(POST_TRADE) == 6


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
    assert sum(s.counts["emails"] for s in sc.values()) == 55
    assert sum(s.counts["attachments"] for s in sc.values()) == 39
    assert sum(s.counts["chats"] for s in sc.values()) == 0


def test_whole_corpus_totals(result: BuildResult) -> None:
    assert sum(s.counts["emails"] for s in result.scenarios) >= 55 + 42 + 138
    assert sum(s.counts["attachments"] for s in result.scenarios if s.phase == "post_trade") == 39


# --- planted problems ------------------------------------------------------------------------------
@pytest.mark.parametrize("sid", COUNTS)
def test_planted_problems(sc: dict[str, Any], sid: str) -> None:
    got = {c["id"].split(":", 1)[1]: c["caught"] for c in sc[sid].expected_contradictions}
    assert got == PLANTED[sid]
    for c in sc[sid].expected_contradictions:
        assert c["where"] and c["values"] and c["description"]
        assert c["caught_by"] in {m["key"] for m in sc[sid].messages}


def test_p2_quantity_query_is_answered_by_the_tolerance(sc: dict[str, Any]) -> None:
    s = sc["p2-london-kestrel-vlsfo"]
    deal = next(iter(traded_deals(s)))
    assert s.outcome["quantity_query"]["within_tolerance"] is True
    answer = next(m for m in s.messages if m["key"] == "quantity-answer")
    assert answer["facts"]["tolerance_pct"] == deal["tolerance_pct"]
    assert abs(d(answer["facts"]["difference"])) <= d(deal["terms"]["quantity"]) * d(deal["tolerance_pct"]) / 100


def traded_deals(s: Any) -> list[dict[str, Any]]:
    return [x for x in TRADED.values() if x["scenario"] == TRADE_OF[s.id]]


TRADED: dict[str, dict[str, Any]] = {}


@pytest.fixture(scope="module", autouse=True)
def _load_traded(result: BuildResult, upstream: dict[str, Any]) -> None:
    TRADED.update(traded(result.universe, upstream))


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
        ("p1-houston-vitol-wti", "invoice", "https://schema.org/Invoice"),
        ("p1-houston-vitol-wti", "credit-note", "https://schema.org/Invoice"),
        ("p1-houston-vitol-wti", "bill-of-lading", None),
        ("p1-houston-vitol-wti", "quantity-certificate", "https://vocabulary.uncefact.org/SpecifiedInspection"),
        ("p1-houston-vitol-wti", "nomination", "https://vocabulary.uncefact.org/TransportMovement"),
        (
            "p1-houston-vitol-wti",
            "trade-confirmation",
            "https://spec.edmcouncil.org/fibo/ontology/FND/Agreements/Agreements/Agreement",
        ),
        ("p2-london-kestrel-vlsfo", "credit-advice", "https://schema.org/MonetaryAmount"),
        ("p5-sydney-bp-jet", "tax-invoice", "https://schema.org/Invoice"),
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


# --- links to the trade phase ----------------------------------------------------------------------
@pytest.mark.parametrize("sid", COUNTS)
def test_every_message_cites_the_trade_id(sc: dict[str, Any], sid: str) -> None:
    deal = traded_deals(sc[sid])[0]
    tids = {deal["trade_id"]}
    with_ids = [m for m in sc[sid].messages if m["facts"].get("trade_id")]
    assert with_ids
    assert {m["facts"]["trade_id"] for m in with_ids} <= tids
    for a in sc[sid].attachments:
        assert a["facts"].get("trade_id", deal["trade_id"]) == deal["trade_id"]


def test_confirmations_follow_the_latest_ticket(
    sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]
) -> None:
    for sid in ("p1-houston-vitol-wti", "p2-london-kestrel-vlsfo", "p3-sydney-harbourline-ulp", "p5-sydney-bp-jet"):
        deal = traded_deals(sc[sid])[0]
        ticket = latest_ticket(deal["trade_id"], result.universe, upstream)
        conf = _att(sc[sid], "trade-confirmation")[0]
        assert conf["facts"]["based_on"] == ticket["filename"]
        for key, val in ticket["facts"].items():
            if key not in ("issued_by", "version"):
                assert conf["facts"][key] == val, (sid, key)


def test_p4_first_confirmation_carries_the_mistyped_price(sc: dict[str, Any]) -> None:
    s = sc["p4-newyork-glencore-isda"]
    deal = traded_deals(s)[0]
    first, second = _att(s, "trade-confirmation")
    assert first["facts"]["fixed_price_usd_per_bbl"] == 68.04
    assert second["facts"]["fixed_price_usd_per_bbl"] == deal["terms"]["fixed_price_usd_per_bbl"] == 68.4


def test_p4_newco_is_the_payer_of_the_call(sc: dict[str, Any]) -> None:
    s = sc["p4-newyork-glencore-isda"]
    deal = traded_deals(s)[0]
    for a in _att(s, "margin-call"):
        assert a["facts"]["called_from"] == deal["terms"]["fixed_payer"]


def test_p6_confirmation_uses_the_final_ticket(
    sc: dict[str, Any], result: BuildResult, upstream: dict[str, Any]
) -> None:
    s = sc["p6-london-northgate-gbp-fwd"]
    deal = traded_deals(s)[0]
    ticket = latest_ticket(deal["trade_id"], result.universe, upstream)
    assert ticket["version"] == 3
    for conf in _att(s, "trade-confirmation"):
        assert conf["facts"]["value_date"] == ticket["facts"]["value_date"] == deal["terms"]["value_date"]


def test_p6_swapped_account_digits(sc: dict[str, Any], result: BuildResult) -> None:
    s = sc["p6-london-northgate-gbp-fwd"]
    acct = result.universe["accounts"]["acct-newco-lon-usd-operating"]["number"]
    first, second = _att(s, "swift-copy")
    wrong = first["facts"]["debit_account"]
    assert wrong != acct
    assert sorted(wrong) == sorted(acct)
    assert sum(a != b for a, b in zip(wrong, acct, strict=True)) == 2
    assert second["facts"]["debit_account"] == acct
    contradiction = s.expected_contradictions[0]["values"]
    assert (contradiction["found"], contradiction["agreed"]) == (wrong, acct)


# --- figures recomputed from the manifest ----------------------------------------------------------
def _invoice_total(facts: dict[str, Any]) -> Decimal:
    lines = Decimal(0)
    for ln in facts["lines"]:
        if "quantity" in ln:
            assert q2(d(ln["quantity"]) * d(ln["price"])) == d(ln["amount"])
        lines += d(ln["amount"])
    assert lines == d(facts["subtotal"])
    total = lines - sum((d(x["amount"]) for x in facts["deductions"]), Decimal(0))
    assert total == d(facts["total_due"])
    return total


def test_every_invoice_recomputes_from_its_lines(sc: dict[str, Any]) -> None:
    n = 0
    for s in sc.values():
        for a in s.attachments:
            if a["kind"] in ("invoice", "tax-invoice", "final-invoice", "prepayment-invoice"):
                _invoice_total(a["facts"])
                n += 1
    assert n == 5


def test_every_credit_note_recomputes(sc: dict[str, Any]) -> None:
    for sid in ("p1-houston-vitol-wti", "p3-sydney-harbourline-ulp"):
        s = sc[sid]
        inv = _att(s, "invoice")[0]["facts"]
        cn = _att(s, "credit-note")[0]["facts"]
        assert cn["against_invoice"] == inv["number"]
        assert d(cn["invoice_total"]) == d(inv["total_due"])
        credit = cn["credit_lines"][0]
        assert q2(d(credit["quantity"]) * d(credit["price"])) == d(credit["amount"]) == d(cn["credit_total"])
        assert d(cn["net_payable"]) == d(inv["total_due"]) - d(cn["credit_total"])
        assert d(posted_payable(sc, sid)) == d(cn["net_payable"])


def posted_payable(sc: dict[str, Any], sid: str) -> Any:
    return sc[sid].data["documents"][-1]["payable"]


def test_p1_overcharge_and_net_payable(sc: dict[str, Any]) -> None:
    s = sc["p1-houston-vitol-wti"]
    deal = traded_deals(s)[0]
    agreed, found = d(deal["terms"]["premium_usd_per_bbl"]), d(next(m for m in deal["mismatches"])["found"])
    inv = _att(s, "invoice")[0]["facts"]
    assess = _att(s, "price-assessments")[0]["facts"]
    qty = d(inv["inputs"]["quantity"])
    prices = [d(r["price"]) for r in assess["assessments"]]
    assert len(prices) == 5
    average = sum(prices, Decimal(0)) / 5
    assert d(assess["average"]) == average == d(inv["inputs"]["average_price"])
    assert d(inv["inputs"]["premium"]) == found == Decimal("0.95")
    base, prem = q2(qty * average), q2(qty * found)
    assert d(inv["total_due"]) == base + prem
    overcharge = prem - q2(qty * agreed)
    sheet = _att(s, "invoice-check-sheet")[0]["facts"]
    assert d(sheet["total_difference"]) == overcharge == d(s.outcome["overcharge"])
    assert round(overcharge) == 25138
    assert d(_att(s, "credit-note")[0]["facts"]["credit_total"]) == overcharge
    assert d(s.outcome["net_payable"]) == base + q2(qty * agreed)
    cert = _att(s, "quantity-certificate")[0]["facts"]["quantity"]
    assert d(cert) == qty
    assert abs(qty - d(deal["terms"]["quantity"])) <= d(deal["terms"]["quantity"]) * d(deal["tolerance_pct"]) / 100


def test_brief_figures_via_last_doc(result: BuildResult, upstream: dict[str, Any]) -> None:
    u = result.universe
    deals = traded(u, upstream)
    by_scenario = {x["scenario"]: x["trade_id"] for x in deals.values()}
    assert d(last_doc(by_scenario["t1-houston-vitol-wti"], u, upstream)["payable"]) == Decimal("16805084.20")
    assert d(last_doc(by_scenario["t2-london-kestrel-vlsfo"], u, upstream)["payable"]) == Decimal("46886.40")
    assert d(last_doc(by_scenario["t6-london-northgate-gbp-fwd"], u, upstream)["payable"]) == Decimal("5138000")
    assert set(posted(u, upstream)) == {x["trade_id"] for x in deals.values() if x["status"] != "resized"}


def test_p2_true_up(sc: dict[str, Any]) -> None:
    s = sc["p2-london-kestrel-vlsfo"]
    deal = traded_deals(s)[0]
    proforma = _att(s, "prepayment-invoice")[0]["facts"]
    final = _att(s, "final-invoice")[0]["facts"]
    assert (
        d(proforma["total_due"])
        == d(deal["prepayment"]["amount"])
        == d(deal["terms"]["quantity"]) * d(deal["terms"]["price_usd_per_mt"])
    )
    cargo, fee = final["lines"]
    inputs = final["inputs"]
    assert q2(d(inputs["quantity"]) * d(inputs["price"])) == d(cargo["amount"])
    cert = _att(s, "quantity-certificate")[0]["facts"]
    assert d(cert["inspection_fee_usd"]) == d(inputs["inspection_fee"])
    assert q2(d(inputs["inspection_fee"]) * d(inputs["fee_share_pct"]) / 100) == d(fee["amount"])
    assert d(final["deductions"][0]["amount"]) == d(inputs["prepayment"]) == d(deal["prepayment"]["amount"])
    assert d(final["total_due"]) == d(cargo["amount"]) + d(fee["amount"]) - d(inputs["prepayment"])


def test_p3_loi_and_quantity_basis(sc: dict[str, Any]) -> None:
    s = sc["p3-sydney-harbourline-ulp"]
    deal = traded_deals(s)[0]
    loi = _att(s, "letter-of-indemnity")[0]["facts"]
    cargo_value = q2(d(loi["cargo_quantity"]) * d(loi["price"]))
    assert d(loi["cargo_value"]) == cargo_value
    assert loi["cover_pct"] == 125
    assert d(loi["indemnity_amount"]) == q2(cargo_value * Decimal("1.25"))
    assert d(loi["price"]) == d(deal["terms"]["price_usd_per_mt"])
    inv = _att(s, "invoice")[0]["facts"]
    bl = _att(s, "bill-of-lading")[0]["facts"]
    discharge = _att(s, "discharge-certificate")[0]["facts"]
    assert d(inv["inputs"]["quantity"]) == d(bl["quantity"]) != d(discharge["quantity"])
    cn = _att(s, "credit-note")[0]["facts"]
    assert d(cn["net_payable"]) == q2(d(discharge["quantity"]) * d(deal["terms"]["price_usd_per_mt"]))
    assert (
        abs(d(discharge["quantity"]) - d(deal["terms"]["quantity"]))
        <= d(deal["terms"]["quantity"]) * d(deal["tolerance_pct"]) / 100
    )


def test_p4_margin_calls_recompute(sc: dict[str, Any]) -> None:
    s = sc["p4-newyork-glencore-isda"]
    deal = traded_deals(s)[0]
    first, second = (a["facts"] for a in _att(s, "margin-call"))
    for f in (first, second):
        exposure = q2((d(f["fixed_price"]) - d(f["forward_price"])) * f["volume_bbl"])
        assert d(f["exposure"]) == exposure
        assert f["volume_bbl"] == deal["terms"]["notional_bbl_per_month"] * deal["terms"]["months"]
        assert d(f["fixed_price"]) == d(deal["terms"]["fixed_price_usd_per_bbl"])
        called = max(exposure - d(f["threshold_applied"]) - d(f["collateral_held"]), Decimal(0))
        assert d(f["amount_called"]) == (called if called >= d(f["min_transfer"]) else Decimal(0))
    threshold = d(s.expected_contradictions[1]["values"]["threshold"])
    assert threshold == Decimal("2500000")
    assert d(first["threshold_applied"]) == 0
    assert d(second["threshold_applied"]) == threshold
    assert d(first["amount_called"]) - d(second["amount_called"]) == threshold
    assert d(second["exposure"]) > threshold
    assert d(s.data["documents"][-1]["payable"]) == d(second["amount_called"])


def test_p5_tax_invoice_aud_and_gst(sc: dict[str, Any]) -> None:
    s = sc["p5-sydney-bp-jet"]
    inv = _att(s, "tax-invoice")[0]["facts"]
    fx = _att(s, "fx-rate-sheet")[0]["facts"]
    total = _invoice_total(inv)
    aud = q2(total * d(inv["fx"]["rate"]))
    assert d(inv["aud_equivalent"]) == aud
    assert d(inv["gst_aud"]) == q2(aud * d(inv["gst_pct"]) / 100) and inv["gst_pct"] == 10
    assert d(inv["total_incl_gst_aud"]) == aud + d(inv["gst_aud"])
    bp_aud = q2(total * d(fx["fx"]["rate"]))
    assert d(fx["aud_equivalent"]) == bp_aud
    assert d(fx["gst_aud"]) == q2(bp_aud * 10 / 100)
    assert inv["fx"]["basis"] != fx["fx"]["basis"]
    assert inv["fx"]["rate"] != fx["fx"]["rate"]


def test_p6_amounts_follow_the_trade(sc: dict[str, Any]) -> None:
    s = sc["p6-london-northgate-gbp-fwd"]
    deal = traded_deals(s)[0]
    t = deal["terms"]
    assert q2(d(t["buy_amount"]) * d(t["rate"])) == d(t["sell_amount"]) == d(s.data["documents"][-1]["payable"])
    for copy in _att(s, "swift-copy"):
        assert d(copy["facts"]["sell_amount"]) == d(t["sell_amount"])
        assert copy["facts"]["value_date"] == t["value_date"]


def test_posted_records(result: BuildResult, upstream: dict[str, Any]) -> None:
    docs = posted(result.universe, upstream)
    assert len(docs) == 6
    for tid, recs in docs.items():
        assert recs
        last = last_doc(tid, result.universe, upstream)
        assert last["payable"] is not None and last["due"] and last["trade_id"] == tid


# --- determinism -----------------------------------------------------------------------------------
def test_post_trade_files_are_byte_identical_on_rebuild(result: BuildResult) -> None:
    first = {k: v for k, v in render_files(result).items() if k.startswith("post-trade/")}
    second = {k: v for k, v in render_files(build()).items() if k.startswith("post-trade/")}
    assert first and first == second
    assert sum(1 for k in first if k.endswith("manifest.json")) == 6
