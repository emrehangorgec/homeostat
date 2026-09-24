"""Fault injection scenarios.

Categories are the columns of the results matrix: known, variant, composite, held_out. The held_out
column stays empty until rules, prompts and policy are frozen (M4); it is filled by the
adversary, never by hand-written scenarios in this file.

`hooked` faults need something adb cannot do to an arbitrary app ("crash on the next N
starts", "lose the content but stay alive"). They run on the simulator, or on a real
device when the target is the homeostat kiosk, through its debug-build
DebugFaultReceiver.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from homeostat.device.base import Device
from homeostat.device.sim import SimDevice
from homeostat.state.schema import ActivityRef

Category = Literal["known", "variant", "composite", "held_out"]
Injector = Callable[[Device, ActivityRef], str]


@dataclass(frozen=True)
class Fault:
    id: str
    category: Category
    description: str
    expected_incident: str | None
    inject: Injector
    hooked: bool = False


def _crash(device: Device, target: ActivityRef) -> str:
    pid = device.shell(f"pidof {target.process_name}").stdout.split()
    if not pid:
        return "target process not running, nothing to crash"
    result = device.shell(f"am crash {pid[0]}")
    if result.ok and "Unknown" not in result.stdout and "Error" not in result.stdout:
        return "am crash"
    device.shell(f"am force-stop {target.package}")
    return "fallback: am crash unavailable, used force-stop"


def _repeated_crash(device: Device, target: ActivityRef) -> str:
    first = _crash(device, target)
    device.shell(f"am start -n {target.component}")
    if not isinstance(device, SimDevice):
        time.sleep(3)  # let the process come back before crashing it again
    return f"{first}, relaunch, {_crash(device, target)}"


def _kill(device: Device, target: ActivityRef) -> str:
    device.shell(f"am force-stop {target.package}")
    return "am force-stop"


def _wrong_foreground(device: Device, target: ActivityRef) -> str:
    device.shell("am start -a android.settings.SETTINGS")
    return "opened Settings over the target"


def _screen_off(device: Device, target: ActivityRef) -> str:
    device.shell("input keyevent KEYCODE_SLEEP")
    return "KEYCODE_SLEEP"


def _crash_then_screen_off(device: Device, target: ActivityRef) -> str:
    return f"{_crash(device, target)} + {_screen_off(device, target)}"


HOMEOSTAT_KIOSK = "com.homeostat.agent"
_DEBUG_FAULT = f"am broadcast -n {HOMEOSTAT_KIOSK}/.DebugFaultReceiver"


def supports_hooks(device: Device, target: ActivityRef) -> bool:
    return isinstance(device, SimDevice) or target.package == HOMEOSTAT_KIOSK


def _hook(device: Device, target: ActivityRef, extras: str) -> str:
    if not supports_hooks(device, target):
        raise RuntimeError("hooked faults need the simulator or the homeostat kiosk as target")
    if isinstance(device, SimDevice):
        if "crash_on_start" in extras:
            device.s.crash_on_start = int(extras.split()[-1])
        if "blank_ui" in extras:
            device.s.blank_ui = True
        return "sim hook"
    out = device.shell(f"{_DEBUG_FAULT} {extras}").stdout.strip().splitlines()
    return out[-1] if out else "no output"


def _crash_loop(device: Device, target: ActivityRef) -> str:
    _hook(device, target, "--ei crash_on_start 3")
    return _crash(device, target) + ", next 3 starts crash"


def _blank_ui(device: Device, target: ActivityRef) -> str:
    return _hook(device, target, "--ez blank_ui true")


FAULTS: dict[str, Fault] = {
    f.id: f
    for f in [
        Fault("app_crash", "known", "Target app crashes.", "app_crash", _crash),
        Fault("app_killed", "variant", "Target app is force stopped (no crash log).", "app_crash", _kill),
        Fault(
            "repeated_crash",
            "variant",
            "Target crashes twice in quick succession; Android covers it with a crash dialog.",
            "blocking_dialog",
            _repeated_crash,
        ),
        Fault("wrong_foreground", "known", "Another app covers the target.", "wrong_foreground", _wrong_foreground),
        Fault("screen_off", "known", "Display goes to sleep.", "screen_off", _screen_off),
        Fault(
            "crash_then_screen_off",
            "composite",
            "Target crashes and the display sleeps.",
            "app_crash",
            _crash_then_screen_off,
        ),
        Fault("crash_loop", "variant", "Target crashes on every start.", "app_crash", _crash_loop, hooked=True),
        Fault(
            "blank_ui",
            "known",
            "Target is up and in front but its content never loads.",
            "stuck_loading",
            _blank_ui,
            hooked=True,
        ),
    ]
}


def reset_hooks(device: Device, target: ActivityRef) -> None:
    """Clear hooked fault state between runs."""
    if isinstance(device, SimDevice):
        device.s.crash_on_start = 0
        device.s.blank_ui = False
    elif target.package == HOMEOSTAT_KIOSK:
        device.shell(f"{_DEBUG_FAULT} --ez reset true")
