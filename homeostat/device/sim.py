"""A simulated Android device that answers the same shell commands as a real one.

It exists so the loop, parsers, policy and eval runner can be exercised without a
phone (tests, CI, `homeostat demo`). It is NOT evidence: results measured on the
simulator must never be reported as device results.

Output formats mirror Android 10 (API 29) as seen on the reference OnePlus 5T.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from homeostat.device.base import DeviceUnreachable, ShellResult
from homeostat.state.schema import ActivityRef
from homeostat.verify.oracle import UiMarker

LAUNCHER = "net.oneplus.launcher/.Launcher"
_REPEAT_CRASH_TICKS = 10
SETTINGS = "com.android.settings/.Settings"


@dataclass
class SimState:
    target: str  # component, "pkg/full.Activity"
    target_process: str  # process the target activity runs in
    running: dict[str, int] = field(default_factory=dict)  # process name -> pid
    foreground: str = LAUNCHER
    awake: bool = True
    wifi: bool = True
    internet: bool = True
    battery_level: int = 80
    battery_temp_tenths: int = 300
    charging: bool = True
    mem_available_kb: int = 2_048_000
    crash_log: list[tuple[int, str]] = field(default_factory=list)  # (tick, line)
    # Fault knobs the simulator exposes beyond what adb can do on a stock device.
    crash_on_start: int = 0  # the next N starts of the target crash immediately
    blank_ui: bool = False  # target runs and is in front, but its UI marker never appears
    # Android shows "<app> keeps stopping" when the target crashes again soon after a crash.
    error_dialog: bool = False
    # A swipe lock screen, as on the reference device: it appears whenever the display sleeps.
    keyguard_enabled: bool = True
    keyguard: bool = False
    last_target_crash_tick: int | None = None


class SimDevice:
    serial = "sim-0001"

    def __init__(self, target: ActivityRef, marker: UiMarker | None = None, start_target: bool = True):
        self.s = SimState(target=target.component, target_process=target.process_name)
        self.marker = marker or UiMarker(content_desc="homeostat-ready")
        self._pids = itertools.count(4000)
        self._tick = 0
        self.log: list[str] = []  # every command received, for assertions
        if start_target:
            self._start(self.s.target)

    # -- shell entry point --------------------------------------------------

    unplugged: bool = False  # simulates the host losing the adb link

    def shell(self, command: str, timeout: float = 10.0) -> ShellResult:
        if self.unplugged:
            raise DeviceUnreachable("adb: no devices/emulators found")
        self._tick += 1
        self.log.append(command)
        outputs, rc = [], 0
        for sub in command.split("; "):
            out, rc = self._one(sub.strip())
            if out:
                outputs.append(out)
        return ShellResult("\n".join(outputs) + ("\n" if outputs else ""), rc)

    # -- helpers -------------------------------------------------------------

    def _ts(self, tick: int | None = None) -> str:
        t = self._tick if tick is None else tick
        return f"09-24 {(t // 3600) % 24:02d}:{(t // 60) % 60:02d}:{t % 60:02d}.000"

    def _process_of(self, component: str) -> str:
        return self.s.target_process if component == self.s.target else component.split("/")[0]

    def _start(self, component: str) -> None:
        process = self._process_of(component)
        self.s.running.setdefault(process, next(self._pids))
        if component == self.s.target and self.s.crash_on_start > 0:
            self.s.crash_on_start -= 1
            self._crash(process, "java.lang.IllegalStateException: sim crash on start")
            return
        self.s.foreground = component

    def _drop(self, process: str) -> None:
        self.s.running.pop(process, None)
        if self._process_of(self.s.foreground) == process:
            self.s.foreground = LAUNCHER

    def _kill(self, pkg: str) -> None:
        for process in [p for p in self.s.running if p == pkg or p.startswith(pkg + ":")]:
            self._drop(process)

    def _crash(self, process: str, exception: str) -> None:
        pid = self.s.running.get(process)
        if pid is None:
            return
        prefix = f"{self._ts()}  {pid}  {pid} E AndroidRuntime: "
        for line in ("FATAL EXCEPTION: main", f"Process: {process}, PID: {pid}", exception):
            self.s.crash_log.append((self._tick, prefix + line))
        self._drop(process)
        if process == self.s.target_process:
            last = self.s.last_target_crash_tick
            if last is not None and self._tick - last <= _REPEAT_CRASH_TICKS:
                self.s.error_dialog = True
            self.s.last_target_crash_tick = self._tick

    def _resumed_line(self) -> str:
        if not self.s.awake:
            return "    mResumedActivity: null"
        return f"    mResumedActivity: ActivityRecord{{a1b2c3 u0 {self.s.foreground} t12}}"

    def _focus_line(self) -> str:
        if not self.s.awake:
            return "  mCurrentFocus=null"
        if self.s.keyguard:
            return "  mCurrentFocus=Window{d9da72 u0 StatusBar}"
        if self.s.error_dialog:
            return f"  mCurrentFocus=Window{{4a22345 u0 Application Error: {self.s.target.split('/')[0]}}}"
        pkg, activity = self.s.foreground.split("/")
        full = pkg + activity if activity.startswith(".") else activity
        return f"  mCurrentFocus=Window{{990455c u0 {pkg}/{full}}}"

    def _ui_dump(self) -> str:
        nodes = []
        if self.s.error_dialog:
            nodes.append('<node index="0" text="homeostat sürekli olarak duruyor" resource-id="android:id/alertTitle" />')
            nodes.append('<node index="1" text="Uygulamayı kapat" resource-id="android:id/aerr_close" />')
        elif self.s.keyguard:
            nodes.append('<node index="0" text="" resource-id="com.android.systemui:id/keyguard_root" />')
        elif self.s.awake and self.s.foreground == self.s.target and not self.s.blank_ui:
            m = self.marker
            nodes.append(
                f'<node index="0" text="{m.text or ""}" resource-id="{m.resource_id or ""}" '
                f'content-desc="{m.content_desc or ""}" />'
            )
        return "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">" + "".join(
            nodes
        ) + "</hierarchy>"

    def _battery(self) -> str:
        return "\n".join(
            [
                "Current Battery Service state:",
                f"  AC powered: {'true' if self.s.charging else 'false'}",
                "  USB powered: false",
                f"  status: {2 if self.s.charging else 3}",
                "  health: 2",
                f"  level: {self.s.battery_level}",
                f"  temperature: {self.s.battery_temp_tenths}",
            ]
        )

    def _logcat_since(self, arg: str) -> str:
        if arg == "1":
            lines = self.s.crash_log[-1:]
        else:
            since = self._parse_ts(arg)
            lines = [entry for entry in self.s.crash_log if entry[0] >= since]
        return "\n".join(line for _, line in lines)

    @staticmethod
    def _parse_ts(ts: str) -> int:
        m = re.match(r"\d\d-\d\d (\d\d):(\d\d):(\d\d)", ts)
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else 0

    # -- command table -------------------------------------------------------

    def _one(self, cmd: str) -> tuple[str, int]:
        s = self.s
        if cmd.startswith("echo "):
            return cmd[5:], 0
        if cmd.startswith("date"):
            return self._ts(), 0
        if cmd == "getprop ro.product.model":
            return "SimPhone", 0
        if cmd == "getprop ro.build.version.sdk":
            return "29", 0
        if cmd.startswith("dumpsys activity activities"):
            return self._resumed_line(), 0
        if cmd.startswith("pidof "):
            pid = s.running.get(cmd.split()[1])
            return (str(pid), 0) if pid else ("", 1)
        if cmd.startswith("dumpsys window policy"):
            return f"    KeyguardServiceDelegate\n      showing={'true' if s.keyguard else 'false'}", 0
        if cmd.startswith("dumpsys window"):
            return self._focus_line(), 0
        if cmd == "wm dismiss-keyguard":
            s.keyguard = False
            return "", 0
        if cmd == "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS":
            if s.error_dialog:
                s.error_dialog = False  # closing the crash dialog ends the app
                self._kill(s.target.split("/")[0])
            return "Broadcast completed: result=0", 0
        if cmd.startswith("dumpsys power"):
            return f"  mWakefulness={'Awake' if s.awake else 'Asleep'}", 0
        if cmd == "settings get global wifi_on":
            return ("1" if s.wifi else "0"), 0
        if cmd.startswith("ping "):
            return ("1" if (s.wifi and s.internet) else "0"), 0
        if cmd == "dumpsys battery":
            return self._battery(), 0
        if cmd.startswith("grep MemAvailable"):
            return f"MemAvailable:    {s.mem_available_kb} kB", 0
        if cmd.startswith("logcat -b crash"):
            m = re.search(r"-t '?([^']+)'?$", cmd)
            return self._logcat_since(m.group(1) if m else "1"), 0
        if cmd.startswith("uiautomator dump"):
            return self._ui_dump(), 0
        if cmd.startswith("am start -n "):
            component = cmd.split()[3]
            self._start(component)
            return f"Starting: Intent {{ cmp={component} }}", 0
        if cmd.startswith("monkey -p "):
            self._start(s.target)
            return "Events injected: 1", 0
        if cmd == "am start -a android.settings.SETTINGS":
            self._start(SETTINGS)
            return "Starting: Intent { act=android.settings.SETTINGS }", 0
        if cmd.startswith("am force-stop "):
            self._kill(cmd.split()[2])
            return "", 0
        if cmd.startswith("am crash "):
            arg = cmd.split()[2]
            if arg.isdigit():
                names = [p for p, pid in s.running.items() if str(pid) == arg]
            else:
                names = [p for p in s.running if p == arg or p.startswith(arg + ":")]
            for name in names:
                self._crash(name, "java.lang.RuntimeException: am crash")
            return "", 0
        if cmd.startswith("pm clear "):
            self._kill(cmd.split()[2])
            return "Success", 0
        if cmd == "input keyevent KEYCODE_WAKEUP":
            s.awake = True
            return "", 0
        if cmd == "input keyevent KEYCODE_SLEEP":
            s.awake = False
            s.keyguard = s.keyguard_enabled
            return "", 0
        if cmd.startswith("svc wifi "):
            s.wifi = cmd.endswith("enable")
            return "", 0
        if cmd == "reboot":
            s.running.clear()
            s.foreground, s.awake = LAUNCHER, True
            return "", 0
        return f"/system/bin/sh: {cmd.split()[0]}: not found", 127
