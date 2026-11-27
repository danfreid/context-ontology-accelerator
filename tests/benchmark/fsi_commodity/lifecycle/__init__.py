# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Phase 6 scenarios (l1): a master agreement from first draft to the calculations that use its terms.

Not part of BRIEF.md's 24 scenarios; ``build.EXPECTED_TOTALS["lifecycle"]`` holds this phase's own counts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .l1_london_shell_mpta import build as l1

# Scenario build functions, registered here in build order.
LIFECYCLE: tuple[Callable[..., Any], ...] = (l1,)
