# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""d2: NewCo London buys three products from BP on one vessel. BP sends the whole document set as one merged
PDF: confirmation, three bills of lading with their originals and copies, certificates, LOI and invoice, with
blank separator pages in some places and not others, and the Eurobob pages scanned. Two problems are planted:
the third original jet B/L is missing, and the invoice bills Eurobob on the nominated quantity, not the B/L."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from ..ontology import cls
from ..post_trade.common import Desk, account_for, business_date, due_date, fmt, num, wait_until
from ..scenario import Scenario
from .common import (
    Docset,
    Product,
    bill_of_lading,
    certificates,
    confirmation,
    deal_record,
    invoice_part,
    loi_pdf,
    office_item,
    pdf_item,
    pricing_workbook,
)

SCENARIO_ID = "d2-london-bp-products-merged"
EXPECT = {"emails": 2, "attachments": 2, "chats": 0}
TRADE_ID = "NC-LON-26-0101"
OURS, THEIRS, BANK = "newco-lon", "cp-bp", "bank-northgate"
VESSEL = "MT Northern Laurel"
INSPECTOR = "Halvorsen Marine Surveyors Ltd"

PRODUCTS = (
    Product(
        "ulsd",
        "ULSD 10 ppm",
        "mt",
        Decimal(30000),
        Decimal(5),
        Decimal("30412.556"),
        fixed_price=Decimal("718.250"),
        quality=(("Density at 15C, kg/m3", "838.4"), ("Sulphur, mg/kg", "7.9"), ("Cetane index", "51.2")),
    ),
    Product(
        "jet",
        "Jet A-1",
        "mt",
        Decimal(20000),
        Decimal(5),
        Decimal("19874.310"),
        index="Platts Jet CIF NWE cargoes",
        differential=Decimal("16.500"),
        quotes=(Decimal("742.250"), Decimal("745.500"), Decimal("744.750"), Decimal("741.000"), Decimal("743.250")),
        quality=(("Density at 15C, kg/m3", "798.6"), ("Freeze point, C", "-48"), ("Flash point, C", "42")),
    ),
    Product(
        "eurobob",
        "Eurobob gasoline",
        "mt",
        Decimal(15000),
        Decimal(10),
        Decimal("15902.004"),
        index="Platts Eurobob FOB AR barges",
        differential=Decimal("-6.750"),
        quotes=(Decimal("781.500"), Decimal("779.250"), Decimal("784.000"), Decimal("782.750"), Decimal("780.500")),
        quality=(("Density at 15C, kg/m3", "742.9"), ("RON", "95.4"), ("DVPE, kPa", "58.0")),
    ),
)
MISSING_ORIGINAL = 3  # the jet B/L set holds originals 1 and 2 only


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    start = max(s.clock for s in upstream.values()) + timedelta(days=1)
    sc = Scenario(
        SCENARIO_ID,
        "docsets",
        u,
        start,
        expect=EXPECT,
        title="London buys ULSD, jet and Eurobob from BP: one merged PDF, a missing original B/L, an invoice quantity off the B/L",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def b(role: str) -> str:
        return f"{THEIRS}:{role}"

    confirm_day = d.today()
    laycan = business_date(d, OURS, confirm_day, 5)
    bl_date = business_date(d, OURS, laycan, 1)
    quote_dates = [business_date(d, OURS, bl_date, k) for k in (-2, -1, 0, 1, 2)]
    deal = deal_record(
        TRADE_ID,
        THEIRS,
        OURS,
        PRODUCTS,
        quote_dates,
        confirmation_date=confirm_day.isoformat(),
        countersigned=confirm_day.isoformat(),
        paper_org=THEIRS,
        incoterm="FOB",
        load_port="Fawley",
        discharge_port="Rotterdam",
        vessel=VESSEL,
        laycan_from=laycan.isoformat(),
        laycan_to=business_date(d, OURS, laycan, 2).isoformat(),
        payment_days=5,
        currency="USD",
    )
    ds = Docset(d, f"{SCENARIO_ID}:docset", deal)
    wait_until(d, b("ops"), business_date(d, OURS, bl_date, 3))

    bls = {p.key: f"NLV-{bl_date:%y%m}-{i:03d}" for i, p in enumerate(PRODUCTS, start=1)}
    ulsd, jet, eurobob = PRODUCTS
    planted_qty = {p.key: p.bl_quantity for p in PRODUCTS} | {"eurobob": eurobob.nominal}
    due = due_date(d, THEIRS, bl_date, deal["payment_days"])
    inv, total = invoice_part(
        d,
        deal,
        PRODUCTS,
        number=f"BPOI-{TRADE_ID}",
        issue_date=d.today(),
        due=due,
        pay_to=account_for(d, THEIRS, "USD"),
        bl_numbers=bls,
        quantities=planted_qty,
    )
    bl_eurobob = bill_of_lading(d, deal, eurobob, number=bls["eurobob"], bl_date=bl_date)
    bl_eurobob.scanned = True
    cert_day = bl_date
    parts = [
        confirmation(d, deal, PRODUCTS, "confirmation", signed_by=OURS),
        None,  # blank separator
        bill_of_lading(d, deal, ulsd, number=bls["ulsd"], bl_date=bl_date),
        bill_of_lading(
            d, deal, jet, number=bls["jet"], bl_date=bl_date, originals=(1, 2)
        ),  # no separator before or after
        None,
        bl_eurobob,
        certificates(deal, ulsd, bl_number=bls["ulsd"], issuer=INSPECTOR, cert_date=cert_day),
        certificates(deal, jet, bl_number=bls["jet"], issuer=INSPECTOR, cert_date=cert_day),
        None,
        certificates(deal, eurobob, bl_number=bls["eurobob"], issuer=INSPECTOR, cert_date=cert_day, scanned=True),
        loi_pdf(d, deal, PRODUCTS, bl_numbers=bls, bank_id=BANK, issue_date=d.today()),
        inv,
    ]
    merged = pdf_item(sc, f"{TRADE_ID}-document-set.pdf", "document-set", cls("TradeDocument"), parts, TRADE_ID)
    ds.mail(
        "document-set",
        b("ops"),
        n("ops"),
        f"Please find the full document set for {TRADE_ID} ({VESSEL}) in one file: confirmation, bills of lading, certificates, LOI and our invoice BPOI-{TRADE_ID} for USD {fmt(total)}.",
        merged,
        subject=f"{VESSEL}: document set {TRADE_ID}",
        tags=["docset", "merged", "scan"],
        doc=cls("TradeDocument"),
    )

    wb, wb_doc = pricing_workbook(deal, PRODUCTS, quote_dates, author=d.org(THEIRS)["name"], created=sc.clock)
    ds.mail(
        "pricing",
        b("settlements"),
        n("accounting"),
        f"The floating-price workbook behind invoice BPOI-{TRADE_ID} is attached.",
        office_item(f"pricing-{TRADE_ID}.xlsx", "price-workbook", cls("MonetaryAmount"), wb, wb_doc, TRADE_ID),
        subject=f"Pricing workbook {TRADE_ID}",
        days=1,
        tags=["invoice", "spreadsheet", "docset"],
        doc=cls("MonetaryAmount"),
    )

    rec = ds.finish()
    recon = rec["reconciliation"]
    d.contradiction(
        "missing-original-bl",
        "document-count",
        f"The confirmation requires 3/3 original bills of lading per product; the set holds originals 1 and 2 of the jet B/L {bls['jet']}.",
        [merged.attachment.filename],
        {"product": "jet", "required": 3, "found": 2, "missing": [MISSING_ORIGINAL]},
        False,
    )
    d.contradiction(
        "invoice-quantity-not-bl",
        "invoice-vs-bl",
        f"The invoice bills Eurobob on the nominated {fmt(eurobob.nominal, 3)} mt; the B/L, certificate and LOI say {fmt(eurobob.bl_quantity, 3)} mt.",
        [merged.attachment.filename],
        {"product": "eurobob", "invoiced": num(eurobob.nominal), "bl": num(eurobob.bl_quantity)},
        False,
    )
    sc.outcome = {
        "result": recon["result"],
        "trade_id": TRADE_ID,
        "exceptions": recon["exceptions"],
        "invoice_total": num(total),
    }
    pages = merged.attachment.facts["pages"]
    logical = [x for x in rec["documents"] if not x.get("supporting") and x["file"] == merged.attachment.filename]
    d.question(
        1,
        "reconciliation",
        f"Which document requirements of the confirmation for {TRADE_ID} are not met?",
        [e for e in recon["exceptions"] if e.startswith("bill-of-lading")],
    )
    d.question(
        2,
        "reconciliation",
        f"For which products of {TRADE_ID} does the invoiced quantity differ from the B/L quantity?",
        [
            p["name"]
            for p in rec["deal"]["products"]
            if next(q for q in recon["quantities"] if q["product"] == p["key"])["invoice"]
            != next(q for q in recon["quantities"] if q["product"] == p["key"])["bill-of-lading"]
        ],
    )
    d.question(3, "metadata", f"How many pages does the merged document set for {TRADE_ID} have?", len(pages))
    d.question(
        4,
        "metadata",
        f"How many blank pages does the merged document set for {TRADE_ID} contain?",
        sum(1 for p in pages if p["kind"] == "blank"),
    )
    d.question(
        5,
        "hybrid_retrieval",
        f"How many separate documents does the merged document set for {TRADE_ID} contain?",
        len(logical),
    )
    d.question(
        6,
        "reconciliation",
        f"Are all bill of lading quantities for {TRADE_ID} within the confirmation's quantity ranges?",
        all(q["within_range"] for q in recon["quantities"]),
    )
    return sc
