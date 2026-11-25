# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""s2: NewCo London onboards Kestrel. PEP owner behind trust layers, enhanced due diligence, secured-only credit."""

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
    contract_title,
    credit_block,
    financial_data,
    financial_statements,
    form,
    lei_record,
    limit_letter,
    money,
    onboarded_record,
    ownership_chart,
    questionnaire,
    screening_report,
    signatory_list,
    ssi_letter,
)

SCENARIO_ID = "s2-london-kestrel-vlsfo"
EXPECT = {"emails": 28, "attachments": 17, "chats": 0}
OURS, THEIRS = "newco-lon", "cp-kestrel"
REQUESTED, CONDITIONAL_LIMIT = 10_000_000, 8_000_000
PEP_ID, TRUST_ID = "own-kestrel-pep", "hold-kestrel-trust"

BASE = {
    "payment": {"days": 10, "currency": "USD"},
    "quantity_tolerance": {"tolerance": 5},
    "quantity_basis": {"basis": "load-port"},
    "no_deductions": {"exception": "taxes the law requires the payer to withhold"},
    "late_interest": {"rate": 4},
    "credit_support": {
        "support": "a prepayment of the full cargo value before the Seller nominates a vessel, until the Seller "
        "approves a confirmed standby letter of credit"
    },
    "termination": {"notice": 30},
    "governing_law": {"law": "England and Wales", "venue": "London"},
}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    sc = Scenario(
        SCENARIO_ID,
        "onboarding",
        u,
        datetime(2026, 1, 19, 9, 0, tzinfo=UTC),
        expect=EXPECT,
        title="London onboards Kestrel: PEP owner, enhanced due diligence, secured-only credit",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def k(role: str) -> str:
        return f"{THEIRS}:{role}"

    me, cp = d.org(OURS), d.org(THEIRS)
    LE, CF, CONTRACT = cls("LegalEntity", "gleif"), cls("CreditFacility"), cls("Contract", "fibo")
    pep = u["persons"][PEP_ID]["name"]

    d.mail(
        "intro",
        n("marketer"),
        k("marketer"),
        "We would like to supply VLSFO to Kestrel. Who handles compliance and credit on your side?",
        subject="NewCo London and Kestrel: bunker fuel supply",
        tags=["intro"],
    )
    d.mail(
        "intro-reply",
        k("marketer"),
        n("marketer"),
        "Our compliance and credit contacts are copied below.",
        reply="intro",
        days=1,
        tags=["intro"],
    )
    d.mail(
        "kyc-request",
        n("compliance_analyst"),
        k("compliance_analyst"),
        "Please send a certificate of incorporation, an ownership chart that goes through to the natural persons, a "
        "trust extract for any trust in the chain, your LEI record, signatories and audited financials.",
        subject="KYC documents for Kestrel Bunkering Limited",
        days=1,
        tags=["kyc"],
        doc=LE,
    )
    d.mail(
        "kyc-docs-1",
        k("compliance_analyst"),
        n("compliance_analyst"),
        "The certificate of incorporation is attached.",
        cert_incorporation(d, THEIRS),
        reply="kyc-request",
        days=1,
        tags=["kyc"],
        doc=LE,
    )
    chart = ownership_chart(d, THEIRS)
    trust = form(
        "sandpiper-trust-extract.pdf",
        "trust-extract",
        "Trust extract: Sandpiper Trust (Jersey) Limited",
        [
            ("Trustee entity", u["orgs"][TRUST_ID]["name"]),
            ("Beneficiary", pep),
            ("Beneficial interest", "100%"),
            (
                "Holds",
                f"{next(e['percent'] for e in u['ownership_edges'] if e['parent'] == TRUST_ID):g}% of {u['orgs']['hold-kestrel-bvi']['name']}",
            ),
        ],  # noqa: E501
        cls("Beneficiary"),
        {"trustee": TRUST_ID, "beneficiary": PEP_ID, "beneficiary_name": pep},
    )
    d.mail(
        "kyc-docs-2",
        k("compliance_analyst"),
        n("compliance_analyst"),
        "The ownership chart, the trust extract, our LEI record and the signatory list are attached.",
        chart,
        trust,
        lei_record(d, THEIRS),
        signatory_list(d, THEIRS),
        reply="kyc-docs-1",
        days=1,
        tags=["kyc", "ownership", "trust-layers"],
        doc=LE,
        facts={"declared_beneficial_owners": chart.facts["declared_beneficial_owners"]},
    )
    fin = financial_data(d, THEIRS)
    d.mail(
        "financials",
        k("credit_analyst"),
        n("credit_analyst"),
        f"Attached are our financial statements for {fin['fiscal_year']}.",
        financial_statements(d, THEIRS, fin),
        subject="Financial statements for Kestrel Bunkering Limited",
        days=1,
        tags=["credit"],
        facts={"net_worth": fin["net_worth"]},
    )
    answers = (
        ("Is any owner or director a politically exposed person?", "No"),
        ("Is the entity or any owner subject to sanctions?", "No"),
        ("Is any owner a beneficiary of a trust?", "Yes, the Sandpiper Trust"),
    )
    d.mail(
        "questionnaire",
        k("compliance_analyst"),
        n("compliance_analyst"),
        "Our KYC questionnaire is attached.",
        questionnaire(d, THEIRS, d.person(k("compliance_analyst"))["name"], answers),
        reply="kyc-docs-2",
        days=1,
        tags=["kyc", "questionnaire", "denies-pep"],
        doc=LE,
        facts={"pep_declared": False},
    )
    d.mail(
        "screen-request",
        n("compliance_analyst"),
        "svc-veridian:analyst",
        "Please screen Kestrel Bunkering Limited and every owner in the attached chain, including trust beneficiaries.",
        subject="Screening request: Kestrel Bunkering Limited",
        days=1,
        tags=["screening"],
    )
    subjects = [cp["name"], *chart.facts["declared_beneficial_owners"], pep]
    hit = {
        "subject": pep,
        "finding": "politically exposed person: former member of a national legislature",
        "category": "pep",
    }
    d.mail(
        "screen-result",
        "svc-veridian:analyst",
        n("compliance_analyst"),
        f"The report is attached. One match: {pep} is a politically exposed person.",
        screening_report(d, THEIRS, subjects, [hit]),
        reply="screen-request",
        days=1,
        tags=["screening", "pep"],
        facts={"result": "hit", "pep": pep},
    )
    d.mail(
        "pep-escalation",
        n("compliance_analyst"),
        [n("credit_analyst"), n("lawyer")],
        f"The questionnaire says no owner is a PEP, but screening finds {pep} as the sole beneficiary of the trust "
        "above Kestrel. This needs enhanced due diligence before credit can go on.",
        subject="Kestrel: PEP found behind the trust",
        days=1,
        tags=["pep", "contradiction-caught"],
        facts={"pep": pep},
    )
    d.contradiction(
        "questionnaire-denies-pep",
        "questionnaire-vs-screening",
        "The questionnaire answers No to a PEP owner. Screening and the trust extract show the beneficiary is a PEP.",
        ["questionnaire", "screen-result", "kyc-docs-2"],
        {"declared": False, "found": True, "pep": pep},
        True,
        "pep-escalation",
    )
    d.mail(
        "edd-request",
        n("compliance_analyst"),
        k("compliance_analyst"),
        "Screening matched one beneficial owner as a politically exposed person. Please send a source-of-wealth "
        "statement and bank evidence, and explain the questionnaire answer.",
        subject="Kestrel: enhanced due diligence",
        days=1,
        tags=["edd", "pep"],
    )
    sow = form(
        "kestrel-source-of-wealth.pdf",
        "source-of-wealth",
        "Source of wealth statement",
        [
            ("Beneficiary", pep),
            ("Source", "Sale of a family shipping business in an earlier decade, then investment income"),
            ("Declared net worth (USD)", money(fin["net_worth"])),
        ],
        cls("Person"),
        {"person": PEP_ID, "source": "business sale"},
    )
    letter = form(
        "kestrel-bank-reference.pdf",
        "bank-reference",
        "Bank reference letter",
        [
            ("Account holder", pep),
            ("Bank", u["orgs"]["bank-concord"]["name"]),
            ("Standing", "Held in good standing for over ten years"),
        ],
        cls("AccountHolder"),
        {"person": PEP_ID, "bank": "bank-concord"},
    )
    d.mail(
        "edd-reply",
        k("compliance_analyst"),
        n("compliance_analyst"),
        "Attached are the statement and a bank reference. The questionnaire answer was an oversight: the owner is "
        "retired and we did not treat that as a political role.",
        sow,
        letter,
        reply="edd-request",
        days=2,
        tags=["edd", "pep"],
        facts={"explanation": "oversight"},
    )
    d.mail(
        "edd-check-request",
        n("compliance_analyst"),
        "svc-veridian:analyst",
        f"Please run an enhanced check on {pep}: source of wealth, adverse media and family associations.",
        subject="Enhanced check: Kestrel beneficiary",
        days=1,
        tags=["edd"],
    )
    d.mail(
        "edd-check-result",
        "svc-veridian:analyst",
        n("compliance_analyst"),
        "The enhanced report is attached. The stated source of wealth is consistent with public records; no sanctions "
        "or adverse media.",
        form(
            "kestrel-edd-report.pdf",
            "edd-report",
            "Enhanced due diligence report",
            [
                ("Subject", pep),
                ("Source of wealth", "Consistent with records"),
                ("Sanctions", "None"),
                ("Adverse media", "None"),
            ],
            cls("Service"),
            {"person": PEP_ID, "result": "consistent"},
        ),
        reply="edd-check-request",
        days=2,
        tags=["edd"],
    )
    conditions = [
        "enhanced monitoring of every payment",
        "annual refresh of the enhanced review",
        "no unsecured credit",
    ]
    d.mail(
        "compliance-approval",
        n("compliance_analyst"),
        [n("credit_analyst"), n("marketer")],
        "Compliance approves onboarding with conditions: " + "; ".join(conditions) + ". The memo is attached.",
        form(
            "kestrel-compliance-approval.pdf",
            "compliance-approval",
            "Compliance approval with conditions",
            [("Counterparty", cp["name"]), ("Decision", "Approved with conditions")],
            cls("Service"),
            {"decision": "approved-with-conditions", "conditions": conditions},
            notes=[f"Condition: {c}" for c in conditions],
        ),
        subject="Kestrel: compliance approval with conditions",
        days=1,
        tags=["edd", "approval"],
    )
    d.mail(
        "credit-request",
        n("marketer"),
        n("credit_analyst"),
        f"Please set up credit for Kestrel for VLSFO sales. We would like USD {money(REQUESTED)}.",
        subject="Credit request: Kestrel",
        days=1,
        tags=["credit"],
        doc=CF,
        facts={"requested_limit": REQUESTED},
    )
    credit = credit_block(REQUESTED, 0, 0, 0, 0)
    memo = form(
        "kestrel-credit-memo.pdf",
        "credit-memo",
        "Credit approval memo: Kestrel Bunkering Limited",
        [
            ("Counterparty", cp["name"]),
            ("Requested limit (USD)", money(REQUESTED)),
            ("Unsecured limit (USD)", money(credit["unsecured"])),
            ("Limit once a confirmed SBLC is in place (USD)", money(CONDITIONAL_LIMIT)),
            ("Current exposure (USD)", money(credit["exposure"])),
            ("Headroom now (USD)", money(credit["headroom"])),
        ],
        CF,
        {
            **credit,
            "conditional_limit": CONDITIONAL_LIMIT,
            "secured_only": True,
            "conditions": ["confirmed SBLC before any credit sale", "prepayment until then"],
        },
        notes=["Secured-only: until a confirmed standby letter of credit exists, sales are on prepayment."],
    )
    d.mail(
        "credit-memo",
        n("credit_analyst"),
        n("marketer"),
        f"Credit is secured-only. No unsecured limit; USD {money(CONDITIONAL_LIMIT)} opens once a confirmed SBLC is in "
        "place. Until then every cargo is on prepayment.",
        memo,
        reply="credit-request",
        days=1,
        tags=["credit", "secured-only"],
        doc=CF,
        facts={"unsecured_limit": 0, "conditional_limit": CONDITIONAL_LIMIT},
    )
    d.mail(
        "limit-letter",
        n("credit_analyst"),
        k("credit_analyst"),
        f"Our credit conditions for Kestrel are attached: secured-only, USD {money(CONDITIONAL_LIMIT)} against a "
        "confirmed SBLC, prepayment until then.",
        limit_letter(
            d,
            OURS,
            THEIRS,
            credit | {"limit": CONDITIONAL_LIMIT},
            d.today(),
            "This limit is available only against a confirmed standby letter of credit.",
        ),
        subject="Credit conditions for Kestrel",
        days=1,
        tags=["credit", "secured-only"],
        doc=CF,
        facts={"conditional_limit": CONDITIONAL_LIMIT},
    )
    d.mail(
        "limit-reply",
        k("credit_analyst"),
        n("credit_analyst"),
        "Understood. We will ask our bank about a standby letter of credit later this year. For now we will prepay.",
        reply="limit-letter",
        days=1,
        tags=["credit"],
    )

    title = contract_title("Master Physical Trading Agreement", me, cp)
    neg = Negotiation(d, title, BASE, CONTRACT)
    d.mail(
        "mpta-draft",
        n("lawyer"),
        k("lawyer"),
        f"Our draft is on English law with payment in {BASE['payment']['days']} days of invoice after the prepayment "
        "credit, and 5% quantity tolerance.",
        subject=f"{title}: draft",
        days=1,
        tags=["legal", "mpta"],
        doc=CONTRACT,
        facts={"payment_days": BASE["payment"]["days"]},
    )
    r1 = {"payment": {"days": 20}, "late_interest": {"rate": 2}}
    a1 = neg.round(k("lawyer"), r1, "mpta-r1", "mpta-round-1.docx")
    d.mail(
        "mpta-r1",
        k("lawyer"),
        n("lawyer"),
        f"Our mark-up: payment in {r1['payment']['days']} days and {r1['late_interest']['rate']}% late interest.",
        a1,
        reply="mpta-draft",
        days=2,
        tags=["legal", "mpta", "redline"],
        doc=CONTRACT,
        facts={"payment_days": r1["payment"]["days"], "late_interest": r1["late_interest"]["rate"]},
    )
    r2 = {"payment": {"days": 12}, "late_interest": {"rate": 3}}
    a2 = neg.round(n("lawyer"), r2, "mpta-r2", "mpta-round-2.docx")
    d.mail(
        "mpta-r2",
        n("lawyer"),
        k("lawyer"),
        f"We can do {r2['payment']['days']} days and {r2['late_interest']['rate']}% "
        "late interest. This is our final text.",
        a2,
        reply="mpta-r1",
        days=2,
        tags=["legal", "mpta", "redline", "final"],
        doc=CONTRACT,
        facts={"payment_days": r2["payment"]["days"], "late_interest": r2["late_interest"]["rate"]},
    )
    d.mail(
        "mpta-agreed",
        k("lawyer"),
        n("lawyer"),
        "Agreed. We will sign.",
        reply="mpta-r2",
        days=1,
        tags=["legal", "mpta", "agreed"],
        doc=CONTRACT,
        facts={"payment_days": neg.value("payment")},
    )
    ssi = ssi_letter(d, OURS, [a for a in me["accounts"] if u["accounts"][a]["currency"] == "USD"][:1])
    d.mail(
        "ssi-send",
        n("settlements"),
        k("settlements"),
        "Our USD settlement instructions are attached.",
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
        k("settlements"),
        n("settlements"),
        f"Loaded. Ours is the {d.org(cp_acct['bank'])['name']} account ending {cp_acct['number'][-4:]}.",
        reply="ssi-send",
        days=1,
        tags=["static-data", "ssi"],
    )
    effective = d.today()
    d.mail(
        "go-live",
        n("lawyer"),
        n("ops"),
        "The agreement is signed and static data is loaded for Kestrel. Trading is on prepayment only.",
        subject="Kestrel live: prepayment only",
        days=1,
        cc=[n("credit_analyst")],
        tags=["go-live"],
        facts={"effective": effective.isoformat(), "prepayment_only": True},
    )
    d.mail(
        "monitoring-flag",
        n("compliance_analyst"),
        n("ops"),
        "Set the enhanced monitoring flag on Kestrel. Every payment "
        "to or from Kestrel goes to compliance before release.",
        subject="Kestrel: enhanced monitoring",
        days=1,
        tags=["edd", "monitoring"],
    )
    d.mail(
        "prepay-reminder",
        n("credit_analyst"),
        n("marketer"),
        "Reminder: no sale to Kestrel on credit until a confirmed SBLC is in place. Every deal needs a prepayment.",
        subject="Kestrel: prepayment only",
        days=1,
        tags=["credit", "secured-only"],
        facts={"prepayment_only": True},
    )

    neg.record_positions("mpta-draft", OURS)
    bo = beneficial_owners(u, THEIRS)
    sc.ownership_edges = [e for e in u["ownership_edges"] if e["child"] in (THEIRS, "hold-kestrel-bvi", TRUST_ID)]
    sc.beneficial_owners = bo
    sc.outcome = {
        "result": "approved-with-conditions",
        "unsecured_limit": 0,
        "conditional_limit": CONDITIONAL_LIMIT,
        "prepayment_only": True,
        "agreement_effective": effective.isoformat(),
    }
    sc.review_events = [
        {
            "after_message": "pep-escalation",
            "event": "mark-person-pep",
            "person": PEP_ID,
            "note": "steward confirms the screening match",
        }
    ]
    onboarded_record(
        sc,
        entity=OURS,
        counterparty=THEIRS,
        agreement=title,
        neg=neg,
        credit=credit,
        effective=effective,
        accounts={"newco": ssi.facts["accounts"][0]["id"], "counterparty": cp["accounts"][0]},
        extra={
            "requested": REQUESTED,
            "currency": "USD",
            "secured_only": True,
            "conditional_limit": CONDITIONAL_LIMIT,
            "prepayment_only": True,
        },
    )
    d.question(
        1,
        "graph_traversal",
        "Who are the beneficial owners of Kestrel Bunkering Limited at 25% or more?",
        sorted(u["persons"][p]["name"] for p in bo),
    )
    d.question(
        2, "reconciliation", "Which owner of Kestrel is a PEP although the questionnaire says there is none?", pep
    )
    d.question(3, "metadata", "What unsecured credit limit does NewCo London have for Kestrel?", 0)
    return sc
