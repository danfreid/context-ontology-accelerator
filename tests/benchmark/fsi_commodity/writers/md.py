# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render an email as ``.md``: the ingestible copy of the ``.eml`` ground truth."""

from __future__ import annotations

from email.utils import format_datetime

from .eml import EmailSpec, Party


def _fmt(parties: tuple[Party, ...]) -> str:
    return ", ".join(f"{p.name} <{p.address}>" for p in parties)


def build_md(spec: EmailSpec, attachment_paths: dict[str, str] | None = None) -> bytes:
    """Return the Markdown rendering: headers, body, attachment list (relative path and SHA-256).

    ``attachment_paths`` maps an attachment filename to its path relative to the ``.md`` file.
    """
    paths = attachment_paths or {}
    lines = [
        f"# {spec.subject}",
        "",
        f"- **Message-ID:** {spec.message_id}",
        f"- **Date:** {format_datetime(spec.date)}",
        f"- **From:** {spec.sender.name} <{spec.sender.address}>",
        f"- **To:** {_fmt(spec.to)}",
    ]
    if spec.cc:
        lines.append(f"- **Cc:** {_fmt(spec.cc)}")
    if spec.in_reply_to:
        lines.append(f"- **In-Reply-To:** {spec.in_reply_to}")
    if spec.references:
        lines.append(f"- **References:** {' '.join(spec.references)}")
    lines += ["", "## Body", "", spec.body.rstrip("\n"), ""]
    if spec.attachments:
        lines += ["## Attachments", ""]
        for att in spec.attachments:
            lines.append(f"- {att.filename} ({paths.get(att.filename, att.filename)}, sha256 {att.sha256})")
        lines.append("")
    return "\n".join(lines).encode("utf-8")
