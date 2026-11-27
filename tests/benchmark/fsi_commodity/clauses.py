# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Clause library in our own wording, and tracked-change redline rounds.

No published form text (GTC, EEI, NAESB, ISDA) is copied. A redline round shows the previous agreed
text with word-level insertions and deletions, as a lawyer's mark-up would.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .writers.docx_writer import Para, Run, deleted, inserted

# clause id -> (title, template)
CLAUSES: dict[str, tuple[str, str]] = {
    "payment": (
        "Payment",
        "The Buyer pays each invoice in full within {days} days after it receives the invoice, in {currency}, "
        "to the account the Seller names on that invoice.",
    ),
    "quantity_tolerance": (
        "Quantity tolerance",
        "The Seller may deliver up to {tolerance}% more or less than the contract quantity. Every barrel or tonne "
        "delivered is priced at the contract price.",
    ),
    "quantity_basis": (
        "Quantity basis",
        "The invoiced quantity is the {basis} quantity certified by the independent inspector.",
    ),
    "no_deductions": (
        "No deductions",
        "Each payment is made without set-off, counterclaim or deduction, including bank charges, except {exception}.",
    ),
    "late_interest": (
        "Late payment",
        "An overdue amount bears interest at {rate}% a year above the Seller's cost of funds until it is paid.",
    ),
    "credit_support": (
        "Credit support",
        "The Buyer keeps {support} in place for the full term of any open transaction.",
    ),
    "termination": (
        "Termination",
        "Either party may end this agreement on {notice} days' written notice. Open transactions run to completion.",
    ),
    "governing_law": (
        "Governing law",
        "This agreement and any dispute arising from it are governed by the law of {law}. The courts of {venue} "
        "have jurisdiction.",
    ),
    "netting": (
        "Close-out netting",
        "On an early termination, every transaction under this agreement is valued and netted into {scope}.",
    ),
    "cross_default": (
        "Cross default",
        "A default on borrowed money above USD {threshold:,}, by either party or its guarantor, is an event of "
        "default under this agreement.",
    ),
    "threshold": (
        "Threshold",
        "Each party's threshold is USD {amount:,}. Credit support is due only for exposure above that amount.",
    ),
    "min_transfer": (
        "Minimum transfer amount",
        "No transfer of credit support is made unless it is at least USD {amount:,}.",
    ),
    "eligible_collateral": (
        "Eligible collateral",
        "A party may post {assets}, valued after the haircut this agreement sets for each asset.",
    ),
    "valuation": (
        "Valuation",
        "Exposure is valued each business day at {time}, using the calculation agent's mid-market prices.",
    ),
    "term": (
        "Term and renewal",
        "This agreement runs for {months} months from the date it is signed by both parties and renews for "
        "further periods of {months} months unless a party gives notice at least {notice} days before a term ends.",
    ),
}


# clause id -> the parameter that carries its negotiated value (used for position history)
KEY_PARAMS: dict[str, str] = {
    "payment": "days",
    "quantity_tolerance": "tolerance",
    "quantity_basis": "basis",
    "no_deductions": "exception",
    "late_interest": "rate",
    "credit_support": "support",
    "termination": "notice",
    "governing_law": "law",
    "netting": "scope",
    "cross_default": "threshold",
    "threshold": "amount",
    "min_transfer": "amount",
    "eligible_collateral": "assets",
    "valuation": "time",
    "term": "notice",
}


def render(clause_id: str, params: Mapping[str, Any]) -> str:
    """Return the clause body with ``params`` filled in. Raises ``KeyError`` on a missing parameter."""
    return CLAUSES[clause_id][1].format(**params)


@dataclass(frozen=True)
class Round:
    """One negotiation round: who marked it up, when, and the parameter changes they propose."""

    author: str
    when: datetime
    changes: Mapping[str, Mapping[str, Any]]
    note: str = ""


@dataclass
class RedlineResult:
    """Paragraphs per round (with tracked changes) and the clause values after the last round."""

    rounds: list[list[Para]] = field(default_factory=list)
    final_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    # clause_id -> list of (round index, params after that round), for the manifest's position history
    history: dict[str, list[tuple[int, dict[str, Any]]]] = field(default_factory=dict)


_TOKEN = re.compile(r"\s+|[^\s]+")


def redline(old: str, new: str, author: str, when: datetime) -> tuple[Run, ...]:
    """Return runs showing ``old`` -> ``new`` as word-level tracked deletions and insertions."""
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    runs: list[Run] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            runs.append(Run("".join(a[i1:i2])))
            continue
        if op in ("delete", "replace"):
            runs.append(deleted("".join(a[i1:i2]), author, when))
        if op in ("insert", "replace"):
            runs.append(inserted("".join(b[j1:j2]), author, when))
    return tuple(runs)


def redline_rounds(title: str, base: Mapping[str, Mapping[str, Any]], rounds: Sequence[Round]) -> RedlineResult:
    """Apply ``rounds`` in order to ``base`` (clause id -> params), producing one marked-up document per round."""
    values = {cid: dict(params) for cid, params in base.items()}
    result = RedlineResult(history={cid: [(0, dict(p))] for cid, p in values.items()})
    for index, rnd in enumerate(rounds, start=1):
        paras = [Para((Run(f"{title}, round {index}"),), heading=True)]
        for cid, params in values.items():
            old_text = render(cid, params)
            if cid in rnd.changes:
                params = {**params, **rnd.changes[cid]}
                values[cid] = params
                result.history[cid].append((index, dict(params)))
            paras.append(Para((Run(CLAUSES[cid][0]),), heading=True))
            paras.append(Para(redline(old_text, render(cid, params), rnd.author, rnd.when)))
        result.rounds.append(paras)
    result.final_values = values
    return result
