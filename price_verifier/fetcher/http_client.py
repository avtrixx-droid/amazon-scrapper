"""
http_client.py — async plain-HTTP product page fetches.

One httpx.AsyncClient per run, shared across all concurrent workers (httpx
clients are safe for concurrent use — connection pooling is the point).
Concurrency is bounded by the caller's semaphore (pipeline/worker_pool.py),
not here — this module does exactly one fetch attempt per call and reports
what happened; retry policy lives in pipeline/retry.py so it's tested and
tuned in one place.

The vendor confirmed price doesn't vary by pincode for this catalog, so
`build_anonymous_client()` — no cookie jar, no prior browser session — is
the default path (see app.py). `build_client()` (cookie-jar replay from a
session_bootstrap.BootstrappedSession) is kept for the case where a run
needs a warmed-up session for other reasons (e.g. to look less like a
fresh, un-visited client under heavier bot-defense); it is not used by
default.
"""

from __future__ import annotations

import httpx

from price_verifier import config
from price_verifier.fetcher.models import FetchResult

_DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# One realistic, current desktop Chrome UA, held fixed for the whole run —
# consistent with a single "browser session" rather than rotating per
# request (which reads as more automated, not less).
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def build_anonymous_client(user_agent: str = _DEFAULT_USER_AGENT) -> httpx.AsyncClient:
    """No cookie jar, no prior session — a fresh client per run. This is the
    default fetch path now that pincode is confirmed not to matter."""
    headers = dict(_DEFAULT_HEADERS)
    headers["User-Agent"] = user_agent
    return httpx.AsyncClient(
        base_url=config.MARKETPLACE_BASE_URL,
        headers=headers,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        http2=True,
    )


def build_client(cookies: dict[str, str], user_agent: str) -> httpx.AsyncClient:
    """Cookie-jar replay variant — `cookies`/`user_agent` come from a
    session_bootstrap.BootstrappedSession. Not used by the default run
    flow (see module docstring); kept for a future "warm session" option."""
    headers = dict(_DEFAULT_HEADERS)
    headers["User-Agent"] = user_agent
    return httpx.AsyncClient(
        base_url=config.MARKETPLACE_BASE_URL,
        headers=headers,
        cookies=cookies,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        http2=True,
    )


async def fetch_product_page(client: httpx.AsyncClient, asin: str) -> FetchResult:
    """Single fetch attempt, no retry. Errors are captured, never raised —
    the pipeline decides what a given error means for the row's status."""
    url = f"/dp/{asin}"
    try:
        resp = await client.get(url)
    except httpx.TimeoutException:
        return FetchResult(asin=asin, status_code=None, html=None, error="timeout")
    except httpx.ConnectError:
        return FetchResult(asin=asin, status_code=None, html=None, error="connection_error")
    except httpx.HTTPError as e:
        return FetchResult(asin=asin, status_code=None, html=None, error=f"http_error:{type(e).__name__}")

    if resp.status_code >= 500 or resp.status_code == 429 or resp.status_code == 503:
        return FetchResult(asin=asin, status_code=resp.status_code, html=resp.text, error="throttled_or_server_error")

    return FetchResult(asin=asin, status_code=resp.status_code, html=resp.text)
