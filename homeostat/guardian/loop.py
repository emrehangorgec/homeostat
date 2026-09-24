"""The closed recovery loop: observe, detect, propose, policy check, act, verify, record."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel

from homeostat.act.catalog import CATALOG, Impact
from homeostat.act.executor import ActionResult, AdbExecutor
from homeostat.detect.rules import Detection, RulePack
from homeostat.device.base import DeviceUnreachable
from homeostat.policy.engine import Decision, Policy, Proposal
from homeostat.state.collect import Collector
from homeostat.state.schema import SCHEMA_VERSION, DeviceState
from homeostat.store.sqlite import Store
from homeostat.verify.oracle import HealthOracle, VerifyResult

Outcome = Literal["recovered", "escalated", "observed_only"]

_HISTORY_WINDOW_S = 24 * 3600.0


class GuardianConfig(BaseModel):
    poll_s: float = 2.0
    settle_s: float = 3.0  # wait after an action before verifying
    observe_wait_s: float = 5.0  # what the `observe` action waits
    max_steps: int = 10  # proposals per incident, a hard stop independent of policy
    visual_every_s: float = 10.0  # screenshot probe interval for detection (0 disables)
    # After an action: no symptom left but the oracle not yet satisfied (a page still
    # loading) is given this many observe steps before it counts as unexplained.
    settle_observations: int = 3


@dataclass
class Step:
    proposal: Proposal
    decision: Decision
    result: ActionResult | None
    verify: VerifyResult | None


@dataclass
class IncidentReport:
    id: str
    detected_at: float
    closed_at: float
    symptoms: list[str]
    incident_type: str | None
    rule_id: str | None
    outcome: Outcome
    attempts: int
    steps: list[Step] = field(default_factory=list)

    @property
    def final_verify(self) -> VerifyResult | None:
        return next((s.verify for s in reversed(self.steps) if s.verify is not None), None)


class Guardian:
    def __init__(
        self,
        collector: Collector,
        rules: RulePack,
        policy: Policy,
        executor: AdbExecutor,
        oracle: HealthOracle,
        store: Store,
        config: GuardianConfig | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.collector = collector
        self.rules = rules
        self.policy = policy
        self.executor = executor
        self.oracle = oracle
        self.store = store
        self.config = config or GuardianConfig()
        self.clock = clock
        self.sleep = sleep
        self.run_id: str | None = None  # set by the eval runner
        # Evaluation runs are independent trials: the runner sets this to the run's start so
        # cooldowns and budgets from a previous run do not leak into the next one. In normal
        # operation it stays None and the policy sees the last 24 h across incidents.
        self.history_floor: float | None = None

    def tick(self) -> IncidentReport | None:
        """Observe once. If something is wrong, run a full recovery episode and return its report."""
        state = self.collector.collect()
        detection = self.rules.detect(state)
        if not detection.unhealthy:
            return None
        return self._episode(state, detection)

    def run_forever(
        self,
        on_incident: Callable[[IncidentReport], None] | None = None,
        on_link_lost: Callable[[str], None] | None = None,
    ) -> None:
        while True:
            try:
                report = self.tick()
            except DeviceUnreachable as e:
                # The host lost the device, the device did not fail. Wait, never act.
                if on_link_lost:
                    on_link_lost(str(e))
                report = None
            if report and on_incident:
                on_incident(report)
            self.sleep(self.config.poll_s)

    def _episode(self, state: DeviceState, detection: Detection) -> IncidentReport:
        incident_id = uuid.uuid4().hex[:12]
        detected_at = self.clock()
        first_state = state
        best = detection.best
        report = IncidentReport(
            id=incident_id,
            detected_at=detected_at,
            closed_at=detected_at,
            symptoms=detection.symptoms,
            incident_type=best.rule.incident if best else None,
            rule_id=best.rule.id if best else None,
            outcome="escalated",
            attempts=0,
        )

        settling = 0
        last_verify: VerifyResult | None = None
        for _ in range(self.config.max_steps):
            if not detection.unhealthy and last_verify is not None and settling < self.config.settle_observations:
                # The last action removed every symptom but the oracle is not satisfied
                # yet, typically a page still loading: give it time before judging.
                settling += 1
                proposal = Proposal("observe", "guardian:settling", ["no symptom left, oracle not yet satisfied"])
                decision = Decision("allow", "observe", "waiting for the last action to settle")
            elif not detection.classified:
                reason = (
                    f"unclassified: symptoms {detection.symptoms} match no rule"
                    if detection.unhealthy
                    else "oracle reports unhealthy but no symptom is present"
                )
                self._record_step(report, Proposal("escalate", "guardian"), Decision("escalate", "escalate", reason))
                report.outcome = "escalated"
                break
            else:
                match = detection.best
                proposal = Proposal(
                    action=match.rule.action,
                    source=f"rule:{match.rule.id}",
                    evidence=match.evidence,
                    alternatives=match.rule.alternatives,
                )
                now = self.clock()
                history = self.store.action_history(since=max(now - _HISTORY_WINDOW_S, self.history_floor or 0.0))
                decision = self.policy.evaluate(proposal, state, incident_id, history, now)

                if decision.verdict == "deny":
                    self._record_step(report, proposal, decision)
                    report.outcome = "observed_only"
                    break
                if decision.verdict == "escalate" or decision.action == "escalate":
                    self._record_step(report, proposal, decision)
                    report.outcome = "escalated"
                    break

            acted_at = self.clock()  # cooldowns count from the action, not from its verification
            result = self._execute(decision.action)
            if CATALOG[decision.action].impact > Impact.NONE:
                report.attempts += 1
            self.sleep(self.config.settle_s)
            verify = self.oracle.verify()
            last_verify = verify
            self._record_step(report, proposal, decision, result, verify, at=acted_at)

            # Recovered means the oracle is satisfied and no symptom is left: a page that
            # looks fine while Wi-Fi is still off is not a recovery. Crash lines seen so
            # far belong to this incident and are history once the oracle is satisfied.
            if verify.healthy:
                self.collector.acknowledge_errors()
            state = self.collector.collect()
            detection = self.rules.detect(state)
            if verify.healthy and not detection.unhealthy:
                report.outcome = "recovered"
                break
        else:
            self._record_step(
                report,
                Proposal("escalate", "guardian"),
                Decision("escalate", "escalate", f"step limit {self.config.max_steps} reached"),
            )
            report.outcome = "escalated"

        report.closed_at = self.clock()
        if report.outcome == "recovered":
            self.collector.acknowledge_errors()
        self._save_incident(report, first_state, state)
        return report

    def _execute(self, action: str) -> ActionResult:
        if action == "observe":
            started = time.monotonic()
            self.sleep(self.config.observe_wait_s)
            return ActionResult("observe", True, "", time.monotonic() - started)
        return self.executor.execute(action)

    def _record_step(
        self,
        report: IncidentReport,
        proposal: Proposal,
        decision: Decision,
        result: ActionResult | None = None,
        verify: VerifyResult | None = None,
        at: float | None = None,
    ) -> None:
        report.steps.append(Step(proposal, decision, result, verify))
        self.store.save_action(
            {
                "incident_id": report.id,
                "at": self.clock() if at is None else at,
                "proposed": proposal.action,
                "action": decision.action,
                "source": proposal.source,
                "verdict": decision.verdict,
                "reason": decision.reason,
                "ok": None if result is None else int(result.ok),
                "duration_s": None if result is None else result.duration_s,
                "verify": None if verify is None else asdict(verify),
            }
        )

    def _save_incident(self, report: IncidentReport, first: DeviceState, last: DeviceState) -> None:
        final = report.final_verify
        self.store.save_incident(
            {
                "id": report.id,
                "run_id": self.run_id,
                "detected_at": report.detected_at,
                "closed_at": report.closed_at,
                "symptoms": report.symptoms,
                "incident_type": report.incident_type,
                "rule_id": report.rule_id,
                "outcome": report.outcome,
                "verify_strength": final.strength if final else None,
                "attempts": report.attempts,
                "schema_version": SCHEMA_VERSION,
                "first_state": first.model_dump(mode="json"),
                "final_state": last.model_dump(mode="json"),
            }
        )
