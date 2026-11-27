# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared builders for the onboarding scenarios and the ``onboarded(u)`` accessor.

``Desk`` wraps a ``Scenario`` so a message is addressed by ``"org-id:role"``. The document builders read
every identifier, name and account from ``universe.json``; amounts that originate in onboarding (limits,
clause values) are set once in a scenario module and printed from that single variable.
"""

# ruff: noqa: E501
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from .. import ids
from ..clauses import KEY_PARAMS, Round, redline_rounds
from ..ontology import cls
from ..scenario import Attachment, Scenario
from ..universe import beneficial_owners
from ..writers.docx_writer import build_docx
from ..writers.pdf_writer import Grid, KeyValues, Text, build_pdf


def money(amount: int) -> str:
    """Format a whole amount with thousands separators."""
    return f"{amount:,}"


def slug(text: str) -> str:
    """Lower-case hyphenated form for file names."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class Desk:
    """Address mailboxes as ``"org-id:role"`` and send messages with a standard greeting and sign-off."""

    def __init__(self, sc: Scenario) -> None:
        self.sc = sc
        self.u = sc.u
        self._by = {(e["org"], e["role"]): a for a, e in sc.u["directory"].items()}

    def addr(self, ref: str) -> str:
        org, role = ref.split(":")
        return self._by[(org, role)]

    def person(self, ref: str) -> dict[str, Any]:
        return self.u["persons"][self.u["directory"][self.addr(ref)]["person"]]

    def org(self, org_id: str) -> dict[str, Any]:
        return self.u["orgs"][org_id]

    def today(self) -> date:
        return self.sc.clock.date()

    def mail(
        self,
        key: str,
        frm: str,
        to: str | Sequence[str],
        text: str,
        *atts: Attachment,
        reply: str | None = None,
        subject: str | None = None,
        days: int = 0,
        tags: Sequence[str] = (),
        facts: Mapping[str, Any] | None = None,
        doc: str | None = None,
        cc: Sequence[str] = (),
        fwd: str | None = None,
        quote: str = "",
    ) -> dict[str, Any]:
        """Send one email. ``text`` is the body between the greeting and the sign-off.

        ``fwd`` makes it a forward of that message key. ``quote`` is appended after the sign-off (quoted history).
        """
        tos = [to] if isinstance(to, str) else list(to)
        first = self.person(tos[0])["name"].split()[0]
        sender = self.person(frm)
        body = f"Hi {first},\n\n{text.strip()}\n\nRegards,\n{sender['name']}\n{self.org(frm.split(':')[0])['name']}\n"
        if quote:
            body += "\n" + quote
        return self.sc.send(
            key,
            self.addr(frm),
            [self.addr(t) for t in tos],
            body,
            subject=subject,
            doc_class=doc,
            attachments=atts,
            reply_to=reply,
            tags=tags,
            facts=facts,
            cc=[self.addr(c) for c in cc],
            after_business_days=days,
            forward_of=fwd,
        )

    def contradiction(
        self,
        ident: str,
        kind: str,
        description: str,
        where: Sequence[str],
        values: Mapping[str, Any],
        caught: bool,
        caught_by: str | None = None,
    ) -> None:
        """Record a planted problem and whether anyone in the corpus noticed it."""
        self.sc.expected_contradictions.append(
            {
                "id": f"{self.sc.id}:{ident}",
                "type": kind,
                "description": description,
                "where": list(where),
                "values": dict(values),
                "caught": caught,
                "caught_by": caught_by,
            }
        )

    def question(self, n: int, qtype: str, text: str, expected: Any, tolerance_pct: float = 0) -> None:
        self.sc.questions.append(
            {
                "id": f"{self.sc.id}-q{n}",
                "type": qtype,
                "question": text,
                "expected": expected,
                "tolerance_pct": tolerance_pct,
            }
        )


# --- generic document ---------------------------------------------------------------------------


def form(
    filename: str,
    kind: str,
    title: str,
    pairs: Sequence[tuple[str, str]],
    class_uri: str,
    facts: Mapping[str, Any],
    *,
    intro: str = "",
    grid: Grid | None = None,
    notes: Sequence[str] = (),
) -> Attachment:
    """A PDF with an optional intro, a label/value block, an optional table and closing notes."""
    blocks: list[Any] = []
    if intro:
        blocks.append(Text(intro))
    blocks.append(KeyValues(tuple((k, str(v)) for k, v in pairs)))
    if grid:
        blocks.append(grid)
    blocks.extend(Text(n) for n in notes)
    return Attachment(filename, build_pdf(title, blocks), kind, class_uri, dict(facts))


# --- KYC documents ------------------------------------------------------------------------------


def _address(o: Mapping[str, Any]) -> str:
    a = o["address"]
    return ", ".join(p for p in (a.get("street"), a.get("city"), a.get("postcode") or None) if p)


def cert_incorporation(d: Desk, org_id: str) -> Attachment:
    o = d.org(org_id)
    inc = o["incorporation"]
    facts = {
        "legal_name": o["name"],
        "registration_number": inc["registration_number"],
        "jurisdiction": inc["jurisdiction"],
        "incorporated": inc["date"],
        "legal_form": o["legal_form"],
    }
    pairs = [
        ("Legal name", o["name"]),
        ("Registration number", inc["registration_number"]),
        ("Jurisdiction", inc["jurisdiction"]),
        ("Date of incorporation", inc["date"]),
        ("Registered office", _address(o)),
    ]
    return form(
        f"{slug(o['name'])}-certificate-of-incorporation.pdf",
        "certificate-of-incorporation",
        f"Certificate of incorporation: {o['name']}",
        pairs,
        cls("LegalEntity", "gleif"),
        facts,
        notes=["This is a certified copy supplied for counterparty onboarding."],
    )


def good_standing(d: Desk, org_id: str, issued: date) -> Attachment:
    o = d.org(org_id)
    facts = {"legal_name": o["name"], "issued": issued.isoformat(), "status": "in good standing", "validity_months": 12}
    pairs = [("Legal name", o["name"]), ("Status", "In good standing"), ("Issued", issued.isoformat())]
    return form(
        f"{slug(o['name'])}-good-standing-{issued.isoformat()}.pdf",
        "good-standing-certificate",
        f"Certificate of good standing: {o['name']}",
        pairs,
        cls("LegalEntity", "gleif"),
        facts,
        notes=["Valid for 12 months from the date of issue."],
    )


def _name(u: Mapping[str, Any], node: str) -> str:
    return u["orgs"][node]["name"] if node in u["orgs"] else u["persons"][node]["name"]


def ancestry(u: Mapping[str, Any], org_id: str, stop_at: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Ownership edges above ``org_id``; edges above any node in ``stop_at`` are left out."""
    out: list[dict[str, Any]] = []
    frontier, seen = [org_id], set()
    while frontier:
        node = frontier.pop(0)
        for e in u["ownership_edges"]:
            if e["child"] == node and (e["parent"], e["child"]) not in seen:
                seen.add((e["parent"], e["child"]))
                out.append(e)
                if e["parent"] not in stop_at:
                    frontier.append(e["parent"])
    return out


def ownership_chart(d: Desk, org_id: str, stop_at: Sequence[str] = (), note: str = "") -> Attachment:
    """The ownership chart a counterparty supplies. ``stop_at`` names nodes whose own owners it leaves out."""
    o = d.org(org_id)
    shown = ancestry(d.u, org_id, stop_at)
    rows = tuple((_name(d.u, e["parent"]), _name(d.u, e["child"]), f"{e['percent']:g}%") for e in shown)
    declared = beneficial_owners({"ownership_edges": shown, "persons": d.u["persons"]}, org_id)
    facts = {
        "entity": o["name"],
        "edges": [
            {"parent": e["parent"], "child": e["child"], "percent": e["percent"], "class_uri": e["class_uri"]}
            for e in shown
        ],
        "declared_beneficial_owners": {_name(d.u, p): pct for p, pct in declared.items()},
        "complete": not stop_at,
    }
    return form(
        f"{slug(o['name'])}-ownership-chart.pdf",
        "ownership-chart",
        f"Ownership chart: {o['name']}",
        [
            ("Entity", o["name"]),
            ("Beneficial owners at 25% or more", ", ".join(facts["declared_beneficial_owners"]) or "none declared"),
        ],  # noqa: E501
        cls("LegalEntity", "gleif"),
        facts,
        grid=Grid(("Owner", "Owned entity", "Share"), rows),
        notes=[note] if note else [],
    )


def lei_record(d: Desk, org_id: str) -> Attachment:
    o = d.org(org_id)
    renewal = (d.today() + timedelta(days=200)).isoformat()
    facts = {"lei": o["lei"], "legal_name": o["name"], "status": "ACTIVE", "next_renewal": renewal}
    pairs = [("LEI", o["lei"]), ("Legal name", o["name"]), ("Registration status", "ACTIVE"), ("Next renewal", renewal)]
    return form(
        f"{slug(o['name'])}-lei-record.pdf",
        "lei-record",
        f"LEI record: {o['name']}",
        pairs,
        cls("LEIRegistration"),
        facts,
    )


def signatory_list(d: Desk, org_id: str) -> Attachment:
    o = d.org(org_id)
    people = [
        d.u["persons"][p] for p in o["people"] if d.u["persons"][p]["role"] in ("trader", "lawyer", "settlements")
    ]
    facts = {"entity": o["name"], "signatories": [{"name": p["name"], "role": p["role"]} for p in people]}
    return form(
        f"{slug(o['name'])}-authorised-signatories.pdf",
        "signatory-list",
        f"Authorised signatories: {o['name']}",
        [("Entity", o["name"])],
        cls("PartyInRole"),
        facts,
        grid=Grid(("Name", "Role"), tuple((p["name"], p["role"].replace("_", " ")) for p in people)),
    )


def financial_data(d: Desk, org_id: str) -> dict[str, int]:
    """Deterministic financial summary for ``org_id``; the figures print on the statements and feed the credit memo."""
    r = ids.rng_for(d.u["seed"], "fin:" + org_id)
    return {
        "fiscal_year": 2025,
        "revenue": r.randint(800, 4000) * 1_000_000,
        "net_worth": r.randint(150, 900) * 1_000_000,
    }


def financial_statements(d: Desk, org_id: str, fin: Mapping[str, int]) -> Attachment:
    o = d.org(org_id)
    pairs = [
        ("Entity", o["name"]),
        ("Fiscal year", str(fin["fiscal_year"])),
        ("Revenue (USD)", money(fin["revenue"])),
        ("Net worth (USD)", money(fin["net_worth"])),
    ]
    return form(
        f"{slug(o['name'])}-financial-statements-{fin['fiscal_year']}.pdf",
        "financial-statements",
        f"Audited financial summary: {o['name']}",
        pairs,
        cls("MonetaryAmount"),
        dict(fin) | {"entity": o["name"]},
    )


def tax_form(d: Desk, org_id: str) -> Attachment:
    o = d.org(org_id)
    tax = o["tax_id"] or o["incorporation"]["registration_number"]
    facts = {"entity": o["name"], "tax_id": tax}
    return form(
        f"{slug(o['name'])}-tax-form.pdf",
        "tax-form",
        f"Tax identification: {o['name']}",
        [("Entity", o["name"]), ("Tax identifier", tax), ("Address", _address(o))],
        cls("LegalEntity", "gleif"),
        facts,
    )


def questionnaire(d: Desk, org_id: str, completed_by: str, answers: Sequence[tuple[str, str]]) -> Attachment:
    o = d.org(org_id)
    facts = {"entity": o["name"], "completed_by": completed_by, "answers": dict(answers)}
    return form(
        f"{slug(o['name'])}-kyc-questionnaire.pdf",
        "kyc-questionnaire",
        f"KYC questionnaire: {o['name']}",
        [("Entity", o["name"]), ("Completed by", completed_by)],
        cls("LegalEntity", "gleif"),
        facts,
        grid=Grid(("Question", "Answer"), tuple(answers)),
    )


def screening_report(d: Desk, org_id: str, subjects: Sequence[str], hits: Sequence[Mapping[str, Any]]) -> Attachment:
    o = d.org(org_id)
    result = "hit" if hits else "clear"
    facts = {"entity": o["name"], "subjects": list(subjects), "hits": [dict(h) for h in hits], "result": result}
    rows = tuple((s, next((h["finding"] for h in hits if h["subject"] == s), "no match")) for s in subjects)
    return form(
        f"{slug(o['name'])}-screening-report.pdf",
        "screening-report",
        f"Screening report: {o['name']}",
        [("Entity", o["name"]), ("Lists", "sanctions, politically exposed persons, adverse media"), ("Result", result)],
        cls("Service"),
        facts,
        grid=Grid(("Subject screened", "Finding"), rows),
    )


def ssi_letter(d: Desk, owner_id: str, account_ids: Sequence[str]) -> Attachment:
    o = d.org(owner_id)
    accts = [d.u["accounts"][a] for a in account_ids]
    rows = tuple(
        (
            a["currency"],
            d.org(a["bank"])["name"],
            a["number"],
            a["bic"],
            a["iban"] or a["aba"] or a["bsb"] or "",
        )
        for a in accts
    )
    facts = {
        "entity": o["name"],
        "accounts": [
            {k: a[k] for k in ("id", "currency", "number", "bic", "iban", "aba", "bsb")} | {"bank": a["bank"]}
            for a in accts
        ],
    }
    return form(
        f"{slug(o['name'])}-settlement-instructions.pdf",
        "ssi-letter",
        f"Standard settlement instructions: {o['name']}",
        [("Account holder", o["name"])],
        cls("Account", "fibo"),
        facts,
        grid=Grid(("Currency", "Bank", "Account", "BIC", "IBAN / ABA / BSB"), rows),
    )


def credit_block(requested: int, unsecured: int, guaranteed: int, sblc: int, exposure: int) -> dict[str, Any]:
    """Credit arithmetic shared by memo, letter and ``onboarded``: limit is the sum of tiers, headroom is limit minus exposure."""
    limit = unsecured + guaranteed + sblc
    return {
        "requested": requested,
        "unsecured": unsecured,
        "guaranteed": guaranteed,
        "sblc": sblc,
        "limit": limit,
        "exposure": exposure,
        "headroom": limit - exposure,
    }


def credit_memo(d: Desk, ours: str, cp: str, credit: Mapping[str, Any], extra: Mapping[str, Any]) -> Attachment:
    o, c = d.org(ours), d.org(cp)
    pairs = [
        ("Counterparty", c["name"]),
        ("Booking entity", o["name"]),
        ("Requested limit (USD)", money(credit["requested"])),
        ("Approved limit (USD)", money(credit["limit"])),
        ("Current exposure (USD)", money(credit["exposure"])),
        ("Headroom (USD)", money(credit["headroom"])),
    ]
    grid = Grid(
        ("Tier", "USD"),
        (
            ("Unsecured", money(credit["unsecured"])),
            ("Parent guarantee", money(credit["guaranteed"])),
            ("Confirmed SBLC", money(credit["sblc"])),
        ),
    )
    facts = {"counterparty": c["name"], "booking_entity": o["name"], **credit, **extra}
    notes = [f"Condition: {x}" for x in extra.get("conditions", ())]
    return form(
        f"{slug(c['name'])}-credit-memo.pdf",
        "credit-memo",
        f"Credit approval memo: {c['name']}",
        pairs,
        cls("CreditFacility"),
        facts,
        grid=grid,
        notes=notes,
    )


def limit_letter(d: Desk, ours: str, cp: str, credit: Mapping[str, Any], effective: date, tail: str = "") -> Attachment:
    o, c = d.org(ours), d.org(cp)
    pairs = [
        ("To", c["name"]),
        ("From", o["name"]),
        ("Approved limit (USD)", money(credit["limit"])),
        ("Effective", effective.isoformat()),
    ]
    facts = {
        "counterparty": c["name"],
        "booking_entity": o["name"],
        "limit": credit["limit"],
        "effective": effective.isoformat(),
    }
    return form(
        f"{slug(c['name'])}-credit-limit-letter.pdf",
        "credit-limit-letter",
        f"Credit limit advice: {c['name']}",
        pairs,
        cls("CreditFacility"),
        facts,
        notes=[tail] if tail else [],
    )


def bureau_score(d: Desk, org_id: str) -> int:
    return ids.rng_for(d.u["seed"], "bureau:" + org_id).randint(610, 790)


# --- negotiated agreements ----------------------------------------------------------------------


class Negotiation:
    """A master agreement negotiated through tracked-change rounds, one DOCX per round."""

    def __init__(self, d: Desk, title: str, base: Mapping[str, Mapping[str, Any]], class_uri: str) -> None:
        self.d = d
        self.title = title
        self.base = {k: dict(v) for k, v in base.items()}
        self.class_uri = class_uri
        self.rounds: list[Round] = []
        self.authors: list[str] = []  # org id of each round's author
        self.msgs: list[str] = []
        self.draft_msg: str | None = None
        self.draft_by: str | None = None
        self.result = redline_rounds(title, self.base, [])

    def round(
        self, author_ref: str, changes: Mapping[str, Mapping[str, Any]], msg_key: str, filename: str
    ) -> Attachment:
        d = self.d
        d.sc.wait_business_days(d.addr(author_ref), 0)
        person = d.person(author_ref)
        self.rounds.append(Round(person["name"], d.sc.clock, changes))
        self.authors.append(author_ref.split(":")[0])
        self.msgs.append(msg_key)
        self.result = redline_rounds(self.title, self.base, self.rounds)
        n = len(self.rounds)
        data = build_docx(f"{self.title}, round {n}", self.result.rounds[-1], author=person["name"], created=d.sc.clock)
        tracked = sum(1 for p in self.result.rounds[-1] for r in p.runs if r.kind != "text")
        facts = {
            "round": n,
            "author": person["name"],
            "changes": {cid: dict(p) for cid, p in changes.items()},
            "tracked_changes": tracked,
            "values_after": {cid: dict(p) for cid, p in self.result.final_values.items()},
        }
        return Attachment(filename, data, "agreement-redline", self.class_uri, facts)

    @property
    def values(self) -> dict[str, dict[str, Any]]:
        return self.result.final_values

    def value(self, clause: str, param: str | None = None) -> Any:
        return self.values[clause][param or KEY_PARAMS[clause]]

    def record_positions(self, draft_msg: str, draft_org: str) -> None:
        """Write every clause's history into ``positions``: v0 is the draft, the last change per clause is agreed."""
        sc = self.d.sc
        out: dict[str, list[dict[str, Any]]] = {}
        for cid, hist in self.result.history.items():
            key = KEY_PARAMS[cid]
            for version, params in hist:
                value = params[key]
                if version and value == out[cid][-1]["value"]:
                    continue
                proposer = draft_org if version == 0 else self.authors[version - 1]
                message = draft_msg if version == 0 else self.msgs[version - 1]
                out.setdefault(cid, []).append(
                    {"clause": cid, "value": value, "proposer": proposer, "version": version, "message": message}
                )
        for rows in out.values():
            for i, row in enumerate(rows):
                sc.add_position(
                    row["clause"],
                    row["value"],
                    row["proposer"],
                    row["version"],
                    row["message"],
                    "agreed" if i == len(rows) - 1 else "superseded",
                )


# --- the onboarded() accessor -------------------------------------------------------------------


def onboarded_record(
    sc: Scenario,
    *,
    entity: str,
    counterparty: str,
    agreement: str,
    neg: Negotiation | None,
    credit: Mapping[str, Any],
    effective: date,
    accounts: Mapping[str, str],
    extra: Mapping[str, Any] | None = None,
    clauses: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Store and return what later phases read: agreement name, clause values, credit, dates and accounts."""
    vals = {cid: dict(p) for cid, p in (neg.values if neg else clauses or {}).items()}
    record: dict[str, Any] = {
        "scenario": sc.id,
        "entity": entity,
        "counterparty": counterparty,
        "agreement": agreement,
        "clauses": vals,
        "payment_days": vals.get("payment", {}).get("days"),
        "tolerance_pct": vals.get("quantity_tolerance", {}).get("tolerance"),
        "governing_law": vals.get("governing_law", {}).get("law"),
        "credit": dict(credit),
        "effective": effective.isoformat(),
        "accounts": dict(accounts),
        **(extra or {}),
    }
    sc.data["onboarded"] = record
    return record


def onboarded(u: Mapping[str, Any], upstream: Mapping[str, Scenario] | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{scenario id: onboarded record}``, reading ``upstream`` or building onboarding in memory."""
    if upstream is None:
        from . import ONBOARDING

        upstream = {}
        for builder in ONBOARDING:
            sc = builder(dict(u), upstream)
            upstream = {**upstream, sc.id: sc}
    return {sid: sc.data["onboarded"] for sid, sc in upstream.items() if "onboarded" in sc.data}


def contract_title(name: str, ours: Mapping[str, Any], theirs: Mapping[str, Any]) -> str:
    """Agreement name used in documents and in ``onboarded``: ``<form> between <ours> and <theirs>``."""
    return f"{name} between {ours['name']} and {theirs['name']}"
