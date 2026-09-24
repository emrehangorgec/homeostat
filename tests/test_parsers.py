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
