# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 scenarios (s1..s6)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .s1_houston_vitol_wti import build as s1
from .s2_london_kestrel_vlsfo import build as s2
from .s3_sydney_harbourline_ulp import build as s3
from .s4_newyork_glencore_isda import build as s4
from .s5_sydney_bp_jet import build as s5
from .s6_london_tiberpeak_decline import build as s6

# Scenario build functions, registered here in build order.
ONBOARDING: tuple[Callable[..., Any], ...] = (s1, s2, s3, s4, s5, s6)
