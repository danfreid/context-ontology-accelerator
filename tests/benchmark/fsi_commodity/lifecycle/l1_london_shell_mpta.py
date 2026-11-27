# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""l1: NewCo London and Shell negotiate, approve, execute and amend a master agreement, then trade under it.

What makes it hard on purpose:

* one file name, ``MPTA_NewCo_Shell.docx``, carries three different revisions; two revisions also travel as
  identical copies in a forward and a "clean from our side" return;
* terms move in tracked changes, in Word comments and in email bodies only; replies quote superseded terms;
  internal forwards carry credit and business instructions that are never offers; one email is sent twice;
* legal, credit and business approve separately; the executed copy is a scan without a text layer;
* an amendment is signed before it takes effect, so "as of" depends on the effective date, not the signing;
* two trades, an invoice each way netted to one payment, late interest, a margin call and the renewal notice
  deadline must each use the term in force on the right date.
"""

# ruff: noqa: E501
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..clauses import CLAUSES, Round, redline_rounds, render
from ..ontology import cls
from ..post_trade.common import Desk, account_for, business_date, due_date, fmt, form, num, wait_until
from ..post_trade.docs import invoice, priced
from ..scenario import Attachment, Scenario
from ..writers.docx_writer import Comment, Para, build_docx, plain
from ..writers.manifest import sha256_hex
from ..writers.pdf_tools import scan
from ..writers.pdf_writer import Heading, KeyValues, Text, build_pdf
from .common import (
    Timeline,
    late_interest,
    margin_call,
    net_settlement,
    renewal_notice_deadline,
    terms_as_of,
    value_as_of,
)

SCENARIO_ID = "l1-london-shell-mpta"
EXPECT = {"emails": 26, "attachments": 17, "chats": 0}
OURS, SHELL = "newco-lon", "cp-shell"
FILE = "MPTA_NewCo_Shell.docx"
TITLE = "Master Physical Trading Agreement between NewCo Trading London Limited and Shell Trading International Ltd"
BASE: dict[str, dict[str, Any]] = {
    "payment": {"days": 30, "currency": "USD"},
    "late_interest": {"rate": 4},
    "quantity_tolerance": {"tolerance": 5},
    "netting": {"scope": "a single net amount across all transactions"},
    "threshold": {"amount": 5_000_000},
    "min_transfer": {"amount": 250_000},
    "term": {"months": 12, "notice": 60},
    "governing_law": {"law": "England and Wales", "venue": "London"},
}
SEPARATE = "separate net amounts for each product group"
EXPOSURE = Decimal("9812400")  # NewCo's mark-to-market exposure to Shell on the valuation date
DAYS_LATE = 4
CREDIT_LIMIT = 40_000_000
TRADES = (
    # trade id, NewCo side, product, B/L quantity (mt), price (USD/mt)
    ("NC-LON-26-0201", "sell", "Gasoil 0.1%", Decimal("25118.412"), Decimal("712.400")),
    ("NC-LON-26-0202", "buy", "Fuel oil 3.5%", Decimal("17960.335"), Decimal("455.100")),
)


def _clean_paras(title: str, values: dict[str, dict[str, Any]]) -> list[Para]:
    paras = [plain(title, heading=True)]
    for cid, params in values.items():
        paras += [plain(CLAUSES[cid][0], heading=True), plain(render(cid, params))]
    return paras


def _with_comments(paras: list[Para], comments: dict[str, tuple[Comment, ...]]) -> list[Para]:
    """Anchor comments to the body paragraph of each named clause (title, then heading/body per clause)."""
    out = list(paras)
    for i, cid in enumerate(BASE):
        if cid in comments:
            out[2 + 2 * i] = replace(out[2 + 2 * i], comments=comments[cid])
    return out


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    start = max(s.clock for s in upstream.values()) + timedelta(days=1)
    sc = Scenario(
        SCENARIO_ID,
        "lifecycle",
        u,
        start,
        expect=EXPECT,
        title="London and Shell: negotiate, approve, execute and amend an MPTA, then trade, net, call margin and diary the renewal",
    )
    d = Desk(sc)
    tl = Timeline()

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def s(role: str) -> str:
        return f"{SHELL}:{role}"

    def at() -> datetime:
        return sc.clock

    def msg(key: str) -> dict[str, Any]:
        return next(m for m in sc.messages if m["key"] == key)

    def quoted(key: str) -> str:
        m = msg(key)
        return f"On {m['timestamp'][:10]}, {m['from']['name']} wrote:\n" + "\n".join(
            f"> {ln}" for ln in sc.body(key).splitlines()
        )

    def forwarded(key: str) -> str:
        m = msg(key)
        return f"---------- Forwarded message ---------\nFrom: {m['from']['name']} <{m['from']['address']}>\nDate: {m['timestamp']}\nSubject: {m['subject']}\n\n{sc.body(key)}"

    def docx(paras: list[Para], author_ref: str, facts: dict[str, Any]) -> Attachment:
        content = build_docx(TITLE, paras, author=d.person(author_ref)["name"], created=at())
        return Attachment(FILE, content, "agreement-draft", cls("Contract", "fibo"), facts)

    revisions: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    calculations: list[dict[str, Any]] = []

    def revision(att: Attachment, key: str) -> None:
        sha = sha256_hex(att.content)
        for r in revisions:
            if r["sha256"] == sha:
                r["messages"].append(key)
                return
        revisions.append({"filename": att.filename, "revision": len(revisions) + 1, "sha256": sha, "messages": [key]})

    # --- 1. NewCo's draft --------------------------------------------------------------------------
    rev1 = docx(
        _clean_paras(TITLE, BASE), n("lawyer"), {"revision": 1, "values": BASE, "tracked_changes": 0, "comments": 0}
    )
    d.mail(
        "draft",
        n("lawyer"),
        s("lawyer"),
        "Our draft master agreement is attached for your review.",
        rev1,
        subject="NewCo / Shell: master physical trading agreement",
        cc=[n("credit_analyst")],
        tags=["legal", "mpta", "draft"],
        doc=cls("Contract", "fibo"),
    )
    revision(rev1, "draft")
    for cid, params in BASE.items():
        for p, v in params.items():
            tl.add(
                cid, p, v, "proposed", OURS, at(), "draft", "draft", attachment=FILE, sha256=sha256_hex(rev1.content)
            )

    # --- 2. Shell's redline, with comments, same file name -----------------------------------------
    sc.wait_business_days(d.addr(s("lawyer")), 3)
    shell_name = d.person(s("lawyer"))["name"]
    r1_changes = {"payment": {"days": 20}, "threshold": {"amount": 10_000_000}, "term": {"notice": 30}}
    rounds = [Round(shell_name, at(), r1_changes)]
    result = redline_rounds(TITLE, BASE, rounds)
    c_net = Comment(
        shell_name, at(), f"Shell needs {SEPARATE}; a single net amount does not work for our product books."
    )
    c_law = Comment(shell_name, at(), "English law and London courts agreed.")
    paras = _with_comments(result.rounds[-1], {"netting": (c_net,), "governing_law": (c_law,)})
    rev2 = docx(paras, s("lawyer"), {"revision": 2, "changes": r1_changes, "tracked_changes": 6, "comments": 2})
    d.mail(
        "shell-redline",
        s("lawyer"),
        n("lawyer"),
        "Our mark-up is attached, with comments on netting and governing law. Shorter payment terms, a higher threshold and a shorter notice period.",
        rev2,
        reply="draft",
        cc=[s("credit_analyst")],
        tags=["legal", "mpta", "redline", "comments"],
        doc=cls("Contract", "fibo"),
    )
    revision(rev2, "shell-redline")
    sha2 = sha256_hex(rev2.content)
    for cid, params in r1_changes.items():
        for p, v in params.items():
            tl.add(cid, p, v, "countered", SHELL, at(), "shell-redline", "redline", attachment=FILE, sha256=sha2)
    tl.add(
        "netting", "scope", SEPARATE, "countered", SHELL, at(), "shell-redline", "comment", attachment=FILE, sha256=sha2
    )
    for p, v in BASE["governing_law"].items():
        tl.add("governing_law", p, v, "accepted", SHELL, at(), "shell-redline", "comment", attachment=FILE, sha256=sha2)
    comments += [
        {
            "clause": "netting",
            "author": shell_name,
            "text": c_net.text,
            "message": "shell-redline",
            "revision": 2,
            "replies_to": None,
        },
        {
            "clause": "governing_law",
            "author": shell_name,
            "text": c_law.text,
            "message": "shell-redline",
            "revision": 2,
            "replies_to": None,
        },
    ]

    # --- 3-4. Internal forward to credit; credit's instruction ---------------------------------------
    d.mail(
        "fwd-credit",
        n("lawyer"),
        n("credit_analyst"),
        "Shell wants a USD 10m threshold and 20-day payment. Can credit live with that?",
        rev2,
        fwd="shell-redline",
        quote=forwarded("shell-redline"),
        days=0,
        tags=["legal", "credit", "internal", "forward"],
    )
    revision(rev2, "fwd-credit")
    d.mail(
        "credit-instruction",
        n("credit_analyst"),
        n("lawyer"),
        "Not USD 10m. We can accept a threshold of USD 7,500,000 if the USD 250,000 minimum transfer stays. 20 days is fine.",
        reply="fwd-credit",
        quote=quoted("fwd-credit"),
        days=1,
        tags=["credit", "internal"],
    )
    tl.add("threshold", "amount", 7_500_000, "internal", OURS, at(), "credit-instruction", "internal")
    tl.add("payment", "days", 20, "internal", OURS, at(), "credit-instruction", "internal")

    # --- 5-7. Terms agreed in email bodies; quoted history; a duplicate resend ------------------------
    d.mail(
        "newco-counter",
        n("lawyer"),
        s("lawyer"),
        "We accept payment in 20 days. On the threshold we can offer USD 7,500,000, with the minimum transfer unchanged at USD 250,000. "
        "Netting stays a single net amount across all transactions; our netting opinion depends on it. On notice we propose 45 days.",
        reply="shell-redline",
        quote=quoted("shell-redline"),
        cc=[n("credit_analyst")],
        days=1,
        tags=["legal", "mpta", "email-terms"],
    )
    tl.add("payment", "days", 20, "agreed", OURS, at(), "newco-counter", "email-body")
    tl.add("threshold", "amount", 7_500_000, "countered", OURS, at(), "newco-counter", "email-body")
    tl.add("netting", "scope", BASE["netting"]["scope"], "countered", OURS, at(), "newco-counter", "email-body")
    tl.add("term", "notice", 45, "countered", OURS, at(), "newco-counter", "email-body")

    shell_reply = (
        "Agreed: a threshold of USD 7,500,000 and 45 days' notice. We can accept a single net amount. "
        "One more point: late payment interest at 3%, not 4%."
    )
    d.mail(
        "shell-agrees",
        s("lawyer"),
        [n("lawyer"), n("credit_analyst")],
        shell_reply,
        reply="newco-counter",
        quote=quoted("newco-counter"),
        cc=[s("credit_analyst")],
        days=1,
        tags=["legal", "mpta", "email-terms"],
    )
    for cid, p, v in (
        ("threshold", "amount", 7_500_000),
        ("term", "notice", 45),
        ("netting", "scope", BASE["netting"]["scope"]),
    ):
        tl.add(cid, p, v, "agreed", SHELL, at(), "shell-agrees", "email-body")
    tl.add("late_interest", "rate", 3, "proposed", SHELL, at(), "shell-agrees", "email-body")
    d.mail(
        "shell-agrees-resent",
        s("lawyer"),
        [n("lawyer"), n("credit_analyst")],
        shell_reply,
        reply="newco-counter",
        quote=quoted("newco-counter"),
        cc=[s("credit_analyst")],
        tags=["legal", "mpta", "duplicate"],
    )

    d.mail(
        "fwd-business",
        n("lawyer"),
        n("trader"),
        "Shell asks for 3% late interest instead of 4%. Commercially acceptable?",
        fwd="shell-agrees",
        quote=forwarded("shell-agrees"),
        tags=["legal", "business", "internal", "forward"],
    )
    d.mail(
        "business-ok",
        n("trader"),
        n("lawyer"),
        "3% is fine commercially.",
        reply="fwd-business",
        days=1,
        tags=["business", "internal"],
    )
    tl.add("late_interest", "rate", 3, "internal", OURS, at(), "business-ok", "internal")

    # --- 8-9. Revised draft with everything agreed; Shell returns the identical file -----------------------
    our_name = d.person(n("lawyer"))["name"]
    r2_changes = {"threshold": {"amount": 7_500_000}, "term": {"notice": 45}, "late_interest": {"rate": 3}}
    rounds.append(Round(our_name, at(), r2_changes))
    result = redline_rounds(TITLE, BASE, rounds)
    c_reply = Comment(
        our_name, at(), f"Single net amount kept, as agreed in your email of {msg('shell-agrees')['timestamp'][:10]}."
    )
    rev3 = docx(
        _with_comments(result.rounds[-1], {"netting": (c_reply,)}),
        n("lawyer"),
        {"revision": 3, "changes": r2_changes, "values_after": result.final_values, "comments": 1},
    )
    d.mail(
        "newco-revised",
        n("lawyer"),
        s("lawyer"),
        "The revised draft attached reflects everything agreed by email. Please confirm it is clean.",
        rev3,
        reply="shell-agrees",
        cc=[n("credit_analyst")],
        days=1,
        tags=["legal", "mpta", "redline", "comments"],
        doc=cls("Contract", "fibo"),
    )
    revision(rev3, "newco-revised")
    sha3 = sha256_hex(rev3.content)
    for cid, params in r2_changes.items():
        for p, v in params.items():
            tl.add(cid, p, v, "agreed", OURS, at(), "newco-revised", "redline", attachment=FILE, sha256=sha3)
    comments.append(
        {
            "clause": "netting",
            "author": our_name,
            "text": c_reply.text,
            "message": "newco-revised",
            "revision": 3,
            "replies_to": 0,
        }
    )
    d.mail(
        "shell-clean",
        s("lawyer"),
        n("lawyer"),
        "Clean from our side. The same file is attached back for your records.",
        rev3,
        reply="newco-revised",
        days=1,
        tags=["legal", "mpta"],
    )
    revision(rev3, "shell-clean")
    final = result.final_values

    # --- 10-12. Approvals ----------------------------------------------------------------------------------
    rows = [(CLAUSES[c][0], render(c, final[c])) for c in final]
    legal = form(
        "mpta-legal-review.pdf",
        "legal-review",
        "Legal review: NewCo / Shell MPTA",
        rows,
        cls("BibliographicResource"),
        {"recommendation": "execute", "final_terms": final},
        intro="Final negotiated terms; legal recommends execution.",
    )
    d.mail(
        "legal-approval",
        n("lawyer"),
        [n("credit_analyst"), n("trader")],
        "Legal review is complete and attached. I recommend execution.",
        legal,
        subject="Approval: NewCo / Shell MPTA",
        days=1,
        tags=["approval", "legal", "internal"],
    )
    approvals.append(
        {
            "stage": "legal",
            "by": d.addr(n("lawyer")),
            "message": "legal-approval",
            "at": at().isoformat(),
            "decision": "approved",
        }
    )
    credit = form(
        "mpta-credit-approval.pdf",
        "credit-approval",
        "Credit approval: Shell Trading International Ltd",
        [
            ("Counterparty", d.org(SHELL)["name"]),
            ("Credit limit (USD)", fmt(CREDIT_LIMIT, 0)),
            ("Threshold (USD)", fmt(final["threshold"]["amount"], 0)),
            ("Minimum transfer (USD)", fmt(final["min_transfer"]["amount"], 0)),
            ("Decision", "Approved"),
        ],
        cls("LoanOrCreditAccount"),
        {"credit_limit": CREDIT_LIMIT, "threshold": final["threshold"]["amount"], "decision": "approved"},
    )
    d.mail(
        "credit-approval",
        n("credit_analyst"),
        n("lawyer"),
        f"Credit approves a USD {fmt(CREDIT_LIMIT, 0)} limit on the agreed terms.",
        credit,
        reply="legal-approval",
        cc=[n("trader")],
        tags=["approval", "credit", "internal"],
    )
    approvals.append(
        {
            "stage": "credit",
            "by": d.addr(n("credit_analyst")),
            "message": "credit-approval",
            "at": at().isoformat(),
            "decision": "approved",
        }
    )
    d.mail(
        "business-approval",
        n("trader"),
        n("lawyer"),
        "Business approves. Go ahead and sign.",
        reply="legal-approval",
        tags=["approval", "business", "internal"],
    )
    approvals.append(
        {
            "stage": "business",
            "by": d.addr(n("trader")),
            "message": "business-approval",
            "at": at().isoformat(),
            "decision": "approved",
        }
    )

    # --- 13-15. Execution: our signed PDF, Shell's countersigned scan, the systems notice ---------------------
    def agreement_pdf(signed_by: list[str], when: date) -> bytes:
        blocks: list[Any] = []
        for cid, params in final.items():
            blocks += [Heading(CLAUSES[cid][0]), Text(render(cid, params))]
        blocks.append(KeyValues(tuple((d.org(o)["name"], f"Signed {when.isoformat()}") for o in signed_by)))
        return build_pdf(TITLE, blocks)

    ours_signed = d.today() + timedelta(days=1)
    exec_ours = Attachment(
        "MPTA_NewCo_Shell_signed_NewCo.pdf",
        agreement_pdf([OURS], ours_signed),
        "agreement-execution",
        cls("Contract", "fibo"),
        {"signed_by": [d.org(OURS)["name"]], "final_terms": final},
    )
    d.mail(
        "execution-newco",
        n("lawyer"),
        s("lawyer"),
        "Our signed execution copy is attached. Please countersign.",
        exec_ours,
        reply="shell-clean",
        days=1,
        tags=["legal", "execution"],
        doc=cls("Contract", "fibo"),
    )
    sc.wait_business_days(d.addr(s("lawyer")), 1)
    effective = d.today()
    executed = scan(agreement_pdf([OURS, SHELL], effective), 4242)
    exec_att = Attachment(
        "MPTA_NewCo_Shell_executed.pdf",
        executed,
        "agreement-executed",
        cls("Contract", "fibo"),
        {
            "signed_by": [d.org(OURS)["name"], d.org(SHELL)["name"]],
            "effective": effective.isoformat(),
            "format": "pdf-scan",
        },
    )
    d.mail(
        "executed",
        s("lawyer"),
        n("lawyer"),
        "Fully executed agreement attached (scan).",
        exec_att,
        reply="execution-newco",
        tags=["legal", "execution", "scan"],
        doc=cls("Contract", "fibo"),
    )
    for cid, params in final.items():
        for p, v in params.items():
            tl.add(
                cid,
                p,
                v,
                "executed",
                OURS,
                at(),
                "executed",
                "executed",
                attachment=exec_att.filename,
                sha256=sha256_hex(executed),
                effective_from=effective,
            )
    summary = "; ".join(f"{CLAUSES[c][0]}: " + ", ".join(f"{k} {v}" for k, v in final[c].items()) for c in final)
    d.mail(
        "systems-notice",
        n("lawyer"),
        [n("ops"), n("settlements"), n("credit_analyst")],
        f"The Shell MPTA is executed, effective {effective.isoformat()}. Terms for systems: {summary}.",
        subject="Executed: NewCo / Shell MPTA",
        cc=[n("trader")],
        tags=["legal", "execution", "internal"],
    )

    # --- 16-17. Amendment: signed now, effective the first of next month -------------------------------------------
    sc.wait_business_days(d.addr(s("lawyer")), 5)
    d.mail(
        "amendment-request",
        s("lawyer"),
        n("lawyer"),
        "Our treasury has asked to cut late payment interest to 2.5% from the first of next month. Can you agree an amendment?",
        subject="NewCo / Shell MPTA: late payment interest",
        tags=["legal", "amendment", "email-terms"],
    )
    tl.add("late_interest", "rate", 2.5, "proposed", SHELL, at(), "amendment-request", "email-body")
    signed_on = d.today()
    amend_from = (signed_on.replace(day=1) + timedelta(days=32)).replace(day=1)
    amend = form(
        "MPTA_NewCo_Shell_amendment_1.pdf",
        "agreement-amendment",
        "Amendment No. 1 to the NewCo / Shell MPTA",
        [
            ("Clause", CLAUSES["late_interest"][0]),
            ("New text", render("late_interest", {"rate": "2.5"})),
            ("Effective from", amend_from.isoformat()),
            ("Signed", f"{d.org(OURS)['name']} and {d.org(SHELL)['name']}, {signed_on.isoformat()}"),
        ],
        cls("Contract", "fibo"),
        {
            "clause": "late_interest",
            "rate": 2.5,
            "effective_from": amend_from.isoformat(),
            "signed": signed_on.isoformat(),
        },
    )
    d.mail(
        "amendment-signed",
        n("lawyer"),
        s("lawyer"),
        f"Agreed. Amendment No. 1, signed by both of us, applies from {amend_from.isoformat()}.",
        amend,
        reply="amendment-request",
        tags=["legal", "amendment"],
        doc=cls("Contract", "fibo"),
    )
    tl.add(
        "late_interest",
        "rate",
        2.5,
        "amended",
        OURS,
        at(),
        "amendment-signed",
        "amendment",
        attachment=amend.filename,
        sha256=sha256_hex(amend.content),
        effective_from=amend_from,
    )
    amendment_signed_at = at()

    # --- 18-24. Trading under the agreement ----------------------------------------------------------------------------------
    wait_until(d, n("ops"), business_date(d, OURS, amend_from, 2))
    bl_date = d.today()
    confirms = [
        form(
            f"{tid}-confirmation.pdf",
            "trade-confirmation",
            f"Trade confirmation {tid}",
            [
                ("Trade id", tid),
                ("Seller", d.org(OURS if side == "sell" else SHELL)["name"]),
                ("Buyer", d.org(SHELL if side == "sell" else OURS)["name"]),
                ("Product", product),
                ("Quantity", f"{fmt(qty, 3)} mt (B/L)"),
                ("Price", f"USD {price:.3f} per mt"),
                ("B/L date", bl_date.isoformat()),
                ("Agreement", "NewCo / Shell MPTA"),
            ],
            cls("TradeTransaction"),
            {"trade_id": tid, "side": side, "quantity": num(qty), "price": num(price), "bl_date": bl_date.isoformat()},
        )
        for tid, side, product, qty, price in TRADES
    ]
    d.mail(
        "confirmations",
        n("ops"),
        s("ops"),
        "Confirmations for the two cargoes under our MPTA are attached.",
        *confirms,
        subject="Confirmations NC-LON-26-0201 and -0202",
        tags=["trade", "confirmation"],
        doc=cls("TradeTransaction"),
    )
    when_terms = at()
    pay_days = value_as_of(tl.events, "payment", "days", when_terms)
    due = due_date(d, OURS, bl_date, pay_days)
    (t1, _, p1, q1, pr1), (t2, _, p2, q2_, pr2) = TRADES
    inv1, a1 = invoice(
        d,
        number=f"NCL-INV-{t1}",
        filename=f"{t1}-invoice.pdf",
        title=f"Invoice NCL-INV-{t1}",
        trade_id=t1,
        issuer_id=OURS,
        bill_to_id=SHELL,
        issue_date=d.today(),
        due=due,
        lines=[priced(p1, q1, "mt", pr1)],
        currency="USD",
        pay_to=account_for(d, OURS, "USD"),
        extra=[("Payment terms", f"{pay_days} days after B/L date (MPTA, payment clause)")],
    )
    d.mail(
        "invoice-ours",
        n("settlements"),
        s("settlements"),
        f"Invoice NCL-INV-{t1} for USD {fmt(a1)}, due {due.isoformat()}.",
        inv1,
        subject=f"Invoice NCL-INV-{t1}",
        days=1,
        tags=["invoice"],
        doc=cls("Invoice"),
    )
    inv2, a2 = invoice(
        d,
        number=f"STI-INV-{t2}",
        filename=f"{t2}-invoice.pdf",
        title=f"Invoice STI-INV-{t2}",
        trade_id=t2,
        issuer_id=SHELL,
        bill_to_id=OURS,
        issue_date=d.today(),
        due=due,
        lines=[priced(p2, q2_, "mt", pr2)],
        currency="USD",
        pay_to=account_for(d, SHELL, "USD"),
        extra=[("Payment terms", f"{pay_days} days after B/L date")],
    )
    d.mail(
        "invoice-theirs",
        s("settlements"),
        n("settlements"),
        f"Our invoice STI-INV-{t2} for USD {fmt(a2)}, due {due.isoformat()}.",
        inv2,
        subject=f"Invoice STI-INV-{t2}",
        tags=["invoice"],
        doc=cls("Invoice"),
    )
    net, payer = net_settlement(a1, a2)
    scope = value_as_of(tl.events, "netting", "scope", at())
    netting = form(
        f"netting-{due.isoformat()}.pdf",
        "netting-statement",
        f"Netting statement for {due.isoformat()}",
        [
            ("Agreement", "NewCo / Shell MPTA, netting clause"),
            ("Netting basis", scope),
            (f"NCL-INV-{t1} (Shell pays)", fmt(a1)),
            (f"STI-INV-{t2} (NewCo pays)", fmt(a2)),
            ("Net amount (USD)", fmt(net)),
            ("Payable by", d.org(SHELL if payer == "them" else OURS)["name"]),
            ("Value date", due.isoformat()),
        ],
        cls("MonetaryAmount"),
        {
            "receivable": num(a1),
            "payable": num(a2),
            "net": num(net),
            "payer": SHELL if payer == "them" else OURS,
            "value_date": due.isoformat(),
        },
    )
    d.mail(
        "netting",
        n("settlements"),
        s("settlements"),
        f"Both invoices fall due on {due.isoformat()}. Under the MPTA they net to USD {fmt(net)}, payable by Shell.",
        netting,
        reply="invoice-theirs",
        days=1,
        tags=["netting"],
        doc=cls("MonetaryAmount"),
    )
    calculations.append(
        {
            "id": "netting",
            "as_of": at().isoformat(),
            "terms": {"netting.scope": scope, "payment.days": pay_days},
            "inputs": {
                "receivable": num(a1),
                "payable": num(a2),
                "bl_date": bl_date.isoformat(),
                "due": due.isoformat(),
            },
            "result": {"net": num(net), "payer": SHELL if payer == "them" else OURS},
        }
    )

    wait_until(d, n("settlements"), due + timedelta(days=DAYS_LATE))
    rate = value_as_of(
        tl.events, "late_interest", "rate", datetime.combine(due, datetime.min.time(), tzinfo=at().tzinfo)
    )
    interest = late_interest(net, Decimal(str(rate)), DAYS_LATE)
    li, li_total = invoice(
        d,
        number=f"NCL-INT-{due.isoformat()}",
        filename="late-interest-invoice.pdf",
        title="Late payment interest",
        trade_id=t1,
        issuer_id=OURS,
        bill_to_id=SHELL,
        issue_date=d.today(),
        due=d.today() + timedelta(days=7),
        lines=[
            priced(f"Interest on USD {fmt(net)} at {rate}% a year for {DAYS_LATE} days (actual/360)", 1, "", interest)
        ],
        currency="USD",
        pay_to=account_for(d, OURS, "USD"),
    )
    d.mail(
        "late-interest",
        n("settlements"),
        s("settlements"),
        f"The net USD {fmt(net)} due {due.isoformat()} arrived {DAYS_LATE} days late. Interest at {rate}% (Amendment No. 1) is USD {fmt(li_total)}; invoice attached.",
        li,
        reply="netting",
        tags=["invoice", "late-interest"],
        doc=cls("Invoice"),
    )
    calculations.append(
        {
            "id": "late-interest",
            "as_of": due.isoformat(),
            "terms": {"late_interest.rate": rate},
            "inputs": {"amount": num(net), "days_late": DAYS_LATE, "basis": "actual/360"},
            "result": {"interest": num(interest)},
        }
    )

    threshold = value_as_of(tl.events, "threshold", "amount", at())
    mta = value_as_of(tl.events, "min_transfer", "amount", at())
    call = margin_call(EXPOSURE, Decimal(threshold), Decimal(mta), Decimal(1))
    mc = form(
        f"margin-call-{d.today().isoformat()}.pdf",
        "margin-call",
        "Margin call: Shell Trading International Ltd",
        [
            ("Valuation date", d.today().isoformat()),
            ("Exposure (USD)", fmt(EXPOSURE)),
            ("Threshold (USD)", fmt(threshold, 0)),
            ("Minimum transfer (USD)", fmt(mta, 0)),
            ("Amount called (USD)", fmt(call)),
        ],
        cls("CLM"),
        {"exposure": num(EXPOSURE), "threshold": threshold, "min_transfer": mta, "call": num(call)},
    )
    d.mail(
        "margin-call",
        n("credit_analyst"),
        s("credit_analyst"),
        f"Exposure is USD {fmt(EXPOSURE)} against a USD {fmt(threshold, 0)} threshold. Please transfer USD {fmt(call)} today.",
        mc,
        subject="Margin call: NewCo / Shell",
        days=1,
        tags=["margin-call", "credit"],
        doc=cls("CLM"),
    )
    calculations.append(
        {
            "id": "margin-call",
            "as_of": at().isoformat(),
            "terms": {"threshold.amount": threshold, "min_transfer.amount": mta},
            "inputs": {"exposure": num(EXPOSURE)},
            "result": {"call": num(call)},
        }
    )

    months = value_as_of(tl.events, "term", "months", at())
    notice = value_as_of(tl.events, "term", "notice", at())
    term_end, deadline = renewal_notice_deadline(effective, months, notice)
    d.mail(
        "renewal-diary",
        n("lawyer"),
        [n("trader"), n("credit_analyst")],
        f"Diary note: the Shell MPTA's first term ends {term_end.isoformat()}. Notice of non-renewal must be given by {deadline.isoformat()} ({notice} days).",
        subject="Diary: Shell MPTA renewal",
        days=1,
        tags=["legal", "renewal", "internal"],
    )
    calculations.append(
        {
            "id": "renewal-notice",
            "as_of": at().isoformat(),
            "terms": {"term.months": months, "term.notice": notice},
            "inputs": {"effective": effective.isoformat()},
            "result": {"term_end": term_end.isoformat(), "notice_deadline": deadline.isoformat()},
        }
    )

    # --- ground truth -------------------------------------------------------------------------------------
    dup_ids = [msg("shell-agrees")["message_id"], msg("shell-agrees-resent")["message_id"]]
    sc.lifecycle = {
        "agreement": {
            "title": TITLE,
            "parties": [OURS, SHELL],
            "effective": effective.isoformat(),
            "executed_message": "executed",
            "final_terms": final,
            "amendments": [
                {
                    "clause": "late_interest",
                    "param": "rate",
                    "value": 2.5,
                    "signed": signed_on.isoformat(),
                    "effective_from": amend_from.isoformat(),
                    "message": "amendment-signed",
                }
            ],
        },
        "events": tl.events,
        "comments": comments,
        "revisions": revisions,
        "duplicate_messages": [["shell-agrees", "shell-agrees-resent"]],
        "forwards": [{"message": m["key"], "forward_of": m["forward_of"]} for m in sc.messages if "forward_of" in m],
        "quoted_history": [
            {"message": k, "quotes": q}
            for k, q in (
                ("credit-instruction", "fwd-credit"),
                ("newco-counter", "shell-redline"),
                ("shell-agrees", "newco-counter"),
                ("shell-agrees-resent", "newco-counter"),
            )
        ],
        "approvals": approvals,
        "calculations": calculations,
    }

    def ts(key: str) -> datetime:
        return datetime.fromisoformat(msg(key)["timestamp"])

    d.question(
        1,
        "reconciliation",
        "As of NewCo legal's forward to credit, what payment term (days) was on the table for the Shell MPTA?",
        value_as_of(tl.events, "payment", "days", ts("fwd-credit"), "on_table"),
    )
    d.question(
        2,
        "hybrid_retrieval",
        "What threshold did NewCo's credit team say it could accept for the Shell MPTA?",
        7_500_000,
    )
    d.question(
        3,
        "graph_traversal",
        "Which message first agreed the USD 7,500,000 threshold with Shell?",
        msg("shell-agrees")["message_id"],
    )
    d.question(
        4,
        "reconciliation",
        "What late payment interest rate did the executed Shell MPTA set?",
        value_as_of(tl.events, "late_interest", "rate", ts("executed")),
    )
    d.question(
        5,
        "reconciliation",
        "What late payment interest rate applied on the day Amendment No. 1 was signed?",
        value_as_of(tl.events, "late_interest", "rate", amendment_signed_at),
    )
    d.question(
        6, "reconciliation", f"What late payment interest rate applied to the net payment due {due.isoformat()}?", rate
    )
    d.question(7, "metadata", f"How many different revisions of {FILE} are in the mailbox?", len(revisions))
    d.question(8, "metadata", f"How many messages carry revision 3 of {FILE}?", len(revisions[2]["messages"]))
    d.question(9, "hybrid_retrieval", "What did Shell's comment on the netting clause ask for?", SEPARATE)
    d.question(
        10,
        "reconciliation",
        f"What net amount is payable on {due.isoformat()} under the Shell MPTA, and by whom?",
        [num(net), d.org(SHELL)["name"]],
    )
    d.question(11, "reconciliation", "What late payment interest is due on the late net payment?", num(li_total))
    d.question(12, "reconciliation", "What amount does the margin call to Shell request?", num(call))
    d.question(
        13,
        "reconciliation",
        "By what date must notice of non-renewal of the Shell MPTA be given?",
        deadline.isoformat(),
    )
    d.question(14, "metadata", "Which email in the Shell negotiation is a duplicate of another?", dup_ids[1:])
    d.question(
        15,
        "metadata",
        "Which approvals were given before the Shell MPTA was executed?",
        [a["stage"] for a in approvals],
    )
    assert terms_as_of(tl.events, at())  # the timeline answers at the end of the scenario
    return sc
