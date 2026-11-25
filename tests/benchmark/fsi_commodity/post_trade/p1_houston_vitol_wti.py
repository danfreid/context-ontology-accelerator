# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""p1: NewCo Houston buys WTI from Vitol. The invoice charges premium 0.95 against the agreed 0.85; a check sheet and a credit note follow."""

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
    planted,
    register,
    trade_start,
    wait_until,
)
from .docs import (
    assessments,
    bill_of_lading,
    check_sheet,
    confirmation,
    credit_note,
    inspection_certificate,
    invoice,
    nomination,
    priced,
)

SCENARIO_ID = "p1-houston-vitol-wti"
TRADE_SCENARIO = "t1-houston-vitol-wti"
EXPECT = {"emails": 12, "attachments": 9, "chats": 0}
VESSEL = "MT Coastal Meridian"
DISCHARGE_PORT = "Rotterdam"
CERTIFIED_QUANTITY = Decimal("251381.194")  # bbl, shore-tank quantity at the load port, within the agreement tolerance
PLATTS = (Decimal("65.980"), Decimal("66.120"), Decimal("65.950"), Decimal("66.040"), Decimal("65.915"))  # USD per bbl


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    tid, terms = deal["trade_id"], deal["terms"]
    ours, theirs = deal["entity"], deal["counterparty"]
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    ticket = latest_ticket(tid, u, upstream)
    notice_days = next(
        ln["terms"]["nomination_notice_days"]
        for c in upstream[deal["scenario"]].chats
        for ln in c["lines"]
        if "nomination_notice_days" in ln["terms"]
    )
    sc = Scenario(
        SCENARIO_ID,
        "post_trade",
        u,
        trade_start(upstream, deal),
        expect=EXPECT,
        title="Houston buys WTI from Vitol: the invoice charges the recap premium, the check sheet finds it, a credit note fixes it",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    loading_from = date.fromisoformat(terms["loading_from"])
    qty = CERTIFIED_QUANTITY
    unit = terms["unit"]

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
        f"Please find our confirmation of {tid}, built from deal ticket version {ticket['version']}. Sign and return it.",
        conf,
        subject=f"Confirmation {tid}: {terms['product']}",
        tags=["confirmation"],
        doc=cls("TradeTransaction"),
        facts={
            "trade_id": tid,
            "ticket_version": ticket["version"],
            "premium_usd_per_bbl": ticket["facts"]["premium_usd_per_bbl"],
        },
    )
    d.mail(
        "confirmation-signed",
        v("ops"),
        n("ops"),
        f"Signed and returned for {tid}.",
        reply="confirmation",
        days=1,
        tags=["confirmation"],
    )

    wait_until(d, n("ops"), business_date(d, ours, loading_from, -notice_days))
    nom = nomination(
        d,
        filename=f"{tid}-nomination.pdf",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        vessel=VESSEL,
        port=terms["location"],
        laycan_from=loading_from,
        laycan_to=date.fromisoformat(terms["loading_to"]),
        quantity=terms["quantity"],
        unit=unit,
        product=terms["product"],
        notice_date=d.today(),
    )
    d.mail(
        "nomination",
        n("ops"),
        v("ops"),
        f"We nominate {VESSEL} for {tid}, laycan {terms['loading_from']} to {terms['loading_to']}.",
        nom,
        subject=f"Vessel nomination {tid}",
        tags=["logistics", "nomination"],
        doc=cls("TransportMovement"),
        facts={"trade_id": tid, "vessel": VESSEL},
    )
    d.mail(
        "nomination-ack",
        v("ops"),
        n("ops"),
        f"{VESSEL} accepted for {tid}.",
        reply="nomination",
        days=1,
        tags=["logistics"],
    )

    bl_date = business_date(d, ours, loading_from, 3)
    wait_until(d, v("ops"), bl_date)
    bl_number = f"BL-{tid}-001"
    bl = bill_of_lading(
        d,
        number=bl_number,
        filename=f"{tid}-bill-of-lading.pdf",
        trade_id=tid,
        shipper_id=theirs,
        consignee_id=ours,
        notify_id=ours,
        vessel=VESSEL,
        load_port=terms["location"],
        discharge_port=DISCHARGE_PORT,
        bl_date=d.today(),
        product=terms["product"],
        quantity=qty,
        unit=unit,
    )
    bl_issued = d.today()
    d.mail(
        "bill-of-lading",
        v("ops"),
        n("ops"),
        f"Bill of lading {bl_number} for {tid} is attached. Loaded quantity {fmt(qty, 3)} {unit}.",
        bl,
        subject=f"Bill of lading {bl_number}",
        tags=["logistics", "bill-of-lading"],
        doc=cls("BillOfLading"),
        facts={"trade_id": tid, "bl_number": bl_number, "quantity": num(qty)},
    )
    qty_cert = inspection_certificate(
        d,
        filename=f"{tid}-quantity-certificate.pdf",
        kind="quantity-certificate",
        title=f"Certificate of quantity {tid}",
        trade_id=tid,
        cert_date=d.today(),
        place=terms["location"],
        results=[
            ("Product", terms["product"]),
            ("Quantity", f"{fmt(qty, 3)} {unit}"),
            ("Basis", "shore tank, load port"),
        ],
        facts={"quantity": num(qty), "unit": unit, "basis": "load-port", "bl_number": bl_number},
    )
    qual_cert = inspection_certificate(
        d,
        filename=f"{tid}-quality-certificate.pdf",
        kind="quality-certificate",
        title=f"Certificate of quality {tid}",
        trade_id=tid,
        cert_date=d.today(),
        place=terms["location"],
        results=[("Product", terms["product"]), ("API gravity", "39.6"), ("Sulphur, % mass", "0.24")],
        facts={"api_gravity": 39.6, "sulphur_pct": 0.24, "bl_number": bl_number},
    )
    d.mail(
        "certificates",
        "svc-cardinal:inspector",
        n("ops"),
        f"Certificates of quantity and quality for {tid} are attached.",
        qty_cert,
        qual_cert,
        subject=f"Inspection certificates {tid}",
        days=1,
        tags=["logistics", "certificate"],
        doc=cls("SpecifiedInspection"),
        facts={"trade_id": tid, "quantity": num(qty)},
    )
    d.mail(
        "certificates-ack",
        n("ops"),
        v("ops"),
        f"Certificates received for {tid}. Please invoice on the certified quantity.",
        reply="certificates",
        days=1,
        tags=["logistics"],
    )

    # --- invoice -----------------------------------------------------------------------------
    wait_until(d, v("settlements"), business_date(d, ours, bl_date, 2))
    around = [business_date(d, ours, bl_date, k) for k in (-2, -1, 0, 1, 2)]
    assess_att, average = assessments(
        d,
        filename=f"{tid}-platts-assessments.pdf",
        trade_id=tid,
        rows=list(zip(around, PLATTS, strict=True)),
        source="Platts WTI Midland",
    )
    agreed, charged = (
        dec(planted(deal, "recap-differs-from-chat")["agreed"]),
        dec(planted(deal, "recap-differs-from-chat")["found"]),
    )
    due = due_date(d, theirs, bl_issued, deal["payment_days"])
    pay_to = account_for(d, theirs, terms["currency"], onb["accounts"]["counterparty"])
    inv_number = f"VIT-INV-{tid}"
    base_line = priced("WTI Midland, average of five Platts assessments", qty, unit, average)
    prem_line = priced("Premium", qty, unit, charged)
    inv, inv_total = invoice(
        d,
        number=inv_number,
        filename=f"{tid}-invoice.pdf",
        title=f"Invoice {inv_number}",
        trade_id=tid,
        issuer_id=theirs,
        bill_to_id=ours,
        issue_date=d.today(),
        due=due,
        lines=[base_line, prem_line],
        currency=terms["currency"],
        pay_to=pay_to,
        extra=[
            ("Bill of lading", bl_number),
            ("Payment terms", f"{deal['payment_days']} days from bill of lading date"),
        ],
        facts={
            "inputs": {
                "quantity": num(qty),
                "average_price": num(average),
                "premium": num(charged),
                "bl_number": bl_number,
            }
        },
    )
    d.mail(
        "invoice",
        v("settlements"),
        n("accounting"),
        f"Invoice {inv_number} for {tid} is attached with the five Platts assessments. The total is USD {fmt(inv_total)}, due {due.isoformat()}.",
        inv,
        assess_att,
        subject=f"Invoice {inv_number}: {tid}",
        tags=["invoice", "planted"],
        doc=cls("Invoice"),
        facts={
            "trade_id": tid,
            "invoice_number": inv_number,
            "total_due": num(inv_total),
            "premium_usd_per_bbl": num(charged),
        },
    )
    sheet, overcharge = check_sheet(
        d,
        filename=f"{tid}-invoice-check-sheet.pdf",
        trade_id=tid,
        invoice_number=inv_number,
        rows=[
            ("Platts average", base_line.amount, priced("", qty, unit, average).amount),
            ("Premium", prem_line.amount, priced("", qty, unit, agreed).amount),
        ],
        currency=terms["currency"],
        prepared_by=d.person(n("accounting"))["name"],
    )
    d.mail(
        "check",
        n("accounting"),
        n("ops"),
        f"I checked {inv_number} against the deal. The premium is billed at {charged} where the chat settled at {agreed}. The difference is USD {fmt(overcharge)}; the sheet is attached.",
        sheet,
        subject=f"Check sheet {inv_number}",
        days=1,
        tags=["invoice", "check"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "overcharge": num(overcharge)},
    )
    d.mail(
        "dispute",
        n("accounting"),
        v("settlements"),
        f"Your invoice {inv_number} charges a premium of {charged} per {unit}. The agreed premium is {agreed}. Please issue a credit note for USD {fmt(overcharge)}.",
        reply="invoice",
        days=1,
        tags=["invoice", "dispute"],
        facts={"trade_id": tid, "agreed": num(agreed), "charged": num(charged), "overcharge": num(overcharge)},
    )
    credit_line = priced("Premium credit: charged less agreed", qty, unit, charged - agreed)
    cn, net = credit_note(
        d,
        number=f"VIT-CN-{tid}",
        filename=f"{tid}-credit-note.pdf",
        trade_id=tid,
        issuer_id=theirs,
        to_id=ours,
        issue_date=d.today(),
        invoice_number=inv_number,
        invoice_total=inv_total,
        credit=credit_line,
        currency=terms["currency"],
        reason="Premium invoiced above the agreed premium",
        facts={"inputs": {"quantity": num(qty), "charged": num(charged), "agreed": num(agreed)}},
    )
    d.mail(
        "credit-note",
        v("settlements"),
        n("accounting"),
        f"You are right. Credit note {cn.facts['number']} for USD {fmt(credit_line.amount)} is attached. The amount payable is USD {fmt(net)}.",
        cn,
        reply="dispute",
        days=1,
        tags=["invoice", "credit-note"],
        doc=cls("Invoice"),
        facts={"trade_id": tid, "credit_total": num(credit_line.amount), "net_payable": num(net)},
    )
    d.mail(
        "payable",
        n("accounting"),
        n("settlements"),
        f"Pay Vitol USD {fmt(net)} by {due.isoformat()} for {tid}: invoice USD {fmt(inv_total)} less credit note USD {fmt(credit_line.amount)}.",
        subject=f"Payment approval {tid}",
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
        issuer=theirs,
        payer=ours,
        payee=theirs,
        currency=terms["currency"],
        payable=num(net),
        due=due.isoformat(),
        payer_account=onb["accounts"]["newco"],
        payee_account=pay_to,
        inputs={"invoice_total": num(inv_total), "credit": num(credit_line.amount)},
    )
    d.contradiction(
        "invoice-premium-overcharge",
        "invoice-vs-agreed",
        f"Vitol's invoice charges a premium of {charged}, the recap figure; the chat settled at {agreed}. NewCo accounting caught it with the check sheet.",
        ["chat:premium", inv.filename, sheet.filename],
        {"field": "premium_usd_per_bbl", "agreed": num(agreed), "found": num(charged), "overcharge": num(overcharge)},
        True,
        "check",
    )
    sc.outcome = {
        "result": "corrected",
        "trade_id": tid,
        "overcharge": num(overcharge),
        "net_payable": num(net),
        "due": due.isoformat(),
    }
    d.question(
        1, "reconciliation", f"By how much does Vitol's invoice for {tid} overcharge the premium?", num(overcharge)
    )
    d.question(2, "hybrid_retrieval", f"What certified quantity is on the quantity certificate for {tid}?", num(qty))
    d.question(3, "metadata", f"What is the amount payable for {tid} after the credit note?", num(net))
    return sc
