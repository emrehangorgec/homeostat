"""Collect a DeviceState from a device in a single shell round trip.

All probes run in one `adb shell` invocation separated by markers, because detection
latency is a reported metric and each adb round trip costs hundreds of milliseconds.
A probe that fails leaves its field as None (unknown), never as a guessed value.

Two probes carry state across ticks through a device-time watermark: crash lines and
health contract lines. The screenshot probe is expensive, so it runs only every
`visual_every_s` seconds and reports None in between.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from homeostat.device.base import Device
from homeostat.state import parsers
from homeostat.state.schema import (
    ActivityRef,
    AppHealth,
    DeviceInfo,
    DeviceState,
    Memory,
    Mode,
    Network,
    ProcessInfo,
    Screen,
)

_MARK = "__homeostat__"


HEALTH_TAG = "homeostat-health"


def _probes(process: str, since: str | None) -> dict[str, str]:
    crash_cmd = f"logcat -b crash -v threadtime -t '{since}'" if since else "logcat -b crash -v threadtime -t 1"
    health_cmd = (
        f"logcat -d -v threadtime -s {HEALTH_TAG}:I -t '{since}'"
        if since
        else f"logcat -d -v threadtime -s {HEALTH_TAG}:I | tail -n 1"
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
        "health": health_cmd,
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
        visual_every_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.device = device
        self.target = target
        self.mode = mode
        self.error_window_s = error_window_s
        self.visual_every_s = visual_every_s
        self.clock = clock
        self._since: str | None = None  # device time of the previous tick
        self._errors: deque[tuple[float, str]] = deque(maxlen=20)
        self._seen_crashes: deque[str] = deque(maxlen=200)  # the watermark has 1 s resolution
        self._health: tuple[float, dict] | None = None  # (device seconds of the line, payload)
        self._next_visual = 0.0

    def collect(self) -> DeviceState:
        probes = _probes(self.target.process_name, self._since)
        result = self.device.shell(build_script(probes), timeout=20.0)
        s = split_sections(result.stdout)

        now_mono = self.clock()
        if self._since is not None:
            for key, summary in parsers.parse_crash_events(s.get("crash", ""), self.target.process_name):
                if key not in self._seen_crashes:
                    self._seen_crashes.append(key)
                    self._errors.append((now_mono, summary))
        while self._errors and now_mono - self._errors[0][0] > self.error_window_s:
            self._errors.popleft()

        device_now = s.get("now", "").strip()
        latest = parsers.parse_health_lines(s.get("health", ""))
        if latest is not None:
            stamp = parsers.logcat_seconds(latest[0])
            if stamp is not None:
                self._health = (stamp, latest[1])
        app = self._app_health(parsers.logcat_seconds(device_now) if device_now else None)
        self._since = device_now or self._since

        blank = None
        if self.visual_every_s > 0 and now_mono >= self._next_visual:
            self._next_visual = now_mono + self.visual_every_s
            blank = parsers.screen_is_flat(self.device.shell_bytes("screencap"))

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
                blank=blank,
            ),
            network=Network(
                wifi_enabled=parsers.parse_setting_bool(s.get("wifi", "")),
                internet_reachable=parsers.parse_setting_bool(s.get("ping", "")),
            ),
            battery=parsers.parse_battery(s.get("battery", "")),
            memory=Memory(available_mb=parsers.parse_mem_available_mb(s.get("meminfo", ""))),
            recent_errors=[summary for _, summary in self._errors],
            app=app,
        )

    def _app_health(self, device_now: float | None) -> AppHealth:
        if self._health is None:
            return AppHealth()
        stamp, data = self._health
        heartbeat_age = max(0.0, device_now - stamp) if device_now is not None else None
        age_ms = data.get("age_ms")
        state_age = (
            age_ms / 1000 + heartbeat_age
            if isinstance(age_ms, (int, float)) and heartbeat_age is not None
            else None
        )
        state = data.get("state")
        http = data.get("http")
        return AppHealth(
            state=state if state in ("loading", "ready", "error", "auth_error", "app_error") else None,
            detail=data.get("detail") or None,
            http_status=http if isinstance(http, int) else None,
            state_age_s=state_age,
            heartbeat_age_s=heartbeat_age,
            declared=data.get("declared") if isinstance(data.get("declared"), bool) else None,
        )

    def acknowledge_errors(self) -> None:
        """Forget crash lines that belong to an incident that has been closed."""
        self._errors.clear()
