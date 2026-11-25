# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""p5: NewCo Sydney sells Jet A-1 CIF Brisbane to BP Australia. A tax invoice with an AUD equivalent and GST; BP disputes the FX rate date."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..onboarding.common import form
from ..ontology import cls
from ..scenario import Scenario
from ..writers.pdf_writer import Grid
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
    q2,
    register,
    trade_start,
    wait_until,
)
from .docs import bill_of_lading, confirmation, inspection_certificate, invoice, nomination, priced

SCENARIO_ID = "p5-sydney-bp-jet"
TRADE_SCENARIO = "t5-sydney-bp-jet"
EXPECT = {"emails": 10, "attachments": 7, "chats": 0}
VESSEL = "MT Pacific Tern"
LOAD_PORT = "Singapore"
LOADED_QUANTITY = Decimal("30012.500")  # mt, certified at the load port
GST_PCT = 10
FX_AT_BL_DATE = Decimal("1.5410")  # AUD per USD on the bill of lading date, used by NewCo
FX_AT_INVOICE_DATE = Decimal("1.5365")  # AUD per USD on the invoice date, asked for by BP


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    tid, terms = deal["trade_id"], deal["terms"]
    ours, theirs = deal["entity"], deal["counterparty"]
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    ticket = latest_ticket(tid, u, upstream)
    sc = Scenario(
        SCENARIO_ID,
        "post_trade",
        u,
        trade_start(upstream, deal),
        expect=EXPECT,
        title="Sydney sells Jet A-1 to BP Australia: tax invoice with AUD equivalent and GST, FX rate date in dispute",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    unit, price, currency = terms["unit"], dec(terms["price_usd_per_mt"]), terms["currency"]
    delivery_from = date.fromisoformat(terms["delivery_from"])
    qty = LOADED_QUANTITY
    pay_to = account_for(d, ours, currency, onb["accounts"]["newco"])

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
        f"Our confirmation of {tid} is attached, built from deal ticket version {ticket['version']}: {terms['incoterm']} {terms['location']}.",
        conf,
        subject=f"Confirmation {tid}: {terms['product']}",
        tags=["confirmation"],
        doc=cls("TradeTransaction"),
        facts={"trade_id": tid, "ticket_version": ticket["version"], "incoterm": ticket["facts"]["incoterm"]},
    )
    wait_until(d, n("ops"), business_date(d, ours, delivery_from, -14))
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
    d.mail(
        "nomination",
        n("ops"),
        v("ops"),
        f"We nominate {VESSEL} for {tid}.",
        nom,
        subject=f"Vessel nomination {tid}",
        tags=["logistics", "nomination"],
        doc=cls("TransportMovement"),
        facts={"trade_id": tid, "vessel": VESSEL},
    )
    bl_date = business_date(d, ours, delivery_from, -8)
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
        quantity=qty,
        unit=unit,
    )
    bl_issued = d.today()
    d.mail(
        "bill-of-lading",
        n("ops"),
        v("ops"),
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
        place=LOAD_PORT,
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
        place=LOAD_PORT,
        results=[("Product", terms["product"]), ("Flash point, C", "41"), ("Freezing point, C", "-49.5")],
        facts={"flash_point_c": 41, "freezing_point_c": -49.5, "bl_number": bl_number},
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
        "certificates-forward",
        n("ops"),
        n("accounting"),
        f"Certified quantity for {tid} is {fmt(qty, 3)} {unit}. Please invoice.",
        reply="certificates",
        days=1,
        tags=["logistics"],
    )

    # --- tax invoice with AUD equivalent and GST ---------------------------------------------
    wait_until(d, n("accounting"), business_date(d, ours, bl_date, 2))
    due = due_date(d, ours, bl_issued, deal["payment_days"])
    inv_number = f"NCS-TAX-{tid}"
    line = priced(f"{terms['product']}, {terms['incoterm']} {terms['location']}", qty, unit, price)
    usd_total = line.amount
    aud_equivalent = q2(usd_total * FX_AT_BL_DATE)
    gst_aud = q2(aud_equivalent * GST_PCT / 100)
    inv, inv_total = invoice(
        d,
        number=inv_number,
        filename=f"{tid}-tax-invoice.pdf",
        title=f"Tax invoice {inv_number}",
        trade_id=tid,
        issuer_id=ours,
        bill_to_id=theirs,
        issue_date=d.today(),
        due=due,
        lines=[line],
        currency=currency,
        pay_to=pay_to,
        extra=[
            ("Bill of lading", bl_number),
            ("Payment terms", f"{deal['payment_days']} days from bill of lading date"),
            (
                "FX rate used (AUD per USD)",
                f"{FX_AT_BL_DATE:.4f}, rate of {bl_issued.isoformat()} (bill of lading date)",
            ),
            ("AUD equivalent of the supply", f"AUD {fmt(aud_equivalent)}"),
            (f"GST at {GST_PCT}% (AUD)", fmt(gst_aud)),
            ("Total including GST (AUD)", fmt(aud_equivalent + gst_aud)),
        ],
        notes=[
            "GST is stated in Australian dollars and settled in Australian dollars; the USD price is payable in USD."
        ],
        facts={
            "inputs": {"quantity": num(qty), "price": num(price)},
            "fx": {
                "rate": num(FX_AT_BL_DATE),
                "rate_date": bl_issued.isoformat(),
                "basis": "bill-of-lading-date",
                "pair": "AUD per USD",
            },
            "aud_equivalent": num(aud_equivalent),
            "gst_pct": GST_PCT,
            "gst_aud": num(gst_aud),
            "total_incl_gst_aud": num(aud_equivalent + gst_aud),
        },
        kind="tax-invoice",
    )
    d.mail(
        "invoice",
        n("accounting"),
        v("accounting"),
        f"Tax invoice {inv_number} for {tid} is attached: USD {fmt(inv_total)}, AUD equivalent {fmt(aud_equivalent)} at {FX_AT_BL_DATE:.4f}, GST AUD {fmt(gst_aud)}. Due {due.isoformat()}.",
        inv,
        subject=f"Tax invoice {inv_number}: {tid}",
        tags=["invoice", "tax-invoice", "fx"],
        doc=cls("Invoice"),
        facts={"trade_id": tid, "invoice_number": inv_number, "total_due": num(inv_total), "gst_aud": num(gst_aud)},
    )
    bp_aud = q2(usd_total * FX_AT_INVOICE_DATE)
    bp_gst = q2(bp_aud * GST_PCT / 100)
    fx_sheet = form(
        f"{tid}-bp-fx-rate-sheet.pdf",
        "fx-rate-sheet",
        f"FX rate calculation for {inv_number}",
        [
            ("Prepared by", d.org(theirs)["name"]),
            ("Trade id", tid),
            ("Rate basis", "invoice date"),
            ("Rate date", d.today().isoformat()),
            ("Rate (AUD per USD)", f"{FX_AT_INVOICE_DATE:.4f}"),
            ("AUD equivalent", f"AUD {fmt(bp_aud)}"),
            (f"GST at {GST_PCT}% (AUD)", fmt(bp_gst)),
        ],
        cls("MonetaryAmount"),
        {
            "trade_id": tid,
            "invoice_number": inv_number,
            "fx": {
                "rate": num(FX_AT_INVOICE_DATE),
                "rate_date": d.today().isoformat(),
                "basis": "invoice-date",
                "pair": "AUD per USD",
            },
            "usd_total": num(usd_total),
            "aud_equivalent": num(bp_aud),
            "gst_pct": GST_PCT,
            "gst_aud": num(bp_gst),
        },
        grid=Grid(
            ("Basis", "Rate", "AUD equivalent", "GST"),
            (
                ("Bill of lading date (NewCo)", f"{FX_AT_BL_DATE:.4f}", fmt(aud_equivalent), fmt(gst_aud)),
                ("Invoice date (BP)", f"{FX_AT_INVOICE_DATE:.4f}", fmt(bp_aud), fmt(bp_gst)),
            ),
        ),
    )
    d.mail(
        "fx-query",
        v("accounting"),
        n("accounting"),
        f"Our tax rules take the rate on the invoice date. At {FX_AT_INVOICE_DATE:.4f} the AUD equivalent is {fmt(bp_aud)} and GST {fmt(bp_gst)}, not the figures on your invoice. Our calculation is attached.",
        fx_sheet,
        reply="invoice",
        days=1,
        tags=["invoice", "fx", "dispute"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "rate": num(FX_AT_INVOICE_DATE), "basis": "invoice-date"},
    )
    d.mail(
        "fx-reply",
        n("accounting"),
        v("accounting"),
        f"We used the rate of {bl_issued.isoformat()}, the bill of lading date, as on our other supplies. The difference in GST is AUD {fmt(gst_aud - bp_gst)}. Please confirm which date applies.",
        reply="fx-query",
        days=1,
        tags=["invoice", "fx", "dispute"],
        facts={
            "trade_id": tid,
            "rate": num(FX_AT_BL_DATE),
            "basis": "bill-of-lading-date",
            "gst_difference_aud": num(gst_aud - bp_gst),
        },
    )
    d.mail(
        "fx-open",
        v("accounting"),
        n("accounting"),
        f"We will pay the USD invoice by {due.isoformat()}. The GST amount waits until the rate date is settled.",
        reply="fx-reply",
        days=1,
        tags=["invoice", "fx", "payment"],
        facts={"trade_id": tid, "usd_payable": num(inv_total)},
    )
    d.mail(
        "payable",
        n("accounting"),
        n("settlements"),
        f"Expect USD {fmt(inv_total)} from BP Australia by {due.isoformat()} for {tid}. The GST rate date is open.",
        subject=f"Expected receipt {tid}",
        days=1,
        tags=["payment"],
        facts={"trade_id": tid, "usd_payable": num(inv_total), "due": due.isoformat()},
    )
    register(
        sc,
        trade_id=tid,
        kind="tax-invoice",
        filename=inv.filename,
        message="invoice",
        number=inv_number,
        issuer=ours,
        payer=theirs,
        payee=ours,
        currency=currency,
        payable=num(inv_total),
        due=due.isoformat(),
        payer_account=account_for(d, theirs, currency, onb["accounts"]["counterparty"]),
        payee_account=pay_to,
        inputs={"aud_equivalent": num(aud_equivalent), "gst_aud": num(gst_aud), "fx_rate": num(FX_AT_BL_DATE)},
    )
    d.contradiction(
        "fx-rate-date-dispute",
        "invoice-vs-counterparty",
        f"NewCo converts at the bill of lading date rate {FX_AT_BL_DATE:.4f}; BP converts at the invoice date rate {FX_AT_INVOICE_DATE:.4f}. The GST differs by AUD {fmt(gst_aud - bp_gst)}. Not settled in the corpus.",
        [inv.filename, fx_sheet.filename],
        {
            "field": "fx_rate",
            "agreed": None,
            "found": num(FX_AT_BL_DATE),
            "counterparty_value": num(FX_AT_INVOICE_DATE),
        },
        True,
        "fx-query",
    )
    sc.outcome = {
        "result": "open-dispute",
        "trade_id": tid,
        "usd_payable": num(inv_total),
        "gst_aud": num(gst_aud),
        "gst_aud_bp": num(bp_gst),
        "due": due.isoformat(),
    }
    d.question(
        1,
        "reconciliation",
        f"Which FX rates do NewCo and BP apply to the invoice for {tid}?",
        [num(FX_AT_BL_DATE), num(FX_AT_INVOICE_DATE)],
    )
    d.question(2, "reconciliation", f"What is the GST in AUD on NewCo's tax invoice for {tid}?", num(gst_aud))
    d.question(3, "metadata", f"What is the USD total on the tax invoice for {tid}?", num(inv_total))
    return sc
