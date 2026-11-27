# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Phase 5 scenarios (d1, d2): multi-product cargo document sets in mixed formats, split and reconciled.

Not part of BRIEF.md's 24 scenarios; ``build.EXPECTED_TOTALS["docsets"]`` holds this phase's own counts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .d1_houston_vitol_products_batched import build as d1
from .d2_london_bp_products_merged import build as d2

# Scenario build functions, registered here in build order.
DOCSETS: tuple[Callable[..., Any], ...] = (d1, d2)
