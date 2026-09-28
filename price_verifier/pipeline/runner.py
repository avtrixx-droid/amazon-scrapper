"""
runner.py — orchestrates one run: fetch → parse → classify → compare →
checkpoint, for every pending/retryable row, under a resizable concurrency
gate and a circuit breaker.

This is the one place that turns a (FetchResult, ParsedProduct) pair into a
row's final status. Kept as an explicit function (`classify`) rather than
scattered across the fetcher, because the fetcher/parser modules know
nothing about run/DB status vocabulary on purpose (fetcher/parser.py is
reused as-is regardless of what a "run" even is).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from price_verifier import config
from price_verifier.fetcher.http_client import build_anonymous_client, build_client, fetch_product_page
from price_verifier.fetcher.models import FetchResult, ParsedProduct
from price_verifier.fetcher.parser import parse_product_page
from price_verifier.fetcher.session_bootstrap import BootstrappedSession
from price_verifier.pipeline.circuit_breaker import CircuitBreaker
from price_verifier.pipeline.compare import prices_match
from price_verifier.pipeline.retry import backoff_delay
from price_verifier.pipeline.worker_pool import DynamicGate, PauseGate
from price_verifier.storage import checkpoint

logger = logging.getLogger("price_verifier.runner")

# Fetch-level outcomes that should be retried (transient / bot-defense), as
# opposed to outcomes that are a legitimate terminal answer on first read.
_RETRYABLE_PAGE_KINDS = {"captcha", "blocked", "unknown"}
_RETRYABLE_ERRORS = {"timeout", "connection_error", "throttled_or_server_error"}


@dataclass
class ItemOutcome:
    asin: str
    status: str
    actual_price: Optional[float]
    product_title: Optional[str]
    url: Optional[str]
    error_reason: Optional[str]
    mrp: Optional[float] = None
    seller: Optional[str] = None


def classify(
    fetch: FetchResult, parsed: Optional[ParsedProduct], expected_price: float, tolerance_abs: float, tolerance_pct: float
) -> tuple[str, Optional[str], bool]:
    """Single source of truth for both a row's status AND whether this
    particular attempt should be retried. Keeping these in one function
    (rather than a separate _is_retryable check re-deriving its answer from
    raw fetch/parse fields) avoids the two disagreeing — e.g. a 404 body
    that also happens to fail the parser's "product" heuristic must still
    resolve to NOT_FOUND (not retried), which only works if the status code
    is checked before the page-kind fallback, in one place.

    Returns (status, error_reason, retryable). status is one of
    checkpoint.STATUS_*; retryable is only meaningful when status is
    STATUS_FAILED (all other statuses are always terminal on first read)."""
    if fetch.error:
        return checkpoint.STATUS_FAILED, fetch.error, fetch.error in _RETRYABLE_ERRORS
    if fetch.status_code == 404:
        return checkpoint.STATUS_NOT_FOUND, "HTTP 404", False
    if parsed is None:
        return checkpoint.STATUS_FAILED, "empty_response", True

    if parsed.page_kind == "captcha":
        return checkpoint.STATUS_FAILED, "captcha_challenge", True
    if parsed.page_kind == "blocked":
        return checkpoint.STATUS_FAILED, "soft_block_or_throttle", True
    if parsed.page_kind == "not_found":
        return checkpoint.STATUS_NOT_FOUND, "product page reported not found", False
    if parsed.page_kind == "unknown":
        return checkpoint.STATUS_FAILED, "page did not match expected product layout (selector drift?)", True

    # page_kind == "product" — always a terminal read, never retried.
    if parsed.is_in_stock is False:
        reason = (parsed.availability_raw or "").lower()
        status = checkpoint.STATUS_OUT_OF_STOCK if "out of stock" in reason else checkpoint.STATUS_UNAVAILABLE
        return status, parsed.availability_raw or "unavailable", False
    if parsed.is_in_stock is None:
        return checkpoint.STATUS_UNAVAILABLE, parsed.availability_raw or "availability could not be determined", False

    if parsed.price is None:
        return checkpoint.STATUS_FAILED, "in stock but price could not be parsed (selector drift?)", False

    if prices_match(expected_price, parsed.price, tolerance_abs, tolerance_pct):
        return checkpoint.STATUS_MATCHED, None, False
    return checkpoint.STATUS_MISMATCHED, None, False


async def _process_one(
    client,
    item: checkpoint.RunItemRow,
    run_id: str,
    url_for: Callable[[str], str],
    tolerance_abs: float,
    tolerance_pct: float,
    gate: DynamicGate,
    pause: PauseGate,
    breaker: CircuitBreaker,
    db_path,
    on_item_done: Optional[Callable[[ItemOutcome], Awaitable[None]]],
) -> None:
    last_error = "unknown"
    for attempt in range(1, config.MAX_ATTEMPTS + 1):
        await pause.wait()
        await gate.acquire()
        try:
            fetch = await fetch_product_page(client, item.asin)
        finally:
            await gate.release()
        await asyncio.to_thread(checkpoint.record_attempt, run_id, item.asin, db_path)

        parsed = parse_product_page(fetch.html, item.asin) if fetch.html else None
        status, reason, retryable = classify(fetch, parsed, item.expected_price, tolerance_abs, tolerance_pct)

        if retryable:
            last_error = reason or "unknown"
            cooldown = await breaker.record_failure()
            if cooldown is not None:
                new_capacity = max(1, gate.capacity // 2)
                await gate.resize(new_capacity)
                logger.warning(
                    "Circuit breaker tripped (run=%s): cooling down %.0fs, concurrency %d",
                    run_id, cooldown, new_capacity,
                )
                await pause.pause(cooldown)
            else:
                await asyncio.sleep(backoff_delay(attempt))
            continue

        await breaker.record_success()
        await asyncio.to_thread(
            checkpoint.mark_item_result,
            run_id=run_id, asin=item.asin, status=status,
            actual_price=parsed.price if parsed else None,
            product_title=parsed.title if parsed else None,
            mrp=parsed.mrp if parsed else None,
            seller=parsed.seller if parsed else None,
            url=url_for(item.asin), error_reason=reason, db_path=db_path,
        )
        if on_item_done:
            await on_item_done(ItemOutcome(
                item.asin, status, parsed.price if parsed else None, parsed.title if parsed else None,
                url_for(item.asin), reason, parsed.mrp if parsed else None, parsed.seller if parsed else None,
            ))
        return

    # Attempts exhausted on a retryable failure — terminal FAILED row.
    await asyncio.to_thread(
        checkpoint.mark_item_result,
        run_id=run_id, asin=item.asin, status=checkpoint.STATUS_FAILED,
        error_reason=f"exhausted {config.MAX_ATTEMPTS} attempts, last error: {last_error}",
        url=url_for(item.asin), db_path=db_path,
    )
    if on_item_done:
        await on_item_done(ItemOutcome(item.asin, checkpoint.STATUS_FAILED, None, None, url_for(item.asin), last_error))


async def run_pipeline(
    run_id: str,
    items: list[checkpoint.RunItemRow],
    concurrency: int,
    tolerance_abs: float,
    tolerance_pct: float,
    session: Optional[BootstrappedSession] = None,
    db_path=config.DB_PATH,
    on_item_done: Optional[Callable[[ItemOutcome], Awaitable[None]]] = None,
) -> None:
    """Processes every item in `items` (already filtered to pending/failed by
    the caller via checkpoint.get_pending_and_retryable_items). Each item
    gets a fresh MAX_ATTEMPTS budget for this pass — safe to call again
    after a crash or an explicit resume with a fresh `items` list, since
    resumability comes from re-querying the DB for non-terminal rows, not
    from any state held in memory here.

    `session` is optional and unused by default: the vendor confirmed price
    doesn't vary by pincode for this catalog, so a plain anonymous client
    (no prior browser session, no cookie jar) is the default fetch path.
    Pass a BootstrappedSession only if a future run genuinely needs a
    warmed-up session for other reasons."""
    if not items:
        return

    def url_for(asin: str) -> str:
        return f"{config.MARKETPLACE_BASE_URL}/dp/{asin}"

    client = build_client(session.cookies, session.user_agent) if session else build_anonymous_client()
    gate = DynamicGate(concurrency)
    pause = PauseGate()
    breaker = CircuitBreaker()

    try:
        tasks = [
            _process_one(client, item, run_id, url_for, tolerance_abs, tolerance_pct, gate, pause, breaker, db_path, on_item_done)
            for item in items
        ]
        await asyncio.gather(*tasks)
    finally:
        await client.aclose()
