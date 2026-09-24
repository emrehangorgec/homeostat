"""Deterministic policy gate. Whoever proposes an action (rule or model), this layer disposes.

There is no fixed "restart before reboot" ladder. Each proposal is judged on impact,
privilege, evidence, confidence, attempt history, cooldowns and whether a lower-impact
alternative is still untried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from homeostat.act.catalog import CATALOG, Impact, Privilege
from homeostat.state.schema import DeviceState, Mode


class PolicyConfig(BaseModel):
    privileges: list[Privilege] = Field(default_factory=lambda: ["none", "adb"])
    # Disruptive actions (impact MEDIUM or above) per incident. Cheap actions are bounded
    # by per_action_max instead, so patient low impact retries (reloading while a backend
    # is down) do not use up the budget that protects against restart and reboot loops.
    retry_budget: int = 2
    per_action_max: int = 2  # same action within one incident
    per_action_limits: dict[str, int] = Field(default_factory=lambda: {"reload_content": 5})
    max_autonomous_impact: Impact = Impact.MEDIUM  # above this needs a human
    cooldown_s: dict[str, float] = Field(
        default_factory=lambda: {
            "reload_content": 8.0,
            "restart_target": 60.0,
            "clear_target_data": 3600.0,
            "reboot": 6 * 3600.0,
        }
    )
    # If the proposed action is cooling down for at most this long, wait (observe) rather
    # than jump to a more disruptive alternative.
    wait_for_cooldown_up_to_s: float = 30.0
    # Minimum confidence per impact level for proposals that carry one (model proposals).
    min_confidence: dict[Impact, float] = Field(
        default_factory=lambda: {Impact.NONE: 0.0, Impact.LOW: 0.5, Impact.MEDIUM: 0.7, Impact.HIGH: 0.9}
    )


@dataclass
class Proposal:
    action: str
    source: str  # "rule:<id>" or "model:<name>"
    evidence: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    confidence: float | None = None  # None for deterministic rules


@dataclass(frozen=True)
class PastAction:
    action: str
    at: float  # unix time
    incident_id: str


@dataclass
class Decision:
    verdict: Literal["allow", "substitute", "deny", "escalate"]
    action: str
    reason: str

    @property
    def executes(self) -> bool:
        return self.verdict in ("allow", "substitute") and self.action not in ("escalate",)


_PASSIVE = {"observe", "escalate"}


class Policy:
    def __init__(self, config: PolicyConfig | None = None):
        self.config = config or PolicyConfig()

    def evaluate(
        self,
        proposal: Proposal,
        state: DeviceState,
        incident_id: str,
        history: list[PastAction],
        now: float,
    ) -> Decision:
        cfg = self.config
        action = proposal.action

        if state.mode == Mode.FREE:
            return Decision("deny", "observe", "free mode: guardian observes only")
        if action not in CATALOG:
            return Decision("escalate", "escalate", f"{action!r} is not in the action catalog")
        if action in _PASSIVE:
            return Decision("allow", action, "passive action")
        if not proposal.evidence:
            return Decision("escalate", "escalate", "proposal carries no evidence")

        this_incident = [p for p in history if p.incident_id == incident_id and p.action not in _PASSIVE]
        disruptive = [p for p in this_incident if p.action in CATALOG and CATALOG[p.action].impact >= Impact.MEDIUM]
        if len(disruptive) >= cfg.retry_budget and CATALOG[action].impact >= Impact.MEDIUM and not any(
            CATALOG[a].impact < Impact.MEDIUM for a in proposal.alternatives if a in CATALOG
        ):
            return Decision("escalate", "escalate", f"retry budget of {cfg.retry_budget} disruptive actions exhausted")

        wait = self._short_cooldown(action, history, now)
        if wait is not None and self._problem(action, proposal, this_incident, history, now + wait) is None:
            return Decision("substitute", "observe", f"{action!r} cooling down for {wait:.0f}s more, waiting")

        candidates = [action, *[a for a in proposal.alternatives if a != action]]
        reasons: list[str] = []
        for candidate in candidates:
            problem = self._problem(candidate, proposal, this_incident, history, now)
            if problem is None:
                lower = self._untried_lower_alternative(candidate, proposal, this_incident, history, now)
                if lower is not None:
                    return Decision("substitute", lower, f"lower impact {lower!r} still untried, before {candidate!r}")
                if candidate == action:
                    return Decision("allow", action, "all checks passed")
                return Decision("substitute", candidate, f"{action!r} rejected ({'; '.join(reasons)})")
            reasons.append(f"{candidate}: {problem}")
        return Decision("escalate", "escalate", "; ".join(reasons))

    def _problem(
        self,
        action: str,
        proposal: Proposal,
        this_incident: list[PastAction],
        history: list[PastAction],
        now: float,
    ) -> str | None:
        cfg = self.config
        spec = CATALOG.get(action)
        if spec is None:
            return "not in catalog"
        if spec.impact >= Impact.MEDIUM and sum(
            CATALOG[p.action].impact >= Impact.MEDIUM for p in this_incident if p.action in CATALOG
        ) >= cfg.retry_budget:
            return f"retry budget of {cfg.retry_budget} disruptive actions exhausted"
        if spec.privilege not in cfg.privileges:
            return f"requires privilege {spec.privilege!r}"
        if spec.impact > cfg.max_autonomous_impact:
            return f"impact {spec.impact.name} needs human approval"
        limit = cfg.per_action_limits.get(action, cfg.per_action_max)
        if sum(p.action == action for p in this_incident) >= limit:
            return f"already tried {limit}x in this incident"
        cooldown = cfg.cooldown_s.get(action, 0.0)
        last = max((p.at for p in history if p.action == action), default=None)
        if last is not None and now - last < cooldown:
            return f"cooldown, {cooldown - (now - last):.0f}s left"
        if proposal.confidence is not None and proposal.confidence < cfg.min_confidence[spec.impact]:
            return f"confidence {proposal.confidence:.2f} below {cfg.min_confidence[spec.impact]:.2f}"
        if not spec.reversible and proposal.confidence is not None:
            return "irreversible actions are never taken on model proposals alone"
        return None

    def _short_cooldown(self, action: str, history: list[PastAction], now: float) -> float | None:
        cooldown = self.config.cooldown_s.get(action, 0.0)
        last = max((p.at for p in history if p.action == action), default=None)
        if last is None or now - last >= cooldown:
            return None
        left = cooldown - (now - last)
        return left if left <= self.config.wait_for_cooldown_up_to_s else None

    def _untried_lower_alternative(
        self,
        action: str,
        proposal: Proposal,
        this_incident: list[PastAction],
        history: list[PastAction],
        now: float,
    ) -> str | None:
        impact = CATALOG[action].impact
        tried = {p.action for p in this_incident}
        for alt in proposal.alternatives:
            spec = CATALOG.get(alt)
            if spec is None or spec.impact >= impact or alt in tried:
                continue
            if self._problem(alt, proposal, this_incident, history, now) is None:
                return alt
        return None
