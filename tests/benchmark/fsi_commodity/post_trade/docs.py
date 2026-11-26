# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Document builders for the post-trade phase: confirmations, logistics documents, invoices, notes, margin calls.

Every figure on an invoice, note or margin call is computed here from figures that are printed on the same
document (quantity times price, sums, differences). The facts dict repeats every input so a test can recompute
the result from the manifest.
"""

# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ..ontology import cls
from ..scenario import Attachment
from ..writers.pdf_writer import Block, Grid, Heading, Mono, Text, build_pdf
from .common import Desk, dec, fmt, form, num, q2, show

_SKIP = ("trade_id", "issued_by", "version")


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


# --- confirmations ----------------------------------------------------------------------------------
def confirmation(
    d: Desk,
    *,
    filename: str,
    title: str,
    trade_id: str,
    issuer_id: str,
    to_id: str,
    confirm_date: date,
    terms: Mapping[str, Any],
    based_on: str,
    class_uri: str,
    kind: str = "trade-confirmation",
    extra: Sequence[tuple[str, str]] = (),
    facts: Mapping[str, Any] | None = None,
) -> Attachment:
    """A confirmation built from ``terms`` (the latest ticket's facts, or the stated variant of them)."""
    body = {k: v for k, v in terms.items() if k not in _SKIP}
    pairs = [
        ("Trade id", trade_id),
        ("Confirmation date", confirm_date.isoformat()),
        ("Issued by", d.org(issuer_id)["name"]),
        ("To", d.org(to_id)["name"]),
        ("Based on", based_on),
    ]
    pairs += [(_label(k), show(k, v)) for k, v in body.items()]
    pairs += list(extra)
    data = {
        "trade_id": trade_id,
        "issued_by": d.org(issuer_id)["name"],
        "confirmation_date": confirm_date.isoformat(),
        "based_on": based_on,
        **body,
        **(facts or {}),
    }
    return form(filename, kind, title, pairs, class_uri, data, intro="Please check the terms and sign back.")


# --- logistics --------------------------------------------------------------------------------------
def nomination(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    issuer_id: str,
    to_id: str,
    vessel: str,
    port: str,
    laycan_from: date,
    laycan_to: date,
    quantity: int,
    unit: str,
    product: str,
    notice_date: date,
) -> Attachment:
    facts = {
        "trade_id": trade_id,
        "nominated_by": d.org(issuer_id)["name"],
        "vessel": vessel,
        "port": port,
        "laycan_from": laycan_from.isoformat(),
        "laycan_to": laycan_to.isoformat(),
        "quantity": quantity,
        "unit": unit,
        "product": product,
        "notice_date": notice_date.isoformat(),
    }
    pairs = [
        ("Trade id", trade_id),
        ("Nominated by", d.org(issuer_id)["name"]),
        ("To", d.org(to_id)["name"]),
        ("Vessel", vessel),
        ("Port", port),
        ("Laycan", f"{laycan_from.isoformat()} to {laycan_to.isoformat()}"),
        ("Product", product),
        ("Quantity", f"{quantity:,} {unit}"),
        ("Notice date", notice_date.isoformat()),
    ]
    return form(
        filename,
        "nomination",
        f"Vessel nomination: {trade_id}",
        pairs,
        cls("TransportMovement"),
        facts,
    )


def bill_of_lading(
    d: Desk,
    *,
    number: str,
    filename: str,
    trade_id: str,
    shipper_id: str,
    consignee_id: str,
    notify_id: str,
    vessel: str,
    load_port: str,
    discharge_port: str,
    bl_date: date,
    product: str,
    quantity: Decimal,
    unit: str,
) -> Attachment:
    """A bill of lading with the DCSA parties and one cargo item; the quantity is the loaded quantity."""
    facts = {
        "bl_number": number,
        "trade_id": trade_id,
        "shipper": d.org(shipper_id)["name"],
        "consignee": d.org(consignee_id)["name"],
        "notify_party": d.org(notify_id)["name"],
        "vessel": vessel,
        "load_port": load_port,
        "discharge_port": discharge_port,
        "bl_date": bl_date.isoformat(),
        "product": product,
        "quantity": num(quantity),
        "unit": unit,
        "class_uris": {
            "shipper": cls("Shipper"),
            "consignee": cls("Consignee"),
            "notify_party": cls("NotifyParty"),
            "cargo_item": cls("CargoItem"),
            "consignment": cls("Consignment"),
        },
    }
    pairs = [
        ("B/L number", number),
        ("Trade id", trade_id),
        ("Shipper", facts["shipper"]),
        ("Consignee", facts["consignee"]),
        ("Notify party", facts["notify_party"]),
        ("Vessel", vessel),
        ("Port of loading", load_port),
        ("Port of discharge", discharge_port),
        ("Date of issue", bl_date.isoformat()),
        ("Cargo item", f"{product}, {fmt(quantity, 3)} {unit} shipped on board"),
    ]
    return form(filename, "bill-of-lading", f"Bill of lading {number}", pairs, cls("BillOfLading"), facts)


def inspection_certificate(
    d: Desk,
    *,
    filename: str,
    kind: str,
    title: str,
    trade_id: str,
    cert_date: date,
    place: str,
    results: Sequence[tuple[str, str]],
    facts: Mapping[str, Any],
    fee: Decimal | None = None,
) -> Attachment:
    """A certificate issued by Cardinal Inspection Services (quantity, quality or discharge)."""
    cardinal = d.org("svc-cardinal")["name"]
    pairs = [("Trade id", trade_id), ("Issued by", cardinal), ("Date", cert_date.isoformat()), ("Place", place)]
    pairs += list(results)
    data: dict[str, Any] = {
        "trade_id": trade_id,
        "issued_by": cardinal,
        "date": cert_date.isoformat(),
        "place": place,
        **facts,
        "class_uris": {"quantity": cls("QuantityElement"), "event": cls("ObjectEvent")},
    }
    if fee is not None:
        pairs.append(("Inspection fee, total (USD)", fmt(fee)))
        data["inspection_fee_usd"] = num(fee)
    return form(filename, kind, title, pairs, cls("SpecifiedInspection"), data)


def assessments(
    d: Desk, *, filename: str, trade_id: str, rows: Sequence[tuple[date, Decimal]], source: str
) -> tuple[Attachment, Decimal]:
    """The price assessments behind a floating price; returns the sheet and the printed average."""
    average = sum((p for _, p in rows), Decimal(0)) / len(rows)
    facts = {
        "trade_id": trade_id,
        "source": source,
        "assessments": [{"date": day.isoformat(), "price": num(p)} for day, p in rows],
        "average": num(average),
    }
    grid = Grid(("Date", "Assessment (USD per unit)"), tuple((day.isoformat(), f"{p:.3f}") for day, p in rows))
    att = form(
        filename,
        "price-assessments",
        f"Price assessments: {trade_id}",
        [("Trade id", trade_id), ("Source", source), ("Average of the five", f"{average:.3f}")],
        cls("MonetaryAmount"),
        facts,
        grid=grid,
    )
    return att, average


# --- invoices and notes -----------------------------------------------------------------------------
@dataclass(frozen=True)
class InvLine:
    """One invoice line. ``amount`` is ``quantity * price`` rounded to cents when both are given."""

    description: str
    amount: Decimal
    quantity: Decimal | None = None
    unit: str = ""
    price: Decimal | None = None

    def row(self) -> tuple[str, str, str, str, str]:
        qty = (
            ""
            if self.quantity is None
            else fmt(self.quantity, 0 if self.quantity == self.quantity.to_integral_value() else 3)
        )
        price = (
            ""
            if self.price is None
            else (f"{self.price:,.2f}" if q2(self.price) == self.price else f"{self.price:,.4f}")
        )
        return (self.description, qty, self.unit, price, fmt(self.amount))

    def fact(self) -> dict[str, Any]:
        out: dict[str, Any] = {"description": self.description, "amount": num(self.amount)}
        if self.quantity is not None:
            out |= {
                "quantity": num(self.quantity),
                "unit": self.unit,
                "price": None if self.price is None else num(self.price),
            }
        return out


def priced(description: str, quantity: Decimal | int, unit: str, price: Decimal) -> InvLine:
    qty = dec(quantity)
    return InvLine(description, q2(qty * price), qty, unit, price)


def acct_block(d: Desk, acct_id: str) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Payment details of an account, from ``universe.json``."""
    a = d.u["accounts"][acct_id]
    code_label = "IBAN" if a["iban"] else "ABA" if a["aba"] else "BSB"
    code = a["iban"] or a["aba"] or a["bsb"]
    pairs = [
        ("Pay to bank", d.org(a["bank"])["name"]),
        ("Account number", a["number"]),
        ("BIC", a["bic"]),
        (code_label, code),
    ]
    facts = {
        "account_id": acct_id,
        "bank": d.org(a["bank"])["name"],
        "number": a["number"],
        "bic": a["bic"],
        code_label.lower(): code,
    }
    return pairs, facts


def invoice(
    d: Desk,
    *,
    number: str,
    filename: str,
    title: str,
    trade_id: str,
    issuer_id: str,
    bill_to_id: str,
    issue_date: date,
    due: date,
    lines: Sequence[InvLine],
    currency: str,
    pay_to: str,
    deductions: Sequence[tuple[str, Decimal]] = (),
    extra: Sequence[tuple[str, str]] = (),
    notes: Sequence[str] = (),
    facts: Mapping[str, Any] | None = None,
    kind: str = "invoice",
) -> tuple[Attachment, Decimal]:
    """An invoice. Printed total = sum of lines minus deductions; returns the attachment and that total."""
    subtotal = sum((ln.amount for ln in lines), Decimal(0))
    total = subtotal - sum((amt for _, amt in deductions), Decimal(0))
    rows = [ln.row() for ln in lines]
    rows.append(("Subtotal", "", "", "", fmt(subtotal)))
    rows += [(f"Less: {label}", "", "", "", f"-{fmt(amt)}") for label, amt in deductions]
    rows.append((f"Total due ({currency})", "", "", "", fmt(total)))
    acct_pairs, acct_facts = acct_block(d, pay_to)
    pairs = [
        ("Invoice number", number),
        ("Trade id", trade_id),
        ("Issued by", d.org(issuer_id)["name"]),
        ("Billed to", d.org(bill_to_id)["name"]),
        ("Invoice date", issue_date.isoformat()),
        ("Due date", due.isoformat()),
        ("Currency", currency),
        *extra,
        *acct_pairs,
    ]
    data = {
        "number": number,
        "trade_id": trade_id,
        "issued_by": d.org(issuer_id)["name"],
        "billed_to": d.org(bill_to_id)["name"],
        "issue_date": issue_date.isoformat(),
        "due_date": due.isoformat(),
        "currency": currency,
        "lines": [ln.fact() for ln in lines],
        "subtotal": num(subtotal),
        "deductions": [{"description": label, "amount": num(amt)} for label, amt in deductions],
        "total_due": num(total),
        "pay_to": acct_facts,
        "class_uris": {"amount": cls("MonetaryAmount")},
        **(facts or {}),
    }
    att = form(
        filename,
        kind,
        title,
        pairs,
        cls("Invoice"),
        data,
        grid=Grid(("Description", "Quantity", "Unit", "Price", f"Amount ({currency})"), tuple(rows)),
        notes=list(notes),
    )
    return att, total


def credit_note(
    d: Desk,
    *,
    number: str,
    filename: str,
    trade_id: str,
    issuer_id: str,
    to_id: str,
    issue_date: date,
    invoice_number: str,
    invoice_total: Decimal,
    credit: InvLine,
    currency: str,
    reason: str,
    facts: Mapping[str, Any] | None = None,
) -> tuple[Attachment, Decimal]:
    """A credit note against one invoice; returns the attachment and the net payable (invoice minus credit)."""
    net = invoice_total - credit.amount
    pairs = [
        ("Credit note number", number),
        ("Against invoice", invoice_number),
        ("Trade id", trade_id),
        ("Issued by", d.org(issuer_id)["name"]),
        ("To", d.org(to_id)["name"]),
        ("Date", issue_date.isoformat()),
        ("Reason", reason),
        (f"Original invoice total ({currency})", fmt(invoice_total)),
        (f"Credit ({currency})", fmt(credit.amount)),
        (f"Net payable after credit ({currency})", fmt(net)),
    ]
    data = {
        "number": number,
        "against_invoice": invoice_number,
        "trade_id": trade_id,
        "issued_by": d.org(issuer_id)["name"],
        "issue_date": issue_date.isoformat(),
        "currency": currency,
        "invoice_total": num(invoice_total),
        "credit_lines": [credit.fact()],
        "credit_total": num(credit.amount),
        "net_payable": num(net),
        "reason": reason,
        "class_uris": {"amount": cls("MonetaryAmount")},
        **(facts or {}),
    }
    grid = Grid(("Description", "Quantity", "Unit", "Price", f"Amount ({currency})"), (credit.row(),))
    att = form(filename, "credit-note", f"Credit note {number}", pairs, cls("Invoice"), data, grid=grid)
    return att, net


def credit_advice(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    bank_id: str,
    account_id: str,
    value_date: date,
    amount: Decimal,
    currency: str,
    reference: str,
    originator: str,
) -> Attachment:
    """A bank credit advice for an incoming payment."""
    acct_pairs, acct_facts = acct_block(d, account_id)
    pairs = [
        ("Advice from", d.org(bank_id)["name"]),
        ("Value date", value_date.isoformat()),
        ("Credited amount", f"{currency} {fmt(amount)}"),
        ("Originator", originator),
        ("Payment reference", reference),
        *[(k.replace("Pay to bank", "Account held at"), v) for k, v in acct_pairs],
    ]
    facts = {
        "trade_id": trade_id,
        "bank": d.org(bank_id)["name"],
        "value_date": value_date.isoformat(),
        "amount": num(amount),
        "currency": currency,
        "reference": reference,
        "originator": originator,
        "account": acct_facts,
    }
    return form(filename, "credit-advice", f"Credit advice: {reference}", pairs, cls("MonetaryAmount"), facts)


def letter_of_indemnity(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    issuer_id: str,
    to_id: str,
    bank_id: str,
    bl_number: str,
    vessel: str,
    cargo_quantity: Decimal,
    unit: str,
    price: Decimal,
    cover_pct: int,
    currency: str,
    issue_date: date,
) -> Attachment:
    """A letter of indemnity counter-signed by a bank for ``cover_pct`` percent of the cargo value."""
    cargo_value = q2(cargo_quantity * price)
    cover = q2(cargo_value * cover_pct / 100)
    pairs = [
        ("Trade id", trade_id),
        ("Indemnifier", d.org(issuer_id)["name"]),
        ("Indemnified party", d.org(to_id)["name"]),
        ("Counter-signed by", d.org(bank_id)["name"]),
        ("Date", issue_date.isoformat()),
        ("Bill of lading", bl_number),
        ("Vessel", vessel),
        ("Cargo quantity", f"{fmt(cargo_quantity)} {unit}"),
        ("Price", f"{currency} {price:,.2f} per {unit}"),
        ("Cargo value", f"{currency} {fmt(cargo_value)}"),
        ("Cover", f"{cover_pct}% of cargo value"),
        ("Amount of indemnity", f"{currency} {fmt(cover)}"),
    ]
    facts = {
        "trade_id": trade_id,
        "indemnifier": d.org(issuer_id)["name"],
        "indemnified": d.org(to_id)["name"],
        "counter_signed_by": d.org(bank_id)["name"],
        "bl_number": bl_number,
        "cargo_quantity": num(cargo_quantity),
        "unit": unit,
        "price": num(price),
        "currency": currency,
        "cargo_value": num(cargo_value),
        "cover_pct": cover_pct,
        "indemnity_amount": num(cover),
        "issue_date": issue_date.isoformat(),
    }
    return form(
        filename,
        "letter-of-indemnity",
        f"Letter of indemnity: {bl_number}",
        pairs,
        cls("BilateralContract"),
        facts,
        notes=["The indemnifier will deliver the original bill of lading when it arrives."],
    )


def check_sheet(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    invoice_number: str,
    rows: Sequence[tuple[str, Decimal, Decimal]],
    currency: str,
    prepared_by: str,
) -> tuple[Attachment, Decimal]:
    """Invoice check sheet: per line the amount invoiced, the amount expected from the deal, and the difference."""
    diffs = [(label, billed, expected, billed - expected) for label, billed, expected in rows]
    total = sum((x[3] for x in diffs), Decimal(0))
    grid = Grid(
        ("Line", "Invoiced", "Expected from the deal", "Difference"),
        tuple((label, fmt(b), fmt(e), fmt(diff)) for label, b, e, diff in diffs)
        + (("Total difference", "", "", fmt(total)),),
    )
    facts = {
        "trade_id": trade_id,
        "invoice_number": invoice_number,
        "currency": currency,
        "prepared_by": prepared_by,
        "rows": [
            {"line": label, "invoiced": num(b), "expected": num(e), "difference": num(diff)}
            for label, b, e, diff in diffs
        ],
        "total_difference": num(total),
    }
    att = form(
        filename,
        "invoice-check-sheet",
        f"Invoice check sheet: {invoice_number}",
        [("Trade id", trade_id), ("Invoice", invoice_number), ("Prepared by", prepared_by), ("Currency", currency)],
        cls("MonetaryAmount"),
        facts,
        grid=grid,
    )
    return att, total


# --- margin call and SWIFT-style copy ---------------------------------------------------------------
def margin_call(
    d: Desk,
    *,
    filename: str,
    number: str,
    trade_id: str,
    issuer_id: str,
    to_id: str,
    valuation_date: date,
    transfer_by: date,
    fixed_price: Decimal,
    forward_price: Decimal,
    volume: int,
    threshold_applied: Decimal,
    collateral_held: Decimal,
    min_transfer: Decimal,
    currency: str,
    revision: int,
    facts: Mapping[str, Any] | None = None,
) -> tuple[Attachment, dict[str, Decimal]]:
    """A variation margin call. Exposure = (fixed - forward) * volume; call = exposure - threshold - collateral held."""
    exposure = q2((fixed_price - forward_price) * volume)
    call = max(exposure - threshold_applied - collateral_held, Decimal(0))
    if call < min_transfer:
        call = Decimal(0)
    pairs = [
        ("Margin call number", number),
        ("Revision", str(revision)),
        ("Trade id", trade_id),
        ("Called by", d.org(issuer_id)["name"]),
        ("Called from", d.org(to_id)["name"]),
        ("Valuation date", valuation_date.isoformat()),
        ("Transfer by", transfer_by.isoformat()),
        ("Fixed price (USD per bbl)", f"{fixed_price:.2f}"),
        ("Average forward price, remaining term (USD per bbl)", f"{forward_price:.2f}"),
        ("Remaining volume (bbl)", f"{volume:,}"),
        (f"Exposure ({currency})", fmt(exposure)),
        (f"Threshold applied ({currency})", fmt(threshold_applied)),
        (f"Collateral held ({currency})", fmt(collateral_held)),
        (f"Minimum transfer ({currency})", fmt(min_transfer)),
        (f"Amount called ({currency})", fmt(call)),
    ]
    inputs = {
        "number": number,
        "revision": revision,
        "trade_id": trade_id,
        "valuation_date": valuation_date.isoformat(),
        "transfer_by": transfer_by.isoformat(),
        "fixed_price": num(fixed_price),
        "forward_price": num(forward_price),
        "volume_bbl": volume,
        "threshold_applied": num(threshold_applied),
        "collateral_held": num(collateral_held),
        "min_transfer": num(min_transfer),
        "currency": currency,
        "exposure": num(exposure),
        "amount_called": num(call),
        "called_by": d.org(issuer_id)["name"],
        "called_from": d.org(to_id)["name"],
        "class_uris": {"contract_type": cls("CLM"), "agreement": cls("CollateralAgreement")},
        **(facts or {}),
    }
    att = form(filename, "margin-call", f"Variation margin call {number}", pairs, cls("CLM"), inputs)
    return att, {"exposure": exposure, "call": call}


def swift_copy(
    d: Desk,
    *,
    filename: str,
    title: str,
    trade_id: str,
    bank_id: str,
    party_id: str,
    ticket: Mapping[str, Any],
    pay_account: str,
    pay_account_shown: str,
    version: int,
) -> Attachment:
    """A SWIFT-style text copy of a forward confirmation (own simplified layout, not a published MT format)."""
    bank_bic = d.u["accounts"][pay_account]["bic"]
    lines = [
        f"{{1:F01{bank_bic}0000000000}}{{2:O300}}",
        f":20:{trade_id}-V{version}",
        ":22A:NEWT",
        f":82A:{bank_bic}",
        f":30T:{ticket['value_date'].replace('-', '')}",
        f":36:{ticket['rate']:.4f}",
        f":32B:{ticket['buy_currency']}{ticket['buy_amount']},00",
        f":33B:{ticket['sell_currency']}{ticket['sell_amount']},00",
        f":53A:{ticket['sell_currency']} DEBIT ACCOUNT {pay_account_shown}",
    ]
    facts = {
        "trade_id": trade_id,
        "version": version,
        "bank": d.org(bank_id)["name"],
        "party": d.org(party_id)["name"],
        "value_date": ticket["value_date"],
        "rate": ticket["rate"],
        "buy_currency": ticket["buy_currency"],
        "buy_amount": ticket["buy_amount"],
        "sell_currency": ticket["sell_currency"],
        "sell_amount": ticket["sell_amount"],
        "debit_account": pay_account_shown,
    }
    blocks: list[Block] = [Heading("Message text"), Mono("\n".join(lines)), Text("Copy of the message as sent.")]
    return Attachment(filename, build_pdf(title, blocks), "swift-copy", cls("SettlementTerms"), facts)


__all__ = [
    "InvLine",
    "acct_block",
    "assessments",
    "bill_of_lading",
    "check_sheet",
    "confirmation",
    "credit_advice",
    "credit_note",
    "inspection_certificate",
    "invoice",
    "letter_of_indemnity",
    "margin_call",
    "nomination",
    "priced",
    "swift_copy",
]
