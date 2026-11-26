# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""s4: NewCo New York and Glencore. ISDA Schedule and CSA in three rounds, a qualified netting opinion, an alias."""

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
    cert_incorporation,
    credit_block,
    credit_memo,
    financial_data,
    financial_statements,
    form,
    lei_record,
    limit_letter,
    money,
    onboarded_record,
    ownership_chart,
    screening_report,
    signatory_list,
    ssi_letter,
)

SCENARIO_ID = "s4-newyork-glencore-isda"
EXPECT = {"emails": 24, "attachments": 22, "chats": 0}
OURS, THEIRS = "newco-nyc", "cp-glencore"
COUNSEL = "svc-calloway"
REQUESTED, UNSECURED = 30_000_000, 20_000_000
COLLATERAL_ACCOUNT = "acct-newco-nyc-usd-collateral"

SCHEDULE: dict[str, dict[str, Any]] = {
    "netting": {"scope": "a single net amount payable by one party"},
    "cross_default": {"threshold": 5_000_000},
    "governing_law": {"law": "the State of New York", "venue": "New York County"},
    "termination": {"notice": 30},
}
CSA: dict[str, dict[str, Any]] = {
    "threshold": {"amount": 0},
    "min_transfer": {"amount": 500_000},
    "eligible_collateral": {"assets": "cash in US dollars"},
    "valuation": {"time": "close of business in New York"},
}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    sc = Scenario(
        SCENARIO_ID,
        "onboarding",
        u,
        datetime(2026, 2, 2, 15, 0, tzinfo=UTC),
        expect=EXPECT,
        title="New York onboards Glencore: ISDA Schedule and CSA in three rounds, netting opinion, collateral",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def g(role: str) -> str:
        return f"{THEIRS}:{role}"

    me, cp = d.org(OURS), d.org(THEIRS)
    alias = next(a for a in cp["aliases"] if a != cp["name"] and a.endswith("Limited"))
    LE, CF = cls("LegalEntity", "gleif"), cls("CreditFacility")
    CONTRACT, CSA_CLASS = cls("Contract", "fibo"), cls("CollateralAgreement")

    d.mail(
        "intro",
        n("marketer"),
        g("marketer"),
        "We want to trade WTI swaps with Glencore. Our lawyer will start the ISDA "
        "documents once KYC and credit are done.",
        subject="NewCo New York and Glencore: WTI swaps",
        tags=["intro"],
    )
    d.mail(
        "kyc-request",
        n("compliance_analyst"),
        g("compliance_analyst"),
        "Please send your certificate of incorporation, ownership chart, LEI record, signatories and financial "
        "statements.",
        subject="KYC documents for Glencore",
        days=1,
        tags=["kyc"],
        doc=LE,
    )
    chart = ownership_chart(d, THEIRS)
    d.mail(
        "kyc-docs",
        g("compliance_analyst"),
        n("compliance_analyst"),
        f"Attached: certificate of incorporation, ownership chart, LEI record and signatories. Our counsel will sign as "
        f"{alias} for the ISDA documents.",
        cert_incorporation(d, THEIRS),
        chart,
        lei_record(d, THEIRS),
        signatory_list(d, THEIRS),
        reply="kyc-request",
        days=1,
        tags=["kyc", "alias"],
        doc=LE,
        facts={"legal_name": cp["name"], "alias_used": alias, "lei": cp["lei"]},
    )
    fin = financial_data(d, THEIRS)
    d.mail(
        "financials",
        g("credit_analyst"),
        n("credit_analyst"),
        f"Our {fin['fiscal_year']} financial statements are attached.",
        financial_statements(d, THEIRS, fin),
        subject="Financial statements for Glencore",
        days=1,
        tags=["credit"],
        facts={"net_worth": fin["net_worth"]},
    )
    d.mail(
        "screen-request",
        n("compliance_analyst"),
        "svc-veridian:analyst",
        f"Please screen {cp['name']} and its owners. It may also appear as {alias}.",
        subject="Screening request: Glencore",
        days=1,
        tags=["screening", "alias"],
    )
    d.mail(
        "screen-result",
        "svc-veridian:analyst",
        n("compliance_analyst"),
        f"No matches for {cp['name']} or {alias}. The report is attached.",
        screening_report(d, THEIRS, [cp["name"], alias], []),
        reply="screen-request",
        days=1,
        tags=["screening", "clear"],
        facts={"result": "clear"},
    )
    d.mail(
        "credit-request",
        n("marketer"),
        n("credit_analyst"),
        f"Please set a derivatives limit of USD {money(REQUESTED)} for Glencore.",
        subject="Credit request: Glencore",
        days=1,
        tags=["credit"],
        doc=CF,
        facts={"requested_limit": REQUESTED},
    )
    credit = credit_block(REQUESTED, UNSECURED, 0, 0, 0)
    d.mail(
        "credit-memo",
        n("credit_analyst"),
        n("marketer"),
        f"Approved: USD {money(credit['limit'])} against USD {money(REQUESTED)} requested, on a CSA with a threshold. "
        "The memo is attached.",
        credit_memo(
            d,
            OURS,
            THEIRS,
            credit,
            {
                "net_worth": fin["net_worth"],
                "security": "CSA",
                "conditions": ["executed ISDA Schedule and CSA", "netting opinion"],
            },
        ),
        reply="credit-request",
        days=1,
        tags=["credit"],
        doc=CF,
        facts={"requested": REQUESTED, "approved": credit["limit"]},
    )
    d.mail(
        "limit-letter",
        n("credit_analyst"),
        g("credit_analyst"),
        "Our credit advice is attached.",
        limit_letter(d, OURS, THEIRS, credit, d.today()),
        subject="Credit limit advice",
        days=1,
        tags=["credit"],
        doc=CF,
        facts={"limit": credit["limit"]},
    )
    d.mail(
        "counsel-instruct",
        n("lawyer"),
        f"{COUNSEL}:partner",
        "Please prepare the ISDA Schedule and Credit Support Annex for "
        "NewCo Derivatives New York, Inc. and Glencore. Cash collateral only; the threshold needs to be negotiated.",
        subject="ISDA Schedule and CSA: Glencore",
        days=1,
        tags=["legal", "isda"],
        doc=CONTRACT,
    )

    sched_title = f"ISDA Schedule between {me['name']} and {alias}"
    csa_title = f"Credit Support Annex between {me['name']} and {alias}"
    sneg, cneg = Negotiation(d, sched_title, SCHEDULE, CONTRACT), Negotiation(d, csa_title, CSA, CSA_CLASS)
    rounds: tuple[tuple[str, dict[str, dict[str, Any]], dict[str, dict[str, Any]]], ...] = (
        (f"{COUNSEL}:partner", {"cross_default": {"threshold": 10_000_000}}, {"threshold": {"amount": 1_000_000}}),
        (
            g("lawyer"),
            {
                "cross_default": {"threshold": 25_000_000},
                "netting": {"scope": "separate net amounts for each product group"},
            },
            {"threshold": {"amount": 5_000_000}, "min_transfer": {"amount": 250_000}},
        ),
        (
            f"{COUNSEL}:partner",
            {"cross_default": {"threshold": 15_000_000}, "netting": {"scope": SCHEDULE["netting"]["scope"]}},
            {"threshold": {"amount": 2_500_000}},
        ),
    )
    for i, (who, sch, csa) in enumerate(rounds, start=1):
        dest = g("lawyer") if i != 2 else f"{COUNSEL}:partner"
        sa = sneg.round(who, sch, f"isda-r{i}", f"isda-schedule-round-{i}.docx")
        ca = cneg.round(who, csa, f"isda-r{i}", f"isda-csa-round-{i}.docx")
        text = (
            f"Round {i} of the Schedule and the CSA is attached. Cross default threshold USD {money(sneg.value('cross_default'))}; "
            f"CSA threshold USD {money(cneg.value('threshold'))}, minimum transfer USD {money(cneg.value('min_transfer'))}."
        )
        d.mail(
            f"isda-r{i}",
            who,
            dest,
            text,
            sa,
            ca,
            reply=None if i == 1 else f"isda-r{i - 1}",
            subject=f"ISDA Schedule and CSA: round {i}" if i == 1 else None,
            days=2,
            tags=["legal", "isda", "redline"] + (["final"] if i == 3 else []),
            doc=CONTRACT,
            facts={
                "cross_default": sneg.value("cross_default"),
                "threshold": cneg.value("threshold"),
                "min_transfer": cneg.value("min_transfer"),
            },
        )
    d.mail(
        "isda-agreed",
        g("lawyer"),
        f"{COUNSEL}:partner",
        f"Agreed on the round 3 text, including the USD {money(cneg.value('threshold'))} threshold.",
        reply="isda-r3",
        days=1,
        tags=["legal", "isda", "agreed"],
        doc=CONTRACT,
        facts={"threshold": cneg.value("threshold")},
    )
    d.mail(
        "isda-executed",
        f"{COUNSEL}:partner",
        n("lawyer"),
        "The executed Schedule and CSA are attached.",
        form(
            "isda-schedule-executed.pdf",
            "agreement",
            sched_title + " (executed)",
            [
                ("Party A", me["name"]),
                ("Party B", alias),
                ("Cross default threshold (USD)", money(sneg.value("cross_default"))),
            ],
            CONTRACT,
            {
                "party_b_as_signed": alias,
                "party_b_legal_name": cp["name"],
                "cross_default": sneg.value("cross_default"),
            },
        ),
        form(
            "isda-csa-executed.pdf",
            "agreement",
            csa_title + " (executed)",
            [
                ("Threshold (USD)", money(cneg.value("threshold"))),
                ("Minimum transfer (USD)", money(cneg.value("min_transfer"))),
                ("Eligible collateral", cneg.value("eligible_collateral")),
            ],
            CSA_CLASS,
            {
                "threshold": cneg.value("threshold"),
                "min_transfer": cneg.value("min_transfer"),
                "roles": {"obligor": cls("Obligor"), "obligee": cls("Obligee")},
            },
        ),
        reply="isda-agreed",
        days=3,
        tags=["legal", "isda", "alias"],
        doc=CONTRACT,
        facts={"party_b_as_signed": alias},
    )
    d.contradiction(
        "alias-glencore-limited",
        "alias",
        f"The ISDA documents name the counterparty {alias!r}; the certificate, LEI and universe use {cp['name']!r}.",
        ["kyc-docs", "isda-executed"],
        {"signed_as": alias, "legal_name": cp["name"], "lei": cp["lei"]},
        False,
    )
    qualifications = [
        "enforceability of close-out netting is not covered where a party is a bank branch in an unlisted state",
        "netting of collateral is excluded in a bankruptcy of the counterparty's guarantor",
    ]
    opinion = form(
        "glencore-netting-opinion.pdf",
        "legal-opinion",
        "Netting opinion",
        [
            ("Counterparty", cp["name"]),
            ("Conclusion", "Close-out netting is enforceable, subject to the qualifications below"),
        ],
        cls("Agreement"),
        {"qualified": True, "qualifications": qualifications},
        notes=[f"Qualification: {q}" for q in qualifications],
    )
    d.mail(
        "netting-opinion",
        f"{COUNSEL}:partner",
        n("lawyer"),
        "The netting opinion is attached. It is qualified; please read the qualifications.",
        opinion,
        subject="Netting opinion: Glencore",
        days=1,
        tags=["legal", "opinion", "qualified"],
        doc=cls("Agreement"),
        facts={"qualified": True},
    )
    d.mail(
        "opinion-summary",
        n("lawyer"),
        n("credit_analyst"),
        "The netting opinion is clean: close-out netting is enforceable with "
        "no qualifications. The credit approval can stand.",
        subject="Glencore netting opinion",
        days=1,
        tags=["legal", "opinion", "qualified-opinion-misread"],
        facts={"stated": "no qualifications"},
    )
    d.contradiction(
        "qualified-opinion",
        "opinion-vs-summary",
        "The netting opinion carries two qualifications; the internal summary says there are none.",
        ["netting-opinion", "opinion-summary"],
        {"opinion_qualified": True, "summary": "no qualifications"},
        False,
    )
    coll = u["accounts"][COLLATERAL_ACCOUNT]
    d.mail(
        "collateral-request",
        n("ops"),
        f"{coll['bank']}:operations",
        "Please confirm the segregated collateral account for the CSA, as set out in the attached form.",
        form(
            "collateral-account-setup.pdf",
            "account-setup",
            "Collateral account set-up",
            [
                ("Account holder", me["name"]),
                ("Account", coll["number"]),
                ("Currency", coll["currency"]),
                ("Purpose", coll["purpose"]),
            ],
            cls("Account", "fibo"),
            {"account": COLLATERAL_ACCOUNT, "number": coll["number"]},
        ),
        subject="Collateral account set-up",
        days=1,
        tags=["collateral", "static-data"],
        doc=cls("Account", "fibo"),
    )
    d.mail(
        "collateral-confirm",
        f"{coll['bank']}:operations",
        n("ops"),
        "The account is open and segregated for collateral.",
        form(
            "collateral-account-confirmation.pdf",
            "account-confirmation",
            "Collateral account confirmation",
            [("Account", coll["number"]), ("Bank", d.org(coll["bank"])["name"]), ("Status", "Open, segregated")],
            cls("AccountHolder"),
            {"account": COLLATERAL_ACCOUNT, "status": "open"},
        ),
        reply="collateral-request",
        days=1,
        tags=["collateral", "static-data"],
    )
    ssi = ssi_letter(d, OURS, [a for a in me["accounts"]])
    d.mail(
        "ssi-send",
        n("settlements"),
        g("settlements"),
        "Our settlement and collateral account details are attached.",
        ssi,
        subject="Standard settlement instructions",
        days=1,
        tags=["static-data", "ssi"],
        doc=cls("Account", "fibo"),
        facts={"accounts": [a["id"] for a in ssi.facts["accounts"]]},
    )
    d.mail(
        "collateral-terms",
        g("settlements"),
        n("settlements"),
        f"We confirm cash collateral, the USD {money(cneg.value('threshold'))} "
        "threshold and the margin contacts attached.",
        form(
            "glencore-margin-contacts.pdf",
            "margin-contacts",
            "Margin call contacts",
            [
                ("Threshold (USD)", money(cneg.value("threshold"))),
                ("Eligible collateral", cneg.value("eligible_collateral")),
                ("Margin contact", d.person(g("settlements"))["name"]),
            ],
            CSA_CLASS,
            {"threshold": cneg.value("threshold"), "contact": d.person(g("settlements"))["name"]},
        ),
        reply="ssi-send",
        days=1,
        tags=["collateral"],
        facts={"threshold": cneg.value("threshold")},
    )
    d.mail(
        "margin-process",
        g("ops"),
        n("ops"),
        "Our valuation agent contact and margin call process are attached.",
        form(
            "glencore-margin-process.pdf",
            "margin-process",
            "Margin call process",
            [("Valuation", cneg.value("valuation")), ("Minimum transfer (USD)", money(cneg.value("min_transfer")))],
            CSA_CLASS,
            {"min_transfer": cneg.value("min_transfer"), "valuation": cneg.value("valuation")},
        ),
        subject="Margin call process",
        days=1,
        tags=["collateral"],
    )
    d.mail(
        "limit-active",
        n("credit_analyst"),
        [n("marketer"), n("ops")],
        f"The Glencore limit of USD {money(credit['limit'])} is active. "
        f"The CSA threshold is USD {money(cneg.value('threshold'))} and the minimum transfer is USD {money(cneg.value('min_transfer'))}.",
        subject="Glencore limit active",
        days=1,
        tags=["credit", "collateral"],
        doc=CF,
        facts={"limit": credit["limit"], "threshold": cneg.value("threshold")},
    )
    effective = d.today()
    d.mail(
        "go-live",
        n("lawyer"),
        n("ops"),
        "The documents are signed and Glencore is open for swaps.",
        subject="Glencore live for derivatives",
        days=1,
        tags=["go-live"],
        facts={"effective": effective.isoformat()},
    )

    sneg.record_positions("isda-r1", OURS)
    cneg.record_positions("isda-r1", OURS)
    bo = beneficial_owners(u, THEIRS)
    sc.ownership_edges = [e for e in u["ownership_edges"] if e["child"] == THEIRS]
    sc.beneficial_owners = bo
    sc.outcome = {
        "result": "approved",
        "limit": credit["limit"],
        "threshold": cneg.value("threshold"),
        "agreement_effective": effective.isoformat(),
    }
    sc.review_events = [
        {
            "after_message": "isda-executed",
            "event": "merge-alias",
            "alias": alias,
            "canonical": cp["name"],
            "org": THEIRS,
        }
    ]
    values = {k: dict(v) for k, v in sneg.values.items()}
    onboarded_record(
        sc,
        entity=OURS,
        counterparty=THEIRS,
        agreement=sched_title,
        neg=sneg,
        credit=credit,
        effective=effective,
        accounts={"newco": COLLATERAL_ACCOUNT, "counterparty": cp["accounts"][0]},
        extra={
            "requested": REQUESTED,
            "currency": "USD",
            "csa": {k: dict(v) for k, v in cneg.values.items()},
            "csa_agreement": csa_title,
            "threshold": cneg.value("threshold"),
            "min_transfer": cneg.value("min_transfer"),
            "alias": alias,
            "schedule": values,
        },
    )
    d.question(
        1,
        "reconciliation",
        "Under what name was the Glencore ISDA Schedule signed, and what is the legal name?",
        [alias, cp["name"]],
    )
    d.question(2, "metadata", "What is the CSA threshold agreed with Glencore (USD)?", cneg.value("threshold"))
    d.question(3, "hybrid_retrieval", "Is the Glencore netting opinion qualified?", "yes")
    return sc
