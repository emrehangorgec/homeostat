"""A simulated Android device that answers the same shell commands as a real one.

It exists so the loop, parsers, policy and eval runner can be exercised without a
phone (tests, CI, `homeostat demo`). It is NOT evidence: results measured on the
simulator must never be reported as device results.

Output formats mirror Android 10 (API 29) as seen on the reference OnePlus 5T. The
target is modelled as the homeostat web kiosk: it loads a page from a simulated
testbed backend and publishes health contract v1 lines.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from homeostat.device.base import DeviceUnreachable, ShellResult
from homeostat.state.schema import ActivityRef
from homeostat.verify.oracle import UiMarker

LAUNCHER = "net.oneplus.launcher/.Launcher"
SETTINGS = "com.android.settings/.Settings"
_REPEAT_CRASH_TICKS = 10
_JS_POLL_S = 5.0
_SCREEN = (64, 128)


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
    crash_log: list[tuple[float, str]] = field(default_factory=list)  # (device seconds, line)
    # Fault knobs the simulator exposes beyond what adb can do on a stock device.
    crash_on_start: int = 0  # the next N starts of the target crash immediately
    blank_ui: bool = False  # content gone while the kiosk believes it is fine
    hung: bool = False  # the kiosk's main thread is blocked: no heartbeat
    # Android shows "<app> keeps stopping" when the target crashes again soon after a crash.
    error_dialog: bool = False
    # A swipe lock screen, as on the reference device: it appears whenever the display sleeps.
    keyguard_enabled: bool = True
    keyguard: bool = False
    last_target_crash_tick: int | None = None
    # The kiosk's content, driven by the simulated testbed backend.
    contract: bool = True  # the target publishes health contract lines
    # The target is the Device Owner package: Android ignores `am force-stop` for it.
    owner_protected: bool = True
    backend: str = "ok"
    backend_once: bool = False
    backend_revert_at: float | None = None
    session_valid: bool = True
    page_loaded: bool = False
    pending_hang: bool = False  # the page request is hanging
    pending_hang_once: bool = False  # ...only that request: a reload is needed
    app_state: str = "loading"
    app_detail: str = ""
    app_http: int | None = None
    app_since: float = 0.0
    last_poll: float = 0.0
    last_user_input: float = -3600.0  # device seconds of the last touch or key event


class SimTestbed:
    """The testbed backend, inside the simulator."""

    def __init__(self, device: SimDevice):
        self.device = device

    def set_mode(self, mode: str, for_s: float | None = None, once: bool = False) -> str:
        s = self.device.s
        s.backend, s.backend_once = mode, once
        s.backend_revert_at = self.device.now() + for_s if for_s else None
        if mode == "auth_expired":
            s.session_valid = False
        return f"backend {mode}" + (" (once)" if once else "") + (f" for {for_s:g}s" if for_s else "")

    def reset(self) -> None:
        self.set_mode("ok")


class SimDevice:
    serial = "sim-0001"
    unplugged: bool = False  # simulates the host losing the adb link

    def __init__(
        self,
        target: ActivityRef,
        marker: UiMarker | None = None,
        start_target: bool = True,
        clock: Callable[[], float] | None = None,
    ):
        self.s = SimState(target=target.component, target_process=target.process_name)
        self.marker = marker or UiMarker(content_desc="homeostat-ready")
        self._clock = clock
        self._pids = itertools.count(4000)
        self._tick = 0
        self.log: list[str] = []  # every command received, for assertions
        self.testbed = SimTestbed(self)
        self.s.last_user_input = self.now() - 3600.0  # nobody has touched it for an hour
        if start_target:
            self._start(self.s.target)

    def now(self) -> float:
        """Device time in seconds: the shared simulated clock if given, else one second per command."""
        return self._clock() if self._clock else float(self._tick)

    # -- shell entry points ---------------------------------------------------

    def shell(self, command: str, timeout: float = 10.0) -> ShellResult:
        if self.unplugged:
            raise DeviceUnreachable("adb: no devices/emulators found")
        self._tick += 1
        self.log.append(command)
        self._advance_content()
        outputs, rc = [], 0
        for sub in command.split("; "):
            out, rc = self._one(sub.strip())
            if out:
                outputs.append(out)
        return ShellResult("\n".join(outputs) + ("\n" if outputs else ""), rc)

    def shell_bytes(self, command: str, timeout: float = 20.0) -> bytes:
        if self.unplugged:
            raise DeviceUnreachable("adb: no devices/emulators found")
        self._tick += 1
        self.log.append(command)
        self._advance_content()
        return self._screencap() if command == "screencap" else b""

    # -- helpers -------------------------------------------------------------

    def _ts(self, seconds: float | None = None) -> str:
        t = self.now() if seconds is None else seconds
        day = t % 86400
        return f"09-24 {int(day // 3600):02d}:{int(day // 60) % 60:02d}:{int(day) % 60:02d}.{int(t * 1000) % 1000:03d}"

    @staticmethod
    def _parse_ts(ts: str) -> float:
        m = re.match(r"\d\d-\d\d (\d\d):(\d\d):(\d\d)(?:\.(\d{3}))?", ts)
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4) or 0) / 1000

    def _process_of(self, component: str) -> str:
        return self.s.target_process if component == self.s.target else component.split("/")[0]

    def _target_running(self) -> bool:
        return self.s.target_process in self.s.running

    def _start(self, component: str) -> None:
        process = self._process_of(component)
        new_process = process not in self.s.running
        self.s.running.setdefault(process, next(self._pids))
        if component == self.s.target and self.s.crash_on_start > 0:
            self.s.crash_on_start -= 1
            self._crash(process, "java.lang.IllegalStateException: sim crash on start")
            return
        self.s.foreground = component
        if component == self.s.target and new_process:
            self.s.hung = False
            self._load_page()

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
            self.s.crash_log.append((self.now() % 86400, prefix + line))
        self._drop(process)
        if process == self.s.target_process:
            last = self.s.last_target_crash_tick
            if last is not None and self._tick - last <= _REPEAT_CRASH_TICKS:
                self.s.error_dialog = True
            self.s.last_target_crash_tick = self._tick

    # -- kiosk content ---------------------------------------------------------

    def _set_app(self, state: str, detail: str = "", http: int | None = None) -> None:
        s = self.s
        if (state, detail, http) != (s.app_state, s.app_detail, s.app_http):
            s.app_state, s.app_detail, s.app_http, s.app_since = state, detail, http, self.now()

    def _backend_mode(self, page: bool) -> str:
        s = self.s
        if s.backend_revert_at is not None and self.now() >= s.backend_revert_at:
            s.backend, s.backend_revert_at = "ok", None
        mode = s.backend
        if page and s.backend_once:
            s.backend, s.backend_once = "ok", False
        return mode

    def _load_page(self) -> None:
        s = self.s
        s.blank_ui = False
        s.page_loaded, s.pending_hang, s.pending_hang_once = False, False, False
        self._set_app("loading")
        once = s.backend_once
        mode = self._backend_mode(page=True)
        if mode == "down":
            self._set_app("error", "http 503", 503)
        elif mode == "hang":
            s.pending_hang, s.pending_hang_once = True, once
        elif mode == "js_error":
            s.page_loaded = True
            self._set_app("app_error", "Uncaught Error: testbed js_error")
        else:
            s.page_loaded = True
            self._poll()

    def _poll(self) -> None:
        s = self.s
        s.last_poll = self.now()
        mode = self._backend_mode(page=False)
        if mode == "down":
            self._set_app("error", "api 503")
        elif mode == "maintenance":
            self._set_app("error", "maintenance: planned window, back in a few minutes")
        elif mode == "config_error":
            self._set_app("error", "config: kiosk id missing, set it in the admin panel")
        elif not s.session_valid:
            self._set_app("auth_error", "api 401")
        else:
            self._set_app("ready")

    def _advance_content(self) -> None:
        s = self.s
        if not self._target_running() or s.hung:
            return
        if s.pending_hang and not s.pending_hang_once and self._backend_mode(page=False) != "hang":
            # A timed or persistent hang releases the request when the backend recovers.
            s.pending_hang, s.page_loaded = False, True
            self._poll()
        if s.page_loaded and s.app_state != "app_error" and self.now() - s.last_poll >= _JS_POLL_S:
            self._poll()

    def _health_line(self) -> str:
        s = self.s
        if not s.contract or not self._target_running() or s.hung:
            return ""  # a dead or hung kiosk publishes nothing
        pid = s.running[s.target_process]
        payload = {
            "v": 1, "state": s.app_state, "detail": s.app_detail, "http": s.app_http,
            "age_ms": int((self.now() - s.app_since) * 1000), "url": "http://127.0.0.1:8080/", "declared": True,
        }
        return f"{self._ts()}  {pid}  {pid} I homeostat-health: {json.dumps(payload)}"

    def _content_visible(self) -> bool:
        s = self.s
        return s.awake and not s.keyguard and not s.error_dialog and s.foreground == s.target

    def _ui_dump(self) -> str:
        s = self.s
        nodes = []
        if s.error_dialog:
            nodes.append('<node index="0" text="homeostat sürekli olarak duruyor" resource-id="android:id/alertTitle" />')
            nodes.append('<node index="1" text="Uygulamayı kapat" resource-id="android:id/aerr_close" />')
        elif s.keyguard:
            nodes.append('<node index="0" text="" resource-id="com.android.systemui:id/keyguard_root" />')
        elif self._content_visible() and (not s.contract or s.app_state == "ready"):
            m = self.marker
            nodes.append(
                f'<node index="0" text="{m.text or ""}" resource-id="{m.resource_id or ""}" '
                f'content-desc="{m.content_desc or ""}" />'
            )
        return "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">" + "".join(
            nodes
        ) + "</hierarchy>"

    def _screencap(self) -> bytes:
        """Raw RGBA screencap: flat white for a blank page or an unfinished load, flat black
        when asleep, a dark page with light text otherwise."""
        s = self.s
        width, height = _SCREEN
        header = width.to_bytes(4, "little") + height.to_bytes(4, "little") + (1).to_bytes(4, "little")
        if not s.awake:
            return header + b"\x00\x00\x00\xff" * (width * height)
        flat = self._content_visible() and (s.blank_ui or (s.contract and not s.page_loaded and s.app_state == "loading"))
        if flat:
            return header + b"\xff\xff\xff\xff" * (width * height)
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                text = height // 3 < y < height // 2 and (x // 4) % 2 == 0
                pixels += b"\xe8\xec\xef\xff" if text else b"\x10\x14\x18\xff"
        return header + bytes(pixels)

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

    def _contract_command(self, action: str) -> str:
        if not self._target_running():
            return 'Broadcast completed: result=0, data="kiosk not running"'
        if action.endswith("RESET_SESSION"):
            self.s.session_valid = True
        self._load_page()
        return 'Broadcast completed: result=0, data="reloading"'

    # -- command table -------------------------------------------------------

    def _one(self, cmd: str) -> tuple[str, int]:
        s = self.s
        if cmd.startswith("echo "):
            return cmd[5:], 0
        if cmd.startswith("date"):
            return self._ts()[:-4] + ".000", 0  # the collector asks for whole seconds
        if cmd == "getprop ro.product.model":
            return "SimPhone", 0
        if cmd == "getprop ro.build.version.sdk":
            return "29", 0
        if cmd.startswith("dumpsys activity activities"):
            if not s.awake:
                return "    mResumedActivity: null", 0
            return f"    mResumedActivity: ActivityRecord{{a1b2c3 u0 {s.foreground} t12}}", 0
        if cmd.startswith("pidof "):
            pid = s.running.get(cmd.split()[1])
            return (str(pid), 0) if pid else ("", 1)
        if cmd.startswith("dumpsys window policy"):
            return f"    KeyguardServiceDelegate\n      showing={'true' if s.keyguard else 'false'}", 0
        if cmd.startswith("dumpsys window"):
            if not s.awake:
                return "  mCurrentFocus=null", 0
            if s.keyguard:
                return "  mCurrentFocus=Window{d9da72 u0 StatusBar}", 0
            if s.error_dialog:
                return f"  mCurrentFocus=Window{{4a22345 u0 Application Error: {s.target.split('/')[0]}}}", 0
            pkg, activity = s.foreground.split("/")
            full = pkg + activity if activity.startswith(".") else activity
            return f"  mCurrentFocus=Window{{990455c u0 {pkg}/{full}}}", 0
        if cmd == "wm dismiss-keyguard":
            s.keyguard = False
            return "", 0
        if cmd == "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS":
            if s.error_dialog:
                s.error_dialog = False  # closing the crash dialog ends the app
                self._kill(s.target.split("/")[0])
            return "Broadcast completed: result=0", 0
        if cmd.startswith("am broadcast -p ") and cmd.endswith("com.homeostat.contract.RESTART"):
            # Handled by the app's main process, so it works while the kiosk is hung.
            self._drop(s.target_process)
            s.hung = False
            self._start(s.target)
            return 'Broadcast completed: result=0, data="killed, started"', 0
        if cmd.startswith("am broadcast -p ") and "com.homeostat.contract." in cmd:
            return self._contract_command(cmd.split()[-1]), 0
        if cmd.startswith("dumpsys power"):
            ago = int(max(0.0, self.now() - s.last_user_input) * 1000)
            return (f"  mWakefulness={'Awake' if s.awake else 'Asleep'}\n"
                    f"  mLastUserActivityTime={int(self.now() * 1000) - ago} ({ago} ms ago)"), 0
        if cmd.startswith("input tap ") or cmd.startswith("input swipe "):
            s.last_user_input = self.now()
            return "", 0
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
        if cmd.startswith("logcat") and "homeostat-health" in cmd:
            return self._health_line(), 0
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
            pkg = cmd.split()[2]
            if not (s.owner_protected and pkg == s.target.split("/")[0]):
                self._kill(pkg)
            return "", 0  # succeeds silently either way, as on the device
        if cmd.startswith("run-as ") and " kill -9 " in cmd:
            # SELinux on the reference device denies this (runas_app may not signal
            # untrusted_app): the command runs and nothing dies.
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
            s.session_valid = True  # app data, cookies included, is gone
            return "Success", 0
        if cmd == "input keyevent KEYCODE_WAKEUP":
            s.awake = True
            s.last_user_input = self.now()  # a key event is user activity to Android
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
