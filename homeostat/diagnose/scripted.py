"""A scripted diagnostician for tests and the simulator.

It follows a few hand-written heuristics so the hybrid and llm_only code paths can be
exercised without a model or an API key. Its numbers are never results: a report built
from it measures these heuristics, not a model.
"""

from __future__ import annotations

from collections.abc import Callable

from homeostat.diagnose.base import Diagnosis, DiagnosisResult, IncidentContext


def heuristic(context: IncidentContext) -> Diagnosis:
    state = context.state
    app = state.get("app") or {}
    screen = state.get("screen") or {}
    detail = app.get("detail") or ""
    tried = {step.action for step in context.steps}

    def d(label: str, action: str, confidence: float, why: str, abstain: bool = False) -> Diagnosis:
        return Diagnosis(diagnosis=label, explanation=why, confidence=confidence, abstain=abstain,
                         proposed_action=action, evidence=["app.detail"] if detail else [])

    if detail.startswith("maintenance"):
        return d("planned_maintenance", "observe", 0.8, "the page reports a planned maintenance window")
    if detail.startswith("config"):
        return d("configuration_error", "escalate", 0.9, "a missing setting cannot be fixed on the device", True)
    user_recent = (screen.get("last_user_activity_s") or 1e9) < 60
    if state.get("target_in_foreground") is False and user_recent:
        return d("user_intent", "escalate", 0.75, "someone is using another app", True)
    if context.rule_matches:
        best = context.rule_matches[0]
        if best["action"] not in tried or best["action"] == "observe":
            return d("unknown", best["action"], 0.7, f"following rule {best['rule']}")
    return d("unknown", "escalate", 0.5, "no explanation from the evidence", True)


class ScriptedDiagnostician:
    def __init__(self, fn: Callable[[IncidentContext], Diagnosis] = heuristic, name: str = "scripted"):
        self.fn = fn
        self.name = name
        self.calls: list[IncidentContext] = []

    def diagnose(self, context: IncidentContext) -> DiagnosisResult:
        self.calls.append(context)
        return DiagnosisResult(model=self.name, diagnosis=self.fn(context))
