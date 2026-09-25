package com.homeostat.agent

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.webkit.ConsoleMessage
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebStorage
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import java.lang.ref.WeakReference

/**
 * Web kiosk: shows one URL full screen and publishes its health (see [Health]).
 *
 * Runs in its own process (":kiosk") so a crash here never takes down the device admin
 * receiver or, later, the on-device guardian.
 *
 * Health marker for the oracle: a 1 px view whose content description is
 * "homeostat-ready" exactly while the health state is ready, "homeostat-error" on an
 * error state and "homeostat-loading" otherwise. It cannot live on the WebView: WebView
 * replaces its own accessibility node with the page's, so a content description set on
 * it never reaches the UI dump (found on the OnePlus 5T during M0).
 *
 * Health sources, per page load:
 * - main frame network or HTTP error: error (sticky for this load)
 * - uncaught script error: app_error (sticky for this load)
 * - load finished: ready, unless the page declared the contract through the JS bridge,
 *   in which case the page reports its own state (`homeostat.report(state, detail)`).
 */
class KioskActivity : Activity() {

    private lateinit var web: WebView
    private lateinit var marker: View
    private var loadFailed = false
    private val main = Handler(Looper.getMainLooper())
    private val heartbeat = object : Runnable {
        override fun run() {
            Health.publish()
            main.postDelayed(this, HEARTBEAT_MS)
        }
    }

    @SuppressLint("SetJavaScriptEnabled", "AddJavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        DebugFaults.onKioskStart(this)

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        intent.getStringExtra(EXTRA_URL)?.let { prefs(this).edit().putString(PREF_URL, it).apply() }

        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            webViewClient = HealthClient()
            webChromeClient = ScriptErrors()
            // The page at the owner's URL may report its own state. The bridge only
            // accepts the contract's states, nothing else is exposed.
            addJavascriptInterface(Bridge(), "homeostat")
        }
        marker = View(this).apply {
            contentDescription = MARKER_LOADING
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_YES
        }
        setContentView(FrameLayout(this).apply {
            addView(web, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            addView(marker, FrameLayout.LayoutParams(1, 1))
        })
        load()
        main.post(heartbeat)
    }

    /**
     * Every navigation starts here, so per-load state is reset here and not in
     * onPageStarted: for a fast local page WebView can deliver onPageStarted after the
     * page's scripts already ran, which would wipe their declare() or an early script error
     * (seen on the OnePlus 5T during M2).
     */
    private fun load() {
        val target = prefs(this).getString(PREF_URL, DEFAULT_URL)!!
        loadFailed = false
        Health.declared = false
        Health.url = target
        setHealth("loading")
        web.loadUrl(target)
    }

    /** A new URL while the kiosk is already running (singleTask delivers it here, not to onCreate). */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra(EXTRA_URL)?.let {
            prefs(this).edit().putString(PREF_URL, it).apply()
            load()
        }
    }

    /** Guardian action reload_content. */
    fun reloadContent() = runOnUiThread { web.webViewClient = HealthClient(); load() }

    /** Guardian action reset_session: drop cookies and web storage, then reload. */
    fun resetSession() = runOnUiThread {
        CookieManager.getInstance().removeAllCookies(null)
        CookieManager.getInstance().flush()
        WebStorage.getInstance().deleteAllData()
        web.clearCache(true)
        web.webViewClient = HealthClient()
        load()
    }

    /** Debug fault: the content disappears while the kiosk still believes it is fine. */
    fun goBlank() = runOnUiThread {
        web.webViewClient = WebViewClient() // no callbacks: health and marker stay as they are
        web.loadUrl("about:blank")
        Log.w(TAG, "debug fault: blank ui (kiosk unaware)")
    }

    /** Debug fault: block the main thread; the heartbeat stops, the process stays alive. */
    fun hangMainThread() {
        main.post {
            Log.w(TAG, "debug fault: main thread hang")
            Thread.sleep(Long.MAX_VALUE)
        }
    }

    private fun setHealth(state: String, detail: String = "", http: Int? = null) {
        Health.set(state, detail, http)
        marker.contentDescription = when (state) {
            "ready" -> MARKER_READY
            "loading" -> MARKER_LOADING
            else -> MARKER_ERROR
        }
    }

    override fun onDestroy() {
        main.removeCallbacks(heartbeat)
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

    private inner class HealthClient : WebViewClient() {
        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
            if (!loadFailed && !Health.declared) setHealth("loading")
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) {
                loadFailed = true
                setHealth("error", "net ${error.errorCode}: ${error.description}")
            }
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
            if (request.isForMainFrame) {
                loadFailed = true
                setHealth("error", "http ${response.statusCode}", response.statusCode)
            }
        }

        override fun onPageFinished(view: WebView, url: String?) {
            if (!loadFailed && !Health.declared) setHealth("ready")
        }
    }

    private inner class ScriptErrors : WebChromeClient() {
        override fun onConsoleMessage(message: ConsoleMessage): Boolean {
            if (message.messageLevel() == ConsoleMessage.MessageLevel.ERROR && message.message().startsWith("Uncaught")) {
                loadFailed = true
                runOnUiThread { setHealth("app_error", message.message()) }
            }
            return false
        }
    }

    private inner class Bridge {
        @JavascriptInterface
        fun declare(version: Int) {
            if (version == 1) Health.declared = true
        }

        /**
         * Accepted only from a page that declared itself during the current load. While a
         * new load is pending, the previous document keeps running its scripts; without
         * this check its polls reported "ready" over a load that was actually hanging
         * (found on the OnePlus 5T during M2).
         */
        @JavascriptInterface
        fun report(state: String, detail: String) {
            if (Health.declared && state in Health.STATES && !loadFailed) runOnUiThread { setHealth(state, detail) }
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
        private const val HEARTBEAT_MS = 5_000L

        /** The live kiosk in this process, for guardian commands and debug fault hooks. */
        @Volatile
        var current: WeakReference<KioskActivity>? = null

        fun prefs(context: Context) = context.getSharedPreferences("kiosk", Context.MODE_PRIVATE)
    }
}
