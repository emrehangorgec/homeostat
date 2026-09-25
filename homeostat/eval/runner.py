"""Evaluation runner: inject a fault N times, let the guardian respond, record every run.

Each run: establish a verified healthy baseline, wait a random jitter, inject, poll the
guardian until it opens an incident or the detection timeout passes, then store
detection latency, time to recovery, outcome, and how disruptive the actions were
compared with what the fault needed.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from homeostat.act.catalog import CATALOG, Impact
from homeostat.device.base import Device, DeviceUnreachable
from homeostat.faults.scenarios import Fault, FaultNotApplicable, reset_hooks
from homeostat.guardian.loop import Guardian
from homeostat.testbed.client import Testbed

RunOutcome = str  # recovered | escalated | observed_only | undetected | not_healthy_at_start | link_lost

_PASSIVE = {"observe", "escalate"}


@dataclass
class RunResult:
    run_id: str
    experiment_id: str
    scenario_id: str
    category: str
    arm: str
    injected_at: float
    incident_id: str | None
    outcome: RunOutcome
    detection_latency_s: float | None
    time_to_recovery_s: float | None
    notes: str
    max_impact: int | None = None  # highest impact action executed in the incident
    excess_actions: int = 0  # executed actions above the fault's needed impact
    correct: bool | None = None  # outcome as expected and nothing above the needed impact
    llm_calls: int = 0
    cost_usd: float = 0.0


def run_scenario(
    guardian: Guardian,
    device: Device,
    fault: Fault,
    n: int,
    experiment_id: str,
    arm: str = "rules_only",
    jitter_s: tuple[float, float] = (0.0, 3.0),
    detect_timeout_s: float = 30.0,
    rng: random.Random | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    on_run: Callable[[RunResult], None] | None = None,
    testbed: Testbed | None = None,
    link_wait_s: float = 600.0,
    on_link_restored: Callable[[], None] | None = None,
) -> list[RunResult]:
    rng = rng or random.Random()
    target = guardian.executor.target
    results: list[RunResult] = []

    for _ in range(n):
        run_id = uuid.uuid4().hex[:12]
        guardian.run_id = run_id
        started = clock()
        guardian.history_floor = started
        try:
            result = _one_run(guardian, device, fault, run_id, experiment_id, arm, jitter_s,
                              detect_timeout_s, rng, clock, sleep, testbed)
        except DeviceUnreachable as e:
            # A host to device link failure says nothing about recovery: record it, never score
            # it, and wait for the link instead of failing every following run in a second
            # (a short USB drop once burned 53 runs of m2-device-01 in under a minute).
            result = RunResult(run_id, experiment_id, fault.id, fault.category, arm, started, None,
                               "link_lost", None, None, str(e))
            _save(guardian, result)
            results.append(result)
            if on_run:
                on_run(result)
            if not _wait_for_link(device, clock, sleep, link_wait_s):
                raise DeviceUnreachable(f"link did not come back within {link_wait_s:.0f}s") from e
            if on_link_restored:
                on_link_restored()
            continue
        except FaultNotApplicable as e:
            result = RunResult(run_id, experiment_id, fault.id, fault.category, arm, started, None,
                               "not_applicable", None, None, str(e))
        _save(guardian, result)
        results.append(result)
        if on_run:
            on_run(result)

    guardian.run_id = None
    guardian.history_floor = None
    try:
        reset_hooks(device, target, testbed)
    except DeviceUnreachable:
        pass
    return results


def _one_run(guardian: Guardian, device: Device, fault: Fault, run_id: str, experiment_id: str, arm: str,
             jitter_s: tuple[float, float], detect_timeout_s: float, rng: random.Random,
             clock: Callable[[], float], sleep: Callable[[float], None], testbed: Testbed | None) -> RunResult:
    target = guardian.executor.target
    reset_hooks(device, target, testbed)

    if not _ensure_healthy(guardian):
        return RunResult(
            run_id, experiment_id, fault.id, fault.category, arm, clock(), None,
            "not_healthy_at_start", None, None, "could not establish a healthy baseline",
        )

    guardian.collector.collect()  # move the log watermarks past earlier runs
    guardian.collector.acknowledge_errors()
    sleep(rng.uniform(*jitter_s))

    injected_at = clock()
    note = fault.inject(device, target, testbed)
    # A real fault lands at a random phase of the polling cycle; ticking right after
    # the injection would understate detection latency by up to one poll interval.
    sleep(rng.uniform(0.0, guardian.config.poll_s))

    report = None
    while clock() - injected_at < detect_timeout_s:
        report = guardian.tick()
        if report is not None:
            break
        sleep(guardian.config.poll_s)

    if report is None:
        return RunResult(
            run_id, experiment_id, fault.id, fault.category, arm, injected_at, None,
            "undetected", None, None, note, correct=False,
        )
    executed = [
        a["action"] for a in guardian.store.actions_for(report.id)
        if a["verdict"] in ("allow", "substitute") and a["action"] not in _PASSIVE
    ]
    impacts = [int(CATALOG[a].impact) for a in executed if a in CATALOG]
    excess = sum(i > fault.needed_impact for i in impacts)
    return RunResult(
        run_id, experiment_id, fault.id, fault.category, arm, injected_at, report.id,
        report.outcome,
        report.detected_at - injected_at,
        report.closed_at - injected_at if report.outcome == "recovered" else None,
        note,
        max_impact=max(impacts, default=int(Impact.NONE)),
        excess_actions=excess,
        correct=report.outcome == fault.expected_outcome and excess == 0,
        llm_calls=report.llm_calls,
        cost_usd=report.cost_usd,
    )


def _wait_for_link(device: Device, clock: Callable[[], float], sleep: Callable[[float], None],
                   timeout_s: float) -> bool:
    deadline = clock() + timeout_s
    while clock() < deadline:
        try:
            if device.shell("echo homeostat-link").stdout.strip() == "homeostat-link":
                return True
        except DeviceUnreachable:
            pass
        sleep(5.0)
    return False


def _ensure_healthy(guardian: Guardian) -> bool:
    if guardian.oracle.verify().healthy:
        return True
    # Harness reset, not scored: leave no state from the previous run behind.
    for action in ("wake_screen", "dismiss_keyguard", "dismiss_system_dialogs", "enable_wifi", "relaunch_target",
                   "reload_content"):
        guardian.executor.execute(action)
    guardian.sleep(guardian.config.settle_s)
    if guardian.oracle.verify().healthy:
        return True
    guardian.executor.execute("restart_target")  # e.g. a kiosk left hung by the previous run
    guardian.sleep(guardian.config.settle_s * 2)
    return guardian.oracle.verify().healthy


def _save(guardian: Guardian, result: RunResult) -> None:
    guardian.store.save_run(
        {
            "id": result.run_id,
            "experiment_id": result.experiment_id,
            "scenario_id": result.scenario_id,
            "category": result.category,
            "arm": result.arm,
            "injected_at": result.injected_at,
            "incident_id": result.incident_id,
            "outcome": result.outcome,
            "detection_latency_s": result.detection_latency_s,
            "time_to_recovery_s": result.time_to_recovery_s,
            "notes": result.notes,
            "max_impact": result.max_impact,
            "excess_actions": result.excess_actions,
            "correct": None if result.correct is None else int(result.correct),
            "llm_calls": result.llm_calls,
            "cost_usd": result.cost_usd,
        }
    )
