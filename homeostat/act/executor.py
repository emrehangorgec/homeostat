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
        "enable_wifi": ["svc wifi enable"],
        # Health contract v1 commands, addressed to the target's package.
        "reload_content": [f"am broadcast -p {target.package} -a com.homeostat.contract.RELOAD"],
        "reset_session": [f"am broadcast -p {target.package} -a com.homeostat.contract.RESET_SESSION"],
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
        if action == "restart_target":
            return self._restart()
        started = time.monotonic()
        outputs, ok = [], True
        for command in _commands(action, self.target):
            result = self.device.shell(command, timeout=30.0)
            outputs.append(result.stdout.strip())
            # `am start` exits 0 even when it fails, so check its output too; a contract
            # command to a target that is not running reports that in its result data.
            if not result.ok or "Error" in result.stdout or "not running" in result.stdout:
                ok = False
                break
        return ActionResult(action, ok, "\n".join(o for o in outputs if o), time.monotonic() - started)

    def _restart(self) -> ActionResult:
        """force-stop, and make sure it worked.

        Android ignores `am force-stop` for the Device Owner package and returns success,
        and SELinux denies `run-as <pkg> kill` (found on the OnePlus 5T during M2). So when
        the old process survives, ask the app to restart its UI process itself: the health
        contract RESTART command is handled in another process of the same app, which may
        kill it. `run-as` stays as the last resort for other debuggable targets.
        """
        started = time.monotonic()
        process = self.target.process_name
        before = self.device.shell(f"pidof {process}").stdout.split()
        self.device.shell(f"am force-stop {self.target.package}")
        notes = []
        def alive() -> bool:
            return bool(before) and before[0] in self.device.shell(f"pidof {process}").stdout.split()

        def died() -> bool:  # a killed process can take a moment to disappear
            return any(not alive() for _ in range(5))

        if alive():
            notes.append("force-stop left the process alive")
            self.device.shell(f"am broadcast -p {self.target.package} -a com.homeostat.contract.RESTART")
            if not died():
                self.device.shell(f"run-as {self.target.package} kill -9 {before[0]}")
                if not died():
                    notes.append("contract RESTART and run-as kill both failed")
                    return ActionResult("restart_target", False, "; ".join(notes), time.monotonic() - started)
                notes.append("killed it with run-as")
            else:
                notes.append("restarted through the health contract")
        result = self.device.shell(_commands("relaunch_target", self.target)[0], timeout=30.0)
        ok = result.ok and "Error" not in result.stdout
        notes.append(result.stdout.strip())
        return ActionResult("restart_target", ok, "; ".join(n for n in notes if n), time.monotonic() - started)
