"""Testbed backend: the page the kiosk shows, with injectable server-side faults.

    homeostat backend --port 8080
    adb reverse tcp:8080 tcp:8080                       # phone reaches it over USB
    adb shell am start -n com.homeostat.agent/.KioskActivity --es url http://127.0.0.1:8080/

Modes (POST /_control?mode=<mode>, optionally &for=<seconds> to revert to ok afterwards,
or &once=1 to affect only the next page request):

    ok            normal
    down          every request answers 503
    hang          the page request does not complete (content stuck loading); with
                  once=1 only that request hangs, as after a dropped connection
    js_error      the page throws before it can report ready
    auth_expired  existing sessions are invalidated: the API answers 401 until the
                  client drops its cookie and gets a new session
    maintenance   the API answers 200 with a planned maintenance notice; the page shows
                  it and reports an error with detail "maintenance: ..."
    config_error  the API answers 200 saying the kiosk is not configured; the page reports
                  "config: ...". Nothing on the device can fix it.

The page speaks health contract v1: it declares itself, then reports ready,
auth_error or error through the kiosk's `homeostat` JS bridge after each API poll.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODES = ("ok", "down", "hang", "js_error", "auth_expired", "maintenance", "config_error")
HANG_LIMIT_S = 120.0

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>homeostat testbed</title>
<style>
  html, body { margin: 0; height: 100%; background: #101418; color: #e8ecef; font-family: system-ui, sans-serif; }
  body { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2vh; }
  #clock { font-size: 22vw; font-weight: 200; font-variant-numeric: tabular-nums; }
  #status { font-size: 4vw; opacity: 0.7; }
  body[data-state="auth_error"] #status, body[data-state="error"] #status { color: #f0a0a0; opacity: 1; }
</style></head>
<body data-state="loading">
<div id="clock">--:--</div><div id="status">connecting</div>
__FAULT__
<script>
  const hs = window.homeostat;
  if (hs) hs.declare(1);                       // this page reports its own state
  function report(state, detail) {
    document.body.dataset.state = state;
    if (hs) hs.report(state, detail || "");
  }
  async function poll() {
    try {
      const r = await fetch("/api/status", { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) { status.textContent = "session expired"; return report("auth_error", "api 401"); }
      if (!r.ok) { status.textContent = "backend error " + r.status; return report("error", "api " + r.status); }
      const d = await r.json();
      if (d.maintenance) { status.textContent = "maintenance: " + d.maintenance; return report("error", "maintenance: " + d.maintenance); }
      if (d.config_error) { status.textContent = "not configured: " + d.config_error; return report("error", "config: " + d.config_error); }
      clock.textContent = new Date(d.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      status.textContent = "backend ok · session " + d.session.slice(0, 6);
      report("ready");
    } catch (e) {
      status.textContent = "network error";
      report("error", "network: " + e.message);
    }
  }
  poll();
  setInterval(poll, 5000);
</script>
</body></html>
"""


class Backend:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "ok"
        self.revert_at: float | None = None
        self.once = False  # the mode applies to the next page request only
        self.sessions: set[str] = set()
        self.requests = 0

    def current_mode(self) -> str:
        with self.lock:
            if self.revert_at is not None and time.monotonic() >= self.revert_at:
                self.mode, self.revert_at = "ok", None
            return self.mode

    def set_mode(self, mode: str, duration_s: float | None = None, once: bool = False) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        with self.lock:
            self.mode = mode
            self.once = once
            self.revert_at = time.monotonic() + duration_s if duration_s else None
            if mode == "auth_expired":
                self.sessions.clear()  # every existing cookie becomes invalid

    def take_page_mode(self) -> str:
        """Mode for a page request; a one-shot mode is consumed by it."""
        mode = self.current_mode()
        with self.lock:
            if self.once:
                self.mode, self.once = "ok", False
        return mode

    def new_session(self) -> str:
        sid = secrets.token_hex(8)
        with self.lock:
            self.sessions.add(sid)
        return sid

    def valid(self, sid: str | None) -> bool:
        with self.lock:
            return sid is not None and sid in self.sessions

    def state(self) -> dict:
        mode = self.current_mode()
        with self.lock:
            left = None if self.revert_at is None else max(0.0, self.revert_at - time.monotonic())
            return {"mode": mode, "once": self.once, "reverts_in_s": left, "sessions": len(self.sessions),
                    "requests": self.requests}


def make_handler(backend: Backend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "homeostat-testbed/1"

        def log_message(self, fmt: str, *args) -> None:  # quiet by default
            pass

        def _send(self, code: int, body: str, ctype: str = "text/plain", headers: dict | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _sid(self) -> str | None:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "sid":
                    return value
            return None

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if url.path != "/_control":
                return self._send(404, "not found")
            q = parse_qs(url.query)
            try:
                duration = float(q["for"][0]) if "for" in q else None
                backend.set_mode(q.get("mode", ["ok"])[0], duration, once=q.get("once", ["0"])[0] == "1")
            except ValueError as e:
                return self._send(400, str(e))
            self._send(200, json.dumps(backend.state()), "application/json")

        def do_GET(self) -> None:
            with backend.lock:
                backend.requests += 1
            url = urlparse(self.path)
            if url.path == "/_control":
                return self._send(200, json.dumps(backend.state()), "application/json")
            mode = backend.take_page_mode() if url.path == "/" else backend.current_mode()
            if mode == "down":
                return self._send(503, "service unavailable")
            if url.path == "/":
                if mode == "hang":
                    # One-shot (the mode was consumed by this request): hold only this
                    # request, like a silently dropped connection; a reload gets a fresh one.
                    # Persistent or timed: hold until the mode reverts.
                    one_shot = backend.current_mode() != "hang"
                    deadline = time.monotonic() + HANG_LIMIT_S
                    while time.monotonic() < deadline and (one_shot or backend.current_mode() == "hang"):
                        time.sleep(0.2)
                    mode = backend.current_mode()
                sid = self._sid()
                headers = {}
                if sid is None or (not backend.valid(sid) and mode != "auth_expired"):
                    # A client without a session (or after reset) gets a fresh one. While
                    # auth_expired is active, a stale cookie is kept, as a real app would.
                    headers["Set-Cookie"] = f"sid={backend.new_session()}; Path=/; HttpOnly"
                fault = '<script>throw new Error("testbed js_error");</script>' if mode == "js_error" else ""
                return self._send(200, PAGE.replace("__FAULT__", fault), "text/html", headers)
            if url.path == "/api/status":
                sid = self._sid()
                if not backend.valid(sid):
                    return self._send(401, json.dumps({"error": "session expired"}), "application/json")
                body = {"time": time.time(), "session": sid}
                if mode == "maintenance":
                    body["maintenance"] = "planned window, back in a few minutes"
                if mode == "config_error":
                    body["config_error"] = "kiosk id missing, set it in the admin panel"
                return self._send(200, json.dumps(body), "application/json")
            return self._send(404, "not found")

    return Handler


def serve(port: int = 8080, host: str = "0.0.0.0") -> tuple[ThreadingHTTPServer, Backend]:
    backend = Backend()
    server = ThreadingHTTPServer((host, port), make_handler(backend))
    server.daemon_threads = True
    server.block_on_close = False  # a hanging request must not block shutdown
    return server, backend
