"""
http_client.py — async plain-HTTP product page fetches, replaying the cookie
jar session_bootstrap.py captured.

One httpx.AsyncClient per run, shared across all concurrent workers (httpx
clients are safe for concurrent use — connection pooling is the point).
Concurrency is bounded by the caller's semaphore (pipeline/worker_pool.py),
not here — this module does exactly one fetch attempt per call and reports
what happened; retry policy lives in pipeline/retry.py so it's tested and
tuned in one place.
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


def build_client(cookies: dict[str, str], user_agent: str) -> httpx.AsyncClient:
    """One client for the whole run. `cookies`/`user_agent` come from a
    session_bootstrap.BootstrappedSession — the pincode lives entirely in
    the cookie jar, so no per-request pincode parameter exists here (matches
    the spec's batch-pincode architecture decision)."""
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
