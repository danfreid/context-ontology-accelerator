# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trade execution: Bloomberg shorthand per trader pair, learned confidence, and ETRM bookings vs confirmations.

* A **dialect** belongs to a trader pair (NewCo trader, counterparty trader). It says how that pair writes each
  term: product and location aliases, the quantity suffix (``250k`` is 250,000 bbl for one pair, ``10k`` is
  10,000 mt for another) and the price scale (``+85`` is USD 0.85/bbl for one pair, ``+12`` is USD 12.00/mt for
  another). Chats are rendered from the dialect, so the ground truth knows every token's meaning.
* **Confidence** in reading a token comes from earlier chats of the *same pair* that ended in a confirmed booking
  with the same meaning; evidence from other pairs counts for less and conflicting evidence counts for nothing.
* The **ETRM** is a RightAngle-style deal capture database (a structured source, ``etrm.py``). The email
  confirmation is the legally binding record; a booking that differs from it is a break to remediate with a new
  deal version.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

THRESHOLD = 0.75  # auto-book at or above; review below


@dataclass(frozen=True)
class Dialect:
    """How one trader pair writes terms in chat."""

    pair: str  # e.g. "hou-vitol"
    ours: str  # "newco-hou:trader"
    theirs: str  # "cp-vitol:trader"
    firm: str  # Bloomberg firm string shown for the counterparty
    ba_code: str  # ETRM business associate code
    unit: str  # unit the bare "k" suffix means for this pair
    price_scale: Decimal  # "+85" * scale = premium (0.01 for cents per bbl, 1 for USD per mt)
    products: Mapping[str, tuple[str, str]]  # alias -> (product name, ETRM product code)
    locations: Mapping[str, str]  # alias -> location


@dataclass(frozen=True)
class ChatDeal:
    """One deal as agreed in chat, in normalised terms (NewCo's side)."""

    trade_id: str
    dialect: Dialect
    side: str  # "buy" or "sell" (NewCo)
    alias: str  # product alias used in chat
    loc: str  # location alias used in chat
    qty_k: int  # thousands
    month: str  # "Apr"
    incoterm: str
    premium_ticks: int | None = None  # "+85"
    fixed: Decimal | None = None  # "flat 438"
    index: str = ""

    @property
    def product(self) -> str:
        return self.dialect.products[self.alias][0]

    @property
    def product_code(self) -> str:
        return self.dialect.products[self.alias][1]

    @property
    def location(self) -> str:
        return self.dialect.locations[self.loc]

    @property
    def quantity(self) -> int:
        return self.qty_k * 1000

    @property
    def premium(self) -> Decimal | None:
        return None if self.premium_ticks is None else self.premium_ticks * self.dialect.price_scale

    def terms(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "side": self.side,
            "product": self.product,
            "location": self.location,
            "incoterm": self.incoterm.upper(),
            "quantity": self.quantity,
            "unit": self.dialect.unit,
            "period": self.month,
        }
        if self.fixed is not None:
            out |= {"price_type": "fixed", "price": float(self.fixed)}
        else:
            out |= {"price_type": "index", "index": self.index, "premium": float(self.premium or 0)}
        return out

    def tokens(self) -> dict[str, tuple[str, Any]]:
        """Token key -> (field, meaning) used by this chat; the keys are what confidence is learned on."""
        out: dict[str, tuple[str, Any]] = {
            f"product:{self.alias}": ("product", self.product),
            f"location:{self.loc}": ("location", self.location),
            "qty:k": ("unit", self.dialect.unit),
            f"firm:{self.dialect.firm}": ("counterparty", self.dialect.theirs.split(":")[0]),
        }
        if self.premium_ticks is not None:
            out["price:+n"] = ("price_scale", float(self.dialect.price_scale))
        return out

    def lines(self) -> list[tuple[str, str, str, dict[str, Any]]]:
        """(speaker ref, text, intent, stated terms) in the pair's shorthand."""
        ours, theirs = self.dialect.ours, self.dialect.theirs
        verb = "bid" if self.side == "buy" else "offer"
        ask = "offer" if self.side == "buy" else "bid"
        if self.fixed is not None:
            price, stated = f"flat {self.fixed:g}", {"price": float(self.fixed)}
        else:
            price, stated = f"+{self.premium_ticks}", {"premium": float(self.premium or 0)}
        t = self.terms()
        return [
            (
                ours,
                f"{self.alias} {self.qty_k}k {self.incoterm} {self.loc} {self.month}, your {ask}?",
                "enquiry",
                {k: t[k] for k in ("product", "quantity", "unit", "incoterm", "location", "period")},
            ),
            (theirs, f"{price}", "offer", stated),
            (
                ours,
                f"{verb} {price} {self.qty_k}k, done?",
                "counter" if self.side == "buy" else "offer",
                {"side": self.side},
            ),
            (theirs, "done. pls send conf", "accept", t),
        ]


def confidence(token: str, meaning: Any, pair: str, history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Confidence in reading ``token`` as ``meaning`` in a chat of ``pair``, from earlier *confirmed* chats.

    Same-pair support ``n`` gives ``n / (n + 1)``. With no same-pair evidence, consistent evidence from other
    pairs (``m`` chats) gives ``0.5 * m / (m + 1)``; any other-pair evidence with a different meaning, or none at
    all, gives 0. Decision: ``auto`` at or above ``THRESHOLD``, otherwise ``review``.
    """
    same = [h["chat"] for h in history if h["pair"] == pair and h["tokens"].get(token) == meaning]
    if same:
        score = round(len(same) / (len(same) + 1), 3)
        basis, evidence = "pair", same
    else:
        other = [h for h in history if h["pair"] != pair and token in h["tokens"]]
        conflicting = [h["chat"] for h in other if h["tokens"][token] != meaning]
        agreeing = [h["chat"] for h in other if h["tokens"][token] == meaning]
        if conflicting or not agreeing:
            score, basis, evidence = 0.0, "conflict" if conflicting else "none", conflicting
        else:
            score, basis, evidence = round(0.5 * len(agreeing) / (len(agreeing) + 1), 3), "desk", agreeing
    return {
        "token": token,
        "meaning": meaning,
        "confidence": score,
        "basis": basis,
        "evidence": evidence,
        "decision": "auto" if score >= THRESHOLD else "review",
    }


ETRM_COLUMNS = (
    "DealNumber",
    "Version",
    "DealDate",
    "Trader",
    "InternalBA",
    "ExternalBA",
    "BuySell",
    "ProductCode",
    "Location",
    "DeliveryTerm",
    "Period",
    "Quantity",
    "UOM",
    "PriceType",
    "FixedPrice",
    "PriceIndex",
    "Premium",
    "Currency",
    "DealStatus",
    "ExternalRef",
    "ChangeReason",
)


def etrm_row(
    deal: ChatDeal,
    *,
    number: str,
    version: int,
    date: str,
    trader: str,
    internal: str,
    status: str,
    overrides: Mapping[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """One RightAngle-style deal capture row, as booked (``overrides`` plant booking errors)."""
    row: dict[str, Any] = {
        "DealNumber": number,
        "Version": version,
        "DealDate": date,
        "Trader": trader,
        "InternalBA": internal,
        "ExternalBA": deal.dialect.ba_code,
        "BuySell": "B" if deal.side == "buy" else "S",
        "ProductCode": deal.product_code,
        "Location": deal.location,
        "DeliveryTerm": deal.incoterm.upper(),
        "Period": deal.month,
        "Quantity": deal.quantity,
        "UOM": deal.dialect.unit.upper(),
        "PriceType": "FIXED" if deal.fixed is not None else "INDEX",
        "FixedPrice": "" if deal.fixed is None else f"{deal.fixed:.3f}",
        "PriceIndex": deal.index,
        "Premium": "" if deal.premium is None else f"{deal.premium:.3f}",
        "Currency": "USD",
        "DealStatus": status,
        "ExternalRef": deal.trade_id,
        "ChangeReason": reason,
    }
    row.update(overrides or {})
    return row


# Fields compared between the confirmation (system of record) and the ETRM booking.
def booked_terms(row: Mapping[str, Any], products: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "side": "buy" if row["BuySell"] == "B" else "sell",
        "product": products[row["ProductCode"]],
        "quantity": int(row["Quantity"]),
        "unit": str(row["UOM"]).lower(),
    }
    if row["PriceType"] == "FIXED":
        out["price"] = float(Decimal(str(row["FixedPrice"])))
    else:
        out["premium"] = float(Decimal(str(row["Premium"])))
    return out


def breaks(confirmed: Mapping[str, Any], row: Mapping[str, Any], products: Mapping[str, str]) -> list[dict[str, Any]]:
    """Fields where the ETRM booking differs from the confirmation."""
    booked = booked_terms(row, products)
    return [{"field": k, "confirmation": confirmed[k], "etrm": v} for k, v in booked.items() if confirmed.get(k) != v]
