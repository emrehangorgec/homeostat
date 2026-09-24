import http.cookiejar
import threading
import urllib.error
import urllib.request

import pytest

from homeostat.testbed import backend as backend_module
from homeostat.testbed.backend import serve
from homeostat.testbed.client import HttpTestbed


@pytest.fixture
def testbed(monkeypatch):
    monkeypatch.setattr(backend_module, "HANG_LIMIT_S", 3.0)
    server, backend = serve(port=0, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield url, backend
    server.shutdown()
    server.server_close()


def client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def get(opener, url, timeout=5.0):
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_page_issues_a_session_and_the_api_accepts_it(testbed):
    url, _ = testbed
    c = client()
    status, page = get(c, url + "/")
    assert status == 200 and "hs.declare(1)" in page
    assert get(c, url + "/api/status")[0] == 200


def test_down_answers_503_and_reverts(testbed):
    url, backend = testbed
    HttpTestbed(url).set_mode("down")
    assert get(client(), url + "/")[0] == 503
    HttpTestbed(url).reset()
    assert get(client(), url + "/")[0] == 200


def test_auth_expired_needs_a_new_session(testbed):
    url, _ = testbed
    old = client()
    get(old, url + "/")
    HttpTestbed(url).set_mode("auth_expired")
    get(old, url + "/")  # a reload keeps the stale cookie
    assert get(old, url + "/api/status")[0] == 401
    fresh = client()  # what reset_session achieves: no cookie, new session
    get(fresh, url + "/")
    assert get(fresh, url + "/api/status")[0] == 200


def test_js_error_once_affects_only_the_next_page(testbed):
    url, _ = testbed
    HttpTestbed(url).set_mode("js_error", once=True)
    assert "testbed js_error" in get(client(), url + "/")[1]
    assert "testbed js_error" not in get(client(), url + "/")[1]


def test_hang_once_holds_one_request_and_serves_the_next(testbed):
    url, _ = testbed
    HttpTestbed(url).set_mode("hang", once=True)
    with pytest.raises(Exception):
        get(client(), url + "/", timeout=0.5)
    assert get(client(), url + "/", timeout=2)[0] == 200
