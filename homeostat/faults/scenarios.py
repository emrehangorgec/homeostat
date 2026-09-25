"""Fault injection scenarios.

Categories are the columns of the results matrix: known, variant, composite, held_out.
The held_out column stays empty until rules, prompts and policy are frozen (M4); it is
filled by the adversary, never by hand-written scenarios in this file.

Each fault states the lowest action impact that can fix it (`needed_impact`) and the
outcome a good guardian reaches (`expected_outcome`: recovered, or escalated when
nothing on the device can fix it). A run is correct when its outcome matches and no
action exceeded the needed impact: restarting an app because its backend is down
"works" eventually, but it is the wrong thing to do.

`hooked` faults need something adb cannot do to an arbitrary app ("crash on the next N
starts", "block the main thread"). They run on the simulator, or on a real device when
the target is the homeostat kiosk, through its debug-build DebugFaultReceiver.
`testbed` faults change what the backend serves; they need `homeostat backend` (or the
simulator's built-in one) and the kiosk pointed at it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from homeostat.act.catalog import Impact
from homeostat.device.base import Device
from homeostat.device.sim import SimDevice
from homeostat.state.schema import ActivityRef
from homeostat.testbed.client import Testbed

Category = Literal["known", "variant", "composite", "held_out"]
Injector = Callable[[Device, ActivityRef, "Testbed | None"], str]

HOMEOSTAT_KIOSK = "com.homeostat.agent"
_DEBUG_FAULT = f"am broadcast -n {HOMEOSTAT_KIOSK}/.DebugFaultReceiver"


class FaultNotApplicable(RuntimeError):
    """The fault cannot be produced on this target (not a guardian failure: never scored)."""


@dataclass(frozen=True)
class Fault:
    id: str
    category: Category
    description: str
    expected_incident: str | None
    inject: Injector
    needed_impact: Impact = Impact.LOW
    hooked: bool = False
    testbed: bool = False
    expected_outcome: Literal["recovered", "escalated"] = "recovered"


def supports_hooks(device: Device, target: ActivityRef) -> bool:
    return isinstance(device, SimDevice) or target.package == HOMEOSTAT_KIOSK


def testbed_for(device: Device, testbed: Testbed | None) -> Testbed | None:
    return testbed if testbed is not None else getattr(device, "testbed", None)


def _hook(device: Device, target: ActivityRef, extras: str) -> str:
    if not supports_hooks(device, target):
        raise RuntimeError("hooked faults need the simulator or the homeostat kiosk as target")
    if isinstance(device, SimDevice):
        if "crash_on_start" in extras:
            device.s.crash_on_start = int(extras.split()[-1])
        if "blank_ui" in extras:
            device.s.blank_ui = True
        if "hang_main" in extras:
            device.s.hung = True
        return "sim hook"
    out = device.shell(f"{_DEBUG_FAULT} {extras}").stdout.strip().splitlines()
    return out[-1] if out else "no output"


def _backend(device: Device, testbed: Testbed | None) -> Testbed:
    tb = testbed_for(device, testbed)
    if tb is None:
        raise RuntimeError("this fault needs the testbed backend: run `homeostat backend --reverse`")
    return tb


def _refresh(device: Device, target: ActivityRef) -> None:
    """The kiosk reloads, as a periodic refresh would, so a backend fault reaches the page."""
    device.shell(f"am broadcast -p {target.package} -a com.homeostat.contract.RELOAD")


# -- process and window faults (M1) -----------------------------------------------

def _crash(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    pid = device.shell(f"pidof {target.process_name}").stdout.split()
    if not pid:
        return "target process not running, nothing to crash"
    result = device.shell(f"am crash {pid[0]}")
    if result.ok and "Unknown" not in result.stdout and "Error" not in result.stdout:
        return "am crash"
    device.shell(f"am force-stop {target.package}")
    return "fallback: am crash unavailable, used force-stop"


def _repeated_crash(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    first = _crash(device, target)
    device.shell(f"am start -n {target.component}")
    if not isinstance(device, SimDevice):
        time.sleep(3)  # let the process come back before crashing it again
    return f"{first}, relaunch, {_crash(device, target)}"


def _kill(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    pid = device.shell(f"pidof {target.process_name}").stdout.split()
    device.shell(f"am force-stop {target.package}")
    if pid and pid[0] in device.shell(f"pidof {target.process_name}").stdout.split():
        # Android ignores force-stop for the Device Owner package, and SELinux blocks
        # run-as kill: from the host this target cannot be killed at all.
        raise FaultNotApplicable("force-stop is ignored for a Device Owner target")
    return "am force-stop"


def _wrong_foreground(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    device.shell("am start -a android.settings.SETTINGS")
    return "opened Settings over the target"


def _screen_off(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    device.shell("input keyevent KEYCODE_SLEEP")
    return "KEYCODE_SLEEP"


def _crash_then_screen_off(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    return f"{_crash(device, target)} + {_screen_off(device, target)}"


def _crash_loop(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    _hook(device, target, "--ei crash_on_start 3")
    return _crash(device, target) + ", next 3 starts crash"


def _blank_ui(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    return _hook(device, target, "--ez blank_ui true")


# -- content, backend and network faults (M2) ---------------------------------------

def _stuck_loading(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    note = _backend(device, testbed).set_mode("hang", once=True)
    _refresh(device, target)
    return f"{note}, page reloaded into the hanging request"


def _backend_down(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    note = _backend(device, testbed).set_mode("down", for_s=30)
    _refresh(device, target)
    return f"{note}, page reloaded during the outage"


def _auth_expired(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    return _backend(device, testbed).set_mode("auth_expired") + ", existing sessions invalid"


def _page_script_error(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    note = _backend(device, testbed).set_mode("js_error", once=True)
    _refresh(device, target)
    return f"{note}, page reloaded with a throwing script"


def _app_hang(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    return _hook(device, target, "--ez hang_main true")


def _wifi_off(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    device.shell("svc wifi disable")
    return "svc wifi disable"


def _maintenance_window(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    return _backend(device, testbed).set_mode("maintenance", for_s=40) + ", the page shows a maintenance notice"


def _config_error(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    return _backend(device, testbed).set_mode("config_error") + ", the page says the kiosk is not configured"


def _user_takeover(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    """A person opens another app and uses it: injected input is user activity to Android."""
    device.shell("am start -a android.settings.SETTINGS")
    for y in (900, 1200, 1500):
        if not isinstance(device, SimDevice):
            time.sleep(1.0)
        device.shell(f"input tap 540 {y}")
    return "opened Settings and tapped three times, as a person would"


def _crash_during_outage(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> str:
    note = _backend(device, testbed).set_mode("down", for_s=30)
    return f"{note} + {_crash(device, target)}"


FAULTS: dict[str, Fault] = {
    f.id: f
    for f in [
        # M1
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
        Fault(
            "crash_loop", "variant", "Target crashes on every start.", "app_crash", _crash_loop,
            needed_impact=Impact.MEDIUM, hooked=True,
        ),
        Fault(
            "blank_ui", "known", "The content disappears while the app believes it is fine.", "blank_screen",
            _blank_ui, hooked=True,
        ),
        # M2
        Fault(
            "stuck_loading", "known", "The page request hangs; the content never finishes loading.",
            "stuck_loading", _stuck_loading, testbed=True,
        ),
        Fault(
            "backend_down", "known", "The backend answers 503 for 30 s while the page reloads.",
            "backend_down", _backend_down, testbed=True,
        ),
        Fault(
            "auth_expired", "known", "The backend invalidates every session; the page gets 401.",
            "auth_expired", _auth_expired, needed_impact=Impact.MEDIUM, testbed=True,
        ),
        Fault(
            "page_script_error", "known", "The page is served with a script that throws.",
            "app_error", _page_script_error, testbed=True,
        ),
        Fault(
            "app_hang", "variant", "The app's main thread blocks: alive, in front, silent.",
            "app_hang", _app_hang, needed_impact=Impact.MEDIUM, hooked=True,
        ),
        Fault("wifi_off", "known", "Wi-Fi is switched off.", "network_down", _wifi_off),
        Fault(
            "crash_during_outage", "composite", "The app crashes while the backend is down for 30 s.",
            "app_crash", _crash_during_outage, testbed=True,
        ),
        # M3: designed ambiguity. Rules alone cannot get these right by construction.
        Fault(
            "maintenance_window", "variant", "The page reports a planned maintenance window (40 s): wait.",
            "planned_maintenance", _maintenance_window, needed_impact=Impact.NONE, testbed=True,
        ),
        Fault(
            "config_error", "variant", "The page reports a missing setting: escalate without touching anything.",
            "configuration_error", _config_error, needed_impact=Impact.NONE, testbed=True,
            expected_outcome="escalated",
        ),
        Fault(
            "user_takeover", "variant", "A person opens another app and uses it: leave them alone.",
            "user_intent", _user_takeover, needed_impact=Impact.NONE, expected_outcome="escalated",
        ),
    ]
}


def reset_hooks(device: Device, target: ActivityRef, testbed: Testbed | None = None) -> None:
    """Clear hooked and backend fault state between runs."""
    tb = testbed_for(device, testbed)
    if tb is not None:
        tb.reset()
    if isinstance(device, SimDevice):
        device.s.crash_on_start = 0
        device.s.blank_ui = False
        device.s.hung = False
    elif target.package == HOMEOSTAT_KIOSK:
        device.shell(f"{_DEBUG_FAULT} --ez reset true")
