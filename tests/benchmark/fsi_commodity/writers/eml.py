# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Write an RFC 5322 message as a deterministic ``.eml`` (stdlib ``email``)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.generator import BytesGenerator
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from io import BytesIO

_POLICY = policy.default.clone(linesep="\n")


@dataclass(frozen=True)
class Party:
    """A display name and an address."""

    name: str
    address: str

    def header(self) -> Address:
        """Return the stdlib ``Address`` for this party."""
        user, _, domain = self.address.partition("@")
        return Address(self.name, user, domain)


@dataclass(frozen=True)
class AttachmentFile:
    """An attachment as written on disk and carried in the message."""

    filename: str
    content: bytes
    mimetype: str = "application/octet-stream"

    @property
    def sha256(self) -> str:
        """SHA-256 of the content."""
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class EmailSpec:
    """Everything needed to render one message as ``.eml`` and ``.md``."""

    message_id: str
    date: datetime
    sender: Party
    to: tuple[Party, ...]
    subject: str
    body: str
    cc: tuple[Party, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[AttachmentFile, ...] = field(default_factory=tuple)


def make_message_id(scenario_id: str, key: str, domain: str) -> str:
    """Return ``<sha1(scenario|key)[:20]@domain>``; stable for the same inputs."""
    digest = hashlib.sha1(f"{scenario_id}|{key}".encode(), usedforsecurity=False).hexdigest()[:20]
    return f"<{digest}@{domain}>"


def build_eml(spec: EmailSpec) -> bytes:
    """Serialise ``spec`` to bytes. The MIME boundary is derived from the Message-ID, never random."""
    if spec.date.tzinfo is None:
        raise ValueError("EmailSpec.date must be timezone-aware")
    msg = EmailMessage(policy=_POLICY)
    msg["Message-ID"] = spec.message_id
    msg["Date"] = format_datetime(spec.date)
    msg["From"] = spec.sender.header()
    msg["To"] = [p.header() for p in spec.to]
    if spec.cc:
        msg["Cc"] = [p.header() for p in spec.cc]
    msg["Subject"] = spec.subject
    if spec.in_reply_to:
        msg["In-Reply-To"] = spec.in_reply_to
    if spec.references:
        msg["References"] = " ".join(spec.references)
    msg.set_content(spec.body)
    for att in spec.attachments:
        maintype, _, subtype = att.mimetype.partition("/")
        msg.add_attachment(att.content, maintype=maintype, subtype=subtype, filename=att.filename)
    if spec.attachments:
        boundary = "=_nc_" + hashlib.sha1(spec.message_id.encode(), usedforsecurity=False).hexdigest()[:24]
        msg.set_boundary(boundary)
    out = BytesIO()
    BytesGenerator(out, policy=_POLICY, mangle_from_=False).flatten(msg)
    return out.getvalue()
