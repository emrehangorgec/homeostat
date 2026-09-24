package com.homeostat.agent

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import java.lang.ref.WeakReference

/**
 * Web kiosk: shows one URL full screen.
 *
 * Runs in its own process (":kiosk") so a crash here never takes down the device admin
 * receiver or, later, the on-device guardian.
 *
 * Health marker for the oracle: a 1 px view whose content description is
 * "homeostat-loading" until the main frame finishes without error, then
 * "homeostat-ready". A main frame error sets "homeostat-error". The marker is only ever
 * set by the page load itself, independent of the process and foreground signals the
 * guardian uses for detection.
 *
 * It cannot live on the WebView: WebView replaces its own accessibility node with the
 * page's, so a content description set on it never reaches the UI dump (found on the
 * OnePlus 5T during M0).
 */
class KioskActivity : Activity() {

    private lateinit var web: WebView
    private lateinit var marker: View
    private var mainFrameFailed = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        DebugFaults.onKioskStart(this)

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        intent.getStringExtra(EXTRA_URL)?.let { prefs(this).edit().putString(PREF_URL, it).apply() }

        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            webViewClient = MarkerClient()
        }
        marker = View(this).apply {
            contentDescription = MARKER_LOADING
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_YES
        }
        setContentView(FrameLayout(this).apply {
            addView(web, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            addView(marker, FrameLayout.LayoutParams(1, 1))
        })
        web.loadUrl(prefs(this).getString(PREF_URL, DEFAULT_URL)!!)
    }

    /** Debug fault: the process stays healthy and in front, but the content is gone. */
    fun goBlank() {
        runOnUiThread {
            web.webViewClient = WebViewClient()
            web.loadUrl("about:blank")
            marker.contentDescription = MARKER_LOADING
            Log.w(TAG, "debug fault: blank ui")
        }
    }

    /** Undo goBlank: reload the configured page with the marker client. */
    fun restoreContent() {
        runOnUiThread {
            web.webViewClient = MarkerClient()
            web.loadUrl(prefs(this).getString(PREF_URL, DEFAULT_URL)!!)
        }
    }

    override fun onDestroy() {
        if (current?.get() === this) current = null
        super.onDestroy()
    }

    override fun onResume() {
        super.onResume()
        current = WeakReference(this)
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            or View.SYSTEM_UI_FLAG_FULLSCREEN
            or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            or View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
    }

    private inner class MarkerClient : WebViewClient() {
        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
            mainFrameFailed = false
            marker.contentDescription = MARKER_LOADING
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) {
                mainFrameFailed = true
                marker.contentDescription = MARKER_ERROR
                Log.w(TAG, "main frame error ${error.errorCode}: ${error.description}")
            }
        }

        override fun onPageFinished(view: WebView, url: String?) {
            if (!mainFrameFailed) marker.contentDescription = MARKER_READY
        }
    }

    companion object {
        const val TAG = "homeostat-kiosk"
        const val EXTRA_URL = "url"
        const val MARKER_LOADING = "homeostat-loading"
        const val MARKER_READY = "homeostat-ready"
        const val MARKER_ERROR = "homeostat-error"
        private const val PREF_URL = "url"
        private const val DEFAULT_URL = "file:///android_asset/kiosk.html"

        /** The live kiosk in this process, for debug fault hooks. */
        @Volatile
        var current: WeakReference<KioskActivity>? = null

        fun prefs(context: Context) = context.getSharedPreferences("kiosk", Context.MODE_PRIVATE)
    }
}
