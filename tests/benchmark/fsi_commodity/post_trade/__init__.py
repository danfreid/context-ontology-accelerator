# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 scenarios (p1..p6)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .p1_houston_vitol_wti import build as p1
from .p2_london_kestrel_vlsfo import build as p2
from .p3_sydney_harbourline_ulp import build as p3
from .p4_newyork_glencore_isda import build as p4
from .p5_sydney_bp_jet import build as p5
from .p6_london_northgate_gbp_fwd import build as p6

# Scenario build functions, registered here in build order.
POST_TRADE: tuple[Callable[..., Any], ...] = (p1, p2, p3, p4, p5, p6)
