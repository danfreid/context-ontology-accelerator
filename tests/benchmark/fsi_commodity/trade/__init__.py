# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 scenarios (t1..t6)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .t1_houston_vitol_wti import build as t1
from .t2_london_kestrel_vlsfo import build as t2
from .t3_sydney_harbourline_ulp import build as t3
from .t4_newyork_glencore_isda import build as t4
from .t5_sydney_bp_jet import build as t5
from .t6_london_northgate_gbp_fwd import build as t6

# Scenario build functions, registered here in build order.
TRADE: tuple[Callable[..., Any], ...] = (t1, t2, t3, t4, t5, t6)
