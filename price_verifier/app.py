"""
app.py — Flask entry point, serves the localhost UI:
Upload -> Map columns -> Confirm -> Progress -> Results (+ Retry) -> History.

The async pipeline (pipeline/runner.py) runs on its own event loop in a
background thread, since Flask's dev/WSGI server is synchronous. Progress
reaches the browser via Server-Sent Events.

Single-user by design — module-level dicts are enough; no per-session
isolation, no job queue. Use is gated by a license key (licensing.py, the
same license server as the Amazon Scraper): pages need an activated key, and
every run (new / resume / retry) is authorized by the server BEFORE the
pipeline thread starts. SQLite is the durable record; everything
in memory here is disposable UI state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, send_file, url_for
from jinja2 import DictLoader
from openpyxl.utils import get_column_letter

from price_verifier import config, licensing
from price_verifier.excel.report import build_brand_report, build_report
from price_verifier.ingest.column_detect import detect_columns, load_table, parse_rows
from price_verifier.ingest.input_parser import InputValidationError
from price_verifier.pipeline.runner import run_pipeline
from price_verifier.storage import checkpoint
from price_verifier.templates_inline import TEMPLATES

APP_HOST = "127.0.0.1"
APP_PORT = 5001
_HEALTH_TOKEN = "price-verifier-ok"

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap
# Templates ship as Python source (templates_inline.py), not a templates/
# folder, so there's no PyInstaller data-file path to get wrong when frozen.
app.jinja_loader = DictLoader(TEMPLATES)
# Only signs the flash-message cookie of this local, single-user app.
app.secret_key = os.urandom(32)

log = logging.getLogger("price_verifier.app")

# ── In-memory UI state ─────────────────────────────────────────────────────
_PENDING_UPLOADS: dict[str, dict] = {}   # upload_id -> {filename, data, sheet, table, detection, report, mapping, created}
_UPLOAD_TTL_S = 3600
_RUN_STATE: dict[str, dict] = {}
_RUN_LOCK = threading.Lock()

_PHASE_LABELS = {
    "fast": "Checking prices",
    "recovery": "Re-checking rows Amazon slowed down, with a fresh session",
    "offers": "Re-checking the remaining rows on Amazon's offers page",
    "browser": "Final check of the remaining rows in Google Chrome",
    "done": "Finishing up — building your report",
    "cancelled": "Pausing — saving progress",
}

_FIELD_UI = {
    "asin": dict(label="ASIN or Amazon link", required=True, none_label="",
                 help="The column with ASINs (B0…) or Amazon product links."),
    "expected_price": dict(label="Expected price (₹)", required=True, none_label="",
                           help="The price you agreed with the seller — rows differing by more than your tolerance are flagged."),
    "brand": dict(label="Brand", required=False, none_label="— Use the brand shown on Amazon —",
                  help="Groups the report into one sheet per brand. Optional."),
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _evict_stale_uploads() -> None:
    cutoff = time.time() - _UPLOAD_TTL_S
    for uid in [k for k, v in _PENDING_UPLOADS.items() if v["created"] < cutoff]:
        _PENDING_UPLOADS.pop(uid, None)


def _col_label(index: int, header: str) -> str:
    letter = get_column_letter(index + 1)
    return letter if header in ("", f"Column {letter}") else f"{letter} · {header}"


def _mapping_view(entry: dict, error: str | None = None):
    det = entry["detection"]
    table = det.table
    selected = entry.get("mapping") or det.mapping
    fields = []
    for key in ("asin", "expected_price", "brand"):
        options = []
        for o in det.options.get(key, []):
            samples = ", ".join(s for s in o.samples if s)[:60]
            label = _col_label(o.index, o.header) + (f" — e.g. {samples}" if samples else "")
            options.append({"index": o.index, "label": label})
        options.sort(key=lambda o: o["index"])  # stable left-to-right order in the dropdown
        fields.append({
            "key": key, **_FIELD_UI[key],
            "options": options,
            "selected": selected.get(key),
            "confidence": det.confidence.get(key, 0.0) if selected.get(key) == det.mapping.get(key) else 1.0,
        })
    return render_template(
        "mapping.html",
        upload_id=entry["id"], filename=entry["filename"],
        sheets=table.sheet_names, sheet=table.sheet_name,
        header_row=table.header_row, row_count=len(table.rows),
        headers=[_col_label(i, h) for i, h in enumerate(table.headers)],
        preview=det.preview, fields=fields, warnings=det.warnings, error=error,
    )


def _describe_mapping(entry: dict) -> dict:
    table = entry["detection"].table
    mapping = entry["mapping"]

    def name(key):
        idx = mapping.get(key)
        if idx is None:
            return "brand shown on Amazon" if key == "brand" else "—"
        return f"column {_col_label(idx, table.headers[idx])}"

    return {k: name(k) for k in ("asin", "expected_price", "brand")}


def _confirm_view(entry: dict, error: str | None = None):
    report = entry["report"]
    brands = {(r.brand or "").strip() for r in report.valid}
    brand_from_amazon = entry["mapping"].get("brand") is None
    return render_template(
        "confirm.html",
        upload_id=entry["id"], filename=entry["filename"], sheet=entry["detection"].table.sheet_name,
        mapping_desc=_describe_mapping(entry),
        total_rows=report.total_rows, valid_rows=report.valid_rows, invalid_rows=report.invalid,
        brand_count="—" if brand_from_amazon else len(brands), brand_from_amazon=brand_from_amazon,
        default_concurrency=config.DEFAULT_CONCURRENCY, max_concurrency=config.MAX_CONCURRENCY,
        default_tolerance_abs=config.DEFAULT_TOLERANCE_ABS,
        default_tolerance_pct=config.DEFAULT_TOLERANCE_PCT,
        error=error,
    )


def _float_field(name: str, default: float, lo: float = 0.0) -> float:
    try:
        return max(lo, float(request.form.get(name, "") or default))
    except ValueError:
        return default


def _int_field(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(request.form.get(name, "") or default)))
    except ValueError:
        return default


def _fmt_duration(started: str | None, finished: str | None) -> str | None:
    try:
        secs = int((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds())
    except (TypeError, ValueError):
        return None
    m, s = divmod(max(0, secs), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


# ── Run orchestration ──────────────────────────────────────────────────────

def _claim_run(run_id: str, total: int) -> bool:
    """Atomically: refuse if this run already has a live pipeline, else
    register one. A double-clicked Resume/Retry must not start two pipelines
    on the same rows (double fetching, and Pause would only stop one)."""
    with _RUN_LOCK:
        st = _RUN_STATE.get(run_id)
        if st is not None and st["status"] == "running":
            return False
        _init_run_state_locked(run_id, total)
        return True


def _init_run_state_locked(run_id: str, total: int) -> None:
    """Caller holds _RUN_LOCK."""
    _RUN_STATE[run_id] = {
        "done": 0, "total": total, "matched": 0, "mismatched": 0,
        "out_of_stock": 0, "failed": 0, "status": "running",
        "started_at": time.time(), "phase": "fast", "phase_detail": {},
        "loop": None, "cancel": None,
    }


def _record_outcome(run_id: str, outcome) -> None:
    key = {
        checkpoint.STATUS_MATCHED: "matched",
        checkpoint.STATUS_MISMATCHED: "mismatched",
        checkpoint.STATUS_FAILED: "failed",
    }.get(outcome.status, "out_of_stock")
    with _RUN_LOCK:
        st = _RUN_STATE.get(run_id)
        if st is not None:
            st["done"] += 1
            st[key] += 1


def _record_phase(run_id: str, phase: str, detail: dict) -> None:
    detail = dict(detail or {})
    with _RUN_LOCK:
        st = _RUN_STATE.get(run_id)
        if st is None:
            return
        if detail.get("event") == "block":
            # Mid-pass pause: keep the pass's own label, add a countdown.
            st["block_pause"] = (time.time(), float(detail.get("pause_seconds") or 0))
            return
        st["phase"] = phase
        st["phase_detail"] = detail
        st["phase_started"] = time.time()
        st["block_pause"] = None


def _run_in_background(run_id: str, items, run_cfg: checkpoint.RunConfig, use_browser: bool) -> None:
    async def on_item_done(outcome):
        _record_outcome(run_id, outcome)

    async def on_phase(phase, detail):
        _record_phase(run_id, phase, detail)
        await asyncio.to_thread(checkpoint.set_run_phase, run_id, phase)

    async def main():
        cancel = asyncio.Event()
        with _RUN_LOCK:
            _RUN_STATE[run_id]["loop"] = asyncio.get_running_loop()
            _RUN_STATE[run_id]["cancel"] = cancel
        return await run_pipeline(
            run_id, items,
            concurrency=run_cfg.concurrency,
            tolerance_abs=run_cfg.tolerance_abs,
            tolerance_pct=run_cfg.tolerance_pct,
            use_browser_fallback=use_browser,
            on_item_done=on_item_done,
            on_phase=on_phase,
            cancel_event=cancel,
        )

    try:
        log.info("Run %s: starting %d rows (concurrency=%d, browser=%s)",
                 run_id, len(items), run_cfg.concurrency, use_browser)
        # run_pipeline persists its own phase + stats_json to the DB.
        stats = asyncio.run(main())
        log.info("Run %s: pipeline finished %s", run_id, json.dumps(stats.as_dict(), default=str))
        if stats.cancelled:
            checkpoint.mark_run_paused(run_id)
            final_status = "paused"
        else:
            # Every row is saved by now, so a report problem (file open in
            # Excel, disk full) must not turn a finished run into "crashed":
            # finish it anyway; /download rebuilds the report on demand.
            try:
                output_path = str(build_report(run_id))
            except Exception:
                log.exception("Run %s: report build failed; it will be rebuilt on download", run_id)
                output_path = None
            checkpoint.finish_run(run_id, output_path)
            final_status = "completed"
        with _RUN_LOCK:
            _RUN_STATE[run_id]["status"] = final_status
    except Exception:
        log.exception("Run %s crashed", run_id)
        checkpoint.mark_run_crashed(run_id)
        with _RUN_LOCK:
            if run_id in _RUN_STATE:
                _RUN_STATE[run_id]["status"] = "crashed"


def _start_run(items: list[checkpoint.RunItemRow], run_cfg: checkpoint.RunConfig,
               use_browser: bool, run_id: str | None = None) -> str | None:
    """Returns the run_id, or None if that run is already being processed."""
    try:
        from price_verifier.fetcher.browser_fallback import sweep_stale_profiles
        from price_verifier.fetcher.debug_dump import prune_debug_html
        prune_debug_html()          # the app may stay open for weeks
        sweep_stale_profiles()
    except Exception:
        log.debug("pre-run cleanup failed", exc_info=True)
    if run_id is None:
        run_id = checkpoint.create_run(run_cfg, items)
        _claim_run(run_id, total=len(items))
    else:
        if not _claim_run(run_id, total=len(items)):
            return None
        checkpoint.reopen_run_for_retry(run_id)
    thread = threading.Thread(target=_run_in_background, args=(run_id, items, run_cfg, use_browser), daemon=True)
    thread.start()
    return run_id


def _is_active(run_id: str) -> bool:
    with _RUN_LOCK:
        st = _RUN_STATE.get(run_id)
        return bool(st and st["status"] == "running")


# ── License gate ───────────────────────────────────────────────────────────
_LICENSE_CACHE: dict = {"status": None, "at": 0.0}
_LICENSE_CACHE_S = 60.0
# Reachable without a valid license: the activation page itself, and what a
# run already in progress needs (watch it, pause it) — the gate is at START.
_LICENSE_OPEN_ENDPOINTS = {"activate", "license_status_json", "healthz", "static",
                           "progress_view", "stream", "cancel"}


def _license_status(force: bool = False) -> dict:
    if not licensing.enforced():
        return {"status": "valid", "disabled": True}
    now = time.time()
    if force or _LICENSE_CACHE["status"] is None or now - _LICENSE_CACHE["at"] > _LICENSE_CACHE_S:
        _LICENSE_CACHE["status"] = licensing.client().status()
        _LICENSE_CACHE["at"] = now
    return _LICENSE_CACHE["status"]


@app.before_request
def _license_gate():
    if request.endpoint is None or request.endpoint in _LICENSE_OPEN_ENDPOINTS:
        return None
    if _license_status()["status"] in licensing.BLOCKING_STATUSES:
        return redirect(url_for("activate"))
    return None


@app.context_processor
def _inject_license():
    return {"license_status": _license_status()}


def _authorize_run(item_count: int):
    """Server authorization for a run — call BEFORE _start_run. Returns None
    if the run may start, a redirect to the activation page for a license
    problem, or a plain error message for a transient one (no internet and
    no successful check in the last 24 h)."""
    if not licensing.enforced():
        return None
    result = licensing.client().authorize_run(item_count)
    if result.ok:
        if result.offline:
            flash("The license server couldn't be reached — this run uses the 24-hour offline allowance.",
                  "notice")
        return None
    _license_status(force=True)
    if result.relicense:
        flash(result.message, "error")
        return redirect(url_for("activate"))
    return result.message


def _restart_existing_run(run_id: str):
    """Shared by Resume and Retry: re-queue this run's pending + failed rows."""
    run = checkpoint.get_run(run_id)
    if run is None:
        return redirect(url_for("index"))
    if _is_active(run_id):
        return redirect(url_for("progress_view", run_id=run_id))
    items = checkpoint.get_pending_and_retryable_items(run_id)
    if not items:
        return redirect(url_for("results", run_id=run_id))
    run_cfg = checkpoint.RunConfig(
        input_filename=run["input_filename"],
        price_source=run["price_source"], tolerance_abs=run["tolerance_abs"],
        tolerance_pct=run["tolerance_pct"], concurrency=run["concurrency"],
        use_browser=bool(run.get("use_browser", 1)),
    )
    denied = _authorize_run(len(items))
    if denied is not None:
        if isinstance(denied, str):
            flash(denied, "error")
            return redirect(url_for("history"))
        return denied
    _start_run(items, run_cfg, use_browser=run_cfg.use_browser, run_id=run_id)
    return redirect(url_for("progress_view", run_id=run_id))


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return _HEALTH_TOKEN


@app.route("/")
def index():
    incomplete = checkpoint.find_incomplete_run()
    if incomplete and _is_active(incomplete["run_id"]):
        incomplete = None
    return render_template("upload.html", incomplete=incomplete)


@app.errorhandler(413)
def too_large(_e):
    return render_template("upload.html", error="That file is too large (limit 50 MB).", incomplete=None), 413


@app.route("/upload", methods=["POST"])
def upload():
    _evict_stale_uploads()
    file = request.files.get("file")
    if file is None or not file.filename:
        return render_template("upload.html", error="Please choose a CSV or Excel file.", incomplete=None)
    data = file.read()
    try:
        table = load_table(file.filename, data)
        detection = detect_columns(table)
    except InputValidationError as e:
        return render_template("upload.html", error=str(e), incomplete=None)
    except Exception:
        log.exception("Could not read upload %s", file.filename)
        return render_template(
            "upload.html", incomplete=None,
            error="Sorry — that file couldn't be read. Please save it as .xlsx or .csv and try again.",
        )

    upload_id = uuid.uuid4().hex
    _PENDING_UPLOADS[upload_id] = {
        "id": upload_id, "filename": file.filename, "data": data,
        "detection": detection, "mapping": None, "report": None, "created": time.time(),
    }
    return redirect(url_for("map_columns", upload_id=upload_id))


@app.route("/map/<upload_id>", methods=["GET", "POST"])
def map_columns(upload_id: str):
    entry = _PENDING_UPLOADS.get(upload_id)
    if entry is None:
        return render_template("upload.html", error="That upload has expired — please upload the file again.", incomplete=None)

    sheet = request.args.get("sheet") or None
    if sheet and sheet != entry["detection"].table.sheet_name:
        try:
            entry["detection"] = detect_columns(load_table(entry["filename"], entry["data"], sheet_name=sheet))
            entry["mapping"] = None
        except InputValidationError as e:
            return _mapping_view(entry, error=str(e))

    if request.method == "GET":
        return _mapping_view(entry)

    ncols = len(entry["detection"].table.headers)
    mapping: dict[str, int | None] = {}
    for key in ("asin", "expected_price", "brand"):
        raw = request.form.get(f"col_{key}", "")
        mapping[key] = int(raw) if raw.isdigit() and int(raw) < ncols else None
    entry["mapping"] = mapping

    if mapping["asin"] is None or mapping["expected_price"] is None:
        return _mapping_view(entry, error="Please choose both the ASIN column and the expected price column.")
    chosen = [v for v in mapping.values() if v is not None]
    if len(chosen) != len(set(chosen)):
        return _mapping_view(entry, error="Each column can only be used once — please pick different columns.")

    try:
        report = parse_rows(entry["detection"].table, mapping)
    except InputValidationError as e:
        return _mapping_view(entry, error=str(e))
    entry["report"] = report
    if report.valid_rows == 0:
        return _mapping_view(
            entry,
            error="None of the rows could be read with these columns — check that the ASIN and price columns are right.",
        )
    return _confirm_view(entry)


@app.route("/start", methods=["POST"])
def start():
    upload_id = request.form.get("upload_id", "")
    entry = _PENDING_UPLOADS.get(upload_id)
    if entry is None or entry.get("report") is None:
        return redirect(url_for("index"))

    run_cfg = checkpoint.RunConfig(
        input_filename=entry["filename"],
        tolerance_abs=_float_field("tolerance_abs", config.DEFAULT_TOLERANCE_ABS),
        tolerance_pct=_float_field("tolerance_pct", config.DEFAULT_TOLERANCE_PCT),
        concurrency=_int_field("concurrency", config.DEFAULT_CONCURRENCY, config.MIN_CONCURRENCY, config.MAX_CONCURRENCY),
    )
    items = [
        checkpoint.RunItemRow(asin=r.asin, expected_price=r.expected_price, brand=(r.brand or "").strip())
        for r in entry["report"].valid
    ]
    run_cfg.use_browser = request.form.get("use_browser") == "1"
    # License check before anything starts; the upload is kept so a
    # transient failure (no internet) can simply be retried from this page.
    denied = _authorize_run(len(items))
    if denied is not None:
        return _confirm_view(entry, error=denied) if isinstance(denied, str) else denied
    _PENDING_UPLOADS.pop(upload_id, None)
    run_id = _start_run(items, run_cfg, use_browser=run_cfg.use_browser)
    return redirect(url_for("progress_view", run_id=run_id))


@app.route("/resume/<run_id>", methods=["POST"])
def resume(run_id: str):
    return _restart_existing_run(run_id)


@app.route("/retry/<run_id>", methods=["POST"])
def retry(run_id: str):
    return _restart_existing_run(run_id)


@app.route("/cancel/<run_id>", methods=["POST"])
def cancel(run_id: str):
    with _RUN_LOCK:
        st = _RUN_STATE.get(run_id)
        loop, ev = (st or {}).get("loop"), (st or {}).get("cancel")
    if loop is not None and ev is not None:
        loop.call_soon_threadsafe(ev.set)
    return ("", 204)


@app.route("/discard/<run_id>", methods=["POST"])
def discard(run_id: str):
    checkpoint.mark_run_crashed(run_id)  # keeps history, removes it from "incomplete" detection
    return redirect(url_for("index"))


@app.route("/progress/<run_id>")
def progress_view(run_id: str):
    with _RUN_LOCK:
        in_memory = run_id in _RUN_STATE
    if not in_memory:
        # Not running in this app session (restarted app, old link): the
        # live feed has nothing to show, so send them somewhere useful.
        run = checkpoint.get_run(run_id)
        if run and run["status"] == "completed":
            return redirect(url_for("results", run_id=run_id))
        return redirect(url_for("history") if run else url_for("index"))
    return render_template("progress.html", run_id=run_id)


def _progress_payload(st: dict) -> dict:
    now = time.time()
    elapsed = now - st["started_at"]
    rate = st["done"] / elapsed if elapsed > 0 and st["done"] else 0.0
    remaining = st["total"] - st["done"]
    eta = remaining / rate if rate > 0 else None

    phase = st.get("phase") or "fast"
    label = _PHASE_LABELS.get(phase, phase)
    detail = st.get("phase_detail") or {}
    if detail.get("items"):
        label += f" ({detail['items']} rows)"
    if detail.get("event") == "start":
        pause = float(detail.get("pause_seconds") or 0)
        left = pause - (now - st.get("phase_started", now))
        if left > 0:
            label += f" — starting in {int(left) + 1}s"
    block = st.get("block_pause")
    if block:
        left = block[1] - (now - block[0])
        if left > 0:
            label += f" — Amazon asked us to slow down, resuming in {int(left) + 1}s"
    return {
        "done": st["done"], "total": st["total"],
        "matched": st["matched"], "mismatched": st["mismatched"],
        "out_of_stock": st["out_of_stock"], "failed": st["failed"],
        "elapsed_seconds": round(elapsed), "eta_seconds": round(eta) if eta else None,
        "rate_label": f"{rate * 60:.0f} ASINs/min" if rate else None,
        "phase_label": label, "status": st["status"],
    }


@app.route("/stream/<run_id>")
def stream(run_id: str):
    def gen():
        last = None
        while True:
            with _RUN_LOCK:
                st = _RUN_STATE.get(run_id)
                payload = _progress_payload(st) if st else None
            if payload is None:
                yield "event: error\ndata: unknown run\n\n"
                return
            key = json.dumps(payload, sort_keys=True)
            if key != last:
                last = key
                yield f"data: {key}\n\n"
                if payload["status"] != "running":
                    return
            time.sleep(1)

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.route("/results/<run_id>")
def results(run_id: str):
    run = checkpoint.get_run(run_id)
    if run is None:
        return redirect(url_for("index"))
    if _is_active(run_id):
        return redirect(url_for("progress_view", run_id=run_id))
    return render_template(
        "results.html", run=run,
        brands=checkpoint.get_brand_issue_counts(run_id),
        duration=_fmt_duration(run.get("started_at"), run.get("finished_at")),
    )


@app.route("/download/<run_id>")
def download(run_id: str):
    run = checkpoint.get_run(run_id)
    if run is None:
        return redirect(url_for("index"))
    path = Path(run["output_path"]) if run.get("output_path") else None
    if path is None or not path.exists():
        path = build_report(run_id)
    return send_file(str(path), as_attachment=True, download_name=path.name)


@app.route("/download/<run_id>/brand/<path:brand>")
def download_brand(run_id: str, brand: str):
    """One brand's issue list as a small standalone workbook, ready to attach
    to the email for that brand's seller."""
    if checkpoint.get_run(run_id) is None:
        return redirect(url_for("index"))
    path = build_brand_report(run_id, brand)
    return send_file(str(path), as_attachment=True, download_name=path.name)


@app.route("/activate", methods=["GET", "POST"])
def activate():
    if not licensing.enforced():
        return redirect(url_for("index"))
    lic = licensing.client()
    error = None
    if request.method == "POST":
        result = lic.activate(request.form.get("key", ""))
        if result.ok:
            status = _license_status(force=True)
            who = status.get("customer")
            flash(f"License activated{' for ' + who if who else ''}. Welcome!", "ok")
            return redirect(url_for("index"))
        error = result.message
    status = _license_status(force=True)
    current = lic.load() or {}
    return render_template(
        "activate.html", error=error, status=status,
        current_key=current.get("key", ""), machine_id=licensing.get_machine_id(),
        already_valid=status["status"] not in licensing.BLOCKING_STATUSES,
    )


@app.route("/license-status")
def license_status_json():
    return jsonify(_license_status(force=request.args.get("refresh") == "1"))


@app.route("/history")
def history():
    with _RUN_LOCK:
        active = {rid for rid, st in _RUN_STATE.items() if st["status"] == "running"}
    return render_template("history.html", runs=checkpoint.list_runs(), active_runs=active)


# ── Startup ────────────────────────────────────────────────────────────────

def _init_startup_log() -> None:
    """A frozen Windows build runs windowed (console=False), so an exception
    here would otherwise vanish with zero feedback. Mirrors gui.py."""
    try:
        log_dir = config.BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_dir / "app.log"), encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        logging.getLogger("startup").info("BASE_DIR=%s frozen=%s", config.BASE_DIR, getattr(sys, "frozen", False))
    except Exception:
        pass


def _show_fatal_error(message: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "Price Verification Tool — Startup Error", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _already_running() -> bool:
    """True if another copy of this app is already serving on our port —
    the vendor double-clicked the .exe twice. Then we just reopen the
    browser tab instead of failing with 'address already in use'."""
    try:
        with socket.create_connection((APP_HOST, APP_PORT), timeout=1.0) as s:
            s.sendall(f"GET /healthz HTTP/1.0\r\nHost: {APP_HOST}\r\n\r\n".encode())
            return _HEALTH_TOKEN.encode() in s.recv(4096)
    except OSError:
        return False


def main() -> None:
    _init_startup_log()
    url = f"http://{APP_HOST}:{APP_PORT}/"
    try:
        if _already_running():
            webbrowser.open(url)
            return
        from price_verifier.storage.db import init_db
        init_db(config.DB_PATH)
        try:
            from price_verifier.fetcher.browser_fallback import sweep_stale_profiles
            from price_verifier.fetcher.debug_dump import prune_debug_html
            prune_debug_html()
            sweep_stale_profiles()
        except Exception:
            pass
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
    except OSError:
        logging.getLogger("startup").exception("Port %s unavailable", APP_PORT)
        _show_fatal_error(
            f"The Price Verification Tool couldn't start because port {APP_PORT} is in use by another program.\n\n"
            "Close other programs (or restart the computer) and try again."
        )
        sys.exit(1)
    except Exception:
        logging.getLogger("startup").exception("Fatal startup error")
        _show_fatal_error(
            "The Price Verification Tool could not start.\n\n"
            f"Details were written to:\n{config.BASE_DIR / 'logs' / 'app.log'}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
