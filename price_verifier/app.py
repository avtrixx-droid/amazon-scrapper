"""
app.py — Flask entry point, serves the localhost UI (spec section 9):
Upload -> Progress -> Results -> History.

The async pipeline (pipeline/runner.py) runs on its own event loop in a
background thread, since Flask's dev/WSGI server is synchronous. Progress
reaches the browser via Server-Sent Events, the same shape this repo's own
gui.py already uses for its worker-queue -> browser progress strip.

Single-user by design (spec section 9) — one module-level `_STATE` dict is
enough; no per-session isolation, no auth, no job queue.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for

from price_verifier import config
from price_verifier.excel.report import build_brand_report, build_report
from price_verifier.ingest.input_parser import InputValidationError, parse_upload
from price_verifier.pipeline.runner import run_pipeline
from price_verifier.storage import checkpoint

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# ── In-memory state (single-user; nothing here needs to survive a restart —
#    SQLite is the durable record) ─────────────────────────────────────────
_PENDING_UPLOADS: dict[str, dict] = {}   # upload_id -> {report, filename, bytes}
_RUN_STATE: dict[str, dict] = {}         # run_id -> {done, total, matched, mismatched, oos, failed, events, status, started_at}
_RUN_LOCK = threading.Lock()


def _init_run_state(run_id: str, total: int) -> None:
    with _RUN_LOCK:
        _RUN_STATE[run_id] = {
            "done": 0, "total": total, "matched": 0, "mismatched": 0,
            "out_of_stock": 0, "failed": 0, "status": "running",
            "started_at": time.time(), "events": [],
        }


def _record_event(run_id: str, outcome) -> None:
    with _RUN_LOCK:
        st = _RUN_STATE.get(run_id)
        if st is None:
            return
        st["done"] += 1
        key = {
            checkpoint.STATUS_MATCHED: "matched",
            checkpoint.STATUS_MISMATCHED: "mismatched",
            checkpoint.STATUS_OUT_OF_STOCK: "out_of_stock",
            checkpoint.STATUS_UNAVAILABLE: "out_of_stock",
            checkpoint.STATUS_NOT_FOUND: "out_of_stock",
            checkpoint.STATUS_FAILED: "failed",
        }.get(outcome.status)
        if key:
            st[key] += 1
        st["events"].append(f"{outcome.asin}: {outcome.status}")
        st["events"] = st["events"][-50:]  # bounded — this is a UI tail, not the record of truth


def _run_in_background(run_id: str, items, run_cfg: checkpoint.RunConfig) -> None:
    async def on_item_done(outcome):
        _record_event(run_id, outcome)

    async def main():
        # No session passed: price doesn't vary by pincode for this catalog
        # (vendor-confirmed), so run_pipeline uses its anonymous-client
        # default — no browser dependency, no pincode/cookie bootstrap.
        await run_pipeline(
            run_id, items, run_cfg.concurrency,
            run_cfg.tolerance_abs, run_cfg.tolerance_pct, on_item_done=on_item_done,
        )

    try:
        asyncio.run(main())
        output_path = build_report(run_id)
        checkpoint.finish_run(run_id, str(output_path))
        with _RUN_LOCK:
            _RUN_STATE[run_id]["status"] = "completed"
    except Exception:
        app.logger.exception("Run %s crashed", run_id)
        checkpoint.mark_run_crashed(run_id)
        with _RUN_LOCK:
            if run_id in _RUN_STATE:
                _RUN_STATE[run_id]["status"] = "crashed"


def _start_run(items: list[checkpoint.RunItemRow], run_cfg: checkpoint.RunConfig, run_id: str | None = None) -> str:
    """Creates (or resumes into) a run row and launches the pipeline in a
    background thread. No browser/session bootstrap — see _run_in_background."""
    if run_id is None:
        run_id = checkpoint.create_run(run_cfg, items)
    _init_run_state(run_id, total=len(items))
    thread = threading.Thread(target=_run_in_background, args=(run_id, items, run_cfg), daemon=True)
    thread.start()
    return run_id


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    incomplete = checkpoint.find_incomplete_run()
    return render_template("upload.html", incomplete=incomplete)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        return render_template("upload.html", error="Please choose a CSV or XLSX file.", incomplete=None)

    try:
        report = parse_upload(file.filename, file.read())
    except InputValidationError as e:
        return render_template("upload.html", error=str(e), incomplete=None)

    upload_id = uuid.uuid4().hex
    _PENDING_UPLOADS[upload_id] = {
        "report": report,
        "filename": file.filename,
    }
    brand_count = len({r.brand for r in report.valid})
    return render_template(
        "confirm.html",
        upload_id=upload_id,
        filename=file.filename,
        total_rows=report.total_rows,
        valid_rows=report.valid_rows,
        invalid_rows=report.invalid,
        brand_count=brand_count,
        default_concurrency=config.DEFAULT_CONCURRENCY,
        default_tolerance_abs=config.DEFAULT_TOLERANCE_ABS,
        default_tolerance_pct=config.DEFAULT_TOLERANCE_PCT,
        price_source=config.DEFAULT_PRICE_SOURCE,
    )


@app.route("/start", methods=["POST"])
def start():
    upload_id = request.form.get("upload_id", "")
    pending = _PENDING_UPLOADS.pop(upload_id, None)
    if pending is None:
        return redirect(url_for("index"))

    run_cfg = checkpoint.RunConfig(
        input_filename=pending["filename"],
        price_source=request.form.get("price_source", config.DEFAULT_PRICE_SOURCE),
        tolerance_abs=float(request.form.get("tolerance_abs") or config.DEFAULT_TOLERANCE_ABS),
        tolerance_pct=float(request.form.get("tolerance_pct") or config.DEFAULT_TOLERANCE_PCT),
        concurrency=int(request.form.get("concurrency") or config.DEFAULT_CONCURRENCY),
    )
    items = [
        checkpoint.RunItemRow(asin=r.asin, expected_price=r.expected_price, brand=r.brand)
        for r in pending["report"].valid
    ]

    run_id = _start_run(items, run_cfg)
    return redirect(url_for("progress_view", run_id=run_id))


@app.route("/resume/<run_id>", methods=["POST"])
def resume(run_id: str):
    run = checkpoint.get_run(run_id)
    if run is None:
        return redirect(url_for("index"))
    pending_items = checkpoint.get_pending_and_retryable_items(run_id)
    run_cfg = checkpoint.RunConfig(
        input_filename=run["input_filename"],
        price_source=run["price_source"], tolerance_abs=run["tolerance_abs"],
        tolerance_pct=run["tolerance_pct"], concurrency=run["concurrency"],
    )
    _start_run(pending_items, run_cfg, run_id=run_id)
    return redirect(url_for("progress_view", run_id=run_id))


@app.route("/discard/<run_id>", methods=["POST"])
def discard(run_id: str):
    checkpoint.mark_run_crashed(run_id)  # leaves history intact but out of "incomplete" detection going forward
    return redirect(url_for("index"))


@app.route("/progress/<run_id>")
def progress_view(run_id: str):
    return render_template("progress.html", run_id=run_id)


@app.route("/stream/<run_id>")
def stream(run_id: str):
    def gen():
        last_done = -1
        while True:
            with _RUN_LOCK:
                st = _RUN_STATE.get(run_id)
            if st is None:
                yield "event: error\ndata: unknown run\n\n"
                return
            if st["done"] != last_done or st["status"] != "running":
                last_done = st["done"]
                elapsed = time.time() - st["started_at"]
                rate = st["done"] / elapsed if elapsed > 0 and st["done"] else 0
                remaining = st["total"] - st["done"]
                eta = remaining / rate if rate > 0 else None
                payload = {
                    "done": st["done"], "total": st["total"],
                    "matched": st["matched"], "mismatched": st["mismatched"],
                    "out_of_stock": st["out_of_stock"], "failed": st["failed"],
                    "elapsed_seconds": round(elapsed), "eta_seconds": round(eta) if eta else None,
                    "status": st["status"],
                }
                import json
                yield f"data: {json.dumps(payload)}\n\n"
                if st["status"] != "running":
                    return
            time.sleep(1)

    return Response(gen(), mimetype="text/event-stream")


@app.route("/results/<run_id>")
def results(run_id: str):
    run = checkpoint.get_run(run_id)
    if run is None:
        return redirect(url_for("index"))
    brands = checkpoint.get_brands_with_issues(run_id)
    return render_template("results.html", run=run, brands=brands)


@app.route("/download/<run_id>")
def download(run_id: str):
    run = checkpoint.get_run(run_id)
    if run is None or not run.get("output_path"):
        path = build_report(run_id)
    else:
        path = Path(run["output_path"])
        if not path.exists():
            path = build_report(run_id)
    return send_file(str(path), as_attachment=True, download_name=path.name)


@app.route("/download/<run_id>/brand/<path:brand>")
def download_brand(run_id: str, brand: str):
    """A single brand's issue list as a small standalone workbook, ready to
    attach directly to the email that goes to that brand's seller."""
    run = checkpoint.get_run(run_id)
    if run is None:
        return redirect(url_for("index"))
    path = build_brand_report(run_id, brand)
    return send_file(str(path), as_attachment=True, download_name=path.name)


@app.route("/history")
def history():
    runs = checkpoint.list_runs()
    return render_template("history.html", runs=runs)


def main() -> None:
    from price_verifier.storage.db import init_db
    init_db(config.DB_PATH)
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5001/")).start()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)


if __name__ == "__main__":
    main()
