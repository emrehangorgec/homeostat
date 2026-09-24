package com.homeostat.agent

import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.wifi.WifiManager
import android.os.Build
import android.provider.Settings
import android.util.Log
import org.json.JSONObject

/**
 * M0 capability probe. Tries each privileged operation homeostat might rely on and
 * reports what this device actually allows, restoring every change it makes.
 *
 * Guarded by android.permission.DUMP: the adb shell holds it, ordinary apps cannot.
 *
 *   adb shell am broadcast -n com.homeostat.agent/.ProbeReceiver -a com.homeostat.agent.PROBE
 *       [--ez disruptive true]     also toggle Wi-Fi off and on
 *       [--es hide_pkg <package>]  also hide and unhide that package
 *       [--ez reboot true]         reboot through DevicePolicyManager at the end
 *   adb shell am broadcast -n com.homeostat.agent/.ProbeReceiver -a com.homeostat.agent.LAUNCH_KIOSK
 *
 * Results come back as JSON in the broadcast result data.
 */
class ProbeReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            ACTION_PROBE -> resultData = probe(context, intent).toString()
            ACTION_LAUNCH_KIOSK -> resultData = launchKiosk(context).toString()
        }
    }

    private fun probe(context: Context, intent: Intent): JSONObject {
        val dpm = context.getSystemService(DevicePolicyManager::class.java)
        val admin = AdminReceiver.component(context)
        val pkg = context.packageName
        val results = JSONObject()
        results.put("sdk", Build.VERSION.SDK_INT)
        results.put("model", "${Build.MANUFACTURER} ${Build.MODEL}")

        fun check(name: String, block: () -> String) {
            val outcome = JSONObject()
            try {
                outcome.put("ok", true).put("detail", block())
            } catch (e: Throwable) {
                outcome.put("ok", false).put("detail", "${e.javaClass.simpleName}: ${e.message}")
            }
            results.put(name, outcome)
            Log.i(TAG, "$name -> $outcome")
        }

        check("device_owner") {
            if (!dpm.isDeviceOwnerApp(pkg)) throw IllegalStateException("not device owner")
            "yes"
        }

        check("lock_task_packages") {
            val previous = dpm.getLockTaskPackages(admin)
            dpm.setLockTaskPackages(admin, arrayOf(pkg))
            val readBack = dpm.getLockTaskPackages(admin).toList()
            dpm.setLockTaskPackages(admin, previous)
            if (readBack != listOf(pkg)) throw IllegalStateException("read back $readBack")
            "set and restored"
        }

        check("persistent_home_activity") {
            val filter = IntentFilter(Intent.ACTION_MAIN).apply {
                addCategory(Intent.CATEGORY_HOME)
                addCategory(Intent.CATEGORY_DEFAULT)
            }
            dpm.addPersistentPreferredActivity(admin, filter, ComponentName(context, KioskActivity::class.java))
            dpm.clearPackagePersistentPreferredActivities(admin, pkg)
            "set and cleared"
        }

        check("keyguard_disable") {
            if (!dpm.setKeyguardDisabled(admin, true)) throw IllegalStateException("returned false")
            dpm.setKeyguardDisabled(admin, false)
            "disabled and restored"
        }

        check("status_bar_disable") {
            if (!dpm.setStatusBarDisabled(admin, true)) throw IllegalStateException("returned false")
            dpm.setStatusBarDisabled(admin, false)
            "disabled and restored"
        }

        check("stay_on_while_plugged") {
            val key = Settings.Global.STAY_ON_WHILE_PLUGGED_IN
            val previous = Settings.Global.getString(context.contentResolver, key) ?: "0"
            dpm.setGlobalSetting(admin, key, "7")
            val readBack = Settings.Global.getString(context.contentResolver, key)
            dpm.setGlobalSetting(admin, key, previous)
            if (readBack != "7") throw IllegalStateException("read back $readBack")
            "set and restored (was $previous)"
        }

        intent.getStringExtra("hide_pkg")?.let { target ->
            check("application_hidden") {
                if (!dpm.setApplicationHidden(admin, target, true)) throw IllegalStateException("hide returned false")
                dpm.setApplicationHidden(admin, target, false)
                "hid and restored $target"
            }
        }

        if (intent.getBooleanExtra("disruptive", false)) {
            check("wifi_toggle") {
                @Suppress("DEPRECATION")
                val wifi = context.applicationContext.getSystemService(WifiManager::class.java)
                @Suppress("DEPRECATION")
                if (!wifi.setWifiEnabled(false)) throw IllegalStateException("disable returned false")
                @Suppress("DEPRECATION")
                wifi.setWifiEnabled(true)
                "toggled off and on"
            }
        }

        if (intent.getBooleanExtra("reboot", false)) {
            check("reboot") {
                dpm.reboot(admin)
                "rebooting"
            }
        }
        return results
    }

    /** Starts the kiosk from a background context: tests the background activity start exemption. */
    private fun launchKiosk(context: Context): JSONObject = try {
        context.startActivity(
            Intent(context, KioskActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
        JSONObject().put("ok", true).put("detail", "startActivity returned; check the foreground from the host")
    } catch (e: Throwable) {
        JSONObject().put("ok", false).put("detail", "${e.javaClass.simpleName}: ${e.message}")
    }

    companion object {
        const val TAG = "homeostat-probe"
        const val ACTION_PROBE = "com.homeostat.agent.PROBE"
        const val ACTION_LAUNCH_KIOSK = "com.homeostat.agent.LAUNCH_KIOSK"
    }
}
