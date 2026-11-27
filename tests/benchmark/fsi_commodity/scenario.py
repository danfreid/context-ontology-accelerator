# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``Scenario``: the clock, ``send`` and ``chat`` API, and the per-scenario ground-truth manifest."""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import ids
from .calendars import CALENDARS, add_business_days, is_business_day, local_datetime, next_business_day, working_minutes
from .ontology import cls
from .writers.eml import AttachmentFile, EmailSpec, Party, build_eml, make_message_id
from .writers.manifest import dumps_json, sha256_hex
from .writers.md import build_md

PHASE_DIRS = {
    "onboarding": "onboarding",
    "trade": "trade",
    "post_trade": "post-trade",
    "settlement": "settlement",
    "docsets": "docsets",
    "lifecycle": "lifecycle",
    "execution": "execution",
}

_MIME_FALLBACK = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


@dataclass(frozen=True)
class Attachment:
    """A document to attach: bytes from a writer plus the ground truth about it."""

    filename: str
    content: bytes
    kind: str
    class_uri: str
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatLine:
    """One chat line: who said it, what, the intent, and the terms it states."""

    speaker: str
    text: str
    intent: str
    terms: Mapping[str, Any] = field(default_factory=dict)


def _mimetype(filename: str) -> str:
    suffix = "." + filename.rsplit(".", 1)[-1].lower()
    return _MIME_FALLBACK.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


class Scenario:
    """One scenario: send messages and chats on a business-day clock, then render files and a manifest.

    Args:
        scenario_id: Folder name, for example ``s1-houston-vitol-wti``.
        phase: ``onboarding``, ``trade``, ``post_trade``, ``settlement``, ``docsets``, ``lifecycle`` or ``execution``.
        universe: The dict from ``build_universe``.
        start: Timezone-aware start of the scenario clock.
        seed: The corpus seed; per-message jitter derives from ``seed + crc32(key)``.
        expect: Required ``{"emails", "attachments", "chats"}`` counts; ``check_expect`` enforces them.
        title: Human-readable title for the manifest.
    """

    def __init__(
        self,
        scenario_id: str,
        phase: str,
        universe: Mapping[str, Any],
        start: datetime,
        *,
        seed: int | None = None,
        expect: Mapping[str, int] | None = None,
        title: str = "",
    ) -> None:
        if phase not in PHASE_DIRS:
            raise ValueError(f"unknown phase {phase!r}")
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self.id = scenario_id
        self.phase = phase
        self.u = universe
        self.seed = universe["seed"] if seed is None else seed
        self.clock = start
        self.expect = dict(expect) if expect else None
        self.title = title
        self.messages: list[dict[str, Any]] = []
        self.attachments: list[dict[str, Any]] = []
        self.chats: list[dict[str, Any]] = []
        self._files: dict[str, bytes] = {}
        self._specs: dict[str, EmailSpec] = {}
        # Scenario-level ground truth, filled by scenario modules.
        self.ownership_edges: list[dict[str, Any]] = []
        self.beneficial_owners: dict[str, float] = {}
        self.positions: list[dict[str, Any]] = []
        self.deals: list[dict[str, Any]] = []
        self.expected_contradictions: list[dict[str, Any]] = []
        self.outcome: dict[str, Any] = {}
        self.questions: list[dict[str, Any]] = []
        self.review_events: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {}  # computed values later phases read (limits, prices, due dates, ...)
        # Document sets (docsets phase): logical documents, how files split into them, and the reconciliation.
        self.docsets: list[dict[str, Any]] = []
        # Contract lifecycle (lifecycle phase): clause events, revisions, approvals and the calculations using terms.
        self.lifecycle: dict[str, Any] = {}
        # Trade execution (execution phase): chat dialects, learned interpretations, ETRM rows and breaks.
        self.execution: dict[str, Any] = {}

    # --- clock ------------------------------------------------------------------------------------

    def _calendar_for(self, address: str):
        org = self.u["orgs"][self.u["directory"][address]["org"]]
        return CALENDARS[org["calendar"]]

    def _advance(self, address: str, key: str, business_days: int = 0, min_gap: int = 7) -> datetime:
        """Move the clock forward to a moment inside ``address``'s working hours and return it."""
        cal = self._calendar_for(address)
        r = ids.rng_for(self.seed, f"clock:{self.id}:{key}")
        open_min, close_min = working_minutes(cal)
        jitter = r.randint(0, 29)
        local = self.clock.astimezone(ZoneInfo(cal.tz)) + timedelta(minutes=r.randint(min_gap, 90))
        day = local.date()
        minute = local.hour * 60 + local.minute
        if business_days:
            day, minute = add_business_days(cal, day, business_days), open_min + jitter
        if not is_business_day(cal, day):
            day, minute = next_business_day(cal, day), open_min + jitter
        elif minute < open_min:
            minute = open_min + jitter
        elif minute > close_min:
            day, minute = next_business_day(cal, day + timedelta(days=1)), open_min + jitter
        moment = local_datetime(cal, day, minute)
        if moment <= self.clock:  # business_days landed on the same instant or earlier: step to next day
            moment = local_datetime(cal, next_business_day(cal, day + timedelta(days=1)), open_min + jitter)
        self.clock = moment
        return moment

    def wait_business_days(self, address: str, days: int) -> None:
        """Advance the clock ``days`` business days in the calendar of ``address``'s organisation."""
        self._advance(address, f"wait:{len(self.messages)}:{days}", business_days=days)

    # --- parties ----------------------------------------------------------------------------------

    def _party(self, address: str) -> tuple[Party, dict[str, Any]]:
        try:
            entry = self.u["directory"][address]
        except KeyError:
            raise KeyError(f"{address} is not a mailbox in the universe") from None
        person = self.u["persons"][entry["person"]]
        return Party(person["name"], address), {
            "address": address,
            "name": person["name"],
            "org": entry["org"],
            "role": entry["role"],
        }

    # --- send -------------------------------------------------------------------------------------

    def send(
        self,
        key: str,
        fm: str,
        to: str | Sequence[str],
        body: str,
        *,
        subject: str | None = None,
        doc_class: str | None = None,
        attachments: Sequence[Attachment] = (),
        reply_to: str | None = None,
        tags: Sequence[str] = (),
        facts: Mapping[str, Any] | None = None,
        cc: Sequence[str] = (),
        after_business_days: int = 0,
        forward_of: str | None = None,
    ) -> dict[str, Any]:
        """Record and render one email; returns the message record.

        ``reply_to`` is the key of an earlier message in this scenario: it sets ``In-Reply-To``,
        ``References``, the thread and (unless ``subject`` is given) a ``Re:`` subject. ``forward_of`` does the
        same for a forward (``Fwd:`` subject, ``References`` but no ``In-Reply-To``); the record keeps the key.
        """
        if key in self._specs:
            raise ValueError(f"duplicate message key {key!r} in {self.id}")
        if reply_to and forward_of:
            raise ValueError("a message is a reply or a forward, not both")
        recipients = [to] if isinstance(to, str) else list(to)
        sender_party, sender_info = self._party(fm)
        to_parties = [self._party(a) for a in recipients]
        cc_parties = [self._party(a) for a in cc]
        when = self._advance(fm, key, business_days=after_business_days)

        ref = reply_to or forward_of
        if ref is not None and ref not in self._specs:
            raise KeyError(ref)
        parent = self._specs[ref] if ref else None
        thread = next(m["thread"] for m in self.messages if m["key"] == ref) if ref else key
        if subject is None:
            if parent is None:
                raise ValueError("subject is required for a message that is not a reply")
            if forward_of:
                subject = f"Fwd: {parent.subject}"
            else:
                subject = parent.subject if parent.subject.lower().startswith("re:") else f"Re: {parent.subject}"
        domain = fm.split("@", 1)[1]
        message_id = make_message_id(self.id, key, domain)
        seq = len(self.messages) + 1
        stem = f"{seq:03d}-{key}"
        files = tuple(AttachmentFile(a.filename, a.content, _mimetype(a.filename)) for a in attachments)
        spec = EmailSpec(
            message_id=message_id,
            date=when,
            sender=sender_party,
            to=tuple(p for p, _ in to_parties),
            subject=subject,
            body=body,
            cc=tuple(p for p, _ in cc_parties),
            in_reply_to=parent.message_id if parent and not forward_of else None,
            references=(*parent.references, parent.message_id) if parent else (),
            attachments=files,
        )
        att_paths = {a.filename: f"attachments/{seq:03d}-{a.filename}" for a in attachments}
        self._files[f"messages/{stem}.eml"] = build_eml(spec)
        self._files[f"messages/{stem}.md"] = build_md(spec, {n: "../" + p for n, p in att_paths.items()})
        for att in attachments:
            path = att_paths[att.filename]
            self._files[path] = att.content
            self.attachments.append(
                {
                    "filename": att.filename,
                    "path": path,
                    "message": key,
                    "kind": att.kind,
                    "sha256": sha256_hex(att.content),
                    "size": len(att.content),
                    "facts": dict(att.facts),
                    "class_uri": att.class_uri,
                    "provenance": {
                        "class_uri": cls("Entity", "prov"),
                        "bibliographic_class_uri": cls("BibliographicResource"),
                        "wasDerivedFrom": message_id,
                        "wasAttributedTo": sender_info["address"],
                    },
                }
            )
        sender_newco = sender_info["org"].startswith("newco-")
        if sender_newco and all(i["org"].startswith("newco-") for _, i in to_parties):
            direction = "internal"
        else:
            direction = "outbound" if sender_newco else "inbound"
        record = {
            "key": key,
            "seq": seq,
            "message_id": message_id,
            "thread": thread,
            "timestamp": when.isoformat(),
            "from": sender_info,
            "to": [i for _, i in to_parties],
            "cc": [i for _, i in cc_parties],
            "subject": subject,
            "direction": direction,
            "class_uri": doc_class or cls("Entity", "prov"),
            "provenance": {
                "class_uri": cls("Entity", "prov"),
                "bibliographic_class_uri": cls("BibliographicResource"),
                "wasAttributedTo": sender_info["address"],
            },
            "phase": self.phase,
            "tags": sorted(tags),
            "facts": dict(facts or {}),
            "in_reply_to": reply_to,
            **({"forward_of": forward_of} if forward_of else {}),
            "attachments": [a.filename for a in attachments],
            "files": {"eml": f"messages/{stem}.eml", "md": f"messages/{stem}.md"},
        }
        self.messages.append(record)
        self._specs[key] = spec
        return record

    def body(self, key: str) -> str:
        """The text body of an earlier message (for quoting it in a reply or forward)."""
        return self._specs[key].body

    # --- chat -------------------------------------------------------------------------------------

    def chat(
        self,
        key: str,
        lines: Sequence[ChatLine],
        *,
        deal: str | None = None,
        tags: Sequence[str] = (),
        channel: str = "chat",
        class_uri: str | None = None,
    ) -> dict[str, Any]:
        """Record one chat thread as ``chats/NNN-key.json``; every line carries ``intent`` and ``terms``.

        ``class_uri`` is the class the negotiated deal should resolve to; it defaults to the PROV ``Entity``.
        """
        if not lines:
            raise ValueError("a chat needs at least one line")
        if any(c["key"] == key for c in self.chats):
            raise ValueError(f"duplicate chat key {key!r} in {self.id}")
        seq = len(self.chats) + 1
        r = ids.rng_for(self.seed, f"chat:{self.id}:{key}")
        stamp = self._advance(lines[0].speaker, "chat:" + key)
        out_lines = []
        people: dict[str, dict[str, Any]] = {}
        for i, line in enumerate(lines):
            _, info = self._party(line.speaker)
            people.setdefault(line.speaker, info)
            if i:
                stamp = stamp + timedelta(seconds=r.randint(20, 240))
            out_lines.append(
                {
                    "timestamp": stamp.isoformat(),
                    "speaker": info,
                    "text": line.text,
                    "intent": line.intent,
                    "terms": dict(line.terms),
                }
            )
        self.clock = stamp
        path = f"chats/{seq:03d}-{key}.json"
        record = {
            "key": key,
            "seq": seq,
            "channel": channel,
            "participants": list(people.values()),
            "deal": deal,
            "class_uri": class_uri or cls("Entity", "prov"),
            "tags": sorted(tags),
            "phase": self.phase,
            "file": path,
            "lines": out_lines,
        }
        self.chats.append(record)
        self._files[path] = dumps_json(record)
        return record

    # --- ground truth helpers ---------------------------------------------------------------------

    def add_position(
        self, clause: str, value: Any, proposer: str, version: int, message: str, status: str = "agreed"
    ) -> None:
        """Record one negotiated position (status: ``superseded`` or ``agreed``)."""
        self.positions.append(
            {
                "clause": clause,
                "value": value,
                "proposer": proposer,
                "version": version,
                "message": message,
                "status": status,
            }
        )

    # --- output -----------------------------------------------------------------------------------

    @property
    def counts(self) -> dict[str, int]:
        """Emails, attachments and chats recorded so far."""
        return {"emails": len(self.messages), "attachments": len(self.attachments), "chats": len(self.chats)}

    def check_expect(self) -> None:
        """Raise ``ValueError`` when the counts differ from ``expect``."""
        if self.expect is not None and self.counts != self.expect:
            raise ValueError(f"{self.id}: built {self.counts}, expected {self.expect}")

    def manifest(self) -> dict[str, Any]:
        """Return the per-scenario ground truth. ``docsets`` is present only for scenarios that record one."""
        extra: dict[str, Any] = {"docsets": self.docsets} if self.docsets else {}
        if self.lifecycle:
            extra["lifecycle"] = self.lifecycle
        if self.execution:
            extra["execution"] = self.execution
        return {
            "scenario": self.id,
            "phase": self.phase,
            "title": self.title,
            "counts": self.counts,
            "messages": self.messages,
            "attachments": self.attachments,
            "chats": self.chats,
            "ownership_edges": self.ownership_edges,
            "beneficial_owners": self.beneficial_owners,
            "positions": self.positions,
            "deals": self.deals,
            "expected_contradictions": self.expected_contradictions,
            "outcome": self.outcome,
            "questions": self.questions,
            "review_events": self.review_events,
            **extra,
        }

    def files(self) -> dict[str, bytes]:
        """Return every file of this scenario, keyed by path relative to the scenario folder."""
        out = dict(self._files)
        out["manifest.json"] = dumps_json(self.manifest())
        return out
