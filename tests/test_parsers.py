from homeostat.state import parsers
from tests.fixtures import android10 as fx


def test_resumed_activity_android10():
    ref = parsers.parse_resumed_activity(fx.RESUMED_Q)
    assert ref.package == "com.homeostat.agent"
    assert ref.activity == "com.homeostat.agent.KioskActivity"


def test_resumed_activity_android12_format():
    ref = parsers.parse_resumed_activity(fx.RESUMED_S)
    assert ref.package == "com.android.settings"
    assert ref.activity == "com.android.settings.Settings"


def test_resumed_activity_none_is_unknown():
    assert parsers.parse_resumed_activity(fx.RESUMED_NONE) is None
    assert parsers.parse_resumed_activity("") is None


def test_pidof():
    assert parsers.parse_pidof("4211\n") == 4211
    assert parsers.parse_pidof("") is None


def test_battery_charging():
    b = parsers.parse_battery(fx.BATTERY)
    assert (b.level, b.temperature_c, b.charging, b.health) == (87, 31.2, True, "good")


def test_battery_hot_unplugged():
    b = parsers.parse_battery(fx.BATTERY_UNPLUGGED_HOT)
    assert (b.temperature_c, b.charging, b.health) == (46.8, False, "overheat")


def test_battery_missing_output_is_unknown():
    b = parsers.parse_battery("")
    assert (b.level, b.temperature_c, b.charging, b.health) == (None, None, None, None)


def test_meminfo():
    assert parsers.parse_mem_available_mb(fx.MEMINFO) == 3072


def test_wakefulness():
    assert parsers.parse_wakefulness(fx.POWER_AWAKE) is True
    assert parsers.parse_wakefulness(fx.POWER_ASLEEP) is False
    assert parsers.parse_wakefulness("") is None


def test_setting_bool():
    assert parsers.parse_setting_bool("1\n") is True
    assert parsers.parse_setting_bool("0") is False
    assert parsers.parse_setting_bool("null") is None


def test_crashes_filtered_by_package():
    crashes = parsers.parse_crashes(fx.CRASH_LOG, "com.homeostat.agent")
    assert crashes == ["crash: java.lang.IllegalStateException: token refresh failed"]
    assert parsers.parse_crashes(fx.CRASH_LOG, "com.not.there") == []


def test_crashes_match_process_name_exactly():
    log = (
        "09-24 17:31:02.123  4211  4211 E AndroidRuntime: FATAL EXCEPTION: main\n"
        "09-24 17:31:02.123  4211  4211 E AndroidRuntime: Process: com.homeostat.agent:kiosk, PID: 4211\n"
        "09-24 17:31:02.123  4211  4211 E AndroidRuntime: java.lang.RuntimeException: boom\n"
    )
    assert parsers.parse_crashes(log, "com.homeostat.agent:kiosk") == ["crash: java.lang.RuntimeException: boom"]
    assert parsers.parse_crashes(log, "com.homeostat.agent") == []


def test_current_focus_captured_on_device():
    assert parsers.parse_current_focus(fx.FOCUS_KIOSK) == "com.homeostat.agent/com.homeostat.agent.KioskActivity"
    assert parsers.parse_current_focus(fx.FOCUS_CRASH_DIALOG) == "Application Error: com.homeostat.agent"
    assert parsers.parse_current_focus("  mCurrentFocus=null\n") is None


def test_blocking_dialog_classification():
    from homeostat.state.schema import Screen

    assert Screen(focus_window="Application Error: com.homeostat.agent").blocking_dialog == "app_error"
    assert Screen(focus_window="Application Not Responding: com.x").blocking_dialog == "anr"
    assert Screen(focus_window="com.homeostat.agent/com.homeostat.agent.KioskActivity").blocking_dialog == "none"
    assert Screen().blocking_dialog is None


def test_keyguard_showing_captured_on_device():
    assert parsers.parse_keyguard_showing(fx.KEYGUARD_SHOWING) is True
    assert parsers.parse_keyguard_showing("    KeyguardServiceDelegate\n      showing=false\n") is False
    assert parsers.parse_keyguard_showing("") is None


def _raw(width, height, pixel_at, header=12):
    head = width.to_bytes(4, "little") + height.to_bytes(4, "little") + (1).to_bytes(4, "little")
    head += b"\x00" * (header - 12)
    return head + b"".join(pixel_at(x, y) for y in range(height) for x in range(width))


def test_flat_screen_detection():
    white = lambda x, y: b"\xff\xff\xff\xff"  # noqa: E731
    text = lambda x, y: b"\xff\xff\xff\xff" if (y // 8) % 3 == 0 and x % 5 == 0 else b"\x10\x14\x18\xff"  # noqa: E731
    assert parsers.screen_is_flat(_raw(96, 192, white)) is True
    assert parsers.screen_is_flat(_raw(96, 192, text)) is False
    assert parsers.screen_is_flat(_raw(96, 192, white, header=16)) is True  # Android 12+ header
    assert parsers.screen_is_flat(b"") is None
    assert parsers.screen_is_flat(b"garbage" * 10) is None


def test_health_line_parsing():
    log = (
        '09-24 22:40:01.500  8563  8563 I homeostat-health: {"v":1,"state":"loading","detail":"","http":null,"age_ms":0}\n'
        '09-24 22:40:06.500  8563  8563 I homeostat-health: {"v":1,"state":"ready","detail":"","http":null,"age_ms":4000}\n'
        "09-24 22:40:07.000  8563  8563 I homeostat-health: not json\n"
    )
    stamp, data = parsers.parse_health_lines(log)
    assert stamp == "09-24 22:40:06.500" and data["state"] == "ready"
    assert parsers.logcat_seconds("09-24 22:40:06.500") - parsers.logcat_seconds("09-24 22:40:01.000") == 5.5


def test_near_white_system_bar_band_still_reads_as_blank():
    # Captured shape on the 5T: a blank page is #FFFFFF with a #FAFAFA band at the bottom.
    band = lambda x, y: b"\xfa\xfa\xfa\xff" if y > 170 else b"\xff\xff\xff\xff"  # noqa: E731
    assert parsers.screen_is_flat(_raw(96, 192, band, header=16)) is True
