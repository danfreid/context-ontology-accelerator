# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Phase 7 scenarios (e1): Bloomberg shorthand learned per trader pair, ETRM bookings against confirmations.

Not part of BRIEF.md's 24 scenarios; ``build.EXPECTED_TOTALS["execution"]`` holds this phase's own counts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .e1_desk_chats_etrm import build as e1

# Scenario build functions, registered here in build order.
EXECUTION: tuple[Callable[..., Any], ...] = (e1,)
