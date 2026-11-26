# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""s3: NewCo Sydney onboards Harbourline. Layered credit, NSW-law MPTA, a stale certificate and a short-dated SBLC."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    limit_letter,
    money,
    onboarded_record,
    ownership_chart,
    questionnaire,
    screening_report,
    signatory_list,
    ssi_letter,
)

SCENARIO_ID = "s3-sydney-harbourline-ulp"
EXPECT = {"emails": 26, "attachments": 20, "chats": 0}
OURS, THEIRS = "newco-syd", "cp-harbourline"
PARENT = "hold-harbourline-grp"
ISSUING, ADVISING, CONFIRMING = "bank-southerntide", "bank-bluegum", "bank-concord"
REQUESTED, UNSECURED, GUARANTEED, SBLC = 35_000_000, 5_000_000, 15_000_000, 10_000_000
TERM_DAYS = 365  # trading period the credit must cover
CLAIM_DAYS = 30  # days after the last payment due date that a drawing must still be possible
STALE_DAYS = 420  # age of the good-standing certificate Harbourline supplies

BASE: dict[str, dict[str, Any]] = {
    "payment": {"days": 30, "currency": "USD"},
    "quantity_tolerance": {"tolerance": 5},
    "quantity_basis": {"basis": "discharge"},
    "no_deductions": {"exception": "taxes the law requires the payer to withhold"},
    "late_interest": {"rate": 4},
    "credit_support": {"support": "the layered credit support described in the credit approval"},
    "termination": {"notice": 30},
    "governing_law": {"law": "New South Wales", "venue": "Sydney"},
}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    sc = Scenario(
        SCENARIO_ID,
        "onboarding",
        u,
        datetime(2026, 1, 26, 22, 0, tzinfo=UTC),
        expect=EXPECT,
        title="Sydney onboards Harbourline: layered credit, NSW-law MPTA, SBLC that expires too early",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def h(role: str) -> str:
        return f"{THEIRS}:{role}"

    me, cp, parent = d.org(OURS), d.org(THEIRS), d.org(PARENT)
    LE, CF, CONTRACT = cls("LegalEntity", "gleif"), cls("CreditFacility"), cls("Contract", "fibo")

    d.mail(
        "intro",
        n("marketer"),
        h("marketer"),
        "We would like to supply ULP 91 to Harbourline at Newcastle. Please copy "
        "your compliance and credit contacts on the next messages.",
        subject="NewCo Sydney and Harbourline: ULP supply",
        tags=["intro"],
    )
    d.mail(
        "intro-reply",
        h("marketer"),
        n("marketer"),
        "Pleased to proceed. I have copied our team.",
        reply="intro",
        days=1,
        tags=["intro"],
    )
    d.mail(
        "kyc-request",
        n("compliance_analyst"),
        h("compliance_analyst"),
        "Please send a certificate of incorporation, a certificate of good standing, an ownership chart, signatories, "
        "financials and your completed questionnaire.",
        subject="KYC documents for Harbourline Petroleum Pty Ltd",
        days=1,
        tags=["kyc"],
        doc=LE,
    )
    cert = cert_incorporation(d, THEIRS)
    standing = good_standing(d, THEIRS, d.today() - timedelta(days=STALE_DAYS))
    d.mail(
        "kyc-docs-1",
        h("compliance_analyst"),
        n("compliance_analyst"),
        "The certificate of incorporation and a certificate of good standing are attached.",
        cert,
        standing,
        reply="kyc-request",
        days=1,
        tags=["kyc", "stale-certificate"],
        doc=LE,
        facts={"good_standing_issued": standing.facts["issued"]},
    )
    d.contradiction(
        "stale-good-standing",
        "stale-document",
        f"The good-standing certificate is {STALE_DAYS} days old; it is valid for 12 months and nobody asked for a new one.",
        ["kyc-docs-1", standing.filename],
        {"issued": standing.facts["issued"], "validity_months": 12, "age_days": STALE_DAYS},
        False,
    )
    chart = ownership_chart(d, THEIRS)
    d.mail(
        "kyc-docs-2",
        h("compliance_analyst"),
        n("compliance_analyst"),
        "The ownership chart and the signatory list are attached.",
        chart,
        signatory_list(d, THEIRS),
        reply="kyc-docs-1",
        days=1,
        tags=["kyc", "ownership"],
        doc=LE,
        facts={"declared_beneficial_owners": chart.facts["declared_beneficial_owners"]},
    )
    fin = financial_data(d, THEIRS)
    d.mail(
        "financials",
        h("credit_analyst"),
        n("credit_analyst"),
        f"Our {fin['fiscal_year']} financial statements are attached.",
        financial_statements(d, THEIRS, fin),
        subject="Financial statements for Harbourline",
        days=1,
        tags=["credit"],
        facts={"net_worth": fin["net_worth"]},
    )
    d.mail(
        "screen-request",
        n("compliance_analyst"),
        "svc-veridian:analyst",
        "Please screen Harbourline Petroleum Pty Ltd, its parent and both principals.",
        subject="Screening request: Harbourline",
        days=1,
        tags=["screening"],
    )
    subjects = [cp["name"], parent["name"], *chart.facts["declared_beneficial_owners"]]
    d.mail(
        "screen-result",
        "svc-veridian:analyst",
        n("compliance_analyst"),
        "No matches. The report is attached.",
        screening_report(d, THEIRS, subjects, []),
        reply="screen-request",
        days=1,
        tags=["screening", "clear"],
        facts={"result": "clear"},
    )
    answers = (
        ("Is any owner or director a politically exposed person?", "No"),
        ("Is the entity or any owner subject to sanctions?", "No"),
        ("Who guarantees the entity's obligations?", parent["name"]),
    )
    d.mail(
        "questionnaire",
        h("compliance_analyst"),
        n("compliance_analyst"),
        "Our completed questionnaire is attached.",
        questionnaire(d, THEIRS, d.person(h("compliance_analyst"))["name"], answers),
        reply="kyc-docs-2",
        days=1,
        tags=["kyc", "questionnaire"],
        doc=LE,
    )
    d.mail(
        "credit-request",
        n("marketer"),
        n("credit_analyst"),
        f"We need credit of about USD {money(REQUESTED)} for Harbourline: "
        "monthly ULP cargoes. The application is attached.",
        form(
            "harbourline-credit-application.pdf",
            "credit-application",
            "Credit application: Harbourline Petroleum Pty Ltd",
            [
                ("Requested limit (USD)", money(REQUESTED)),
                ("Product", "ULP 91, CFR Newcastle"),
                ("Tenor (days)", str(TERM_DAYS)),
            ],
            CF,
            {"requested_limit": REQUESTED},
        ),
        subject="Credit request: Harbourline",
        days=1,
        tags=["credit"],
        doc=CF,
        facts={"requested_limit": REQUESTED},
    )
    d.mail(
        "bureau-request",
        n("credit_analyst"),
        "svc-ledgerline:analyst",
        "Please send your latest report on Harbourline Petroleum Pty Ltd.",
        subject="Credit report request: Harbourline",
        days=1,
    )
    score = bureau_score(d, THEIRS)
    d.mail(
        "bureau-reply",
        "svc-ledgerline:analyst",
        n("credit_analyst"),
        f"The score is {score}, in the moderate-risk band.",
        reply="bureau-request",
        days=1,
        tags=["credit"],
        facts={"bureau_score": score},
    )
    credit = credit_block(REQUESTED, UNSECURED, GUARANTEED, SBLC, 0)
    # The SBLC is issued later in the scenario; its expiry is fixed there and the memo claims it covers the full term.
    memo = credit_memo(
        d,
        OURS,
        THEIRS,
        credit,
        {
            "net_worth": fin["net_worth"],
            "bureau_score": score,
            "security": "parent guarantee and confirmed SBLC",
            "sblc_covers": "the full term of the agreement",
            "conditions": ["executed guarantee", "confirmed SBLC covering the full term"],
        },
    )
    d.mail(
        "credit-memo",
        n("credit_analyst"),
        n("marketer"),
        f"Approved in layers: USD {money(UNSECURED)} unsecured, USD {money(GUARANTEED)} backed by the parent guarantee "
        f"and USD {money(SBLC)} backed by a confirmed SBLC, USD {money(credit['limit'])} in all against USD {money(REQUESTED)} "
        "requested. The SBLC must cover the full term.",
        memo,
        reply="credit-request",
        days=1,
        tags=["credit", "layered"],
        doc=CF,
        facts={"requested": REQUESTED, "approved": credit["limit"], "tiers": [UNSECURED, GUARANTEED, SBLC]},
    )
    d.mail(
        "limit-letter",
        n("credit_analyst"),
        h("credit_analyst"),
        "Our credit advice is attached.",
        limit_letter(d, OURS, THEIRS, credit, d.today(), "Tiers: unsecured, parent guarantee, confirmed SBLC."),
        subject="Credit limit advice",
        days=1,
        tags=["credit", "layered"],
        doc=CF,
        facts={"limit": credit["limit"]},
    )
    guarantee = form(
        "harbourline-parent-guarantee.pdf",
        "guarantee",
        "Parent guarantee",
        [
            ("Guarantor (obligor)", parent["name"]),
            ("Principal debtor", cp["name"]),
            ("Beneficiary (obligee)", me["name"]),
            ("Amount (USD)", money(GUARANTEED)),
        ],
        cls("Contract", "fibo"),
        {
            "guarantor": PARENT,
            "debtor": THEIRS,
            "beneficiary": OURS,
            "amount": GUARANTEED,
            "roles": {"obligor": cls("Obligor"), "obligee": cls("Obligee"), "beneficiary": cls("Beneficiary")},
        },
    )
    d.mail(
        "guarantee-draft",
        n("lawyer"),
        h("lawyer"),
        f"Our draft of the parent guarantee for USD {money(GUARANTEED)} is attached.",
        guarantee,
        subject="Harbourline parent guarantee: draft",
        days=1,
        tags=["legal", "guarantee"],
        doc=CONTRACT,
        facts={"amount": GUARANTEED},
    )
    signed = form(
        "harbourline-parent-guarantee-executed.pdf",
        "guarantee",
        "Parent guarantee (executed)",
        [("Guarantor", parent["name"]), ("Signed on", d.today().isoformat()), ("Amount (USD)", money(GUARANTEED))],
        cls("Contract", "fibo"),
        {"guarantor": PARENT, "amount": GUARANTEED, "executed": True},
    )
    opinion = form(
        "harbourline-capacity-opinion.pdf",
        "legal-opinion",
        "Capacity and authority opinion",
        [("Guarantor", parent["name"]), ("Opinion", "The guarantor has capacity and the signatories are authorised")],
        cls("Agreement"),
        {"guarantor": PARENT, "qualified": False},
    )
    d.mail(
        "guarantee-executed",
        h("lawyer"),
        n("lawyer"),
        "The executed guarantee and our capacity opinion are attached.",
        signed,
        opinion,
        reply="guarantee-draft",
        days=2,
        tags=["legal", "guarantee"],
        doc=CONTRACT,
        facts={"executed": True},
    )

    issue_day = d.today() + timedelta(days=21)
    sblc_expiry = issue_day + timedelta(days=TERM_DAYS // 2)
    required_expiry = issue_day + timedelta(days=TERM_DAYS + BASE["payment"]["days"] + CLAIM_DAYS)
    sblc_facts = {
        "issuing_bank": ISSUING,
        "advising_bank": ADVISING,
        "confirming_bank": CONFIRMING,
        "applicant": THEIRS,
        "beneficiary": OURS,
        "amount": SBLC,
        "currency": "USD",
        "expiry": sblc_expiry.isoformat(),
    }
    sblc_pairs = [
        ("Issuing bank", d.org(ISSUING)["name"]),
        ("Advising bank", d.org(ADVISING)["name"]),
        ("Confirming bank", d.org(CONFIRMING)["name"]),
        ("Applicant", cp["name"]),
        ("Beneficiary", me["name"]),
        ("Amount (USD)", money(SBLC)),
        ("Expiry", sblc_expiry.isoformat()),
    ]
    d.mail(
        "sblc-request",
        n("credit_analyst"),
        f"{ADVISING}:trade_finance",
        f"Please advise a standby letter of credit for USD {money(SBLC)} in our favour. The wording we need is attached.",
        form(
            "harbourline-sblc-wording.pdf",
            "sblc-draft",
            "Standby letter of credit: required wording",
            [
                ("Beneficiary", me["name"]),
                ("Amount (USD)", money(SBLC)),
                ("Required validity", f"to {required_expiry.isoformat()} at the latest"),
            ],
            CF,
            {"amount": SBLC, "required_expiry": required_expiry.isoformat()},
        ),
        subject="SBLC for NewCo Sydney",
        days=1,
        tags=["sblc"],
        doc=CF,
        facts={"required_expiry": required_expiry.isoformat()},
    )
    d.mail(
        "sblc-issued",
        f"{ISSUING}:trade_finance",
        [f"{ADVISING}:trade_finance", n("credit_analyst")],
        f"We issued the standby letter of credit for USD {money(SBLC)}, expiry {sblc_expiry.isoformat()}. The issuance "
        "advice is attached.",
        form(
            "harbourline-sblc-issuance.pdf", "sblc-issuance-advice", "SBLC issuance advice", sblc_pairs, CF, sblc_facts
        ),
        subject="Standby letter of credit issued",
        days=3,
        tags=["sblc", "expiry-too-early"],
        doc=CF,
        facts={"expiry": sblc_expiry.isoformat()},
    )
    d.mail(
        "sblc-confirmed",
        f"{CONFIRMING}:trade_finance",
        n("credit_analyst"),
        f"We confirm the standby letter of credit issued by {d.org(ISSUING)['name']}.",
        form(
            "harbourline-sblc-confirmation.pdf",
            "sblc-confirmation",
            "SBLC confirmation",
            sblc_pairs + [("Confirmation", "Added")],
            cls("CEG"),
            sblc_facts | {"confirmed": True},
        ),
        subject="Confirmation of standby letter of credit",
        days=2,
        tags=["sblc", "confirmation"],
        doc=cls("CEG"),
        facts={"expiry": sblc_expiry.isoformat(), "confirmed": True},
    )
    d.contradiction(
        "sblc-expires-too-early",
        "security-term",
        "The SBLC expires before the trading period, the payment term and the claim window end; the credit memo says it covers the full term.",
        ["credit-memo", "sblc-issued", "sblc-confirmed"],
        {
            "sblc_expiry": sblc_expiry.isoformat(),
            "required_expiry": required_expiry.isoformat(),
            "short_by_days": (required_expiry - sblc_expiry).days,
        },
        False,
    )

    title = contract_title("Master Physical Trading Agreement", me, cp)
    neg = Negotiation(d, title, BASE, CONTRACT)
    d.mail(
        "mpta-draft",
        n("lawyer"),
        h("lawyer"),
        f"Our draft is on {BASE['governing_law']['law']} law with payment in "
        f"{BASE['payment']['days']} days, {BASE['quantity_basis']['basis']} quantity basis and {BASE['late_interest']['rate']}% "
        "late interest.",
        subject=f"{title}: draft",
        days=1,
        tags=["legal", "mpta"],
        doc=CONTRACT,
        facts={"payment_days": BASE["payment"]["days"], "late_interest": BASE["late_interest"]["rate"]},
    )
    r1 = {"late_interest": {"rate": 2}, "payment": {"days": 45}}
    a1 = neg.round(h("lawyer"), r1, "mpta-r1", "mpta-round-1.docx")
    d.mail(
        "mpta-r1",
        h("lawyer"),
        n("lawyer"),
        f"Our only change is late interest at {r1['late_interest']['rate']}%. Mark-up attached.",
        a1,
        reply="mpta-draft",
        days=2,
        tags=["legal", "mpta", "redline", "email-vs-redline"],
        doc=CONTRACT,
        facts={"late_interest": r1["late_interest"]["rate"], "payment_days_stated": None},
    )
    r2 = {"late_interest": {"rate": 3}}
    a2 = neg.round(n("lawyer"), r2, "mpta-r2", "mpta-round-2.docx")
    d.mail(
        "mpta-r2",
        n("lawyer"),
        h("lawyer"),
        f"We accept your note that only late interest changed and counter at "
        f"{r2['late_interest']['rate']}%. Payment stays at {BASE['payment']['days']} days.",
        a2,
        reply="mpta-r1",
        days=2,
        tags=["legal", "mpta", "redline", "email-vs-redline"],
        doc=CONTRACT,
        facts={"late_interest": r2["late_interest"]["rate"], "payment_days_stated": BASE["payment"]["days"]},
    )
    d.contradiction(
        "email-disagrees-with-redline",
        "email-vs-redline",
        "Harbourline's email says its only change is late interest, but its redline also moves payment from "
        f"{BASE['payment']['days']} to {r1['payment']['days']} days. NewCo's reply repeats that payment stays at {BASE['payment']['days']} days.",
        ["mpta-r1", "mpta-r2", "mpta-round-1.docx"],
        {"email_payment_days": BASE["payment"]["days"], "redline_payment_days": neg.value("payment")},
        False,
    )
    r3 = {"quantity_tolerance": {"tolerance": 8}}
    a3 = neg.round(h("lawyer"), r3, "mpta-r3", "mpta-round-3.docx")
    d.mail(
        "mpta-r3",
        h("lawyer"),
        n("lawyer"),
        f"We accept {r2['late_interest']['rate']}% and ask for {r3['quantity_tolerance']['tolerance']}% "
        "quantity tolerance.",
        a3,
        reply="mpta-r2",
        days=2,
        tags=["legal", "mpta", "redline"],
        doc=CONTRACT,
        facts={"tolerance": r3["quantity_tolerance"]["tolerance"]},
    )
    d.mail(
        "mpta-agreed",
        n("lawyer"),
        h("lawyer"),
        f"Agreed: {neg.value('quantity_tolerance')}% tolerance and {neg.value('late_interest')}% "
        "late interest. We will sign.",
        reply="mpta-r3",
        days=1,
        tags=["legal", "mpta", "agreed"],
        doc=CONTRACT,
        facts={"tolerance": neg.value("quantity_tolerance"), "late_interest": neg.value("late_interest")},
    )
    ssi = ssi_letter(d, OURS, [a for a in me["accounts"] if u["accounts"][a]["currency"] == "USD"][:1])
    d.mail(
        "ssi-send",
        n("settlements"),
        h("settlements"),
        "Our USD settlement instructions are attached.",
        ssi,
        subject="Standard settlement instructions",
        days=1,
        tags=["static-data", "ssi"],
        doc=cls("Account", "fibo"),
        facts={"account": ssi.facts["accounts"][0]["id"]},
    )
    effective = d.today()
    d.mail(
        "go-live",
        n("lawyer"),
        n("ops"),
        "The agreement is signed and Harbourline static data is loaded.",
        subject="Harbourline live for trading",
        days=1,
        cc=[n("credit_analyst")],
        tags=["go-live"],
        facts={"effective": effective.isoformat()},
    )

    neg.record_positions("mpta-draft", OURS)
    bo = beneficial_owners(u, THEIRS)
    sc.ownership_edges = [e for e in u["ownership_edges"] if e["child"] in (THEIRS, PARENT)]
    sc.beneficial_owners = bo
    sc.outcome = {"result": "approved", "limit": credit["limit"], "agreement_effective": effective.isoformat()}
    sc.review_events = [
        {
            "after_message": "sblc-confirmed",
            "event": "flag-security-expiry",
            "expiry": sblc_expiry.isoformat(),
            "required_expiry": required_expiry.isoformat(),
        }
    ]
    cp_acct = u["accounts"][cp["accounts"][0]]
    onboarded_record(
        sc,
        entity=OURS,
        counterparty=THEIRS,
        agreement=title,
        neg=neg,
        credit=credit,
        effective=effective,
        accounts={"newco": ssi.facts["accounts"][0]["id"], "counterparty": cp_acct["id"]},
        extra={
            "requested": REQUESTED,
            "currency": "USD",
            "sblc_expiry": sblc_expiry.isoformat(),
            "required_sblc_expiry": required_expiry.isoformat(),
            "sblc_banks": [ISSUING, ADVISING, CONFIRMING],
            "guarantor": PARENT,
        },
    )
    d.question(1, "metadata", "What is the combined credit limit approved for Harbourline?", credit["limit"])
    d.question(
        2,
        "reconciliation",
        "What payment days does the Harbourline redline set, against what the emails state?",
        neg.value("payment"),
    )
    d.question(
        3, "hybrid_retrieval", "When does the Harbourline standby letter of credit expire?", sblc_expiry.isoformat()
    )
    return sc
