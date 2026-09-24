package com.homeostat.agent

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Health contract v1 commands, handled in the ":kiosk" process. Any app implementing
 * the contract registers these two actions; the guardian addresses it by package.
 * Guarded by android.permission.DUMP (the adb shell has it, ordinary apps do not).
 *
 *   adb shell am broadcast -p com.homeostat.agent -a com.homeostat.contract.RELOAD
 *   adb shell am broadcast -p com.homeostat.agent -a com.homeostat.contract.RESET_SESSION
 */
class KioskControlReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val kiosk = KioskActivity.current?.get()
        if (kiosk == null) {
            resultData = "kiosk not running"
            return
        }
        resultData = when (intent.action) {
            ACTION_RELOAD -> { kiosk.reloadContent(); "reloading" }
            ACTION_RESET_SESSION -> { kiosk.resetSession(); "session reset, reloading" }
            else -> "unknown action ${intent.action}"
        }
    }

    companion object {
        const val ACTION_RELOAD = "com.homeostat.contract.RELOAD"
        const val ACTION_RESET_SESSION = "com.homeostat.contract.RESET_SESSION"
    }
}
