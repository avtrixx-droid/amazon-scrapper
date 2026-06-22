"""
gui.py — Amazon Scraper Web UI
Opens automatically in your browser. No Tkinter required.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import sys
import threading
import time
import warnings
import webbrowser

warnings.filterwarnings("ignore", category=Warning, module="urllib3")
from datetime import datetime
from pathlib import Path

def _get_base_dir() -> Path:
    """Return user-writable base dir: handles PyInstaller frozen .exe and .app."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # Inside macOS .app bundle — use ~/Library/Application Support to stay writable
        if sys.platform == "darwin" and ".app/Contents/MacOS" in str(exe):
            d = Path.home() / "Library" / "Application Support" / "AmazonScraper"
            d.mkdir(parents=True, exist_ok=True)
            return d
        # Windows or macOS non-.app: directory that contains the exe
        return exe.parent
    return Path(__file__).resolve().parent

BASE_DIR = _get_base_dir()
sys.path.insert(0, str(BASE_DIR))

from flask import Flask, Response, jsonify, redirect, render_template_string, request
import scraper as sc
import license as lic

app = Flask(__name__)
app.logger.setLevel(logging.ERROR)
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# ── Startup crash log (always on; written before Flask starts) ─────────────────
def _init_startup_log() -> None:
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_dir / "startup.log"), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("startup").info(f"BASE_DIR={BASE_DIR}  frozen={getattr(sys,'frozen',False)}")
    except Exception:
        pass  # Never crash on logging setup

_init_startup_log()

# ── Hard-coded run settings (vendor used to control these; no longer exposed) ──
MIN_DELAY = 3.0
MAX_DELAY = 8.0
HEADLESS = True
MAX_AUTO_RETRY_COMBOS = 500  # Skip Tier A retry if more than this many failures

# ── Global run state ──────────────────────────────────────────────────────────

_st = {
    "running": False,
    "processes": [],
    "msg_queue": None,
    "poll_thread": None,
    "done": 0,
    "total": 0,
    "success": 0,
    "failed": 0,
    "workers_done": 0,
    "num_workers": 0,
    "worker_results": {},
    "worker_failed": [],
    "asin_entries": None,
    "pincodes": None,
    "start_time": None,
    "log": [],            # append-only; SSE clients use an index cursor
    "worker_status": {},  # worker_id → {"msg":…, "status":…}
    "status_text": "Ready",
    "xlsx_path": None,
    "retrying": False,
}


# ── Worker entry (module-level so multiprocessing can pickle it) ──────────────

def _worker_entry(worker_id, pincodes, asin_entries, settings, base_dir_str, q):
    sc.run_worker(worker_id, pincodes, asin_entries, settings, base_dir_str, q)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunk(items, n):
    k, m = divmod(len(items), n)
    out, start = [], 0
    for i in range(n):
        end = start + k + (1 if i < m else 0)
        if start < end:
            out.append(items[start:end])
        start = end
    return out


def _log(msg, kind="log"):
    ts = datetime.now().strftime("%H:%M:%S")
    _st["log"].append({"ts": ts, "msg": msg, "kind": kind})


def _fmt_elapsed(start):
    if not start:
        return ""
    s = int((datetime.now() - start).total_seconds())
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _default_settings():
    return {
        "MIN_DELAY": MIN_DELAY, "MAX_DELAY": MAX_DELAY, "MAX_RETRIES": 2,
        "HEADLESS": HEADLESS,
        "SEND_EMAIL": False, "EMAIL_FROM": "", "EMAIL_PASSWORD": "",
        "EMAIL_TO": "", "EMAIL_SUBJECT": "",
        "OUTPUT_FOLDER": "Downloads",
        "OUTPUT_FILENAME": "Amazon_Report_{date}.xlsx",
    }


# ── License gate (cached 60s so every page load doesn't ping the server) ─────
_license_status: dict | None = None
_license_status_ts: float = 0.0
_LICENSE_CACHE_SECONDS = 60.0


def _get_license_status(force: bool = False) -> dict:
    global _license_status, _license_status_ts
    now = time.time()
    if force or _license_status is None or (now - _license_status_ts) > _LICENSE_CACHE_SECONDS:
        try:
            _license_status = lic.check_license_status()
        except Exception as exc:
            # Never let a license bug brick the GUI — fall back to needs_activation.
            logging.getLogger("license").error("check_license_status failed: %s", exc)
            _license_status = {"status": "needs_activation", "reason": "internal_error"}
        _license_status_ts = now
    return _license_status


# ── Background poll thread (reads multiprocessing.Queue → updates _st) ────────

def _poll():
    while _st["workers_done"] < _st["num_workers"]:
        if not _st["running"]:
            return  # user hit Stop
        try:
            msg = _st["msg_queue"].get(timeout=0.15)
            _handle(msg)
        except Exception:
            pass

    if _st["status_text"] in ("Stopped", "Error"):
        return

    # Tier A — automatic single retry pass over the failed combinations.
    failed_snapshot = list(_st["worker_failed"])
    if failed_snapshot and len(failed_snapshot) <= MAX_AUTO_RETRY_COMBOS:
        _st["status_text"] = "Retrying failures…"
        _log(
            f"↻ Auto-retrying {len(failed_snapshot)} failed combination(s)…",
            "info",
        )
        try:
            _retry_failures(failed_snapshot)
        except Exception as exc:  # never block report build on a retry hiccup
            _log(f"⚠️  Auto-retry pass failed: {exc}", "warn")
    elif failed_snapshot:
        _log(
            f"⚠️  Skipping auto-retry — {len(failed_snapshot)} failures exceed "
            f"the {MAX_AUTO_RETRY_COMBOS} threshold. Use the manual retry button.",
            "warn",
        )

    _st["status_text"] = "Building Excel…"
    _log("All workers finished — building Excel report…", "info")
    threading.Thread(target=_build_excel, daemon=True).start()


def _handle(msg):
    t = msg.get("type")
    w = msg.get("worker", 0)

    if t == "progress":
        _st["done"] = min(_st["done"] + 1, _st["total"])
        s = msg.get("status", "")
        if s == "OK":
            _st["success"] += 1
        elif s in ("FAILED", "PINCODE_FAILED"):
            _st["failed"] += 1
        _st["worker_status"][w] = {"msg": msg.get("msg", ""), "status": s}
        _log(msg.get("msg", ""), "progress")

    elif t == "log":
        _log(msg.get("msg", ""), "info")

    elif t == "done":
        _st["workers_done"] += 1
        for asin, pc_dict in msg.get("results", {}).items():
            _st["worker_results"].setdefault(asin, {}).update(pc_dict)
        _st["worker_failed"].extend(msg.get("failed_rows", []))
        _log(f"✔  Worker {w} finished.", "info")
        _st["worker_status"][w] = {"msg": "finished ✔", "status": "DONE"}

    elif t == "error":
        _st["workers_done"] += 1
        _log(f"❌ Worker {w} error: {msg.get('msg', '')}", "error")
        _st["worker_status"][w] = {"msg": "error", "status": "ERROR"}


# ── Retry helpers (Tier A and Tier B) ─────────────────────────────────────────

def _dedup_failed(failed_rows):
    """Return a deduped list of (asin, pincode, city) tuples from worker_failed rows.

    Each ``worker_failed`` entry is a 5-element list
    ``[asin, pincode, city, reason, timestamp]`` (see scraper.run_worker)."""
    seen = set()
    combos = []
    for row in failed_rows:
        if not row or len(row) < 3:
            continue
        asin, pincode, city = row[0], row[1], row[2] if len(row) >= 3 else ""
        key = (asin, pincode)
        if key in seen:
            continue
        seen.add(key)
        combos.append((asin, pincode, city))
    return combos


def _asin_entries_for(asins):
    """Subset _st['asin_entries'] to the given ASIN list, preserving metadata."""
    if not _st["asin_entries"]:
        return []
    by_asin = {e.asin: e for e in _st["asin_entries"]}
    out = []
    for asin in asins:
        if asin in by_asin:
            out.append(by_asin[asin])
        else:
            # Fallback — minimal entry so the scraper can still run.
            out.append(sc.ASINEntry(asin=asin))
    return out


def _run_retry_workers(combos, label):
    """Shared plumbing for Tier A and Tier B retries.

    Spawns workers over the given (asin, pincode, city) combos, polls the queue
    in-line until they finish, then merges OK results into ``_st['worker_results']``
    and replaces ``_st['worker_failed']`` with the combos that still failed."""
    if not combos:
        return 0, 0

    # Group by pincode → asins for that pincode.
    by_pincode = {}
    city_for = {}
    for asin, pincode, city in combos:
        by_pincode.setdefault(pincode, []).append(asin)
        if pincode not in city_for or not city_for[pincode]:
            city_for[pincode] = city

    pincodes = {pc: (city_for.get(pc) or "") for pc in by_pincode}
    all_asins = sorted({a for asins in by_pincode.values() for a in asins})
    asin_entries = _asin_entries_for(all_asins)

    # 1–2 workers — small retry workloads don't need more.
    num_workers = min(2, len(pincodes)) or 1
    pc_items = list(pincodes.items())
    chunks = _chunk(pc_items, num_workers)

    retry_queue = multiprocessing.Queue()
    settings = _default_settings()
    processes = []

    _log(f"↻ {label}: spawning {num_workers} retry worker(s) for "
         f"{len(combos)} combinations across {len(pincodes)} pincode(s).", "info")

    for i, chunk in enumerate(chunks):
        pc_dict = dict(chunk)
        # Subset ASINs to only those that failed for this pincode.
        wanted = {a for pc in pc_dict for a in by_pincode.get(pc, [])}
        entries_subset = [e for e in asin_entries if e.asin in wanted]
        if not entries_subset:
            continue
        p = multiprocessing.Process(
            target=_worker_entry,
            args=(900 + i + 1, pc_dict, entries_subset, settings,
                  str(BASE_DIR), retry_queue),
            daemon=True,
        )
        p.start()
        processes.append(p)
        _log(f"  Retry W{900 + i + 1} → pincodes: "
             f"{', '.join(pc_dict.values()) or '(none)'}", "info")

    # Poll inline — Tier A runs from within _poll, Tier B from its own thread.
    new_results = {}     # asin → {pincode: serialized result dict}
    new_failed = []
    workers_done = 0
    target = len(processes)

    if target == 0:
        return 0, 0

    while workers_done < target:
        if not _st["running"]:
            break
        try:
            msg = retry_queue.get(timeout=0.2)
        except Exception:
            continue
        mtype = msg.get("type")
        w = msg.get("worker", 0)
        if mtype == "progress":
            s = msg.get("status", "")
            _st["worker_status"][w] = {"msg": msg.get("msg", ""), "status": s}
            _log(msg.get("msg", ""), "progress")
        elif mtype == "log":
            _log(msg.get("msg", ""), "info")
        elif mtype == "done":
            workers_done += 1
            for asin, pc_dict in msg.get("results", {}).items():
                new_results.setdefault(asin, {}).update(pc_dict)
            new_failed.extend(msg.get("failed_rows", []))
            _log(f"✔  Retry worker {w} finished.", "info")
            _st["worker_status"][w] = {"msg": "retry finished ✔", "status": "DONE"}
        elif mtype == "error":
            workers_done += 1
            _log(f"❌ Retry worker {w} error: {msg.get('msg', '')}", "error")
            _st["worker_status"][w] = {"msg": "retry error", "status": "ERROR"}

    # Merge OK results into the global cache; track which (asin, pincode) succeeded.
    recovered = set()
    for asin, pc_dict in new_results.items():
        for pincode, data in pc_dict.items():
            status = (data.get("status") or "").upper()
            if status == "OK":
                _st["worker_results"].setdefault(asin, {})[pincode] = data
                recovered.add((asin, pincode))
            else:
                # Overwrite the previous failed entry with the latest attempt's data
                # so the Excel "Failed" sheet reflects the most recent failure_reason.
                _st["worker_results"].setdefault(asin, {})[pincode] = data

    # Rebuild worker_failed: drop combos we retried that recovered; replace the
    # remaining retry-attempt rows with the new_failed rows from this pass.
    retried_keys = {(a, pc) for (a, pc, _c) in combos}
    new_failed_keys = {(r[0], r[1]) for r in new_failed if r and len(r) >= 2}

    remaining = []
    for row in _st["worker_failed"]:
        if not row or len(row) < 2:
            continue
        key = (row[0], row[1])
        if key in retried_keys:
            # We just retried this combo. Drop the old row; if it failed again,
            # the new row from new_failed will be appended below.
            continue
        remaining.append(row)
    # Append new failures from this retry pass.
    for row in new_failed:
        remaining.append(row)

    _st["worker_failed"] = remaining

    # Adjust counters: every recovered combo flips one failure → one success.
    recovered_n = sum(1 for k in retried_keys if k not in new_failed_keys)
    if recovered_n:
        _st["success"] += recovered_n
        _st["failed"] = max(0, _st["failed"] - recovered_n)

    _log(
        f"↻ {label} complete — recovered {recovered_n}/{len(combos)}; "
        f"{len(new_failed_keys)} still failing.",
        "success" if recovered_n else "warn",
    )

    return recovered_n, len(new_failed_keys)


def _retry_failures(failed_rows):
    """Tier A — automatic single retry pass before Excel is built."""
    combos = _dedup_failed(failed_rows)
    if not combos:
        return
    _run_retry_workers(combos, "Auto-retry")


def _retry_thread():
    """Tier B — manual retry triggered by the user via POST /retry."""
    try:
        combos = _dedup_failed(_st["worker_failed"])
        if not combos:
            _st["running"] = False
            _st["retrying"] = False
            _st["status_text"] = "Complete!"
            return

        _st["done"] = 0
        _st["total"] = len(combos)
        _st["workers_done"] = 0
        _st["worker_status"] = {}
        _st["status_text"] = "Retrying failures…"
        _st["start_time"] = datetime.now()

        _run_retry_workers(combos, "Manual retry")

        _st["status_text"] = "Building Excel…"
        _log("Manual retry finished — rebuilding Excel report…", "info")
        _build_excel()
    except Exception as exc:
        _st["status_text"] = "Error"
        _log(f"❌ Manual retry failed: {exc}", "error")
    finally:
        _st["retrying"] = False
        _st["running"] = False


# ── Excel builder (runs in background thread after all workers finish) ─────────

def _build_excel():
    try:
        asin_entries = _st["asin_entries"] or []
        pincodes = _st["pincodes"] or {}

        results_cache = {}
        for asin, pc_dict in _st["worker_results"].items():
            results_cache[asin] = {}
            for pc, d in pc_dict.items():
                results_cache[asin][pc] = sc.ScrapeResult(
                    asin=d.get("asin", asin),
                    product_name=d.get("product_name", ""),
                    mrp=d.get("mrp"),
                    price=d.get("price"),
                    discount_percent=d.get("discount_percent", "N/A"),
                    pincode=d.get("pincode", pc),
                    city=d.get("city", ""),
                    in_stock=d.get("in_stock", ""),
                    delivery_date=d.get("delivery_date", ""),
                    free_delivery=d.get("free_delivery", ""),
                    seller=d.get("seller", ""),
                    rating=d.get("rating", ""),
                    reviews=d.get("reviews", ""),
                    bsr=d.get("bsr", "N/A"),
                    product_url=d.get("product_url", ""),
                    scraped_at=d.get("scraped_at", ""),
                    status=d.get("status", "FAILED"),
                    failure_reason=d.get("failure_reason", ""),
                )

        # Manual retry should overwrite the previous xlsx path; the initial run
        # picks a fresh timestamped one.
        if _st.get("xlsx_path"):
            xlsx_path = Path(_st["xlsx_path"])
        else:
            xlsx_path = sc.resolve_output_path({
                "OUTPUT_FOLDER": "Downloads",
                "OUTPUT_FILENAME": "Amazon_Report_{date}.xlsx",
            })
        started = _st["start_time"] or datetime.now()
        finished = datetime.now()

        price_vals = [
            r.price for pd2 in results_cache.values()
            for r in pd2.values() if r.price is not None]
        rating_vals = []
        for pd2 in results_cache.values():
            for r in pd2.values():
                try:
                    if r.rating not in ("Not Found", "", None):
                        rating_vals.append(float(r.rating))
                except Exception:
                    pass

        totals = {
            "total_asins": len(asin_entries),
            "total_combos": _st["total"],
            "pincodes_checked": len(pincodes),
            "success": _st["success"],
            "failed": _st["failed"],
            "out_of_stock": sum(
                1 for pd2 in results_cache.values()
                for r in pd2.values()
                if (r.in_stock or "").lower().startswith("out of stock")),
            "price_sum": sum(price_vals),
            "price_count": len(price_vals),
            "rating_sum": sum(rating_vals),
            "rating_count": len(rating_vals),
        }

        wb = sc.build_pivoted_excel(
            results_cache, asin_entries, pincodes,
            xlsx_path, _st["worker_failed"],
            logging.getLogger("gui_excel"))
        sc.autofit_columns(wb["Results"], len(sc.FIXED_HEADERS) + len(pincodes))
        sc.autofit_columns(wb["Failed"], 5)
        sc.write_summary_sheet(wb, totals, started, finished)
        wb.save(xlsx_path)

        _st["xlsx_path"] = str(xlsx_path)
        _st["status_text"] = "Complete!"
        _st["running"] = False
        _log(f"✅ Report saved to: {xlsx_path}", "success")
        sc.open_file_cross_platform(xlsx_path)

    except Exception as e:
        _st["status_text"] = "Error"
        _st["running"] = False
        _log(f"❌ Excel build failed: {e}", "error")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    status = _get_license_status()
    s = status.get("status")
    if s in ("valid", "grace"):
        return render_template_string(HTML, license_status=status)
    # All other statuses (needs_activation, expired, revoked) route to /activate;
    # the activation page renders the appropriate message for each.
    return redirect("/activate")


@app.route("/activate", methods=["GET", "POST"])
def activate_route():
    """GET → show the activation page. POST {key} → attempt activation."""
    if request.method == "GET":
        status = _get_license_status()
        return render_template_string(ACTIVATE_HTML, license_status=status)

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip().upper()
    if not key:
        return jsonify({"ok": False, "error": "Please enter a license key."})

    ok, err = lic.activate(key)
    if ok:
        # Force a re-check on the next request so the cached "needs_activation"
        # doesn't keep redirecting the user back here.
        _get_license_status(force=True)
        status = _get_license_status(force=True)
        return jsonify({
            "ok": True,
            "expires_at": status.get("expires_at", ""),
            "customer": status.get("customer", ""),
        })
    return jsonify({"ok": False, "error": err})


@app.route("/license-status")
def license_status_route():
    return jsonify(_get_license_status())


@app.route("/start", methods=["POST"])
def start():
    if _st["running"]:
        return jsonify({"ok": False, "error": "Already running"})

    data = request.get_json()
    try:
        mode = data.get("mode", "manual")
        asin_raw = data.get("asins" if mode == "manual" else "asin_content", "").strip()
        pin_raw = data.get("pincodes" if mode == "manual" else "pin_content", "").strip()

        if not asin_raw:
            return jsonify({"ok": False, "error": "No ASINs provided."})
        if not pin_raw:
            return jsonify({"ok": False, "error": "No pincodes provided."})

        asin_entries = sc.parse_asins_from_text(asin_raw)
        pincodes = sc.parse_pincodes_from_text(pin_raw)

        if not asin_entries:
            return jsonify({"ok": False, "error": "No valid ASINs found. Each must be 10 chars starting with B."})
        if not pincodes:
            return jsonify({"ok": False, "error": "No valid pincodes found. Format: 110001,Delhi"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    # Server-side run authorization — the hard gate.
    authorized, auth_error, run_token, run_expires, reason = lic.authorize_run(
        asin_count=len(asin_entries), pincode_count=len(pincodes),
    )
    if not authorized:
        return jsonify({
            "ok": False,
            "error": auth_error or "license_invalid",
            "relicense": reason in lic.RELICENSE_REASONS,
            "reason": reason,
        })

    # Run settings are hard-coded — vendor no longer controls them.
    num_workers = min(4, len(pincodes))
    settings = _default_settings()

    _st.update({
        "running": True, "processes": [],
        "done": 0, "total": len(asin_entries) * len(pincodes),
        "success": 0, "failed": 0,
        "workers_done": 0, "num_workers": num_workers,
        "worker_results": {}, "worker_failed": [],
        "asin_entries": asin_entries, "pincodes": pincodes,
        "start_time": datetime.now(), "log": [],
        "worker_status": {}, "status_text": "Running…",
        "xlsx_path": None,
        "retrying": False,
        "run_token": run_token,
        "run_expires": run_expires,
    })

    _log(
        f"Starting {num_workers} worker(s)  |  "
        f"{len(asin_entries)} ASINs × {len(pincodes)} pincodes "
        f"= {_st['total']} combinations", "info")

    _st["msg_queue"] = multiprocessing.Queue()
    pc_chunks = _chunk(list(pincodes.items()), num_workers)

    for i, chunk in enumerate(pc_chunks):
        pc_dict = dict(chunk)
        p = multiprocessing.Process(
            target=_worker_entry,
            args=(i + 1, pc_dict, asin_entries, settings,
                  str(BASE_DIR), _st["msg_queue"]),
            daemon=True,
        )
        p.start()
        _st["processes"].append(p)
        _log(f"  Worker {i+1} → pincodes: {', '.join(pc_dict.values())}", "info")

    t = threading.Thread(target=_poll, daemon=True)
    _st["poll_thread"] = t
    t.start()

    return jsonify({"ok": True, "total": _st["total"], "workers": num_workers})


@app.route("/retry", methods=["POST"])
def retry():
    """Tier B — manual retry over the current worker_failed list."""
    if _st["running"] or _st["retrying"]:
        return jsonify({"ok": False, "error": "A run is already in progress."})

    combos = _dedup_failed(_st["worker_failed"])
    if not combos:
        return jsonify({"ok": False, "error": "Nothing to retry — no failed combinations."})

    authorized, auth_error, _, _, reason = lic.authorize_run(
        asin_count=len(combos), pincode_count=1,
    )
    if not authorized:
        return jsonify({
            "ok": False,
            "error": auth_error or "license_invalid",
            "relicense": reason in lic.RELICENSE_REASONS,
            "reason": reason,
        })

    _st["retrying"] = True
    _st["running"] = True
    threading.Thread(target=_retry_thread, daemon=True).start()
    return jsonify({"ok": True, "retrying": True, "count": len(combos)})


@app.route("/stop", methods=["POST"])
def stop():
    _st["running"] = False
    _st["status_text"] = "Stopped"
    for p in _st["processes"]:
        try:
            p.terminate()
        except Exception:
            pass
    _st["processes"].clear()
    _log("⚠️  Scraping stopped by user.", "warn")
    return jsonify({"ok": True})


@app.route("/status")
def status():
    pct = int(_st["done"] / _st["total"] * 100) if _st["total"] else 0
    # Cap failed_list at 50 to keep the payload small.
    failed_list = [
        {
            "asin": row[0] if len(row) > 0 else "",
            "pincode": row[1] if len(row) > 1 else "",
            "city": row[2] if len(row) > 2 else "",
            "reason": row[3] if len(row) > 3 else "",
        }
        for row in (_st["worker_failed"] or [])[:50]
    ]
    return jsonify({
        "running": _st["running"],
        "retrying": _st.get("retrying", False),
        "done": _st["done"], "total": _st["total"], "pct": pct,
        "success": _st["success"], "failed": _st["failed"],
        "status": _st["status_text"],
        "elapsed": _fmt_elapsed(_st["start_time"]) if _st["start_time"] else "",
        "workers": {str(k): v for k, v in _st["worker_status"].items()},
        "xlsx": _st["xlsx_path"],
        "failed_list": failed_list,
        "failed_total": len(_st["worker_failed"] or []),
    })


@app.route("/stream")
def stream():
    """Server-Sent Events endpoint — streams log entries to the browser."""
    start_idx = int(request.args.get("from", 0))

    def generate():
        idx = start_idx
        while True:
            batch = _st["log"][idx:]
            for entry in batch:
                yield f"data: {json.dumps(entry)}\n\n"
            idx += len(batch)
            finished = (
                _st["status_text"] in ("Complete!", "Stopped", "Error")
            )
            if finished and not batch:
                yield f"data: {json.dumps({'kind': 'eof'})}\n\n"
                break
            time.sleep(0.15)

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── HTML (JPMorgan Chase-themed enterprise UI; custom design tokens) ──────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Amazon Scraper</title>
<link rel="icon" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect x='2' y='2' width='12' height='12' fill='%230066B2'/><rect x='18' y='2' width='12' height='12' fill='%230066B2'/><rect x='2' y='18' width='12' height='12' fill='%230066B2'/><rect x='18' y='18' width='12' height='12' fill='%230066B2'/><rect x='12' y='12' width='8' height='8' fill='%230066B2'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#f7f7f5;
  --surface:#ffffff;
  --surface-2:#f0efec;
  --border:rgba(0, 0, 0, 0.08);
  --border-strong:rgba(0, 0, 0, 0.16);
  --text:#181818;
  --text-secondary:#555555;
  --text-tertiary:#8a8a8a;
  --accent:#0066B2;
  --accent-hover:#004F8C;
  --accent-dark:#003594;
  --accent-soft:#E6F0F8;
  --success:#008542;
  --warning:#FF8200;
  --danger:#D32F2F;
  --radius:4px;
  --radius-lg:8px;
  --shadow-none:0 0 0 transparent;
  --ease:cubic-bezier(0.4, 0, 0.2, 1);
  /* Live-log terminal palette (mono surface) */
  --log-bg:#0a0a0b;
  --log-fg:#d4d4d8;
  --log-thumb:#3f3f46;
  --log-info:#9ec5fe;
  --log-success:#7ee2a8;
  --log-warn:#ffc680;
  --log-error:#f49a9a;
  --log-muted:#d4d4d8;
}
*{box-sizing:border-box}
html,body{background:var(--bg);color:var(--text);margin:0}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;letter-spacing:-0.011em;font-size:14.5px;line-height:1.55;min-height:100vh;display:flex;flex-direction:column}
h1,h2,h3,h4{font-weight:600;letter-spacing:-0.02em;margin:0;color:var(--text)}
.mono{font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace}

/* ── Header ─────────────────────────────────────────── */
.site-header{background:var(--surface);border-bottom:1px solid var(--border);height:64px;display:flex;align-items:center;padding:0 24px}
.site-header-inner{max-width:960px;width:100%;margin:0 auto;display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:10px}
.brand-mark{width:24px;height:24px;flex-shrink:0}
.brand-mark rect{fill:var(--accent)}
.wordmark{font-size:13px;font-weight:600;letter-spacing:0.06em;color:var(--text);text-transform:uppercase}
.lic-badge{display:inline-flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border-strong);padding:6px 12px;border-radius:999px;font-size:12.5px;color:var(--text);font-weight:500;font-variant-numeric:tabular-nums}
.lic-dot{width:8px;height:8px;border-radius:50%;background:var(--text-tertiary);flex-shrink:0;transition:background 180ms var(--ease)}
.lic-dot.valid{background:var(--success)}
.lic-dot.grace{background:var(--warning)}
.lic-dot.expired,.lic-dot.revoked{background:var(--danger)}
.lic-dot.required{background:var(--accent)}

.grace-banner{background:rgba(255,130,0,0.08);border-bottom:1px solid rgba(255,130,0,0.25);color:#7a3e00;font-size:13px}
.grace-inner{max-width:960px;margin:0 auto;padding:10px 24px;display:flex;align-items:center;gap:10px}
.grace-inner svg{color:var(--warning);flex-shrink:0}
.grace-x{margin-left:auto;border:none;background:transparent;color:#7a3e00;cursor:pointer;padding:4px;border-radius:var(--radius);display:inline-flex;align-items:center;justify-content:center}
.grace-x:hover{background:rgba(255,130,0,0.14)}

.status-pill{display:none}

/* ── Page / cards ──────────────────────────────────── */
.page{flex:1;max-width:960px;width:100%;margin:0 auto;padding:32px 24px 48px;display:flex;flex-direction:column;gap:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:32px;box-shadow:var(--shadow-none);opacity:0;transform:translateY(4px);animation:card-in 240ms var(--ease) forwards}
.card:nth-of-type(1){animation-delay:0ms}
.card:nth-of-type(2){animation-delay:40ms}
.card:nth-of-type(3){animation-delay:80ms}
@keyframes card-in{to{opacity:1;transform:translateY(0)}}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-tertiary);margin-bottom:8px}
.card-title{font-size:17px;font-weight:600;margin-bottom:6px;letter-spacing:-0.02em}
.card-sub{color:var(--text-secondary);font-size:13.5px;margin-bottom:24px;line-height:1.55}

/* ── Tab control (replaces sliding pill) ─────────────── */
.seg{display:flex;gap:0;margin-bottom:24px;border-bottom:1px solid var(--border)}
.seg button{position:relative;border:none;background:transparent;padding:10px 20px 12px;font-size:13.5px;font-weight:500;color:var(--text-secondary);cursor:pointer;font-family:inherit;transition:color 180ms var(--ease);margin-bottom:-1px}
.seg button:hover{color:var(--text)}
.seg button.active{color:var(--accent);font-weight:600}
.seg button.active::after{content:'';position:absolute;left:0;right:0;bottom:0;height:2px;background:var(--accent)}

/* ── Drop zones ────────────────────────────────────── */
.drop{border:1px dashed var(--border-strong);border-radius:var(--radius);padding:28px;text-align:center;color:var(--text-secondary);transition:all 180ms var(--ease);cursor:pointer;background:var(--surface);position:relative}
.drop:hover{border-color:var(--accent);background:var(--accent-soft)}
.drop.dragover{border-style:solid;border-width:2px;padding:27px;border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
.drop-icon{display:block;margin:0 auto 10px;color:var(--text-tertiary)}
.drop-title{font-size:14px;font-weight:600;color:var(--text)}
.drop-hint{font-size:12.5px;color:var(--text-tertiary);margin-top:4px}
.drop.loaded{border-style:solid;border-color:var(--success);background:rgba(0,133,66,0.04);text-align:left;cursor:default;padding:16px 20px}
.drop.loaded .loaded-row{display:flex;align-items:center;gap:12px}
.drop.loaded .loaded-icon{color:var(--success);flex-shrink:0}
.drop.loaded .loaded-name{font-size:14px;font-weight:500;color:var(--text)}
.drop.loaded .loaded-chip{margin-left:auto;font-size:12px;color:var(--text-secondary);background:var(--surface-2);padding:4px 10px;border-radius:var(--radius);font-weight:500;border:1px solid var(--border)}
.drop.loaded .clear-x{margin-left:8px;border:none;background:transparent;color:var(--text-tertiary);cursor:pointer;padding:4px;border-radius:var(--radius);transition:all 180ms var(--ease)}
.drop.loaded .clear-x:hover{color:var(--text);background:var(--surface-2)}

.upload-row + .upload-row{margin-top:16px}
.upload-label{font-size:12px;font-weight:600;color:var(--text-tertiary);margin-bottom:8px;text-transform:uppercase;letter-spacing:.06em}

/* ── Paste panel ───────────────────────────────────── */
.paste-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media (max-width:640px){.paste-grid{grid-template-columns:1fr}}
.paste-block label{display:block;font-size:12px;font-weight:600;color:var(--text-tertiary);margin-bottom:8px;text-transform:uppercase;letter-spacing:.06em}
textarea.ta{width:100%;min-height:200px;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius);padding:12px 14px;font-family:'JetBrains Mono','SF Mono',Menlo,monospace;font-size:13px;line-height:1.6;color:var(--text);resize:vertical;transition:border-color 180ms var(--ease)}
textarea.ta::placeholder{color:var(--text-tertiary)}
textarea.ta:focus{outline:none;border-color:var(--accent);border-width:2px;padding:11px 13px}
.count-chip{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--text-secondary);margin-top:8px;font-weight:500}
.count-chip .ok{color:var(--success)}
.count-chip .bad{color:var(--danger);cursor:help}

/* ── Buttons ───────────────────────────────────────── */
.btn-primary{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--accent);color:#fff;border:none;padding:12px 24px;border-radius:var(--radius);font-weight:600;font-size:14px;font-family:inherit;cursor:pointer;transition:background 180ms var(--ease);min-width:160px;letter-spacing:-0.005em}
.btn-primary:hover{background:var(--accent-hover)}
.btn-primary:active{background:var(--accent-dark)}
.btn-primary:disabled{background:var(--accent);opacity:.6;cursor:not-allowed}
.btn-secondary{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--surface);color:var(--accent);border:1px solid var(--accent);padding:11px 22px;border-radius:var(--radius);font-weight:600;font-size:14px;font-family:inherit;cursor:pointer;transition:background 180ms var(--ease)}
.btn-secondary:hover{background:var(--accent-soft)}
.btn-danger{background:var(--surface);color:var(--danger);border:1px solid var(--border-strong)}
.btn-danger:hover{background:rgba(211,47,47,0.05);border-color:var(--danger)}
@media (max-width:640px){.btn-primary,.btn-secondary{width:100%}}
button:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,0.45);border-top-color:#fff;border-radius:50%;animation:spin 700ms linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}

/* ── Progress card ─────────────────────────────────── */
.progress-card{visibility:hidden;opacity:0;transition:opacity 180ms var(--ease)}
.progress-card.show{visibility:visible;opacity:1}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:0;margin-bottom:28px;border:1px solid var(--border);border-radius:var(--radius)}
.stat{text-align:center;padding:20px 12px;border-right:1px solid var(--border)}
.stat:last-child{border-right:none}
.stat-num{font-size:28px;font-weight:600;letter-spacing:-0.02em;color:var(--text);font-variant-numeric:tabular-nums;line-height:1}
.stat-num.ok{color:var(--success)}
.stat-num.fail{color:var(--danger)}
.stat-label{font-size:11.5px;color:var(--text-tertiary);margin-top:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em}
.progress-meta{display:flex;justify-content:space-between;font-size:12.5px;color:var(--text-secondary);margin-bottom:8px;font-variant-numeric:tabular-nums}
.bar-track{height:6px;background:var(--surface-2);border-radius:var(--radius);overflow:hidden;position:relative;border:1px solid var(--border)}
.bar-fill{height:100%;width:0%;background:var(--accent);transition:width 300ms var(--ease),background 180ms var(--ease)}
.bar-fill.complete{background:var(--success)}

/* ── Worker cards (replaces pills) ─────────────────── */
.pills{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-top:20px}
.pill{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--border);padding:10px 12px;border-radius:var(--radius);font-size:12.5px;color:var(--text);transition:border-color 180ms var(--ease)}
.pill-label{font-weight:600;color:var(--text);font-size:12px;letter-spacing:0.04em}
.pill-msg{color:var(--text-secondary);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--text-tertiary);flex-shrink:0}
.pill.active{border-color:var(--accent)}
.pill.active .dot{background:var(--accent)}
.pill.ok{border-color:var(--success)}
.pill.ok .dot{background:var(--success)}
.pill.fail{border-color:var(--danger)}
.pill.fail .dot{background:var(--danger)}
.pill.warn{border-color:var(--warning)}
.pill.warn .dot{background:var(--warning)}

.log-header{display:flex;align-items:center;justify-content:space-between;margin-top:28px;margin-bottom:10px}
.log-header h4{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-tertiary);font-weight:600}
.log-clear{border:none;background:transparent;color:var(--text-tertiary);font-size:12px;cursor:pointer;padding:4px 8px;border-radius:var(--radius);font-family:inherit;transition:all 180ms var(--ease)}
.log-clear:hover{color:var(--text);background:var(--surface-2)}
#log-box{background:var(--log-bg);color:var(--log-fg);border-radius:var(--radius);padding:14px 16px;height:260px;overflow-y:auto;font-family:'JetBrains Mono','SF Mono',Menlo,monospace;font-size:11.5px;line-height:1.6;border:1px solid var(--border)}
#log-box::-webkit-scrollbar{width:4px}
#log-box::-webkit-scrollbar-track{background:var(--log-bg)}
#log-box::-webkit-scrollbar-thumb{background:var(--log-thumb);border-radius:2px}
#log-box div + div{margin-top:1px}

/* ── Summary ───────────────────────────────────────── */
.summary{display:none;text-align:center;padding:12px 0 24px;animation:summary-in 180ms var(--ease)}
.summary.show{display:block}
@keyframes summary-in{from{opacity:0}to{opacity:1}}
.summary-icon{width:40px;height:40px;border-radius:var(--radius);background:var(--accent-soft);color:var(--accent);display:inline-flex;align-items:center;justify-content:center;margin-bottom:16px}
.summary-title{font-size:18px;font-weight:600;color:var(--text);margin-bottom:6px;letter-spacing:-0.02em}
.summary-sub{font-size:13.5px;color:var(--text-secondary);margin-bottom:20px}

/* ── Retries card ──────────────────────────────────── */
.retries{display:none;animation:card-in 240ms var(--ease)}
.retries.show{display:block}
.retries-header{display:flex;align-items:center;gap:10px;color:var(--warning);font-weight:600;font-size:15px;margin-bottom:16px}
.retries-header svg{color:var(--warning);flex-shrink:0}
.retries-table{width:100%;border-collapse:collapse;margin-bottom:16px;font-size:12.5px}
.retries-table th{text-align:left;color:var(--text-tertiary);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:8px 10px;border-bottom:1px solid var(--border)}
.retries-table td{padding:10px;border-bottom:1px solid var(--border);color:var(--text);vertical-align:top}
.retries-table td.reason{color:var(--text-secondary);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.retries-table tr:last-child td{border-bottom:none}
.retries-more{font-size:12px;color:var(--text-secondary);margin:0 0 16px}

.fade-out{opacity:.4;pointer-events:none}

/* ── Footer ────────────────────────────────────────── */
.site-footer{background:var(--surface-2);border-top:1px solid var(--border);padding:28px 24px;margin-top:auto}
.site-footer-inner{max-width:960px;margin:0 auto;display:flex;gap:32px;align-items:center;justify-content:space-between}
@media (max-width:640px){.site-footer-inner{flex-direction:column;gap:20px;align-items:flex-start}}
.footer-brand-row{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.footer-mark{width:20px;height:20px}
.footer-mark rect{fill:var(--accent)}
.footer-name{font-size:14px;font-weight:600;color:var(--text);letter-spacing:-0.005em}
.footer-version{font-size:12px;color:var(--text-tertiary);font-weight:500;margin-left:6px}
.footer-meta{font-size:12.5px;color:var(--text-secondary);line-height:1.7}
.footer-meta a{color:var(--accent);text-decoration:none}
.footer-meta a:hover{text-decoration:underline}
.footer-right{display:flex;flex-direction:column;align-items:flex-end;gap:8px}
@media (max-width:640px){.footer-right{align-items:flex-start}}
.footer-contact{font-size:12px;color:var(--text-tertiary)}
.footer-contact a{color:var(--accent);text-decoration:none}

/* ── Modal (key request) ─────────────────────────── */
.modal-overlay{position:fixed;inset:0;background:rgba(24,24,24,0.45);display:none;align-items:center;justify-content:center;z-index:1000;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);max-width:460px;width:100%;padding:32px;position:relative;animation:modal-in 180ms var(--ease)}
@keyframes modal-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.modal-close{position:absolute;top:14px;right:14px;border:none;background:transparent;cursor:pointer;color:var(--text-tertiary);padding:6px;display:inline-flex;border-radius:var(--radius);line-height:0}
.modal-close:hover{color:var(--text);background:var(--surface-2)}
.modal-eyebrow{font-size:11.5px;font-weight:600;letter-spacing:0.08em;color:var(--text-tertiary);text-transform:uppercase;margin-bottom:8px}
.modal-title{font-size:21px;font-weight:600;letter-spacing:-0.02em;color:var(--text);margin:0 0 6px}
.modal-sub{font-size:13.5px;color:var(--text-secondary);line-height:1.55;margin:0 0 18px}
.modal-label{display:block;font-size:12px;font-weight:600;color:var(--text-secondary);margin:14px 0 6px}
.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}
.fld{width:100%;box-sizing:border-box;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius);padding:10px 12px;font-size:14px;font-family:inherit;color:var(--text);transition:border 180ms var(--ease)}
.fld:focus{outline:none;border-color:var(--accent);border-width:2px;padding:9px 11px}
textarea.fld{min-height:96px;resize:vertical;line-height:1.55}
.contact-confirm{display:none;font-size:13px;color:var(--success);font-weight:500;margin-top:14px;padding:10px 12px;background:rgba(0,133,66,0.06);border:1px solid rgba(0,133,66,0.18);border-radius:var(--radius)}
.contact-confirm.show{display:block}
.contact-error{display:none;font-size:13px;color:var(--danger);font-weight:500;margin-top:14px;padding:10px 12px;background:rgba(211,47,47,0.06);border:1px solid rgba(211,47,47,0.2);border-radius:var(--radius)}
.contact-error.show{display:block}
</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────── -->
<header class="site-header">
  <div class="site-header-inner">
    <div class="brand">
      <svg class="brand-mark" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <rect x="2" y="2" width="12" height="12"/>
        <rect x="18" y="2" width="12" height="12"/>
        <rect x="2" y="18" width="12" height="12"/>
        <rect x="18" y="18" width="12" height="12"/>
        <rect x="12" y="12" width="8" height="8"/>
      </svg>
      <span class="wordmark">Amazon Scraper</span>
    </div>
    <div class="lic-badge" id="lic-badge" role="status" aria-live="polite">
      <span class="lic-dot" id="lic-dot"></span>
      <span id="lic-text">Checking…</span>
    </div>
  </div>
</header>

{% if license_status and license_status.status == 'grace' %}
<div class="grace-banner" id="grace-banner">
  <div class="grace-inner">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
    <span>{{ license_status.message }}</span>
    <button type="button" class="grace-x" onclick="document.getElementById('grace-banner').remove()" aria-label="Dismiss">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>
  </div>
</div>
{% endif %}

<div class="page">

  <!-- ── INPUT CARD ────────────────────────────────────────── -->
  <div class="card" id="input-card">
    <div class="eyebrow">Step 1 · Input</div>
    <div class="card-title">Provide ASINs and pincodes</div>
    <div class="card-sub">Upload .txt files or paste lines directly. Each pincode is a 6-digit Indian postal code — one per line. A city name after a comma is optional.</div>

    <!-- Tabbed control -->
    <div class="seg" id="mode-seg" role="tablist">
      <button type="button" class="active" data-mode="upload" onclick="setMode('upload')" role="tab">Upload</button>
      <button type="button" data-mode="paste" onclick="setMode('paste')" role="tab">Paste</button>
    </div>

    <!-- Upload panel -->
    <div id="panel-upload">
      <div class="upload-row">
        <div class="upload-label">ASINs file</div>
        <div class="drop" id="drop-asin" tabindex="0">
          <input type="file" id="asin-file" accept=".txt" hidden onchange="handleFileInput('asin')">
          <div class="drop-default">
            <svg class="drop-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="17 8 12 3 7 8"/>
              <line x1="12" y1="3" x2="12" y2="15"/>
            </svg>
            <div class="drop-title">Upload file</div>
            <div class="drop-hint">Drag and drop, or browse to select a .txt file</div>
          </div>
        </div>
      </div>
      <div class="upload-row">
        <div class="upload-label">Pincodes file</div>
        <div class="drop" id="drop-pin" tabindex="0">
          <input type="file" id="pin-file" accept=".txt" hidden onchange="handleFileInput('pin')">
          <div class="drop-default">
            <svg class="drop-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="17 8 12 3 7 8"/>
              <line x1="12" y1="3" x2="12" y2="15"/>
            </svg>
            <div class="drop-title">Upload file</div>
            <div class="drop-hint">Drag and drop, or browse to select a .txt file</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Paste panel -->
    <div id="panel-paste" style="display:none">
      <div class="paste-grid">
        <div class="paste-block">
          <label>ASINs</label>
          <textarea class="ta" id="asin-text" placeholder="B09W9FND7M&#10;B08N5WRWNW,USB Hub,LC-HUB-4P"></textarea>
          <div class="count-chip" id="asin-count"></div>
        </div>
        <div class="paste-block">
          <label>Pincodes</label>
          <textarea class="ta" id="pin-text" placeholder="110001&#10;400001&#10;560001,Bengaluru"></textarea>
          <div class="count-chip" id="pin-count"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── ACTION ROW ───────────────────────────────────────── -->
  <div class="actions">
    <button class="btn-primary" id="start-btn" onclick="startScraping()">
      <span id="start-label">Run scrape</span>
    </button>
    <button class="btn-secondary btn-danger" id="stop-btn" onclick="stopScraping()" style="display:none">
      Stop
    </button>
  </div>

  <!-- ── PROGRESS CARD ────────────────────────────────────── -->
  <div class="card progress-card" id="progress-card">
    <div class="eyebrow">Step 2 · Progress</div>
    <!-- Completion summary (shown only after a run) -->
    <div class="summary" id="summary">
      <div class="summary-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </div>
      <div class="summary-title" id="summary-title">Scraped X of Y combinations</div>
      <div class="summary-sub" id="summary-sub"></div>
      <button class="btn-secondary" id="open-report-btn" onclick="openReport()">Open report</button>
    </div>

    <div class="stats">
      <div class="stat">
        <div class="stat-num" id="done-num">0</div>
        <div class="stat-label">Done</div>
      </div>
      <div class="stat">
        <div class="stat-num ok" id="success-num">0</div>
        <div class="stat-label">Success</div>
      </div>
      <div class="stat">
        <div class="stat-num fail" id="fail-num">0</div>
        <div class="stat-label">Failed</div>
      </div>
    </div>

    <div class="progress-meta">
      <span id="combo-label">0 / 0 combinations</span>
      <span id="elapsed-label"></span>
    </div>
    <div class="bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="bar-track">
      <div class="bar-fill" id="bar-fill"></div>
    </div>

    <div class="pills" id="pills"></div>

    <div class="log-header">
      <h4>Live log</h4>
      <button class="log-clear" onclick="clearLog()">Clear</button>
    </div>
    <div id="log-box"></div>
  </div>

  <!-- ── RETRIES CARD ─────────────────────────────────────── -->
  <div class="card retries" id="retries-card">
    <div class="eyebrow">Step 3 · Retries</div>
    <div class="retries-header">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
        <line x1="12" y1="9" x2="12" y2="13"/>
        <line x1="12" y1="17" x2="12.01" y2="17"/>
      </svg>
      <span id="retries-header-text">0 combinations need attention</span>
    </div>
    <table class="retries-table">
      <thead>
        <tr><th>ASIN</th><th>Pincode</th><th>City</th><th>Reason</th></tr>
      </thead>
      <tbody id="retries-body"></tbody>
    </table>
    <p class="retries-more" id="retries-more"></p>
    <button class="btn-primary" id="retry-btn" onclick="retryFailed()">
      <span id="retry-label">Retry failed</span>
    </button>
  </div>

</div>

<!-- ── Footer ───────────────────────────────────────────── -->
<footer class="site-footer">
  <div class="site-footer-inner">
    <div class="footer-left">
      <div class="footer-brand-row">
        <svg class="footer-mark" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <rect x="2" y="2" width="12" height="12"/>
          <rect x="18" y="2" width="12" height="12"/>
          <rect x="2" y="18" width="12" height="12"/>
          <rect x="18" y="18" width="12" height="12"/>
          <rect x="12" y="12" width="8" height="8"/>
        </svg>
        <span class="footer-name">Amazon Scraper</span>
        <span class="footer-version">v2.0</span>
      </div>
      <div class="footer-meta">For licensed use only &middot; &copy; 2026</div>
    </div>
    <div class="footer-right">
      <button type="button" class="btn-secondary" onclick="openKeyModal()">Need a key or renewal?</button>
      <div class="footer-contact">Contact: <a href="mailto:avtrixlab@gmail.com">avtrixlab@gmail.com</a></div>
    </div>
  </div>
</footer>

<!-- ── Key-request modal ─────────────────────────────────────── -->
<div class="modal-overlay" id="key-modal" aria-hidden="true">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="km-title">
    <button class="modal-close" type="button" onclick="closeKeyModal()" aria-label="Close">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
    <div class="modal-eyebrow">Support request</div>
    <h2 class="modal-title" id="km-title">Need a key or renewal?</h2>
    <p class="modal-sub">Send a request and we'll get back to you within one business day.</p>
    <form id="contact-form" onsubmit="return submitContact(event)" novalidate>
      <label class="modal-label" for="contact-email">Your email</label>
      <input class="fld" type="email" id="contact-email" placeholder="you@example.com" autocomplete="email" required>
      <label class="modal-label" for="contact-subject">Subject</label>
      <input class="fld" type="text" id="contact-subject" value="License key request" required>
      <label class="modal-label" for="contact-message">Message</label>
      <textarea class="fld" id="contact-message" rows="4" placeholder="Tell us briefly what you need — a new key, a renewal, or to release a machine slot."></textarea>
      <div class="contact-error" id="contact-error"></div>
      <div class="contact-confirm" id="contact-confirm">Your email client should open. If not, write to avtrixlab@gmail.com directly.</div>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" onclick="closeKeyModal()">Cancel</button>
        <button type="submit" class="btn-primary" id="contact-submit">Send request</button>
      </div>
    </form>
  </div>
</div>

<script>
let mode = 'upload';
let asinContent = '', pinContent = '';
let asinFilename = '', pinFilename = '';
let pollTimer = null, sseSource = null;
let isRunning = false;
let pasteDebounce = {};

let _licenseInvalidTitle = false;
function setPageTitle(pct) {
  if (_licenseInvalidTitle) {
    document.title = '[License invalid] Amazon Scraper';
    return;
  }
  document.title = (pct == null) ? 'Amazon Scraper' : '(' + pct + '%) Amazon Scraper';
}
// Retained for compatibility with all callers; status now lives in the page
// title and (when relevant) the worker pill area — the header pill is reserved
// for license status. No-op for the visual header element to keep JPMC clarity.
function setHeaderStatus(text, state) {
  // Stash the state on the document so other UI affordances can read it if
  // needed without re-querying the DOM elsewhere.
  document.body.dataset.runState = state || '';
  document.body.dataset.runLabel = text || '';
}
function setMode(m) {
  mode = m;
  document.getElementById('panel-upload').style.display = (m === 'upload') ? '' : 'none';
  document.getElementById('panel-paste').style.display  = (m === 'paste')  ? '' : 'none';
  const seg = document.getElementById('mode-seg');
  seg.classList.toggle('is-paste', m === 'paste');
  seg.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
}

// ── ASIN / pincode parsing (matches scraper.parse_*_from_text loosely) ──────
// Both parsers return invalid entries as {line, n} so the tooltip can read
// `Invalid: '<line>' line <n>`.
function parseAsins(text) {
  const valid = [], invalid = [];
  text.split(/\r?\n/).forEach((raw, idx) => {
    const line = raw.trim();
    if (!line || line.startsWith('#')) return;
    const asin = line.split(',')[0].trim().toUpperCase();
    if (/^B[0-9A-Z]{9}$/.test(asin)) valid.push(asin);
    else invalid.push({ line: line, n: idx + 1 });
  });
  return { valid, invalid };
}
// Pincode line: a 6-digit token is all that's required. An optional city may
// follow a comma ("110001" valid; "110001,Delhi" valid; "999abc" invalid).
function parsePincodes(text) {
  const valid = [], invalid = [];
  text.split(/\r?\n/).forEach((raw, idx) => {
    const line = raw.trim();
    if (!line || line.startsWith('#')) return;
    const token = (line.split(',')[0] || '').trim();
    if (/^\d{6}$/.test(token)) valid.push(token);
    else invalid.push({ line: line, n: idx + 1 });
  });
  return { valid, invalid };
}

// Strip anything that isn't a digit, comma, or newline from a textarea value
// so the live pincode field can't accept random punctuation. City names use
// letters — those are NOT stripped; letters within a city after the comma
// are kept by the textarea (we only strip control chars that break the
// 6-digit shape). Actually: digits, letters (for city), space, comma, newline.
// The 6-digit token rule is enforced by parsePincodes; this strip is the
// "no garbage like tabs/punctuation" guard.
function sanitizePincodeText(s) {
  // Keep digits, ASCII letters, spaces, commas, newlines, carriage returns.
  return (s || '').replace(/[^0-9A-Za-z,\n\r ]/g, '');
}

function renderChip(elId, parsed, label) {
  const el = document.getElementById(elId);
  if (!el) return;
  const { valid, invalid } = parsed;
  if (!valid.length && !invalid.length) { el.innerHTML = ''; return; }
  let html = `<span class="ok">${valid.length} valid &middot; ${invalid.length} invalid</span>`;
  if (invalid.length) {
    const lines = invalid.slice(0, 8).map(x => `Invalid: '${x.line}' line ${x.n}`);
    if (invalid.length > 8) lines.push(`…and ${invalid.length - 8} more`);
    const tip = lines.join('\n');
    html = `<span class="ok">${valid.length} valid</span> &middot; ` +
           `<span class="bad" title="${tip.replace(/"/g, '&quot;').replace(/&/g, '&amp;').replace(/'/g, '&#39;')}">` +
           `${invalid.length} invalid</span>`;
  }
  el.innerHTML = html;
}

function bindPasteCounts() {
  const aT = document.getElementById('asin-text');
  const pT = document.getElementById('pin-text');
  const upd = (which) => {
    clearTimeout(pasteDebounce[which]);
    pasteDebounce[which] = setTimeout(() => {
      if (which === 'asin') renderChip('asin-count', parseAsins(aT.value), 'ASIN');
      else                  renderChip('pin-count',  parsePincodes(pT.value), 'pincode');
    }, 150);
  };
  aT.addEventListener('input', () => upd('asin'));
  pT.addEventListener('input', () => {
    // Live-strip garbage characters so the 6-digit-token format stays clean.
    const before = pT.value;
    const after = sanitizePincodeText(before);
    if (before !== after) {
      const pos = pT.selectionStart;
      pT.value = after;
      // Best-effort cursor restore — clamp to new length.
      const newPos = Math.max(0, Math.min(after.length, pos - (before.length - after.length)));
      try { pT.setSelectionRange(newPos, newPos); } catch (e) {}
    }
    upd('pin');
  });
}

// ── Drag-and-drop wiring ────────────────────────────────────────────────────
function wireDrop(zoneId, inputId, kind) {
  const zone = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  zone.addEventListener('click', (e) => {
    if (e.target.closest('.clear-x')) return;
    if (!zone.classList.contains('loaded')) input.click();
  });
  zone.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && !zone.classList.contains('loaded')) {
      e.preventDefault(); input.click();
    }
  });
  ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, (e) => {
    e.preventDefault(); e.stopPropagation(); zone.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, (e) => {
    e.preventDefault(); e.stopPropagation(); zone.classList.remove('dragover');
  }));
  zone.addEventListener('drop', (e) => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) readFile(file, kind);
  });
}

function handleFileInput(kind) {
  const input = document.getElementById(kind + '-file');
  const file = input.files[0];
  if (file) readFile(file, kind);
}

function readFile(file, kind) {
  const reader = new FileReader();
  reader.onload = (e) => {
    const content = e.target.result;
    if (kind === 'asin') { asinContent = content; asinFilename = file.name; }
    else                  { pinContent  = content; pinFilename  = file.name; }
    renderLoaded(kind);
  };
  reader.readAsText(file);
}

function renderLoaded(kind) {
  const zone = document.getElementById('drop-' + kind);
  const fname = kind === 'asin' ? asinFilename : pinFilename;
  const content = kind === 'asin' ? asinContent : pinContent;
  const parsed = kind === 'asin' ? parseAsins(content) : parsePincodes(content);
  const label = kind === 'asin' ? 'ASIN' : 'pincode';
  const count = parsed.valid.length;
  zone.classList.add('loaded');
  zone.innerHTML = `
    <div class="loaded-row">
      <svg class="loaded-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"/>
      </svg>
      <div class="loaded-name">${fname}</div>
      <span class="loaded-chip">${count} ${label}${count === 1 ? '' : 's'} detected${parsed.invalid.length ? ' · ' + parsed.invalid.length + ' invalid' : ''}</span>
      <button class="clear-x" type="button" onclick="clearFile('${kind}')" title="Remove">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>
  `;
}

function clearFile(kind) {
  const zone = document.getElementById('drop-' + kind);
  zone.classList.remove('loaded');
  if (kind === 'asin') { asinContent = ''; asinFilename = ''; }
  else                  { pinContent  = ''; pinFilename  = ''; }
  document.getElementById(kind + '-file').value = '';
  zone.innerHTML = `
    <input type="file" id="${kind}-file" accept=".txt" hidden onchange="handleFileInput('${kind}')">
    <div class="drop-default">
      <svg class="drop-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <div class="drop-title">Drop file or click to browse</div>
      <div class="drop-hint">${kind === 'asin' ? 'ASINs format: B09W9FND7M or B09…,Name,Code' : 'Pincodes format: 110001,Delhi — one per line'}</div>
    </div>
  `;
}

// ── Start ────────────────────────────────────────────────────────────────────
async function startScraping() {
  let payload = {};
  if (mode === 'upload') {
    if (!asinContent) { alert('Please choose an ASINs file first.'); return; }
    if (!pinContent)  { alert('Please choose a pincodes file first.'); return; }
    payload.mode         = 'file';
    payload.asin_content = asinContent;
    payload.pin_content  = pinContent;
  } else {
    const asins = document.getElementById('asin-text').value.trim();
    const pins  = document.getElementById('pin-text').value.trim();
    if (!asins) { alert('Please enter at least one ASIN.'); return; }
    if (!pins)  { alert('Please enter at least one pincode.'); return; }
    payload.mode     = 'manual';
    payload.asins    = asins;
    payload.pincodes = pins;
  }

  // Loading state
  setStartLoading(true);

  let data;
  try {
    const res = await fetch('/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch (err) {
    setStartLoading(false);
    alert('Network error: ' + err);
    return;
  }
  if (!data.ok) {
    setStartLoading(false);
    if (data.error === 'license_invalid' || data.relicense) {
      try { sessionStorage.setItem('denied_reason', data.reason || ''); } catch (e) {}
      window.location.href = '/activate';
      return;
    }
    alert('Error: ' + data.error);
    return;
  }

  // Reset run UI
  isRunning = true;
  document.getElementById('start-btn').style.display = 'none';
  document.getElementById('stop-btn').style.display  = 'inline-flex';
  document.getElementById('progress-card').classList.add('show');
  document.getElementById('summary').classList.remove('show');
  document.getElementById('retries-card').classList.remove('show');
  document.getElementById('input-card').classList.add('fade-out');
  document.getElementById('bar-fill').classList.add('shimmer');
  document.getElementById('bar-fill').classList.remove('complete');
  document.getElementById('log-box').innerHTML = '';
  setHeaderStatus('Running', 'running');
  setPageTitle(0);
  buildPills(data.workers);
  startPolling();
  startSSE();
}

function setStartLoading(loading) {
  const btn = document.getElementById('start-btn');
  const label = document.getElementById('start-label');
  btn.disabled = loading;
  if (loading) {
    label.innerHTML = '<span class="spinner"></span>';
  } else {
    label.textContent = 'Run scrape';
  }
}

// ── Stop ─────────────────────────────────────────────────────────────────────
async function stopScraping() {
  try { await fetch('/stop', { method: 'POST' }); } catch {}
  stopPolling();
  isRunning = false;
  setStartLoading(false);
  document.getElementById('start-btn').style.display = '';
  document.getElementById('stop-btn').style.display  = 'none';
  document.getElementById('bar-fill').classList.remove('shimmer');
  document.getElementById('input-card').classList.remove('fade-out');
  setHeaderStatus('Stopped', 'warn');
  setPageTitle(null);
}

// ── Retry (Tier B) ──────────────────────────────────────────────────────────
async function retryFailed() {
  const btn = document.getElementById('retry-btn');
  const label = document.getElementById('retry-label');
  btn.disabled = true;
  label.innerHTML = '<span class="spinner"></span>';
  let data;
  try {
    const res = await fetch('/retry', { method: 'POST' });
    data = await res.json();
  } catch (err) {
    alert('Network error: ' + err);
    btn.disabled = false;
    label.textContent = 'Retry failed';
    return;
  }
  if (!data.ok) {
    if (data.error === 'license_invalid' || data.relicense) {
      try { sessionStorage.setItem('denied_reason', data.reason || ''); } catch (e) {}
      window.location.href = '/activate';
      return;
    }
    alert('Error: ' + data.error);
    btn.disabled = false;
    label.textContent = 'Retry failed';
    return;
  }
  // Re-enable run UI for the retry pass.
  isRunning = true;
  document.getElementById('start-btn').style.display = 'none';
  document.getElementById('stop-btn').style.display  = 'inline-flex';
  document.getElementById('summary').classList.remove('show');
  document.getElementById('bar-fill').classList.add('shimmer');
  document.getElementById('bar-fill').classList.remove('complete');
  document.getElementById('log-box').innerHTML = '';
  setHeaderStatus('Running', 'running');
  setPageTitle(0);
  startPolling();
  startSSE();
}

// ── Status polling ────────────────────────────────────────────────────────────
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollStatus, 600);
  pollStatus();
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (sseSource) { sseSource.close(); sseSource = null; }
}

async function pollStatus() {
  let d;
  try { d = await (await fetch('/status')).json(); }
  catch { return; }

  const pct = d.pct || 0;
  document.getElementById('bar-fill').style.width = pct + '%';
  const track = document.getElementById('bar-track');
  if (track) track.setAttribute('aria-valuenow', String(pct));
  document.getElementById('combo-label').textContent  = d.done + ' / ' + d.total + ' combinations';
  document.getElementById('done-num').textContent     = d.done;
  document.getElementById('success-num').textContent  = d.success;
  document.getElementById('fail-num').textContent     = d.failed;
  document.getElementById('elapsed-label').textContent = d.elapsed ? 'Elapsed ' + d.elapsed : '';

  if (d.running) {
    setHeaderStatus(d.status || 'Running', 'running');
    setPageTitle(pct);
  }

  if (d.workers) {
    Object.entries(d.workers).forEach(([id, w]) =>
      updatePill(id, w.msg || '', w.status || ''));
  }

  // When the run finishes
  if (!d.running && d.status !== 'Running…' && d.status !== 'Building Excel…' && d.status !== 'Retrying failures…') {
    stopPolling();
    isRunning = false;
    setStartLoading(false);   // re-enable + restore the "Run scrape" label
    document.getElementById('start-btn').style.display = '';
    document.getElementById('stop-btn').style.display  = 'none';
    document.getElementById('input-card').classList.remove('fade-out');
    document.getElementById('bar-fill').classList.remove('shimmer');
    setPageTitle(null);

    if (d.status === 'Complete!') {
      document.getElementById('bar-fill').classList.add('complete');
      document.getElementById('bar-fill').style.width = '100%';
      setHeaderStatus('Complete', 'complete');
      showSummary(d);
    } else if (d.status === 'Error') {
      setHeaderStatus('Error', 'error');
    } else if (d.status === 'Stopped') {
      setHeaderStatus('Stopped', 'warn');
    }
  }

  renderRetriesCard(d);
}

function showSummary(d) {
  const summary = document.getElementById('summary');
  const title = document.getElementById('summary-title');
  const sub = document.getElementById('summary-sub');
  title.textContent = `Scraped ${d.success} of ${d.total} combinations`;
  sub.textContent = d.elapsed ? `Completed in ${d.elapsed}` : '';
  summary.classList.add('show');
}

function openReport() {
  // The server already opened the file on completion; this is a no-op trigger
  // for users who closed the file and want to see the path.
  pollStatus().then(() => {
    fetch('/status').then(r => r.json()).then(d => {
      if (d.xlsx) alert('Report saved to:\n' + d.xlsx);
    });
  });
}

// ── SSE log stream ────────────────────────────────────────────────────────────
function startSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource('/stream?from=0');
  sseSource.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.kind === 'eof') { sseSource.close(); return; }
    appendLog(data.ts, data.msg, data.kind);
  };
  sseSource.onerror = () => { if (sseSource) sseSource.close(); };
}

// ── Worker pills ──────────────────────────────────────────────────────────────
function buildPills(n) {
  const el = document.getElementById('pills');
  el.innerHTML = '';
  for (let i = 1; i <= n; i++) {
    const d = document.createElement('div');
    d.id = 'pill-' + i;
    d.className = 'pill active';
    d.innerHTML = `<span class="dot"></span><span class="pill-label">W${i}</span><span class="pill-msg">idle</span>`;
    el.appendChild(d);
  }
}

function updatePill(id, msg, status) {
  let el = document.getElementById('pill-' + id);
  if (!el) {
    // Retry workers come in with IDs ≥ 901 — create pills on demand.
    el = document.createElement('div');
    el.id = 'pill-' + id;
    el.className = 'pill active';
    el.innerHTML = `<span class="dot"></span><span class="pill-label">W${id}</span><span class="pill-msg">…</span>`;
    document.getElementById('pills').appendChild(el);
  }
  const short = msg.replace(/^[\s✅❌⚠️↻↪]+/, '').slice(0, 56);
  el.querySelector('.pill-msg').textContent = short || '…';
  el.classList.remove('active', 'ok', 'fail', 'warn');
  if (status === 'OK' || status === 'DONE')                              el.classList.add('ok');
  else if (status === 'FAILED' || status === 'PINCODE_FAILED' || status === 'ERROR') el.classList.add('fail');
  else if (status === 'CAPTCHA')                                         el.classList.add('warn');
  else                                                                    el.classList.add('active');
}

// ── Log ───────────────────────────────────────────────────────────────────────
function appendLog(ts, msg, kind) {
  const box  = document.getElementById('log-box');
  const line = document.createElement('div');
  const col  = kind === 'error'   ? '#f87171'
             : kind === 'success' ? '#4ade80'
             : kind === 'warn'    ? '#fbbf24'
             : kind === 'info'    ? '#a5b4fc'
             : '#d4d4d8';
  line.style.color = col;
  line.textContent = '[' + ts + ']  ' + msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function clearLog() { document.getElementById('log-box').innerHTML = ''; }

// ── Retries card ─────────────────────────────────────────────────────────────
function renderRetriesCard(d) {
  const card = document.getElementById('retries-card');
  const total = d.failed_total || 0;
  // While running, hide the retry card — only show post-completion.
  if (d.running || d.retrying || total === 0) {
    card.classList.remove('show');
    const btn = document.getElementById('retry-btn');
    btn.disabled = false;
    document.getElementById('retry-label').textContent = 'Retry failed';
    return;
  }
  const list = d.failed_list || [];
  document.getElementById('retries-header-text').textContent =
    `${total} combination${total === 1 ? '' : 's'} need attention`;
  const body = document.getElementById('retries-body');
  body.innerHTML = '';
  list.slice(0, 10).forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td class="mono">${escapeHtml(row.asin)}</td>` +
      `<td class="mono">${escapeHtml(row.pincode)}</td>` +
      `<td>${escapeHtml(row.city)}</td>` +
      `<td class="reason" title="${escapeHtml(row.reason)}">${escapeHtml(row.reason)}</td>`;
    body.appendChild(tr);
  });
  const more = document.getElementById('retries-more');
  more.textContent = total > 10 ? `… and ${total - 10} more` : '';
  document.getElementById('retry-label').textContent = `Retry failed (${total})`;
  card.classList.add('show');
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── License badge ─────────────────────────────────────────────────────────────
let _lastLicenseStatus = null;

function _fmtDate(iso) {
  if (!iso) return '';
  // Accept ISO date or "YYYY-MM-DD" or already-formatted strings; reformat as
  // "12 Jul 2026" if we recognize it.
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  let d;
  try {
    d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
  } catch (e) { return String(iso); }
  return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear();
}

function _renderLicenseBadge(status) {
  const dot = document.getElementById('lic-dot');
  const txt = document.getElementById('lic-text');
  if (!dot || !txt || !status) return;
  dot.className = 'lic-dot';
  const s = status.status || '';
  if (s === 'valid') {
    dot.classList.add('valid');
    txt.textContent = 'Active · until ' + (_fmtDate(status.expires_at) || '—');
    _licenseInvalidTitle = false;
  } else if (s === 'grace') {
    dot.classList.add('grace');
    const days = (status.grace_days_left != null) ? status.grace_days_left : status.days_left;
    txt.textContent = 'Grace · ' + (days != null ? days + ' days left' : 'active');
    _licenseInvalidTitle = false;
  } else if (s === 'expired') {
    dot.classList.add('expired');
    txt.textContent = 'Expired';
    _licenseInvalidTitle = true;
  } else if (s === 'revoked') {
    dot.classList.add('revoked');
    txt.textContent = 'Revoked';
    _licenseInvalidTitle = true;
  } else {
    dot.classList.add('required');
    txt.textContent = 'License required';
    _licenseInvalidTitle = true;
  }
  _lastLicenseStatus = status;
}

async function refreshLicenseBadge() {
  try {
    const r = await fetch('/license-status');
    const j = await r.json();
    _renderLicenseBadge(j);
    // Reflect license-invalid state in the page title so a mid-run revocation
    // is immediately visible to the user even if the tab is in the background.
    if (_licenseInvalidTitle) setPageTitle(null);
  } catch (e) { /* silent */ }
}

// ── Key-request modal ─────────────────────────────────────────────────────────
function openKeyModal() {
  const m = document.getElementById('key-modal');
  if (!m) return;
  m.classList.add('show');
  m.setAttribute('aria-hidden', 'false');
  const e = document.getElementById('contact-email');
  if (e) setTimeout(() => e.focus(), 60);
}
function closeKeyModal() {
  const m = document.getElementById('key-modal');
  if (!m) return;
  m.classList.remove('show');
  m.setAttribute('aria-hidden', 'true');
}
// Close on overlay click (but not when clicking inside the dialog) and on Esc.
document.addEventListener('click', (e) => {
  const m = document.getElementById('key-modal');
  if (m && e.target === m) closeKeyModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeKeyModal();
});

// ── Contact form (mailto) ─────────────────────────────────────────────────────
function submitContact(ev) {
  ev.preventDefault();
  const email   = (document.getElementById('contact-email').value || '').trim();
  const subject = (document.getElementById('contact-subject').value || 'License key request').trim();
  const message = (document.getElementById('contact-message').value || '').trim();
  const errEl = document.getElementById('contact-error');
  const okEl  = document.getElementById('contact-confirm');
  errEl.classList.remove('show'); okEl.classList.remove('show');

  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    errEl.textContent = 'Please enter a valid email address.';
    errEl.classList.add('show');
    return false;
  }
  const status = _lastLicenseStatus ? (_lastLicenseStatus.status || 'unknown') : 'unknown';
  const body =
    'From: ' + email + '\n\n' +
    message + '\n\n' +
    '---\n' +
    'App version: 2.0\n' +
    'License status: ' + status;
  const href = 'mailto:avtrixlab@gmail.com'
    + '?subject=' + encodeURIComponent(subject)
    + '&body='    + encodeURIComponent(body);
  // Open mailto; do not clear the form so the user can retry if mailto fails.
  window.location.href = href;
  okEl.classList.add('show');
  return false;
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  wireDrop('drop-asin', 'asin-file', 'asin');
  wireDrop('drop-pin',  'pin-file',  'pin');
  bindPasteCounts();
  setHeaderStatus('Ready', null);
  // Reflect any in-flight state if the user reloads mid-run.
  pollStatus();
  refreshLicenseBadge();
  setInterval(refreshLicenseBadge, 60000);
});
</script>
</body>
</html>"""


# ── Activation HTML page (same design tokens as the main UI) ───────────────────

ACTIVATE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Activate · Amazon Scraper</title>
<link rel="icon" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect x='2' y='2' width='12' height='12' fill='%230066B2'/><rect x='18' y='2' width='12' height='12' fill='%230066B2'/><rect x='2' y='18' width='12' height='12' fill='%230066B2'/><rect x='18' y='18' width='12' height='12' fill='%230066B2'/><rect x='12' y='12' width='8' height='8' fill='%230066B2'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#f7f7f5;
  --surface:#ffffff;
  --surface-2:#f0efec;
  --border:rgba(0, 0, 0, 0.08);
  --border-strong:rgba(0, 0, 0, 0.16);
  --text:#181818;
  --text-secondary:#555555;
  --text-tertiary:#8a8a8a;
  --accent:#0066B2;
  --accent-hover:#004F8C;
  --accent-dark:#003594;
  --accent-soft:#E6F0F8;
  --success:#008542;
  --warning:#FF8200;
  --danger:#D32F2F;
  --radius:4px;
  --radius-lg:8px;
  --shadow-none:0 0 0 transparent;
  --ease:cubic-bezier(0.4, 0, 0.2, 1);
}
*{box-sizing:border-box}
html,body{background:var(--bg);color:var(--text);margin:0}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;letter-spacing:-0.011em;font-size:14.5px;line-height:1.55;min-height:100vh;display:flex;flex-direction:column}
h1,h2,h3,h4{font-weight:600;letter-spacing:-0.02em;margin:0;color:var(--text)}
.mono{font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace}

/* ── Header ─────────────────────────────────────────── */
.site-header{background:var(--surface);border-bottom:1px solid var(--border);height:64px;display:flex;align-items:center;padding:0 24px}
.site-header-inner{max-width:960px;width:100%;margin:0 auto;display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:10px}
.brand-mark{width:24px;height:24px;flex-shrink:0}
.brand-mark rect{fill:var(--accent)}
.wordmark{font-size:13px;font-weight:600;letter-spacing:0.06em;color:var(--text);text-transform:uppercase}
.lic-badge{display:inline-flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border-strong);padding:6px 12px;border-radius:999px;font-size:12.5px;color:var(--text);font-weight:500}
.lic-dot{width:8px;height:8px;border-radius:50%;background:var(--text-tertiary);flex-shrink:0}
.lic-dot.valid{background:var(--success)}
.lic-dot.grace{background:var(--warning)}
.lic-dot.expired,.lic-dot.revoked{background:var(--danger)}
.lic-dot.required{background:var(--accent)}

/* ── Page / card ───────────────────────────────────── */
.page{flex:1;display:flex;align-items:center;justify-content:center;padding:48px 24px}
.card{width:100%;max-width:480px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:40px;box-shadow:var(--shadow-none);opacity:0;transform:translateY(4px);animation:card-in 240ms var(--ease) forwards}
@keyframes card-in{to{opacity:1;transform:translateY(0)}}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-tertiary);margin-bottom:10px}
.card-title{font-size:28px;font-weight:600;letter-spacing:-0.02em;margin-bottom:10px}
.card-sub{color:var(--text-secondary);font-size:14.5px;margin-bottom:24px;line-height:1.55;max-width:60ch}

.key-label{display:block;font-size:12px;font-weight:600;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.key-input{width:100%;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius);padding:14px;font-family:'JetBrains Mono','SF Mono',Menlo,monospace;font-size:16px;letter-spacing:.04em;color:var(--text);text-align:center;transition:border 180ms var(--ease)}
.key-input::placeholder{color:var(--text-tertiary);letter-spacing:.06em}
.key-input:focus{outline:none;border-color:var(--accent);border-width:2px;padding:13px}
.key-hint{font-size:12px;color:var(--text-tertiary);margin-top:6px}

.btn-primary{display:inline-flex;align-items:center;justify-content:center;gap:8px;width:100%;background:var(--accent);color:#fff;border:none;padding:13px 24px;border-radius:var(--radius);font-weight:600;font-size:14.5px;font-family:inherit;cursor:pointer;transition:background 180ms var(--ease);margin-top:18px}
.btn-primary:hover{background:var(--accent-hover)}
.btn-primary:active{background:var(--accent-dark)}
.btn-primary:disabled{background:var(--accent);opacity:.6;cursor:not-allowed}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,0.45);border-top-color:#fff;border-radius:50%;animation:spin 700ms linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.error-box{display:none;margin-top:14px;padding:12px;background:rgba(211,47,47,0.06);border:1px solid rgba(211,47,47,0.22);border-radius:var(--radius);color:#7a1a1a;font-size:13px;line-height:1.5}
.error-box.show{display:block}

.status-banner{display:none;margin-bottom:18px;padding:12px;border-radius:var(--radius);font-size:13px;line-height:1.55}
.status-banner.show{display:block}
.status-banner.expired,.status-banner.revoked{background:rgba(211,47,47,0.06);border:1px solid rgba(211,47,47,0.22);color:#7a1a1a}

.request-note{margin-top:16px;font-size:12.5px;color:var(--text-secondary);text-align:center}
.request-note a{color:var(--accent);text-decoration:none;font-weight:500;cursor:pointer}
.request-note a:hover{text-decoration:underline}

.success-state{display:none;text-align:center;animation:scale-in 180ms var(--ease)}
.success-state.show{display:block}
@keyframes scale-in{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:scale(1)}}
.success-icon{width:56px;height:56px;border-radius:var(--radius);background:var(--accent-soft);color:var(--accent);display:inline-flex;align-items:center;justify-content:center;margin-bottom:18px}
.success-title{font-size:22px;font-weight:600;color:var(--text);margin-bottom:8px;letter-spacing:-0.02em}
.success-sub{font-size:14px;color:var(--text-secondary)}
.success-sub b{color:var(--text);font-weight:600}

.form-state{transition:opacity 240ms var(--ease)}
.form-state.fade-out{opacity:0;pointer-events:none}

/* ── Footer ────────────────────────────────────────── */
.site-footer{background:var(--surface-2);border-top:1px solid var(--border);padding:28px 24px;margin-top:auto}
.site-footer-inner{max-width:960px;margin:0 auto;display:flex;gap:32px;align-items:center;justify-content:space-between}
@media (max-width:640px){.site-footer-inner{flex-direction:column;gap:20px;align-items:flex-start}}
.footer-brand-row{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.footer-mark{width:20px;height:20px}
.footer-mark rect{fill:var(--accent)}
.footer-name{font-size:14px;font-weight:600;color:var(--text)}
.footer-version{font-size:12px;color:var(--text-tertiary);font-weight:500;margin-left:6px}
.footer-meta{font-size:12.5px;color:var(--text-secondary);line-height:1.7}
.footer-meta a{color:var(--accent);text-decoration:none}
.footer-meta a:hover{text-decoration:underline}
.footer-right{display:flex;flex-direction:column;align-items:flex-end;gap:8px}
@media (max-width:640px){.footer-right{align-items:flex-start}}
.footer-contact{font-size:12px;color:var(--text-tertiary)}
.footer-contact a{color:var(--accent);text-decoration:none}

/* ── Modal (key request) ─────────────────────────── */
.modal-overlay{position:fixed;inset:0;background:rgba(24,24,24,0.45);display:none;align-items:center;justify-content:center;z-index:1000;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);max-width:460px;width:100%;padding:32px;position:relative;animation:modal-in 180ms var(--ease)}
@keyframes modal-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.modal-close{position:absolute;top:14px;right:14px;border:none;background:transparent;cursor:pointer;color:var(--text-tertiary);padding:6px;display:inline-flex;border-radius:var(--radius);line-height:0}
.modal-close:hover{color:var(--text);background:var(--surface-2)}
.modal-eyebrow{font-size:11.5px;font-weight:600;letter-spacing:0.08em;color:var(--text-tertiary);text-transform:uppercase;margin-bottom:8px}
.modal-title{font-size:21px;font-weight:600;letter-spacing:-0.02em;color:var(--text);margin:0 0 6px}
.modal-sub{font-size:13.5px;color:var(--text-secondary);line-height:1.55;margin:0 0 18px}
.modal-label{display:block;font-size:12px;font-weight:600;color:var(--text-secondary);margin:14px 0 6px}
.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}
.modal .btn-primary,.modal .btn-secondary{width:auto;margin-top:0}
.fld{width:100%;box-sizing:border-box;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius);padding:10px 12px;font-size:14px;font-family:inherit;color:var(--text);transition:border 180ms var(--ease)}
.fld:focus{outline:none;border-color:var(--accent);border-width:2px;padding:9px 11px}
textarea.fld{min-height:96px;resize:vertical;line-height:1.55}
.btn-secondary{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--surface);color:var(--accent);border:1px solid var(--accent);padding:11px 22px;border-radius:var(--radius);font-weight:600;font-size:14px;font-family:inherit;cursor:pointer;transition:background 180ms var(--ease)}
.btn-secondary:hover{background:var(--accent-soft)}
.contact-confirm{display:none;font-size:13px;color:var(--success);font-weight:500;margin-top:14px;padding:10px 12px;background:rgba(0,133,66,0.06);border:1px solid rgba(0,133,66,0.18);border-radius:var(--radius)}
.contact-confirm.show{display:block}
.contact-error{display:none;font-size:13px;color:var(--danger);font-weight:500;margin-top:14px;padding:10px 12px;background:rgba(211,47,47,0.06);border:1px solid rgba(211,47,47,0.2);border-radius:var(--radius)}
.contact-error.show{display:block}
</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────── -->
<header class="site-header">
  <div class="site-header-inner">
    <div class="brand">
      <svg class="brand-mark" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <rect x="2" y="2" width="12" height="12"/>
        <rect x="18" y="2" width="12" height="12"/>
        <rect x="2" y="18" width="12" height="12"/>
        <rect x="18" y="18" width="12" height="12"/>
        <rect x="12" y="12" width="8" height="8"/>
      </svg>
      <span class="wordmark">Amazon Scraper</span>
    </div>
    <div class="lic-badge" id="lic-badge" role="status" aria-live="polite">
      <span class="lic-dot required" id="lic-dot"></span>
      <span id="lic-text">License required</span>
    </div>
  </div>
</header>

<div class="page">
  <div class="card">

    <div class="form-state" id="form-state">

      <!-- JS-driven banner: reflects the reason a run was just denied (server
           is authoritative, so this can differ from the local status below). -->
      <div class="status-banner" id="denied-banner"></div>

      {% if license_status and license_status.status == 'expired' %}
      <div class="status-banner expired show" data-fallback="1">
        Your license has expired{% if license_status.expires_at %} on
        <span class="mono">{{ license_status.expires_at }}</span>{% endif %}.
        Enter a renewed key below, or request a new one using the form at the bottom of this page.
      </div>
      {% elif license_status and license_status.status == 'revoked' %}
      <div class="status-banner revoked show" data-fallback="1">
        This installation's license has been revoked. Request a new key using the form at the bottom of this page,
        or enter a replacement below.
      </div>
      {% endif %}

      <div class="eyebrow">License activation</div>
      <h1 class="card-title">Activate your license</h1>
      <p class="card-sub">
        Enter the key you received at purchase. Your license is tied to this machine — keep it confidential.
      </p>

      <label class="key-label" for="key-input">License key</label>
      <input
        id="key-input"
        type="text"
        class="key-input"
        autocomplete="off"
        autocapitalize="characters"
        spellcheck="false"
        placeholder="AMZ-XXXX-XXXX-XXXX-XXXX"
        maxlength="23"
      >
      <div class="key-hint">Format: AMZ-XXXX-XXXX-XXXX-XXXX</div>

      <button class="btn-primary" id="activate-btn" onclick="submitActivation()">
        <span id="activate-label">Activate license</span>
      </button>

      <div class="error-box" id="error-box"></div>

      <p class="request-note">
        Don't have a key?
        <a onclick="openKeyModal()">Request one here.</a>
      </p>
    </div>

    <div class="success-state" id="success-state">
      <div class="success-icon">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </div>
      <div class="success-title">License activated</div>
      <div class="success-sub" id="success-sub"></div>
    </div>

  </div>
</div>

<!-- ── Footer ───────────────────────────────────────────── -->
<footer class="site-footer" id="site-footer">
  <div class="site-footer-inner">
    <div class="footer-left">
      <div class="footer-brand-row">
        <svg class="footer-mark" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <rect x="2" y="2" width="12" height="12"/>
          <rect x="18" y="2" width="12" height="12"/>
          <rect x="2" y="18" width="12" height="12"/>
          <rect x="18" y="18" width="12" height="12"/>
          <rect x="12" y="12" width="8" height="8"/>
        </svg>
        <span class="footer-name">Amazon Scraper</span>
        <span class="footer-version">v2.0</span>
      </div>
      <div class="footer-meta">For licensed use only &middot; &copy; 2026</div>
    </div>
    <div class="footer-right">
      <button type="button" class="btn-secondary" onclick="openKeyModal()">Need a key or renewal?</button>
      <div class="footer-contact">Contact: <a href="mailto:avtrixlab@gmail.com">avtrixlab@gmail.com</a></div>
    </div>
  </div>
</footer>

<!-- ── Key-request modal ─────────────────────────────────────── -->
<div class="modal-overlay" id="key-modal" aria-hidden="true">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="km-title">
    <button class="modal-close" type="button" onclick="closeKeyModal()" aria-label="Close">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
    <div class="modal-eyebrow">Support request</div>
    <h2 class="modal-title" id="km-title">Need a key or renewal?</h2>
    <p class="modal-sub">Send a request and we'll get back to you within one business day.</p>
    <form id="contact-form" onsubmit="return submitContact(event)" novalidate>
      <label class="modal-label" for="contact-email">Your email</label>
      <input class="fld" type="email" id="contact-email" placeholder="you@example.com" autocomplete="email" required>
      <label class="modal-label" for="contact-subject">Subject</label>
      <input class="fld" type="text" id="contact-subject" value="License key request" required>
      <label class="modal-label" for="contact-message">Message</label>
      <textarea class="fld" id="contact-message" rows="4" placeholder="Tell us briefly what you need — a new key, a renewal, or to release a machine slot."></textarea>
      <div class="contact-error" id="contact-error"></div>
      <div class="contact-confirm" id="contact-confirm">Your email client should open. If not, write to avtrixlab@gmail.com directly.</div>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" onclick="closeKeyModal()">Cancel</button>
        <button type="submit" class="btn-primary" id="contact-submit">Send request</button>
      </div>
    </form>
  </div>
</div>

<script>
const KEY_RE = /[^A-Z0-9]/g;
let _lastLicenseStatus = {status: 'needs_activation'};

function formatKey(raw) {
  const cleaned = raw.toUpperCase().replace(KEY_RE, '');
  // Force the AMZ prefix; auto-insert dashes every 4 chars after AMZ.
  let body = cleaned;
  if (body.startsWith('AMZ')) body = body.slice(3);
  const groups = [];
  for (let i = 0; i < body.length && groups.length < 4; i += 4) {
    groups.push(body.slice(i, i + 4));
  }
  if (groups.length === 0 && !cleaned) return '';
  return 'AMZ' + (groups.length ? '-' + groups.join('-') : '');
}

function _fmtDate(iso) {
  if (!iso) return '';
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear();
  } catch (e) { return String(iso); }
}

function _renderLicenseBadge(status) {
  const dot = document.getElementById('lic-dot');
  const txt = document.getElementById('lic-text');
  if (!dot || !txt || !status) return;
  dot.className = 'lic-dot';
  const s = status.status || '';
  if (s === 'valid') {
    dot.classList.add('valid');
    txt.textContent = 'Active · until ' + (_fmtDate(status.expires_at) || '—');
  } else if (s === 'grace') {
    dot.classList.add('grace');
    const days = (status.grace_days_left != null) ? status.grace_days_left : status.days_left;
    txt.textContent = 'Grace · ' + (days != null ? days + ' days left' : 'active');
  } else if (s === 'expired') {
    dot.classList.add('expired');
    txt.textContent = 'Expired';
  } else if (s === 'revoked') {
    dot.classList.add('revoked');
    txt.textContent = 'Revoked';
  } else {
    dot.classList.add('required');
    txt.textContent = 'License required';
  }
  _lastLicenseStatus = status;
}

async function refreshLicenseBadge() {
  try {
    const r = await fetch('/license-status');
    const j = await r.json();
    _renderLicenseBadge(j);
  } catch (e) { /* silent */ }
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('key-input');
  input.addEventListener('input', (e) => {
    const before = e.target.value;
    const after = formatKey(before);
    if (before !== after) {
      e.target.value = after;
      // Move cursor to end after reformat
      e.target.setSelectionRange(after.length, after.length);
    }
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      submitActivation();
    }
  });
  input.focus();
  refreshLicenseBadge();
  setInterval(refreshLicenseBadge, 60000);
  showDeniedBanner();
});

// Show a banner explaining why a scraping run was just denied. The reason is
// stashed in sessionStorage by the main page before redirecting here. This is
// authoritative (it came from the server), so it takes priority over the
// locally-rendered status banner.
function showDeniedBanner() {
  let reason = '';
  try { reason = sessionStorage.getItem('denied_reason') || ''; } catch (e) {}
  if (!reason) return;
  try { sessionStorage.removeItem('denied_reason'); } catch (e) {}

  const MESSAGES = {
    'key_not_found': "We couldn't find your license key on our system. Enter a valid key below, or request one using the form at the bottom of this page.",
    'revoked':       "This license has been revoked. Enter a replacement key below, or contact us using the form at the bottom of this page.",
    'expired':       "Your license has expired. Enter a renewed key below, or request a renewal using the form at the bottom of this page.",
    'max_machines_reached': "This license is already active on the maximum number of machines. Contact us using the form at the bottom of this page to free up a slot.",
    'no_license':    "No license is activated on this machine. Enter your key below, or request one using the form at the bottom of this page.",
  };
  const msg = MESSAGES[reason];
  if (!msg) return;

  // Hide the server-rendered fallback banner to avoid duplicates.
  document.querySelectorAll('.status-banner[data-fallback]').forEach(el => {
    el.classList.remove('show');
  });

  const banner = document.getElementById('denied-banner');
  if (!banner) return;
  banner.className = 'status-banner ' +
    (reason === 'expired' ? 'expired' : 'revoked') + ' show';
  banner.textContent = msg;
}

function showError(msg) {
  const box = document.getElementById('error-box');
  box.textContent = msg;
  box.classList.add('show');
}

function clearError() {
  document.getElementById('error-box').classList.remove('show');
}

function scrollToContact(ev) {
  if (ev) ev.preventDefault();
  const target = document.getElementById('site-footer');
  if (target) target.scrollIntoView({behavior: 'smooth', block: 'start'});
  setTimeout(() => {
    const em = document.getElementById('contact-email');
    if (em) em.focus();
  }, 400);
}

async function submitActivation() {
  const input = document.getElementById('key-input');
  const key = input.value.trim();
  clearError();

  // Strict shape: AMZ-XXXX-XXXX-XXXX-XXXX  (23 chars, 4 groups of 4)
  if (!/^AMZ-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(key)) {
    showError(`That key doesn't look right. Format: AMZ-XXXX-XXXX-XXXX-XXXX`);
    return;
  }

  const btn = document.getElementById('activate-btn');
  const label = document.getElementById('activate-label');
  btn.disabled = true;
  label.innerHTML = '<span class="spinner"></span>';

  let data;
  try {
    const res = await fetch('/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key}),
    });
    data = await res.json();
  } catch (err) {
    btn.disabled = false;
    label.textContent = 'Activate license';
    showError('Network error: ' + err);
    return;
  }

  if (!data.ok) {
    btn.disabled = false;
    label.textContent = 'Activate license';
    showError(data.error || 'Activation failed. Please try again.');
    return;
  }

  // Success — restrained scale-in, no theatrics.
  document.getElementById('form-state').classList.add('fade-out');
  const sub = document.getElementById('success-sub');
  const exp = data.expires_at || '';
  const cust = data.customer || '';
  if (exp && cust) {
    sub.innerHTML = `Expires <b>${_fmtDate(exp)}</b><br>Licensed to <b>${cust}</b>`;
  } else if (exp) {
    sub.innerHTML = `Expires <b>${_fmtDate(exp)}</b>`;
  } else {
    sub.textContent = 'Redirecting…';
  }
  setTimeout(() => {
    document.getElementById('form-state').style.display = 'none';
    document.getElementById('success-state').classList.add('show');
  }, 240);
  setTimeout(() => { window.location.href = '/'; }, 1500);
}

// ── Key-request modal ─────────────────────────────────────────────────────────
function openKeyModal() {
  const m = document.getElementById('key-modal');
  if (!m) return;
  m.classList.add('show');
  m.setAttribute('aria-hidden', 'false');
  const e = document.getElementById('contact-email');
  if (e) setTimeout(() => e.focus(), 60);
}
function closeKeyModal() {
  const m = document.getElementById('key-modal');
  if (!m) return;
  m.classList.remove('show');
  m.setAttribute('aria-hidden', 'true');
}
// Close on overlay click (but not when clicking inside the dialog) and on Esc.
document.addEventListener('click', (e) => {
  const m = document.getElementById('key-modal');
  if (m && e.target === m) closeKeyModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeKeyModal();
});

// ── Contact form (mailto) ─────────────────────────────────────────────────────
function submitContact(ev) {
  ev.preventDefault();
  const email   = (document.getElementById('contact-email').value || '').trim();
  const subject = (document.getElementById('contact-subject').value || 'License key request').trim();
  const message = (document.getElementById('contact-message').value || '').trim();
  const errEl = document.getElementById('contact-error');
  const okEl  = document.getElementById('contact-confirm');
  errEl.classList.remove('show'); okEl.classList.remove('show');

  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    errEl.textContent = 'Please enter a valid email address.';
    errEl.classList.add('show');
    return false;
  }
  const status = _lastLicenseStatus ? (_lastLicenseStatus.status || 'unknown') : 'unknown';
  const body =
    'From: ' + email + '\n\n' +
    message + '\n\n' +
    '---\n' +
    'App version: 2.0\n' +
    'License status: ' + status;
  const href = 'mailto:avtrixlab@gmail.com'
    + '?subject=' + encodeURIComponent(subject)
    + '&body='    + encodeURIComponent(body);
  window.location.href = href;
  okEl.classList.add('show');
  return false;
}
</script>
</body>
</html>"""


# ── Server launch ──────────────────────────────────────────────────────────────

def _open_browser():
    time.sleep(1.0)
    webbrowser.open("http://127.0.0.1:5050")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    # When frozen, always use spawn — fork is unsafe after threads are started
    # and is unavailable on Windows. In dev, prefer fork on Unix for speed.
    if getattr(sys, "frozen", False):
        try:
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            pass
    else:
        try:
            multiprocessing.set_start_method("fork")
        except RuntimeError:
            pass

    print("=" * 48)
    print("Amazon Scraper is starting…")
    print("Opening: http://127.0.0.1:5050")
    print("Press Ctrl+C to quit.")
    print("=" * 48)

    threading.Thread(target=_open_browser, daemon=True).start()
    try:
        app.run(host="127.0.0.1", port=5050, debug=False,
                use_reloader=False, threaded=True)
    except Exception as _e:
        _crash_log = BASE_DIR / "logs" / "crash.log"
        try:
            import traceback
            _crash_log.parent.mkdir(parents=True, exist_ok=True)
            _crash_log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
