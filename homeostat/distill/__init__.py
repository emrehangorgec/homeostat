"""Distillation (M4): model decisions that were verified correct become rule candidates, and
candidates that survive replay against recorded incidents become a rule pack."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from homeostat.detect.rules import Rule, RulePack
from homeostat.distill.distiller import DistillResult, Distiller, Resolved
from homeostat.distill.replay import Case, Replay, fit_priority, load_cases, replay
from homeostat.state.schema import SCHEMA_VERSION, DeviceState
from homeostat.store.sqlite import Store


@dataclass
class Outcome:
    result: DistillResult
    replay: Replay | None  # None when the candidate was invalid or a duplicate


def resolved_incidents(store: Store, experiments: list[str]) -> list[Resolved]:
    """Incidents whose first decision came from the diagnostician and whose run ended correctly."""
    marks = ",".join("?" * len(experiments))
    rows = store.conn.execute(
        f"SELECT i.id, i.first_state, d.diagnosis, d.explanation, "
        f"  (SELECT a.action FROM actions a WHERE a.incident_id = i.id ORDER BY a.at, a.rowid LIMIT 1) AS action, "
        f"  (SELECT a.source FROM actions a WHERE a.incident_id = i.id ORDER BY a.at, a.rowid LIMIT 1) AS source "
        f"FROM runs r JOIN incidents i ON i.id = r.incident_id "
        f"JOIN diagnoses d ON d.id = (SELECT MIN(id) FROM diagnoses WHERE incident_id = i.id) "
        f"WHERE r.experiment_id IN ({marks}) AND r.correct = 1 AND d.diagnosis IS NOT NULL",
        experiments,
    ).fetchall()
    return [
        Resolved(row["id"], DeviceState.model_validate(json.loads(row["first_state"])), row["diagnosis"],
                 row["action"], row["explanation"] or "", [])
        for row in rows
        if row["source"] and row["source"].startswith("model:")
    ]


def clusters(resolved: list[Resolved]) -> dict[tuple[str, str], list[Resolved]]:
    """Group by what the model decided: one rule should come out of each (diagnosis, action)."""
    groups: dict[tuple[str, str], list[Resolved]] = defaultdict(list)
    for r in resolved:
        groups[(r.diagnosis, r.action)].append(r)
    return dict(groups)


def _key(rule: Rule) -> tuple:
    return rule.action, tuple(sorted((c.path, c.op, repr(c.value)) for c in rule.when))


def distill(store: Store, experiments: list[str], rules: RulePack, distiller: Distiller,
            per_cluster: int = 2, on_result=None) -> list[Outcome]:
    cases: list[Case] = load_cases(store, experiments)
    outcomes: list[Outcome] = []
    seen: set[tuple] = set()
    taken = {r.id for r in rules.rules}
    for group in clusters(resolved_incidents(store, experiments)).values():
        states = [r.state for r in group]
        # Spread the examples over the cluster instead of taking the first few in a row.
        step = max(1, len(group) // per_cluster)
        for source in group[::step][:per_cluster]:
            result = distiller.distill(source, rules)
            outcome = Outcome(result, None)
            if result.rule is not None and _key(result.rule) not in seen:
                seen.add(_key(result.rule))
                rule_id, n = result.rule.id, 2
                while rule_id in taken:  # two different candidates can pick the same name
                    rule_id, n = f"{result.rule.id}_{n}", n + 1
                taken.add(rule_id)
                result.rule = fit_priority(rules, result.rule.model_copy(update={"id": rule_id}), states)
                outcome.replay = replay(rules, result.rule, cases)
            outcomes.append(outcome)
            if on_result:
                on_result(outcome)
    return outcomes


def write_pack(rules: list[Rule], path: Path, note: str) -> None:
    """A rule pack in the same TOML format as the built-in one, loadable with rule_packs."""
    def value(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(float(v))
        return json.dumps(v)

    lines = [f"# Distilled rules. {note}", "# Each rule passed replay validation; edit or delete freely.", "",
             f'schema_version = "{SCHEMA_VERSION}"']
    for r in rules:
        lines += ["", "[[rule]]", f"id = {json.dumps(r.id)}", f"description = {json.dumps(r.description)}",
                  f"incident = {json.dumps(r.incident)}", f"action = {json.dumps(r.action)}",
                  f"priority = {r.priority}", 'origin = "distilled"', "when = ["]
        for c in r.when:
            parts = [f"path = {json.dumps(c.path)}", f"op = {json.dumps(c.op)}"]
            if c.op not in ("is_null", "not_null"):
                parts.append(f"value = {value(c.value)}")
            lines.append("    { " + ", ".join(parts) + " },")
        lines.append("]")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
