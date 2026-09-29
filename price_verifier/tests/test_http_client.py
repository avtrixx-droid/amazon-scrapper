"""
FetchSession tests against a REAL local HTTP server (threaded http.server on
127.0.0.1, random port) — both backends exercised end-to-end, no mocks
except the explicit httpx_transport hook test. No internet access needed.

Run:
  python -m unittest price_verifier.tests.test_http_client
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import httpx

from price_verifier.fetcher import http_client
from price_verifier.fetcher.http_client import FetchSession
from price_verifier.fetcher.models import FetchResult

SLOW_SECONDS = 2.0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, status: int, body: str, extra_headers: list[tuple[str, str]] = ()):
        data = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra_headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        srv = self.server
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        with srv.lock:
            srv.requests.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "cookies": {k: m.value for k, m in cookies.items()},
            })
        if self.path == "/":
            with srv.lock:
                srv.warmups += 1
                n = srv.warmups
            self._send(200, "<html><body>home</body></html>",
                       [("Set-Cookie", f"session-id=warm{n}; Path=/")])
        elif self.path == "/dp/B0SERVER503":
            self._send(503, "<html><head><title>Sorry! Something went wrong!</title></head>"
                            "<body>api-services-support@amazon.com</body></html>")
        elif self.path == "/dp/B0THROTTLE":
            self._send(429, "<html><body>slow down</body></html>")
        elif self.path == "/dp/B0SLOWSLOW":
            time.sleep(SLOW_SECONDS)
            self._send(200, "<html><body>too late</body></html>")
        elif self.path == "/dp/B0REDIRECT":
            self._send(301, "", [("Location", "/dp/B0TARGET01")])
        elif self.path == "/dp/B0SETCOOKI":
            self._send(200, "<html><body>B0SETCOOKI</body></html>",
                       [("Set-Cookie", "extra=1; Path=/")])
        elif self.path == "/dp/B0NOTFOUND":
            self._send(404, "<html><body>Looking for something?</body></html>")
        elif self.path.startswith("/dp/"):
            asin = self.path[4:]
            self._send(200, f"<html><body><span id=\"productTitle\">Item {asin}</span></body></html>")
        else:
            self._send(404, "nope")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.warmups = 0

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.warmups = 0

    def requests_for(self, path: str) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["path"] == path]


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _LiveServerMixin:
    backend: str = "httpx"

    @classmethod
    def setUpClass(cls):
        cls.server = _Server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.reset()

    def make(self, **kw) -> FetchSession:
        kw.setdefault("base_url", self.base_url)
        kw.setdefault("timeout", 5.0)
        return FetchSession(prefer=self.backend, **kw)

    # ── shared behaviour, run once per backend ──────────────────────────────
    async def test_backend_selected(self):
        s = self.make()
        self.assertEqual(s.backend, self.backend)
        await s.close()

    async def test_successful_fetch(self):
        s = self.make()
        await s.start()
        try:
            r = await s.fetch("B0TESTOK01")
        finally:
            await s.close()
        self.assertIsInstance(r, FetchResult)
        self.assertIsNone(r.error)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Item B0TESTOK01", r.html)
        self.assertEqual(r.source, "http")
        self.assertGreater(r.elapsed_ms, 0)

    async def test_warm_up_cookie_and_seed_cookies_sent_on_product_request(self):
        s = self.make()
        await s.start(warm_up=True)
        try:
            await s.fetch("B0COOKIE01")
        finally:
            await s.close()
        self.assertEqual(len(self.server.requests_for("/")), 1, "warm-up must GET /")
        (dp,) = self.server.requests_for("/dp/B0COOKIE01")
        self.assertEqual(dp["cookies"].get("session-id"), "warm1")
        self.assertEqual(dp["cookies"].get("i18n-prefs"), "INR")
        self.assertEqual(dp["cookies"].get("lc-acbin"), "en_IN")

    async def test_no_warm_up_still_sends_seed_cookies(self):
        s = self.make()
        await s.start(warm_up=False)
        try:
            await s.fetch("B0COOKIE02")
        finally:
            await s.close()
        self.assertEqual(self.server.requests_for("/"), [])
        (dp,) = self.server.requests_for("/dp/B0COOKIE02")
        self.assertNotIn("session-id", dp["cookies"])
        self.assertEqual(dp["cookies"].get("i18n-prefs"), "INR")

    async def test_cookie_jar_persists_across_fetches(self):
        s = self.make()
        await s.start(warm_up=False)
        try:
            await s.fetch("B0SETCOOKI")
            await s.fetch("B0AFTER001")
        finally:
            await s.close()
        (after,) = self.server.requests_for("/dp/B0AFTER001")
        self.assertEqual(after["cookies"].get("extra"), "1")

    async def test_rotate_clears_cookies_and_warms_up_again(self):
        s = self.make()
        await s.start()
        try:
            await s.fetch("B0SETCOOKI")
            await s.rotate()
            await s.fetch("B0ROTATED1")
        finally:
            await s.close()
        self.assertEqual(len(self.server.requests_for("/")), 2, "rotate must warm up again")
        (after,) = self.server.requests_for("/dp/B0ROTATED1")
        self.assertNotIn("extra", after["cookies"], "rotate must discard the old cookie jar")
        self.assertEqual(after["cookies"].get("session-id"), "warm2")
        self.assertEqual(after["cookies"].get("i18n-prefs"), "INR")

    async def test_503_is_throttled_with_body(self):
        s = self.make()
        await s.start(warm_up=False)
        try:
            r = await s.fetch("B0SERVER503")
        finally:
            await s.close()
        self.assertEqual(r.error, "throttled_or_server_error")
        self.assertEqual(r.status_code, 503)
        self.assertIn("api-services-support@amazon.com", r.html)

    async def test_429_is_throttled(self):
        s = self.make()
        await s.start(warm_up=False)
        try:
            r = await s.fetch("B0THROTTLE")
        finally:
            await s.close()
        self.assertEqual(r.error, "throttled_or_server_error")
        self.assertEqual(r.status_code, 429)

    async def test_404_is_not_an_error(self):
        # A 404 dog page is a terminal "not found" for the parser to classify,
        # not a network error.
        s = self.make()
        await s.start(warm_up=False)
        try:
            r = await s.fetch("B0NOTFOUND")
        finally:
            await s.close()
        self.assertIsNone(r.error)
        self.assertEqual(r.status_code, 404)
        self.assertIn("Looking for something?", r.html)

    async def test_follows_redirects(self):
        s = self.make()
        await s.start(warm_up=False)
        try:
            r = await s.fetch("B0REDIRECT")
        finally:
            await s.close()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Item B0TARGET01", r.html)

    async def test_connection_refused(self):
        s = self.make(base_url=f"http://127.0.0.1:{_closed_port()}")
        await s.start(warm_up=True)  # warm-up failure must be swallowed
        try:
            r = await s.fetch("B0REFUSED1")
        finally:
            await s.close()
        self.assertEqual(r.error, "connection_error")
        self.assertIsNone(r.status_code)
        self.assertIsNone(r.html)

    async def test_timeout(self):
        s = self.make(timeout=0.5)
        await s.start(warm_up=False)
        t0 = time.perf_counter()
        try:
            r = await s.fetch("B0SLOWSLOW")
        finally:
            await s.close()
        self.assertEqual(r.error, "timeout")
        self.assertLess(time.perf_counter() - t0, SLOW_SECONDS, "must give up before the server answers")
        self.assertGreaterEqual(r.elapsed_ms, 400)

    async def test_close_is_idempotent_and_fetch_after_close_does_not_raise(self):
        s = self.make()
        await s.start()
        await s.close()
        await s.close()
        r = await s.fetch("B0CLOSED01")
        self.assertIsInstance(r, FetchResult)
        self.assertIsNotNone(r.error)
        await s.rotate()  # never raises, even when closed
        await s.close()

    async def test_close_without_start(self):
        s = self.make()
        await s.close()
        await s.close()

    async def test_fetch_without_start_lazily_starts(self):
        s = self.make()
        try:
            r = await s.fetch("B0LAZY0001")
        finally:
            await s.close()
        self.assertEqual(r.status_code, 200)

    async def test_fetch_never_raises_on_garbage_input(self):
        s = self.make()
        await s.start(warm_up=False)
        try:
            for bad in ("B0 BAD\r\nX-Injected: 1", "\x00\x01", "../../etc/passwd", ""):
                r = await s.fetch(bad)
                self.assertIsInstance(r, FetchResult)
        finally:
            await s.close()

    async def test_concurrent_fetches_survive_rotate(self):
        s = self.make()
        await s.start()
        try:
            tasks = [asyncio.create_task(s.fetch(f"B0CONC{i:04d}")) for i in range(20)]
            await asyncio.sleep(0)
            await s.rotate()
            results = await asyncio.gather(*tasks)
        finally:
            await s.close()
        self.assertEqual(len(results), 20)
        for r in results:
            self.assertIsInstance(r, FetchResult)
            self.assertEqual(r.status_code, 200, r.error)

    async def test_chrome_like_headers_are_coherent(self):
        s = self.make()
        await s.start(warm_up=False)
        try:
            await s.fetch("B0HEADERS1")
        finally:
            await s.close()
        (dp,) = self.server.requests_for("/dp/B0HEADERS1")
        h = dp["headers"]
        self.assertEqual(h.get("accept-language"), "en-IN,en;q=0.9")
        ua = h.get("user-agent", "")
        self.assertIn("Chrome/", ua)
        major = ua.split("Chrome/")[1].split(".")[0]
        self.assertIn(f'"Google Chrome";v="{major}"', h.get("sec-ch-ua", ""))
        self.assertEqual(h.get("sec-fetch-mode"), "navigate")
        self.assertEqual(h.get("sec-fetch-dest"), "document")

    async def test_loopback_bypasses_env_proxy(self):
        # Point every proxy variable at a dead port and drop NO_PROXY: if the
        # session honoured the env proxy for 127.0.0.1 this fetch would fail.
        dead = f"http://127.0.0.1:{_closed_port()}"
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("no_proxy",)}
        env.update({"HTTP_PROXY": dead, "HTTPS_PROXY": dead, "ALL_PROXY": dead,
                    "http_proxy": dead, "https_proxy": dead, "all_proxy": dead})
        with mock.patch.dict(os.environ, env, clear=True):
            s = self.make()
            await s.start(warm_up=False)
            try:
                r = await s.fetch("B0NOPROXY1")
            finally:
                await s.close()
        self.assertIsNone(r.error)
        self.assertEqual(r.status_code, 200)


@unittest.skipUnless(http_client._CURL_AVAILABLE and http_client.CURL_CHROME_TARGETS,
                     "curl_cffi not installed")
class CurlCffiBackendTests(_LiveServerMixin, unittest.IsolatedAsyncioTestCase):
    backend = "curl_cffi"

    async def test_default_backend_is_curl_cffi(self):
        s = FetchSession(base_url=self.base_url)
        self.assertEqual(s.backend, "curl_cffi")
        await s.close()

    async def test_targets_are_recent_chrome(self):
        self.assertTrue(http_client.CURL_CHROME_TARGETS)
        for t in http_client.CURL_CHROME_TARGETS:
            self.assertTrue(t.startswith("chrome"), t)
            self.assertNotIn("android", t)

    async def test_rotate_switches_impersonation_target(self):
        if len(http_client.CURL_CHROME_TARGETS) < 2:
            self.skipTest("only one impersonation target available")
        s = self.make()
        await s.start(warm_up=False)
        first = s.impersonate_target
        await s.rotate()
        second = s.impersonate_target
        await s.close()
        self.assertIn(first, http_client.CURL_CHROME_TARGETS)
        self.assertNotEqual(first, second)

    async def test_user_agent_matches_impersonation_target(self):
        # UA is set by impersonation, not by us — it must name the same
        # Chrome major as the TLS fingerprint.
        s = self.make()
        await s.start(warm_up=False)
        target = s.impersonate_target
        try:
            await s.fetch("B0UAMATCH1")
        finally:
            await s.close()
        (dp,) = self.server.requests_for("/dp/B0UAMATCH1")
        major = "".join(ch for ch in target if ch.isdigit())
        self.assertIn(f"Chrome/{major}.", dp["headers"].get("user-agent", ""))


class HttpxBackendTests(_LiveServerMixin, unittest.IsolatedAsyncioTestCase):
    backend = "httpx"

    async def test_rotate_switches_profile(self):
        s = self.make()
        await s.start(warm_up=False)
        first = s.impersonate_target
        await s.rotate()
        second = s.impersonate_target
        await s.close()
        self.assertNotEqual(first, second)


class HttpxTransportHookTests(unittest.IsolatedAsyncioTestCase):
    """The pipeline's own tests inject httpx.MockTransport — keep that working."""

    async def test_mock_transport_forces_httpx_and_serves_pages(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/":
                return httpx.Response(200, text="home", headers={"Set-Cookie": "session-id=mock1; Path=/"})
            return httpx.Response(200, text=f"<html>{request.url.path}</html>")

        s = FetchSession(base_url="https://www.amazon.in", httpx_transport=httpx.MockTransport(handler))
        self.assertEqual(s.backend, "httpx")
        await s.start()
        r = await s.fetch("B0MOCK0001")
        await s.close()
        self.assertEqual(r.status_code, 200)
        self.assertIn("/dp/B0MOCK0001", r.html)
        self.assertEqual([q.url.path for q in seen], ["/", "/dp/B0MOCK0001"])
        cookie = seen[1].headers.get("cookie", "")
        self.assertIn("session-id=mock1", cookie)
        self.assertIn("i18n-prefs=INR", cookie)
        self.assertIn("lc-acbin=en_IN", cookie)

    async def test_mock_transport_error_mapping(self):
        cases = {
            "B0TIMEOUT1": (httpx.ReadTimeout("slow"), "timeout"),
            "B0CONNECT1": (httpx.ConnectError("refused"), "connection_error"),
            "B0PROXYER1": (httpx.ProxyError("proxy"), "connection_error"),
            "B0DECODE01": (httpx.DecodingError("bad gzip"), "http_error:DecodingError"),
            "B0VALUEER1": (ValueError("boom"), "http_error:ValueError"),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            asin = request.url.path.rsplit("/", 1)[-1]
            if asin in cases:
                raise cases[asin][0]
            return httpx.Response(200, text="ok")

        s = FetchSession(httpx_transport=httpx.MockTransport(handler))
        await s.start(warm_up=False)
        try:
            for asin, (_, expected) in cases.items():
                r = await s.fetch(asin)
                self.assertEqual(r.error, expected, asin)
                self.assertIsNone(r.status_code)
        finally:
            await s.close()

    async def test_mock_transport_500_keeps_body(self):
        s = FetchSession(httpx_transport=httpx.MockTransport(
            lambda req: httpx.Response(500, text="<html>oops</html>")))
        await s.start(warm_up=True)
        r = await s.fetch("B0FIVEHUND")
        await s.close()
        self.assertEqual(r.error, "throttled_or_server_error")
        self.assertEqual(r.html, "<html>oops</html>")


class HelperTests(unittest.TestCase):
    def test_cookie_domain(self):
        self.assertEqual(http_client._cookie_domain("www.amazon.in"), ".amazon.in")
        self.assertEqual(http_client._cookie_domain("amazon.in"), ".amazon.in")
        self.assertEqual(http_client._cookie_domain("127.0.0.1"), "127.0.0.1")
        self.assertEqual(http_client._cookie_domain("localhost"), "localhost")

    def test_loopback_detection(self):
        for h in ("127.0.0.1", "localhost", "::1", "[::1]", "127.8.9.10"):
            self.assertTrue(http_client._is_loopback_host(h), h)
        for h in ("www.amazon.in", "10.0.0.1", "", None):
            self.assertFalse(http_client._is_loopback_host(h), h)


if __name__ == "__main__":
    unittest.main()
