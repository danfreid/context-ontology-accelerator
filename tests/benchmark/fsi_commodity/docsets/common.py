# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Multi-product cargo document sets: the deal model, document builders, the ``Docset`` recorder and ``reconcile``.

A document set is everything that proves and prices one physical deal: the trade confirmation (several
products, quantity ranges, fixed or floating prices, a documents clause), and per product a bill of lading (a
number of originals and copies) and certificates of quality and quantity, then one letter of indemnity and one
invoice for the whole cargo. Files arrive in any format and grouping; the ground truth records the *logical*
documents, which file and pages hold each one, and the expected reconciliation.
"""

# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..ontology import cls
from ..post_trade.common import Desk, dec, fmt, num, q2
from ..post_trade.docs import InvLine, invoice, priced
from ..scenario import Attachment, Scenario
from ..writers.docx_writer import build_docx, plain
from ..writers.pdf_tools import BLANK, merge, page_count, scan
from ..writers.pdf_writer import Grid, Heading, KeyValues, Text, build_pdf
from ..writers.xlsx_writer import Cell, Sheet, build_xlsx

MILLI = Decimal("0.001")

# The documents clause every docset confirmation carries.
REQUIREMENTS: tuple[dict[str, Any], ...] = (
    {"kind": "bill-of-lading", "per": "product", "originals": 3, "copies": 1},
    {"kind": "quality-certificate", "per": "product", "count": 1},
    {"kind": "quantity-certificate", "per": "product", "count": 1},
    {"kind": "letter-of-indemnity", "per": "set", "count": 1},
    {"kind": "invoice", "per": "set", "count": 1},
)
REQUIREMENT_TEXT = (
    "1. Per product: a full set of 3/3 original bills of lading and one non-negotiable copy.",
    "2. Per product: a certificate of quality and a certificate of quantity issued by an independent inspector at the load port.",
    "3. One letter of indemnity covering all products, issued by the seller and countersigned by a bank.",
    "4. One commercial invoice covering all products.",
)


def q3(value: Decimal) -> Decimal:
    return value.quantize(MILLI, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Product:
    """One product of a multi-product confirmation and the figures its documents carry."""

    key: str
    name: str
    unit: str
    nominal: Decimal
    tolerance_pct: Decimal
    bl_quantity: Decimal
    fixed_price: Decimal | None = None
    index: str | None = None
    differential: Decimal | None = None
    quotes: tuple[Decimal, ...] = ()
    quality: tuple[tuple[str, str], ...] = ()

    @property
    def floating(self) -> bool:
        return self.fixed_price is None

    @property
    def qmin(self) -> Decimal:
        return q3(self.nominal * (1 - self.tolerance_pct / 100))

    @property
    def qmax(self) -> Decimal:
        return q3(self.nominal * (1 + self.tolerance_pct / 100))

    def final_price(self) -> Decimal:
        """Fixed price, or the average of the quotations plus the differential, to 0.001."""
        if self.fixed_price is not None:
            return self.fixed_price
        assert self.differential is not None and self.quotes
        return q3(sum(self.quotes, Decimal(0)) / len(self.quotes) + self.differential)

    def quantity_text(self) -> str:
        return f"{fmt(self.qmin, 3)} to {fmt(self.qmax, 3)} {self.unit} ({fmt(self.nominal, 0)} {self.unit} +/- {self.tolerance_pct}% at seller's option)"

    def price_text(self) -> str:
        if self.fixed_price is not None:
            return f"Fixed USD {self.fixed_price:.3f} per {self.unit}"
        sign = "plus" if self.differential >= 0 else "minus"  # type: ignore[operator]
        return f"{self.index}, average of 5 quotations from B/L date -2 to +2 business days, {sign} USD {abs(self.differential):.3f} per {self.unit}"  # type: ignore[arg-type]


def deal_record(
    trade_id: str, seller: str, buyer: str, products: Sequence[Product], quote_dates: Sequence[date], **terms: Any
) -> dict[str, Any]:
    """The confirmed deal as ground truth (also printed on the confirmation)."""
    return {
        "trade_id": trade_id,
        "seller": seller,
        "buyer": buyer,
        **terms,
        "products": [
            {
                "key": p.key,
                "name": p.name,
                "unit": p.unit,
                "nominal": num(p.nominal),
                "tolerance_pct": num(p.tolerance_pct),
                "quantity_min": num(p.qmin),
                "quantity_max": num(p.qmax),
                "price_type": "floating" if p.floating else "fixed",
                "fixed_price": None if p.fixed_price is None else num(p.fixed_price),
                "index": p.index,
                "differential": None if p.differential is None else num(p.differential),
                "quotes": [{"date": d.isoformat(), "price": num(v)} for d, v in zip(quote_dates, p.quotes, strict=True)]
                if p.floating
                else [],
                "final_price": num(p.final_price()),
            }
            for p in products
        ],
        "requirements": [dict(r) for r in REQUIREMENTS],
    }


# --- logical documents and files -----------------------------------------------------------------
@dataclass
class Part:
    """One logical document's bytes (a PDF) and ground truth, before it is put in a file."""

    pdf: bytes
    docs: list[dict[str, Any]]
    scanned: bool = False


@dataclass
class Item:
    """One attachment and the logical documents it holds (with their page ranges)."""

    attachment: Attachment
    docs: list[dict[str, Any]]
    pages: list[dict[str, Any]] = field(default_factory=list)


def _doc(doc_id: str, kind: str, products: Sequence[str], **extra: Any) -> dict[str, Any]:
    return {"doc_id": doc_id, "kind": kind, "products": list(products), **extra}


def _scan_seed(sc: Scenario, name: str) -> int:
    from .. import ids

    return ids.rng_for(sc.seed, f"scan:{sc.id}:{name}").randint(1, 10**6)


def pdf_item(
    sc: Scenario, filename: str, kind: str, class_uri: str, parts: Sequence[Part | None], trade_id: str
) -> Item:
    """Put parts (and ``None`` for a blank page) into one PDF attachment; scanned parts lose their text layer."""
    pdfs: list[bytes | None] = []
    for i, p in enumerate(parts):
        if p is None:
            pdfs.append(BLANK)
        else:
            pdfs.append(scan(p.pdf, _scan_seed(sc, f"{filename}:{i}")) if p.scanned else p.pdf)
    single = pdfs[0] if len(pdfs) == 1 else None
    content, ranges = (single, [(1, page_count(single))]) if single is not None else merge(pdfs)
    docs: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for p, (first, last) in zip(parts, ranges, strict=True):
        if p is None:
            pages.append({"page": first, "kind": "blank", "doc_id": None, "scanned": False})
            continue
        span = last - first + 1
        per = span // len(p.docs)  # a part holding two logical documents splits its pages evenly
        for j, doc in enumerate(p.docs):
            lo = first + j * per
            hi = last if j == len(p.docs) - 1 else lo + per - 1
            doc.update(file=filename, page_range=[lo, hi], format="pdf-scan" if p.scanned else "pdf")
            docs.append(doc)
            pages += [
                {"page": n, "kind": doc["kind"], "doc_id": doc["doc_id"], "scanned": p.scanned}
                for n in range(lo, hi + 1)
            ]
    facts = {
        "trade_id": trade_id,
        "format": "pdf",
        "page_count": len(pages),
        "documents": [d["doc_id"] for d in docs],
        "pages": pages,
    }
    return Item(Attachment(filename, content, kind, class_uri, facts), docs, pages)


def office_item(filename: str, kind: str, class_uri: str, content: bytes, doc: dict[str, Any], trade_id: str) -> Item:
    """A DOCX or XLSX attachment holding one logical document."""
    fmt_ = filename.rsplit(".", 1)[-1]
    doc.update(file=filename, page_range=None, format=fmt_)
    facts = {"trade_id": trade_id, "format": fmt_, "documents": [doc["doc_id"]]}
    return Item(Attachment(filename, content, kind, class_uri, facts), [doc])


# --- document builders ------------------------------------------------------------------------------
def _confirm_pairs(d: Desk, deal: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        ("Trade id", deal["trade_id"]),
        ("Confirmation date", deal["confirmation_date"]),
        ("Seller", d.org(deal["seller"])["name"]),
        ("Buyer", d.org(deal["buyer"])["name"]),
        ("Delivery", f"{deal['incoterm']} {deal['load_port']}"),
        ("Vessel", deal["vessel"]),
        ("Laycan", f"{deal['laycan_from']} to {deal['laycan_to']}"),
        ("Payment", f"{deal['payment_days']} days after B/L date against invoice and documents"),
    ]


def confirmation(
    d: Desk, deal: Mapping[str, Any], products: Sequence[Product], doc_id: str, *, signed_by: str | None = None
) -> Part:
    """The multi-product confirmation as a PDF; ``signed_by`` adds the counterparty's acceptance."""
    grid = Grid(("Product", "Quantity", "Price"), tuple((p.name, p.quantity_text(), p.price_text()) for p in products))
    blocks: list[Any] = [KeyValues(tuple(_confirm_pairs(d, deal))), Heading("Products"), grid, Heading("Documents")]
    blocks += [Text(t) for t in REQUIREMENT_TEXT]
    if signed_by:
        blocks.append(Text(f"Accepted and agreed for {d.org(signed_by)['name']}. Signed: {deal['countersigned']}"))
    pdf = build_pdf(f"Trade confirmation {deal['trade_id']}", blocks)
    issuer = d.org(deal["paper_org"])["name"]
    return Part(
        pdf,
        [_doc(doc_id, "trade-confirmation", [p.key for p in products], issuer=issuer, signed=signed_by is not None)],
    )


def confirmation_docx(
    d: Desk, deal: Mapping[str, Any], products: Sequence[Product], doc_id: str, created: datetime
) -> tuple[bytes, dict[str, Any]]:
    """The same confirmation as an unsigned DOCX (sent for signature)."""
    paras = [plain(f"Trade confirmation {deal['trade_id']}", heading=True)]
    paras += [plain(f"{k}: {v}") for k, v in _confirm_pairs(d, deal)]
    paras.append(plain("Products", heading=True))
    paras += [plain(f"{p.name}. Quantity: {p.quantity_text()}. Price: {p.price_text()}.") for p in products]
    paras.append(plain("Documents", heading=True))
    paras += [plain(t) for t in REQUIREMENT_TEXT]
    issuer = d.org(deal["paper_org"])["name"]
    content = build_docx(f"Trade confirmation {deal['trade_id']}", paras, author=issuer, created=created)
    return content, _doc(doc_id, "trade-confirmation", [p.key for p in products], issuer=issuer, signed=False)


def bill_of_lading(
    d: Desk,
    deal: Mapping[str, Any],
    product: Product,
    *,
    number: str,
    bl_date: date,
    originals: Sequence[int] = (1, 2, 3),
    issued: int = 3,
    copies: int = 1,
) -> Part:
    """One B/L: a page per original (``ORIGINAL n of issued``) and per non-negotiable copy."""
    pairs = (
        ("B/L number", number),
        ("Shipper", d.org(deal["seller"])["name"]),
        ("Consignee", "To order"),
        ("Notify party", d.org(deal["buyer"])["name"]),
        ("Vessel", deal["vessel"]),
        ("Port of loading", deal["load_port"]),
        ("Port of discharge", deal["discharge_port"]),
        ("Date of issue", bl_date.isoformat()),
        ("Cargo item", f"{product.name}, {fmt(product.bl_quantity, 3)} {product.unit} shipped on board"),
        ("Originals issued", f"{issued} ({['one', 'two', 'three', 'four'][issued - 1]})"),
    )
    marks = [f"ORIGINAL {n} of {issued}" for n in originals] + ["COPY - NOT NEGOTIABLE"] * copies
    pages = [build_pdf(f"Bill of lading {number}", [Heading(m), KeyValues(pairs)]) for m in marks]
    pdf = merge(pages)[0] if len(pages) > 1 else pages[0]
    doc = _doc(
        f"bl-{product.key}",
        "bill-of-lading",
        [product.key],
        number=number,
        issuer=deal["vessel"] + " (master)",
        date=bl_date.isoformat(),
        originals=list(originals),
        originals_issued=issued,
        copies=copies,
        quantities={product.key: num(product.bl_quantity)},
        unit=product.unit,
        class_uris={
            "shipper": cls("Shipper"),
            "consignee": cls("Consignee"),
            "notify_party": cls("NotifyParty"),
            "cargo_item": cls("CargoItem"),
        },
    )
    return Part(pdf, [doc])


def certificates(
    deal: Mapping[str, Any], product: Product, *, bl_number: str, issuer: str, cert_date: date, scanned: bool = False
) -> Part:
    """A two-page certificates document: quality (page 1) and quantity (page 2)."""
    head = (
        ("Issued by", issuer),
        ("Date", cert_date.isoformat()),
        ("Place", deal["load_port"]),
        ("Vessel", deal["vessel"]),
        ("B/L number", bl_number),
        ("Product", product.name),
    )
    qual = build_pdf(
        f"Certificate of quality: {product.name}", [KeyValues(head), Heading("Results"), KeyValues(product.quality)]
    )
    qty = build_pdf(
        f"Certificate of quantity: {product.name}",
        [
            KeyValues(head),
            KeyValues(
                (
                    ("Quantity loaded", f"{fmt(product.bl_quantity, 3)} {product.unit}"),
                    ("Basis", "shore tank, load port"),
                )
            ),
        ],
    )
    common = {"issuer": issuer, "date": cert_date.isoformat(), "bl_number": bl_number}
    return Part(
        merge([qual, qty])[0],
        [
            _doc(f"coq-{product.key}", "quality-certificate", [product.key], results=dict(product.quality), **common),
            _doc(
                f"cqty-{product.key}",
                "quantity-certificate",
                [product.key],
                quantities={product.key: num(product.bl_quantity)},
                unit=product.unit,
                **common,
            ),
        ],
        scanned,
    )


def _loi_lines(
    products: Sequence[Product], bl_numbers: Mapping[str, str], quantities: Mapping[str, Decimal]
) -> list[tuple[Product, str, Decimal, Decimal, Decimal]]:
    return [
        (p, bl_numbers[p.key], quantities[p.key], p.final_price(), q2(quantities[p.key] * p.final_price()))
        for p in products
    ]


def letter_of_indemnity(
    d: Desk,
    deal: Mapping[str, Any],
    products: Sequence[Product],
    *,
    bl_numbers: Mapping[str, str],
    bank_id: str,
    issue_date: date,
    quantities: Mapping[str, Decimal] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """One LOI for every product of the set; returns PDF blocks (or DOCX paragraphs via ``loi_docx``) and the doc."""
    qtys = quantities or {p.key: p.bl_quantity for p in products}
    lines = _loi_lines(products, bl_numbers, qtys)
    total = sum((v for *_, v in lines), Decimal(0))
    text = (
        f"In consideration of your paying for the cargoes below without presentation of the original bills of lading, "
        f"{d.org(deal['seller'])['name']} indemnifies {d.org(deal['buyer'])['name']} against all consequences, up to 110% of the cargo value."
    )
    blocks = [
        KeyValues(
            (
                ("Trade id", deal["trade_id"]),
                ("Indemnifier", d.org(deal["seller"])["name"]),
                ("Countersigned by", d.org(bank_id)["name"]),
                ("Date", issue_date.isoformat()),
                ("Vessel", deal["vessel"]),
            )
        ),
        Text(text),
        Grid(
            ("Product", "B/L number", "Quantity", "Price (USD)", "Value (USD)"),
            tuple((p.name, bl, f"{fmt(q, 3)} {p.unit}", f"{pr:.3f}", fmt(v)) for p, bl, q, pr, v in lines),
        ),
        KeyValues(
            (("Total cargo value (USD)", fmt(total)), ("Amount of indemnity (USD)", fmt(q2(total * Decimal("1.1")))))
        ),
    ]
    doc = _doc(
        "loi",
        "letter-of-indemnity",
        [p.key for p in products],
        issuer=d.org(deal["seller"])["name"],
        countersigned_by=d.org(bank_id)["name"],
        date=issue_date.isoformat(),
        bl_numbers=dict(bl_numbers),
        quantities={k: num(v) for k, v in qtys.items()},
        cargo_value=num(total),
        indemnity_amount=num(q2(total * Decimal("1.1"))),
        class_uri=cls("LetterOfIndemnity"),
    )
    return blocks, doc


def loi_pdf(d: Desk, deal: Mapping[str, Any], products: Sequence[Product], **kw: Any) -> Part:
    blocks, doc = letter_of_indemnity(d, deal, products, **kw)
    return Part(build_pdf(f"Letter of indemnity: {deal['trade_id']}", blocks), [doc])


def loi_docx(
    d: Desk, deal: Mapping[str, Any], products: Sequence[Product], created: datetime, **kw: Any
) -> tuple[bytes, dict[str, Any]]:
    blocks, doc = letter_of_indemnity(d, deal, products, **kw)
    paras = [plain(f"Letter of indemnity: {deal['trade_id']}", heading=True)]
    for b in blocks:
        if isinstance(b, KeyValues):
            paras += [plain(f"{k}: {v}") for k, v in b.pairs]
        elif isinstance(b, Text):
            paras.append(plain(b.text))
        elif isinstance(b, Grid):
            paras += [plain("; ".join(f"{h}: {c}" for h, c in zip(b.header, row, strict=True))) for row in b.rows]
    return build_docx(f"Letter of indemnity {deal['trade_id']}", paras, author=doc["issuer"], created=created), doc


def invoice_part(
    d: Desk,
    deal: Mapping[str, Any],
    products: Sequence[Product],
    *,
    number: str,
    issue_date: date,
    due: date,
    pay_to: str,
    bl_numbers: Mapping[str, str],
    quantities: Mapping[str, Decimal] | None = None,
) -> tuple[Part, Decimal]:
    """One commercial invoice for every product; ``quantities`` overrides the invoiced quantity (planted errors)."""
    qtys = quantities or {p.key: p.bl_quantity for p in products}
    lines: list[InvLine] = [
        priced(f"{p.name}, B/L {bl_numbers[p.key]}", qtys[p.key], p.unit, p.final_price()) for p in products
    ]
    att, total = invoice(
        d,
        number=number,
        filename="invoice.pdf",
        title=f"Commercial invoice {number}",
        trade_id=deal["trade_id"],
        issuer_id=deal["seller"],
        bill_to_id=deal["buyer"],
        issue_date=issue_date,
        due=due,
        lines=lines,
        currency="USD",
        pay_to=pay_to,
        extra=[("Vessel", deal["vessel"]), ("Payment terms", f"{deal['payment_days']} days after B/L date")],
    )
    doc = _doc(
        "invoice",
        "invoice",
        [p.key for p in products],
        number=number,
        issuer=d.org(deal["seller"])["name"],
        date=issue_date.isoformat(),
        due=due.isoformat(),
        quantities={p.key: num(qtys[p.key]) for p in products},
        prices={p.key: num(p.final_price()) for p in products},
        amounts={p.key: num(ln.amount) for p, ln in zip(products, lines, strict=True)},
        total=num(total),
    )
    return Part(att.content, [doc]), total


def quote_rows(dates: Sequence[date], quotes: Sequence[Decimal]) -> list[list[Cell]]:
    """Header plus one row per quotation date."""
    rows: list[list[Cell]] = [["Date", "Assessment (USD)"]]
    rows += [[day.isoformat(), price] for day, price in zip(dates, quotes, strict=True)]
    return rows


def pricing_workbook(
    deal: Mapping[str, Any], products: Sequence[Product], quote_dates: Sequence[date], *, author: str, created: datetime
) -> tuple[bytes, dict[str, Any]]:
    """The seller's floating-price backup: a summary sheet and one sheet of quotations per floating product."""
    rows: list[list[Any]] = [["Product", "Pricing", "Index", "Average", "Differential", "Final price", "Unit"]]
    sheets = []
    for p in products:
        if p.floating:
            avg = sum(p.quotes, Decimal(0)) / len(p.quotes)
            rows.append([p.name, "floating", p.index, avg, p.differential, p.final_price(), f"USD/{p.unit}"])
            sheets.append(
                Sheet(
                    p.name[:31],
                    quote_rows(quote_dates, p.quotes),
                )
            )
        else:
            rows.append([p.name, "fixed", None, None, None, p.fixed_price, f"USD/{p.unit}"])
    content = build_xlsx(
        [Sheet("Summary", rows), *sheets], title=f"Pricing {deal['trade_id']}", author=author, created=created
    )
    doc = _doc(
        "pricing",
        "price-workbook",
        [p.key for p in products],
        issuer=author,
        supporting=True,
        prices={p.key: num(p.final_price()) for p in products},
    )
    return content, doc


def quantity_report(
    deal: Mapping[str, Any],
    products: Sequence[Product],
    bl_numbers: Mapping[str, str],
    shore: Mapping[str, Decimal],
    *,
    author: str,
    created: datetime,
) -> tuple[bytes, dict[str, Any]]:
    """An inspector's load-port quantity summary (supporting, not a required document)."""
    rows: list[list[Any]] = [
        ["Product", "B/L number", "B/L quantity", "Shore tank quantity", "Difference", "Difference %", "Unit"]
    ]
    for p in products:
        diff = shore[p.key] - p.bl_quantity
        rows.append(
            [p.name, bl_numbers[p.key], p.bl_quantity, shore[p.key], diff, q3(diff / p.bl_quantity * 100), p.unit]
        )
    content = build_xlsx(
        [Sheet("Quantities", rows)], title=f"Load port quantities {deal['trade_id']}", author=author, created=created
    )
    doc = _doc(
        "quantity-report",
        "quantity-report",
        [p.key for p in products],
        issuer=author,
        supporting=True,
        quantities={p.key: num(p.bl_quantity) for p in products},
    )
    return content, doc


# --- recorder -----------------------------------------------------------------------------------
class Docset:
    """Collects the files and logical documents of one document set while the scenario sends them."""

    def __init__(self, d: Desk, docset_id: str, deal: dict[str, Any]) -> None:
        self.d, self.id, self.deal = d, docset_id, deal
        self.files: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []

    def mail(self, key: str, frm: str, to: str, text: str, *items: Item, **kw: Any) -> dict[str, Any]:
        rec = self.d.mail(key, frm, to, text, *[i.attachment for i in items], **kw)
        for it in items:
            for doc in it.docs:
                doc["message"] = key
                doc["source"] = self.d.addr(frm)
                self.documents.append(doc)
            self.files.append(
                {
                    "file": it.attachment.filename,
                    "message": key,
                    "format": it.attachment.facts["format"],
                    "page_count": it.attachment.facts.get("page_count"),
                    "documents": [doc["doc_id"] for doc in it.docs],
                    "pages": it.pages,
                }
            )
        return rec

    def finish(self) -> dict[str, Any]:
        rec = {
            "id": self.id,
            "trade_id": self.deal["trade_id"],
            "deal": self.deal,
            "files": self.files,
            "documents": self.documents,
        }
        rec["reconciliation"] = reconcile(rec)
        self.d.sc.docsets.append(rec)
        return rec


# --- reconciliation ---------------------------------------------------------------------------------
def reconcile(docset: Mapping[str, Any]) -> dict[str, Any]:
    """Check counts, quantities and prices of a document set against its confirmation.

    Pure function of the ground truth (``deal`` and ``documents``), so a test can recompute it from a manifest.
    """
    deal, docs = docset["deal"], [x for x in docset["documents"] if not x.get("supporting")]
    names = {p["key"]: p["name"] for p in deal["products"]}
    counts: list[dict[str, Any]] = []
    exceptions: list[str] = []

    def row(kind: str, product: str | None, unit: str, required: int, found: int) -> None:
        ok = found >= required
        counts.append({"kind": kind, "product": product, "unit": unit, "required": required, "found": found, "ok": ok})
        if not ok:
            where = names[product] if product else "the set"
            exceptions.append(f"{kind} {unit} for {where}: {found} of {required}")

    for req in deal["requirements"]:
        if req["per"] == "product":
            for key in names:
                mine = [x for x in docs if x["kind"] == req["kind"] and key in x["products"]]
                if req["kind"] == "bill-of-lading":
                    row(req["kind"], key, "originals", req["originals"], len({n for x in mine for n in x["originals"]}))
                    row(req["kind"], key, "copies", req["copies"], sum(x["copies"] for x in mine))
                else:
                    row(req["kind"], key, "documents", req["count"], len(mine))
        else:
            row(req["kind"], None, "documents", req["count"], len([x for x in docs if x["kind"] == req["kind"]]))

    def qty(kind: str, key: str) -> Any:
        found = [x["quantities"][key] for x in docs if x["kind"] == kind and key in x.get("quantities", {})]
        return found[0] if found else None

    quantities, prices = [], []
    for p in deal["products"]:
        key = p["key"]
        values = {k: qty(k, key) for k in ("bill-of-lading", "quantity-certificate", "letter-of-indemnity", "invoice")}
        bl = values["bill-of-lading"]
        within = bl is not None and p["quantity_min"] <= bl <= p["quantity_max"]
        agree = bl is not None and all(v == bl for v in values.values())
        quantities.append(
            {
                "product": key,
                "range": [p["quantity_min"], p["quantity_max"]],
                **values,
                "within_range": within,
                "documents_agree": agree,
            }
        )
        if bl is not None and not within:
            exceptions.append(
                f"B/L quantity for {p['name']} {bl} {p['unit']} is outside {p['quantity_min']} to {p['quantity_max']}"
            )
        for k, v in values.items():
            if k != "bill-of-lading" and v is not None and bl is not None and v != bl:
                exceptions.append(
                    f"{k} quantity for {p['name']} {v} {p['unit']} differs from the B/L quantity {bl} {p['unit']}"
                )
        inv = next((x for x in docs if x["kind"] == "invoice"), None)
        invoiced = inv["prices"].get(key) if inv else None
        ok = invoiced == p["final_price"]
        prices.append(
            {"product": key, "type": p["price_type"], "expected": p["final_price"], "invoiced": invoiced, "ok": ok}
        )
        if invoiced is not None and not ok:
            exceptions.append(
                f"invoiced price for {p['name']} {invoiced} differs from the confirmed price {p['final_price']}"
            )

    inv = next((x for x in docs if x["kind"] == "invoice"), None)
    arithmetic = None
    if inv:
        lines_ok = all(
            dec(inv["amounts"][k]) == q2(dec(inv["quantities"][k]) * dec(inv["prices"][k])) for k in inv["amounts"]
        )
        arithmetic = lines_ok and dec(inv["total"]) == sum((dec(v) for v in inv["amounts"].values()), Decimal(0))
        if not arithmetic:
            exceptions.append("invoice lines or total do not add up")
    return {
        "result": "pass" if not exceptions else "fail",
        "counts": counts,
        "quantities": quantities,
        "prices": prices,
        "invoice_arithmetic_ok": arithmetic,
        "exceptions": exceptions,
    }
