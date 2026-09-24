"""Executes catalog actions over ADB. Each action maps to a fixed command; no free-form input."""

from __future__ import annotations

import time
from dataclasses import dataclass

from homeostat.act.catalog import CATALOG
from homeostat.device.base import Device
from homeostat.state.schema import ActivityRef


@dataclass(frozen=True)
class ActionResult:
    action: str
    ok: bool
    output: str
    duration_s: float


def _commands(action: str, target: ActivityRef) -> list[str]:
    start = f"am start -n {target.component}" if target.activity else (
        f"monkey -p {target.package} -c android.intent.category.LAUNCHER 1"
    )
    return {
        "observe": [],
        "escalate": [],
        "wake_screen": ["input keyevent KEYCODE_WAKEUP"],
        # Works from the shell on Android 10; apps lose this broadcast on Android 12+.
        "dismiss_system_dialogs": ["am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS"],
        "dismiss_keyguard": ["wm dismiss-keyguard"],
        "relaunch_target": [start],
        "restart_target": [f"am force-stop {target.package}", start],
        "clear_target_data": [f"pm clear {target.package}", start],
        "reboot": ["reboot"],
    }[action]


class AdbExecutor:
    def __init__(self, device: Device, target: ActivityRef):
        self.device = device
        self.target = target

    def execute(self, action: str) -> ActionResult:
        if action not in CATALOG:
            raise ValueError(f"action not in catalog: {action}")
        started = time.monotonic()
        outputs, ok = [], True
        for command in _commands(action, self.target):
            result = self.device.shell(command, timeout=30.0)
            outputs.append(result.stdout.strip())
            # `am start` exits 0 even when it fails, so check its output too.
            if not result.ok or "Error" in result.stdout:
                ok = False
                break
        return ActionResult(action, ok, "\n".join(o for o in outputs if o), time.monotonic() - started)
