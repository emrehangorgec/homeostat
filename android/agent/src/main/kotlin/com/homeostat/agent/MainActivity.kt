package com.homeostat.agent

import android.app.Activity
import android.app.admin.DevicePolicyManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * M0 status screen: shows whether we hold Device Owner and offers a way to give it back.
 * Release is on long press only, so a stray tap cannot drop the privilege.
 */
class MainActivity : Activity() {

    private lateinit var dpm: DevicePolicyManager
    private lateinit var status: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        dpm = getSystemService(DevicePolicyManager::class.java)

        status = TextView(this).apply { textSize = 18f }
        val release = Button(this).apply {
            text = "Release Device Owner (long press)"
            setOnLongClickListener {
                releaseDeviceOwner()
                true
            }
        }

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 96, 48, 48)
            addView(status)
            addView(release)
        })
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    private fun render() {
        val owner = dpm.isDeviceOwnerApp(packageName)
        val admin = dpm.isAdminActive(AdminReceiver.component(this))
        status.text = buildString {
            appendLine("homeostat agent ${packageManager.getPackageInfo(packageName, 0).versionName}")
            appendLine("Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})")
            appendLine("${Build.MANUFACTURER} ${Build.MODEL}")
            appendLine()
            appendLine("Device Owner: ${if (owner) "YES" else "no"}")
            appendLine("Device Admin: ${if (admin) "active" else "inactive"}")
        }
    }

    @Suppress("DEPRECATION")
    private fun releaseDeviceOwner() {
        if (dpm.isDeviceOwnerApp(packageName)) {
            dpm.clearDeviceOwnerApp(packageName)
            Log.i(AdminReceiver.TAG, "device owner released")
        }
        val admin = AdminReceiver.component(this)
        if (dpm.isAdminActive(admin)) dpm.removeActiveAdmin(admin)
        render()
    }
}
