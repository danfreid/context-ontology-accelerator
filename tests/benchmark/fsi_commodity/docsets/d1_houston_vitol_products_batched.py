# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""d1: NewCo Houston buys three products from Vitol on one vessel. The documents arrive in five batches from
three sources, as DOCX, text PDF, scanned (image-only) PDF and XLSX. Every requirement is met: the control case."""

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
    confirmation_docx,
    deal_record,
    invoice_part,
    loi_docx,
    office_item,
    pdf_item,
    pricing_workbook,
    quantity_report,
)

SCENARIO_ID = "d1-houston-vitol-products-batched"
EXPECT = {"emails": 6, "attachments": 12, "chats": 0}
TRADE_ID = "NC-HOU-26-0101"
OURS, THEIRS, BANK = "newco-hou", "cp-vitol", "bank-hudson"
VESSEL = "MT Gulf Sentinel"
SECOND_INSPECTOR = "Bayou Petroleum Inspection Co."  # Jet certificates come from Vitol's own inspector

PRODUCTS = (
    Product(
        "ulsd",
        "ULSD (62 grade)",
        "bbl",
        Decimal(300000),
        Decimal(5),
        Decimal("301284.160"),
        fixed_price=Decimal("97.850"),
        quality=(("API gravity", "34.8"), ("Sulphur, ppm", "8.6"), ("Flash point, C", "61")),
    ),
    Product(
        "rbob",
        "RBOB gasoline (M4)",
        "bbl",
        Decimal(250000),
        Decimal(5),
        Decimal("246905.330"),
        index="Platts USGC RBOB pipeline",
        differential=Decimal("1.150"),
        quotes=(Decimal("88.410"), Decimal("88.920"), Decimal("89.135"), Decimal("88.760"), Decimal("88.505")),
        quality=(("RVP, psi", "9.0"), ("Octane (R+M)/2", "83.1")),
    ),
    Product(
        "jet",
        "Jet fuel (54 grade)",
        "bbl",
        Decimal(200000),
        Decimal(10),
        Decimal("208117.045"),
        index="Platts USGC Jet 54 pipeline",
        differential=Decimal("-0.850"),
        quotes=(Decimal("95.300"), Decimal("95.015"), Decimal("94.880"), Decimal("95.410"), Decimal("95.265")),
        quality=(("Density at 15C, kg/m3", "805.2"), ("Freeze point, C", "-42")),
    ),
)
SHORE = {"ulsd": Decimal("301301.870"), "rbob": Decimal("246862.115")}  # inspector's shore-tank figures


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    start = max(s.clock for s in upstream.values()) + timedelta(days=1)
    sc = Scenario(
        SCENARIO_ID,
        "docsets",
        u,
        start,
        expect=EXPECT,
        title="Houston buys ULSD, RBOB and jet from Vitol: five batches, mixed formats, all documents in order",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def v(role: str) -> str:
        return f"{THEIRS}:{role}"

    laycan = business_date(d, OURS, d.today(), 6)
    bl_date = business_date(d, OURS, laycan, 1)
    quote_dates = [business_date(d, OURS, bl_date, k) for k in (-2, -1, 0, 1, 2)]
    deal = deal_record(
        TRADE_ID,
        THEIRS,
        OURS,
        PRODUCTS,
        quote_dates,
        confirmation_date=d.today().isoformat(),
        countersigned="",
        paper_org=OURS,
        incoterm="FOB",
        load_port="Houston Ship Channel",
        discharge_port="Santos",
        vessel=VESSEL,
        laycan_from=laycan.isoformat(),
        laycan_to=business_date(d, OURS, laycan, 2).isoformat(),
        payment_days=30,
        currency="USD",
    )
    ds = Docset(d, f"{SCENARIO_ID}:docset", deal)
    cls_conf, cls_bl, cls_cert = cls("TradeTransaction"), cls("BillOfLading"), cls("Attestation")

    # 1. Our confirmation goes out as a Word document for signature.
    content, conf_doc = confirmation_docx(d, deal, PRODUCTS, "confirmation", sc.clock)
    ds.mail(
        "confirmation",
        n("ops"),
        v("ops"),
        f"Please find our confirmation of {TRADE_ID} for three products on {VESSEL}. Sign and return it.",
        office_item(f"{TRADE_ID}-confirmation.docx", "trade-confirmation", cls_conf, content, conf_doc, TRADE_ID),
        subject=f"Confirmation {TRADE_ID}: ULSD, RBOB, jet",
        tags=["confirmation", "docset"],
        doc=cls_conf,
    )

    # 2. Vitol signs a printout and returns a scan: the same terms, no text layer.
    deal["countersigned"] = business_date(d, THEIRS, d.today(), 1).isoformat()
    signed = confirmation(d, deal, PRODUCTS, "confirmation-signed", signed_by=THEIRS)
    signed.scanned = True
    ds.mail(
        "confirmation-signed",
        v("ops"),
        n("ops"),
        f"Signed confirmation for {TRADE_ID} attached (scan).",
        pdf_item(sc, f"{TRADE_ID}-confirmation-signed.pdf", "trade-confirmation", cls_conf, [signed], TRADE_ID),
        reply="confirmation",
        days=1,
        tags=["confirmation", "docset", "scan"],
        doc=cls_conf,
    )

    # 3. The ship's agent sends the ULSD and RBOB bills of lading (RBOB as a scan).
    wait_until(d, n("ops"), bl_date)
    bls = {p.key: f"GSMA-{TRADE_ID}-{i:02d}" for i, p in enumerate(PRODUCTS, start=1)}
    bl_ulsd = bill_of_lading(d, deal, PRODUCTS[0], number=bls["ulsd"], bl_date=bl_date)
    bl_rbob = bill_of_lading(d, deal, PRODUCTS[1], number=bls["rbob"], bl_date=bl_date)
    bl_rbob.scanned = True
    ds.mail(
        "bills-ulsd-rbob",
        "svc-gulfstream:agent",
        n("ops"),
        f"{VESSEL} sailed. Bills of lading for ULSD ({bls['ulsd']}) and RBOB ({bls['rbob']}) attached: three originals and one copy each. Jet follows from the shipper.",
        pdf_item(sc, f"bl-{bls['ulsd']}.pdf", "bill-of-lading", cls_bl, [bl_ulsd], TRADE_ID),
        pdf_item(sc, f"bl-{bls['rbob']}.pdf", "bill-of-lading", cls_bl, [bl_rbob], TRADE_ID),
        subject=f"{VESSEL}: bills of lading ULSD and RBOB",
        tags=["bill-of-lading", "docset"],
        doc=cls_bl,
    )

    # 4. Cardinal sends certificates for ULSD and RBOB, plus its quantity summary as a spreadsheet.
    cert_date = d.today()
    cardinal = d.org("svc-cardinal")["name"]
    rep, rep_doc = quantity_report(deal, PRODUCTS[:2], bls, SHORE, author=cardinal, created=sc.clock)
    ds.mail(
        "certificates-ulsd-rbob",
        "svc-cardinal:inspector",
        n("ops"),
        "Certificates of quality and quantity for ULSD and RBOB are attached, with our load-port quantity summary.",
        pdf_item(
            sc,
            "certificates-ulsd.pdf",
            "inspection-certificates",
            cls_cert,
            [certificates(deal, PRODUCTS[0], bl_number=bls["ulsd"], issuer=cardinal, cert_date=cert_date)],
            TRADE_ID,
        ),
        pdf_item(
            sc,
            "certificates-rbob.pdf",
            "inspection-certificates",
            cls_cert,
            [certificates(deal, PRODUCTS[1], bl_number=bls["rbob"], issuer=cardinal, cert_date=cert_date)],
            TRADE_ID,
        ),
        office_item("load-port-quantities.xlsx", "quantity-report", cls("Assessment"), rep, rep_doc, TRADE_ID),
        subject=f"Certificates {VESSEL}: ULSD, RBOB",
        days=1,
        tags=["certificate", "docset", "spreadsheet"],
        doc=cls_cert,
    )

    # 5. Vitol sends the jet B/L and its own inspector's certificates (scanned): a different source.
    bl_jet = bill_of_lading(d, deal, PRODUCTS[2], number=bls["jet"], bl_date=bl_date)
    ds.mail(
        "documents-jet",
        v("ops"),
        n("ops"),
        f"Jet documents for {TRADE_ID}: bill of lading {bls['jet']} and the certificates from {SECOND_INSPECTOR}.",
        pdf_item(sc, f"bl-{bls['jet']}.pdf", "bill-of-lading", cls_bl, [bl_jet], TRADE_ID),
        pdf_item(
            sc,
            "jet-certificates-scan.pdf",
            "inspection-certificates",
            cls_cert,
            [
                certificates(
                    deal, PRODUCTS[2], bl_number=bls["jet"], issuer=SECOND_INSPECTOR, cert_date=cert_date, scanned=True
                )
            ],
            TRADE_ID,
        ),
        subject=f"Jet documents {TRADE_ID}",
        days=1,
        tags=["bill-of-lading", "certificate", "docset", "scan"],
        doc=cls_bl,
    )

    # 6. After the last quotation date: invoice (PDF), pricing workbook (XLSX) and one LOI for the set (DOCX).
    wait_until(d, v("settlements"), business_date(d, OURS, bl_date, 3))
    due = due_date(d, THEIRS, bl_date, deal["payment_days"])
    inv, total = invoice_part(
        d,
        deal,
        PRODUCTS,
        number=f"VIT-INV-{TRADE_ID}",
        issue_date=d.today(),
        due=due,
        pay_to=account_for(d, THEIRS, "USD"),
        bl_numbers=bls,
    )
    wb, wb_doc = pricing_workbook(deal, PRODUCTS, quote_dates, author=d.org(THEIRS)["name"], created=sc.clock)
    loi, loi_doc = loi_docx(d, deal, PRODUCTS, sc.clock, bl_numbers=bls, bank_id=BANK, issue_date=d.today())
    ds.mail(
        "invoice-set",
        v("settlements"),
        n("accounting"),
        f"Invoice VIT-INV-{TRADE_ID} for USD {fmt(total)} covers all three products, due {due.isoformat()}. The pricing workbook shows the floating prices; our LOI for the cargo is attached.",
        pdf_item(sc, "invoice.pdf", "invoice", cls("Invoice"), [inv], TRADE_ID),
        office_item(f"pricing-{TRADE_ID}.xlsx", "price-workbook", cls("MonetaryAmount"), wb, wb_doc, TRADE_ID),
        office_item(f"loi-{TRADE_ID}.docx", "letter-of-indemnity", cls("LetterOfIndemnity"), loi, loi_doc, TRADE_ID),
        subject=f"Invoice and LOI {TRADE_ID}",
        tags=["invoice", "letter-of-indemnity", "docset", "spreadsheet"],
        doc=cls("Invoice"),
    )

    rec = ds.finish()
    sc.outcome = {"result": rec["reconciliation"]["result"], "trade_id": TRADE_ID, "invoice_total": num(total)}
    jet = next(p for p in rec["deal"]["products"] if p["key"] == "jet")
    d.question(
        1,
        "reconciliation",
        f"Does the document set for {TRADE_ID} meet every document requirement of its confirmation?",
        True,
    )
    d.question(
        2,
        "hybrid_retrieval",
        f"What quantity of RBOB was loaded under bill of lading {bls['rbob']}?",
        num(PRODUCTS[1].bl_quantity),
    )
    d.question(
        3,
        "reconciliation",
        f"What price per barrel does the confirmation's formula give for the jet fuel in {TRADE_ID}?",
        jet["final_price"],
    )
    d.question(
        4, "metadata", f"How many original bills of lading does the confirmation for {TRADE_ID} require per product?", 3
    )
    d.question(5, "reconciliation", f"What is the invoiced total for {TRADE_ID}?", num(total))
    d.question(6, "hybrid_retrieval", f"Who issued the certificates for the jet fuel in {TRADE_ID}?", SECOND_INSPECTOR)
    return sc
