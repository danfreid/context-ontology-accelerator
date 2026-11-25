# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Phase 4 scenarios (x1..x6)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .x1_houston_vitol_wti import build as x1
from .x2_london_kestrel_vlsfo import build as x2
from .x3_sydney_harbourline_ulp import build as x3
from .x4_newyork_glencore_isda import build as x4
from .x5_sydney_bp_jet import build as x5
from .x6_london_northgate_gbp_fwd import build as x6

# Scenario build functions, registered here in build order.
SETTLEMENT: tuple[Callable[..., Any], ...] = (x1, x2, x3, x4, x5, x6)
