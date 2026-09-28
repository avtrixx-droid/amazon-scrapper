"""
templates_inline.py — Jinja templates as Python string constants, loaded via
a DictLoader instead of Flask's default FileSystemLoader.

Why: this repo's own gui.py avoids file-based Flask templates entirely
(it uses render_template_string for everything) specifically because a
templates/ folder is one more thing that can go missing or resolve to the
wrong path once PyInstaller freezes the app — the proven amazon_scraper_windows.spec
carries no `datas` entry for a templates directory at all. Rather than
introduce a new, untested mechanism (bundling price_verifier/templates/ as
PyInstaller data files, then computing a frozen-aware template_folder), this
reuses the same de-risked shape: templates ship as ordinary Python source,
so they're just part of the bundled code with no separate data-file step.

Route code is unaffected — app.py still calls render_template("upload.html", ...);
only app.jinja_loader is swapped for a DictLoader built from TEMPLATES below,
so name-based lookups (including {% extends "base.html" %}) resolve exactly
as they did against the filesystem.

Content here must stay byte-identical to what used to live in
price_verifier/templates/*.html when this was converted — edit here, not a
.html file, since the .html copies no longer exist.
"""

from __future__ import annotations

BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{% block title %}Amazon Price Verification Tool{% endblock %}</title>
<style>
  :root { --navy: #003366; --green: #1a7f37; --red: #c0392b; --amber: #b7791f; --grey: #6b7280; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a1a; }
  header { background: var(--navy); color: #fff; padding: 16px 24px; }
  header h1 { margin: 0; font-size: 18px; }
  header nav { margin-top: 6px; }
  header nav a { color: #cfe0f5; text-decoration: none; margin-right: 16px; font-size: 13px; }
  header nav a:hover { text-decoration: underline; }
  main { max-width: 820px; margin: 32px auto; background: #fff; border-radius: 8px; padding: 28px 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  h2 { margin-top: 0; }
  .error { background: #fdecea; color: var(--red); border: 1px solid #f5c6cb; padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }
  .notice { background: #fff8e1; color: var(--amber); border: 1px solid #ffe08a; padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }
  label { display: block; font-weight: 600; margin-top: 16px; margin-bottom: 4px; font-size: 14px; }
  input[type=text], input[type=number], input[type=file], select { width: 100%; padding: 8px 10px; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 14px; }
  .row { display: flex; gap: 16px; }
  .row > div { flex: 1; }
  button, .btn { background: var(--navy); color: #fff; border: none; padding: 10px 20px; border-radius: 6px; font-size: 14px; cursor: pointer; margin-top: 20px; text-decoration: none; display: inline-block; }
  button:hover, .btn:hover { background: #024a8c; }
  .btn-secondary { background: #6b7280; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }
  th { background: #f0f2f5; }
  .stat-row { display: flex; gap: 12px; margin: 20px 0; flex-wrap: wrap; }
  .stat { flex: 1; min-width: 110px; background: #f0f2f5; border-radius: 6px; padding: 14px; text-align: center; }
  .stat .n { font-size: 26px; font-weight: 700; }
  .stat.matched .n { color: var(--green); }
  .stat.mismatched .n { color: var(--amber); }
  .stat.oos .n { color: var(--grey); }
  .stat.failed .n { color: var(--red); }
  .muted { color: #6b7280; font-size: 13px; }
  progress { width: 100%; height: 18px; }
</style>
</head>
<body>
<header>
  <h1>Amazon Price Verification Tool</h1>
  <nav>
    <a href="{{ url_for('index') }}">Upload</a>
    <a href="{{ url_for('history') }}">History</a>
  </nav>
</header>
<main>
{% block content %}{% endblock %}
</main>
</body>
</html>
"""

UPLOAD_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Upload a batch</h2>

{% if error %}<div class="error">{{ error }}</div>{% endif %}

{% if incomplete %}
<div class="notice">
  An earlier run (<strong>{{ incomplete.run_id }}</strong>, started {{ incomplete.started_at }}) did not finish —
  {{ incomplete.total_rows }} rows. Resume it, or discard it and start fresh.
  <form method="post" action="{{ url_for('resume', run_id=incomplete.run_id) }}" style="display:inline">
    <button type="submit">Resume this run</button>
  </form>
  <form method="post" action="{{ url_for('discard', run_id=incomplete.run_id) }}" style="display:inline">
    <button type="submit" class="btn-secondary">Discard</button>
  </form>
</div>
{% endif %}

<p class="muted">Upload a CSV or XLSX with <code>asin</code>, <code>expected_price</code>, and <code>brand</code> columns. The output is grouped into one sheet per brand — only rows with a price mismatch, an out-of-stock/unavailable listing, or a not-found ASIN are included, ready to forward to that brand's seller.</p>

<form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
  <label for="file">File</label>
  <input type="file" id="file" name="file" accept=".csv,.xlsx" required>
  <button type="submit">Validate &amp; continue</button>
</form>
{% endblock %}
"""

CONFIRM_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Confirm run — {{ filename }}</h2>

<div class="stat-row">
  <div class="stat"><div class="n">{{ total_rows }}</div><div class="muted">rows parsed</div></div>
  <div class="stat matched"><div class="n">{{ valid_rows }}</div><div class="muted">valid</div></div>
  <div class="stat failed"><div class="n">{{ invalid_rows|length }}</div><div class="muted">invalid (skipped)</div></div>
  <div class="stat"><div class="n">{{ brand_count }}</div><div class="muted">brands</div></div>
</div>

{% if invalid_rows %}
<p class="muted">Invalid rows will not be scraped. Fix and re-upload if these matter:</p>
<table>
  <tr><th>Row</th><th>ASIN</th><th>Reason</th></tr>
  {% for r in invalid_rows[:25] %}
  <tr><td>{{ r.row_number }}</td><td>{{ r.asin }}</td><td>{{ r.reason }}</td></tr>
  {% endfor %}
</table>
{% if invalid_rows|length > 25 %}<p class="muted">...and {{ invalid_rows|length - 25 }} more.</p>{% endif %}
{% endif %}

<form method="post" action="{{ url_for('start') }}">
  <input type="hidden" name="upload_id" value="{{ upload_id }}">

  <p class="muted">Price is the same at every pincode for this catalog, so no delivery location needs to be set — this fetches straight away.</p>

  <div class="row">
    <div>
      <label for="price_source">Price source</label>
      <select id="price_source" name="price_source">
        <option value="buybox" {% if price_source == 'buybox' %}selected{% endif %}>Buy Box price</option>
        <option value="lowest" {% if price_source == 'lowest' %}selected{% endif %} disabled>Lowest across sellers (not yet implemented)</option>
      </select>
    </div>
    <div>
      <label for="concurrency">Concurrent requests</label>
      <input type="number" id="concurrency" name="concurrency" min="1" max="40" value="{{ default_concurrency }}">
    </div>
  </div>

  <div class="row">
    <div>
      <label for="tolerance_abs">Flag if price differs by more than (₹)</label>
      <input type="number" step="0.01" id="tolerance_abs" name="tolerance_abs" value="{{ default_tolerance_abs }}">
    </div>
    <div>
      <label for="tolerance_pct">Also flag if it differs by more than (%)</label>
      <input type="number" step="0.01" id="tolerance_pct" name="tolerance_pct" value="{{ default_tolerance_pct }}">
    </div>
  </div>

  <button type="submit" {% if valid_rows == 0 %}disabled{% endif %}>Start run ({{ valid_rows }} ASINs, {{ brand_count }} brands)</button>
</form>
{% endblock %}
"""

PROGRESS_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Run in progress</h2>
<p class="muted">Run ID: {{ run_id }}</p>

<div>
  <progress id="bar" value="0" max="1"></progress>
  <p id="count">0 / 0</p>
</div>

<div class="stat-row">
  <div class="stat matched"><div class="n" id="matched">0</div><div class="muted">Matched</div></div>
  <div class="stat mismatched"><div class="n" id="mismatched">0</div><div class="muted">Mismatched</div></div>
  <div class="stat oos"><div class="n" id="oos">0</div><div class="muted">Out of Stock</div></div>
  <div class="stat failed"><div class="n" id="failed">0</div><div class="muted">Failed</div></div>
</div>

<p class="muted">Elapsed: <span id="elapsed">0s</span> &nbsp;|&nbsp; ETA: <span id="eta">—</span></p>
<p id="status-line" class="muted"></p>

<script>
const runId = {{ run_id | tojson }};
const evtSource = new EventSource(`/stream/${runId}`);
function fmt(s) {
  if (s === null || s === undefined) return "—";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}
evtSource.onmessage = function(e) {
  const d = JSON.parse(e.data);
  document.getElementById("bar").value = d.done;
  document.getElementById("bar").max = d.total || 1;
  document.getElementById("count").textContent = `${d.done} / ${d.total}`;
  document.getElementById("matched").textContent = d.matched;
  document.getElementById("mismatched").textContent = d.mismatched;
  document.getElementById("oos").textContent = d.out_of_stock;
  document.getElementById("failed").textContent = d.failed;
  document.getElementById("elapsed").textContent = fmt(d.elapsed_seconds);
  document.getElementById("eta").textContent = fmt(d.eta_seconds);
  if (d.status !== "running") {
    document.getElementById("status-line").textContent =
      d.status === "completed" ? "Run complete." : `Run ended: ${d.status}`;
    evtSource.close();
    if (d.status === "completed") {
      window.location.href = `/results/${runId}`;
    }
  }
};
evtSource.onerror = function() {
  document.getElementById("status-line").textContent = "Connection to progress stream lost — the run may still be going. Refresh to check.";
};
</script>
{% endblock %}
"""

RESULTS_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Run complete</h2>
<p class="muted">Run ID: {{ run.run_id }} &nbsp;|&nbsp; File: {{ run.input_filename }}</p>

<div class="stat-row">
  <div class="stat"><div class="n">{{ run.total_rows }}</div><div class="muted">Total</div></div>
  <div class="stat matched"><div class="n">{{ run.matched }}</div><div class="muted">Matched</div></div>
  <div class="stat mismatched"><div class="n">{{ run.mismatched }}</div><div class="muted">Mismatched</div></div>
  <div class="stat oos"><div class="n">{{ run.out_of_stock }}</div><div class="muted">Out of Stock</div></div>
  <div class="stat failed"><div class="n">{{ run.failed }}</div><div class="muted">Failed</div></div>
</div>

<a class="btn" href="{{ url_for('download', run_id=run.run_id) }}">Download full workbook (all brands, .xlsx)</a>
<a class="btn btn-secondary" href="{{ url_for('index') }}">Start another run</a>

{% if brands %}
<h2 style="margin-top:32px;">Per-brand issue lists</h2>
<p class="muted">Each of these is a single sheet with only that brand's mismatches / out-of-stock / not-found rows — attach directly to the email for that seller.</p>
<table>
  <tr><th>Brand</th><th></th></tr>
  {% for brand in brands %}
  <tr>
    <td>{{ brand }}</td>
    <td><a href="{{ url_for('download_brand', run_id=run.run_id, brand=brand) }}">Download</a></td>
  </tr>
  {% endfor %}
</table>
{% else %}
<p class="muted" style="margin-top:24px;">No brand had any issues — nothing to send.</p>
{% endif %}
{% endblock %}
"""

HISTORY_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Run history</h2>

{% if not runs %}
<p class="muted">No runs yet.</p>
{% else %}
<table>
  <tr>
    <th>Run ID</th><th>Started</th><th>Status</th><th>Total</th>
    <th>Matched</th><th>Mismatched</th><th>OOS</th><th>Failed</th><th></th>
  </tr>
  {% for r in runs %}
  <tr>
    <td>{{ r.run_id }}</td>
    <td>{{ r.started_at }}</td>
    <td>{{ r.status }}</td>
    <td>{{ r.total_rows }}</td>
    <td>{{ r.matched }}</td>
    <td>{{ r.mismatched }}</td>
    <td>{{ r.out_of_stock }}</td>
    <td>{{ r.failed }}</td>
    <td>
      {% if r.status == 'completed' %}
      <a href="{{ url_for('download', run_id=r.run_id) }}">Download</a>
      {% elif r.status == 'running' %}
      <a href="{{ url_for('progress_view', run_id=r.run_id) }}">View progress</a>
      {% else %}
      <form method="post" action="{{ url_for('resume', run_id=r.run_id) }}" style="display:inline">
        <button type="submit" style="margin:0;padding:4px 10px;font-size:12px;">Resume</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
{% endif %}
{% endblock %}
"""

TEMPLATES = {
    "base.html": BASE_HTML,
    "upload.html": UPLOAD_HTML,
    "confirm.html": CONFIRM_HTML,
    "progress.html": PROGRESS_HTML,
    "results.html": RESULTS_HTML,
    "history.html": HISTORY_HTML,
}
