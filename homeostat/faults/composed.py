"""Composed scenarios: faults written as data, from a fixed set of device level primitives.

This is the vocabulary of the adversary (M4). A scenario is a list of steps, each one a
primitive with parameters, plus what a good guardian should end with. Primitives describe
what can be done to the device and its backend, never how homeostat reacts: the adversary
writes scenarios without seeing rules, prompts or policy.

Scenario files are TOML:

    [[scenario]]
    id = "quiet_upgrade"
    description = "The page announces an upgrade in its own words, then comes back."
    expected_outcome = "recovered"
    needed_impact = "none"
    steps = [
        { do = "notice", text = "Upgrading the heating controller, back in a minute", for_s = 45 },
    ]
"""

from __future__ import annotations

import json
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from homeostat.act.catalog import Impact
from homeostat.device.base import Device
from homeostat.device.sim import SimDevice
from homeostat.faults import scenarios as s
from homeostat.faults.scenarios import Fault
from homeostat.state.schema import ActivityRef
from homeostat.testbed.client import Testbed


@dataclass(frozen=True)
class Primitive:
    description: str
    params: dict[str, str]  # name -> description, shown to the adversary
    run: Callable[..., str]
    hooked: bool = False
    testbed: bool = False


def _pause(device: Device, seconds: float) -> None:
    # The simulator cannot advance its own clock from here (the runner owns it), so steps
    # there happen at once, as in the hand-written faults.
    if not isinstance(device, SimDevice):
        time.sleep(seconds)


def _touch(device: Device, target: ActivityRef, testbed: Testbed | None, times: int = 3) -> str:
    for n in range(times):
        if n:
            _pause(device, 1.0)
        device.shell(f"input tap 540 {900 + 150 * n}")
    return f"touched the screen {times}x"


def _backend_step(device: Device, target: ActivityRef, testbed: Testbed | None, mode: str,
                  for_s: float = 0.0, once: bool = False) -> str:
    note = s._backend(device, testbed).set_mode(mode, for_s=for_s or None, once=once)
    s._refresh(device, target)
    return note


def _notice(device: Device, target: ActivityRef, testbed: Testbed | None, text: str, for_s: float = 0.0) -> str:
    note = s._backend(device, testbed).set_mode("notice", for_s=for_s or None, text=text)
    return f"{note}: {text!r}"


def _wait(device: Device, target: ActivityRef, testbed: Testbed | None, seconds: float) -> str:
    _pause(device, seconds)
    return f"waited {seconds:g}s"


PRIMITIVES: dict[str, Primitive] = {
    "crash_app": Primitive("The kiosk app crashes once.", {}, lambda d, t, tb: s._crash(d, t)),
    "stop_app": Primitive("The kiosk app is force stopped, with no crash.", {}, lambda d, t, tb: s._kill(d, t)),
    "crash_on_start": Primitive(
        "The kiosk app crashes now and on its next starts.", {"times": "next starts that crash, 1 to 5"},
        lambda d, t, tb, times=3: s._hook(d, t, f"--ei crash_on_start {int(times)}") + " " + s._crash(d, t),
        hooked=True),
    "blank_screen": Primitive("The page content disappears; the app still believes it is fine.", {},
                              lambda d, t, tb: s._blank_ui(d, t), hooked=True),
    "freeze_app": Primitive("The app's main thread blocks: alive and on screen, but frozen.", {},
                            lambda d, t, tb: s._app_hang(d, t), hooked=True),
    "open_other_app": Primitive("Another app (the system Settings) opens over the kiosk.", {},
                                lambda d, t, tb: s._wrong_foreground(d, t)),
    "touch_screen": Primitive("Someone touches the screen, as a person using the device would.",
                              {"times": "touches, 1 to 5"}, _touch),
    "screen_off": Primitive("The display goes to sleep.", {}, lambda d, t, tb: s._screen_off(d, t)),
    "wifi_off": Primitive("Wi-Fi is switched off.", {}, lambda d, t, tb: s._wifi_off(d, t)),
    "backend": Primitive(
        "The page's backend misbehaves, then the page reloads.",
        {"mode": "down (answers 503), hang (a request never completes), js_error (the page script throws), "
                 "auth_expired (every session is invalid until the app starts a new one)",
         "for_s": "seconds until the backend is normal again, 0 for never (at most 120)",
         "once": "true: only the next page request is affected"},
        _backend_step, testbed=True),
    "notice": Primitive(
        "The page shows a message from its backend, in your words, and reports it as an error.",
        {"text": "the message the page shows, as its operator would write it",
         "for_s": "seconds until the message goes away and the page is normal, 0 for never (at most 120)"},
        _notice, testbed=True),
    "wait": Primitive("Nothing happens for a while.", {"seconds": "0 to 20"}, _wait),
}

_MODES = {"down", "hang", "js_error", "auth_expired"}


class Step(BaseModel):
    do: str
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data: Any) -> Any:
        # TOML writes steps flat ({ do = "notice", text = "..." }); keep params apart inside.
        if isinstance(data, dict) and "params" not in data:
            data = {"do": data.get("do"), "params": {k: v for k, v in data.items() if k != "do"}}
        return data

    @model_validator(mode="after")
    def _check(self) -> Step:
        if self.do not in PRIMITIVES:
            raise ValueError(f"unknown primitive {self.do!r}")
        unknown = set(self.params) - set(PRIMITIVES[self.do].params)
        if unknown:
            raise ValueError(f"{self.do}: unknown parameters {sorted(unknown)}")
        p = self.params
        if self.do == "backend" and p.get("mode") not in _MODES:
            raise ValueError(f"backend mode must be one of {sorted(_MODES)}")
        if self.do == "notice" and not str(p.get("text", "")).strip():
            raise ValueError("notice needs a text")
        for name, (low, high) in {"times": (1, 5), "for_s": (0, 120), "seconds": (0, 20)}.items():
            if name in p and not low <= float(p[name]) <= high:
                raise ValueError(f"{self.do}: {name} must be within {low} to {high}")
        return self


class Scenario(BaseModel):
    id: str
    description: str
    expected_outcome: Literal["recovered", "escalated"]
    needed_impact: Literal["none", "low", "medium"]
    steps: list[Step] = Field(min_length=1, max_length=6)
    rationale: str = ""  # why this outcome and impact are right, for the reviewer

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not value.replace("_", "").isalnum() or not value.islower():
            raise ValueError(f"id must be lower snake_case, got {value!r}")
        return value

    @model_validator(mode="after")
    def _has_a_fault(self) -> Scenario:
        if all(step.do == "wait" for step in self.steps):
            raise ValueError("a scenario needs at least one step that is not wait")
        return self

    @property
    def hooked(self) -> bool:
        return any(PRIMITIVES[step.do].hooked for step in self.steps)

    @property
    def testbed(self) -> bool:
        return any(PRIMITIVES[step.do].testbed for step in self.steps)

    def inject(self, device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
        notes = []
        for step in self.steps:
            params = dict(step.params)
            if "times" in params:
                params["times"] = int(params["times"])
            if "once" in params:
                params["once"] = str(params["once"]).lower() == "true"
            notes.append(PRIMITIVES[step.do].run(device, target, testbed, **params))
        return "; ".join(notes)

    def to_fault(self) -> Fault:
        return Fault(self.id, "held_out", self.description, None, self.inject,
                     needed_impact=Impact[self.needed_impact.upper()], hooked=self.hooked, testbed=self.testbed,
                     expected_outcome=self.expected_outcome)


def load(path: Path) -> list[Scenario]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return [Scenario.model_validate(item) for item in data.get("scenario", [])]


def dump(scenarios: list[Scenario], path: Path, header: str) -> None:
    def value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return json.dumps(v, ensure_ascii=False)

    lines = [f"# {line}" for line in header.splitlines()]
    for sc in scenarios:
        lines += ["", "[[scenario]]", f"id = {value(sc.id)}", f"description = {value(sc.description)}",
                  f"expected_outcome = {value(sc.expected_outcome)}", f"needed_impact = {value(sc.needed_impact)}"]
        if sc.rationale:
            lines.append(f"rationale = {value(sc.rationale)}")
        lines.append("steps = [")
        for step in sc.steps:
            parts = [f"do = {value(step.do)}"] + [f"{k} = {value(v)}" for k, v in step.params.items()]
            lines.append("    { " + ", ".join(parts) + " },")
        lines.append("]")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
