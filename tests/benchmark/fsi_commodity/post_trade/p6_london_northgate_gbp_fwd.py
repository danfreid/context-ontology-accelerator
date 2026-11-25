# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""p6: Northgate Bank confirms NewCo London's GBP forward. The SWIFT-style copy shows NewCo's USD account with two digits swapped."""

# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from .. import ids
from ..ontology import cls
from ..scenario import Scenario
from .common import Desk, account_for, deal_of, fmt, latest_ticket, num, register, trade_start
from .docs import confirmation, swift_copy

SCENARIO_ID = "p6-london-northgate-gbp-fwd"
TRADE_SCENARIO = "t6-london-northgate-gbp-fwd"
EXPECT = {"emails": 6, "attachments": 4, "chats": 0}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    tid, terms = deal["trade_id"], deal["terms"]
    ours, bank = deal["entity"], deal["counterparty"]
    ticket = latest_ticket(tid, u, upstream)
    sc = Scenario(
        SCENARIO_ID,
        "post_trade",
        u,
        trade_start(upstream, deal),
        expect=EXPECT,
        title="Northgate confirms NewCo London's GBP forward: the bank's copy shows the USD account with two digits swapped",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def b(role: str) -> str:
        return f"{bank}:{role}"

    usd_account = account_for(d, ours, terms["sell_currency"], deal["accounts"]["usd"])
    true_number = u["accounts"][usd_account]["number"]
    wrong_number = ids.swap_digits(true_number)
    ticket_facts = ticket["facts"]

    def pair(version: int, shown: str, based_on: str):
        conf = confirmation(
            d,
            filename=f"{tid}-bank-confirmation-v{version}.pdf",
            title=f"Forward confirmation {tid}",
            trade_id=tid,
            issuer_id=bank,
            to_id=ours,
            confirm_date=d.today(),
            terms=ticket_facts,
            based_on=based_on,
            class_uri=cls("DerivativeInstrument"),
            extra=[
                ("Debit account", f"{terms['sell_currency']} {shown}"),
                ("Account held at", d.org(u["accounts"][usd_account]["bank"])["name"]),
            ],
            facts={"debit_account": shown},
        )
        copy = swift_copy(
            d,
            filename=f"{tid}-swift-copy-v{version}.pdf",
            title=f"Message copy {tid}",
            trade_id=tid,
            bank_id=bank,
            party_id=ours,
            ticket=terms | {"value_date": ticket_facts["value_date"]},
            pay_account=usd_account,
            pay_account_shown=shown,
            version=version,
        )
        return conf, copy

    conf1, copy1 = pair(1, wrong_number, ticket["filename"])
    d.mail(
        "bank-confirmation",
        b("trade_finance"),
        n("ops"),
        f"Our confirmation of {tid} and a copy of the message sent through the network are attached: buy GBP {fmt(terms['buy_amount'], 0)} against USD {fmt(terms['sell_amount'], 0)} at {terms['rate']:.4f}, value {ticket_facts['value_date']}.",
        conf1,
        copy1,
        subject=f"Forward confirmation {tid}",
        tags=["confirmation", "swift", "planted"],
        doc=cls("DerivativeInstrument"),
        facts={"trade_id": tid, "debit_account": wrong_number, "value_date": ticket_facts["value_date"]},
    )
    d.mail(
        "ops-check",
        n("ops"),
        n("settlements"),
        f"Bank confirmation for {tid} received. Please check the debit account against our settlement instructions.",
        reply="bank-confirmation",
        days=1,
        tags=["confirmation"],
        facts={"trade_id": tid},
    )
    d.mail(
        "account-query",
        n("settlements"),
        b("trade_finance"),
        f"The debit account on your confirmation of {tid} is {wrong_number}. Our USD account is {true_number}. Please correct it before value date.",
        reply="bank-confirmation",
        days=1,
        tags=["confirmation", "swift", "dispute"],
        facts={"trade_id": tid, "found": wrong_number, "expected": true_number},
    )
    conf2, copy2 = pair(2, true_number, f"{ticket['filename']}, debit account corrected")
    d.mail(
        "bank-corrected",
        b("trade_finance"),
        n("settlements"),
        f"Corrected confirmation and message copy for {tid} attached. The debit account is now {true_number}.",
        conf2,
        copy2,
        reply="account-query",
        days=1,
        tags=["confirmation", "swift", "correction"],
        doc=cls("DerivativeInstrument"),
        facts={"trade_id": tid, "debit_account": true_number},
    )
    d.mail(
        "corrected-ack",
        n("settlements"),
        b("trade_finance"),
        f"Received, thank you. {tid} is correct now.",
        reply="bank-corrected",
        days=1,
        tags=["confirmation"],
    )
    d.mail(
        "booked",
        n("settlements"),
        n("ops"),
        f"{tid} settles on {ticket_facts['value_date']}: pay USD {fmt(terms['sell_amount'], 0)} from account {true_number} against GBP {fmt(terms['buy_amount'], 0)}.",
        subject=f"Settlement instruction {tid}",
        days=1,
        tags=["payment"],
        facts={"trade_id": tid, "usd_payable": num(terms["sell_amount"]), "value_date": ticket_facts["value_date"]},
    )
    register(
        sc,
        trade_id=tid,
        kind="bank-confirmation",
        filename=conf2.filename,
        message="bank-corrected",
        number=f"{tid}-V2",
        issuer=bank,
        payer=ours,
        payee=bank,
        currency=terms["sell_currency"],
        payable=num(terms["sell_amount"]),
        due=ticket_facts["value_date"],
        payer_account=usd_account,
        payee_account=None,
        inputs={"buy_amount": num(terms["buy_amount"]), "rate": num(terms["rate"])},
    )
    d.contradiction(
        "swapped-account-digits",
        "account-vs-ssi",
        f"The bank's first confirmation and message copy show NewCo's USD account as {wrong_number}; the standing instruction is {true_number}. Caught by NewCo settlements.",
        [conf1.filename, copy1.filename, usd_account],
        {"field": "debit_account", "agreed": true_number, "found": wrong_number},
        True,
        "account-query",
    )
    sc.outcome = {
        "result": "corrected",
        "trade_id": tid,
        "usd_payable": num(terms["sell_amount"]),
        "value_date": ticket_facts["value_date"],
        "debit_account": true_number,
    }
    d.question(1, "reconciliation", f"What debit account did the first bank confirmation for {tid} show?", wrong_number)
    d.question(2, "metadata", f"What USD amount settles {tid}?", num(terms["sell_amount"]))
    d.question(3, "hybrid_retrieval", f"What rate is on the confirmation for {tid}?", num(terms["rate"]))
    return sc
