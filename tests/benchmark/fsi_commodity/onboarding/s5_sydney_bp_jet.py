# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""s5: NewCo Sydney relies on London's KYC for BP, with a sub-limit inside a USD 150m group limit and an accession letter."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..ontology import cls
from ..scenario import Scenario
from ..universe import beneficial_owners
from .common import (
    Desk,
    cert_incorporation,
    contract_title,
    credit_block,
    form,
    good_standing,
    limit_letter,
    money,
    onboarded_record,
    ownership_chart,
    screening_report,
    ssi_letter,
)

SCENARIO_ID = "s5-sydney-bp-jet"
EXPECT = {"emails": 20, "attachments": 12, "chats": 0}
OURS, LONDON, PARENT, BP_AU = "newco-syd", "newco-lon", "cp-bp", "cp-bp-au"
GROUP_LIMIT, LONDON_EXPOSURE, SUB_LIMIT = 150_000_000, 62_000_000, 40_000_000
POLICY_MONTHS = 12  # KYC must have been refreshed within this many months
STALE_DAYS = 500  # age of the London good-standing certificate on file

# The agreement NewCo London signed with BP before this scenario; Sydney accedes to it.
LONDON_AGREEMENT: dict[str, dict[str, Any]] = {
    "payment": {"days": 30, "currency": "USD"},
    "quantity_tolerance": {"tolerance": 5},
    "quantity_basis": {"basis": "discharge"},
    "governing_law": {"law": "England and Wales", "venue": "London"},
}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    sc = Scenario(
        SCENARIO_ID,
        "onboarding",
        u,
        datetime(2026, 2, 16, 22, 0, tzinfo=UTC),
        expect=EXPECT,
        title="Sydney onboards BP via London: KYC reliance, USD 40m sub-limit inside a USD 150m group limit",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def lo(role: str) -> str:
        return f"{LONDON}:{role}"

    def bp(role: str) -> str:
        return f"{PARENT}:{role}"

    def au(role: str) -> str:
        return f"{BP_AU}:{role}"

    me, parent, cp = d.org(OURS), d.org(PARENT), d.org(BP_AU)
    LE, CF, CONTRACT = cls("LegalEntity", "gleif"), cls("CreditFacility"), cls("Contract", "fibo")
    agreement = contract_title("Master Physical Trading Agreement", d.org(LONDON), parent)

    d.mail(
        "intro",
        n("marketer"),
        lo("marketer"),
        "BP Australia wants Jet A-1 from Sydney. London already trades with BP. Can we "
        "work under your agreement and your KYC?",
        subject="BP: Sydney joins London's relationship",
        tags=["intro"],
    )
    d.mail(
        "reliance-request",
        n("compliance_analyst"),
        lo("compliance_analyst"),
        "Please share the KYC file for BP Oil International so Sydney can rely on it for BP Australia.",
        subject="BP: KYC reliance",
        days=1,
        tags=["kyc", "reliance"],
        doc=LE,
    )
    standing = good_standing(d, PARENT, d.today() - timedelta(days=STALE_DAYS))
    chart = ownership_chart(d, PARENT)
    d.mail(
        "kyc-file",
        lo("compliance_analyst"),
        n("compliance_analyst"),
        "London's file for BP Oil International is attached: certificate of incorporation, certificate of good standing and the "
        "ownership chart.",
        cert_incorporation(d, PARENT),
        standing,
        chart,
        reply="reliance-request",
        days=1,
        tags=["kyc", "reliance", "stale-certificate"],
        doc=LE,
        facts={"good_standing_issued": standing.facts["issued"]},
    )
    d.contradiction(
        "stale-london-kyc",
        "stale-document",
        f"Sydney relies on London's KYC, whose good-standing certificate is {STALE_DAYS} days old against a {POLICY_MONTHS}-month policy.",
        ["kyc-file", standing.filename, "reliance-confirm"],
        {"issued": standing.facts["issued"], "policy_months": POLICY_MONTHS, "age_days": STALE_DAYS},
        False,
    )
    d.mail(
        "au-docs",
        au("compliance_analyst"),
        n("compliance_analyst"),
        "Our certificate of incorporation is attached.",
        cert_incorporation(d, BP_AU),
        subject="BP Australia Pty Ltd: certificate of incorporation",
        days=1,
        tags=["kyc"],
        doc=LE,
    )
    d.mail(
        "screen-request",
        n("compliance_analyst"),
        "svc-veridian:analyst",
        "Please refresh screening for BP Oil International Limited and BP Australia Pty Ltd.",
        subject="Screening request: BP entities",
        days=1,
        tags=["screening"],
    )
    d.mail(
        "screen-result",
        "svc-veridian:analyst",
        n("compliance_analyst"),
        "No matches for either entity. The report is attached.",
        screening_report(d, BP_AU, [parent["name"], cp["name"]], []),
        reply="screen-request",
        days=1,
        tags=["screening", "clear"],
        facts={"result": "clear"},
    )
    letter = form(
        "newco-sydney-reliance-letter.pdf",
        "reliance-letter",
        "KYC reliance letter",
        [
            ("Relying entity", me["name"]),
            ("Relied-on entity", d.org(LONDON)["name"]),
            ("Counterparty", cp["name"]),
            ("Source file", parent["name"]),
        ],
        cls("Agreement"),
        {"relying": OURS, "relied_on": LONDON, "counterparty": BP_AU},
    )
    d.mail(
        "reliance-letter",
        n("compliance_analyst"),
        lo("compliance_analyst"),
        "Our reliance letter is attached for your confirmation.",
        letter,
        subject="BP: reliance letter",
        days=1,
        tags=["kyc", "reliance"],
        doc=cls("Agreement"),
    )
    d.mail(
        "reliance-confirm",
        lo("compliance_analyst"),
        n("compliance_analyst"),
        "Confirmed: London's KYC for BP Oil International stands and Sydney may rely on it.",
        reply="reliance-letter",
        days=1,
        tags=["kyc", "reliance"],
        facts={"confirmed": True},
    )
    terms = LONDON_AGREEMENT
    d.mail(
        "agreement-terms",
        lo("lawyer"),
        n("lawyer"),
        f"London's BP agreement has payment in {terms['payment']['days']} days, "
        f"{terms['quantity_tolerance']['tolerance']}% tolerance on {terms['quantity_basis']['basis']} quantity and "
        f"{terms['governing_law']['law']} law. Sydney would take it as is.",
        subject="BP: London agreement terms",
        days=1,
        tags=["legal", "accession"],
        doc=CONTRACT,
        facts={"payment_days": terms["payment"]["days"]},
    )
    d.mail(
        "credit-request",
        n("marketer"),
        n("credit_analyst"),
        f"Please set a sub-limit of USD {money(SUB_LIMIT)} for BP Australia, inside the BP group limit.",
        subject="Credit request: BP Australia",
        days=1,
        tags=["credit"],
        doc=CF,
        facts={"requested_limit": SUB_LIMIT},
    )
    d.mail(
        "exposure-request",
        n("credit_analyst"),
        lo("credit_analyst"),
        "What is NewCo London's open exposure to the BP group today?",
        subject="BP group exposure",
        days=1,
        tags=["credit", "group-limit"],
    )
    group_headroom = GROUP_LIMIT - LONDON_EXPOSURE
    exposure_doc = form(
        "bp-group-exposure.pdf",
        "exposure-statement",
        "BP group exposure statement",
        [
            ("Group limit (USD)", money(GROUP_LIMIT)),
            ("NewCo London open exposure (USD)", money(LONDON_EXPOSURE)),
            ("Group headroom (USD)", money(group_headroom)),
        ],
        CF,
        {"group_limit": GROUP_LIMIT, "london_exposure": LONDON_EXPOSURE, "group_headroom": group_headroom},
    )
    d.mail(
        "exposure-reply",
        lo("credit_analyst"),
        n("credit_analyst"),
        f"Open London exposure to the BP group is USD {money(LONDON_EXPOSURE)} "
        f"against the USD {money(GROUP_LIMIT)} group limit. Statement attached.",
        exposure_doc,
        reply="exposure-request",
        days=1,
        tags=["credit", "group-limit"],
        facts={"london_exposure": LONDON_EXPOSURE, "group_limit": GROUP_LIMIT},
    )
    credit = credit_block(SUB_LIMIT, SUB_LIMIT, 0, 0, 0)
    sub_headroom = min(credit["headroom"], group_headroom)
    memo = form(
        "bp-australia-credit-memo.pdf",
        "credit-memo",
        "Credit approval memo: BP Australia Pty Ltd",
        [
            ("Counterparty", cp["name"]),
            ("Group limit (USD)", money(GROUP_LIMIT)),
            ("London open exposure (USD)", money(LONDON_EXPOSURE)),
            ("Sydney sub-limit (USD)", money(credit["limit"])),
            ("Sydney exposure (USD)", money(credit["exposure"])),
            ("Sydney headroom (USD)", money(credit["headroom"])),
            ("Group headroom after London (USD)", money(group_headroom)),
        ],
        CF,
        {
            **credit,
            "group_limit": GROUP_LIMIT,
            "london_exposure": LONDON_EXPOSURE,
            "group_headroom": group_headroom,
            "available": sub_headroom,
        },
    )
    d.mail(
        "credit-memo",
        n("credit_analyst"),
        n("marketer"),
        f"Approved: a USD {money(credit['limit'])} sub-limit for BP Australia inside the "
        f"USD {money(GROUP_LIMIT)} group limit. London already uses USD {money(LONDON_EXPOSURE)}, so group headroom is "
        f"USD {money(group_headroom)} and Sydney can use up to USD {money(sub_headroom)}.",
        memo,
        reply="credit-request",
        days=1,
        tags=["credit", "group-limit"],
        doc=CF,
        facts={
            "sub_limit": credit["limit"],
            "group_limit": GROUP_LIMIT,
            "london_exposure": LONDON_EXPOSURE,
            "available": sub_headroom,
        },
    )
    d.mail(
        "limit-letter",
        n("credit_analyst"),
        au("credit_analyst"),
        "Our credit advice is attached.",
        limit_letter(
            d, OURS, BP_AU, credit, d.today(), f"The limit sits inside the group limit of USD {money(GROUP_LIMIT)}."
        ),
        subject="Credit limit advice",
        days=1,
        tags=["credit"],
        doc=CF,
        facts={"limit": credit["limit"]},
    )
    accession = form(
        "bp-accession-letter.pdf",
        "accession-letter",
        "Accession letter",
        [
            ("Existing agreement", agreement),
            ("Acceding party", me["name"]),
            ("Counterparty", cp["name"]),
            ("Terms apply as signed", "Yes, with no change to payment, tolerance or governing law"),
        ],
        CONTRACT,
        {
            "agreement": agreement,
            "acceding": OURS,
            "counterparty": BP_AU,
            "terms": {k: dict(v) for k, v in LONDON_AGREEMENT.items()},
        },
    )
    d.mail(
        "accession-draft",
        n("lawyer"),
        lo("lawyer"),
        "Please prepare an accession letter that brings Sydney under London's BP agreement without changing its terms.",
        subject="BP: accession letter",
        days=1,
        tags=["legal", "accession"],
        doc=CONTRACT,
        facts={"agreement": agreement},
    )
    d.mail(
        "accession-send",
        lo("lawyer"),
        bp("lawyer"),
        "The accession letter is attached. Please countersign it and return it.",
        accession,
        subject="BP: accession letter for countersignature",
        days=1,
        tags=["legal", "accession"],
        doc=CONTRACT,
    )
    countersigned = form(
        "bp-accession-letter-countersigned.pdf",
        "accession-letter",
        "Accession letter (countersigned)",
        [
            ("Existing agreement", agreement),
            ("Acceding party", me["name"]),
            ("Countersigned by", parent["name"]),
            ("Date", d.today().isoformat()),
        ],
        CONTRACT,
        {"agreement": agreement, "acceding": OURS, "countersigned": True},
    )
    d.mail(
        "accession-signed",
        bp("lawyer"),
        [n("lawyer"), lo("lawyer")],
        "The countersigned accession letter is attached.",
        countersigned,
        reply="accession-send",
        days=2,
        tags=["legal", "accession", "agreed"],
        doc=CONTRACT,
        facts={"countersigned": True},
    )
    acct = [a for a in me["accounts"] if u["accounts"][a]["currency"] == "USD"][:1]
    ssi = ssi_letter(d, OURS, acct)
    d.mail(
        "ssi-send",
        n("settlements"),
        au("settlements"),
        "Our USD settlement instructions are attached.",
        ssi,
        subject="Standard settlement instructions",
        days=1,
        tags=["static-data", "ssi"],
        doc=cls("Account", "fibo"),
        facts={"account": acct[0]},
    )
    cp_acct = u["accounts"][cp["accounts"][1]]
    d.mail(
        "ssi-confirm",
        au("settlements"),
        n("settlements"),
        f"Loaded. Ours is the {d.org(cp_acct['bank'])['name']} USD account ending {cp_acct['number'][-4:]}.",
        reply="ssi-send",
        days=1,
        tags=["static-data", "ssi"],
    )
    effective = d.today()
    d.mail(
        "go-live",
        n("credit_analyst"),
        [n("ops"), n("marketer")],
        f"BP Australia is open: sub-limit USD {money(credit['limit'])} inside the group limit.",
        subject="BP Australia live for trading",
        days=1,
        tags=["go-live"],
        facts={"effective": effective.isoformat(), "sub_limit": credit["limit"]},
    )

    sc.ownership_edges = [e for e in u["ownership_edges"] if e["child"] in (BP_AU, PARENT)]
    sc.beneficial_owners = beneficial_owners(u, BP_AU)
    sc.outcome = {
        "result": "approved",
        "sub_limit": credit["limit"],
        "group_limit": GROUP_LIMIT,
        "london_exposure": LONDON_EXPOSURE,
        "agreement_effective": effective.isoformat(),
    }
    onboarded_record(
        sc,
        entity=OURS,
        counterparty=BP_AU,
        agreement=agreement,
        neg=None,
        clauses=LONDON_AGREEMENT,
        credit=credit,
        effective=effective,
        accounts={"newco": acct[0], "counterparty": cp_acct["id"]},
        extra={
            "requested": SUB_LIMIT,
            "currency": "USD",
            "group_limit": GROUP_LIMIT,
            "london_exposure": LONDON_EXPOSURE,
            "group_headroom": group_headroom,
            "available": sub_headroom,
            "parent_counterparty": PARENT,
            "london_entity": LONDON,
        },
    )
    d.question(1, "metadata", "What is NewCo London's open exposure to the BP group?", LONDON_EXPOSURE)
    d.question(
        2,
        "graph_traversal",
        "Which NewCo entity holds the master agreement with BP that Sydney acceded to?",
        d.org(LONDON)["name"],
    )
    d.question(3, "reconciliation", "How old is the good-standing certificate Sydney relied on, in days?", STALE_DAYS)
    return sc
