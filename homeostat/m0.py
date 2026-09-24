"""M0 feasibility probe: what can this device actually do?

Runs the agent's ProbeReceiver (Device Owner capabilities) and then checks, from the
host, the assumptions the M1 loop makes: background activity start, `am crash`, the UI
dump and the kiosk health marker. The device capability table and the action
allowlist come from this output, not from documentation.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from homeostat.device.base import Device
from homeostat.state import parsers
from homeostat.verify.oracle import UiMarker

PKG = "com.homeostat.agent"
KIOSK = f"{PKG}/.KioskActivity"
PROBE = f"am broadcast -n {PKG}/.ProbeReceiver"
MARKER = UiMarker(content_desc="homeostat-ready")


def _broadcast_data(stdout: str) -> str | None:
    match = re.search(r'data="(.*)"\s*$', stdout.strip(), re.DOTALL)
    return match.group(1) if match else None


def _foreground(device: Device) -> str | None:
    ref = parsers.parse_resumed_activity(device.shell("dumpsys activity activities | grep -E 'ResumedActivity'").stdout)
    return ref.component if ref else None


def _entry(ok: bool, detail: str) -> dict[str, Any]:
    return {"ok": ok, "detail": detail}


def run(device: Device, disruptive: bool = False, hide_pkg: str | None = None, reboot: bool = False,
        sleep=time.sleep) -> dict[str, Any]:
    results: dict[str, Any] = {}

    installed = PKG in device.shell(f"pm list packages {PKG}").stdout
    results["agent_installed"] = _entry(installed, "yes" if installed else "adb install -t agent-debug.apk first")
    if not installed:
        return results

    extras = ""
    if disruptive:
        extras += " --ez disruptive true"
    if hide_pkg:
        extras += f" --es hide_pkg {hide_pkg}"
    out = device.shell(f"{PROBE} -a com.homeostat.agent.PROBE{extras}", timeout=60).stdout
    data = _broadcast_data(out)
    if data is None:
        results["probe"] = _entry(False, f"no result data: {out.strip()[:200]}")
    else:
        probe = json.loads(data)
        results["sdk"] = probe.pop("sdk", None)
        results["model"] = probe.pop("model", None)
        results.update(probe)

    # Background activity start (Android 10 restricts it; Device Owner should be exempt).
    device.shell("input keyevent KEYCODE_WAKEUP")
    device.shell("input keyevent KEYCODE_HOME")
    sleep(2)
    device.shell(f"{PROBE} -a com.homeostat.agent.LAUNCH_KIOSK")
    sleep(4)
    fg = _foreground(device)
    in_front = fg is not None and fg.startswith(f"{PKG}/") and fg.endswith("KioskActivity")
    results["background_activity_start"] = _entry(in_front, f"foreground after launch: {fg}")

    if not in_front:
        device.shell(f"am start -n {KIOSK}")
        sleep(4)

    xml = device.shell("uiautomator dump /sdcard/homeostat_ui.xml >/dev/null 2>&1 && cat /sdcard/homeostat_ui.xml",
                       timeout=30).stdout
    dump_ok = "<hierarchy" in xml
    results["uiautomator_dump"] = _entry(dump_ok, f"{len(xml)} bytes" if dump_ok else xml.strip()[:200])
    marker = dump_ok and bool(MARKER.pattern().search(xml))
    results["kiosk_ready_marker"] = _entry(marker, "found" if marker else "homeostat-ready not in the UI tree")

    pid_before = parsers.parse_pidof(device.shell(f"pidof {PKG}:kiosk").stdout)
    crash = device.shell(f"am crash {pid_before}")
    sleep(2)
    pid_after = parsers.parse_pidof(device.shell(f"pidof {PKG}:kiosk").stdout)
    crash_ok = crash.ok and "Unknown" not in crash.stdout and pid_before is not None and pid_after != pid_before
    results["am_crash"] = _entry(crash_ok, f"pid {pid_before} -> {pid_after}; {crash.stdout.strip()[:120]}")

    admin_pid = parsers.parse_pidof(device.shell(f"pidof {PKG}").stdout)
    results["kiosk_process_isolated"] = _entry(
        admin_pid is not None and admin_pid != pid_before,
        f"main process {admin_pid}, kiosk process {pid_before}",
    )
    device.shell(f"am start -n {KIOSK}")

    if reboot:
        device.shell(f"{PROBE} -a com.homeostat.agent.PROBE --ez reboot true", timeout=30)
        results["reboot"] = _entry(True, "reboot requested; confirm the device restarts")
    return results


def table(results: dict[str, Any]) -> str:
    lines = [f"Device: {results.get('model')} (API {results.get('sdk')})", "",
             "| capability | ok | detail |", "| --- | --- | --- |"]
    for name, value in results.items():
        if isinstance(value, dict):
            lines.append(f"| {name} | {'yes' if value['ok'] else 'NO'} | {value['detail']} |")
    return "\n".join(lines)


def save(results: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
