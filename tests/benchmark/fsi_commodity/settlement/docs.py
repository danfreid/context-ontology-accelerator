# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Document builders for the settlement phase: releases, bank advices, statements, reconciliations, netting.

Every figure printed on a document is computed here from figures printed on the same document; the facts dict
repeats every input so a test can recompute the result from the manifest.
"""

# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..ontology import cls
from ..post_trade.docs import acct_block
from ..scenario import Attachment
from ..writers.pdf_writer import Grid
from .common import Desk, fmt, form, num, q2

PRICE = Decimal("0.0001")


def _acct_party(d: Desk, acct_id: str, label: str) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    pairs, facts = acct_block(d, acct_id)
    return [(f"{label}: {k.lower()}" if k != "Pay to bank" else f"{label}: bank", v) for k, v in pairs], facts


# --- release, advices and notices --------------------------------------------------------------
def release_request(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    requested_by: str,
    approver: str,
    payee_id: str,
    amount: Decimal,
    currency: str,
    value_date: date,
    limit: Decimal,
    from_account: str,
    to_account: str,
    reference: str,
) -> Attachment:
    """A treasury payment release form. A release above ``limit`` needs a second approver."""
    needs_second = amount > limit
    pairs = [
        ("Trade id", trade_id),
        ("Payee", d.org(payee_id)["name"]),
        ("Amount", f"{currency} {fmt(amount)}"),
        ("Value date", value_date.isoformat()),
        ("Payment reference", reference),
        ("Single release limit", f"{currency} {fmt(limit)}"),
        ("Second approval required", "yes" if needs_second else "no"),
        ("Requested by", requested_by),
        ("Second approver", approver if needs_second else "not required"),
        *_acct_party(d, from_account, "Debit account")[0],
        *_acct_party(d, to_account, "Beneficiary account")[0],
    ]
    facts = {
        "trade_id": trade_id,
        "payee": d.org(payee_id)["name"],
        "amount": num(amount),
        "currency": currency,
        "value_date": value_date.isoformat(),
        "reference": reference,
        "release_limit": num(limit),
        "second_approval_required": needs_second,
        "debit_account": from_account,
        "beneficiary_account": to_account,
    }
    return form(
        filename,
        "payment-release-request",
        f"Treasury payment release: {reference}",
        pairs,
        cls("MonetaryAmount"),
        facts,
    )


def debit_advice(
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
    beneficiary: str,
) -> Attachment:
    """A bank debit advice for an outgoing payment."""
    pairs, acct_facts = _acct_party(d, account_id, "Debited account")
    all_pairs = [
        ("Advice from", d.org(bank_id)["name"]),
        ("Value date", value_date.isoformat()),
        ("Debited amount", f"{currency} {fmt(amount)}"),
        ("Beneficiary", beneficiary),
        ("Payment reference", reference),
        *pairs,
    ]
    facts = {
        "trade_id": trade_id,
        "bank": d.org(bank_id)["name"],
        "value_date": value_date.isoformat(),
        "amount": num(amount),
        "currency": currency,
        "reference": reference,
        "beneficiary": beneficiary,
        "account": acct_facts,
    }
    return form(filename, "debit-advice", f"Debit advice: {reference}", all_pairs, cls("MonetaryAmount"), facts)


def payment_notice(
    d: Desk,
    *,
    filename: str,
    kind: str,
    title: str,
    trade_id: str,
    payer_id: str,
    payee_id: str,
    value_date: date,
    amount: Decimal,
    currency: str,
    reference: str,
    from_account: str,
    to_account: str,
    covers: Sequence[tuple[str, str]] = (),
) -> Attachment:
    """A notice from the payer to the payee that a payment has been instructed."""
    to_pairs, to_facts = _acct_party(d, to_account, "Paid to")
    pairs = [
        ("Trade id", trade_id),
        ("Payer", d.org(payer_id)["name"]),
        ("Payee", d.org(payee_id)["name"]),
        ("Amount", f"{currency} {fmt(amount)}"),
        ("Value date", value_date.isoformat()),
        ("Payment reference", reference),
        *covers,
        *to_pairs,
    ]
    facts = {
        "trade_id": trade_id,
        "payer": d.org(payer_id)["name"],
        "payee": d.org(payee_id)["name"],
        "amount": num(amount),
        "currency": currency,
        "value_date": value_date.isoformat(),
        "reference": reference,
        "from_account": from_account,
        "to_account": to_facts,
        "covers": {k: v for k, v in covers},
    }
    return form(filename, kind, title, pairs, cls("MonetaryAmount"), facts)


def settlement_credit_advice(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    bank_id: str,
    account_id: str,
    value_date: date,
    instructed: Decimal,
    charges: Decimal,
    currency: str,
    reference: str,
    originator: str,
    charge_note: str = "",
) -> tuple[Attachment, Decimal]:
    """A bank credit advice: instructed amount less deducted charges equals the credited amount."""
    credited = instructed - charges
    pairs, acct_facts = _acct_party(d, account_id, "Credited account")
    all_pairs = [
        ("Advice from", d.org(bank_id)["name"]),
        ("Value date", value_date.isoformat()),
        ("Instructed amount", f"{currency} {fmt(instructed)}"),
        ("Charges deducted", f"{currency} {fmt(charges)}"),
        ("Credited amount", f"{currency} {fmt(credited)}"),
        ("Originator", originator),
        ("Payment reference", reference),
        *pairs,
    ]
    if charge_note:
        all_pairs.append(("Charge note", charge_note))
    facts = {
        "trade_id": trade_id,
        "bank": d.org(bank_id)["name"],
        "value_date": value_date.isoformat(),
        "instructed": num(instructed),
        "charges": num(charges),
        "amount": num(credited),
        "currency": currency,
        "reference": reference,
        "originator": originator,
        "account": acct_facts,
    }
    att = form(filename, "credit-advice", f"Credit advice: {reference}", all_pairs, cls("MonetaryAmount"), facts)
    return att, credited


# --- statements and reconciliations ---------------------------------------------------------
def account_statement(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    issuer_id: str,
    to_id: str,
    invoice_number: str,
    balance: Decimal,
    due: date,
    as_of: date,
    currency: str,
    pay_to: str,
) -> Attachment:
    """A statement of account that reminds the debtor of an open balance."""
    days_over = (as_of - due).days
    pairs = [
        ("Trade id", trade_id),
        ("From", d.org(issuer_id)["name"]),
        ("To", d.org(to_id)["name"]),
        ("Statement date", as_of.isoformat()),
        ("Invoice", invoice_number),
        ("Due date", due.isoformat()),
        ("Days overdue", str(days_over)),
        (f"Open balance ({currency})", fmt(balance)),
        *acct_block(d, pay_to)[0],
    ]
    facts = {
        "trade_id": trade_id,
        "invoice_number": invoice_number,
        "balance": num(balance),
        "currency": currency,
        "due": due.isoformat(),
        "as_of": as_of.isoformat(),
        "days_overdue": days_over,
        "pay_to": acct_block(d, pay_to)[1],
    }
    return form(filename, "account-statement", f"Statement of account: {invoice_number}", pairs, cls("Invoice"), facts)


def late_interest_statement(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    issuer_id: str,
    to_id: str,
    balance: Decimal,
    rate_pct: Decimal,
    due: date,
    paid: date,
    currency: str,
    clause: str,
    waived: bool,
) -> tuple[Attachment, Decimal]:
    """Late-payment interest: balance x rate x days / 365, rounded to cents; optionally waived."""
    days = (paid - due).days
    interest = q2(balance * rate_pct / 100 * days / 365)
    pairs = [
        ("Trade id", trade_id),
        ("Issued by", d.org(issuer_id)["name"]),
        ("To", d.org(to_id)["name"]),
        ("Overdue balance", f"{currency} {fmt(balance)}"),
        ("Due date", due.isoformat()),
        ("Paid on", paid.isoformat()),
        ("Days late", str(days)),
        ("Rate, % a year", f"{rate_pct}"),
        ("Interest (balance x rate x days / 365)", f"{currency} {fmt(interest)}"),
        ("Clause", clause),
        ("Interest waived", "yes" if waived else "no"),
        ("Interest payable", f"{currency} {fmt(Decimal(0) if waived else interest)}"),
    ]
    facts = {
        "trade_id": trade_id,
        "balance": num(balance),
        "rate_pct": num(rate_pct),
        "due": due.isoformat(),
        "paid": paid.isoformat(),
        "days_late": days,
        "interest": num(interest),
        "waived": waived,
        "interest_payable": num(Decimal(0) if waived else interest),
        "currency": currency,
    }
    att = form(filename, "late-interest-statement", f"Late interest: {trade_id}", pairs, cls("MonetaryAmount"), facts)
    return att, interest


def cash_reconciliation(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    prepared_by: str,
    rows: Sequence[tuple[str, Decimal, Decimal]],
    currency: str,
    status: str,
) -> tuple[Attachment, Decimal]:
    """Cash reconciliation: per line the amount expected, the amount received and the difference."""
    diffs = [(label, exp, got, exp - got) for label, exp, got in rows]
    total = sum((x[3] for x in diffs), Decimal(0))
    grid = Grid(
        ("Line", "Expected", "Received", "Difference"),
        tuple((label, fmt(e), fmt(g), fmt(diff)) for label, e, g, diff in diffs)
        + (("Unreconciled difference", "", "", fmt(total)),),
    )
    facts = {
        "trade_id": trade_id,
        "prepared_by": prepared_by,
        "currency": currency,
        "status": status,
        "rows": [{"line": lb, "expected": num(e), "received": num(g), "difference": num(df)} for lb, e, g, df in diffs],
        "unreconciled": num(total),
    }
    att = form(
        filename,
        "cash-reconciliation",
        f"Cash reconciliation: {trade_id}",
        [("Trade id", trade_id), ("Prepared by", prepared_by), ("Status", status), ("Currency", currency)],
        cls("MonetaryAmount"),
        facts,
        grid=grid,
    )
    return att, total


def collateral_notice(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    from_id: str,
    to_id: str,
    amount: Decimal,
    currency: str,
    value_date: date,
    margin_call: str,
    csa: str,
    from_account: str,
    to_account: str,
) -> Attachment:
    """A collateral transfer notice that answers a margin call."""
    pairs = [
        ("Trade id", trade_id),
        ("Margin call", margin_call),
        ("Under", csa),
        ("Transferor", d.org(from_id)["name"]),
        ("Transferee", d.org(to_id)["name"]),
        ("Collateral", f"cash in {currency}"),
        ("Amount", f"{currency} {fmt(amount)}"),
        ("Value date", value_date.isoformat()),
        *_acct_party(d, from_account, "From account")[0],
        *_acct_party(d, to_account, "To account")[0],
    ]
    facts = {
        "trade_id": trade_id,
        "margin_call": margin_call,
        "amount": num(amount),
        "currency": currency,
        "value_date": value_date.isoformat(),
        "from_account": from_account,
        "to_account": to_account,
    }
    return form(
        filename,
        "collateral-transfer-notice",
        f"Collateral transfer: {margin_call}",
        pairs,
        cls("CollateralAgreement"),
        facts,
    )


def swap_statement(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    version: int,
    agent_id: str,
    to_id: str,
    statement_date: date,
    fixed_price: Decimal,
    notional: int,
    prices: Sequence[tuple[date, Decimal]],
    source: str,
    currency: str,
    note: str = "",
) -> tuple[Attachment, dict[str, Decimal]]:
    """A swap calculation statement. Average = mean of the daily prices (4 places); amount = (fixed - average) x notional."""
    average = (sum((p for _, p in prices), Decimal(0)) / len(prices)).quantize(PRICE, rounding=ROUND_HALF_UP)
    amount = q2((fixed_price - average) * notional)
    grid = Grid(("Pricing day", f"{source} (USD per bbl)"), tuple((day.isoformat(), f"{p:.2f}") for day, p in prices))
    pairs = [
        ("Trade id", trade_id),
        ("Statement version", str(version)),
        ("Calculation agent", d.org(agent_id)["name"]),
        ("To", d.org(to_id)["name"]),
        ("Statement date", statement_date.isoformat()),
        ("Calculation period", f"{prices[0][0].isoformat()} to {prices[-1][0].isoformat()}"),
        ("Number of daily prices", str(len(prices))),
        ("Fixed price (USD per bbl)", f"{fixed_price:.2f}"),
        ("Average floating price (USD per bbl)", f"{average:.4f}"),
        ("Notional (bbl)", f"{notional:,}"),
        (f"Fixed payer owes ({currency})", fmt(amount)),
    ]
    facts = {
        "trade_id": trade_id,
        "version": version,
        "statement_date": statement_date.isoformat(),
        "fixed_price": num(fixed_price),
        "notional": notional,
        "source": source,
        "prices": [{"date": day.isoformat(), "price": num(p)} for day, p in prices],
        "price_count": len(prices),
        "average": num(average),
        "amount": num(amount),
        "currency": currency,
    }
    att = form(
        filename,
        "swap-statement",
        f"Swap statement {trade_id} v{version}",
        pairs,
        cls("SettlementTerms"),
        facts,
        grid=grid,
        notes=[note] if note else (),
    )
    return att, {"average": average, "amount": amount}


def netting_statement(
    d: Desk,
    *,
    filename: str,
    issuer_id: str,
    to_id: str,
    agreement: str,
    value_date: date,
    items: Sequence[tuple[str, str, Decimal]],
    currency: str,
    reference: str,
) -> tuple[Attachment, Decimal]:
    """A netting statement. ``items`` are (label, 'receivable' or 'payable', amount) from the NewCo side."""
    net = sum((a if side == "receivable" else -a for _, side, a in items), Decimal(0))
    grid = Grid(
        ("Item", "Direction for NewCo", f"Amount ({currency})"),
        tuple((label, side, fmt(a)) for label, side, a in items)
        + (("Net amount", "payable to NewCo" if net >= 0 else "payable by NewCo", fmt(abs(net))),),
    )
    pairs = [
        ("Reference", reference),
        ("Issued by", d.org(issuer_id)["name"]),
        ("To", d.org(to_id)["name"]),
        ("Under", agreement),
        ("Value date", value_date.isoformat()),
    ]
    facts = {
        "reference": reference,
        "agreement": agreement,
        "value_date": value_date.isoformat(),
        "currency": currency,
        "items": [{"item": lb, "direction": side, "amount": num(a)} for lb, side, a in items],
        "net": num(net),
    }
    att = form(
        filename, "netting-statement", f"Netting statement: {reference}", pairs, cls("MonetaryAmount"), facts, grid=grid
    )
    return att, net


def fx_settlement_advice(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    bank_id: str,
    value_date: date,
    buy_currency: str,
    buy_amount: Decimal,
    sell_currency: str,
    rate: Decimal,
    debit_account: str,
    credit_account: str,
) -> tuple[Attachment, Decimal]:
    """A bank advice of both legs of a forward: sell amount = buy amount x rate, rounded to cents."""
    sell_amount = q2(buy_amount * rate)
    pairs = [
        ("Trade id", trade_id),
        ("Advice from", d.org(bank_id)["name"]),
        ("Value date", value_date.isoformat()),
        ("Rate", f"{rate:.4f}"),
        (f"Credited ({buy_currency})", fmt(buy_amount)),
        (f"Debited ({sell_currency})", fmt(sell_amount)),
        *_acct_party(d, debit_account, "Debit leg")[0],
        *_acct_party(d, credit_account, "Credit leg")[0],
    ]
    facts: Mapping[str, Any] = {
        "trade_id": trade_id,
        "bank": d.org(bank_id)["name"],
        "value_date": value_date.isoformat(),
        "rate": num(rate),
        "buy_currency": buy_currency,
        "buy_amount": num(buy_amount),
        "sell_currency": sell_currency,
        "sell_amount": num(sell_amount),
        "debit_account": debit_account,
        "credit_account": credit_account,
    }
    att = form(
        filename, "fx-settlement-advice", f"FX settlement advice: {trade_id}", pairs, cls("SettlementTerms"), facts
    )
    return att, sell_amount


def fx_instruction(
    d: Desk,
    *,
    filename: str,
    trade_id: str,
    party_id: str,
    bank_id: str,
    value_date: date,
    pay_currency: str,
    pay_amount: Decimal,
    receive_currency: str,
    receive_amount: Decimal,
    debit_account: str,
    credit_account: str,
) -> Attachment:
    """A customer instruction to a bank to settle both legs of a forward on the value date."""
    pairs = [
        ("Trade id", trade_id),
        ("Instructed by", d.org(party_id)["name"]),
        ("To", d.org(bank_id)["name"]),
        ("Value date", value_date.isoformat()),
        (f"Pay ({pay_currency})", fmt(pay_amount)),
        (f"Receive ({receive_currency})", fmt(receive_amount)),
        *_acct_party(d, debit_account, "Debit")[0],
        *_acct_party(d, credit_account, "Credit")[0],
    ]
    facts = {
        "trade_id": trade_id,
        "party": d.org(party_id)["name"],
        "bank": d.org(bank_id)["name"],
        "value_date": value_date.isoformat(),
        "pay_currency": pay_currency,
        "pay_amount": num(pay_amount),
        "receive_currency": receive_currency,
        "receive_amount": num(receive_amount),
        "debit_account": debit_account,
        "credit_account": credit_account,
    }
    return form(
        filename,
        "fx-settlement-instruction",
        f"Settlement instruction: {trade_id}",
        pairs,
        cls("SettlementTerms"),
        facts,
    )
