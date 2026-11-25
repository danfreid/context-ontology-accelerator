# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""s6: NewCo London and Tiber Peak. A Cypriot holding hides a sanctioned 60% owner; neutral decline, group-wide block."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..ontology import cls
from ..scenario import Scenario
from ..universe import beneficial_owners
from .common import (
    Desk,
    cert_incorporation,
    credit_block,
    form,
    money,
    onboarded_record,
    ownership_chart,
    questionnaire,
    screening_report,
)

SCENARIO_ID = "s6-london-tiberpeak-decline"
EXPECT = {"emails": 14, "attachments": 7, "chats": 0}
OURS, THEIRS = "newco-lon", "cp-tiberpeak"
HOLDING, NOMINEE, FOUNDATION = "hold-tiber-cy", "hold-tiber-nominee", "hold-tiber-foundation"
HIDDEN, DECLARED = "own-tiber-hidden", "own-tiber-declared"
REQUESTED = 5_000_000
OTHER_DESKS = ("newco-syd", "newco-hou", "newco-nyc")


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    sc = Scenario(
        SCENARIO_ID,
        "onboarding",
        u,
        datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
        expect=EXPECT,
        title="London declines Tiber Peak: hidden sanctioned owner, neutral decline, group-wide block",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def t(role: str) -> str:
        return f"{THEIRS}:{role}"

    cp = d.org(THEIRS)
    LE, CF = cls("LegalEntity", "gleif"), cls("CreditFacility")
    hidden, declared = u["persons"][HIDDEN]["name"], u["persons"][DECLARED]["name"]
    hidden_pct = next(e["percent"] for e in u["ownership_edges"] if e["parent"] == NOMINEE)
    effective_hidden = beneficial_owners(u, THEIRS)[HIDDEN]

    d.mail(
        "intro",
        n("marketer"),
        t("marketer"),
        f"We would like to start buying from Tiber Peak. USD {money(REQUESTED)} of credit is "
        "what we have in mind to begin with.",
        subject="NewCo London and Tiber Peak: supply relationship",
        tags=["intro"],
    )
    d.mail(
        "intro-reply",
        t("marketer"),
        n("marketer"),
        "Glad to proceed. Our compliance contact will send the documents.",
        reply="intro",
        days=1,
        tags=["intro"],
    )
    d.mail(
        "kyc-request",
        n("compliance_analyst"),
        t("compliance_analyst"),
        "Please send your certificate of incorporation, an ownership chart down to the natural persons, and the completed "
        "questionnaire.",
        subject="KYC documents for Tiber Peak Trading DMCC",
        days=1,
        tags=["kyc"],
        doc=LE,
    )
    chart = ownership_chart(d, THEIRS, stop_at=(NOMINEE,), note="Aurelian Nominees Ltd holds its shares as nominee.")
    d.mail(
        "kyc-docs",
        t("compliance_analyst"),
        n("compliance_analyst"),
        "The certificate of incorporation and the ownership chart are attached.",
        cert_incorporation(d, THEIRS),
        chart,
        reply="kyc-request",
        days=1,
        tags=["kyc", "ownership", "incomplete-chart"],
        doc=LE,
        facts={"declared_beneficial_owners": chart.facts["declared_beneficial_owners"], "chart_complete": False},
    )
    answers = (
        ("Does any owner other than the declared shareholder hold 25% or more?", "No"),
        ("Is the entity or any owner subject to sanctions?", "No"),
        ("Is any shareholder a nominee?", "Yes, Aurelian Nominees Ltd, for an investor who prefers not to be named"),
    )
    d.mail(
        "questionnaire",
        t("compliance_analyst"),
        n("compliance_analyst"),
        "Our questionnaire is attached.",
        questionnaire(d, THEIRS, d.person(t("compliance_analyst"))["name"], answers),
        reply="kyc-docs",
        days=1,
        tags=["kyc", "questionnaire", "concealed-owner"],
        doc=LE,
        facts={"owner_over_25_other_than_declared": False},
    )
    d.mail(
        "screen-request",
        n("compliance_analyst"),
        "svc-veridian:analyst",
        "Please screen Tiber Peak, the holding, the nominee and everyone behind the nominee, down to the natural persons.",
        subject="Screening request: Tiber Peak",
        days=1,
        tags=["screening"],
    )
    hit = {
        "subject": hidden,
        "finding": f"sanctions match; ultimate owner of {u['orgs'][NOMINEE]['name']} through "
        f"{u['orgs'][FOUNDATION]['name']}",
        "category": "sanctions",
    }
    d.mail(
        "screen-result",
        "svc-veridian:analyst",
        n("compliance_analyst"),
        f"One match. {hidden} is on a sanctions list and owns {u['orgs'][FOUNDATION]['name']}, which holds the nominee. The report is attached.",
        screening_report(
            d,
            THEIRS,
            [
                cp["name"],
                u["orgs"][HOLDING]["name"],
                u["orgs"][NOMINEE]["name"],
                u["orgs"][FOUNDATION]["name"],
                declared,
                hidden,
            ],
            [hit],
        ),
        reply="screen-request",
        days=2,
        tags=["screening", "sanctions"],
        facts={"result": "hit", "hidden_owner": HIDDEN},
    )
    d.contradiction(
        "hidden-sanctioned-owner",
        "ownership-concealment",
        f"The chart and questionnaire show no owner above 25% except {declared}; screening finds {hidden} owning {hidden_pct:g}% of "
        f"the holding through a foundation and a nominee ({effective_hidden:g}% effective).",
        ["kyc-docs", "questionnaire", "screen-result"],
        {
            "declared": chart.facts["declared_beneficial_owners"],
            "hidden_owner": hidden,
            "effective_percent": effective_hidden,
            "hops": 4,
        },
        True,
        "decline-memo",
    )
    credit = credit_block(REQUESTED, 0, 0, 0, 0)
    memo = form(
        "tiberpeak-compliance-decision.pdf",
        "compliance-decision",
        "Compliance decision: Tiber Peak Trading DMCC",
        [
            ("Counterparty", cp["name"]),
            ("Decision", "Decline; block across the group"),
            ("Reason", f"Sanctioned ultimate owner behind {u['orgs'][NOMINEE]['name']}"),
            ("Disclosure to counterparty", "None"),
        ],
        cls("Service"),
        {"decision": "decline", "group_block": True, "hidden_owner": HIDDEN, "disclose_reason": False},
        notes=["Do not disclose the reason to the counterparty."],
    )
    d.mail(
        "decline-memo",
        n("compliance_analyst"),
        [n("credit_analyst"), n("lawyer")],
        "Decision: decline Tiber Peak and block it across the group. The reason is internal only; nothing about screening or "
        "owners goes to the counterparty. The memo is attached.",
        memo,
        subject="Tiber Peak: decline and group block",
        days=1,
        tags=["decline", "contradiction-caught"],
        facts={"decision": "decline"},
    )
    letter = form(
        "tiberpeak-decline-letter.pdf",
        "decline-letter",
        "Counterparty decision",
        [("To", cp["name"]), ("Decision", "We are not able to proceed with a trading relationship at this time")],
        cls("Agreement"),
        {"decision": "decline", "reason_disclosed": False},
    )
    d.mail(
        "decline-letter",
        n("marketer"),
        t("marketer"),
        "We are not able to proceed with a trading relationship at this time. "
        "Thank you for your interest. Our letter is attached.",
        letter,
        subject="NewCo London and Tiber Peak: decision",
        days=1,
        tags=["decline", "neutral"],
        facts={"reason_disclosed": False},
    )
    d.mail(
        "reason-query",
        t("marketer"),
        n("marketer"),
        "Can you tell us what the issue was, so we can fix it?",
        reply="decline-letter",
        days=1,
        tags=["decline"],
    )
    d.mail(
        "reason-reply",
        n("marketer"),
        t("marketer"),
        "We cannot go into the detail of the decision. Thank you for understanding.",
        reply="reason-query",
        days=1,
        tags=["decline", "neutral"],
        facts={"reason_disclosed": False},
    )
    block = form(
        "tiberpeak-group-block.pdf",
        "block-notice",
        "Group-wide counterparty block",
        [
            ("Counterparty", cp["name"]),
            ("LEI", cp["lei"]),
            ("Related entities", ", ".join(u["orgs"][o]["name"] for o in (HOLDING, NOMINEE))),
            ("Action", "No onboarding, trades or payments at any NewCo entity"),
        ],
        cls("LegalEntity", "gleif"),
        {
            "counterparty": THEIRS,
            "lei": cp["lei"],
            "blocked_entities": [HOLDING, NOMINEE, FOUNDATION],
            "scope": list(OTHER_DESKS) + [OURS],
        },
    )
    d.mail(
        "group-block",
        n("compliance_analyst"),
        [f"{o}:compliance_analyst" for o in OTHER_DESKS],
        "Tiber Peak Trading DMCC and its holding companies are blocked across NewCo. Do not onboard, trade or pay. Notice attached.",
        block,
        subject="Group-wide block: Tiber Peak",
        days=1,
        tags=["group-block"],
        doc=LE,
        facts={"blocked": True},
    )
    d.mail(
        "block-ack",
        f"{OTHER_DESKS[1]}:compliance_analyst",
        n("compliance_analyst"),
        "Block applied in the Houston counterparty list.",
        reply="group-block",
        days=1,
        tags=["group-block"],
    )
    d.mail(
        "static-block",
        n("credit_analyst"),
        n("ops"),
        "Tiber Peak is blocked. Set its limit to zero and mark the static data closed.",
        subject="Tiber Peak: static data closed",
        days=1,
        tags=["group-block", "static-data"],
        doc=CF,
        facts={"limit": 0, "status": "closed"},
    )

    sc.ownership_edges = [e for e in u["ownership_edges"] if e["child"] in (THEIRS, HOLDING, NOMINEE, FOUNDATION)]
    sc.beneficial_owners = beneficial_owners(u, THEIRS)
    sc.outcome = {"result": "declined", "group_block": True, "reason_disclosed": False, "hidden_owner": HIDDEN}
    sc.review_events = [{"after_message": "screen-result", "event": "confirm-sanctions-match", "person": HIDDEN}]
    onboarded_record(
        sc,
        entity=OURS,
        counterparty=THEIRS,
        agreement="",
        neg=None,
        credit=credit,
        effective=sc.clock.date(),
        accounts={},
        extra={"requested": REQUESTED, "declined": True, "group_block": True},
    )
    d.question(
        1, "graph_traversal", "Who is the ultimate beneficial owner behind Tiber Peak's 60% shareholder?", hidden
    )
    d.question(
        2, "graph_traversal", "How many ownership hops lie between the sanctioned owner and Tiber Peak Trading DMCC?", 4
    )
    d.question(
        3,
        "reconciliation",
        "Did Tiber Peak's questionnaire disclose an owner above 25% other than the declared shareholder?",
        "no",
    )
    return sc
