package com.homeostat.agent

import android.os.SystemClock
import android.util.Log
import org.json.JSONObject

/**
 * Health contract v1: the kiosk's logical state, published on every change and as a
 * heartbeat, as one JSON line under the log tag "homeostat-health".
 *
 *   {"v":1,"state":"ready","detail":"","http":null,"age_ms":5021,"url":"...","declared":true}
 *
 * state: loading | ready | error | auth_error | app_error
 *
 * The heartbeat runs on the kiosk's main thread, so a hung UI thread stops it: a
 * missing heartbeat is evidence in itself. On the host the guardian reads these lines
 * over adb; the on-device guardian (M5) will read them in process.
 */
object Health {
    const val TAG = "homeostat-health"
    val STATES = setOf("loading", "ready", "error", "auth_error", "app_error")

    @Volatile var state = "loading"; private set
    @Volatile var detail = ""; private set
    @Volatile var httpStatus: Int? = null; private set
    @Volatile var url = ""
    /** The page speaks the contract itself: page load events no longer mean "ready". */
    @Volatile var declared = false
    private var since = SystemClock.elapsedRealtime()

    fun set(newState: String, newDetail: String = "", http: Int? = null) {
        require(newState in STATES) { "unknown health state $newState" }
        if (newState != state || newDetail != detail || http != httpStatus) {
            state = newState
            detail = newDetail.take(200)
            httpStatus = http
            since = SystemClock.elapsedRealtime()
            publish()
        }
    }

    fun publish() {
        val line = JSONObject()
            .put("v", 1)
            .put("state", state)
            .put("detail", detail)
            .put("http", httpStatus ?: JSONObject.NULL)
            .put("age_ms", SystemClock.elapsedRealtime() - since)
            .put("url", url)
            .put("declared", declared)
        Log.i(TAG, line.toString())
    }
}
