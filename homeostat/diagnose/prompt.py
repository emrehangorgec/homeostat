"""Prompt for the diagnostician: a stable, cacheable system prefix and a volatile evidence message.

Everything that is the same for every incident (role, policy, action catalog, rule
catalog, field glossary) lives in the system prompt and is rendered deterministically,
so the prefix caches. Everything about this incident goes in the user message.
"""

from __future__ import annotations

import json

from homeostat.act.catalog import CATALOG
from homeostat.detect.rules import RulePack
from homeostat.diagnose.base import INCIDENT_LABELS, IncidentContext

_ROLE = """\
You diagnose incidents on an Android device that is used as an always-on screen: a web
kiosk or a single app that should be visible and working at all times (a wall panel, a
menu board, a status display). A guardian watches the device, detects symptoms, and
asks you when its deterministic rules do not settle what to do.

You propose; you do not act. A deterministic policy decides whether your proposal runs:
- it only runs actions from the catalog below;
- a proposal needs at least this confidence for its impact: none 0.0, low 0.5,
  medium 0.7; high impact actions always need a human;
- an irreversible action never runs on a model proposal alone;
- disruptive actions (medium or high impact) are budgeted per incident, and some
  actions have cooldowns.
After an action, an independent oracle checks the screen (target in front, its UI
marker, not a blank screen, stable process, live heartbeat). The incident is closed only
when the oracle is satisfied and no symptom is left.

What a good diagnosis looks like:
- Pick the least disruptive action that the evidence actually justifies. Restarting or
  clearing an app whose backend is down, or whose page says it is in maintenance, does
  not help and costs the user.
- A person may be using the device. Recent user input while another app is in front
  suggests intent, not a fault; interrupting them is a cost.
- If the evidence does not identify a cause, or nothing in the catalog can plausibly fix
  it (a missing configuration, a hardware problem, an outage outside the device), set
  abstain to true and propose "escalate". Abstaining is a correct answer, not a failure.
- Use the steps already taken: do not repeat an action that just failed to verify.
- confidence is the probability that your proposed action leads to verified recovery
  (for "observe": that waiting leads to recovery; for "escalate": that a human is needed).
  Be calibrated: 0.9 should be right about nine times in ten.
- evidence lists the state fields your diagnosis rests on, as dotted paths
  (for example "app.detail", "screen.last_user_activity_s").
"""

_GLOSSARY = """\
Evidence fields (DeviceState v1). null means the value could not be read; never treat it
as false.
- mode: "panel" (keep the target up) or "free" (observe only; you will not be asked).
- target / foreground: the app that should be in front, and the one that is.
- target_in_foreground: whether they match.
- target_process.running / pid: the target's process.
- screen.awake, screen.keyguard_showing, screen.blocking_dialog ("app_error", "anr",
  "none"), screen.focus_window, screen.blank (the screen is one flat color; sampled
  every few seconds, null in between), screen.last_user_activity_s (seconds since the
  last touch or key; the guardian's own wake_screen also counts).
- network.wifi_enabled, network.internet_reachable (three pings to 1.1.1.1).
- app: what the target reports about itself (health contract v1; all null if it does
  not implement it): state (loading, ready, error, auth_error, app_error), detail, http
  status of the page, state_age_s, heartbeat_age_s (a hung app stops its heartbeat).
- battery, memory: device resources.
- recent_errors: crash lines of the target process in the last minutes.
"""


def _actions_section() -> str:
    lines = ["Action catalog (name | impact | reversible | what it does):"]
    for name in sorted(CATALOG):
        spec = CATALOG[name]
        lines.append(f"- {name} | {spec.impact.name.lower()} | {'yes' if spec.reversible else 'no'} | {spec.description}")
    return "\n".join(lines)


def _rules_section(rules: RulePack) -> str:
    lines = ["The guardian's deterministic rules (you are consulted when none of them decides):"]
    for rule in sorted(rules.rules, key=lambda r: r.id):
        lines.append(f"- {rule.id}: {rule.description} -> {rule.action}")
    return "\n".join(lines)


def system_prompt(rules: RulePack) -> str:
    labels = ", ".join(INCIDENT_LABELS)
    return "\n\n".join(
        [
            _ROLE,
            _actions_section(),
            _rules_section(rules),
            _GLOSSARY,
            f"Diagnosis labels: {labels}. Use \"unknown\" when none fits.",
            "Answer with the JSON object only.",
        ]
    )


def _redacted(state: dict) -> dict:
    """The evidence minus identifiers: a model never needs the device serial."""
    state = dict(state)
    if isinstance(state.get("device"), dict):
        state["device"] = {k: v for k, v in state["device"].items() if k != "serial"}
    return state


def evidence_message(context: IncidentContext) -> str:
    payload = {
        "symptoms": context.symptoms,
        "rule_matches": context.rule_matches,
        "steps_taken_in_this_incident": [step.__dict__ for step in context.steps],
        "recent_incidents_on_this_device": context.history,
        "device_state": _redacted(context.state),
    }
    return "Incident evidence:\n" + json.dumps(payload, indent=1, sort_keys=True, default=str)
