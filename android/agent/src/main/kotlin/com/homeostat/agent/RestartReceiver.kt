package com.homeostat.agent

import android.app.ActivityManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Process
import android.os.SystemClock
import android.util.Log
import java.io.File

/**
 * Health contract v1 command RESTART, handled in the agent's main process, never in the
 * kiosk process it restarts: a kiosk whose main thread is blocked cannot handle
 * anything itself.
 *
 * From the host there is no other way to stop a hung Device Owner kiosk. Android ignores
 * `am force-stop` for the Device Owner package, and SELinux denies `run-as <pkg> kill`
 * (runas_app may not signal untrusted_app). Both found on the OnePlus 5T during M2. The
 * main process runs as the same app in the same SELinux domain, so it may kill it.
 *
 * The kiosk is started again only after the old process is gone and the system has
 * dropped its activity: starting it right after the kill hands the start to the dying
 * activity record, which the system then removes ("app died, no saved state").
 *
 *   adb shell am broadcast -p com.homeostat.agent -a com.homeostat.contract.RESTART
 */
class RestartReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val pending = goAsync()
        val app = context.applicationContext
        Thread {
            try {
                val kioskProcess = "${app.packageName}:kiosk"
                val am = app.getSystemService(ActivityManager::class.java)
                val victims = am.runningAppProcesses.orEmpty().filter { it.processName == kioskProcess }.map { it.pid }
                victims.forEach {
                    Log.w(TAG, "restart: killing kiosk process $it")
                    Process.killProcess(it)
                }
                val deadline = SystemClock.elapsedRealtime() + DEATH_TIMEOUT_MS
                while (victims.any { File("/proc/$it").exists() } && SystemClock.elapsedRealtime() < deadline) {
                    SystemClock.sleep(50)
                }
                if (victims.isNotEmpty()) SystemClock.sleep(SYSTEM_CLEANUP_MS)
                app.startActivity(Intent(app, KioskActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                pending.resultData = if (victims.isEmpty()) "kiosk was not running, started" else "killed $victims, started"
            } finally {
                pending.finish()
            }
        }.start()
    }

    companion object {
        const val TAG = "homeostat"
        private const val DEATH_TIMEOUT_MS = 3_000L
        private const val SYSTEM_CLEANUP_MS = 500L
    }
}
