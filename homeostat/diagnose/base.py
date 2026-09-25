"""The diagnostician contract, independent of any model provider.

A diagnostician proposes; the deterministic policy disposes. It sees the normalized
evidence (never raw logs), returns a structured diagnosis, and may abstain. Its
`confidence` has one meaning, so it can be calibrated against outcomes without hand
labels: the probability that the proposed action leads to verified recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from homeostat.act.catalog import CATALOG

# Fixed, sorted-stable label set: part of the output schema, so changing it changes the
# cached prompt prefix and makes results incomparable across sessions.
INCIDENT_LABELS: list[str] = [
    "app_crash",
    "app_error",
    "app_hang",
    "auth_expired",
    "backend_down",
    "blank_screen",
    "blocking_dialog",
    "configuration_error",
    "device_resource",
    "keyguard",
    "network_down",
    "network_outage",
    "page_load_failed",
    "planned_maintenance",
    "screen_off",
    "stuck_loading",
    "transient_crash",
    "unknown",
    "user_intent",
    "wrong_foreground",
]

ACTIONS: list[str] = sorted(CATALOG)


class Diagnosis(BaseModel):
    diagnosis: str
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    abstain: bool
    proposed_action: str
    alternatives: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @field_validator("diagnosis")
    @classmethod
    def _known_label(cls, value: str) -> str:
        if value not in INCIDENT_LABELS:
            raise ValueError(f"unknown diagnosis label {value!r}")
        return value

    @field_validator("proposed_action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in CATALOG:
            raise ValueError(f"action not in catalog: {value!r}")
        return value

    @field_validator("alternatives")
    @classmethod
    def _known_alternatives(cls, values: list[str]) -> list[str]:
        return [v for v in values if v in CATALOG]


def output_schema() -> dict[str, Any]:
    """Strict JSON schema for structured outputs. Ranges are validated after parsing."""
    return {
        "type": "object",
        "properties": {
            "diagnosis": {"type": "string", "enum": INCIDENT_LABELS},
            "explanation": {"type": "string"},
            "confidence": {"type": "number"},
            "abstain": {"type": "boolean"},
            "proposed_action": {"type": "string", "enum": ACTIONS},
            "alternatives": {"type": "array", "items": {"type": "string", "enum": ACTIONS}},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["diagnosis", "explanation", "confidence", "abstain", "proposed_action", "alternatives", "evidence"],
        "additionalProperties": False,
    }


@dataclass
class StepSummary:
    action: str
    verdict: str
    reason: str
    healthy_after: bool | None
    failed_checks: list[str]


@dataclass
class IncidentContext:
    state: dict[str, Any]  # DeviceState, JSON mode
    symptoms: list[str]
    rule_matches: list[dict[str, Any]]  # {"rule", "action", "evidence"} for each match
    steps: list[StepSummary] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)  # recent incidents on this device


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


@dataclass
class DiagnosisResult:
    model: str
    diagnosis: Diagnosis | None  # None when the call failed or the output was invalid
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    served_by: str | None = None  # a refusal fallback can serve the request with another model

    @property
    def abstained(self) -> bool:
        return self.diagnosis is None or self.diagnosis.abstain


class Diagnostician(Protocol):
    name: str

    def diagnose(self, context: IncidentContext) -> DiagnosisResult: ...
