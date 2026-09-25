"""Summaries with confidence intervals, and the arm x category results matrix.

The headline metric is the correct-outcome rate: the run ended as the fault expects
(recovered, or escalated when nothing on the device can fix it) and no action exceeded
the impact the fault needed. Runs recorded before correctness was stored (M1, M2) are
scored by the same rule: every M1/M2 fault expects recovery.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

CATEGORIES = ["known", "variant", "composite", "held_out"]
# Harness failures: they say nothing about recovery, so they are shown but never scored.
UNSCORED = {"link_lost", "not_healthy_at_start", "not_applicable"}
ARMS = ["rules_only", "llm_only", "hybrid", "hybrid_distilled"]


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval. Stays sensible at 0/n and n/n, unlike the normal approximation."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _get(run: Any, name: str, default: Any = None) -> Any:
    if isinstance(run, dict):
        return run.get(name, default)
    return getattr(run, name, default)


def is_correct(run: Any) -> bool:
    stored = _get(run, "correct")
    if stored is not None:
        return bool(stored)
    return _get(run, "outcome") == "recovered" and not (_get(run, "excess_actions") or 0)


@dataclass
class Summary:
    key: str
    n: int  # scored runs
    correct: int
    recovered: int
    ci: tuple[float, float]  # Wilson interval of the correct rate
    outcomes: dict[str, int]
    median_detection_s: float | None
    median_ttr_s: float | None
    excess_actions: int = 0  # executed actions above what the fault needed, over all scored runs
    llm_calls: int = 0
    cost_usd: float = 0.0

    @property
    def rate(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def cost_per_run(self) -> float:
        return self.cost_usd / self.n if self.n else 0.0

    def cell(self) -> str:
        if self.n == 0:
            return "not run"
        lo, hi = self.ci
        return f"{self.rate:.0%} [{lo:.0%}, {hi:.0%}] n={self.n}"


def summarize(runs: Iterable[Any], key: str) -> Summary:
    all_runs = list(runs)
    outcomes: dict[str, int] = defaultdict(int)
    for r in all_runs:
        outcomes[_get(r, "outcome")] += 1
    runs = [r for r in all_runs if _get(r, "outcome") not in UNSCORED]
    correct = sum(is_correct(r) for r in runs)
    detections = [_get(r, "detection_latency_s") for r in runs if _get(r, "detection_latency_s") is not None]
    ttrs = [_get(r, "time_to_recovery_s") for r in runs if _get(r, "time_to_recovery_s") is not None]
    return Summary(
        key=key,
        n=len(runs),
        correct=correct,
        recovered=outcomes.get("recovered", 0),
        ci=wilson(correct, len(runs)),
        outcomes=dict(outcomes),
        median_detection_s=statistics.median(detections) if detections else None,
        median_ttr_s=statistics.median(ttrs) if ttrs else None,
        excess_actions=sum(_get(r, "excess_actions") or 0 for r in runs),
        llm_calls=sum(_get(r, "llm_calls") or 0 for r in runs),
        cost_usd=sum(_get(r, "cost_usd") or 0.0 for r in runs),
    )


def by_scenario(runs: Iterable[Any]) -> list[Summary]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in runs:
        groups[_get(r, "scenario_id")].append(r)
    return [summarize(rs, key) for key, rs in groups.items()]


def scenario_table(runs: Iterable[Any]) -> str:
    fmt = lambda v: "-" if v is None else f"{v:.1f}s"  # noqa: E731
    lines = [
        "| scenario | correct (95% CI) | median detection | median TTR | unneeded disruptive | model calls | outcomes |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in by_scenario(runs):
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(s.outcomes.items()))
        lines.append(
            f"| {s.key} | {s.cell()} | {fmt(s.median_detection_s)} | {fmt(s.median_ttr_s)} | "
            f"{s.excess_actions} | {s.llm_calls} | {outcomes} |"
        )
    return "\n".join(lines)


def matrix(runs: Iterable[Any]) -> str:
    cells: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in runs:
        cells[(_get(r, "arm"), _get(r, "category"))].append(r)
    lines = ["| arm | " + " | ".join(CATEGORIES) + " |", "| --- |" + " --- |" * len(CATEGORIES)]
    for arm in ARMS:
        row = [summarize(cells[(arm, c)], f"{arm}/{c}").cell() for c in CATEGORIES]
        lines.append(f"| {arm} | " + " | ".join(row) + " |")
    return "\n".join(lines)
