"""
browser_fallback.py — real-Chrome fetcher for the pipeline's last pass.

Used only for rows the plain-HTTP passes could not settle: identities Amazon
kept soft-blocking, and product pages that came back ambiguous (in stock but
no price, or no availability and no price) that a fully rendered page should
answer. Sequential, one Chrome, a few seconds between pages — slow, but it
only ever sees the handful of rows that are left.

The Chrome setup is a standalone port of the root scraper.py's live-proven
`detect_chrome_major_version` + `build_driver` (undetected-chromedriver with
an explicit `version_main`, cache cleared and retried once on a driver
version mismatch, headless, images blocked, en-IN, a fresh temp profile per
driver that is always deleted). It deliberately does NOT import scraper.py
or the root config.py — this package is a separate tool.

undetected-chromedriver / selenium are imported lazily inside start(), so
importing this module never requires them (the packaged build may omit
them; chrome_available() then simply reports False).
"""

from __future__ import annotations

import atexit
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from price_verifier import config
from price_verifier.fetcher.models import FetchResult

logger = logging.getLogger("price_verifier.browser")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36",
]

# CSS selector strings (selenium's By.CSS_SELECTOR == "css selector"; kept as
# a literal so this module needs no selenium import outside start()).
_BY_CSS = "css selector"
_READY_SELECTOR = (
    "#productTitle, #captchacharacters, form[action*='validateCaptcha'], "
    "a[href*='cs_404'], img[alt*='Dogs of Amazon'], #g img[alt*='Sorry']"
)
_PRODUCT_SELECTOR = "#productTitle"
_SETTLE_SELECTOR = (
    "#corePriceDisplay_desktop_feature_div .a-offscreen, #corePrice_feature_div .a-offscreen, "
    ".apexPriceToPay .a-offscreen, .priceToPay .a-offscreen, #availability span, #outOfStock, "
    "#add-to-cart-button, #buybox-see-all-buying-choices, #unqualifiedBuyBox_feature_div"
)
_TITLE_TERMINAL_MARKERS = ("page not found", "robot check", "sorry! something went wrong")
_POLL_SECONDS = 0.25


class BrowserUnavailable(RuntimeError):
    """Chrome could not be started. The message is safe to show the vendor."""


# ── Temp profile bookkeeping (always removed: close() and atexit) ──────────────
_LIVE_TEMP_DIRS: set[str] = set()
_TEMP_LOCK = threading.Lock()


# Chrome / ChromeDriver processes this process started and hasn't quit yet.
# The engine reports them with its heartbeat (pipeline/engine.py), so if the
# engine process dies, the app can still kill the Chrome it left behind.
_LIVE_PIDS: set[int] = set()


def live_chrome_pids() -> list[int]:
    with _TEMP_LOCK:
        return sorted(_LIVE_PIDS)


def _driver_pids(driver) -> list[int]:
    pids = []
    for get in (lambda: driver.browser_pid, lambda: driver.service.process.pid):
        try:
            pid = int(get())
            if pid > 0:
                pids.append(pid)
        except Exception:
            pass
    return pids


def _register_temp_dir(path: str) -> None:
    with _TEMP_LOCK:
        _LIVE_TEMP_DIRS.add(path)


def _remove_temp_dir(path: Optional[str]) -> None:
    if not path:
        return
    # On Windows, Chrome's child processes can hold profile files for a
    # moment after quit(); retry briefly, and if it still won't go, keep it
    # registered so the atexit sweep (or the next startup's
    # sweep_stale_profiles) gets it.
    for delay in (0.0, 0.5, 1.5):
        if delay:
            time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            with _TEMP_LOCK:
                _LIVE_TEMP_DIRS.discard(path)
            return


def sweep_stale_profiles(max_age_hours: float = 24.0) -> int:
    """Delete leftover pvchrome_* temp profiles (from a crash, a kill, or a
    locked file) older than `max_age_hours`. Called at app startup. Never
    raises; returns how many were removed."""
    removed = 0
    try:
        cutoff = time.time() - max_age_hours * 3600
        for p in Path(tempfile.gettempdir()).glob("pvchrome_*"):
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 0 if p.exists() else 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


@atexit.register
def _cleanup_all_temp_dirs() -> None:
    with _TEMP_LOCK:
        dirs = list(_LIVE_TEMP_DIRS)
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


# ── Chrome detection (port of scraper.py detect_chrome_major_version) ─────────
# On Windows the version is read WITHOUT starting Chrome: `chrome.exe --version`
# prints nothing there (it tries to open a browser window instead), and the
# BLBeacon registry key only exists once Chrome has been opened at least once
# — so on a PC where Chrome was installed but never opened (or a fresh
# machine) relying on those two reported "Chrome not found".
_FULL_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")


def _read_windows_app_paths() -> list[str]:
    """chrome.exe as registered under App Paths (covers non-standard install
    folders). Empty list off Windows or when not registered."""
    try:
        import winreg  # type: ignore
    except ImportError:
        return []
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
            try:
                value, _ = winreg.QueryValueEx(key, "")
            finally:
                winreg.CloseKey(key)
            if value:
                found.append(str(value).strip().strip('"'))
        except OSError:
            continue
    return found


def _chrome_candidates(platform: str, env: dict, app_paths: Callable[[], list[str]] = _read_windows_app_paths) -> list[str]:
    candidates: list[str] = []
    if config.CHROME_BINARY:
        candidates.append(config.CHROME_BINARY)
    if platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    elif platform.startswith("win"):
        try:
            candidates.extend(app_paths())
        except Exception:
            pass
        candidates.extend([
            os.path.join(env.get("PROGRAMFILES", "C:\\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(env.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(env.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ])
    else:
        candidates.extend(["google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"])
    seen, unique = set(), []
    for c in candidates:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            unique.append(c)
    return unique


def _windows_file_version(path: str) -> Optional[str]:
    """chrome.exe's own version resource (what Explorer shows under
    Properties > Details), e.g. "154.0.8037.58". None off Windows / on error."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        ver = ctypes.WinDLL("version", use_last_error=True)
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        ver.GetFileVersionInfoW.restype = wintypes.BOOL
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        ver.VerQueryValueW.restype = wintypes.BOOL

        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or length.value < 16:
            return None
        # VS_FIXEDFILEINFO: dwSignature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS, ...
        fixed = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 4)).contents
        if fixed[0] != 0xFEEF04BD:
            return None
        ms, ls = fixed[2], fixed[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def _version_from_install_dir(exe: str, listdir: Callable[[str], list[str]] = os.listdir) -> Optional[str]:
    """Chrome on Windows keeps its files in a folder named after its version,
    next to chrome.exe (...\\Application\\154.0.8037.58\\). While an update
    is pending two such folders exist; the older one is the version running."""
    try:
        names = listdir(os.path.dirname(exe))
    except OSError:
        return None
    versions = sorted((tuple(int(x) for x in m.groups()), n)
                      for n in names for m in [_FULL_VERSION_RE.match(n)] if m)
    if not versions:
        return None
    pending = "new_chrome.exe" in {n.lower() for n in names}
    return versions[0][1] if pending and len(versions) > 1 else versions[-1][1]


def _read_windows_registry_version() -> Optional[str]:
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for reg_path in (r"Software\Google\Chrome\BLBeacon", r"Software\Wow6432Node\Google\Chrome\BLBeacon"):
            try:
                key = winreg.OpenKey(hive, reg_path)
                try:
                    version, _ = winreg.QueryValueEx(key, "version")
                finally:
                    winreg.CloseKey(key)
                return str(version)
            except OSError:
                continue
    return None


def _run_version(exe: str) -> str:
    return subprocess.check_output([exe, "--version"], stderr=subprocess.STDOUT, text=True, timeout=5).strip()


def _major(version: Optional[str]) -> Optional[int]:
    m = re.search(r"(\d+)\.", version or "")
    return int(m.group(1)) if m else None


def detect_chrome_major_version(
    *,
    platform: str = sys.platform,
    env: Optional[dict] = None,
    run: Callable[[str], str] = _run_version,
    registry_reader: Callable[[], Optional[str]] = _read_windows_registry_version,
    isfile: Callable[[str], bool] = os.path.isfile,
    file_version: Callable[[str], Optional[str]] = _windows_file_version,
    listdir: Callable[[str], list[str]] = os.listdir,
    app_paths: Callable[[], list[str]] = _read_windows_app_paths,
) -> tuple[Optional[int], Optional[str]]:
    """Returns (major_version, chrome_exe_path); either may be None.

    Windows: never starts Chrome. For each installed chrome.exe, its version
    resource, else its version-named folder; then the BLBeacon registry key.
    Elsewhere: each candidate's `--version` output."""
    env = dict(os.environ) if env is None else env
    candidates = _chrome_candidates(platform, env, app_paths)

    if platform.startswith("win"):
        installed = [p for p in candidates if isfile(p)]
        for exe in installed:
            major = _major(file_version(exe)) or _major(_version_from_install_dir(exe, listdir))
            if major:
                return major, exe
        major = _major(registry_reader())
        if major:
            return major, (installed[0] if installed else None)
        logger.warning("Google Chrome not found (looked at: %s)", "; ".join(candidates))
        return None, None

    for exe in candidates:
        try:
            out = run(exe)
        except Exception:
            continue
        major = _major(out)
        if major:
            return major, exe
    logger.warning("Google Chrome not found (tried: %s)", ", ".join(candidates))
    return None, None


_AVAILABLE: Optional[bool] = None
_AVAILABLE_LOCK = threading.Lock()


def chrome_available(refresh: bool = False) -> bool:
    """True if undetected-chromedriver imports AND a Chrome install is found.
    A found Chrome is cached for the process (refresh=True re-probes); a
    missing one is probed again next time. Never raises."""
    global _AVAILABLE
    with _AVAILABLE_LOCK:
        if _AVAILABLE is not None and not refresh:
            return _AVAILABLE
        try:
            import undetected_chromedriver  # noqa: F401
        except Exception:
            _AVAILABLE = False
            return False
        try:
            major, _exe = detect_chrome_major_version()
        except Exception:
            logger.warning("Chrome detection failed", exc_info=True)
            major = None
        # Only a positive answer is cached: if Chrome gets installed (or
        # opened) after a run, the next Retry finds it without a restart.
        _AVAILABLE = True if major is not None else None
        return major is not None


# ── Driver construction (port of scraper.py build_driver) ─────────────────────
def _friendly_start_error(msg: str) -> str:
    m = msg.lower()
    if "chrome binary" in m or "cannot find chrome" in m or "no such file" in m:
        return "Google Chrome was not found. Install Chrome from google.com/chrome to let the tool double-check hard pages."
    if "only supports chrome version" in m or "session not created" in m:
        return ("Chrome could not start because the ChromeDriver version does not match your Chrome. "
                "Update Chrome (Help > About Google Chrome) and try again.")
    if "403" in m or "forbidden" in m or "tunnel connection failed" in m or "proxy" in m:
        return ("Chrome's driver download is being blocked by this network (office Wi-Fi / VPN / proxy). "
                "Try another network or ask IT to allow ChromeDriver downloads.")
    return "Chrome could not be started on this computer."


def _cached_driver_path(major) -> Optional[Path]:
    try:
        major = int(major)
    except (TypeError, ValueError):
        return None
    suffix = ".exe" if sys.platform.startswith("win") else ""
    return config.UC_CACHE_DIR / f"pv_chromedriver_{major}{suffix}"


def _clear_uc_cache() -> None:
    cache = config.UC_CACHE_DIR
    if not cache.exists():
        return
    for p in cache.glob("*"):
        try:
            if p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


def _build_uc_driver(headless: bool, user_data_dir: str):
    """Start undetected-chromedriver. Raises BrowserUnavailable."""
    try:
        import undetected_chromedriver as uc
    except Exception as e:
        raise BrowserUnavailable(
            "The Chrome fallback is not included in this build (undetected-chromedriver missing)."
        ) from e

    try:
        from undetected_chromedriver.patcher import Patcher

        config.UC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        Patcher.data_path = str(config.UC_CACHE_DIR.resolve())
    except Exception:
        logger.debug("Could not override undetected-chromedriver cache path (non-fatal)")

    major, exe = detect_chrome_major_version()
    if major is None:
        raise BrowserUnavailable(_friendly_start_error("cannot find chrome"))

    def make_options():
        options = uc.ChromeOptions()
        options.add_argument(f"--window-size={random.randint(1280, 1920)},{random.randint(800, 1080)}")
        options.add_argument("--lang=en-IN")
        options.add_argument(f"--user-agent={random.choice(USER_AGENTS).format(v=major)}")
        for arg in (
            "--disable-blink-features=AutomationControlled", "--disable-infobars", "--no-first-run",
            "--no-default-browser-check", "--disable-notifications", "--disable-dev-shm-usage",
            "--no-sandbox", "--disable-gpu", "--memory-pressure-off",
            "--js-flags=--max-old-space-size=512", "--blink-settings=imagesEnabled=false",
        ):
            options.add_argument(arg)
        try:
            options.add_experimental_option("prefs", {
                "profile.managed_default_content_settings.images": 2,
                "profile.default_content_setting_values.notifications": 2,
                "profile.managed_default_content_settings.media_stream": 2,
            })
        except Exception:
            pass
        if headless:
            options.add_argument("--headless=new")
        return options

    def start(version_main):
        kwargs = dict(options=make_options(), use_subprocess=True, user_data_dir=user_data_dir,
                      version_main=version_main)
        if exe and os.path.isabs(exe):
            kwargs["browser_executable_path"] = exe
        elif exe:
            resolved = shutil.which(exe)
            if resolved:
                kwargs["browser_executable_path"] = resolved
        # Left to itself, undetected-chromedriver downloads and patches a
        # fresh ChromeDriver on EVERY start (each Chrome restart of a run),
        # then spends up to 3 s per instance deleting it at exit. Keep the
        # patched driver per Chrome major and hand it back on later starts.
        cached = _cached_driver_path(version_main)
        if cached is not None and cached.is_file():
            kwargs["driver_executable_path"] = str(cached)
        driver = uc.Chrome(**kwargs)
        if cached is not None and not cached.is_file():
            try:
                shutil.copy2(driver.patcher.executable_path, cached)
            except Exception:
                logger.debug("could not cache the patched ChromeDriver", exc_info=True)
        return driver

    try:
        return start(major)
    except Exception as first:
        msg = str(first)
        logger.warning("Chrome start failed: %s", msg.splitlines()[0] if msg else type(first).__name__,
                       exc_info=(type(first), first, first.__traceback__))
        if "only supports chrome version" in msg.lower() or "session not created" in msg.lower():
            _clear_uc_cache()
            try:
                return start(detect_chrome_major_version()[0] or major)
            except Exception as second:
                raise BrowserUnavailable(_friendly_start_error(str(second))) from second
        raise BrowserUnavailable(_friendly_start_error(msg)) from first


# ── Fetcher ─────────────────────────────────────────────────────────────────
class BrowserFetcher:
    """One headless Chrome, used sequentially. Synchronous API — the pipeline
    calls it through asyncio.to_thread.

      start()  -> None; raises BrowserUnavailable (friendly message)
      fetch(asin, base_url=None) -> FetchResult(source="browser"); never raises
      close()  -> idempotent; quits Chrome and deletes the temp profile

    `driver_factory` is a test hook: a zero-arg callable returning a
    selenium-driver-like object (get, page_source, title, find_elements,
    quit, ...). When given, no Chrome/UC is involved at all.
    """

    def __init__(
        self,
        headless: bool = True,
        page_timeout: int = 30,
        driver_factory: Optional[Callable[[], object]] = None,
        *,
        ready_timeout: float = config.BROWSER_READY_TIMEOUT_SECONDS,
        settle_timeout: float = config.BROWSER_SETTLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.headless = headless
        self.page_timeout = page_timeout
        self._driver_factory = driver_factory
        self._ready_timeout = ready_timeout
        self._settle_timeout = settle_timeout
        self._clock = clock
        self._sleep = sleep
        self._driver = None
        self._temp_dir: Optional[str] = None
        self._pids: list[int] = []
        self.starts = 0

    @property
    def started(self) -> bool:
        return self._driver is not None

    def start(self) -> None:
        if self._driver is not None:
            return
        if self._driver_factory is not None:
            try:
                self._driver = self._driver_factory()
            except BrowserUnavailable:
                raise
            except Exception as e:
                raise BrowserUnavailable(_friendly_start_error(str(e))) from e
        else:
            self._temp_dir = tempfile.mkdtemp(prefix="pvchrome_")
            _register_temp_dir(self._temp_dir)
            try:
                self._driver = _build_uc_driver(self.headless, self._temp_dir)
            except BaseException:
                _remove_temp_dir(self._temp_dir)
                self._temp_dir = None
                raise
        self._pids = _driver_pids(self._driver)
        with _TEMP_LOCK:
            _LIVE_PIDS.update(self._pids)
        self.starts += 1
        self._configure_driver()

    def _configure_driver(self) -> None:
        d = self._driver
        for fn in (
            lambda: d.set_page_load_timeout(self.page_timeout),
            lambda: d.set_script_timeout(15),
            lambda: d.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Kolkata"}),
            lambda: d.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"),
        ):
            try:
                fn()
            except Exception:
                pass  # all cosmetic / best-effort, same as scraper.py

    def restart(self) -> None:
        """Fresh Chrome + fresh profile (new cookies/fingerprint)."""
        self.close()
        self.start()

    def _has(self, selector: str) -> bool:
        try:
            return bool(self._driver.find_elements(_BY_CSS, selector))
        except Exception:
            return False

    def _title_is_terminal(self) -> bool:
        try:
            title = (self._driver.title or "").lower()
        except Exception:
            return False
        return any(m in title for m in _TITLE_TERMINAL_MARKERS)

    def _wait_for(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = self._clock() + timeout
        while True:
            if predicate():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(_POLL_SECONDS)

    def fetch(self, asin: str, base_url: Optional[str] = None) -> FetchResult:
        t0 = time.monotonic()
        try:
            if self._driver is None:
                self.start()
            url = f"{(base_url or config.MARKETPLACE_BASE_URL).rstrip('/')}/dp/{asin}"
            try:
                self._driver.get(url)
            except Exception as e:
                # A page-load timeout still leaves a (partially) rendered DOM
                # worth reading; anything else means the driver is unusable.
                if "timeout" not in type(e).__name__.lower():
                    raise
                try:
                    self._driver.execute_script("window.stop();")
                except Exception:
                    pass

            ready = self._wait_for(
                lambda: self._has(_READY_SELECTOR) or self._title_is_terminal(), self._ready_timeout
            )
            if ready and self._has(_PRODUCT_SELECTOR):
                # Price/availability blocks can hydrate a beat after the title.
                self._wait_for(lambda: self._has(_SETTLE_SELECTOR), self._settle_timeout)

            html = self._driver.page_source
            return FetchResult(
                asin=asin, status_code=None, html=html, error=None,
                elapsed_ms=(time.monotonic() - t0) * 1000.0, source="browser",
            )
        except Exception as e:
            return FetchResult(
                asin=asin, status_code=None, html=None, error=f"browser_error:{type(e).__name__}",
                elapsed_ms=(time.monotonic() - t0) * 1000.0, source="browser",
            )

    def close(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        pids, self._pids = self._pids, []
        with _TEMP_LOCK:
            _LIVE_PIDS.difference_update(pids)
        temp_dir, self._temp_dir = self._temp_dir, None
        _remove_temp_dir(temp_dir)
