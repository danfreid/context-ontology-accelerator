# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""e1: three weeks of Bloomberg shorthand on the Houston and London desks, then six new deals booked in the ETRM.

History (12 chats, three trader pairs) ends in confirmed RightAngle-style bookings, so each pair's shorthand is
learnable. The same tokens mean different things per pair: ``k`` is thousand bbl for Houston/Vitol but thousand
mt for London/BP and Houston/Shell, and ``+n`` is cents per bbl for crude but dollars per mt for gasoil. Six new deals
follow, one with a trader pair never seen before. Their email confirmations are the system of record. The ETRM
export carries four breaks; ops catches three (side, product, unit), the trader confirms and the ETRM amends them
to version 2. The fourth (a gasoil premium read with the crude price scale) is left open.
"""

# ruff: noqa: E501
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from ..ontology import cls
from ..post_trade.common import Desk, fmt, form
from ..scenario import Attachment, ChatLine, Scenario
from .common import THRESHOLD, ChatDeal, Dialect, booked_terms, breaks, confidence, etrm_csv, etrm_row

SCENARIO_ID = "e1-desk-chats-etrm"
EXPECT = {"emails": 13, "attachments": 11, "chats": 18}

HOU_VIT = Dialect(
    "hou-vitol",
    "newco-hou:trader",
    "cp-vitol:trader",
    "VITOL INC",
    "VITOLINC",
    "bbl",
    Decimal("0.01"),
    {"WTI Mid": ("WTI Midland", "WTIMID"), "Mars": ("Mars blend", "MARS")},
    {"CC": "Corpus Christi", "HOU": "Houston"},
)
HOU_SHL = Dialect(
    "hou-shell",
    "newco-hou:trader",
    "cp-shell:trader",
    "SHELL INTL TRADING",
    "SHELLSTIL",
    "mt",
    Decimal("1"),
    {"HSFO": ("HSFO 3.5%", "HSFO35"), "VLS": ("VLSFO 0.5%", "VLSFO05")},
    {"HOU": "Houston"},
)
LON_BP = Dialect(
    "lon-bp",
    "newco-lon:trader",
    "cp-bp:trader",
    "BP OIL INTL",
    "BPOIL",
    "mt",
    Decimal("1"),
    {"GO": ("Gasoil 0.1%", "GO01"), "Jet": ("Jet A-1", "JETA1")},
    {"ARA": "Amsterdam-Rotterdam-Antwerp"},
)
HOU_GLN = Dialect(
    "hou-glencore",
    "newco-hou:trader",
    "cp-glencore:trader",
    "GLENCORE LTD",
    "GLENCOREUS",
    "bbl",
    Decimal("0.01"),
    {"WTI Mid": ("WTI Midland", "WTIMID")},
    {"CC": "Corpus Christi"},
)
PRODUCT_CODES = {code: name for dl in (HOU_VIT, HOU_SHL, LON_BP, HOU_GLN) for name, code in dl.products.values()}
PLATTS_WTI, ICE_GO = "Platts WTI Midland 5-day average", "ICE gasoil front month"

HISTORY = (
    ChatDeal("NC-HOU-26-0301", HOU_VIT, "buy", "WTI Mid", "CC", 250, "Apr", "fob", 85, index=PLATTS_WTI),
    ChatDeal("NC-HOU-26-0302", HOU_VIT, "sell", "Mars", "HOU", 150, "Apr", "fob", 40, index=PLATTS_WTI),
    ChatDeal("NC-LON-26-0301", LON_BP, "buy", "GO", "ARA", 10, "Apr", "fob", 12, index=ICE_GO),
    ChatDeal("NC-HOU-26-0303", HOU_SHL, "buy", "HSFO", "HOU", 25, "Apr", "fob", fixed=Decimal("438")),
    ChatDeal("NC-HOU-26-0304", HOU_VIT, "buy", "WTI Mid", "CC", 300, "May", "fob", 90, index=PLATTS_WTI),
    ChatDeal("NC-LON-26-0302", LON_BP, "sell", "Jet", "ARA", 15, "May", "fob", 18, index=ICE_GO),
    ChatDeal("NC-HOU-26-0305", HOU_VIT, "sell", "WTI Mid", "HOU", 200, "May", "fob", 70, index=PLATTS_WTI),
    ChatDeal("NC-LON-26-0303", LON_BP, "buy", "GO", "ARA", 12, "May", "fob", 11, index=ICE_GO),
    ChatDeal("NC-HOU-26-0306", HOU_SHL, "sell", "VLS", "HOU", 20, "May", "fob", fixed=Decimal("512.5")),
    ChatDeal("NC-HOU-26-0307", HOU_VIT, "buy", "Mars", "HOU", 100, "Jun", "fob", 35, index=PLATTS_WTI),
    ChatDeal("NC-LON-26-0304", LON_BP, "buy", "GO", "ARA", 8, "Jun", "fob", 14, index=ICE_GO),
    ChatDeal("NC-HOU-26-0308", HOU_VIT, "buy", "WTI Mid", "CC", 250, "Jun", "fob", 80, index=PLATTS_WTI),
)
CURRENT = (
    ChatDeal("NC-HOU-26-0311", HOU_VIT, "buy", "WTI Mid", "CC", 200, "Jul", "fob", 75, index=PLATTS_WTI),
    ChatDeal("NC-HOU-26-0312", HOU_VIT, "buy", "Mars", "HOU", 150, "Jul", "fob", 45, index=PLATTS_WTI),
    ChatDeal("NC-LON-26-0311", LON_BP, "sell", "GO", "ARA", 10, "Jul", "fob", 13, index=ICE_GO),
    ChatDeal("NC-LON-26-0312", LON_BP, "buy", "GO", "ARA", 9, "Jul", "fob", 12, index=ICE_GO),
    ChatDeal("NC-HOU-26-0313", HOU_SHL, "buy", "HSFO", "HOU", 30, "Jul", "fob", fixed=Decimal("441")),
    ChatDeal("NC-HOU-26-0314", HOU_GLN, "buy", "WTI Mid", "CC", 100, "Jul", "fob", 105, index=PLATTS_WTI),
)
# Booking errors in the ETRM (field overrides) and whether ops catches them.
PLANTED = {
    "NC-HOU-26-0312": ({"BuySell": "S"}, True, "side"),
    "NC-LON-26-0311": ({"UOM": "BBL"}, True, "unit"),  # "10k" read with the Houston crude meaning
    "NC-LON-26-0312": ({"Premium": "0.120"}, False, "premium"),  # "+12" read as cents
    "NC-HOU-26-0313": ({"ProductCode": "VLSFO05"}, True, "product"),
}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    start = max(s.clock for s in upstream.values()) + timedelta(days=1)
    sc = Scenario(
        SCENARIO_ID,
        "execution",
        u,
        start,
        expect=EXPECT,
        title="Houston and London desks: Bloomberg shorthand learned per trader pair, ETRM bookings checked against the confirmations",
    )
    d = Desk(sc)
    history: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    numbers = {deal.trade_id: f"RA-{26_000 + i:06d}" for i, deal in enumerate(HISTORY + CURRENT, start=101)}

    def internal(deal: ChatDeal) -> str:
        return {"newco-hou": "NEWCOHOU", "newco-lon": "NEWCOLON"}[deal.dialect.ours.split(":")[0]]

    def trader_code(deal: ChatDeal) -> str:
        p = d.person(deal.dialect.ours)["name"].split()
        return (p[0][0] + p[-1]).upper()

    def chat(deal: ChatDeal, key: str) -> dict[str, Any]:
        lines = [ChatLine(d.addr(ref), text, intent, terms) for ref, text, intent, terms in deal.lines()]
        return sc.chat(
            key,
            lines,
            deal=deal.trade_id,
            channel="bloomberg",
            tags=["bloomberg", deal.dialect.pair],
            class_uri=cls("Order"),
        )

    def export(key: str, batch: list[dict[str, Any]], text: str, **kw: Any) -> Attachment:
        att = Attachment(
            f"rightangle-deal-capture-{sc.clock.date().isoformat()}.csv",
            etrm_csv(batch),
            "etrm-export",
            cls("TradeTransaction"),
            {"system": "RightAngle", "rows": len(batch), "deals": [r["DealNumber"] for r in batch]},
        )
        d.mail(
            key,
            "newco-hou:ops",
            ["newco-hou:trader", "newco-lon:ops"],
            text,
            att,
            tags=["etrm", "export"],
            doc=cls("TradeTransaction"),
            **kw,
        )
        return att

    # --- history: chat, confirmed booking; one export per four deals ---------------------------------------
    batch: list[dict[str, Any]] = []
    for i, deal in enumerate(HISTORY, start=1):
        sc.wait_business_days(d.addr(deal.dialect.ours), 1)
        chat(deal, f"h{i:02d}")
        row = etrm_row(
            deal,
            number=numbers[deal.trade_id],
            version=1,
            date=sc.clock.date().isoformat(),
            trader=trader_code(deal),
            internal=internal(deal),
            status="CONFIRMED",
        )
        batch.append(row)
        history.append(
            {
                "chat": f"h{i:02d}",
                "pair": deal.dialect.pair,
                "trade_id": deal.trade_id,
                "deal_number": row["DealNumber"],
                "tokens": {k: v for k, (_, v) in deal.tokens().items()},
                "terms": deal.terms(),
                "status": "confirmed",
            }
        )
        if i % 4 == 0:
            export(
                f"export-h{i // 4}",
                batch,
                f"RightAngle deal capture export: {len(batch)} deals, all confirmed and matched.",
                subject=f"RightAngle deal capture {sc.clock.date().isoformat()}",
            )
            rows += batch
            batch = []

    # --- current deals: chat, interpretation from history, email confirmation --------------------------------
    interpretations: list[dict[str, Any]] = []
    for j, deal in enumerate(CURRENT, start=1):
        sc.wait_business_days(d.addr(deal.dialect.ours), 0)
        key = f"c{j:02d}"
        chat(deal, key)
        # Only confirmed history counts; every history chat precedes the current ones, and the current deals are
        # not confirmed yet when their chats are read.
        known = [h for h in history if h["status"] == "confirmed"]
        tokens = [
            confidence(tok, meaning, deal.dialect.pair, known) | {"field": field}
            for tok, (field, meaning) in deal.tokens().items()
        ]
        interpretations.append(
            {
                "chat": key,
                "trade_id": deal.trade_id,
                "pair": deal.dialect.pair,
                "terms": deal.terms(),
                "tokens": tokens,
                "decision": "auto" if all(t["decision"] == "auto" for t in tokens) else "review",
            }
        )
    confirmations: dict[str, dict[str, Any]] = {}
    for j, deal in enumerate(CURRENT, start=1):
        ours, theirs = deal.dialect.ours.split(":")[0], deal.dialect.theirs.split(":")[0]
        issuer = theirs if deal.dialect.pair == "lon-bp" else ours  # BP confirms on its paper
        seller, buyer = (theirs, ours) if deal.side == "buy" else (ours, theirs)
        t = deal.terms()
        price = (
            f"USD {deal.fixed:.3f} per {deal.dialect.unit} fixed"
            if deal.fixed is not None
            else f"{deal.index} plus USD {deal.premium:.3f} per {deal.dialect.unit}"
        )
        conf = form(
            f"{deal.trade_id}-confirmation.pdf",
            "trade-confirmation",
            f"Trade confirmation {deal.trade_id}",
            [
                ("Trade id", deal.trade_id),
                ("Seller", d.org(seller)["name"]),
                ("Buyer", d.org(buyer)["name"]),
                ("Product", deal.product),
                ("Quantity", f"{fmt(deal.quantity, 0)} {deal.dialect.unit}"),
                ("Delivery", f"{deal.incoterm.upper()} {deal.location}, {deal.month} 2026"),
                ("Price", price),
                (
                    "Agreed",
                    f"Bloomberg chat between {d.person(deal.dialect.ours)['name']} and {d.person(deal.dialect.theirs)['name']}",
                ),
            ],
            cls("TradeTransaction"),
            {"trade_id": deal.trade_id, **t, "seller": seller, "buyer": buyer},
        )
        frm, to = (f"{issuer}:ops", f"{ours if issuer == theirs else theirs}:ops")
        d.mail(
            f"confirm-{j}",
            frm,
            to,
            f"Confirmation of {deal.trade_id} as agreed on Bloomberg today: {deal.product}, {fmt(deal.quantity, 0)} {deal.dialect.unit}.",
            conf,
            subject=f"Confirmation {deal.trade_id}",
            tags=["confirmation"],
            doc=cls("TradeTransaction"),
        )
        confirmations[deal.trade_id] = {"message": f"confirm-{j}", "issuer": issuer, **t}

    # --- the ETRM bookings, with planted breaks -----------------------------------------------------------------
    current_rows = []
    for deal in CURRENT:
        overrides = PLANTED.get(deal.trade_id, ({}, False, ""))[0]
        current_rows.append(
            etrm_row(
                deal,
                number=numbers[deal.trade_id],
                version=1,
                date=sc.clock.date().isoformat(),
                trader=trader_code(deal),
                internal=internal(deal),
                status="ENTERED",
                overrides=overrides,
            )
        )
    export(
        "export-current",
        current_rows,
        f"RightAngle deal capture export: {len(current_rows)} new deals entered today.",
        subject=f"RightAngle deal capture {sc.clock.date().isoformat()}",
        days=1,
    )
    rows += current_rows

    found: dict[str, list[dict[str, Any]]] = {}
    for row in current_rows:
        diff = breaks(confirmations[row["ExternalRef"]], row, PRODUCT_CODES)
        if diff:
            found[row["ExternalRef"]] = diff
    caught = [tid for tid, (_, c, _) in PLANTED.items() if c]
    lines = "\n".join(
        f"- {tid} ({numbers[tid]}): {b['field']} is {b['etrm']} in RightAngle, {b['confirmation']} on the confirmation"
        for tid in caught
        for b in found[tid]
    )
    d.mail(
        "breaks",
        "newco-hou:ops",
        ["newco-hou:trader", "newco-lon:trader"],
        f"These bookings differ from the email confirmations, which are the binding record. Please confirm and I will amend:\n{lines}",
        subject="RightAngle breaks against confirmations",
        tags=["etrm", "break"],
        cc=["newco-lon:ops"],
    )
    d.mail(
        "breaks-confirmed",
        "newco-hou:trader",
        "newco-hou:ops",
        "Confirmed: the confirmations are right on all three. Amend the bookings.",
        reply="breaks",
        days=0,
        tags=["etrm", "break"],
    )
    amended = []
    for row in current_rows:
        if row["ExternalRef"] in caught:
            fix = {b["field"]: b for b in found[row["ExternalRef"]]}
            binding = confirmations[row["ExternalRef"]]
            fixed = {
                **row,
                "Version": 2,
                "DealStatus": "AMENDED",
                "ChangeReason": f"Corrected to confirmation {row['ExternalRef']}: " + ", ".join(fix),
            }
            fixed["BuySell"] = "B" if binding["side"] == "buy" else "S"
            fixed["UOM"] = binding["unit"].upper()
            fixed["ProductCode"] = next(code for code, name in PRODUCT_CODES.items() if name == binding["product"])
            amended.append(fixed)
    export(
        "export-amended",
        amended,
        f"RightAngle amendments: {len(amended)} deals corrected to their confirmations (version 2).",
        subject=f"RightAngle amendments {sc.clock.date().isoformat()}",
        reply="breaks",
    )
    rows += amended

    remaining = {tid: diff for tid, diff in found.items() if tid not in caught}
    for a in amended:
        assert not breaks(confirmations[a["ExternalRef"]], a, PRODUCT_CODES)
    for tid, (_, was_caught, field) in PLANTED.items():
        d.contradiction(
            f"etrm-{field}-{tid}",
            "etrm-vs-confirmation",
            f"RightAngle books {tid} with a different {field} from the email confirmation.",
            [f"{tid}-confirmation.pdf", f"rightangle-deal-capture-{rows[0]['DealDate']}.csv"],
            {"field": field, "confirmation": found[tid][0]["confirmation"], "etrm": found[tid][0]["etrm"]},
            was_caught,
            "breaks" if was_caught else None,
        )

    sc.execution = {
        "threshold": THRESHOLD,
        "system_of_record": "email confirmation",
        "dialects": [
            {
                "pair": dl.pair,
                "ours": d.addr(dl.ours),
                "theirs": d.addr(dl.theirs),
                "firm": dl.firm,
                "counterparty": dl.theirs.split(":")[0],
                "etrm_ba": dl.ba_code,
                "k_unit": dl.unit,
                "price_scale": float(dl.price_scale),
                "products": {a: n for a, (n, _) in dl.products.items()},
                "locations": dict(dl.locations),
            }
            for dl in (HOU_VIT, HOU_SHL, LON_BP, HOU_GLN)
        ],
        "product_codes": PRODUCT_CODES,
        "history": history,
        "interpretations": interpretations,
        "confirmations": confirmations,
        "etrm_rows": rows,
        "breaks": [
            {
                "trade_id": tid,
                "deal_number": numbers[tid],
                "fields": diff,
                "caught": tid in caught,
                "status": "remediated" if tid in caught else "open",
                "remediation_version": 2 if tid in caught else None,
            }
            for tid, diff in found.items()
        ],
    }
    assert set(found) == set(PLANTED) and set(remaining) == {"NC-LON-26-0312"}

    review = [i["chat"] for i in interpretations if i["decision"] == "review"]
    k_lon = next(t for t in interpretations[2]["tokens"] if t["token"] == "qty:k")
    p_lon = next(t for t in interpretations[3]["tokens"] if t["token"] == "price:+n")
    d.question(
        1,
        "reconciliation",
        "Which deals in the RightAngle export differ from their email confirmations?",
        sorted(found),
    )
    d.question(
        2, "reconciliation", "Which RightAngle breaks against the confirmations are still open?", sorted(remaining)
    )
    d.question(
        3,
        "reconciliation",
        "For NC-HOU-26-0312, what side does the email confirmation state and what side did RightAngle book first?",
        [confirmations["NC-HOU-26-0312"]["side"], booked_terms(current_rows[1], PRODUCT_CODES)["side"]],
    )
    d.question(
        4,
        "hybrid_retrieval",
        "In the London desk's Bloomberg chats with BP, what unit does a quantity like 10k mean?",
        k_lon["meaning"],
    )
    d.question(
        5,
        "hybrid_retrieval",
        "In the Houston desk's Bloomberg chats with Vitol, what premium in USD per bbl does +85 mean?",
        0.85,
    )
    d.question(
        6,
        "graph_traversal",
        "Which earlier confirmed chats support reading +12 as USD 12.00 per mt in the London/BP chat for NC-LON-26-0312?",
        p_lon["evidence"],
    )
    d.question(7, "metadata", "Which new Bloomberg chats should go to review before booking?", review)
    d.question(
        8,
        "metadata",
        "How many versions of the RightAngle deal for NC-LON-26-0311 exist?",
        sum(1 for r in rows if r["ExternalRef"] == "NC-LON-26-0311"),
    )
    d.question(
        9, "hybrid_retrieval", "Which counterparty is the Bloomberg firm SHELL INTL TRADING?", d.org("cp-shell")["name"]
    )
    return sc
