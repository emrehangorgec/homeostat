"""Collect a DeviceState from a device in a single shell round trip.

All probes run in one `adb shell` invocation separated by markers, because detection
latency is a reported metric and each adb round trip costs hundreds of milliseconds.
A probe that fails leaves its field as None (unknown), never as a guessed value.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

from homeostat.device.base import Device
from homeostat.state import parsers
from homeostat.state.schema import (
    ActivityRef,
    DeviceInfo,
    DeviceState,
    Memory,
    Mode,
    Network,
    ProcessInfo,
    Screen,
)

_MARK = "__homeostat__"


def _probes(process: str, crash_since: str | None) -> dict[str, str]:
    crash_cmd = (
        f"logcat -b crash -v threadtime -t '{crash_since}'" if crash_since else "logcat -b crash -v threadtime -t 1"
    )
    return {
        "now": "date +'%m-%d %H:%M:%S.000'",
        "model": "getprop ro.product.model",
        "sdk": "getprop ro.build.version.sdk",
        "resumed": "dumpsys activity activities | grep -E 'ResumedActivity'",
        "pid": f"pidof {process}",
        "power": "dumpsys power | grep mWakefulness=",
        "focus": "dumpsys window | grep mCurrentFocus=",
        "keyguard": "dumpsys window policy | grep -A1 KeyguardServiceDelegate",
        "wifi": "settings get global wifi_on",
        "ping": "ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 && echo 1 || echo 0",
        "battery": "dumpsys battery",
        "meminfo": "grep MemAvailable /proc/meminfo",
        "crash": crash_cmd,
    }


def build_script(probes: dict[str, str]) -> str:
    return "; ".join(f"echo {_MARK}{name}; {cmd}" for name, cmd in probes.items())


def split_sections(stdout: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        if line.startswith(_MARK):
            current = line[len(_MARK) :].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


class Collector:
    def __init__(
        self,
        device: Device,
        target: ActivityRef,
        mode: Mode = Mode.PANEL,
        error_window_s: float = 120.0,
    ):
        self.device = device
        self.target = target
        self.mode = mode
        self.error_window_s = error_window_s
        self._crash_since: str | None = None
        self._errors: deque[tuple[float, str]] = deque(maxlen=20)

    def collect(self) -> DeviceState:
        probes = _probes(self.target.process_name, self._crash_since)
        result = self.device.shell(build_script(probes), timeout=20.0)
        s = split_sections(result.stdout)

        now_mono = time.monotonic()
        crash_text = s.get("crash", "")
        if self._crash_since is not None:
            for summary in parsers.parse_crashes(crash_text, self.target.process_name):
                self._errors.append((now_mono, summary))
        self._crash_since = s.get("now", "").strip() or self._crash_since
        while self._errors and now_mono - self._errors[0][0] > self.error_window_s:
            self._errors.popleft()

        sdk = s.get("sdk", "").strip()
        pid = parsers.parse_pidof(s.get("pid", ""))
        return DeviceState(
            captured_at=datetime.now(timezone.utc),
            mode=self.mode,
            device=DeviceInfo(
                serial=self.device.serial,
                model=s.get("model", "").strip() or None,
                sdk=int(sdk) if sdk.isdigit() else None,
            ),
            target=self.target,
            foreground=parsers.parse_resumed_activity(s.get("resumed", "")),
            target_process=ProcessInfo(running=pid is not None if "pid" in s else None, pid=pid),
            screen=Screen(
                awake=parsers.parse_wakefulness(s.get("power", "")),
                focus_window=parsers.parse_current_focus(s.get("focus", "")),
                keyguard_showing=parsers.parse_keyguard_showing(s.get("keyguard", "")),
            ),
            network=Network(
                wifi_enabled=parsers.parse_setting_bool(s.get("wifi", "")),
                internet_reachable=parsers.parse_setting_bool(s.get("ping", "")),
            ),
            battery=parsers.parse_battery(s.get("battery", "")),
            memory=Memory(available_mb=parsers.parse_mem_available_mb(s.get("meminfo", ""))),
            recent_errors=[summary for _, summary in self._errors],
        )

    def acknowledge_errors(self) -> None:
        """Forget crash lines that belong to an incident that has been closed."""
        self._errors.clear()
