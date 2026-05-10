"""
gui.py — Amazon Scraper Web UI
Opens automatically in your browser. No Tkinter required.
"""

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

from flask import Flask, Response, jsonify, render_template_string, request
import scraper as sc

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

    if _st["status_text"] not in ("Stopped", "Error"):
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

        settings = {
            "OUTPUT_FOLDER": "Desktop",
            "OUTPUT_FILENAME": "Amazon_Report_{date}.xlsx",
        }
        xlsx_path = sc.resolve_output_path(settings)
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
    return render_template_string(HTML)


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

    num_workers = min(int(data.get("workers", 4)), len(pincodes))
    min_d = max(float(data.get("min_delay", 3.0)), 1.0)
    max_d = max(float(data.get("max_delay", 8.0)), min_d + 1.0)

    settings = {
        "MIN_DELAY": min_d, "MAX_DELAY": max_d, "MAX_RETRIES": 2,
        "HEADLESS": bool(data.get("headless", True)),
        "SEND_EMAIL": False, "EMAIL_FROM": "", "EMAIL_PASSWORD": "",
        "EMAIL_TO": "", "EMAIL_SUBJECT": "",
        "OUTPUT_FOLDER": "Desktop",
        "OUTPUT_FILENAME": "Amazon_Report_{date}.xlsx",
    }

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
    return jsonify({
        "running": _st["running"],
        "done": _st["done"], "total": _st["total"], "pct": pct,
        "success": _st["success"], "failed": _st["failed"],
        "status": _st["status_text"],
        "elapsed": _fmt_elapsed(_st["start_time"]) if _st["start_time"] else "",
        "workers": {str(k): v for k, v in _st["worker_status"].items()},
        "xlsx": _st["xlsx_path"],
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


# ── HTML (Tailwind CSS via CDN — no extra files needed) ───────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Amazon Scraper</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; }
  .mono { font-family: 'Courier New', monospace; }
  .worker-card { transition: all 0.25s; }
  #log-box::-webkit-scrollbar { width: 6px; }
  #log-box::-webkit-scrollbar-track { background: #111827; }
  #log-box::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }
</style>
</head>
<body class="bg-slate-50 min-h-screen">

<!-- Header -->
<div class="bg-blue-950 text-white px-6 py-4 shadow-lg">
  <div class="max-w-5xl mx-auto flex items-center justify-between">
    <div>
      <h1 class="text-xl font-bold tracking-tight">Amazon.in Scraper</h1>
      <p class="text-blue-400 text-xs mt-0.5">Parallel product data scraper</p>
    </div>
    <span id="header-status" class="text-xs bg-blue-900 px-3 py-1 rounded-full text-blue-200">Ready</span>
  </div>
</div>

<div class="max-w-5xl mx-auto p-6 space-y-5">

  <!-- ── INPUT ── -->
  <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
    <div class="flex items-center gap-2 mb-4">
      <span class="w-6 h-6 rounded-full bg-blue-950 text-white text-xs font-bold flex items-center justify-center">1</span>
      <h2 class="font-semibold text-blue-950">Input — ASINs &amp; Pincodes</h2>
    </div>

    <!-- Mode toggle -->
    <div class="inline-flex rounded-xl border border-slate-200 p-1 mb-5 bg-slate-50 gap-1">
      <button id="btn-upload" onclick="setMode('upload')"
        class="px-4 py-1.5 rounded-lg text-sm font-medium bg-blue-950 text-white transition">
        Upload Files
      </button>
      <button id="btn-manual" onclick="setMode('manual')"
        class="px-4 py-1.5 rounded-lg text-sm font-medium text-slate-500 transition hover:text-slate-800">
        Type / Paste
      </button>
    </div>

    <!-- Upload panel -->
    <div id="panel-upload" class="space-y-3">
      <div class="flex items-center gap-4">
        <span class="w-40 text-sm text-slate-500 shrink-0">ASINs file (.txt)</span>
        <label class="cursor-pointer flex items-center gap-2">
          <span class="px-3 py-1.5 bg-blue-950 text-white text-sm rounded-lg hover:bg-blue-800 transition">Browse…</span>
          <input type="file" id="asin-file" accept=".txt" class="hidden" onchange="loadFile('asin')">
          <span id="asin-fname" class="text-sm text-slate-400">No file selected</span>
        </label>
      </div>
      <div class="flex items-center gap-4">
        <span class="w-40 text-sm text-slate-500 shrink-0">Pincodes file (.txt)</span>
        <label class="cursor-pointer flex items-center gap-2">
          <span class="px-3 py-1.5 bg-blue-950 text-white text-sm rounded-lg hover:bg-blue-800 transition">Browse…</span>
          <input type="file" id="pin-file" accept=".txt" class="hidden" onchange="loadFile('pin')">
          <span id="pin-fname" class="text-sm text-slate-400">No file selected</span>
        </label>
      </div>
      <p class="text-xs text-slate-400 pt-1">
        ASINs format: <code class="bg-slate-100 px-1 rounded">B09W9FND7M</code> or
        <code class="bg-slate-100 px-1 rounded">B09W9FND7M,Item Name,ItemCode</code> &nbsp;·&nbsp;
        # for category header<br>
        Pincodes format: <code class="bg-slate-100 px-1 rounded">110001,Delhi</code> &nbsp;one per line
      </p>
    </div>

    <!-- Manual panel (hidden by default) -->
    <div id="panel-manual" class="hidden">
      <div class="grid grid-cols-2 gap-4">
        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1.5">
            ASINs
            <span class="font-normal text-slate-400 text-xs">— one per line (ASIN or ASIN,Name,Code)</span>
          </label>
          <textarea id="asin-text" rows="9"
            placeholder="B09W9FND7M&#10;B08N5WRWNW,USB Hub,LC-HUB-4P"
            class="mono w-full border border-slate-300 rounded-xl p-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y placeholder-slate-300"></textarea>
        </div>
        <div>
          <label class="block text-sm font-medium text-slate-700 mb-1.5">
            Pincodes
            <span class="font-normal text-slate-400 text-xs">— pincode,City one per line</span>
          </label>
          <textarea id="pin-text" rows="9"
            class="mono w-full border border-slate-300 rounded-xl p-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y">110001,Delhi
400001,Mumbai
560001,Bangalore
600001,Chennai
500001,Hyderabad
411001,Pune</textarea>
        </div>
      </div>
    </div>
  </div>

  <!-- ── SETTINGS ── -->
  <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
    <div class="flex items-center gap-2 mb-4">
      <span class="w-6 h-6 rounded-full bg-blue-950 text-white text-xs font-bold flex items-center justify-center">2</span>
      <h2 class="font-semibold text-blue-950">Settings</h2>
    </div>
    <div class="flex flex-wrap gap-10">

      <!-- Workers -->
      <div>
        <p class="text-sm font-medium text-slate-700 mb-2">Parallel workers</p>
        <div class="flex gap-2" id="worker-btns">
          <button onclick="setWorkers(1)"  class="w-btn">1</button>
          <button onclick="setWorkers(2)"  class="w-btn">2</button>
          <button onclick="setWorkers(4)"  class="w-btn w-btn-active">4</button>
          <button onclick="setWorkers(6)"  class="w-btn">6</button>
          <button onclick="setWorkers(8)"  class="w-btn">8</button>
        </div>
        <p class="text-xs text-slate-400 mt-1.5">4 ≈ 1.5 hrs for 100 ASINs × 8 pincodes</p>
      </div>

      <!-- Delay -->
      <div>
        <p class="text-sm font-medium text-slate-700 mb-2">Delay between requests</p>
        <div class="flex items-center gap-3">
          <div class="flex items-center gap-1.5">
            <span class="text-sm text-slate-500">Min</span>
            <input type="number" id="min-delay" value="3" min="1" max="30" step="0.5"
              class="w-16 border border-slate-300 rounded-lg px-2 py-1.5 text-sm text-center focus:outline-none focus:ring-2 focus:ring-blue-500">
            <span class="text-xs text-slate-400">sec</span>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-sm text-slate-500">Max</span>
            <input type="number" id="max-delay" value="8" min="1" max="60" step="0.5"
              class="w-16 border border-slate-300 rounded-lg px-2 py-1.5 text-sm text-center focus:outline-none focus:ring-2 focus:ring-blue-500">
            <span class="text-xs text-slate-400">sec</span>
          </div>
        </div>
        <p class="text-xs text-amber-500 mt-1.5">⚠ Keep Min ≥ 3 to avoid Amazon blocking</p>
      </div>

      <!-- Headless -->
      <div>
        <p class="text-sm font-medium text-slate-700 mb-2">Browser</p>
        <label class="flex items-center gap-2 cursor-pointer select-none">
          <input type="checkbox" id="headless" checked
            class="w-4 h-4 rounded accent-blue-900 cursor-pointer">
          <span class="text-sm text-slate-600">Headless (hide browser windows)</span>
        </label>
      </div>

    </div>
  </div>

  <!-- ── ACTIONS ── -->
  <div class="flex items-center gap-3">
    <button onclick="startScraping()" id="start-btn"
      class="px-7 py-3 bg-blue-950 hover:bg-blue-800 active:bg-blue-950 text-white font-bold rounded-xl shadow-sm transition text-sm tracking-wide">
      ▶&nbsp; START SCRAPING
    </button>
    <button onclick="stopScraping()" id="stop-btn" disabled
      class="px-7 py-3 bg-red-600 hover:bg-red-700 text-white font-bold rounded-xl shadow-sm transition text-sm tracking-wide disabled:opacity-40 disabled:cursor-not-allowed">
      ■&nbsp; STOP
    </button>
    <span id="status-text" class="text-sm text-slate-500 ml-2">Ready</span>
  </div>

  <!-- ── PROGRESS (hidden until run starts) ── -->
  <div id="progress-section" class="hidden bg-white rounded-2xl shadow-sm border border-slate-200 p-6 space-y-5">
    <div class="flex items-center gap-2">
      <span class="w-6 h-6 rounded-full bg-blue-950 text-white text-xs font-bold flex items-center justify-center">3</span>
      <h2 class="font-semibold text-blue-950">Progress</h2>
    </div>

    <!-- Bar -->
    <div>
      <div class="flex justify-between text-sm text-slate-500 mb-1.5">
        <span id="combo-label">0 / 0 combinations</span>
        <span id="elapsed-label" class="text-slate-400"></span>
      </div>
      <div class="w-full bg-slate-100 rounded-full h-5 overflow-hidden">
        <div id="progress-bar"
          class="bg-blue-950 h-5 rounded-full transition-all duration-500 ease-out flex items-center justify-end pr-2"
          style="width:0%">
          <span id="pct-inner" class="text-white text-xs font-bold hidden">0%</span>
        </div>
      </div>
      <div class="flex justify-between text-xs text-slate-400 mt-1.5">
        <span>✅ <span id="success-count">0</span>&nbsp; success &emsp; ❌ <span id="fail-count">0</span>&nbsp; failed</span>
        <span id="pct-label">0%</span>
      </div>
    </div>

    <!-- Worker strips -->
    <div id="worker-strips" class="grid grid-cols-2 gap-2"></div>

    <!-- Log console -->
    <div>
      <div class="flex items-center justify-between mb-1.5">
        <span class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Live log</span>
        <button onclick="clearLog()"
          class="text-xs text-slate-400 hover:text-slate-600 transition">Clear</button>
      </div>
      <div id="log-box"
        class="mono bg-gray-950 text-gray-100 rounded-xl p-3.5 h-64 overflow-y-auto text-xs leading-5 border border-gray-800">
      </div>
    </div>
  </div>

</div>

<!-- Tailwind arbitrary classes need this -->
<style>
  .w-btn {
    @apply w-11 h-11 rounded-xl border-2 border-slate-200 text-sm font-semibold text-slate-500
           hover:border-blue-950 hover:text-blue-950 transition;
  }
  .w-btn-active {
    @apply border-blue-950 bg-blue-950 text-white hover:bg-blue-800 hover:text-white;
  }
</style>

<script>
// ── Client state ────────────────────────────────────────────────────────────
let mode = 'upload';
let numWorkers = 4;
let asinContent = '';
let pinContent  = '';
let pollTimer   = null;
let sseSource   = null;

// ── Mode toggle ─────────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  document.getElementById('panel-upload').classList.toggle('hidden', m !== 'upload');
  document.getElementById('panel-manual').classList.toggle('hidden', m !== 'manual');
  const btnU = document.getElementById('btn-upload');
  const btnM = document.getElementById('btn-manual');
  if (m === 'upload') {
    btnU.className = 'px-4 py-1.5 rounded-lg text-sm font-medium bg-blue-950 text-white transition';
    btnM.className = 'px-4 py-1.5 rounded-lg text-sm font-medium text-slate-500 transition hover:text-slate-800';
  } else {
    btnM.className = 'px-4 py-1.5 rounded-lg text-sm font-medium bg-blue-950 text-white transition';
    btnU.className = 'px-4 py-1.5 rounded-lg text-sm font-medium text-slate-500 transition hover:text-slate-800';
  }
}

// ── Worker selector ─────────────────────────────────────────────────────────
function setWorkers(n) {
  numWorkers = n;
  document.querySelectorAll('.w-btn').forEach(btn => {
    const active = parseInt(btn.textContent.trim()) === n;
    btn.className = active
      ? 'w-btn w-btn-active w-11 h-11 rounded-xl border-2 border-blue-950 bg-blue-950 text-white text-sm font-semibold transition'
      : 'w-btn w-11 h-11 rounded-xl border-2 border-slate-200 text-sm font-semibold text-slate-500 hover:border-blue-950 hover:text-blue-950 transition';
  });
}

// ── File loader ─────────────────────────────────────────────────────────────
function loadFile(type) {
  const input = document.getElementById(type + '-file');
  const file  = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    if (type === 'asin') { asinContent = e.target.result; }
    else                  { pinContent  = e.target.result; }
    const label = document.getElementById(type + '-fname');
    label.textContent = '✓ ' + file.name;
    label.className = 'text-sm text-green-600';
  };
  reader.readAsText(file);
}

// ── Start ────────────────────────────────────────────────────────────────────
async function startScraping() {
  let payload = {
    workers:   numWorkers,
    min_delay: parseFloat(document.getElementById('min-delay').value),
    max_delay: parseFloat(document.getElementById('max-delay').value),
    headless:  document.getElementById('headless').checked,
  };

  if (mode === 'upload') {
    if (!asinContent) { alert('Please select an ASINs file first.'); return; }
    if (!pinContent)  { alert('Please select a pincodes file first.'); return; }
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

  const res  = await fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!data.ok) { alert('Error: ' + data.error); return; }

  // Reset UI
  document.getElementById('start-btn').disabled = true;
  document.getElementById('stop-btn').disabled  = false;
  document.getElementById('progress-section').classList.remove('hidden');
  setStatus('Running…');
  document.getElementById('log-box').innerHTML = '';
  buildWorkerStrips(data.workers);
  startPolling();
  startSSE();
}

// ── Stop ─────────────────────────────────────────────────────────────────────
async function stopScraping() {
  await fetch('/stop', { method: 'POST' });
  stopPolling();
  document.getElementById('start-btn').disabled = false;
  document.getElementById('stop-btn').disabled  = true;
  setStatus('Stopped');
}

// ── Status polling ────────────────────────────────────────────────────────────
function startPolling() {
  pollTimer = setInterval(pollStatus, 600);
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
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('pct-label').textContent    = pct + '%';
  document.getElementById('combo-label').textContent  = d.done + ' / ' + d.total + ' combinations';
  document.getElementById('success-count').textContent = d.success;
  document.getElementById('fail-count').textContent    = d.failed;
  document.getElementById('elapsed-label').textContent = d.elapsed ? 'Elapsed: ' + d.elapsed : '';
  if (pct >= 5) document.getElementById('pct-inner').classList.remove('hidden');
  document.getElementById('pct-inner').textContent = pct + '%';
  setStatus(d.status);

  if (d.workers) {
    Object.entries(d.workers).forEach(([id, w]) =>
      updateWorker(id, w.msg || '', w.status || ''));
  }

  if (!d.running && d.status !== 'Running…' && d.status !== 'Building Excel…') {
    stopPolling();
    document.getElementById('start-btn').disabled = false;
    document.getElementById('stop-btn').disabled  = true;
  }
}

// ── SSE log stream ────────────────────────────────────────────────────────────
function startSSE() {
  sseSource = new EventSource('/stream?from=0');
  sseSource.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.kind === 'eof') { sseSource.close(); return; }
    appendLog(data.ts, data.msg, data.kind);
  };
  sseSource.onerror = () => { sseSource.close(); };
}

// ── Worker strips ─────────────────────────────────────────────────────────────
function buildWorkerStrips(n) {
  const el = document.getElementById('worker-strips');
  el.innerHTML = '';
  for (let i = 1; i <= n; i++) {
    const d = document.createElement('div');
    d.id = 'wk-' + i;
    d.className = 'worker-card mono border rounded-xl px-3 py-2 text-xs text-slate-400 bg-slate-50 border-slate-200';
    d.textContent = 'W' + i + ': idle';
    el.appendChild(d);
  }
}

function updateWorker(id, msg, status) {
  const el = document.getElementById('wk-' + id);
  if (!el) return;
  const short = msg.replace(/^[\s✅❌⚠️]+/, '').slice(0, 62);
  el.textContent = 'W' + id + ': ' + short;
  const cls = 'worker-card mono border rounded-xl px-3 py-2 text-xs ';
  if (status === 'OK' || status === 'DONE')
    el.className = cls + 'border-green-300 bg-green-50 text-green-800';
  else if (status === 'FAILED' || status === 'PINCODE_FAILED' || status === 'ERROR')
    el.className = cls + 'border-red-300 bg-red-50 text-red-800';
  else if (status === 'CAPTCHA')
    el.className = cls + 'border-amber-300 bg-amber-50 text-amber-800';
  else
    el.className = cls + 'border-blue-300 bg-blue-50 text-blue-800';
}

// ── Log ───────────────────────────────────────────────────────────────────────
function appendLog(ts, msg, kind) {
  const box  = document.getElementById('log-box');
  const line = document.createElement('div');
  const col  = kind === 'error'   ? '#f87171'
             : kind === 'success' ? '#4ade80'
             : kind === 'warn'    ? '#fbbf24'
             : kind === 'info'    ? '#93c5fd'
             : '#e2e8f0';
  line.style.color = col;
  line.textContent = '[' + ts + ']  ' + msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function clearLog() { document.getElementById('log-box').innerHTML = ''; }

function setStatus(text) {
  document.getElementById('status-text').textContent    = text;
  document.getElementById('header-status').textContent  = text;
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
