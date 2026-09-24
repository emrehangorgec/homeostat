"""Declarative symptoms and rules over DeviceState.

Both are data (TOML rule packs), not code, for two reasons: the distiller (M4) must be
able to emit new rules in the same format, and users must be able to share rule packs.

A condition on an unknown (None) value is always false, except `is_null`. Rules never
fire on evidence that could not be collected.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from homeostat.act.catalog import CATALOG
from homeostat.state.schema import SCHEMA_VERSION, DeviceState

Op = Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "empty", "nonempty", "is_null", "not_null"]

_NULL_OK = {"is_null", "not_null"}


class Condition(BaseModel):
    path: str
    op: Op
    value: Any = None

    def holds(self, state: DeviceState) -> bool:
        actual = state.get(self.path)
        if self.op == "is_null":
            return actual is None
        if self.op == "not_null":
            return actual is not None
        if actual is None:
            return False
        match self.op:
            case "eq":
                return actual == self.value
            case "ne":
                return actual != self.value
            case "lt":
                return actual < self.value
            case "le":
                return actual <= self.value
            case "gt":
                return actual > self.value
            case "ge":
                return actual >= self.value
            case "in":
                return actual in self.value
            case "empty":
                return len(actual) == 0
            case "nonempty":
                return len(actual) > 0
        raise AssertionError(self.op)

    def describe(self, state: DeviceState) -> str:
        return f"{self.path}={state.get(self.path)!r} ({self.op} {self.value!r})" if self.op not in _NULL_OK else (
            f"{self.path} {self.op}"
        )


class Symptom(BaseModel):
    id: str
    description: str = ""
    when: list[Condition] = Field(min_length=1)

    def present(self, state: DeviceState) -> bool:
        return all(c.holds(state) for c in self.when)


class Rule(BaseModel):
    id: str
    description: str = ""
    incident: str
    action: str
    alternatives: list[str] = Field(default_factory=list)
    priority: int = 0
    when: list[Condition] = Field(min_length=1)
    origin: Literal["builtin", "user", "distilled"] = "builtin"

    @field_validator("action")
    @classmethod
    def _action_in_catalog(cls, value: str) -> str:
        if value not in CATALOG:
            raise ValueError(f"unknown action {value!r}")
        return value

    @field_validator("alternatives")
    @classmethod
    def _alternatives_in_catalog(cls, values: list[str]) -> list[str]:
        unknown = [v for v in values if v not in CATALOG]
        if unknown:
            raise ValueError(f"unknown actions {unknown}")
        return values

    def matches(self, state: DeviceState) -> bool:
        return all(c.holds(state) for c in self.when)

    def evidence(self, state: DeviceState) -> list[str]:
        return [c.describe(state) for c in self.when]


@dataclass
class Match:
    rule: Rule
    evidence: list[str]


@dataclass
class Detection:
    symptoms: list[str]
    matches: list[Match] = field(default_factory=list)

    @property
    def unhealthy(self) -> bool:
        return bool(self.symptoms)

    @property
    def classified(self) -> bool:
        return bool(self.matches)

    @property
    def best(self) -> Match | None:
        return self.matches[0] if self.matches else None


class RulePack(BaseModel):
    schema_version: str
    symptoms: list[Symptom] = Field(default_factory=list, alias="symptom")
    rules: list[Rule] = Field(default_factory=list, alias="rule")

    @field_validator("schema_version")
    @classmethod
    def _schema_matches(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"rule pack targets DeviceState v{value}, runtime is v{SCHEMA_VERSION}")
        return value

    @classmethod
    def load(cls, *paths: Path) -> RulePack:
        merged: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "symptom": [], "rule": []}
        for path in paths:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
            pack = cls.model_validate(data)
            merged["symptom"] += [s.model_dump() for s in pack.symptoms]
            merged["rule"] += [r.model_dump() for r in pack.rules]
        return cls.model_validate(merged)

    def detect(self, state: DeviceState) -> Detection:
        symptoms = [s.id for s in self.symptoms if s.present(state)]
        if not symptoms:
            return Detection(symptoms=[])
        matches = [Match(r, r.evidence(state)) for r in self.rules if r.matches(state)]
        matches.sort(key=lambda m: m.rule.priority, reverse=True)
        return Detection(symptoms=symptoms, matches=matches)
