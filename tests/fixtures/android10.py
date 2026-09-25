"""Android 10 shell output.

Values marked CAPTURED come from the reference OnePlus 5T (OxygenOS 10.0.1, API 29) on
2026-09-24. The rest are representative and should be replaced as they get captured.
"""

# CAPTURED
RESUMED_Q = """\
    mResumedActivity: ActivityRecord{9f93948 u0 com.homeostat.agent/.KioskActivity t22}
 ResumedActivity:ActivityRecord{9f93948 u0 com.homeostat.agent/.KioskActivity t22}
  ResumedActivity: ActivityRecord{9f93948 u0 com.homeostat.agent/.KioskActivity t22}
"""

# CAPTURED: `dumpsys window policy | grep -A1 KeyguardServiceDelegate` after a screen off
KEYGUARD_SHOWING = "    KeyguardServiceDelegate\n      showing=true\n"

# CAPTURED
FOCUS_KIOSK = "  mCurrentFocus=Window{990455c u0 com.homeostat.agent/com.homeostat.agent.KioskActivity}\n"
# CAPTURED: the "homeostat keeps stopping" dialog after repeated crashes
FOCUS_CRASH_DIALOG = "  mCurrentFocus=Window{4a22345 u0 Application Error: com.homeostat.agent}\n"

RESUMED_S = """\
    topResumedActivity=ActivityRecord{2b7e9d1 u0 com.android.settings/.Settings t77}
"""

RESUMED_NONE = "    mResumedActivity: null\n"

BATTERY = """\
Current Battery Service state:
  AC powered: true
  USB powered: false
  Wireless powered: false
  Max charging current: 3000000
  status: 2
  health: 2
  present: true
  level: 87
  scale: 100
  voltage: 4312
  temperature: 312
  technology: Li-poly
"""

BATTERY_UNPLUGGED_HOT = """\
Current Battery Service state:
  AC powered: false
  USB powered: false
  status: 3
  health: 3
  level: 40
  temperature: 468
"""

MEMINFO = "MemAvailable:    3145728 kB\n"

POWER_AWAKE = "  mWakefulness=Awake\n"
# CAPTURED: `dumpsys power | grep -E 'mWakefulness=|mLastUserActivityTime='` right after `input tap`
POWER_WITH_ACTIVITY = "  mWakefulness=Awake\n  mLastUserActivityTime=59579363 (226 ms ago)\n"
POWER_ASLEEP = "  mWakefulness=Asleep\n"

CRASH_LOG = """\
09-24 17:31:02.123  4211  4211 E AndroidRuntime: FATAL EXCEPTION: main
09-24 17:31:02.123  4211  4211 E AndroidRuntime: Process: com.homeostat.agent, PID: 4211
09-24 17:31:02.123  4211  4211 E AndroidRuntime: java.lang.IllegalStateException: token refresh failed
09-24 17:31:02.124  4211  4211 E AndroidRuntime: \tat com.homeostat.agent.Session.refresh(Session.kt:42)
09-24 17:35:10.500  5100  5100 E AndroidRuntime: FATAL EXCEPTION: main
09-24 17:35:10.500  5100  5100 E AndroidRuntime: Process: com.other.app, PID: 5100
09-24 17:35:10.500  5100  5100 E AndroidRuntime: java.lang.NullPointerException
"""
