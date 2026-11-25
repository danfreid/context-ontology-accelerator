# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""p3: NewCo Sydney sells ULP 91 CFR Newcastle to Harbourline. The invoice uses the B/L quantity where the agreement says discharge quantity."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..ontology import cls
from ..scenario import Scenario
from .common import (
    Desk,
    account_for,
    business_date,
    deal_of,
    dec,
    due_date,
    fmt,
    latest_ticket,
    num,
    onboarded,
    register,
    trade_start,
    wait_until,
)
from .docs import (
    bill_of_lading,
    confirmation,
    credit_note,
    inspection_certificate,
    invoice,
    letter_of_indemnity,
    nomination,
    priced,
)

SCENARIO_ID = "p3-sydney-harbourline-ulp"
TRADE_SCENARIO = "t3-sydney-harbourline-ulp"
EXPECT = {"emails": 10, "attachments": 7, "chats": 0}
VESSEL = "MT Southern Pelican"
LOAD_PORT = "Singapore"
BL_QUANTITY = Decimal("19996.500")  # mt, loaded
DISCHARGE_QUANTITY = Decimal("19874.250")  # mt, certified at Newcastle
LOI_COVER_PCT = 125


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(
        u, upstream, TRADE_SCENARIO, 0
    )  # the first cargo; the resized second cargo is not shipped in this corpus
    tid, terms = deal["trade_id"], deal["terms"]
    ours, theirs = deal["entity"], deal["counterparty"]
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    basis = onb["clauses"]["quantity_basis"]["basis"]
    ticket = latest_ticket(tid, u, upstream)
    sc = Scenario(
        SCENARIO_ID,
        "post_trade",
        u,
        trade_start(upstream, deal, 2),
        expect=EXPECT,
        title="Sydney sells ULP 91 to Harbourline: letter of indemnity for a late B/L, invoice on the wrong quantity basis, credit note",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    unit, price, currency = terms["unit"], dec(terms["price_usd_per_mt"]), terms["currency"]
    delivery_from = date.fromisoformat(terms["delivery_from"])
    pay_to = account_for(d, ours, currency, onb["accounts"]["newco"])
    buyer_bank = d.u["accounts"][onb["accounts"]["counterparty"]]["bank"]

    conf = confirmation(
        d,
        filename=f"{tid}-confirmation.pdf",
        title=f"Trade confirmation {tid}",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        confirm_date=d.today(),
        terms=ticket["facts"],
        based_on=ticket["filename"],
        class_uri=cls("TradeTransaction"),
    )
    d.mail(
        "confirmation",
        n("ops"),
        v("ops"),
        f"Our confirmation of {tid} is attached, built from deal ticket version {ticket['version']}.",
        conf,
        subject=f"Confirmation {tid}: {terms['product']}",
        tags=["confirmation"],
        doc=cls("TradeTransaction"),
        facts={"trade_id": tid, "ticket_version": ticket["version"], "quantity": terms["quantity"]},
    )
    wait_until(d, n("ops"), business_date(d, ours, delivery_from, -12))
    nom = nomination(
        d,
        filename=f"{tid}-nomination.pdf",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        vessel=VESSEL,
        port=terms["location"],
        laycan_from=delivery_from,
        laycan_to=date.fromisoformat(terms["delivery_to"]),
        quantity=terms["quantity"],
        unit=unit,
        product=terms["product"],
        notice_date=d.today(),
    )
    bl_date = business_date(d, ours, delivery_from, -10)
    # The nomination goes out; the B/L is issued at the load port shortly after.
    d.mail(
        "nomination",
        n("ops"),
        v("ops"),
        f"We nominate {VESSEL} for {tid}, delivery window {terms['delivery_from']} to {terms['delivery_to']}.",
        nom,
        subject=f"Vessel nomination {tid}",
        tags=["logistics", "nomination"],
        doc=cls("TransportMovement"),
        facts={"trade_id": tid, "vessel": VESSEL},
    )
    wait_until(d, n("ops"), bl_date)
    bl_number = f"BL-{tid}-001"
    bl = bill_of_lading(
        d,
        number=bl_number,
        filename=f"{tid}-bill-of-lading.pdf",
        trade_id=tid,
        shipper_id=ours,
        consignee_id=theirs,
        notify_id=theirs,
        vessel=VESSEL,
        load_port=LOAD_PORT,
        discharge_port=terms["location"],
        bl_date=d.today(),
        product=terms["product"],
        quantity=BL_QUANTITY,
        unit=unit,
    )
    bl_issued = d.today()
    d.mail(
        "bill-of-lading",
        n("ops"),
        v("ops"),
        f"A copy of bill of lading {bl_number} is attached. Loaded quantity {fmt(BL_QUANTITY, 3)} {unit}.",
        bl,
        subject=f"Bill of lading {bl_number}",
        tags=["logistics", "bill-of-lading"],
        doc=cls("BillOfLading"),
        facts={"trade_id": tid, "bl_number": bl_number, "quantity": num(BL_QUANTITY)},
    )
    d.mail(
        "loi-request",
        n("ops"),
        v("ops"),
        f"The originals of {bl_number} will arrive after the vessel. To discharge on time, please send a letter of indemnity counter-signed by your bank for {LOI_COVER_PCT}% of the cargo value.",
        reply="bill-of-lading",
        days=2,
        tags=["logistics", "loi"],
        facts={"trade_id": tid, "cover_pct": LOI_COVER_PCT},
    )
    wait_until(d, v("ops"), business_date(d, ours, delivery_from, -1))
    loi = letter_of_indemnity(
        d,
        filename=f"{tid}-letter-of-indemnity.pdf",
        trade_id=tid,
        issuer_id=theirs,
        to_id=ours,
        bank_id=buyer_bank,
        bl_number=bl_number,
        vessel=VESSEL,
        cargo_quantity=BL_QUANTITY,
        unit=unit,
        price=price,
        cover_pct=LOI_COVER_PCT,
        currency=currency,
        issue_date=d.today(),
    )
    d.mail(
        "loi",
        v("ops"),
        n("ops"),
        f"The letter of indemnity for {bl_number}, counter-signed by {d.org(buyer_bank)['name']} for {LOI_COVER_PCT}% of the cargo value, is attached.",
        loi,
        reply="loi-request",
        tags=["logistics", "loi"],
        doc=cls("BilateralContract"),
        facts={"trade_id": tid, "indemnity_amount": loi.facts["indemnity_amount"], "cover_pct": LOI_COVER_PCT},
    )
    wait_until(d, "svc-cardinal:inspector", date.fromisoformat(terms["delivery_to"]))
    cert = inspection_certificate(
        d,
        filename=f"{tid}-discharge-certificate.pdf",
        kind="discharge-certificate",
        title=f"Certificate of discharge {tid}",
        trade_id=tid,
        cert_date=d.today(),
        place=terms["location"],
        results=[
            ("Product", terms["product"]),
            ("Discharged quantity", f"{fmt(DISCHARGE_QUANTITY, 3)} {unit}"),
            ("Basis", "shore tank, discharge port"),
        ],
        facts={"quantity": num(DISCHARGE_QUANTITY), "unit": unit, "basis": "discharge", "bl_number": bl_number},
    )
    d.mail(
        "discharge-certificate",
        "svc-cardinal:inspector",
        n("ops"),
        f"The certificate of discharge for {tid} is attached: {fmt(DISCHARGE_QUANTITY, 3)} {unit}.",
        cert,
        subject=f"Discharge certificate {tid}",
        tags=["logistics", "certificate"],
        doc=cls("SpecifiedInspection"),
        facts={"trade_id": tid, "quantity": num(DISCHARGE_QUANTITY)},
    )

    # --- invoice on the wrong basis -------------------------------------------------------------
    wait_until(d, n("accounting"), business_date(d, ours, date.fromisoformat(terms["delivery_to"]), 2))
    due = due_date(d, ours, bl_issued, deal["payment_days"])
    inv_number = f"NCS-INV-{tid}"
    inv_line = priced(
        f"{terms['product']}, {terms['incoterm']} {terms['location']}, bill of lading quantity",
        BL_QUANTITY,
        unit,
        price,
    )
    inv, inv_total = invoice(
        d,
        number=inv_number,
        filename=f"{tid}-invoice.pdf",
        title=f"Invoice {inv_number}",
        trade_id=tid,
        issuer_id=ours,
        bill_to_id=theirs,
        issue_date=d.today(),
        due=due,
        lines=[inv_line],
        currency=currency,
        pay_to=pay_to,
        extra=[
            ("Bill of lading", bl_number),
            ("Payment terms", f"{deal['payment_days']} days from bill of lading date"),
        ],
        facts={"inputs": {"quantity": num(BL_QUANTITY), "price": num(price), "basis": "bill-of-lading"}},
    )
    d.mail(
        "invoice",
        n("accounting"),
        v("accounting"),
        f"Invoice {inv_number} for {tid} is attached: USD {fmt(inv_total)}, due {due.isoformat()}.",
        inv,
        subject=f"Invoice {inv_number}: {tid}",
        tags=["invoice", "planted"],
        doc=cls("Invoice"),
        facts={
            "trade_id": tid,
            "invoice_number": inv_number,
            "total_due": num(inv_total),
            "quantity": num(BL_QUANTITY),
        },
    )
    d.mail(
        "basis-query",
        v("accounting"),
        n("accounting"),
        f"The agreement sets the quantity basis as {basis}. The discharge certificate says {fmt(DISCHARGE_QUANTITY, 3)} {unit}, but the invoice uses {fmt(BL_QUANTITY, 3)} {unit}. Please reissue on the discharge quantity.",
        reply="invoice",
        days=1,
        tags=["invoice", "dispute"],
        facts={
            "trade_id": tid,
            "basis": basis,
            "invoiced_quantity": num(BL_QUANTITY),
            "discharge_quantity": num(DISCHARGE_QUANTITY),
        },
    )
    credit_line = priced(
        "Quantity basis correction: B/L less discharge quantity", BL_QUANTITY - DISCHARGE_QUANTITY, unit, price
    )
    cn, net = credit_note(
        d,
        number=f"NCS-CN-{tid}",
        filename=f"{tid}-credit-note.pdf",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        issue_date=d.today(),
        invoice_number=inv_number,
        invoice_total=inv_total,
        credit=credit_line,
        currency=currency,
        reason=f"Invoice corrected to the {basis} quantity",
        facts={
            "inputs": {
                "bl_quantity": num(BL_QUANTITY),
                "discharge_quantity": num(DISCHARGE_QUANTITY),
                "price": num(price),
            }
        },
    )
    d.mail(
        "credit-note",
        n("accounting"),
        v("accounting"),
        f"Agreed. Credit note {cn.facts['number']} for USD {fmt(credit_line.amount)} is attached, which brings the invoice to USD {fmt(net)}.",
        cn,
        reply="basis-query",
        days=1,
        tags=["invoice", "credit-note"],
        doc=cls("Invoice"),
        facts={"trade_id": tid, "credit_total": num(credit_line.amount), "net_payable": num(net)},
    )
    d.mail(
        "payable",
        n("accounting"),
        n("settlements"),
        f"Expect USD {fmt(net)} from Harbourline by {due.isoformat()} for {tid}.",
        subject=f"Expected receipt {tid}",
        days=1,
        tags=["payment"],
        facts={"trade_id": tid, "net_payable": num(net), "due": due.isoformat()},
    )
    register(
        sc,
        trade_id=tid,
        kind="credit-note",
        filename=cn.filename,
        message="credit-note",
        number=cn.facts["number"],
        issuer=ours,
        payer=theirs,
        payee=ours,
        currency=currency,
        payable=num(net),
        due=due.isoformat(),
        payer_account=account_for(d, theirs, currency, onb["accounts"]["counterparty"]),
        payee_account=pay_to,
        inputs={"invoice_total": num(inv_total), "credit": num(credit_line.amount)},
    )
    d.contradiction(
        "invoice-wrong-quantity-basis",
        "invoice-vs-agreement",
        f"The invoice uses the bill of lading quantity {fmt(BL_QUANTITY, 3)} {unit}; the agreement's quantity basis is {basis}, certified at {fmt(DISCHARGE_QUANTITY, 3)} {unit}.",
        [inv.filename, cert.filename, deal["agreement"]],
        {"field": "quantity", "agreed": num(DISCHARGE_QUANTITY), "found": num(BL_QUANTITY), "basis": basis},
        True,
        "basis-query",
    )
    sc.outcome = {
        "result": "corrected",
        "trade_id": tid,
        "credit": num(credit_line.amount),
        "net_payable": num(net),
        "due": due.isoformat(),
    }
    d.question(
        1, "reconciliation", f"On what quantity should the invoice for {tid} have been issued?", num(DISCHARGE_QUANTITY)
    )
    d.question(
        2,
        "hybrid_retrieval",
        f"What cover does the letter of indemnity for {bl_number} give, as a percentage of cargo value?",
        LOI_COVER_PCT,
    )
    d.question(3, "metadata", f"What is the amount payable for {tid} after the credit note?", num(net))
    return sc
