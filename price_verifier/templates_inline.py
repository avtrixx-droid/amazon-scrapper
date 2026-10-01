"""
templates_inline.py — Jinja templates as Python string constants, loaded via
a DictLoader instead of Flask's default FileSystemLoader.

Why: this repo's own gui.py avoids file-based Flask templates entirely
(it uses render_template_string for everything) because a templates/ folder
is one more thing that can go missing or resolve to the wrong path once
PyInstaller freezes the app. Templates ship as ordinary Python source here,
so they're part of the bundled code with no separate data-file step.
app.py calls render_template("upload.html", ...) as usual; only
app.jinja_loader is swapped for a DictLoader built from TEMPLATES below.
"""

from __future__ import annotations

BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{% block title %}Amazon Price Verification Tool{% endblock %}</title>
<style>
  :root { --navy: #003366; --green: #1a7f37; --red: #c0392b; --amber: #b7791f; --grey: #6b7280; --line: #e5e7eb; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a1a; }
  header { background: var(--navy); color: #fff; padding: 16px 24px; }
  header h1 { margin: 0; font-size: 18px; }
  header nav { margin-top: 6px; }
  header nav a { color: #cfe0f5; text-decoration: none; margin-right: 16px; font-size: 13px; }
  header nav a:hover { text-decoration: underline; }
  main { max-width: 960px; margin: 32px auto; background: #fff; border-radius: 8px; padding: 28px 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  h2 { margin-top: 0; }
  h3 { margin: 28px 0 8px; font-size: 15px; }
  .error { background: #fdecea; color: var(--red); border: 1px solid #f5c6cb; padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }
  .notice { background: #fff8e1; color: #7a5200; border: 1px solid #ffe08a; padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }
  .ok-box { background: #e9f7ef; color: #145a32; border: 1px solid #b7e1c7; padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }
  label { display: block; font-weight: 600; margin-top: 16px; margin-bottom: 4px; font-size: 14px; }
  label.inline { display: inline; font-weight: normal; margin: 0 0 0 6px; }
  input[type=text], input[type=number], input[type=file], select { width: 100%; padding: 8px 10px; border: 1px solid #d0d5dd; border-radius: 6px; font-size: 14px; background: #fff; }
  .row { display: flex; gap: 16px; }
  .row > div { flex: 1; }
  button, .btn { background: var(--navy); color: #fff; border: none; padding: 10px 20px; border-radius: 6px; font-size: 14px; cursor: pointer; margin-top: 20px; text-decoration: none; display: inline-block; }
  button:hover, .btn:hover { background: #024a8c; }
  button:disabled { background: #9aa4b2; cursor: not-allowed; }
  .btn-secondary { background: #6b7280; }
  .btn-warn { background: var(--amber); }
  .btn-small { padding: 4px 10px; font-size: 12px; margin: 0; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; white-space: nowrap; }
  th { background: #f0f2f5; }
  td.sel, th.sel { background: #e7f0fb; }
  th.sel { color: var(--navy); }
  .table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }
  .table-wrap table { margin-top: 0; }
  .stat-row { display: flex; gap: 12px; margin: 20px 0; flex-wrap: wrap; }
  .stat { flex: 1; min-width: 110px; background: #f0f2f5; border-radius: 6px; padding: 14px; text-align: center; }
  .stat .n { font-size: 26px; font-weight: 700; }
  .stat.matched .n { color: var(--green); }
  .stat.mismatched .n { color: var(--amber); }
  .stat.oos .n { color: var(--grey); }
  .stat.failed .n { color: var(--red); }
  .muted { color: #6b7280; font-size: 13px; }
  .req { color: var(--red); }
  .badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; margin-left: 8px; vertical-align: middle; }
  .badge.ok { background: #e9f7ef; color: #145a32; }
  .badge.check { background: #fff8e1; color: #7a5200; }
  .map-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px 20px; }
  .phase { font-weight: 600; margin: 4px 0 12px; }
  progress { width: 100%; height: 18px; }
  .inline-form { display: inline; }
  header nav .licensed { color: #cfe0f5; font-size: 12px; float: right; }
  .mono { font-family: Consolas, Menlo, monospace; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>Amazon Price Verification Tool</h1>
  <nav>
    <a href="{{ url_for('index') }}">Upload</a>
    <a href="{{ url_for('history') }}">History</a>
    {% if license_status and not license_status.disabled %}<a href="{{ url_for('activate') }}">License</a>{% endif %}
    {% if license_status and license_status.customer %}<span class="licensed">Licensed to {{ license_status.customer }}</span>{% endif %}
  </nav>
</header>
<main>
{% if license_status and license_status.status == 'offline' %}
  <div class="notice">{{ license_status.message }}</div>
{% endif %}
{% for category, msg in get_flashed_messages(with_categories=true) %}
  <div class="{{ 'ok-box' if category == 'ok' else ('notice' if category == 'notice' else 'error') }}">{{ msg }}</div>
{% endfor %}
{% block content %}{% endblock %}
</main>
<script>
// A POST button is one action: a double-click must not submit it twice.
document.addEventListener('submit', function (e) {
  e.target.querySelectorAll('button[type=submit], input[type=submit]').forEach(function (b) {
    setTimeout(function () { b.disabled = true; }, 0);
  });
});
</script>
</body>
</html>
"""

UPLOAD_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Upload a batch</h2>

{% if error %}<div class="error">{{ error }}</div>{% endif %}

{% if incomplete %}
<div class="notice">
  An earlier run (<strong>{{ incomplete.run_id }}</strong>, started {{ incomplete.started_at[:16].replace('T', ' ') }}) did not finish —
  {{ incomplete.total_rows }} rows. Resume it, or discard it and start fresh.
  <br>
  <form method="post" action="{{ url_for('resume', run_id=incomplete.run_id) }}" class="inline-form">
    <button type="submit">Resume this run</button>
  </form>
  <form method="post" action="{{ url_for('discard', run_id=incomplete.run_id) }}" class="inline-form">
    <button type="submit" class="btn-secondary">Discard</button>
  </form>
</div>
{% endif %}

<p class="muted">Upload any CSV or Excel file that lists your ASINs (or Amazon product links) and the price you expect.
A brand column is optional — without one, the brand shown on Amazon is used. You'll confirm which columns to use on the next screen.</p>

<form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
  <label for="file">File</label>
  <input type="file" id="file" name="file" accept=".csv,.xlsx,.xlsm,.txt" required>
  <button type="submit">Upload &amp; detect columns</button>
</form>
{% endblock %}
"""

MAPPING_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Check your columns — {{ filename }}</h2>
<p class="muted">Found <strong>{{ row_count }}</strong> data rows{% if header_row %} (column headings on row {{ header_row }}){% else %} (no heading row — showing column letters){% endif %}.
We've matched the columns below — change any that look wrong, then continue.</p>

{% if sheets|length > 1 %}
<form method="get" action="{{ url_for('map_columns', upload_id=upload_id) }}">
  <label for="sheet">Sheet</label>
  <select id="sheet" name="sheet" onchange="this.form.submit()">
    {% for s in sheets %}<option value="{{ s }}" {% if s == sheet %}selected{% endif %}>{{ s }}</option>{% endfor %}
  </select>
</form>
{% endif %}

{% if error %}<div class="error">{{ error }}</div>{% endif %}
{% for w in warnings %}<div class="notice">{{ w }}</div>{% endfor %}

<form method="post" action="{{ url_for('map_columns', upload_id=upload_id, sheet=sheet) }}">
  <div class="map-grid">
  {% for f in fields %}
    <div>
      <label for="col_{{ f.key }}">{{ f.label }}{% if f.required %} <span class="req">*</span>{% endif %}
        {% if f.selected is not none %}
          {% if f.confidence >= 0.7 %}<span class="badge ok">Detected</span>{% else %}<span class="badge check">Please check</span>{% endif %}
        {% endif %}
      </label>
      <select id="col_{{ f.key }}" name="col_{{ f.key }}" class="colpick" {% if f.required %}required{% endif %}>
        {% if f.required %}<option value="" {% if f.selected is none %}selected{% endif %}>— choose a column —</option>
        {% else %}<option value="" {% if f.selected is none %}selected{% endif %}>{{ f.none_label }}</option>{% endif %}
        {% for o in f.options %}
          <option value="{{ o.index }}" {% if o.index == f.selected %}selected{% endif %}>{{ o.label }}</option>
        {% endfor %}
      </select>
      <p class="muted">{{ f.help }}</p>
    </div>
  {% endfor %}
  </div>

  <h3>Preview (first rows of your file)</h3>
  <div class="table-wrap"><table id="preview">
    <tr>{% for h in headers %}<th data-col="{{ loop.index0 }}">{{ h }}</th>{% endfor %}</tr>
    {% for row in preview %}<tr>{% for c in row %}<td data-col="{{ loop.index0 }}">{{ c }}</td>{% endfor %}</tr>{% endfor %}
  </table></div>

  <button type="submit">Use these columns</button>
</form>

<script>
function highlight() {
  const picked = new Set();
  document.querySelectorAll('select.colpick').forEach(s => { if (s.value !== '') picked.add(s.value); });
  document.querySelectorAll('#preview [data-col]').forEach(c => c.classList.toggle('sel', picked.has(c.dataset.col)));
}
document.querySelectorAll('select.colpick').forEach(s => s.addEventListener('change', highlight));
highlight();
</script>
{% endblock %}
"""

CONFIRM_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Ready to check — {{ filename }}</h2>
{% if error %}<div class="error">{{ error }}</div>{% endif %}

<p class="muted">Using: <strong>ASIN</strong> ← {{ mapping_desc.asin }} · <strong>Expected price</strong> ← {{ mapping_desc.expected_price }} ·
<strong>Brand</strong> ← {{ mapping_desc.brand }}
&nbsp; <a href="{{ url_for('map_columns', upload_id=upload_id, sheet=sheet) }}">Change columns</a></p>

<div class="stat-row">
  <div class="stat"><div class="n">{{ total_rows }}</div><div class="muted">rows read</div></div>
  <div class="stat matched"><div class="n">{{ valid_rows }}</div><div class="muted">ready to check</div></div>
  <div class="stat failed"><div class="n">{{ invalid_rows|length }}</div><div class="muted">skipped (see below)</div></div>
  <div class="stat"><div class="n">{{ brand_count }}</div><div class="muted">brands{% if brand_from_amazon %} (from Amazon){% endif %}</div></div>
</div>

{% if invalid_rows %}
<p class="muted">These rows will be skipped. Fix and re-upload if they matter:</p>
<div class="table-wrap"><table>
  <tr><th>Row</th><th>Value</th><th>Reason</th></tr>
  {% for r in invalid_rows[:25] %}
  <tr><td>{{ r.row_number }}</td><td>{{ r.asin }}</td><td>{{ r.reason }}</td></tr>
  {% endfor %}
</table></div>
{% if invalid_rows|length > 25 %}<p class="muted">...and {{ invalid_rows|length - 25 }} more.</p>{% endif %}
{% endif %}

<form method="post" action="{{ url_for('start') }}">
  <input type="hidden" name="upload_id" value="{{ upload_id }}">

  <div class="row">
    <div>
      <label for="tolerance_abs">Flag if price differs by more than (₹)</label>
      <input type="number" step="0.01" min="0" id="tolerance_abs" name="tolerance_abs" value="{{ default_tolerance_abs }}">
    </div>
    <div>
      <label for="tolerance_pct">…but ignore differences up to this % of the expected price (0 = off)</label>
      <input type="number" step="0.01" min="0" id="tolerance_pct" name="tolerance_pct" value="{{ default_tolerance_pct }}">
    </div>
    <div>
      <label for="concurrency">Max parallel requests</label>
      <input type="number" id="concurrency" name="concurrency" min="1" max="{{ max_concurrency }}" value="{{ default_concurrency }}">
    </div>
  </div>

  <p>
    <input type="checkbox" id="use_browser" name="use_browser" value="1" checked>
    <label class="inline" for="use_browser">Double-check anything unclear in Google Chrome (recommended — needs Chrome installed)</label>
  </p>

  <button type="submit" {% if valid_rows == 0 %}disabled{% endif %}>Start check ({{ valid_rows }} ASINs)</button>
</form>
{% endblock %}
"""

PROGRESS_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Checking prices…</h2>
<p class="muted">Run ID: {{ run_id }}</p>

<p class="phase" id="phase">Starting…</p>
<div>
  <progress id="bar" value="0" max="1"></progress>
  <p id="count">0 / 0</p>
</div>

<div class="stat-row">
  <div class="stat matched"><div class="n" id="matched">0</div><div class="muted">Price OK</div></div>
  <div class="stat mismatched"><div class="n" id="mismatched">0</div><div class="muted">Price mismatch</div></div>
  <div class="stat oos"><div class="n" id="oos">0</div><div class="muted">Listing issue</div></div>
  <div class="stat failed"><div class="n" id="failed">0</div><div class="muted">Could not verify</div></div>
</div>

<p class="muted">Elapsed: <span id="elapsed">0s</span> &nbsp;|&nbsp; Remaining: <span id="eta">—</span>
&nbsp;|&nbsp; Speed: <span id="rate">—</span></p>
<p id="status-line" class="muted"></p>

<button type="button" class="btn-secondary" id="cancel-btn">Pause run</button>

<script>
const runId = {{ run_id | tojson }};
const evtSource = new EventSource(`/stream/${runId}`);
function fmt(s) {
  if (s === null || s === undefined) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
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
  document.getElementById("rate").textContent = d.rate_label || "—";
  document.getElementById("phase").textContent = d.phase_label || "";
  if (d.status !== "running") {
    evtSource.close();
    document.getElementById("cancel-btn").style.display = "none";
    if (d.status === "completed") {
      document.getElementById("status-line").textContent = "Done — opening results…";
      window.location.href = `/results/${runId}`;
    } else if (d.status === "paused") {
      document.getElementById("status-line").innerHTML = 'Paused. You can resume it from the <a href="/">Upload</a> page.';
    } else {
      document.getElementById("status-line").textContent = `Run ended: ${d.status}. You can resume it from the Upload page.`;
    }
  }
};
evtSource.onerror = function() {
  document.getElementById("status-line").textContent = "Lost connection to the progress feed — the check may still be running. Refresh this page.";
};
document.getElementById("cancel-btn").addEventListener("click", async () => {
  if (!confirm("Pause this run? Finished rows are saved — you can resume later.")) return;
  document.getElementById("cancel-btn").disabled = true;
  document.getElementById("phase").textContent = "Pausing after the requests already in flight…";
  await fetch(`/cancel/${runId}`, { method: "POST" });
});
</script>
{% endblock %}
"""

RESULTS_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Check complete</h2>
<p class="muted">Run ID: {{ run.run_id }} &nbsp;|&nbsp; File: {{ run.input_filename }}{% if duration %} &nbsp;|&nbsp; Took {{ duration }}{% endif %}</p>

<div class="stat-row">
  <div class="stat"><div class="n">{{ run.total_rows }}</div><div class="muted">ASINs</div></div>
  <div class="stat matched"><div class="n">{{ run.matched }}</div><div class="muted">Price OK</div></div>
  <div class="stat mismatched"><div class="n">{{ run.mismatched }}</div><div class="muted">Price mismatch</div></div>
  <div class="stat oos"><div class="n">{{ run.out_of_stock }}</div><div class="muted">Listing issue</div></div>
  <div class="stat failed"><div class="n">{{ run.failed }}</div><div class="muted">Could not verify</div></div>
</div>

{% if run.failed %}
<div class="notice">
  <strong>{{ run.failed }}</strong> ASIN{{ 's' if run.failed != 1 }} could not be verified even after automatic retries
  (usually Amazon briefly limiting requests). Retrying in a few minutes almost always clears them.
  <form method="post" action="{{ url_for('retry', run_id=run.run_id) }}">
    <button type="submit" class="btn-warn">Retry {{ run.failed }} unverified ASIN{{ 's' if run.failed != 1 }}</button>
  </form>
</div>
{% else %}
<div class="ok-box">Every ASIN was verified.</div>
{% endif %}

<a class="btn" href="{{ url_for('download', run_id=run.run_id) }}">Download full report (all brands, .xlsx)</a>
<a class="btn btn-secondary" href="{{ url_for('index') }}">Start another check</a>

{% if brands %}
<h2 style="margin-top:32px;">Per-brand issue lists</h2>
<p class="muted">Each file has only that brand's price mismatches and listing issues — ready to attach to the email for that seller.</p>
<table>
  <tr><th>Brand</th><th>Issues</th><th></th></tr>
  {% for b in brands %}
  <tr>
    <td>{{ b.brand }}</td>
    <td>{{ b.issues }}</td>
    <td><a href="{{ url_for('download_brand', run_id=run.run_id, brand=b.brand) }}">Download</a></td>
  </tr>
  {% endfor %}
</table>
{% else %}
<p class="muted" style="margin-top:24px;">No brand had any price or listing issues — nothing to send.</p>
{% endif %}
{% endblock %}
"""

HISTORY_HTML = """{% extends "base.html" %}
{% block content %}
<h2>Run history</h2>

{% if not runs %}
<p class="muted">No runs yet.</p>
{% else %}
<div class="table-wrap"><table>
  <tr>
    <th>Started</th><th>File</th><th>Status</th><th>ASINs</th>
    <th>OK</th><th>Mismatch</th><th>Listing issue</th><th>Unverified</th><th></th>
  </tr>
  {% for r in runs %}
  <tr>
    <td>{{ r.started_at[:16].replace('T', ' ') }}</td>
    <td>{{ r.input_filename }}</td>
    <td>{{ r.status }}</td>
    <td>{{ r.total_rows }}</td>
    <td>{{ r.matched }}</td>
    <td>{{ r.mismatched }}</td>
    <td>{{ r.out_of_stock }}</td>
    <td>{{ r.failed }}</td>
    <td>
      {% if r.status == 'completed' %}
        <a href="{{ url_for('results', run_id=r.run_id) }}">Open</a> ·
        <a href="{{ url_for('download', run_id=r.run_id) }}">Download</a>
        {% if r.failed %}
        <form method="post" action="{{ url_for('retry', run_id=r.run_id) }}" class="inline-form">
          · <button type="submit" class="btn-small btn-warn">Retry {{ r.failed }}</button>
        </form>
        {% endif %}
      {% elif r.run_id in active_runs %}
        <a href="{{ url_for('progress_view', run_id=r.run_id) }}">View progress</a>
      {% else %}
        <form method="post" action="{{ url_for('resume', run_id=r.run_id) }}" class="inline-form">
          <button type="submit" class="btn-small">Resume</button>
        </form>
        · <a href="{{ url_for('download', run_id=r.run_id) }}">Download what's checked so far</a>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table></div>
{% endif %}
{% endblock %}
"""

ACTIVATE_HTML = """{% extends "base.html" %}
{% block content %}
{% if already_valid %}
  <h2>License</h2>
  <div class="ok-box">This computer is licensed{% if status.customer %} to <strong>{{ status.customer }}</strong>{% endif %}
  {% if status.expires_at %} until {{ status.expires_at[:10] }}{% endif %}.</div>
  <p class="muted">Key in use: <span class="mono">{{ current_key }}</span></p>
  <p class="muted">To switch to a different key, enter it below.</p>
{% else %}
  <h2>Activate the Price Verification Tool</h2>
  {% if status.status == 'expired' %}
    <div class="error">Your license has expired{% if status.expires_at %} on {{ status.expires_at[:10] }}{% endif %}. Enter a renewed key, or contact support.</div>
  {% elif status.status == 'revoked' %}
    <div class="error">This license has been revoked. Enter a new key, or contact support.</div>
  {% elif status.status == 'product_not_licensed' %}
    <div class="error">This license key doesn't include the Price Verification Tool. Enter a key that does, or contact support to add it.</div>
  {% elif status.message and status.reason != 'no_license' %}
    <div class="error">{{ status.message }}</div>
  {% endif %}
  <p>Enter the license key you received. It is tied to this computer — keep it confidential.</p>
{% endif %}

{% if error %}<div class="error">{{ error }}</div>{% endif %}

<form method="post" action="{{ url_for('activate') }}">
  <label for="key">License key</label>
  <input type="text" id="key" name="key" placeholder="AMZ-XXXX-XXXX-XXXX-XXXX" autocomplete="off" spellcheck="false"
         style="text-transform: uppercase; font-family: Consolas, Menlo, monospace;" required>
  <button type="submit">Activate</button>
</form>

<p class="muted" style="margin-top: 28px;">Needs an internet connection. Computer ID (for support):
<span class="mono">{{ machine_id }}</span></p>
{% endblock %}
"""

TEMPLATES = {
    "base.html": BASE_HTML,
    "upload.html": UPLOAD_HTML,
    "mapping.html": MAPPING_HTML,
    "confirm.html": CONFIRM_HTML,
    "progress.html": PROGRESS_HTML,
    "results.html": RESULTS_HTML,
    "history.html": HISTORY_HTML,
    "activate.html": ACTIVATE_HTML,
}
