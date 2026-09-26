"""The distiller: turns a model's verified decision into a candidate deterministic rule.

The model is a rule compiler here, not a controller. It sees one incident the diagnostician
got right (the opening state, the diagnosis, the action, the explanation) and the rules that
already exist, and writes the narrowest rule that captures why that action was right. What
it writes is only a candidate: replay validation decides whether it is ever used.

Candidate values arrive as strings (a small model handles one value type far better than a
union) and are coerced to the type the state holds at that path.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from homeostat.act.catalog import CATALOG
from homeostat.detect.rules import Condition, Rule, RulePack
from homeostat.diagnose.base import INCIDENT_LABELS
from homeostat.diagnose.prompt import GLOSSARY, redacted
from homeostat.state.schema import DeviceState

OPS = ["eq", "ne", "lt", "le", "gt", "ge", "startswith", "is_null", "not_null"]


@dataclass
class Resolved:
    """An incident the diagnostician decided and whose run ended correctly."""

    incident_id: str
    state: DeviceState
    diagnosis: str
    action: str
    explanation: str
    evidence: list[str]


class CandidateCondition(BaseModel):
    path: str
    op: str
    value: str = ""


class Candidate(BaseModel):
    id: str
    description: str
    incident: str
    action: str
    when: list[CandidateCondition] = Field(min_length=1, max_length=5)


@dataclass
class DistillResult:
    source: Resolved
    rule: Rule | None
    raw: dict[str, Any] | None = None
    error: str | None = None
    latency_s: float = 0.0


class Distiller(Protocol):
    name: str

    def distill(self, resolved: Resolved, rules: RulePack) -> DistillResult: ...


def candidate_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "description": {"type": "string"},
            "incident": {"type": "string", "enum": INCIDENT_LABELS},
            "action": {"type": "string", "enum": sorted(CATALOG)},
            "when": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "op": {"type": "string", "enum": OPS},
                        "value": {"type": "string"},
                    },
                    "required": ["path", "op", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["id", "description", "incident", "action", "when"],
        "additionalProperties": False,
    }


_ROLE = """\
You turn one resolved incident into a deterministic rule for a guardian that keeps an
Android device working as an always-on screen. A diagnostician model decided this incident,
and the outcome was verified correct. Write the rule that would have made the same decision
without asking a model next time.

A rule is a list of conditions on the device state (all must hold) and one action.
- Use 2 to 4 conditions, only on the fields that actually decided this incident. The
  evidence and explanation say which ones.
- Be narrow: the rule must not fire on other faults. Prefer a stable prefix of a text field
  ("startswith") over its full text, and a threshold on a number over its exact value.
- Values are written as strings: "true", "false", "30", or the text prefix.
- Operators: eq, ne, lt, le, gt, ge (numbers), startswith (text), is_null, not_null.
- id: short snake_case name. description: one sentence, what it means and why that action.
- Do not repeat an existing rule. Existing rules are listed with their conditions.
"""


def _rules_listing(rules: RulePack) -> str:
    lines = ["Existing rules:"]
    for r in sorted(rules.rules, key=lambda r: r.id):
        conditions = "; ".join(f"{c.path} {c.op} {c.value!r}" for c in r.when)
        lines.append(f"- {r.id} -> {r.action}: {conditions}")
    return "\n".join(lines)


def system_prompt(rules: RulePack) -> str:
    return "\n\n".join([_ROLE, GLOSSARY, _rules_listing(rules), "Answer with the JSON object only."])


def user_message(resolved: Resolved) -> str:
    payload = {
        "decision": {"diagnosis": resolved.diagnosis, "action": resolved.action,
                     "explanation": resolved.explanation, "evidence": resolved.evidence},
        "device_state": redacted(resolved.state.model_dump(mode="json")),
    }
    return "Resolved incident:\n" + json.dumps(payload, indent=1, sort_keys=True, default=str)


def _coerce(raw: str, current: Any) -> Any:
    text = raw.strip()
    if isinstance(current, bool):
        if text.lower() not in ("true", "false"):
            raise ValueError(f"expected true or false, got {raw!r}")
        return text.lower() == "true"
    if isinstance(current, (int, float)):
        return float(text)
    return text


def to_rule(candidate: Candidate, state: DeviceState) -> Rule:
    """A validated Rule, or ValueError: unknown path, unknown op, or a value of the wrong type."""
    conditions = []
    for c in candidate.when:
        if c.op not in OPS:
            raise ValueError(f"operator {c.op!r} not allowed")
        try:
            current = state.get(c.path)
        except KeyError:
            raise ValueError(f"unknown path {c.path!r}") from None
        value = None if c.op in ("is_null", "not_null") else _coerce(c.value, current)
        conditions.append(Condition(path=c.path, op=c.op, value=value))
    rule_id = re.sub(r"[^a-z0-9_]+", "_", candidate.id.lower()).strip("_") or "distilled"
    return Rule(id=f"distilled_{rule_id}", description=candidate.description, incident=candidate.incident,
                action=candidate.action, when=conditions, origin="distilled")


def check_own_incident(rule: Rule, resolved: Resolved) -> str | None:
    """A candidate must at least describe the incident it came from, with the action taken there."""
    if rule.action != resolved.action:
        return f"proposes {rule.action}, but {resolved.action} was the verified decision"
    if not rule.matches(resolved.state):
        failing = [c.describe(resolved.state) for c in rule.when if not c.holds(resolved.state)]
        return f"does not match its own incident: {failing}"
    return None


class OllamaDistiller:
    def __init__(self, model: str = "qwen3:4b", url: str = "http://127.0.0.1:11434", num_ctx: int = 4096,
                 timeout_s: float = 300.0):
        self.model = model
        self.name = f"ollama:{model}"
        self.url = url.rstrip("/")
        self.num_ctx = num_ctx
        self.timeout_s = timeout_s

    def distill(self, resolved: Resolved, rules: RulePack) -> DistillResult:
        started = time.monotonic()
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt(rules)},
                         {"role": "user", "content": user_message(resolved)}],
            "format": candidate_schema(),
            "think": False,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        request = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return DistillResult(resolved, None, error=f"ollama unreachable: {e}",
                                 latency_s=time.monotonic() - started)
        return parse(resolved, (data.get("message") or {}).get("content", ""), time.monotonic() - started)


def parse(resolved: Resolved, text: str, latency_s: float = 0.0) -> DistillResult:
    try:
        raw = json.loads(text)
        rule = to_rule(Candidate.model_validate(raw), resolved.state)
    except (ValueError, ValidationError) as e:
        return DistillResult(resolved, None, error=f"invalid candidate: {str(e)[:200]}", latency_s=latency_s)
    problem = check_own_incident(rule, resolved)
    if problem:
        return DistillResult(resolved, None, raw, f"rejected: {problem}", latency_s)
    return DistillResult(resolved, rule, raw, latency_s=latency_s)


class ScriptedDistiller:
    """For tests: returns whatever candidate the given function writes. Never a result."""

    def __init__(self, fn: Callable[[Resolved], dict[str, Any]], name: str = "scripted"):
        self.fn = fn
        self.name = name
        self.calls: list[Resolved] = []

    def distill(self, resolved: Resolved, rules: RulePack) -> DistillResult:
        self.calls.append(resolved)
        return parse(resolved, json.dumps(self.fn(resolved)))
