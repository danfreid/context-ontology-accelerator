# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""p2: NewCo London sells VLSFO to Kestrel against a prepayment. The final invoice is trued up; a quantity query is answered by the tolerance."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date, timedelta
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
    q2,
    register,
    trade_start,
    wait_until,
)
from .docs import InvLine, bill_of_lading, confirmation, credit_advice, inspection_certificate, invoice, nomination

SCENARIO_ID = "p2-london-kestrel-vlsfo"
TRADE_SCENARIO = "t2-london-kestrel-vlsfo"
EXPECT = {"emails": 10, "attachments": 8, "chats": 0}
VESSEL = "MT Nordic Harrier"
DISCHARGE_PORT = "Fujairah"
LOADED_QUANTITY = Decimal("8085.000")  # mt, certified at the load port
INSPECTION_FEE = Decimal("1122.80")  # USD, total on the certificate; each party pays half
FEE_SHARE_PCT = 50


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    tid, terms = deal["trade_id"], deal["terms"]
    ours, theirs = deal["entity"], deal["counterparty"]
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    ticket = latest_ticket(tid, u, upstream)
    prepay = deal["prepayment"]
    sc = Scenario(
        SCENARIO_ID,
        "post_trade",
        u,
        trade_start(upstream, deal),
        expect=EXPECT,
        title="London sells VLSFO to Kestrel: prepayment credited, final invoice trued up, quantity query answered by the tolerance",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    unit, price, currency = terms["unit"], dec(terms["price_usd_per_mt"]), terms["currency"]
    contract_qty = dec(terms["quantity"])
    qty = LOADED_QUANTITY
    prepaid = dec(prepay["amount"])
    tolerance = dec(deal["tolerance_pct"])
    loading_from = date.fromisoformat(terms["loading_from"])
    own_account = account_for(d, ours, currency, prepay["account"])
    bank_mail = f"{u['accounts'][own_account]['bank']}:operations"

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
    proforma_lines = [InvLine("Prepayment of 100% of cargo value", prepaid)]
    proforma, proforma_total = invoice(
        d,
        number=f"PRO-{tid}",
        filename=f"{tid}-prepayment-invoice.pdf",
        title=f"Prepayment invoice PRO-{tid}",
        trade_id=tid,
        issuer_id=ours,
        bill_to_id=theirs,
        issue_date=d.today(),
        due=date.fromisoformat(prepay["due"]),
        lines=proforma_lines,
        currency=currency,
        pay_to=own_account,
        extra=[("Basis", f"{fmt(contract_qty, 0)} {unit} at {price:,.2f} per {unit}")],
        facts={"inputs": {"quantity": num(contract_qty), "price": num(price)}},
        kind="prepayment-invoice",
    )
    d.mail(
        "confirmation",
        n("ops"),
        v("ops"),
        f"Our confirmation of {tid} is attached with the prepayment invoice for USD {fmt(proforma_total)}. The vessel nomination follows once we receive the funds.",
        conf,
        proforma,
        subject=f"Confirmation {tid}: {terms['product']}",
        tags=["confirmation", "prepayment"],
        doc=cls("TradeTransaction"),
        facts={"trade_id": tid, "ticket_version": ticket["version"], "prepayment": num(proforma_total)},
    )
    wait_until(d, bank_mail, date.fromisoformat(prepay["due"]) - timedelta(days=1))
    advice = credit_advice(
        d,
        filename=f"{tid}-credit-advice.pdf",
        trade_id=tid,
        bank_id=u["accounts"][own_account]["bank"],
        account_id=own_account,
        value_date=d.today(),
        amount=prepaid,
        currency=currency,
        reference=f"PREPAY {tid}",
        originator=d.org(theirs)["name"],
    )
    d.mail(
        "credit-advice",
        bank_mail,
        n("settlements"),
        f"We credited USD {fmt(prepaid)} to your account, reference PREPAY {tid}.",
        advice,
        subject=f"Credit advice PREPAY {tid}",
        tags=["payment", "prepayment"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "amount": num(prepaid)},
    )
    d.mail(
        "prepayment-received",
        n("settlements"),
        n("ops"),
        f"The prepayment of USD {fmt(prepaid)} for {tid} has arrived. Kestrel can nominate.",
        reply="credit-advice",
        days=1,
        tags=["prepayment"],
    )
    wait_until(d, v("ops"), business_date(d, ours, loading_from, -5))
    nom = nomination(
        d,
        filename=f"{tid}-nomination.pdf",
        trade_id=tid,
        issuer_id=theirs,
        to_id=ours,
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
        v("ops"),
        n("ops"),
        f"We nominate {VESSEL} for {tid}.",
        nom,
        subject=f"Vessel nomination {tid}",
        tags=["logistics", "nomination"],
        doc=cls("TransportMovement"),
        facts={"trade_id": tid, "vessel": VESSEL},
    )
    d.mail(
        "nomination-ack",
        n("ops"),
        v("ops"),
        f"{VESSEL} accepted for {tid}.",
        reply="nomination",
        days=1,
        tags=["logistics"],
    )
    bl_date = business_date(d, ours, loading_from, 2)
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
        place=terms["location"],
        results=[
            ("Product", terms["product"]),
            ("Quantity", f"{fmt(qty, 3)} {unit}"),
            ("Basis", "shore tank, load port"),
        ],
        facts={"quantity": num(qty), "unit": unit, "basis": "load-port", "bl_number": bl_number},
        fee=INSPECTION_FEE,
    )
    qual_cert = inspection_certificate(
        d,
        filename=f"{tid}-quality-certificate.pdf",
        kind="quality-certificate",
        title=f"Certificate of quality {tid}",
        trade_id=tid,
        cert_date=d.today(),
        place=terms["location"],
        results=[("Product", terms["product"]), ("Density at 15C, kg/m3", "988.1"), ("Sulphur, % mass", "0.49")],
        facts={"density_15c": 988.1, "sulphur_pct": 0.49, "bl_number": bl_number},
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

    # --- final invoice, trued up against the prepayment ---------------------------------------
    wait_until(d, n("accounting"), business_date(d, ours, bl_date, 2))
    due = due_date(d, ours, bl_issued, deal["payment_days"])
    fee_share = q2(INSPECTION_FEE * FEE_SHARE_PCT / 100)
    cargo_line = InvLine(f"{terms['product']}, {fmt(qty, 3)} {unit} at {price:,.2f}", q2(qty * price), qty, unit, price)
    inv_number = f"NCL-INV-{tid}"
    inv, balance = invoice(
        d,
        number=inv_number,
        filename=f"{tid}-final-invoice.pdf",
        title=f"Final invoice {inv_number}",
        trade_id=tid,
        issuer_id=ours,
        bill_to_id=theirs,
        issue_date=d.today(),
        due=due,
        lines=[cargo_line, InvLine(f"Inspection fee share ({FEE_SHARE_PCT}% of {fmt(INSPECTION_FEE)})", fee_share)],
        currency=currency,
        pay_to=own_account,
        deductions=[("Prepayment received", prepaid)],
        extra=[
            ("Bill of lading", bl_number),
            ("Payment terms", f"{deal['payment_days']} days from bill of lading date"),
        ],
        facts={
            "inputs": {
                "quantity": num(qty),
                "price": num(price),
                "inspection_fee": num(INSPECTION_FEE),
                "fee_share_pct": FEE_SHARE_PCT,
                "prepayment": num(prepaid),
            }
        },
        kind="final-invoice",
    )
    d.mail(
        "final-invoice",
        n("accounting"),
        v("accounting"),
        f"Final invoice {inv_number} for {tid} is attached. Cargo value on the loaded quantity less the prepayment leaves a balance of USD {fmt(balance)}, due {due.isoformat()}.",
        inv,
        subject=f"Final invoice {inv_number}: {tid}",
        tags=["invoice", "true-up"],
        doc=cls("Invoice"),
        facts={"trade_id": tid, "invoice_number": inv_number, "total_due": num(balance), "prepayment": num(prepaid)},
    )
    over = qty - contract_qty
    d.mail(
        "quantity-query",
        v("accounting"),
        n("accounting"),
        f"The invoice is on {fmt(qty, 3)} {unit}, but we bought {fmt(contract_qty, 0)} {unit}. Please explain the difference of {fmt(over, 3)} {unit}.",
        reply="final-invoice",
        days=1,
        tags=["invoice", "query"],
        facts={"trade_id": tid, "contract_quantity": num(contract_qty), "invoiced_quantity": num(qty)},
    )
    d.mail(
        "quantity-answer",
        n("accounting"),
        v("accounting"),
        f"The agreement allows a quantity tolerance of {tolerance}%, which is {fmt(contract_qty * tolerance / 100, 0)} {unit} on {fmt(contract_qty, 0)} {unit}. The certified {fmt(qty, 3)} {unit} is {over / contract_qty * 100:.2f}% over, inside the tolerance, so the invoice stands.",
        reply="quantity-query",
        days=1,
        tags=["invoice", "tolerance"],
        facts={"trade_id": tid, "tolerance_pct": num(tolerance), "difference": num(over)},
    )
    register(
        sc,
        trade_id=tid,
        kind="final-invoice",
        filename=inv.filename,
        message="final-invoice",
        number=inv_number,
        issuer=ours,
        payer=theirs,
        payee=ours,
        currency=currency,
        payable=num(balance),
        due=due.isoformat(),
        payer_account=onb["accounts"]["counterparty"],
        payee_account=own_account,
        inputs={"cargo": num(cargo_line.amount), "fee_share": num(fee_share), "prepayment": num(prepaid)},
    )
    sc.outcome = {
        "result": "balance-invoiced",
        "trade_id": tid,
        "balance": num(balance),
        "due": due.isoformat(),
        "quantity_query": {"difference": num(over), "within_tolerance": abs(over) <= contract_qty * tolerance / 100},
    }
    d.question(
        1, "reconciliation", f"What balance is due on the final invoice for {tid} after the prepayment?", num(balance)
    )
    d.question(
        2,
        "hybrid_retrieval",
        f"Why was the invoiced quantity for {tid} not queried further after the answer?",
        f"within the {tolerance}% tolerance",
    )
    d.question(3, "metadata", f"What prepayment was credited for {tid}?", num(prepaid))
    return sc
