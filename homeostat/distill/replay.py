"""Replay validation: what a candidate rule would have decided on every recorded incident.

A candidate is never promoted on the model's word. It is replayed against the opening state
of real incidents, each labeled by how its run ended, and judged on what it changes:

- support: it decides an incident the same way a run that ended correctly did;
- override: it replaces a decision that ended correctly with a different action. This is a
  false positive, and a single one rejects the candidate;
- failure: it repeats an action that ended wrongly. Evidence against the action, but noisy:
  a run can fail after a right first step (a wait that outlasts the step limit), so a small
  share is tolerated;
- unverified: it changes a decision that ended wrongly into something never tried there.

Decisions follow the hybrid arm without a model: the best matching rule acts unless no rule
matches or a contest applies to it, in which case the model would have been asked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from homeostat.detect.rules import Rule, RulePack
from homeostat.faults.scenarios import FAULTS
from homeostat.state.schema import DeviceState
from homeostat.store.sqlite import Store

ASK_MODEL = "ask_model"


@dataclass
class Case:
    incident_id: str
    experiment_id: str
    scenario_id: str
    state: DeviceState
    action: str  # the first decision taken in the incident
    correct: bool


@dataclass
class Replay:
    rule: Rule
    support: list[Case] = field(default_factory=list)
    overrides: list[Case] = field(default_factory=list)
    failures: list[Case] = field(default_factory=list)
    unverified: list[Case] = field(default_factory=list)

    def promotable(self, min_support: int, max_failure_share: float = 0.1) -> bool:
        tried = len(self.support) + len(self.failures)
        return (len(self.support) >= min_support and not self.overrides
                and len(self.failures) <= max_failure_share * tried)

    def summary(self) -> str:
        scenarios = sorted({c.scenario_id for c in self.support})
        return (f"support {len(self.support)} ({', '.join(scenarios) or 'none'}), overrides {len(self.overrides)}, "
                f"failures {len(self.failures)}, unverified {len(self.unverified)}")


def load_cases(store: Store, experiments: list[str]) -> list[Case]:
    """Every scored incident of these experiments, with its opening state and its label."""
    marks = ",".join("?" * len(experiments))
    rows = store.conn.execute(
        f"SELECT r.experiment_id, r.scenario_id, r.outcome, r.correct, r.excess_actions, i.id, i.first_state "
        f"FROM runs r JOIN incidents i ON i.id = r.incident_id WHERE r.experiment_id IN ({marks})",
        experiments,
    ).fetchall()
    cases = []
    for row in rows:
        first = store.conn.execute(
            "SELECT action FROM actions WHERE incident_id = ? ORDER BY at, rowid LIMIT 1", (row["id"],)
        ).fetchone()
        if first is None:
            continue
        correct = row["correct"]
        if correct is None:  # runs recorded before M3 scored correctness: derive it the same way
            fault = FAULTS.get(row["scenario_id"])
            if fault is None:
                continue
            correct = row["outcome"] == fault.expected_outcome and not row["excess_actions"]
        cases.append(Case(row["id"], row["experiment_id"], row["scenario_id"],
                          DeviceState.model_validate(json.loads(row["first_state"])), first["action"], bool(correct)))
    return cases


def decide(rules: RulePack, state: DeviceState) -> tuple[str, Rule | None]:
    """What the hybrid arm would do before asking a model: an action and the rule behind it."""
    detection = rules.detect(state)
    best = detection.best
    if best is None or detection.contested(best):
        return ASK_MODEL, None
    return best.rule.action, best.rule


def with_rule(rules: RulePack, rule: Rule) -> RulePack:
    return RulePack.model_validate({
        "schema_version": rules.schema_version,
        "symptom": [s.model_dump() for s in rules.symptoms],
        "rule": [r.model_dump() for r in rules.rules] + [rule.model_dump()],
        "contest": [c.model_dump() for c in rules.contests],
    })


def replay(rules: RulePack, rule: Rule, cases: list[Case]) -> Replay:
    extended = with_rule(rules, rule)
    result = Replay(rule)
    for case in cases:
        action, by = decide(extended, case.state)
        if by is None or by.id != rule.id:
            continue  # the candidate does not decide this incident
        if case.correct:
            (result.support if action == case.action else result.overrides).append(case)
        elif action == case.action:
            result.failures.append(case)
        else:
            result.unverified.append(case)
    return result


def fit_priority(rules: RulePack, rule: Rule, states: list[DeviceState]) -> Rule:
    """Rank the candidate just above every existing rule that matches where it should act."""
    rivals = [r.priority for s in states for r in rules.rules if r.matches(s)]
    return rule.model_copy(update={"priority": max(rivals, default=0) + 1})
