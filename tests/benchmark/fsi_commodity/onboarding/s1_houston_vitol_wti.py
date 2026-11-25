# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""s1: NewCo Houston onboards Vitol. Clean KYC, USD 40m of 60m requested, MPTA in four redline rounds. Control case."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..ontology import cls
from ..scenario import Scenario
from ..universe import beneficial_owners
from .common import (
    Desk,
    Negotiation,
    bureau_score,
    cert_incorporation,
    contract_title,
    credit_block,
    credit_memo,
    financial_data,
    financial_statements,
    form,
    good_standing,
    lei_record,
    limit_letter,
    money,
    onboarded_record,
    ownership_chart,
    questionnaire,
    screening_report,
    signatory_list,
    ssi_letter,
    tax_form,
)

SCENARIO_ID = "s1-houston-vitol-wti"
EXPECT = {"emails": 26, "attachments": 17, "chats": 0}
OURS, THEIRS = "newco-hou", "cp-vitol"
REQUESTED, UNSECURED = 60_000_000, 40_000_000

BASE = {
    "payment": {"days": 30, "currency": "USD"},
    "quantity_tolerance": {"tolerance": 5},
    "quantity_basis": {"basis": "load-port"},
    "no_deductions": {"exception": "taxes the law requires the payer to withhold"},
    "late_interest": {"rate": 3},
    "credit_support": {"support": "the unsecured credit limit the Seller has approved"},
    "termination": {"notice": 30},
    "governing_law": {"law": "the State of Texas", "venue": "Harris County, Texas"},
}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    sc = Scenario(
        SCENARIO_ID,
        "onboarding",
        u,
        datetime(2026, 1, 12, 15, 0, tzinfo=UTC),
        expect=EXPECT,
        title="Houston onboards Vitol: clean KYC, USD 40m credit, MPTA in four rounds",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def v(role: str) -> str:
        return f"{THEIRS}:{role}"

    me, cp = d.org(OURS), d.org(THEIRS)
    LE = cls("LegalEntity", "gleif")
    CONTRACT = cls("Contract", "fibo")

    d.mail(
        "intro",
        n("marketer"),
        v("marketer"),
        "We would like to start buying WTI from Vitol for our Houston desk. Could you point us to your compliance "
        "and credit contacts so we can open the relationship?",
        subject="NewCo Houston and Vitol: crude relationship",
        tags=["intro"],
    )
    d.mail(
        "intro-reply",
        v("marketer"),
        n("marketer"),
        "Happy to. Our compliance and credit teams are copied on the next messages.",
        reply="intro",
        days=1,
        tags=["intro"],
    )
    d.mail(
        "kyc-request",
        n("compliance_analyst"),
        v("compliance_analyst"),
        "Please send a certificate of incorporation, a certificate of good standing, an ownership chart, your LEI "
        "record, an authorised signatory list, audited financials and a tax form.",
        subject="KYC documents for NewCo Houston onboarding",
        days=1,
        tags=["kyc"],
        doc=LE,
        facts={"requested_documents": 7},
    )
    cert, standing = cert_incorporation(d, THEIRS), good_standing(d, THEIRS, d.today())
    d.mail(
        "kyc-docs-1",
        v("compliance_analyst"),
        n("compliance_analyst"),
        "The certificate of incorporation and a certificate of good standing issued this month are attached.",
        cert,
        standing,
        reply="kyc-request",
        days=1,
        tags=["kyc"],
        doc=LE,
        facts={"good_standing_issued": standing.facts["issued"]},
    )
    d.mail(
        "kyc-ack",
        n("compliance_analyst"),
        v("compliance_analyst"),
        "Received, thank you. Still outstanding: the ownership chart, LEI record and signatory list.",
        reply="kyc-docs-1",
        days=1,
        tags=["kyc"],
    )
    chart = ownership_chart(d, THEIRS)
    d.mail(
        "kyc-docs-2",
        v("compliance_analyst"),
        n("compliance_analyst"),
        "Attached are the ownership chart, our LEI record and the signatory list.",
        chart,
        lei_record(d, THEIRS),
        signatory_list(d, THEIRS),
        reply="kyc-ack",
        days=1,
        tags=["kyc", "ownership"],
        doc=LE,
        facts={"declared_beneficial_owners": chart.facts["declared_beneficial_owners"]},
    )
    fin = financial_data(d, THEIRS)
    d.mail(
        "financials",
        v("credit_analyst"),
        n("credit_analyst"),
        f"As your marketer asked, attached are our financial statements for {fin['fiscal_year']} and our tax form.",
        financial_statements(d, THEIRS, fin),
        tax_form(d, THEIRS),
        subject="Financial statements for Vitol Inc.",
        days=1,
        tags=["credit"],
        facts={"net_worth": fin["net_worth"]},
    )
    d.mail(
        "screen-request",
        n("compliance_analyst"),
        "svc-veridian:analyst",
        f"Please screen {cp['name']} and its owners and directors against sanctions, PEP and adverse media lists.",
        subject="Screening request: Vitol Inc.",
        days=1,
        tags=["screening"],
    )
    subjects = [cp["name"], *chart.facts["declared_beneficial_owners"]]
    d.mail(
        "screen-result",
        "svc-veridian:analyst",
        n("compliance_analyst"),
        "The screening report is attached. No sanctions, PEP or adverse media matches.",
        screening_report(d, THEIRS, subjects, []),
        reply="screen-request",
        days=1,
        tags=["screening", "clear"],
        facts={"result": "clear"},
    )
    answers = (
        ("Is any owner or director a politically exposed person?", "No"),
        ("Is the entity or any owner subject to sanctions?", "No"),
        ("Does the entity trade with sanctioned jurisdictions?", "No"),
    )
    d.mail(
        "questionnaire",
        v("compliance_analyst"),
        n("compliance_analyst"),
        "Our completed KYC questionnaire is attached.",
        questionnaire(d, THEIRS, d.person(v("compliance_analyst"))["name"], answers),
        reply="kyc-ack",
        days=1,
        tags=["kyc", "questionnaire"],
        doc=LE,
        facts={"answers": dict(answers)},
    )
    d.mail(
        "kyc-cleared",
        n("compliance_analyst"),
        [n("credit_analyst")],
        "KYC for Vitol Inc. is complete and clear: documents current, owners screened, questionnaire consistent with "
        "the screening report. Credit can proceed.",
        subject="KYC cleared: Vitol Inc.",
        days=1,
        cc=[n("marketer")],
        tags=["kyc", "cleared"],
    )
    credit = credit_block(REQUESTED, UNSECURED, 0, 0, 0)
    d.mail(
        "credit-request",
        n("marketer"),
        n("credit_analyst"),
        f"Please consider a limit of USD {money(REQUESTED)} for WTI purchases from Vitol. The application is attached.",
        form(
            "vitol-credit-application.pdf",
            "credit-application",
            "Credit application: Vitol Inc.",
            [
                ("Counterparty", cp["name"]),
                ("Requested limit (USD)", money(REQUESTED)),
                ("Product", "WTI Midland, FOB"),
                ("Tenor", "12 months"),
            ],
            cls("CreditFacility"),
            {"requested_limit": REQUESTED, "product": "WTI Midland"},
        ),
        subject="Credit request: Vitol Inc.",
        days=1,
        tags=["credit"],
        doc=cls("CreditFacility"),
        facts={"requested_limit": REQUESTED},
    )
    d.mail(
        "bureau-request",
        n("credit_analyst"),
        "svc-ledgerline:analyst",
        f"Please send your latest report on {cp['name']}.",
        subject="Credit report request: Vitol Inc.",
        days=1,
    )
    score = bureau_score(d, THEIRS)
    d.mail(
        "bureau-reply",
        "svc-ledgerline:analyst",
        n("credit_analyst"),
        f"Our score for {cp['name']} is {score}, in the low-risk band.",
        reply="bureau-request",
        days=1,
        tags=["credit"],
        facts={"bureau_score": score},
    )
    memo = credit_memo(
        d,
        OURS,
        THEIRS,
        credit,
        {"net_worth": fin["net_worth"], "bureau_score": score, "security": "none", "conditions": []},
    )
    d.mail(
        "credit-memo",
        n("credit_analyst"),
        n("marketer"),
        f"Approved: USD {money(credit['limit'])} unsecured, against USD {money(REQUESTED)} requested. Current "
        f"exposure is USD {money(credit['exposure'])}, so headroom is USD {money(credit['headroom'])}. The memo is attached.",
        memo,
        subject="Credit approval: Vitol Inc.",
        days=1,
        cc=[n("compliance_analyst")],
        tags=["credit", "approval"],
        doc=cls("CreditFacility"),
        facts={"requested": REQUESTED, "approved": credit["limit"], "headroom": credit["headroom"]},
    )
    letter = limit_letter(d, OURS, THEIRS, credit, d.today())
    d.mail(
        "limit-letter",
        n("credit_analyst"),
        v("credit_analyst"),
        f"We have approved a limit of USD {money(credit['limit'])} for {me['name']}. Our advice is attached.",
        letter,
        subject="Credit limit advice",
        days=1,
        tags=["credit"],
        doc=cls("CreditFacility"),
        facts={"limit": credit["limit"]},
    )
    d.mail(
        "limit-accept",
        v("credit_analyst"),
        n("credit_analyst"),
        "Noted. Please have your lawyer send the master agreement.",
        reply="limit-letter",
        days=1,
        tags=["credit"],
    )

    title = contract_title("Master Physical Trading Agreement", me, cp)
    neg = Negotiation(d, title, BASE, CONTRACT)
    d.mail(
        "mpta-draft",
        n("lawyer"),
        v("lawyer"),
        f"Our draft master agreement follows our standard terms: payment in {BASE['payment']['days']} days, "
        f"{BASE['quantity_tolerance']['tolerance']}% quantity tolerance, {BASE['quantity_basis']['basis']} quantity "
        f"basis, {BASE['late_interest']['rate']}% late interest, Texas law.",
        subject=f"{title}: draft",
        days=1,
        tags=["legal", "mpta"],
        doc=CONTRACT,
        facts={"payment_days": BASE["payment"]["days"], "late_interest": BASE["late_interest"]["rate"]},
    )
    # Each round: who marks it up, the changes, and the email text that states them.
    r1 = {"payment": {"days": 20}, "late_interest": {"rate": 5}}
    a1 = neg.round(v("lawyer"), r1, "mpta-r1", "mpta-round-1.docx")
    d.mail(
        "mpta-r1",
        v("lawyer"),
        n("lawyer"),
        f"Our mark-up is attached: payment in {r1['payment']['days']} days and "
        f"{r1['late_interest']['rate']}% late interest.",
        a1,
        reply="mpta-draft",
        days=2,
        tags=["legal", "mpta", "redline"],
        doc=CONTRACT,
        facts={"payment_days": r1["payment"]["days"], "late_interest": r1["late_interest"]["rate"]},
    )
    r2 = {"payment": {"days": BASE["payment"]["days"]}, "late_interest": {"rate": 4}}
    a2 = neg.round(n("lawyer"), r2, "mpta-r2", "mpta-round-2.docx")
    d.mail(
        "mpta-r2",
        n("lawyer"),
        v("lawyer"),
        f"We hold payment at {r2['payment']['days']} days and offer {r2['late_interest']['rate']}% late interest.",
        a2,
        reply="mpta-r1",
        days=2,
        tags=["legal", "mpta", "redline"],
        doc=CONTRACT,
        facts={"payment_days": r2["payment"]["days"], "late_interest": r2["late_interest"]["rate"]},
    )
    r3 = {"payment": {"days": 25}, "quantity_tolerance": {"tolerance": 10}}
    a3 = neg.round(v("lawyer"), r3, "mpta-r3", "mpta-round-3.docx")
    d.mail(
        "mpta-r3",
        v("lawyer"),
        n("lawyer"),
        f"We can meet at {r3['payment']['days']} days if the quantity tolerance "
        f"widens to {r3['quantity_tolerance']['tolerance']}%.",
        a3,
        reply="mpta-r2",
        days=2,
        tags=["legal", "mpta", "redline"],
        doc=CONTRACT,
        facts={"payment_days": r3["payment"]["days"], "tolerance": r3["quantity_tolerance"]["tolerance"]},
    )
    r4 = {"quantity_tolerance": {"tolerance": BASE["quantity_tolerance"]["tolerance"]}}
    a4 = neg.round(n("lawyer"), r4, "mpta-r4", "mpta-round-4.docx")
    d.mail(
        "mpta-r4",
        n("lawyer"),
        v("lawyer"),
        f"We accept {r3['payment']['days']} days and keep the tolerance at "
        f"{r4['quantity_tolerance']['tolerance']}%. This round is our final text.",
        a4,
        reply="mpta-r3",
        days=2,
        tags=["legal", "mpta", "redline", "final"],
        doc=CONTRACT,
        facts={"payment_days": neg.value("payment"), "tolerance": neg.value("quantity_tolerance")},
    )
    d.mail(
        "mpta-agreed",
        v("lawyer"),
        n("lawyer"),
        "Agreed. We will sign this week.",
        reply="mpta-r4",
        days=1,
        tags=["legal", "mpta", "agreed"],
        doc=CONTRACT,
        facts={"payment_days": neg.value("payment"), "tolerance": neg.value("quantity_tolerance")},
    )
    ssi = ssi_letter(d, OURS, [a for a in me["accounts"] if "settlement" in a][:1] or me["accounts"][:1])
    d.mail(
        "ssi-send",
        n("settlements"),
        v("settlements"),
        "Our settlement instructions for USD payments are attached.",
        ssi,
        subject="Standard settlement instructions",
        days=1,
        tags=["static-data", "ssi"],
        doc=cls("Account", "fibo"),
        facts={"account": ssi.facts["accounts"][0]["id"]},
    )
    cp_acct = u["accounts"][cp["accounts"][0]]
    d.mail(
        "ssi-confirm",
        v("settlements"),
        n("settlements"),
        f"Loaded. Our instructions are the {d.org(cp_acct['bank'])['name']} account ending {cp_acct['number'][-4:]}.",
        reply="ssi-send",
        days=1,
        tags=["static-data", "ssi"],
    )
    effective = d.today()
    d.mail(
        "go-live",
        n("lawyer"),
        n("ops"),
        "The master agreement is signed and the static data is loaded. Vitol Inc. is open for trading.",
        subject="Vitol Inc. live for trading",
        days=1,
        cc=[n("credit_analyst")],
        tags=["go-live"],
        facts={"effective": effective.isoformat()},
    )

    neg.record_positions("mpta-draft", OURS)
    bo = beneficial_owners(u, THEIRS)
    sc.ownership_edges = [e for e in u["ownership_edges"] if e["child"] == THEIRS]
    sc.beneficial_owners = bo
    sc.outcome = {"result": "approved", "limit": credit["limit"], "agreement_effective": effective.isoformat()}
    onboarded_record(
        sc,
        entity=OURS,
        counterparty=THEIRS,
        agreement=title,
        neg=neg,
        credit=credit,
        effective=effective,
        accounts={"newco": ssi.facts["accounts"][0]["id"], "counterparty": cp["accounts"][0]},
        extra={"requested": REQUESTED, "currency": "USD"},
    )
    d.question(1, "metadata", "What credit limit did NewCo Houston approve for Vitol Inc.?", credit["limit"])
    d.question(2, "hybrid_retrieval", "In how many redline rounds was the Vitol master agreement negotiated?", 4)
    d.question(3, "metadata", "What payment days did the final Vitol master agreement set?", neg.value("payment"))
    return sc
