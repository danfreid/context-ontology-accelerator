# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build the corpus: universe, then each phase in order, each reading the phases before it."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .scenario import PHASE_DIRS, Scenario
from .universe import DEFAULT_SEED, build_universe, dumps

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
PHASES = ("onboarding", "trade", "post_trade", "settlement", "docsets", "lifecycle", "execution")

# (emails, attachments, chats) per phase: BRIEF.md section 1 for the first four phases; the docsets phase
# (multi-format and merged document sets, see docsets/__init__.py) adds its own. Enforced once a phase has
# its full number of scenarios.
EXPECTED_TOTALS: dict[str, tuple[int, int, int]] = {
    "onboarding": (138, 95, 0),
    "trade": (42, 18, 13),
    "post_trade": (55, 39, 0),
    "settlement": (34, 19, 0),
    "docsets": (8, 14, 0),
    "lifecycle": (26, 17, 0),
    "execution": (8, 6, 18),
}
SCENARIOS_PER_PHASE: dict[str, int] = {
    "onboarding": 6,
    "trade": 6,
    "post_trade": 6,
    "settlement": 6,
    "docsets": 2,
    "lifecycle": 1,
    "execution": 1,
}

ScenarioBuilder = Callable[[dict[str, Any], Mapping[str, Scenario]], Scenario]


class BuildError(Exception):
    """Raised when a scenario or phase does not match its expected counts."""


@dataclass
class BuildResult:
    """The universe and every built scenario, in build order."""

    seed: int
    universe: dict[str, Any]
    scenarios: list[Scenario] = field(default_factory=list)


def _registry() -> dict[str, Sequence[ScenarioBuilder]]:
    """Return the phase registries. Add a new workflow phase here (see BRIEF.md section 10)."""
    from .docsets import DOCSETS
    from .execution import EXECUTION
    from .lifecycle import LIFECYCLE
    from .onboarding import ONBOARDING
    from .post_trade import POST_TRADE
    from .settlement import SETTLEMENT
    from .trade import TRADE

    return {
        "onboarding": ONBOARDING,
        "trade": TRADE,
        "post_trade": POST_TRADE,
        "settlement": SETTLEMENT,
        "docsets": DOCSETS,
        "lifecycle": LIFECYCLE,
        "execution": EXECUTION,
    }


def build(seed: int = DEFAULT_SEED, registry: Mapping[str, Sequence[ScenarioBuilder]] | None = None) -> BuildResult:
    """Build everything in memory. Each builder receives the universe and all scenarios built so far."""
    registry = _registry() if registry is None else registry
    universe = build_universe(seed)
    result = BuildResult(seed, universe)
    upstream: dict[str, Scenario] = {}
    for phase in PHASES:
        built = []
        for builder in registry.get(phase, ()):
            scenario = builder(universe, upstream)
            if scenario.phase != phase:
                raise BuildError(f"{scenario.id} is registered under {phase} but declares {scenario.phase}")
            if scenario.id in upstream:
                raise BuildError(f"duplicate scenario id {scenario.id}")
            try:
                scenario.check_expect()
            except ValueError as exc:
                raise BuildError(str(exc)) from exc
            upstream[scenario.id] = scenario
            built.append(scenario)
        _check_phase_totals(phase, built)
        result.scenarios.extend(built)
    return result


def _check_phase_totals(phase: str, built: Sequence[Scenario]) -> None:
    if len(built) != SCENARIOS_PER_PHASE[phase]:
        return
    got = (
        sum(s.counts["emails"] for s in built),
        sum(s.counts["attachments"] for s in built),
        sum(s.counts["chats"] for s in built),
    )
    if got != EXPECTED_TOTALS[phase]:
        raise BuildError(f"{phase}: built (emails, attachments, chats) {got}, brief says {EXPECTED_TOTALS[phase]}")


def render_files(result: BuildResult, only: str | None = None) -> dict[str, bytes]:
    """Return ``{path relative to corpus/: bytes}``. ``only`` limits output to one scenario plus universe.json."""
    files: dict[str, bytes] = {"universe.json": dumps(result.universe)}
    matched = False
    for scenario in result.scenarios:
        if only is not None and scenario.id != only:
            continue
        matched = True
        for rel, data in scenario.files().items():
            files[f"{PHASE_DIRS[scenario.phase]}/{scenario.id}/{rel}"] = data
    if only is None:
        from .etrm import render_etrm
        from .structured import render_structured

        files.update(render_structured(result))
        files.update(render_etrm(result))
    if only is not None and not matched:
        known = ", ".join(s.id for s in result.scenarios) or "(none registered)"
        raise KeyError(f"unknown scenario {only!r}; known: {known}")
    return files


def write_files(files: Mapping[str, bytes], out: Path) -> None:
    """Write ``files`` under ``out`` in binary mode."""
    for rel, data in files.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
