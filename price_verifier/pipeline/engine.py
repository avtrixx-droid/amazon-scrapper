"""
engine.py — runs a pipeline invocation in its own process, supervised.

Why: the scraping engine is mostly native code — curl_cffi's
libcurl-impersonate, selectolax's HTML parser, Chrome driven over a socket.
When native code crashes it takes its whole process down with no Python
exception. With the engine inside the app's process that meant the web
server died too: the vendor's progress page went "Lost connection" and every
refresh got "connection refused".

So the app (Flask, the pages, the Excel report) never runs scraping code.
Each run gets an ENGINE PROCESS (engine_main) that reports back over a
queue:

    ("hb", t, chrome_pids)           heartbeat, every second
    ("item", outcome_dict)           one row finalized (already in SQLite)
    ("phase", phase, detail)         pass start / block event / end
    ("done", stats_dict, cancelled)  the invocation finished
    ("error", traceback_text)        it raised (also logged to app.log)

and EngineSupervisor, in the app, relays those to the UI and watches the
process. If it dies (any exit without "done", e.g. a native crash), stops
sending heartbeats (hung), or raises, the supervisor kills it and whatever
Chrome it had started, and starts a fresh engine on the rows this
invocation hasn't finalized yet — rows are written to SQLite one by one, so
nothing finished is lost or re-done. After `max_restarts` restarts it gives
up and the run ends as "crashed" (Resume offered, rows kept).

The engine uses the selector event loop on Windows: curl_cffi needs
add_reader(), which the default Proactor loop lacks — it then runs an extra
selector THREAD that bounces every socket event across threads. A plain
selector loop needs none of that.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from price_verifier import config

logger = logging.getLogger("price_verifier.engine")

HEARTBEAT_SECONDS = 1.0
_CRASH_FILE = None  # the engine's faulthandler target, open for the life of the process


# ── Engine process (child) ───────────────────────────────────────────────────
def _child_logging(log_dir: Path) -> None:
    """A spawned child starts with no logging config, and in the windowed
    .exe with sys.stdout/sys.stderr = None."""
    global _CRASH_FILE
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        if sys.stdout is None or sys.stderr is None:
            stream = open(log_dir / "console.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or stream
            sys.stderr = sys.stderr or stream
        crash = open(log_dir / "crash.log", "a", encoding="utf-8")
        crash.write(f"\n--- engine process {os.getpid()} started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        crash.flush()
        faulthandler.enable(file=crash, all_threads=True)
        _CRASH_FILE = crash
    except Exception:
        pass
    try:
        root = logging.getLogger()
        if not root.handlers:
            handler = logging.FileHandler(str(log_dir / "app.log"), encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)s | engine | %(name)s | %(message)s"))
            root.addHandler(handler)
            root.setLevel(logging.INFO)
    except Exception:
        pass


def engine_main(run_id: str, items: list, opts: dict, out_q, cancel_ev) -> None:
    """Child-process entry point (must stay a module-level function: the
    spawn start method pickles it by name)."""
    _child_logging(Path(opts.get("log_dir") or (config.BASE_DIR / "logs")))
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    def send(*msg) -> None:
        try:
            out_q.put(msg)
        except Exception:
            logger.debug("engine could not send %r", msg[:1], exc_info=True)

    from price_verifier.fetcher import browser_fallback
    from price_verifier.pipeline.runner import run_pipeline

    async def main():
        cancel = asyncio.Event()
        parent = multiprocessing.parent_process()

        async def watch():
            while True:
                send("hb", time.time(), browser_fallback.live_chrome_pids())
                if cancel_ev.is_set():
                    cancel.set()
                if parent is not None and not parent.is_alive():
                    logger.warning("engine: the app closed — stopping the run (rows so far are saved)")
                    cancel.set()
                await asyncio.sleep(HEARTBEAT_SECONDS)

        async def on_item_done(outcome):
            send("item", asdict(outcome))

        async def on_phase(phase, detail):
            send("phase", phase, dict(detail or {}))

        watcher = asyncio.ensure_future(watch())
        try:
            kwargs = {}
            if opts.get("db_path"):
                kwargs["db_path"] = Path(opts["db_path"])
            stats = await run_pipeline(
                run_id, items,
                concurrency=opts["concurrency"],
                tolerance_abs=opts["tolerance_abs"],
                tolerance_pct=opts["tolerance_pct"],
                use_browser_fallback=opts["use_browser"],
                on_item_done=on_item_done,
                on_phase=on_phase,
                cancel_event=cancel,
                **kwargs,
            )
            send("done", stats.as_dict(), bool(stats.cancelled))
        finally:
            watcher.cancel()

    try:
        logger.info("engine %s: run %s, %d rows", os.getpid(), run_id, len(items))
        asyncio.run(main())
    except BaseException:
        logger.exception("engine: run %s raised", run_id)
        send("error", traceback.format_exc())
    finally:
        try:
            out_q.close()
            out_q.join_thread()  # flush every message before the process exits
        except Exception:
            pass


# ── Supervisor (in the app) ──────────────────────────────────────────────────
@dataclass
class EngineResult:
    status: str                      # "completed" | "cancelled" | "crashed"
    stats: Optional[dict] = None     # the last engine's RunStats.as_dict()
    restarts: int = 0
    failures: list = field(default_factory=list)   # why each engine ended early


def _exit_label(code: Optional[int]) -> str:
    if code is None:
        return "still running"
    if code < 0:
        return f"killed by signal {-code}"
    if code > 0xFFFF:
        return f"exit code 0x{code & 0xFFFFFFFF:08X}" + (" (access violation)" if code == 0xC0000005 else "")
    return f"exit code {code}"


def kill_pids(pids) -> None:
    """Best-effort kill of Chrome/ChromeDriver processes a dead engine left
    behind (only ones whose program name says so, in case a PID was
    reused). Never raises."""
    for pid in {int(p) for p in pids or [] if p}:
        try:
            if sys.platform == "win32":
                listing = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).stdout.lower()
                if "chrome" not in listing:
                    continue
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                try:
                    with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                        if "chrom" not in f.read().lower():
                            continue
                except OSError:
                    continue
                os.kill(pid, 9)
        except Exception:
            pass


class EngineSupervisor:
    """Runs one invocation (start / resume / retry) of a run in engine
    processes, restarting a dead or hung engine on the rows still open.
    Call run() from a background thread; cancel() from anywhere."""

    def __init__(
        self,
        run_id: str,
        items: list,
        opts: dict,
        *,
        on_item: Callable[[dict], None],
        on_phase: Callable[[str, dict], None],
        max_restarts: int = 3,
        heartbeat_timeout: float = 240.0,
        cancel_grace: float = 90.0,
        restart_pause: float = 3.0,
        target: Callable = engine_main,
    ):
        self.run_id = run_id
        self.items = list(items)
        self.opts = dict(opts)
        self.on_item = on_item
        self.on_phase = on_phase
        self.max_restarts = max_restarts
        self.heartbeat_timeout = heartbeat_timeout
        self.cancel_grace = cancel_grace
        self.restart_pause = restart_pause
        self.target = target
        self._ctx = multiprocessing.get_context("spawn")
        self._cancel_ev = self._ctx.Event()
        self._cancel_requested = threading.Event()
        self.finalized: set[str] = set()
        self._chrome_pids: list[int] = []

    def cancel(self) -> None:
        self._cancel_requested.set()
        self._cancel_ev.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_requested.is_set()

    def run(self) -> EngineResult:
        result = EngineResult(status="crashed")
        remaining = list(self.items)
        while True:
            kind, info = self._run_one(remaining)
            if kind == "done":
                result.stats, cancelled = info
                result.status = "cancelled" if (cancelled or self.cancelled) else "completed"
                return result
            remaining = [it for it in self.items if it.asin not in self.finalized]
            if self.cancelled:
                result.status = "cancelled"
                return result
            result.failures.append(info)
            logger.error("run %s: engine stopped early (%s); %d rows still open", self.run_id, info,
                         len(remaining))
            if not remaining:
                result.status = "completed"   # it died after finalizing every row
                return result
            if result.restarts >= self.max_restarts:
                result.status = "crashed"
                return result
            result.restarts += 1
            self.on_phase("restart", {"event": "restart", "attempt": result.restarts, "reason": info,
                                      "items": len(remaining)})
            if self._cancel_requested.wait(self.restart_pause):
                result.status = "cancelled"
                return result

    def _run_one(self, items: list) -> tuple[str, object]:
        """One engine process. Returns ("done", (stats, cancelled)) or
        ("failed", reason)."""
        out_q = self._ctx.Queue()
        proc = self._ctx.Process(target=self.target, args=(self.run_id, items, self.opts, out_q, self._cancel_ev),
                                 name=f"pv-engine-{self.run_id}", daemon=True)
        proc.start()
        logger.info("run %s: engine process %s started on %d rows", self.run_id, proc.pid, len(items))
        last_beat = time.monotonic()
        cancel_seen_at: Optional[float] = None
        outcome: Optional[tuple[str, object]] = None
        try:
            while outcome is None:
                try:
                    msg = out_q.get(timeout=0.5)
                except queue.Empty:
                    msg = None
                except (EOFError, OSError):
                    msg = None
                if msg is not None:
                    last_beat = time.monotonic()
                    outcome = self._handle(msg)
                    continue
                now = time.monotonic()
                if self.cancelled and cancel_seen_at is None:
                    cancel_seen_at = now
                if not proc.is_alive():
                    # Messages sent just before it exited may still be in the pipe.
                    outcome = self._drain(out_q) or ("failed", f"engine process ended ({_exit_label(proc.exitcode)})")
                elif now - last_beat > self.heartbeat_timeout:
                    outcome = ("failed", f"engine stopped responding for {int(now - last_beat)} s")
                elif cancel_seen_at is not None and now - cancel_seen_at > self.cancel_grace:
                    outcome = ("failed", "engine did not stop after Pause")
            return outcome
        finally:
            self._reap(proc, finished=outcome is not None and outcome[0] == "done")
            try:
                out_q.close()
            except Exception:
                pass

    def _drain(self, out_q) -> Optional[tuple[str, object]]:
        outcome = None
        while True:
            try:
                msg = out_q.get(timeout=0.2)
            except Exception:
                return outcome
            outcome = self._handle(msg) or outcome

    def _handle(self, msg) -> Optional[tuple[str, object]]:
        kind = msg[0]
        if kind == "hb":
            self._chrome_pids = list(msg[2] or [])
        elif kind == "item":
            outcome = msg[1]
            self.finalized.add(outcome["asin"])
            self._safe(self.on_item, outcome)
        elif kind == "phase":
            self._safe(self.on_phase, msg[1], msg[2])
        elif kind == "done":
            return "done", (msg[1], msg[2])
        elif kind == "error":
            return "failed", "engine error: " + (msg[1].strip().splitlines() or ["?"])[-1]
        return None

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("engine callback raised (ignored)")

    def _reap(self, proc, finished: bool = False) -> None:
        # A finished engine still quits Chrome and cleans up on its way out;
        # give it time before stopping it (its results are already in).
        proc.join(timeout=30 if finished else 5)
        if proc.is_alive():
            if finished:
                logger.info("run %s: engine %s finished but was slow to exit; stopping it", self.run_id, proc.pid)
            else:
                logger.warning("run %s: killing engine process %s", self.run_id, proc.pid)
            proc.kill()
            proc.join(timeout=10)
        if proc.exitcode not in (0, None) and not finished:
            logger.warning("run %s: engine process %s %s", self.run_id, proc.pid, _exit_label(proc.exitcode))
            # Its Chrome (if any) has no one left to quit it.
            kill_pids(self._chrome_pids)
        self._chrome_pids = []
