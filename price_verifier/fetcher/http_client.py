"""
http_client.py — async plain-HTTP product page fetches.

`FetchSession` is the fetch path the pipeline uses. One session is one
logical "browser identity": a TLS/HTTP2 fingerprint, a matching header set,
and a persistent cookie jar that is reused for every request until
`rotate()` throws the whole identity away and starts a fresh one.

Why this exists (evidence from the first real run, 30 ASINs @ concurrency 15):
a cookieless plain-httpx client got ~15 good pages and then soft-block pages
for everything after. Two signals give that kind of client away:

1. **TLS / HTTP2 fingerprint.** httpx's TLS ClientHello (JA3/JA4) and its
   HTTP/2 SETTINGS/priority frames look like Python, not Chrome — no matter
   what the User-Agent says. A Chrome UA on a Python fingerprint is itself a
   bot signal.
2. **No session.** Every request arrived with no cookies, i.e. as a brand-new
   visitor that deep-links straight to /dp/ pages at machine speed.

Backends:

- ``curl_cffi`` (default whenever it imports and no test transport is given):
  ``curl_cffi.requests.AsyncSession(impersonate="chromeNNN")`` — libcurl
  patched to reproduce Chrome's exact TLS handshake, HTTP/2 fingerprint and
  header order. We deliberately do NOT set a User-Agent (or any sec-ch-ua /
  sec-fetch header): impersonation sets ones that agree with the TLS
  fingerprint, and overriding them would re-introduce a mismatch. We only
  add ``Accept-Language: en-IN,en;q=0.9``.
- ``httpx`` (fallback when curl_cffi can't be imported, or when a test passes
  ``httpx_transport``): HTTP/2 with a coherent Chrome-on-Windows header set
  (UA, sec-ch-ua*, sec-fetch-*, Accept, Accept-Language). It cannot fake the
  TLS fingerprint — expect higher block rates than curl_cffi on live Amazon.

Both backends:

- warm up by seeding ``i18n-prefs=INR`` / ``lc-acbin=en_IN`` and GETting
  ``/`` so that product requests carry Amazon's own session cookies
  (session-id, ubid-acbin, ...). Warm-up failures are swallowed.
- follow redirects, keep one cookie jar for the session's lifetime.
- never raise from ``fetch()`` / ``rotate()`` / ``close()``; failures land in
  ``FetchResult.error`` as one of:
    "timeout" | "connection_error" | "throttled_or_server_error" (429/5xx,
    body kept) | "http_error:<ExceptionName>".
- never route loopback hosts (127.0.0.1 / localhost / ::1) through an
  environment proxy — the local test servers and ``PV_MARKETPLACE_BASE_URL``
  simulators must be reached directly even if HTTP(S)_PROXY is set and
  NO_PROXY isn't.

Concurrency and retry policy live in the caller (pipeline/runner.py), not
here. This module does exactly one attempt per ``fetch()`` call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import time
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from price_verifier import config
from price_verifier.fetcher.models import FetchResult

log = logging.getLogger(__name__)

try:  # optional at import time: the httpx backend works without it
    from curl_cffi import requests as _curl_requests
    from curl_cffi.requests import exceptions as _curl_exc

    _CURL_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only where curl_cffi is absent
    _curl_requests = None
    _curl_exc = None
    _CURL_AVAILABLE = False


ACCEPT_LANGUAGE = "en-IN,en;q=0.9"

# Cookies a real amazon.in visitor with an Indian locale carries. Seeded
# before warm-up so even the first request looks like a returning visitor
# whose currency/language preference is already set.
_SEED_COOKIES = {"i18n-prefs": "INR", "lc-acbin": "en_IN"}

# Recent desktop Chrome impersonation targets, newest first. Filtered at
# import time against what the installed curl_cffi actually ships
# (curl_cffi.requests.BrowserType), so an older/newer curl_cffi just narrows
# or changes the pool instead of failing at runtime.
_CURL_CHROME_PREFERENCE = (
    "chrome150", "chrome146", "chrome145", "chrome142", "chrome136", "chrome133a", "chrome131",
)
_CURL_POOL_SIZE = 4


def _available_curl_targets() -> tuple[str, ...]:
    if not _CURL_AVAILABLE:
        return ()
    try:
        supported = {b.value for b in _curl_requests.BrowserType}
    except Exception:
        return ()
    targets = [t for t in _CURL_CHROME_PREFERENCE if t in supported]
    return tuple(targets[:_CURL_POOL_SIZE])


CURL_CHROME_TARGETS: tuple[str, ...] = _available_curl_targets()

# ── httpx fallback identities ────────────────────────────────────────────────
# Each profile is internally coherent: the UA's Chrome major matches the
# sec-ch-ua brand list (including that release's GREASE brand).
_HTTPX_CHROME_PROFILES = (
    ("146", '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'),
    ("145", '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'),
    ("142", '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'),
)


def _httpx_accept_encoding() -> str:
    # Advertise only encodings this httpx install can actually decode: Chrome
    # sends "gzip, deflate, br, zstd", but claiming br/zstd without the
    # brotli/zstandard packages would hand us bodies we can't read.
    try:
        from httpx._decoders import SUPPORTED_DECODERS

        encs = [e for e in ("gzip", "deflate", "br", "zstd") if e in SUPPORTED_DECODERS]
        return ", ".join(encs) or "gzip, deflate"
    except Exception:
        return "gzip, deflate"


def _httpx_chrome_headers(major: str, sec_ch_ua: str) -> dict[str, str]:
    # Order mirrors Chrome's top-level navigation request.
    return {
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "upgrade-insecure-requests": "1",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ),
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        "accept-encoding": _httpx_accept_encoding(),
        "accept-language": ACCEPT_LANGUAGE,
        "priority": "u=0, i",
    }


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _cookie_domain(host: str) -> str:
    """Domain to seed cookies under. 'www.amazon.in' -> '.amazon.in' so every
    amazon.in subdomain sees them (what Amazon itself sets); IPs/localhost
    get the exact host (a leading dot would never match them)."""
    host = host.strip("[]").lower()
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if "." not in host:
        return host
    if host.startswith("www."):
        host = host[4:]
    return "." + host


def _map_status(asin: str, status: int, html: str | None, elapsed_ms: float, source: str) -> FetchResult:
    if status == 429 or status >= 500:
        return FetchResult(asin=asin, status_code=status, html=html,
                           error="throttled_or_server_error", elapsed_ms=elapsed_ms, source=source)
    return FetchResult(asin=asin, status_code=status, html=html, elapsed_ms=elapsed_ms, source=source)


def _map_httpx_exception(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    # ConnectError covers DNS failure, refused connections and TLS handshake
    # failures; ReadError/WriteError/CloseError are resets mid-exchange.
    if isinstance(exc, (httpx.NetworkError, httpx.ProxyError)):
        return "connection_error"
    return f"http_error:{type(exc).__name__}"


# libcurl error codes, for CurlErrors that curl_cffi didn't map to a subclass.
_CURLE_TIMEOUT = {28}
_CURLE_CONNECTION = {5, 6, 7, 35, 52, 55, 56, 58, 59, 60, 77, 83, 90, 91, 97}


def _map_curl_exception(exc: BaseException) -> str:
    if _curl_exc is not None:
        # Timeout before ConnectionError: ConnectTimeout subclasses both.
        if isinstance(exc, _curl_exc.Timeout):
            return "timeout"
        if isinstance(exc, (_curl_exc.ConnectionError, _curl_exc.ProxyError,
                            _curl_exc.SSLError, _curl_exc.DNSError)):
            return "connection_error"
    code = getattr(exc, "code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in _CURLE_TIMEOUT:
        return "timeout"
    if code in _CURLE_CONNECTION:
        return "connection_error"
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    return f"http_error:{type(exc).__name__}"


class _Identity:
    """One underlying client (curl_cffi AsyncSession or httpx.AsyncClient)
    plus in-flight bookkeeping, so rotate() can retire it without cutting
    off requests that are still running on it."""

    __slots__ = ("client", "backend", "target", "inflight", "retired", "closed", "good")

    def __init__(self, client, backend: str, target: str):
        self.client = client
        self.backend = backend
        self.target = target
        self.inflight = 0
        self.retired = False
        self.closed = False
        self.good = 0  # product pages this identity has been served

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.backend == "curl_cffi":
                await self.client.close()
            else:
                await self.client.aclose()
        except Exception:
            log.debug("error closing %s client", self.backend, exc_info=True)


class FetchSession:
    """One logical browser identity: a persistent cookie jar and a fixed
    TLS fingerprint / header set, reused for every request until rotate().

    CONTRACT (pipeline/ codes against exactly this surface):
      - FetchSession(*, base_url=None, httpx_transport=None)
          base_url defaults to config.MARKETPLACE_BASE_URL.
          httpx_transport: test hook — forces the httpx backend with that
          transport (e.g. httpx.MockTransport). Production code never passes it.
      - .backend -> str: "curl_cffi" or "httpx"
      - await .start(warm_up=True): open the session; warm_up GETs the
          homepage first so product requests carry real session cookies.
          Warm-up failure is swallowed (never raises).
      - await .fetch(asin) -> FetchResult: GET /dp/{asin}. Never raises.
      - await .rotate(): close and replace with a fresh identity (new
          cookie jar, new UA), then warm up again. Never raises.
      - await .close(): idempotent.

    Optional keyword-only extras (not needed by the pipeline):
      - timeout: per-request total timeout in seconds
          (default config.REQUEST_TIMEOUT_SECONDS).
      - prefer: "curl_cffi" | "httpx" — force a backend. "curl_cffi" silently
          falls back to httpx when curl_cffi isn't importable.
      - warm_up_timeout: cap for the homepage warm-up GET (default
          min(timeout, 15s)) so a slow homepage can't stall a rotate().
      - initial_state: a dict from export_state() (see session_store): the
          first identity starts with that cookie jar and fingerprint — a
          "returning visitor" instead of a brand-new one. Ignored if it
          doesn't match this session's backend / URL.

    Extras for the pipeline's session persistence:
      - export_state() -> dict | None: the current identity's cookies and
          fingerprint, but only if it has been served product pages.
      - .restored: True if the first identity came from initial_state.
      - .interstitials_passed: how many "Continue shopping" pages were
          clicked through (see _continue_shopping_form).

    Safe for concurrent use from many coroutines: fetch() calls share the
    current identity; rotate() builds and warms the replacement first, swaps
    it in, and closes the old one only after its in-flight requests finish.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        httpx_transport=None,
        timeout: float | None = None,
        prefer: str | None = None,
        warm_up_timeout: float | None = None,
        initial_state: dict | None = None,
    ):
        self._base_url = (base_url or config.MARKETPLACE_BASE_URL).rstrip("/")
        self._transport = httpx_transport
        self._timeout = float(timeout if timeout is not None else config.REQUEST_TIMEOUT_SECONDS)
        self._warm_up_timeout = float(
            warm_up_timeout if warm_up_timeout is not None else min(self._timeout, 15.0)
        )
        host = urlsplit(self._base_url).hostname or ""
        self._host = host
        self._bypass_env_proxy = _is_loopback_host(host)

        use_curl = (
            httpx_transport is None
            and prefer != "httpx"
            and _CURL_AVAILABLE
            and bool(CURL_CHROME_TARGETS)
        )
        self.backend = "curl_cffi" if use_curl else "httpx"

        # Start at a random point in the target pool so parallel runs (or a
        # restarted run) don't all present the identical fingerprint.
        pool = CURL_CHROME_TARGETS if use_curl else tuple(p[0] for p in _HTTPX_CHROME_PROFILES)
        self._targets = pool
        self._target_idx = random.randrange(len(pool)) if pool else 0

        self._identity: _Identity | None = None
        self._retired: set[_Identity] = set()
        self._lock: asyncio.Lock | None = None
        self._closed = False
        self.restored = False
        self.interstitials_passed = 0
        self._initial_cookies: list[dict] = []
        if (
            initial_state
            and initial_state.get("backend") == self.backend
            and initial_state.get("base_url") == self._base_url
            and initial_state.get("target") in pool
        ):
            # Same fingerprint the cookies were issued to: a cookie jar that
            # suddenly arrives with a different browser version is itself a
            # bot signal.
            self._target_idx = pool.index(initial_state["target"])
            self._initial_cookies = [c for c in initial_state.get("cookies") or [] if isinstance(c, dict)]

    # ── public API ───────────────────────────────────────────────────────
    @property
    def impersonate_target(self) -> str | None:
        """curl_cffi impersonation target (e.g. "chrome146") or the Chrome
        major the httpx profile claims — for logs/diagnostics only."""
        return self._identity.target if self._identity is not None else None

    async def start(self, warm_up: bool = True) -> None:
        try:
            async with self._get_lock():
                self._closed = False
                if self._identity is not None:
                    return
                ident = self._new_identity()
                if self._initial_cookies:
                    self.restored = self._load_cookies(ident.client.cookies, self._initial_cookies) > 0
                    self._initial_cookies = []
                if warm_up:
                    await self._warm_up(ident)
                self._identity = ident
                log.info("FetchSession ready: backend=%s target=%s restored=%s",
                         ident.backend, ident.target, self.restored)
        except Exception:
            log.warning("FetchSession.start failed", exc_info=True)

    async def fetch(self, asin: str) -> FetchResult:
        started = time.perf_counter()
        try:
            if self._closed:
                return FetchResult(asin=asin, status_code=None, html=None,
                                   error="http_error:SessionClosed")
            ident = self._identity
            if ident is None:
                await self.start()
                ident = self._identity
                if ident is None:
                    return FetchResult(asin=asin, status_code=None, html=None,
                                       error="http_error:SessionStartFailed",
                                       elapsed_ms=_ms_since(started))
            ident.inflight += 1
            try:
                return await self._fetch_on(ident, asin, started)
            finally:
                ident.inflight -= 1
                if ident.retired and ident.inflight <= 0:
                    await self._dispose(ident)
        except Exception as exc:  # never raise (CancelledError is BaseException and still propagates)
            log.debug("unexpected fetch failure for %s", asin, exc_info=True)
            return FetchResult(asin=asin, status_code=None, html=None,
                               error=f"http_error:{type(exc).__name__}",
                               elapsed_ms=_ms_since(started))

    async def rotate(self) -> None:
        try:
            async with self._get_lock():
                if self._closed:
                    return
                if len(self._targets) > 1:
                    self._target_idx = (self._target_idx + 1) % len(self._targets)
                new = self._new_identity()
                await self._warm_up(new)
                old, self._identity = self._identity, new
            if old is not None:
                await self._retire(old)
        except Exception:
            log.warning("FetchSession.rotate failed", exc_info=True)

    def export_state(self) -> dict | None:
        """The current identity's cookies + fingerprint, for session_store —
        only if it has actually been served product pages (a fresh or
        blocked identity is worth nothing to the next run). Call before
        close(). Never raises."""
        try:
            ident = self._identity
            if ident is None or ident.closed or ident.good <= 0:
                return None
            cookies = [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/"}
                for c in ident.client.cookies.jar
                if c.name and c.value is not None
            ]
            if not cookies:
                return None
            return {"backend": self.backend, "target": ident.target, "base_url": self._base_url,
                    "cookies": cookies}
        except Exception:
            log.debug("export_state failed", exc_info=True)
            return None

    async def close(self) -> None:
        try:
            self._closed = True
            ident, self._identity = self._identity, None
            if ident is not None:
                await ident.aclose()
            for old in list(self._retired):
                await old.aclose()
            self._retired.clear()
        except Exception:
            log.debug("FetchSession.close failed", exc_info=True)

    # ── internals ────────────────────────────────────────────────────────
    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _current_target(self) -> str:
        return self._targets[self._target_idx] if self._targets else ""

    def _new_identity(self) -> _Identity:
        target = self._current_target()
        if self.backend == "curl_cffi":
            client = self._build_curl(target)
        else:
            client = self._build_httpx(target)
        return _Identity(client, self.backend, target)

    def _build_curl(self, target: str):
        kwargs = dict(
            impersonate=target,
            base_url=self._base_url,
            headers={"Accept-Language": ACCEPT_LANGUAGE},
            timeout=self._timeout,
            allow_redirects=True,
            max_clients=max(40, config.MAX_CONCURRENCY),
            trust_env=not self._bypass_env_proxy,
        )
        if self._bypass_env_proxy:
            # trust_env=False only stops curl_cffi's own env lookup; libcurl
            # itself still honours http_proxy/ALL_PROXY unless told otherwise.
            from curl_cffi import CurlOpt

            kwargs["curl_options"] = {CurlOpt.NOPROXY: "*"}
        session = _curl_requests.AsyncSession(**kwargs)
        self._seed_cookies(session.cookies)
        return session

    def _build_httpx(self, target: str) -> httpx.AsyncClient:
        sec_ch_ua = dict(_HTTPX_CHROME_PROFILES).get(target, _HTTPX_CHROME_PROFILES[0][1])
        kwargs = dict(
            base_url=self._base_url,
            headers=_httpx_chrome_headers(target or _HTTPX_CHROME_PROFILES[0][0], sec_ch_ua),
            timeout=self._timeout,
            follow_redirects=True,
        )
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            kwargs["http2"] = True
            kwargs["trust_env"] = not self._bypass_env_proxy
            kwargs["limits"] = httpx.Limits(
                max_connections=max(40, config.MAX_CONCURRENCY) + 10,
                max_keepalive_connections=max(40, config.MAX_CONCURRENCY),
            )
        client = httpx.AsyncClient(**kwargs)
        self._seed_cookies(client.cookies)
        return client

    def _seed_cookies(self, jar) -> None:
        if not self._host:
            return
        domain = _cookie_domain(self._host)
        for name, value in _SEED_COOKIES.items():
            try:
                jar.set(name, value, domain=domain, path="/")
            except Exception:
                log.debug("could not seed cookie %s", name, exc_info=True)

    @staticmethod
    def _load_cookies(jar, cookies: list[dict]) -> int:
        n = 0
        for c in cookies:
            try:
                jar.set(str(c["name"]), str(c["value"]), domain=str(c.get("domain") or ""),
                        path=str(c.get("path") or "/"))
                n += 1
            except Exception:
                log.debug("could not restore cookie %r", c.get("name"), exc_info=True)
        return n

    async def _warm_up(self, ident: _Identity) -> None:
        # Both clients accept a per-request `timeout=` override. The response
        # itself is irrelevant — the point is the Set-Cookie headers.
        try:
            await ident.client.get("/", timeout=self._warm_up_timeout)
        except Exception:
            log.debug("warm-up GET / failed (ignored)", exc_info=True)

    async def _fetch_on(self, ident: _Identity, asin: str, started: float) -> FetchResult:
        mapper = _map_curl_exception if ident.backend == "curl_cffi" else _map_httpx_exception
        try:
            resp = await ident.client.get(f"/dp/{asin}")
        except Exception as exc:
            return FetchResult(asin=asin, status_code=None, html=None,
                               error=mapper(exc), elapsed_ms=_ms_since(started))
        status, html = int(resp.status_code), _safe_text(resp)

        form = _continue_shopping_form(html, self._host) if status == 200 else None
        if form is not None:
            # Amazon's "Click the button below to continue shopping" page:
            # a button and nothing to solve. Press it once, the way a person
            # would, instead of throwing a still-usable identity away.
            action, params = form
            try:
                resp = await ident.client.get(action, params=params,
                                              headers={"Referer": f"{self._base_url}/dp/{asin}"})
                status, html = int(resp.status_code), _safe_text(resp)
                self.interstitials_passed += 1
                log.info("passed a 'continue shopping' page for %s (status %s)", asin, status)
            except Exception as exc:
                return FetchResult(asin=asin, status_code=None, html=None,
                                   error=mapper(exc), elapsed_ms=_ms_since(started))

        if status == 200 and html and _looks_like_product_page(html):
            ident.good += 1
        return _map_status(asin, status, html, _ms_since(started), "http")

    async def _retire(self, ident: _Identity) -> None:
        ident.retired = True
        if ident.inflight <= 0:
            await self._dispose(ident)
        else:
            self._retired.add(ident)

    async def _dispose(self, ident: _Identity) -> None:
        self._retired.discard(ident)
        await ident.aclose()


def _looks_like_product_page(html: str) -> bool:
    return 'id="productTitle"' in html or "id='productTitle'" in html


def _continue_shopping_form(html: str | None, host: str) -> tuple[str, dict] | None:
    """(action, params) if `html` is Amazon's plain "continue shopping"
    interstitial — a GET form to /errors/validateCaptcha with ONLY hidden
    fields and a button. Anything a person would have to solve or type (a
    captcha image, a text box, a non-GET form, a form to another site)
    returns None, so real CAPTCHAs are never touched and keep being treated
    as blocks."""
    if not html or "validatecaptcha" not in html.lower():
        return None
    try:
        tree = HTMLParser(html)
        forms = [f for f in tree.css("form")
                 if "validatecaptcha" in (f.attributes.get("action") or "").lower()]
        if len(forms) != 1:
            return None
        form = forms[0]
        if (form.attributes.get("method") or "get").strip().lower() != "get":
            return None
        if tree.css_first("#captchacharacters") is not None:
            return None
        for img in tree.css("img"):
            if "captcha" in (img.attributes.get("src") or "").lower():
                return None
        params: dict[str, str] = {}
        for inp in form.css("input"):
            kind = (inp.attributes.get("type") or "text").strip().lower()
            if kind == "submit":
                continue
            if kind != "hidden":
                return None
            name = inp.attributes.get("name")
            if name:
                params[name] = inp.attributes.get("value") or ""
        if not params:
            return None
        if form.css_first("button") is None and form.css_first("input[type=submit]") is None:
            return None
        action = (form.attributes.get("action") or "").strip()
        parts = urlsplit(action)
        if parts.scheme or parts.netloc:
            if (parts.hostname or "").lower() != host.lower():
                return None
            action = parts.path
        if not action.startswith("/"):
            return None
        return action, params
    except Exception:
        log.debug("could not inspect a validateCaptcha page", exc_info=True)
        return None


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def _safe_text(resp) -> str | None:
    try:
        return resp.text
    except Exception:
        try:
            return resp.content.decode("utf-8", errors="replace")
        except Exception:
            return None

