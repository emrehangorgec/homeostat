"""Evaluation runner: inject a fault N times, let the guardian respond, record every run.

Each run: establish a verified healthy baseline, wait a random jitter, inject, poll the
guardian until it opens an incident or the detection timeout passes, then store
detection latency, time to recovery and outcome.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from homeostat.faults.scenarios import Fault, reset_hooks
from homeostat.device.base import Device, DeviceUnreachable
from homeostat.guardian.loop import Guardian

RunOutcome = str  # recovered | escalated | observed_only | undetected | not_healthy_at_start | link_lost


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
) -> list[RunResult]:
    rng = rng or random.Random()
    target = guardian.executor.target
    results: list[RunResult] = []

    for _ in range(n):
        run_id = uuid.uuid4().hex[:12]
        guardian.run_id = run_id
        started = clock()
        try:
            result = _one_run(guardian, device, fault, run_id, experiment_id, arm, jitter_s,
                              detect_timeout_s, rng, clock, sleep)
        except DeviceUnreachable as e:
            # A host to device link failure says nothing about recovery: record it, never score it.
            result = RunResult(run_id, experiment_id, fault.id, fault.category, arm, started, None,
                               "link_lost", None, None, str(e))
        _save(guardian, result)
        results.append(result)
        if on_run:
            on_run(result)

    guardian.run_id = None
    try:
        reset_hooks(device, target)
    except DeviceUnreachable:
        pass
    return results


def _one_run(guardian: Guardian, device: Device, fault: Fault, run_id: str, experiment_id: str, arm: str,
             jitter_s: tuple[float, float], detect_timeout_s: float, rng: random.Random,
             clock: Callable[[], float], sleep: Callable[[float], None]) -> RunResult:
    target = guardian.executor.target
    reset_hooks(device, target)

    if not _ensure_healthy(guardian):
        return RunResult(
            run_id, experiment_id, fault.id, fault.category, arm, clock(), None,
            "not_healthy_at_start", None, None, "could not establish a healthy baseline",
        )

    guardian.collector.collect()  # move the crash log watermark past earlier runs
    guardian.collector.acknowledge_errors()
    sleep(rng.uniform(*jitter_s))

    injected_at = clock()
    note = fault.inject(device, target)
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
            "undetected", None, None, note,
        )
    return RunResult(
        run_id, experiment_id, fault.id, fault.category, arm, injected_at, report.id,
        report.outcome,
        report.detected_at - injected_at,
        report.closed_at - injected_at if report.outcome == "recovered" else None,
        note,
    )


def _ensure_healthy(guardian: Guardian) -> bool:
    if guardian.oracle.verify().healthy:
        return True
    # Harness reset, not scored: leave no state from the previous run behind.
    guardian.executor.execute("wake_screen")
    guardian.executor.execute("dismiss_keyguard")
    guardian.executor.execute("dismiss_system_dialogs")
    guardian.executor.execute("relaunch_target")
    guardian.sleep(guardian.config.settle_s)
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
        }
    )
