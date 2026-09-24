"""Pure parsers from raw `adb shell` output to typed values. Unit tested against captured output."""

from __future__ import annotations

import json
import re

from homeostat.state.schema import ActivityRef, Battery

_RESUMED = re.compile(
    r"(?:mResumedActivity|topResumedActivity|ResumedActivity)[:=]\s*ActivityRecord\{\S+ u\d+ ([\w.]+)/([\w.$]+)"
)


def normalize_activity(package: str, activity: str) -> str:
    return package + activity if activity.startswith(".") else activity


def parse_resumed_activity(dumpsys: str) -> ActivityRef | None:
    match = _RESUMED.search(dumpsys)
    if not match:
        return None
    package, activity = match.groups()
    return ActivityRef(package=package, activity=normalize_activity(package, activity))


_FOCUS = re.compile(r"mCurrentFocus=Window\{\S+ u\d+ ([^}]+)\}")


def parse_current_focus(dumpsys_window: str) -> str | None:
    match = _FOCUS.search(dumpsys_window)
    return match.group(1).strip() if match else None


_KEYGUARD = re.compile(r"KeyguardServiceDelegate\s+showing=(true|false)")


def parse_keyguard_showing(dumpsys_window_policy: str) -> bool | None:
    match = _KEYGUARD.search(dumpsys_window_policy)
    return match.group(1) == "true" if match else None


def parse_pidof(stdout: str) -> int | None:
    first = stdout.split()
    return int(first[0]) if first and first[0].isdigit() else None


_BATTERY_HEALTH = {
    1: "unknown",
    2: "good",
    3: "overheat",
    4: "dead",
    5: "over_voltage",
    6: "failure",
    7: "cold",
}
_BATTERY_CHARGING_STATUS = {2, 5}  # charging, full


def _kv(text: str) -> dict[str, str]:
    pairs = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            pairs[key.strip()] = value.strip()
    return pairs


def parse_battery(dumpsys: str) -> Battery:
    kv = _kv(dumpsys)

    def as_int(key: str) -> int | None:
        value = kv.get(key)
        return int(value) if value is not None and value.lstrip("-").isdigit() else None

    temperature = as_int("temperature")
    status = as_int("status")
    health = as_int("health")
    powered = [kv.get(k) for k in ("AC powered", "USB powered", "Wireless powered")]
    if status is not None:
        charging = status in _BATTERY_CHARGING_STATUS
    elif any(p is not None for p in powered):
        charging = "true" in powered
    else:
        charging = None
    return Battery(
        level=as_int("level"),
        temperature_c=temperature / 10 if temperature is not None else None,
        charging=charging,
        health=_BATTERY_HEALTH.get(health) if health is not None else None,
    )


def parse_mem_available_mb(meminfo: str) -> int | None:
    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", meminfo, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def parse_wakefulness(dumpsys_power: str) -> bool | None:
    match = re.search(r"mWakefulness=(\w+)", dumpsys_power)
    return match.group(1) == "Awake" if match else None


def parse_setting_bool(stdout: str) -> bool | None:
    value = stdout.strip()
    if value in ("0", "1"):
        return value == "1"
    return None


_LOGCAT_PREFIX = re.compile(r"^\S+\s+\S+\s+\d+\s+\d+\s+\w\s+[^:]+:\s?")


def parse_crash_events(logcat: str, process: str) -> list[tuple[str, str]]:
    """AndroidRuntime crash blocks that belong to the process named `process`.

    Returns (key, summary) per crash. The key is the raw "FATAL EXCEPTION" line (its
    timestamp and pid), so a crash read twice through an overlapping watermark counts once.
    """
    events: list[tuple[str, str]] = []
    block: list[str] = []
    key = ""

    def flush() -> None:
        if not block:
            return
        process_line = next((i for i, line in enumerate(block) if line.startswith("Process:")), None)
        if process_line is not None and block[process_line].startswith(f"Process: {process},"):
            exception = block[process_line + 1] if process_line + 1 < len(block) else "unknown exception"
            events.append((key, f"crash: {exception}"))

    for raw in logcat.splitlines():
        message = _LOGCAT_PREFIX.sub("", raw).strip()
        if message.startswith("FATAL EXCEPTION"):
            flush()
            block, key = [message], raw.strip()
        elif block:
            block.append(message)
    flush()
    return events


def parse_crashes(logcat: str, process: str) -> list[str]:
    """Summaries of the crashes of `process`: "crash: <exception line>"."""
    return [summary for _, summary in parse_crash_events(logcat, process)]


def parse_logcat_timestamp(line: str) -> str | None:
    match = re.match(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})", line)
    return match.group(1) if match else None


def logcat_seconds(stamp: str) -> float | None:
    """Seconds since an arbitrary epoch for a logcat "MM-DD HH:MM:SS.mmm" stamp (for differences)."""
    match = re.match(r"(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(?:\.(\d{1,3}))?", stamp.strip())
    if not match:
        return None
    month, day, hour, minute, second, ms = match.groups()
    days = (int(month) - 1) * 31 + int(day)
    return days * 86400 + int(hour) * 3600 + int(minute) * 60 + int(second) + int((ms or "0").ljust(3, "0")) / 1000


def parse_health_lines(logcat: str) -> tuple[str, dict] | None:
    """The last health contract line: (logcat timestamp, payload). None if there is none."""
    last = None
    for line in logcat.splitlines():
        marker = line.find("homeostat-health:")
        if marker < 0:
            continue
        payload = line[marker + len("homeostat-health:"):].strip()
        stamp = parse_logcat_timestamp(line)
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if stamp and isinstance(data, dict) and data.get("v") == 1:
            last = (stamp, data)
    return last


def screen_is_flat(raw: bytes, grid: tuple[int, int] = (48, 96), threshold: float = 0.997) -> bool | None:
    """True when a raw `screencap` (RGBA) is one flat color at the sampled points.

    The raw format is a little endian header (width, height, format, and on newer
    Android a color space word; the OnePlus 5T on Android 10 already sends 16 bytes)
    followed by width*height RGBA pixels. Colors are compared at 16 levels per channel:
    a blank page on the 5T is #FFFFFF with a #FAFAFA band from the system bars, which
    exact comparison read as "content". None when the data cannot be parsed, so a failed
    capture never reads as "blank".
    """
    if len(raw) < 16:
        return None
    width, height = int.from_bytes(raw[0:4], "little"), int.from_bytes(raw[4:8], "little")
    if width <= 0 or height <= 0:
        return None
    header = len(raw) - width * height * 4
    if header not in (12, 16):
        return None
    cols, rows = grid
    counts: dict[bytes, int] = {}
    total = 0
    for r in range(rows):
        y = (r * height) // rows + height // (2 * rows)
        for c in range(cols):
            x = (c * width) // cols + width // (2 * cols)
            offset = header + (y * width + x) * 4
            pixel = bytes(b >> 4 for b in raw[offset:offset + 3])  # 16 levels per channel, no alpha
            counts[pixel] = counts.get(pixel, 0) + 1
            total += 1
    return max(counts.values()) / total >= threshold
