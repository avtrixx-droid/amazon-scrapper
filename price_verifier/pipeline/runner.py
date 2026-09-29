"""
runner.py — orchestrates one pipeline invocation over a run's pending rows:
fetch -> parse -> classify -> compare -> checkpoint, in up to three passes.

Why passes: a live run showed Amazon soft-blocks an identity after a short
burst, and a blocked identity never recovers by waiting on it — the old
circuit breaker cooled down 60 s, 120 s, 180 s and retried on the SAME
session, so half the rows failed after ten minutes. Now:

  Pass 1 "fast"      One shared FetchSession, paced by an AIMD
                     AdaptiveRateLimiter. A block/captcha halves the rate,
                     pauses everyone briefly and rotates the identity ONCE
                     per block event (generation counter + lock, so ten
                     concurrent blocked responses cause one rotation, not
                     ten). Each row gets MAX_ATTEMPTS_FAST tries here; rows
                     still unsettled are deferred. Ambiguous product pages
                     (in stock but no price, or no availability and no
                     price) go straight to the browser pass. If blocks keep
                     coming with no success in between, the pass gives up
                     early and defers everything left.
  Pass 2 "recovery"  Only if pass 1 deferred rows. A short pause, then a
                     brand-new identity at a slow fixed rate; rotation on
                     block; same early give-up rule.
  Pass 3 "browser"   Real Chrome (fetcher/browser_fallback.py), sequential,
                     a few seconds apart. Ambiguous pages are final here.
                     A captcha/block restarts Chrome (fresh profile) after a
                     pause and retries once.

Whatever is still unsettled ends STATUS_FAILED with its last reason — and
stays retryable (checkpoint.get_pending_and_retryable_items /
reopen_run_for_retry). Every row gets exactly ONE final write per
invocation (unless cancelled), always with `resolved_by` and
`scraped_brand`; `on_item_done` fires once per finalized row.

classify() is the single place that turns a (FetchResult, ParsedProduct)
pair into a row status + what to do next — fetcher/parser know nothing of
run/DB vocabulary on purpose.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable, Optional

from price_verifier import config
from price_verifier.fetcher import browser_fallback
from price_verifier.fetcher.http_client import FetchSession
from price_verifier.fetcher.models import FetchResult, ParsedProduct
from price_verifier.fetcher.parser import parse_product_page
from price_verifier.pipeline.compare import prices_match
from price_verifier.pipeline.rate_limiter import AdaptiveRateLimiter, LimiterClosed
from price_verifier.storage import checkpoint

logger = logging.getLogger("price_verifier.runner")

ACTION_FINAL = "final"
ACTION_RETRY = "retry"
ACTION_BROWSER = "browser"

PHASE_FAST = "fast"
PHASE_RECOVERY = "recovery"
PHASE_BROWSER = "browser"
PHASE_DONE = "done"
PHASE_CANCELLED = "cancelled"

REASON_CAPTCHA = "captcha_challenge"
REASON_BLOCKED = "soft_block_or_throttle"
REASON_EMPTY = "empty_response"
REASON_UNKNOWN_PAGE = "page did not match expected product layout"

# HTTP statuses that mean "this identity is being throttled/blocked" (as
# opposed to a transient server error, which is retried without rotation).
_BLOCK_STATUS_CODES = frozenset({403, 429, 503})


# ── Public data types ─────────────────────────────────────────────────────────
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
    scraped_brand: Optional[str] = None
    resolved_by: Optional[str] = None   # checkpoint.RESOLVED_BY_*


@dataclass
class PassStats:
    ran: bool = False
    items_in: int = 0
    resolved: int = 0          # rows finalized by this pass (any status, incl. FAILED)
    deferred: int = 0          # rows handed on to a later pass
    attempts: int = 0          # fetches made
    blocks: int = 0            # distinct block events (coalesced)
    block_reports: int = 0     # individual blocked/captcha/throttled responses
    rotations: int = 0         # identity rotations (HTTP) / Chrome restarts (browser)
    aborted: bool = False      # gave up early after a streak of blocks
    final_rps: Optional[float] = None
    lowest_rps: Optional[float] = None
    duration_s: float = 0.0


@dataclass
class RunStats:
    total_items: int = 0
    finalized: int = 0
    cancelled: bool = False
    status_counts: dict = field(default_factory=dict)   # status -> rows finalized with it
    resolved_by: dict = field(default_factory=dict)     # "http"|"recovery"|"browser" -> rows
    fast: PassStats = field(default_factory=PassStats)
    recovery: PassStats = field(default_factory=PassStats)
    browser: PassStats = field(default_factory=PassStats)
    browser_available: Optional[bool] = None            # None = browser pass never needed
    duration_s: float = 0.0

    @property
    def failed(self) -> int:
        return self.status_counts.get(checkpoint.STATUS_FAILED, 0)

    @property
    def pending(self) -> int:
        return self.total_items - self.finalized

    @property
    def blocks(self) -> int:
        return self.fast.blocks + self.recovery.blocks + self.browser.blocks

    @property
    def rotations(self) -> int:
        return self.fast.rotations + self.recovery.rotations + self.browser.rotations

    @property
    def final_rps(self) -> Optional[float]:
        return self.fast.final_rps

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(failed=self.failed, pending=self.pending, blocks=self.blocks,
                 rotations=self.rotations, final_rps=self.final_rps)
        return d



class ChromeCallTimeout(Exception):
    pass


async def _chrome_call(fn, *args, timeout: float, on_abandon: Optional[Callable[[], None]] = None):
    """Run a blocking Chrome/undetected-chromedriver call on its own DAEMON
    thread with a timeout. asyncio.to_thread would use the loop's default
    executor, which asyncio.run() joins with no timeout on shutdown — so one
    call stuck forever (e.g. uc's first-run chromedriver download through a
    proxy that stalls rather than refuses) would freeze the run, and Pause
    could never finish it. Here the caller gets ChromeCallTimeout instead, the
    thread is abandoned, and `on_abandon` (e.g. fetcher.close) runs on that
    thread if the call ever does return, so a late-starting Chrome is quit
    rather than orphaned."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    abandoned = threading.Event()

    def deliver(result, exc):
        if not fut.done():
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(result)

    def target():
        try:
            result, exc = fn(*args), None
        except BaseException as e:  # noqa: BLE001 — relayed to the awaiting coroutine
            result, exc = None, e
        if abandoned.is_set():
            if on_abandon is not None:
                try:
                    on_abandon()
                except Exception:
                    pass
            return
        try:
            loop.call_soon_threadsafe(deliver, result, exc)
        except RuntimeError:  # loop already closed
            if on_abandon is not None:
                try:
                    on_abandon()
                except Exception:
                    pass

    threading.Thread(target=target, daemon=True, name="pv-chrome-call").start()
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except asyncio.TimeoutError:
        abandoned.set()
        raise ChromeCallTimeout(f"Chrome did not respond within {timeout:.0f}s") from None
    except asyncio.CancelledError:
        abandoned.set()
        raise

@dataclass
class PipelineTuning:
    """Every timing/limit knob, read from config at call time. Tests pass a
    tuned-down instance via run_pipeline(tuning=...)."""

    fast_initial_rps: float = 2.0
    fast_min_rps: float = 0.5
    fast_max_rps: float = 6.0
    rate_increase_step: float = 0.25
    rate_increase_every: int = 10
    rate_jitter: float = 0.25
    block_pause_base: float = 15.0
    block_pause_max: float = 60.0
    block_escalation_window: float = 120.0
    block_decay_successes: int = 25
    max_attempts_fast: int = 2
    fast_abort_block_streak: int = 4
    fast_abort_error_streak: int = 30
    retry_backoff: float = 1.5
    recovery_pause: float = 20.0
    recovery_rps: float = 0.5
    recovery_concurrency: int = 2
    max_attempts_recovery: int = 2
    recovery_abort_block_streak: int = 3
    recovery_abort_error_streak: int = 10
    browser_headless: bool = True
    browser_page_timeout: int = 30
    browser_gap_min: float = 2.0
    browser_gap_max: float = 4.0
    browser_block_pause: float = 30.0
    browser_abort_block_streak: int = 3
    browser_start_timeout: float = 180.0
    browser_call_timeout: float = 120.0

    @classmethod
    def from_config(cls) -> "PipelineTuning":
        return cls(
            fast_initial_rps=config.FAST_INITIAL_RPS,
            fast_min_rps=config.FAST_MIN_RPS,
            fast_max_rps=config.FAST_MAX_RPS,
            rate_increase_step=config.RATE_INCREASE_STEP,
            rate_increase_every=config.RATE_INCREASE_EVERY,
            rate_jitter=config.RATE_JITTER_FRACTION,
            block_pause_base=config.BLOCK_PAUSE_BASE_SECONDS,
            block_pause_max=config.BLOCK_PAUSE_MAX_SECONDS,
            block_escalation_window=config.BLOCK_ESCALATION_WINDOW_SECONDS,
            block_decay_successes=config.BLOCK_DECAY_SUCCESSES,
            max_attempts_fast=config.MAX_ATTEMPTS_FAST,
            fast_abort_block_streak=config.FAST_ABORT_BLOCK_STREAK,
            fast_abort_error_streak=config.FAST_ABORT_ERROR_STREAK,
            retry_backoff=config.RETRY_BACKOFF_SECONDS,
            recovery_pause=config.RECOVERY_PAUSE_SECONDS,
            recovery_rps=config.RECOVERY_RPS,
            recovery_concurrency=config.RECOVERY_CONCURRENCY,
            max_attempts_recovery=config.MAX_ATTEMPTS_RECOVERY,
            recovery_abort_block_streak=config.RECOVERY_ABORT_BLOCK_STREAK,
            recovery_abort_error_streak=config.RECOVERY_ABORT_ERROR_STREAK,
            browser_headless=config.BROWSER_HEADLESS,
            browser_page_timeout=config.BROWSER_PAGE_TIMEOUT_SECONDS,
            browser_gap_min=config.BROWSER_GAP_MIN_SECONDS,
            browser_gap_max=config.BROWSER_GAP_MAX_SECONDS,
            browser_block_pause=config.BROWSER_BLOCK_PAUSE_SECONDS,
            browser_abort_block_streak=config.BROWSER_ABORT_BLOCK_STREAK,
            browser_start_timeout=config.BROWSER_START_TIMEOUT_SECONDS,
            browser_call_timeout=config.BROWSER_CALL_TIMEOUT_SECONDS,
        )


# ── Classification ────────────────────────────────────────────────────────────
def classify(
    fetch: FetchResult,
    parsed: Optional[ParsedProduct],
    expected_price: float,
    tolerance_abs: float,
    tolerance_pct: float,
    *,
    final_pass: bool = False,
) -> tuple[str, Optional[str], str]:
    """Returns (status, reason, action), action in {"final","retry","browser"}.

    - retry:   network error, 403/429/5xx, captcha, soft block, unknown page,
               empty body. status is STATUS_FAILED (tentative).
    - browser: a real product page that is ambiguous — in stock but no
               price, or availability unknown and no price. status is the
               tentative STATUS_UNAVAILABLE. With final_pass=True (the
               browser pass itself) these become "final".
    - final:   matched / mismatched (tolerance via compare.py), explicit
               out-of-stock / unavailable, not found (404), no featured offer.

    The 404 check comes before any page-kind fallback so a 404 body the
    parser can't classify still resolves to NOT_FOUND, in one place."""
    if fetch.error:
        return checkpoint.STATUS_FAILED, fetch.error, ACTION_RETRY
    if fetch.status_code == 404:
        return checkpoint.STATUS_NOT_FOUND, "HTTP 404", ACTION_FINAL
    if fetch.status_code is not None and (fetch.status_code in _BLOCK_STATUS_CODES or fetch.status_code >= 500):
        return checkpoint.STATUS_FAILED, f"HTTP {fetch.status_code}", ACTION_RETRY
    if parsed is None or not fetch.html:
        return checkpoint.STATUS_FAILED, REASON_EMPTY, ACTION_RETRY

    kind = parsed.page_kind
    if kind == "captcha":
        return checkpoint.STATUS_FAILED, REASON_CAPTCHA, ACTION_RETRY
    if kind == "blocked":
        return checkpoint.STATUS_FAILED, REASON_BLOCKED, ACTION_RETRY
    if kind == "not_found":
        return checkpoint.STATUS_NOT_FOUND, "product page reported not found", ACTION_FINAL
    if kind != "product":
        return checkpoint.STATUS_FAILED, REASON_UNKNOWN_PAGE, ACTION_RETRY

    if parsed.is_in_stock is False:
        raw = parsed.availability_raw or ""
        status = checkpoint.STATUS_OUT_OF_STOCK if "out of stock" in raw.lower() else checkpoint.STATUS_UNAVAILABLE
        return status, raw or "unavailable", ACTION_FINAL
    if getattr(parsed, "no_featured_offer", False):
        return (checkpoint.STATUS_NO_FEATURED_OFFER,
                "no featured offer (no buy-box winner; see all buying options)", ACTION_FINAL)

    if parsed.price is None:
        if parsed.is_in_stock is True:
            reason = "in stock but no price shown"
        else:
            reason = parsed.availability_raw or "no price and availability could not be determined"
        if final_pass:
            return checkpoint.STATUS_UNAVAILABLE, f"{reason} (checked in Chrome)", ACTION_FINAL
        return checkpoint.STATUS_UNAVAILABLE, reason, ACTION_BROWSER

    if prices_match(expected_price, parsed.price, tolerance_abs, tolerance_pct):
        return checkpoint.STATUS_MATCHED, None, ACTION_FINAL
    return checkpoint.STATUS_MISMATCHED, None, ACTION_FINAL


def is_block_signal(fetch: FetchResult, parsed: Optional[ParsedProduct]) -> bool:
    """True if this response means the current identity is being blocked /
    throttled (rotate + slow down), vs. a transient error (just retry)."""
    if fetch.status_code in _BLOCK_STATUS_CODES:
        return True
    return parsed is not None and parsed.page_kind in ("captcha", "blocked")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _save_debug_html(asin: str, html: Optional[str], reason: str, source: str) -> None:
    """Lazy + best-effort: fetcher/debug_dump.py may not exist in every build."""
    try:
        from price_verifier.fetcher.debug_dump import save_debug_html
    except ImportError:
        return
    try:
        save_debug_html(asin, html, reason, source=source)
    except Exception:
        logger.debug("save_debug_html failed for %s", asin, exc_info=True)


def _url_for(asin: str) -> str:
    return f"{config.MARKETPLACE_BASE_URL}/dp/{asin}"


@dataclass
class _Work:
    """A row moving through the passes. Deliberately holds no HTML (a
    deferred 2,000-row run must not pin 2,000 pages in memory)."""

    item: checkpoint.RunItemRow
    last_reason: str = "not attempted"
    last_source: str = checkpoint.RESOLVED_BY_HTTP


class _SessionManager:
    """Owns the current FetchSession identity and its generation number.

    Invariants:
      - a fetch never STARTS on a generation already reported blocked
        (fetches wait on `_ready` while a rotation is in progress);
      - one rotation per blocked generation, however many concurrent
        responses report it (lock + generation check);
      - the replaced session is closed once its in-flight fetches drain.
    """

    def __init__(self, factory: Callable[[], object]):
        self._factory = factory
        self._session = None
        self.generation = 0
        self._blocked: set[int] = set()
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._in_flight: dict[int, int] = {}
        self._retired: dict[int, object] = {}
        self.rotations = 0
        self.reuse_violations = 0   # must stay 0 — asserted by tests

    async def _new_session(self):
        session = self._factory()
        try:
            await session.start(warm_up=True)
        except Exception:
            logger.warning("FetchSession.start raised (contract says it never does)", exc_info=True)
        return session

    async def start(self) -> None:
        self._session = await self._new_session()
        self.generation = 1
        self._ready.set()

    async def fetch(self, asin: str) -> tuple[FetchResult, int]:
        while True:
            await self._ready.wait()
            gen, session = self.generation, self._session
            if gen not in self._blocked:
                break
            # Defensive (report_block normally keeps _ready clear until the
            # swap is done): never start a fetch on a known-blocked identity.
            await self.report_block(gen)
        self._in_flight[gen] = self._in_flight.get(gen, 0) + 1
        try:
            try:
                result = await session.fetch(asin)
            except Exception as e:  # contract says never raises; don't trust it blindly
                result = FetchResult(asin=asin, status_code=None, html=None, error=f"fetch_error:{type(e).__name__}")
            return result, gen
        finally:
            self._in_flight[gen] -= 1
            await self._close_if_drained(gen)

    async def _close_if_drained(self, gen: int) -> None:
        if gen in self._retired and self._in_flight.get(gen, 0) == 0:
            old = self._retired.pop(gen)
            try:
                await old.close()
            except Exception:
                pass

    def claim_block(self, gen: int) -> bool:
        """Synchronously mark `gen` blocked. True only for the FIRST report
        against the CURRENT identity — i.e. a new block event, which is what
        the rate limiter should count and pause for. Reports against an
        identity that is already blocked / already rotated away are echoes of
        that same event. Keyed on the session generation the request actually
        used (not on when it acquired its rate-limit slot), so a freshly
        rotated identity that is blocked too always counts as a new event —
        otherwise identities could be burned back-to-back with no pause."""
        new = gen == self.generation and gen not in self._blocked
        self._blocked.add(gen)
        if gen == self.generation:
            self._ready.clear()  # stop new fetches on the blocked identity immediately
        return new

    async def report_block(self, gen: int) -> bool:
        """Mark `gen` blocked; rotate if it is still current. Returns True if
        this call performed the rotation."""
        self._blocked.add(gen)
        if gen != self.generation:
            return False
        self._ready.clear()  # stop new fetches on the blocked identity immediately
        async with self._lock:
            if gen != self.generation:
                return False
            try:
                new_session = await self._new_session()
            except BaseException:
                self._ready.set()  # don't strand waiters; the error propagates and ends the pass
                raise
            old = self._session
            self._session = new_session
            self.generation += 1
            self.rotations += 1
            self._retired[gen] = old
            self._ready.set()
        await self._close_if_drained(gen)
        return True

    async def close(self) -> None:
        for s in [self._session, *self._retired.values()]:
            if s is None:
                continue
            try:
                await s.close()
            except Exception:
                pass
        self._retired.clear()
        self._session = None


@dataclass
class _PassCtx:
    name: str
    resolved_by: str
    limiter: AdaptiveRateLimiter
    sessions: _SessionManager
    max_attempts: int
    abort_streak: int          # block events with no success in between
    error_abort_streak: int    # failed attempts of any kind with no success in between
    stats: PassStats
    retry_out: list = field(default_factory=list)
    browser_out: list = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    error_streak: int = 0


class _Pipeline:
    def __init__(self, run_id, items, *, concurrency, tolerance_abs, tolerance_pct, use_browser_fallback,
                 db_path, on_item_done, on_phase, cancel_event, session_factory, browser_factory, tuning):
        self.run_id = run_id
        self.items = list(items)
        self.concurrency = max(1, int(concurrency))
        self.tol_abs = tolerance_abs
        self.tol_pct = tolerance_pct
        self.use_browser = use_browser_fallback
        self.db_path = db_path
        self.on_item_done = on_item_done
        self.on_phase = on_phase
        self.cancel_event = cancel_event
        self.session_factory = session_factory or (lambda: FetchSession())
        self.browser_factory = browser_factory
        self.t = tuning or PipelineTuning.from_config()
        self.stats = RunStats(total_items=len(self.items))
        self._finalized: set[str] = set()
        self._finalizers: set[asyncio.Task] = set()
        self._rng = random.Random()

    # ── small async wrappers (all DB / file I/O off the event loop) ─────────
    async def _db(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, db_path=self.db_path, **kwargs)

    async def _debug(self, asin, html, reason, source):
        await asyncio.to_thread(_save_debug_html, asin, html, reason, source)

    async def _phase(self, phase: str, detail: dict, *, persist: bool = False) -> None:
        if persist:
            await self._db(checkpoint.set_run_phase, self.run_id, phase)
        if self.on_phase is not None:
            try:
                await self.on_phase(phase, detail)
            except Exception:
                logger.exception("on_phase callback raised (ignored)")

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep that returns early if the run is cancelled."""
        if seconds <= 0:
            return
        if self.cancel_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self.cancel_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ── finalization (exactly once per row) ────────────────────────────────
    async def _finalize(self, work: _Work, status: str, reason: Optional[str], parsed: Optional[ParsedProduct],
                        resolved_by: str, html: Optional[str], source: str) -> None:
        asin = work.item.asin
        if asin in self._finalized:
            logger.error("BUG: attempted second final write for %s (ignored)", asin)
            return
        self._finalized.add(asin)
        # Shielded: once a row's final write starts, a cancel must not leave
        # it half-done (DB written but on_item_done never fired).
        task = asyncio.ensure_future(self._do_finalize(work, status, reason, parsed, resolved_by, html, source))
        self._finalizers.add(task)
        task.add_done_callback(self._finalizers.discard)
        await asyncio.shield(task)

    async def _do_finalize(self, work, status, reason, parsed, resolved_by, html, source):
        item = work.item
        p = parsed if parsed is not None and parsed.page_kind == "product" else None
        outcome = ItemOutcome(
            asin=item.asin, status=status,
            actual_price=p.price if p else None,
            product_title=p.title if p else None,
            url=_url_for(item.asin), error_reason=reason,
            mrp=p.mrp if p else None, seller=p.seller if p else None,
            scraped_brand=getattr(p, "brand", None) if p else None,
            resolved_by=resolved_by,
        )
        await self._db(
            checkpoint.mark_item_result, run_id=self.run_id, asin=item.asin, status=status,
            actual_price=outcome.actual_price, mrp=outcome.mrp, seller=outcome.seller,
            product_title=outcome.product_title, url=outcome.url, error_reason=reason,
            scraped_brand=outcome.scraped_brand, resolved_by=resolved_by,
        )
        await self._after_final(outcome, html, source)

    async def _after_final(self, outcome: ItemOutcome, html: Optional[str], source: str) -> None:
        """Stats, debug dump and on_item_done — after the row's DB write."""
        status, resolved_by, reason = outcome.status, outcome.resolved_by, outcome.error_reason
        item_asin = outcome.asin
        self.stats.finalized += 1
        self.stats.status_counts[status] = self.stats.status_counts.get(status, 0) + 1
        self.stats.resolved_by[resolved_by] = self.stats.resolved_by.get(resolved_by, 0) + 1
        # Final answers (OOS / unavailable / not found / no buy box) are
        # correct results, not parser puzzles — only unresolved pages are kept.
        if status == checkpoint.STATUS_FAILED and html:
            await self._debug(item_asin, html, f"{status}: {reason}", source)
        if self.on_item_done is not None:
            try:
                await self.on_item_done(outcome)
            except Exception:
                logger.exception("on_item_done callback raised (ignored)")

    async def _fail(self, work: _Work, note: str, resolved_by: Optional[str] = None,
                    html: Optional[str] = None, source: Optional[str] = None) -> None:
        reason = f"{note} (last error: {work.last_reason})" if work.last_reason else note
        rb = resolved_by or work.last_source
        src = source or ("browser" if rb == checkpoint.RESOLVED_BY_BROWSER else "http")
        await self._finalize(work, checkpoint.STATUS_FAILED, reason, None, rb, html, src)

    async def _fail_many(self, works: list[_Work], note: str, resolved_by: Optional[str] = None) -> None:
        """_fail() for a whole batch in ONE database transaction. Used when a
        pass gives up on everything left (Chrome missing / dead / blocked,
        double-check turned off): per-row writes would take minutes on a
        slow Windows disk for a large run, with the UI sitting still."""
        todo = [w for w in works if w.item.asin not in self._finalized]
        if not todo:
            return
        outcomes = []
        for w in todo:
            self._finalized.add(w.item.asin)
            reason = f"{note} (last error: {w.last_reason})" if w.last_reason else note
            outcomes.append(ItemOutcome(
                asin=w.item.asin, status=checkpoint.STATUS_FAILED, actual_price=None, product_title=None,
                url=_url_for(w.item.asin), error_reason=reason, mrp=None, seller=None, scraped_brand=None,
                resolved_by=resolved_by or w.last_source,
            ))

        async def write_all():
            await self._db(checkpoint.mark_items_failed, self.run_id,
                           [(o.asin, o.error_reason, o.url, o.resolved_by) for o in outcomes])
            for o in outcomes:
                await self._after_final(o, None, "http")

        task = asyncio.ensure_future(write_all())  # shielded, like _finalize
        self._finalizers.add(task)
        task.add_done_callback(self._finalizers.discard)
        await asyncio.shield(task)

    # ── HTTP passes ──────────────────────────────────────────────────────────
    async def _http_item(self, ctx: _PassCtx, work: _Work) -> None:
        item = work.item
        attempted = False
        for _attempt in range(ctx.max_attempts):
            if ctx.aborted:
                break
            try:
                await ctx.limiter.acquire()
            except LimiterClosed:
                break
            try:
                if ctx.aborted:
                    break
                fetch, gen = await ctx.sessions.fetch(item.asin)
            finally:
                ctx.limiter.release()
            ctx.stats.attempts += 1
            attempted = True
            work.last_source = ctx.resolved_by

            parsed = parse_product_page(fetch.html, item.asin) if fetch.html else None
            status, reason, action = classify(fetch, parsed, item.expected_price, self.tol_abs, self.tol_pct)

            # Every pacing / blocking / abort decision is made synchronously,
            # BEFORE the first await: DB writes hop to a thread (slow on a
            # Windows disk), and until the limiter pauses, the identity is
            # marked blocked and a dead pass is aborted, the other workers
            # keep sending requests on a flagged identity / dead network.
            blocked = new_event = False
            pause = 0.0
            if action in (ACTION_FINAL, ACTION_BROWSER):
                ctx.limiter.on_success()  # for BROWSER: the fetch worked, the page is just ambiguous
                ctx.error_streak = 0
            else:
                ctx.error_streak += 1
                work.last_reason = reason or "unknown"
                if not ctx.aborted and is_block_signal(fetch, parsed):
                    blocked = True
                    ctx.stats.block_reports += 1
                    new_event = ctx.sessions.claim_block(gen)
                    pause = ctx.limiter.on_block(ctx.limiter.epoch if new_event else -1)
                    if new_event:
                        ctx.stats.blocks += 1
                        if ctx.limiter.block_streak >= ctx.abort_streak:
                            self._abort_pass(ctx, f"{ctx.limiter.block_streak} blocks in a row", work.last_reason)
                if ctx.error_streak >= ctx.error_abort_streak:
                    self._abort_pass(ctx, f"{ctx.error_streak} failed requests in a row", work.last_reason)

            if blocked:
                # Always follows claim_block(): it re-opens the session gate
                # that claim_block closed, so no worker is left waiting on it.
                rotated = await ctx.sessions.report_block(gen)
                if rotated:
                    ctx.stats.rotations += 1
                if new_event:
                    logger.warning("[%s] block event (%s) on %s: pause %.1fs, rate now %.2f rps, rotated=%s",
                                   ctx.name, reason, item.asin, pause, ctx.limiter.current_rps, rotated)
                    await self._debug(item.asin, fetch.html, f"block: {reason}", "http")
                    await self._phase(ctx.name, {
                        "event": "block", "reason": reason, "pause_seconds": round(pause, 2),
                        "rps": round(ctx.limiter.current_rps, 3), "blocks": ctx.stats.blocks,
                        "rotations": ctx.stats.rotations,
                    })

            await self._db(checkpoint.record_attempt, self.run_id, item.asin)

            if action == ACTION_FINAL:
                ctx.stats.resolved += 1
                await self._finalize(work, status, reason, parsed, ctx.resolved_by, fetch.html, "http")
                return
            if action == ACTION_BROWSER:
                work.last_reason = reason or "ambiguous product page"
                await self._db(checkpoint.note_item_attempt_failure, self.run_id, item.asin,
                               f"needs Chrome double-check: {work.last_reason}")
                await self._debug(item.asin, fetch.html, f"ambiguous: {work.last_reason}", "http")
                ctx.browser_out.append(work)
                ctx.stats.deferred += 1
                return

            # ACTION_RETRY
            await self._db(checkpoint.note_item_attempt_failure, self.run_id, item.asin, work.last_reason)
            if not blocked and not ctx.aborted:
                await self._sleep(self.t.retry_backoff * (0.5 + self._rng.random()))

        if not attempted and ctx.aborted:
            work.last_reason = ctx.abort_reason
        ctx.retry_out.append(work)
        ctx.stats.deferred += 1

    @staticmethod
    def _abort_pass(ctx: _PassCtx, why: str, last_reason: str) -> None:
        """Give up on this pass: every queued/in-progress row is deferred to
        the next pass right away instead of burning attempts on a session,
        IP or network that is clearly not working."""
        if ctx.aborted:
            return
        ctx.aborted = True
        ctx.stats.aborted = True
        ctx.abort_reason = f"skipped: {ctx.name} pass stopped after {why}, last seen {last_reason}"
        ctx.limiter.close()
        logger.warning("[%s] %s (last: %s) — deferring the remaining rows to the next pass",
                       ctx.name, why, last_reason)

    async def _run_http_pass(self, ctx: _PassCtx, works: list[_Work]) -> None:
        ctx.stats.ran = True
        ctx.stats.items_in = len(works)
        t0 = time.monotonic()
        await ctx.sessions.start()
        try:
            tasks = [asyncio.ensure_future(self._http_item(ctx, w)) for w in works]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        finally:
            ctx.stats.duration_s = round(time.monotonic() - t0, 3)
            ctx.stats.final_rps = round(ctx.limiter.current_rps, 3)
            ctx.stats.lowest_rps = ctx.limiter.stats()["lowest_rps"]
            await ctx.sessions.close()

    def _fast_ctx(self) -> _PassCtx:
        t = self.t
        limiter = AdaptiveRateLimiter(
            t.fast_initial_rps, t.fast_min_rps, t.fast_max_rps, self.concurrency,
            t.rate_increase_step, t.rate_increase_every,
            base_pause=t.block_pause_base, max_pause=t.block_pause_max,
            escalation_window=t.block_escalation_window, decay_successes=t.block_decay_successes,
            jitter=t.rate_jitter,
        )
        return _PassCtx(PHASE_FAST, checkpoint.RESOLVED_BY_HTTP, limiter, _SessionManager(self.session_factory),
                        t.max_attempts_fast, t.fast_abort_block_streak, t.fast_abort_error_streak, self.stats.fast)

    def _recovery_ctx(self) -> _PassCtx:
        t = self.t
        limiter = AdaptiveRateLimiter(
            t.recovery_rps, t.recovery_rps, t.recovery_rps, t.recovery_concurrency, 0.0, 1,
            base_pause=t.block_pause_base, max_pause=t.block_pause_max,
            escalation_window=t.block_escalation_window, decay_successes=t.block_decay_successes,
            jitter=t.rate_jitter,
        )
        return _PassCtx(PHASE_RECOVERY, checkpoint.RESOLVED_BY_RECOVERY, limiter,
                        _SessionManager(self.session_factory), t.max_attempts_recovery,
                        t.recovery_abort_block_streak, t.recovery_abort_error_streak, self.stats.recovery)

    # ── Browser pass ─────────────────────────────────────────────────────────
    async def _browser_pass(self, works: list[_Work]) -> None:
        st = self.stats.browser
        st.items_in = len(works)
        if not works:
            return
        if not self.use_browser:
            await self._fail_many(works, "could not be verified (Chrome double-check is turned off)")
            return
        available = self.browser_factory is not None
        if not available:
            try:
                available = bool(await _chrome_call(browser_fallback.chrome_available, timeout=60.0))
            except ChromeCallTimeout:
                available = False
        self.stats.browser_available = available
        if not available:
            await self._fail_many(works, "could not be verified (Google Chrome not found for a double-check)")
            return

        st.ran = True
        t0 = time.monotonic()
        await self._phase(PHASE_BROWSER, {"event": "start", "items": len(works)}, persist=True)
        fetcher = (self.browser_factory() if self.browser_factory is not None else browser_fallback.BrowserFetcher(
            headless=self.t.browser_headless, page_timeout=self.t.browser_page_timeout))
        remaining = list(works)
        try:
            try:
                await _chrome_call(fetcher.start, timeout=self.t.browser_start_timeout, on_abandon=fetcher.close)
            except Exception as e:
                self.stats.browser_available = False
                if isinstance(e, browser_fallback.BrowserUnavailable):
                    msg = str(e)
                elif isinstance(e, ChromeCallTimeout):
                    msg = ("Chrome took too long to start — this network may be blocking the ChromeDriver "
                           "download; retry later or on another network")
                else:
                    msg = "Chrome could not be started"
                await self._fail_many(remaining, f"could not be verified ({msg})")
                remaining = []
                return

            bad_streak = 0   # rows in a row that Chrome could not settle
            first = True
            while remaining:
                if self._cancelled():
                    return
                work = remaining[0]
                if not first:
                    await self._sleep(self._rng.uniform(self.t.browser_gap_min, self.t.browser_gap_max))
                    if self._cancelled():
                        return
                first = False
                outcome = await self._browser_item(fetcher, work)
                remaining.pop(0)
                bad_streak = 0 if outcome == "ok" else bad_streak + 1
                if outcome == "chrome_dead" or bad_streak >= self.t.browser_abort_block_streak:
                    st.aborted = True
                    if outcome == "chrome_dead":
                        note = "could not be verified (Chrome stopped working)"
                    elif outcome == "blocked":
                        note = "could not be verified (Amazon is also blocking Chrome right now; retry later)"
                    else:
                        note = "could not be verified (Chrome could not load Amazon pages either; retry later)"
                    await self._fail_many(remaining, note, resolved_by=checkpoint.RESOLVED_BY_BROWSER)
                    remaining = []
        finally:
            st.duration_s = round(time.monotonic() - t0, 3)
            try:
                await _chrome_call(fetcher.close, timeout=60.0)
            except Exception:
                logger.debug("BrowserFetcher.close raised", exc_info=True)

    async def _browser_item(self, fetcher, work: _Work) -> str:
        """Returns "ok" | "blocked" | "failed" | "chrome_dead"."""
        st = self.stats.browser
        item = work.item
        last_html = None
        was_block = False
        for attempt in (1, 2):
            try:
                fetch = await _chrome_call(fetcher.fetch, item.asin, timeout=self.t.browser_call_timeout)
            except ChromeCallTimeout:
                fetch = FetchResult(asin=item.asin, status_code=None, html=None,
                                    error="browser_error:Timeout", source="browser")
            st.attempts += 1
            work.last_source = checkpoint.RESOLVED_BY_BROWSER
            await self._db(checkpoint.record_attempt, self.run_id, item.asin)
            parsed = parse_product_page(fetch.html, item.asin) if fetch.html else None
            status, reason, action = classify(fetch, parsed, item.expected_price, self.tol_abs, self.tol_pct,
                                              final_pass=True)
            if action == ACTION_FINAL:
                st.resolved += 1
                await self._finalize(work, status, reason, parsed, checkpoint.RESOLVED_BY_BROWSER,
                                     fetch.html, "browser")
                return "ok"

            last_html = fetch.html
            work.last_reason = f"{reason} (in Chrome)"
            await self._db(checkpoint.note_item_attempt_failure, self.run_id, item.asin, work.last_reason)
            was_block = is_block_signal(fetch, parsed)
            if was_block:
                st.block_reports += 1
                st.blocks += 1
                await self._debug(item.asin, fetch.html, f"block: {reason}", "browser")
            if attempt == 2:
                break
            if was_block:
                await self._phase(PHASE_BROWSER, {"event": "block", "reason": reason,
                                                  "pause_seconds": self.t.browser_block_pause})
                await self._sleep(self.t.browser_block_pause)
                if self._cancelled():
                    break
            if was_block or fetch.error:
                # Fresh Chrome + fresh profile — never retry on the flagged one.
                try:
                    await _chrome_call(fetcher.restart, timeout=self.t.browser_start_timeout, on_abandon=fetcher.close)
                    st.rotations += 1
                except Exception:
                    logger.warning("Chrome restart failed", exc_info=True)
                    st.resolved += 1
                    await self._fail(work, "could not be verified (Chrome stopped working)",
                                     resolved_by=checkpoint.RESOLVED_BY_BROWSER, html=last_html, source="browser")
                    return "chrome_dead"

        if self._cancelled():
            return "failed"  # left pending on purpose
        st.resolved += 1
        await self._fail(work, "could not be verified after every method (HTTP, recovery, Chrome)",
                         resolved_by=checkpoint.RESOLVED_BY_BROWSER, html=last_html, source="browser")
        return "blocked" if was_block else "failed"

    # ── Orchestration ────────────────────────────────────────────────────────
    async def run(self) -> RunStats:
        t0 = time.monotonic()
        try:
            await self._run_passes()
        except asyncio.CancelledError:
            self.stats.cancelled = True
            raise
        finally:
            if self._finalizers:
                await asyncio.gather(*list(self._finalizers), return_exceptions=True)
            self.stats.cancelled = self.stats.cancelled or self._cancelled()
            self.stats.duration_s = round(time.monotonic() - t0, 3)
        phase = PHASE_CANCELLED if self.stats.cancelled else PHASE_DONE
        await self._db(checkpoint.set_run_phase, self.run_id, phase)
        await self._db(checkpoint.set_run_stats, self.run_id, self.stats.as_dict())
        await self._phase(phase, self.stats.as_dict())
        return self.stats

    async def _cancellable(self, coro) -> bool:
        """Run `coro`; if cancel_event fires first, cancel it. Returns True
        if it ran to completion."""
        task = asyncio.ensure_future(coro)
        if self.cancel_event is None:
            await task
            return True
        waiter = asyncio.ensure_future(self.cancel_event.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if task.done():
            task.result()  # re-raise real errors
            return True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return False

    async def _run_passes(self) -> None:
        works = [_Work(item) for item in self.items]
        if not works or self._cancelled():
            return

        # Pass 1 — fast
        fast = self._fast_ctx()
        await self._phase(PHASE_FAST, {"event": "start", "items": len(works), "concurrency": self.concurrency,
                                       "rps": round(fast.limiter.current_rps, 3)}, persist=True)
        if not await self._cancellable(self._run_http_pass(fast, works)):
            return
        to_browser = list(fast.browser_out)
        retry = list(fast.retry_out)

        # Pass 2 — recovery (fresh identity, slow)
        if retry:
            rec = self._recovery_ctx()
            await self._phase(PHASE_RECOVERY, {
                "event": "start", "items": len(retry), "pause_seconds": self.t.recovery_pause,
                "rps": self.t.recovery_rps, "concurrency": self.t.recovery_concurrency,
            }, persist=True)
            await self._sleep(self.t.recovery_pause)
            if self._cancelled():
                return
            if not await self._cancellable(self._run_http_pass(rec, retry)):
                return
            to_browser.extend(rec.browser_out)
            to_browser.extend(rec.retry_out)

        # Pass 3 — real Chrome
        if to_browser and not self._cancelled():
            if not await self._cancellable(self._browser_pass(to_browser)):
                return

        # Defensive: nothing should be left, but never leave a row silently pending.
        if not self._cancelled():
            for w in works:
                if w.item.asin not in self._finalized:
                    logger.error("BUG: %s reached the end of the pipeline unfinalized", w.item.asin)
                    await self._fail(w, "could not be verified")


async def run_pipeline(
    run_id: str,
    items: list[checkpoint.RunItemRow],
    *,
    concurrency: int,
    tolerance_abs: float,
    tolerance_pct: float,
    use_browser_fallback: bool = True,
    db_path=config.DB_PATH,
    on_item_done: Optional[Callable[[ItemOutcome], Awaitable[None]]] = None,
    on_phase: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    cancel_event: Optional[asyncio.Event] = None,
    session_factory: Optional[Callable[[], object]] = None,
    browser_factory: Optional[Callable[[], object]] = None,
    tuning: Optional[PipelineTuning] = None,
) -> RunStats:
    """Process `items` (normally checkpoint.get_pending_and_retryable_items)
    through the fast -> recovery -> browser passes described in the module
    docstring. Safe to call again on the same run after a crash, a cancel,
    or a "retry failed" click — resumability comes from re-querying the DB
    for pending/failed rows, not from anything held here.

    on_item_done(outcome): awaited once per row, when it is FINALIZED.
    on_phase(phase, detail): awaited at each pass start (detail["event"] ==
        "start"), on each coalesced block event ("block", with
        "pause_seconds"), and once at the end with phase "done" (or
        "cancelled") and detail = RunStats.as_dict().
    cancel_event: once set, nothing new is scheduled; unfinished rows stay
        pending (resume picks them up). Rows already mid-write complete.
    session_factory / browser_factory / tuning: test hooks.
    """
    pipeline = _Pipeline(
        run_id, items, concurrency=concurrency, tolerance_abs=tolerance_abs, tolerance_pct=tolerance_pct,
        use_browser_fallback=use_browser_fallback, db_path=db_path, on_item_done=on_item_done,
        on_phase=on_phase, cancel_event=cancel_event, session_factory=session_factory,
        browser_factory=browser_factory, tuning=tuning,
    )
    return await pipeline.run()
