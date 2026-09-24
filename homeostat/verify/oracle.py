"""Healthy-state oracle: did the device actually recover?

If verification reused the detection signals, the loop would validate itself. So the
oracle's strong evidence is independent of detection:

* ui_marker: an expected element is present in the live UI hierarchy (uiautomator dump).
  Detection never reads the UI tree, so this cannot be satisfied by the same signal that
  raised the incident. For the homeostat web kiosk the marker is set only after the page
  has finished loading.
* stable: the target keeps the same PID and stays in front for a whole window. A process
  that comes back and dies again (crash loop) fails this even though a single snapshot
  would look healthy.

`foreground` is shared with detection and is kept only as a cheap precondition. A healthy
verdict without a configured marker is reported as `weak`, and that is recorded with the
result.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from homeostat.device.base import Device
from homeostat.state import parsers
from homeostat.state.schema import ActivityRef


class UiMarker(BaseModel):
    """At least one field must be set; all set fields must match the same node."""

    text: str | None = None
    resource_id: str | None = None
    content_desc: str | None = None

    def pattern(self) -> re.Pattern[str]:
        parts = []
        if self.text is not None:
            parts.append(f'text="{re.escape(self.text)}"')
        if self.resource_id is not None:
            parts.append(f'resource-id="{re.escape(self.resource_id)}"')
        if self.content_desc is not None:
            parts.append(f'content-desc="{re.escape(self.content_desc)}"')
        if not parts:
            raise ValueError("UiMarker needs at least one field")
        lookaheads = "".join(f"(?=[^>]*{p})" for p in parts)
        return re.compile(f"<node{lookaheads}[^>]*>")


class OracleConfig(BaseModel):
    marker: UiMarker | None = None
    stable_for_s: float = 10.0
    sample_every_s: float = 2.0


@dataclass
class VerifyResult:
    healthy: bool
    strength: Literal["strong", "weak"]
    checks: dict[str, bool | None] = field(default_factory=dict)
    duration_s: float = 0.0
    detail: str = ""


_DUMP = "uiautomator dump /sdcard/homeostat_ui.xml >/dev/null 2>&1 && cat /sdcard/homeostat_ui.xml"


class HealthOracle:
    def __init__(
        self,
        device: Device,
        target: ActivityRef,
        config: OracleConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.device = device
        self.target = target
        self.config = config or OracleConfig()
        self.sleep = sleep

    def _snapshot(self) -> tuple[ActivityRef | None, int | None]:
        out = self.device.shell(
            f"dumpsys activity activities | grep -E 'ResumedActivity'; echo __pid__; pidof {self.target.process_name}"
        ).stdout
        resumed, _, pid = out.partition("__pid__")
        return parsers.parse_resumed_activity(resumed), parsers.parse_pidof(pid)

    def _marker_present(self) -> bool | None:
        if self.config.marker is None:
            return None
        xml = self.device.shell(_DUMP, timeout=20.0).stdout
        if "<hierarchy" not in xml:
            return None  # dump failed: unknown, not absent
        return bool(self.config.marker.pattern().search(xml))

    def verify(self) -> VerifyResult:
        started = time.monotonic()
        checks: dict[str, bool | None] = {}

        foreground, pid = self._snapshot()
        checks["foreground"] = self.target.matches(foreground)
        checks["process"] = pid is not None
        if not (checks["foreground"] and checks["process"]):
            return self._result(False, checks, started, "target not in front or not running")

        checks["ui_marker"] = self._marker_present()
        if checks["ui_marker"] is False:
            return self._result(False, checks, started, "expected UI marker absent")

        waited = 0.0
        stable = True
        while waited < self.config.stable_for_s:
            step = min(self.config.sample_every_s, self.config.stable_for_s - waited)
            self.sleep(step)
            waited += step
            fg_now, pid_now = self._snapshot()
            if pid_now != pid or not self.target.matches(fg_now):
                stable = False
                break
        checks["stable"] = stable
        if not stable:
            return self._result(False, checks, started, "target restarted or left the foreground during the window")
        return self._result(True, checks, started, "")

    def _result(self, healthy: bool, checks: dict[str, bool | None], started: float, detail: str) -> VerifyResult:
        # Only a healthy verdict can be weak: one without the independent UI evidence.
        # An unhealthy verdict rests on a failed check and is definitive either way.
        weak = healthy and checks.get("ui_marker") is not True
        strength: Literal["strong", "weak"] = "weak" if weak else "strong"
        return VerifyResult(healthy, strength, checks, time.monotonic() - started, detail)
