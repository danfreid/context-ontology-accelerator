# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""x6 (control): the GBP forward settles. NewCo London pays USD 5,138,000 and receives GBP 4,000,000. Nothing is planted."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date
from typing import Any

from ..ontology import cls
from ..scenario import Scenario
from .common import Desk, bank_mailbox, business_date, deal_of, dec, fmt, num, pay_start, payment, register, wait_until
from .docs import fx_instruction, fx_settlement_advice

SCENARIO_ID = "x6-london-northgate-gbp-fwd"
TRADE_SCENARIO = "t6-london-northgate-gbp-fwd"
EXPECT = {"emails": 3, "attachments": 2, "chats": 0}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    terms = deal["terms"]
    pay = payment(u, upstream, deal["trade_id"])
    tid, usd, ours, bank_id = pay["trade_id"], pay["amount"], pay["payer"], pay["payee"]
    due = date.fromisoformat(pay["due"])
    gbp, rate = dec(terms["buy_amount"]), dec(terms["rate"])
    credit_account = deal["accounts"]["gbp"]
    sc = Scenario(
        SCENARIO_ID,
        "settlement",
        u,
        pay_start(upstream, tid),
        expect=EXPECT,
        title="GBP forward settles: USD 5,138,000 against GBP 4,000,000 at 1.2845 (control)",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    bank = f"{bank_id}:operations"
    assert bank == bank_mailbox(d, credit_account)
    wait_until(d, n("settlements"), business_date(d, ours, due, -1))
    instruction = fx_instruction(
        d,
        filename=f"{tid}-settlement-instruction.pdf",
        trade_id=tid,
        party_id=ours,
        bank_id=bank_id,
        value_date=due,
        pay_currency=pay["currency"],
        pay_amount=usd,
        receive_currency=terms["buy_currency"],
        receive_amount=gbp,
        debit_account=pay["payer_account"],
        credit_account=credit_account,
    )
    d.mail(
        "settlement-instruction",
        n("settlements"),
        bank,
        f"Please settle {tid} on {due.isoformat()}: we pay USD {fmt(usd)} and receive GBP {fmt(gbp)}. Instruction attached.",
        instruction,
        subject=f"Settlement instruction {tid}",
        tags=["payment", "fx-settlement"],
        doc=cls("SettlementTerms"),
        facts={"trade_id": tid, "pay_amount": num(usd), "receive_amount": num(gbp), "value_date": due.isoformat()},
    )
    wait_until(d, bank, due)
    advice, sell = fx_settlement_advice(
        d,
        filename=f"{tid}-settlement-advice.pdf",
        trade_id=tid,
        bank_id=bank_id,
        value_date=due,
        buy_currency=terms["buy_currency"],
        buy_amount=gbp,
        sell_currency=pay["currency"],
        rate=rate,
        debit_account=pay["payer_account"],
        credit_account=credit_account,
    )
    if sell != usd:
        raise ValueError(f"{tid}: {gbp} at {rate} is {sell}, but the bank confirmation fixes {usd}")
    d.mail(
        "settlement-advice",
        bank,
        n("settlements"),
        f"{tid} settled on {due.isoformat()}: we debited USD {fmt(sell)} and credited GBP {fmt(gbp)} at {rate:.4f}.",
        advice,
        reply="settlement-instruction",
        subject=f"Settlement advice {tid}",
        tags=["payment", "fx-settlement"],
        doc=cls("SettlementTerms"),
        facts={
            "trade_id": tid,
            "debit": num(sell),
            "credit": num(gbp),
            "rate": num(rate),
            "value_date": due.isoformat(),
        },
    )
    d.mail(
        "reconciled",
        n("settlements"),
        n("accounting"),
        f"{tid} reconciled: USD {fmt(sell)} out and GBP {fmt(gbp)} in match the confirmation at {rate:.4f}. No differences.",
        reply="settlement-advice",
        days=0,
        tags=["reconciliation"],
        facts={"trade_id": tid, "debit": num(sell), "credit": num(gbp), "difference": 0},
    )
    register(sc, trade_id=tid, amount=num(sell), currency=pay["currency"], value_date=due.isoformat(), gbp=num(gbp))
    sc.outcome = {
        "result": "settled",
        "trade_id": tid,
        "usd_paid": num(sell),
        "gbp_received": num(gbp),
        "rate": num(rate),
        "value_date": due.isoformat(),
    }
    d.question(1, "reconciliation", f"How much USD did NewCo London pay to settle {tid}?", num(sell))
    d.question(2, "reconciliation", f"What rate did the settlement of {tid} use?", num(rate))
    d.question(3, "metadata", f"Which account received the GBP for {tid}?", credit_account)
    return sc
