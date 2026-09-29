"""browser_fallback tests — Chrome detection and the BrowserFetcher flow,
with an injected fake driver and fake clock (no Chrome, no network).
Run:
  python -m unittest price_verifier.tests.test_browser_fallback
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from price_verifier import config
from price_verifier.fetcher import browser_fallback as bf

PRODUCT = ('<html><body><span id="productTitle">Widget</span>'
           '<div id="corePrice_feature_div"><span class="a-offscreen">₹1,499.00</span></div></body></html>')
CAPTCHA = "<html><body><input id='captchacharacters'></body></html>"


class TimeoutException(Exception):
    """Named like selenium's page-load timeout."""


class WebDriverException(Exception):
    pass


class FakeDriver:
    """Serves `html` for every get(). find_elements() reports markers only
    after `ready_after` polls (simulating hydration)."""

    def __init__(self, html=PRODUCT, ready_after=0, get_raises=None, title="Amazon.in"):
        self.html = html
        self.ready_after = ready_after
        self.get_raises = get_raises
        self.title = title
        self.gets: list[str] = []
        self.polls = 0
        self.quit_calls = 0
        self.scripts: list[str] = []
        self.page_source = ""

    def get(self, url):
        self.gets.append(url)
        self.page_source = self.html
        if self.get_raises is not None:
            raise self.get_raises

    def find_elements(self, by, selector):
        assert by == "css selector"
        self.polls += 1
        if self.polls <= self.ready_after:
            return []
        src = self.page_source
        hits = {
            bf._READY_SELECTOR: "productTitle" in src or "captchacharacters" in src,
            bf._PRODUCT_SELECTOR: "productTitle" in src,
            bf._SETTLE_SELECTOR: "a-offscreen" in src,
        }
        return [object()] if hits.get(selector) else []

    def execute_script(self, script, *args):
        self.scripts.append(script)

    def execute_cdp_cmd(self, *a):
        pass

    def set_page_load_timeout(self, t):
        self.page_load_timeout = t

    def set_script_timeout(self, t):
        pass

    def quit(self):
        self.quit_calls += 1


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, d):
        self.now += d


def fetcher_with(driver_or_factory, **kw):
    clock = FakeClock()
    factory = driver_or_factory if callable(driver_or_factory) and not isinstance(driver_or_factory, FakeDriver) \
        else (lambda: driver_or_factory)
    f = bf.BrowserFetcher(driver_factory=factory, clock=clock.clock, sleep=clock.sleep,
                          ready_timeout=kw.pop("ready_timeout", 20.0), settle_timeout=kw.pop("settle_timeout", 6.0),
                          **kw)
    return f, clock


class FetchFlowTests(unittest.TestCase):
    def test_product_page_waits_for_hydration_and_returns_browser_result(self):
        driver = FakeDriver(ready_after=3)
        f, clock = fetcher_with(driver, page_timeout=17)
        f.start()
        res = f.fetch("B0TEST0001", base_url="http://127.0.0.1:9/")
        self.assertEqual(driver.gets, ["http://127.0.0.1:9/dp/B0TEST0001"])
        self.assertEqual((res.source, res.status_code, res.error), ("browser", None, None))
        self.assertEqual(res.html, PRODUCT)
        self.assertAlmostEqual(clock.now, 3 * bf._POLL_SECONDS)
        self.assertEqual(driver.page_load_timeout, 17)

    def test_default_base_url_is_marketplace(self):
        driver = FakeDriver()
        f, _ = fetcher_with(driver)
        f.fetch("B0TEST0001")  # implicit start()
        self.assertEqual(driver.gets, [f"{config.MARKETPLACE_BASE_URL}/dp/B0TEST0001"])

    def test_captcha_page_returns_immediately_without_settle_wait(self):
        driver = FakeDriver(html=CAPTCHA)
        f, clock = fetcher_with(driver)
        res = f.fetch("B0TEST0001")
        self.assertEqual(res.html, CAPTCHA)
        self.assertEqual(clock.now, 0.0)

    def test_page_that_never_becomes_ready_times_out_but_still_returns_source(self):
        driver = FakeDriver(html="<html><body>blank</body></html>")
        f, clock = fetcher_with(driver, ready_timeout=5.0)
        res = f.fetch("B0TEST0001")
        self.assertIsNone(res.error)
        self.assertIn("blank", res.html)
        self.assertGreaterEqual(clock.now, 5.0)
        self.assertLess(clock.now, 5.0 + 2 * bf._POLL_SECONDS)

    def test_title_marker_counts_as_ready(self):
        driver = FakeDriver(html="<html><body>dogs</body></html>", title="Page Not Found")
        f, clock = fetcher_with(driver)
        f.fetch("B0TEST0001")
        self.assertEqual(clock.now, 0.0)

    def test_page_load_timeout_still_reads_the_dom(self):
        driver = FakeDriver(get_raises=TimeoutException("page load"))
        f, _ = fetcher_with(driver)
        res = f.fetch("B0TEST0001")
        self.assertIsNone(res.error)
        self.assertEqual(res.html, PRODUCT)
        self.assertIn("window.stop();", driver.scripts)

    def test_driver_error_becomes_browser_error_never_raises(self):
        driver = FakeDriver(get_raises=WebDriverException("chrome not reachable"))
        f, _ = fetcher_with(driver)
        res = f.fetch("B0TEST0001")
        self.assertEqual(res.error, "browser_error:WebDriverException")
        self.assertIsNone(res.html)
        self.assertEqual(res.source, "browser")

    def test_factory_failure_raises_friendly_browser_unavailable(self):
        def boom():
            raise RuntimeError("cannot find Chrome binary")

        f, _ = fetcher_with(boom)
        with self.assertRaises(bf.BrowserUnavailable) as ctx:
            f.start()
        self.assertIn("Chrome was not found", str(ctx.exception))
        self.assertEqual(f.fetch("B0TEST0001").error, "browser_error:BrowserUnavailable")

    def test_close_is_idempotent_and_restart_gets_fresh_driver(self):
        drivers = []

        def factory():
            drivers.append(FakeDriver())
            return drivers[-1]

        f, _ = fetcher_with(factory)
        f.start()
        f.restart()
        self.assertEqual(len(drivers), 2)
        self.assertEqual(drivers[0].quit_calls, 1)
        f.close()
        f.close()
        self.assertEqual(drivers[1].quit_calls, 1)
        self.assertFalse(f.started)
        self.assertEqual(f.starts, 2)


class TempProfileTests(unittest.TestCase):
    def test_temp_profile_created_per_driver_and_always_removed(self):
        seen = []

        def fake_build(headless, user_data_dir):
            seen.append((headless, user_data_dir))
            self.assertTrue(os.path.isdir(user_data_dir))
            return FakeDriver()

        with mock.patch.object(bf, "_build_uc_driver", fake_build):
            f = bf.BrowserFetcher(headless=True)
            f.start()
            first = seen[0][1]
            self.assertTrue(os.path.basename(first).startswith("pvchrome_"))
            self.assertIn(first, bf._LIVE_TEMP_DIRS)
            f.restart()
            self.assertFalse(os.path.exists(first), "old profile removed on restart")
            second = seen[1][1]
            self.assertNotEqual(first, second)
            f.close()
            self.assertFalse(os.path.exists(second))
            self.assertNotIn(second, bf._LIVE_TEMP_DIRS)

    def test_temp_profile_removed_when_chrome_fails_to_start(self):
        seen = []

        def failing_build(headless, user_data_dir):
            seen.append(user_data_dir)
            raise bf.BrowserUnavailable("nope")

        with mock.patch.object(bf, "_build_uc_driver", failing_build):
            f = bf.BrowserFetcher()
            with self.assertRaises(bf.BrowserUnavailable):
                f.start()
        self.assertFalse(os.path.exists(seen[0]))

    def test_atexit_cleanup_removes_leftovers(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="pvchrome_")
        bf._register_temp_dir(d)
        bf._cleanup_all_temp_dirs()
        self.assertFalse(os.path.exists(d))


class DetectionTests(unittest.TestCase):
    def test_linux_tries_candidates_in_order(self):
        tried = []

        def run(exe):
            tried.append(exe)
            if exe == "chromium":
                return "Chromium 140.0.7339.80 built on Debian"
            raise FileNotFoundError(exe)

        with mock.patch.object(config, "CHROME_BINARY", None):
            self.assertEqual(bf.detect_chrome_major_version(platform="linux", env={}, run=run), (140, "chromium"))
        self.assertEqual(tried[:2], ["google-chrome", "google-chrome-stable"])

    def test_explicit_binary_wins(self):
        with mock.patch.object(config, "CHROME_BINARY", "/opt/chrome/chrome"):
            got = bf.detect_chrome_major_version(platform="linux", env={},
                                                 run=lambda exe: "Google Chrome 147.0.1.2")
        self.assertEqual(got, (147, "/opt/chrome/chrome"))

    def test_windows_registry_first(self):
        with mock.patch.object(config, "CHROME_BINARY", None):
            got = bf.detect_chrome_major_version(
                platform="win32", env={"PROGRAMFILES": "C:\\PF"}, registry_reader=lambda: "139.0.7258.66",
                isfile=lambda p: p.startswith("C:\\PF"), run=lambda exe: self.fail("subprocess not needed"),
            )
        self.assertEqual(got[0], 139)
        self.assertTrue(got[1].startswith("C:\\PF"))

    def test_windows_falls_back_to_version_command(self):
        with mock.patch.object(config, "CHROME_BINARY", None):
            got = bf.detect_chrome_major_version(platform="win32", env={}, registry_reader=lambda: None,
                                                 run=lambda exe: "Google Chrome 138.0.1")
        self.assertEqual(got[0], 138)

    def test_nothing_found(self):
        def run(exe):
            raise OSError("missing")

        with mock.patch.object(config, "CHROME_BINARY", None):
            self.assertEqual(bf.detect_chrome_major_version(platform="darwin", env={}, run=run), (None, None))

    def test_chrome_available_is_cached_and_refreshable(self):
        with mock.patch.object(bf, "detect_chrome_major_version", return_value=(None, None)):
            self.assertFalse(bf.chrome_available(refresh=True))
        with mock.patch.object(bf, "detect_chrome_major_version", return_value=(131, "chrome")):
            self.assertFalse(bf.chrome_available(), "cached")
            self.assertTrue(bf.chrome_available(refresh=True))
        bf._AVAILABLE = None

    def test_chrome_unavailable_when_uc_missing(self):
        with mock.patch.dict(sys.modules, {"undetected_chromedriver": None}):
            with mock.patch.object(bf, "detect_chrome_major_version", return_value=(131, "chrome")):
                self.assertFalse(bf.chrome_available(refresh=True))
            with self.assertRaises(bf.BrowserUnavailable) as ctx:
                bf._build_uc_driver(True, "/tmp/x")
            self.assertIn("not included", str(ctx.exception))
        bf._AVAILABLE = None

    def test_friendly_errors(self):
        self.assertIn("does not match", bf._friendly_start_error("session not created: only supports chrome version 148"))
        self.assertIn("blocked by this network", bf._friendly_start_error("HTTP Error 403: Forbidden"))
        self.assertIn("could not be started", bf._friendly_start_error("weird"))


if __name__ == "__main__":
    unittest.main()
