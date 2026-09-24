package com.homeostat.agent

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Fault hooks that adb alone cannot produce on a stock device, so the crash_loop and
 * blank_ui scenarios can run on real hardware. Debug builds only.
 *
 * All state lives in the ":kiosk" process (the receiver is declared there too), so the
 * flags are never cached and overwritten by another process.
 *
 *   adb shell am broadcast -n com.homeostat.agent/.DebugFaultReceiver --ei crash_on_start 3
 *   adb shell am broadcast -n com.homeostat.agent/.DebugFaultReceiver --ez blank_ui true
 *   adb shell am broadcast -n com.homeostat.agent/.DebugFaultReceiver --ez hang_main true
 *   adb shell am broadcast -n com.homeostat.agent/.DebugFaultReceiver --ez reset true
 */
object DebugFaults {
    private const val PREFS = "debug_faults"
    private const val CRASH_ON_START = "crash_on_start"

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun setCrashOnStart(context: Context, count: Int) {
        prefs(context).edit().putInt(CRASH_ON_START, count).commit()
    }

    fun reset(context: Context) {
        prefs(context).edit().clear().commit()
    }

    /** Called first thing in KioskActivity.onCreate. */
    fun onKioskStart(context: Context) {
        if (!BuildConfig.DEBUG) return
        val remaining = prefs(context).getInt(CRASH_ON_START, 0)
        if (remaining > 0) {
            prefs(context).edit().putInt(CRASH_ON_START, remaining - 1).commit()
            Log.w(KioskActivity.TAG, "debug fault: crash on start ($remaining left)")
            throw IllegalStateException("homeostat debug fault: crash on start")
        }
    }
}

class DebugFaultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (!BuildConfig.DEBUG) {
            resultData = "debug faults are disabled in release builds"
            return
        }
        if (intent.getBooleanExtra("reset", false)) {
            DebugFaults.reset(context)
            KioskActivity.current?.get()?.reloadContent()
        }
        if (intent.hasExtra("crash_on_start")) {
            DebugFaults.setCrashOnStart(context, intent.getIntExtra("crash_on_start", 0))
        }
        if (intent.getBooleanExtra("hang_main", false)) {
            val kiosk = KioskActivity.current?.get()
            kiosk?.hangMainThread()
            resultData = if (kiosk != null) "hang_main applied" else "hang_main: kiosk not running"
            return
        }
        if (intent.getBooleanExtra("blank_ui", false)) {
            val kiosk = KioskActivity.current?.get()
            kiosk?.goBlank()
            resultData = if (kiosk != null) "blank_ui applied" else "blank_ui: kiosk not running"
            return
        }
        resultData = "ok"
    }
}
