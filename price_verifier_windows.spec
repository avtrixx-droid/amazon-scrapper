# price_verifier_windows.spec
# PyInstaller spec for building PriceVerificationTool.exe on Windows.
#
# Run with:  pyinstaller price_verifier_windows.spec --clean --noconfirm
# Output:    dist\PriceVerificationTool.exe
#
# Separate from amazon_scraper_windows.spec on purpose — price_verifier/ is
# an independently-architected tool (see CLAUDE.md's price_verifier/ note)
# with a different, much leaner dependency set. It deliberately does NOT
# bundle selenium/undetected-chromedriver: the default run flow needs no
# browser at all (see price_verifier/README.md — pincode doesn't matter for
# this catalog), and packaging a real Chrome+chromedriver pair for the
# unused session_bootstrap.py fallback is out of scope until that path is
# actually needed.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# ── Collect certifi's CA bundle — httpx needs this at runtime for TLS ──────
# verification; without it, every request fails with SSLError inside the
# frozen build even though it works fine from a normal `pip install` venv,
# because certifi ships its cacert.pem as package data, not code, and
# static import analysis alone won't bundle it.
try:
    import certifi
    certifi_datas = [(certifi.where(), "certifi")]
except ImportError:
    certifi_datas = []

# ── selectolax ships a compiled (Cython) extension — collect_all pulls in
# its binary + any package data so PyInstaller's import scanner (which only
# sees pure-Python imports reliably) doesn't miss it. ──────────────────────
selectolax_datas, selectolax_binaries, selectolax_hidden = collect_all("selectolax")

all_datas = certifi_datas + selectolax_datas
all_binaries = selectolax_binaries

a = Analysis(
    ["price_verifier/app.py"],
    pathex=["."],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=[
        # ── Flask / Werkzeug / Jinja2 (templates are inline Python source —
        #    see price_verifier/templates_inline.py — so no templates/ datas
        #    entry is needed here, unlike a typical Flask PyInstaller spec) ──
        "flask",
        "flask.json",
        "flask.logging",
        "flask.helpers",
        "flask.wrappers",
        "flask.signals",
        "flask.globals",
        "werkzeug",
        "werkzeug.serving",
        "werkzeug.exceptions",
        "werkzeug.routing",
        "werkzeug.routing.rules",
        "werkzeug.routing.map",
        "werkzeug.utils",
        "werkzeug.datastructures",
        "werkzeug.http",
        "werkzeug.local",
        "werkzeug.sansio",
        "jinja2",
        "jinja2.ext",
        "jinja2.defaults",
        "jinja2.loaders",
        "click",
        "itsdangerous",
        "blinker",
        # ── httpx + its transport stack ──
        "httpx",
        "httpx._transports",
        "httpx._transports.default",
        "httpcore",
        "httpcore._async",
        "httpcore._sync",
        "h11",
        "h2",
        "hpack",
        "hyperframe",
        "certifi",
        "idna",
        "sniffio",
        "anyio",
        "anyio._backends",
        "anyio._backends._asyncio",
        # ── openpyxl ──
        "openpyxl",
        "openpyxl.styles",
        "openpyxl.styles.alignment",
        "openpyxl.styles.fonts",
        "openpyxl.styles.fills",
        "openpyxl.utils",
        "openpyxl.utils.cell",
        "openpyxl.writer",
        "openpyxl.reader",
        "openpyxl.workbook",
        "openpyxl.worksheet",
        "et_xmlfile",
        # ── selectolax ──
        *selectolax_hidden,
        "selectolax",
        "selectolax.parser",
        # ── Standard lib helpers ──
        "sqlite3",
        "asyncio",
        "webbrowser",
        "logging.handlers",
        "ctypes",
        # ── Local package (submodules PyInstaller's static scan can miss
        #    behind dynamic/conditional imports, e.g. session_bootstrap's
        #    guarded `import undetected_chromedriver`) ──
        "price_verifier",
        "price_verifier.config",
        "price_verifier.app",
        "price_verifier.templates_inline",
        "price_verifier.fetcher",
        "price_verifier.fetcher.models",
        "price_verifier.fetcher.parser",
        "price_verifier.fetcher.http_client",
        "price_verifier.fetcher.session_bootstrap",
        "price_verifier.pipeline",
        "price_verifier.pipeline.runner",
        "price_verifier.pipeline.compare",
        "price_verifier.pipeline.retry",
        "price_verifier.pipeline.circuit_breaker",
        "price_verifier.pipeline.worker_pool",
        "price_verifier.storage",
        "price_verifier.storage.db",
        "price_verifier.storage.checkpoint",
        "price_verifier.excel",
        "price_verifier.excel.report",
        "price_verifier.ingest",
        "price_verifier.ingest.input_parser",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Session bootstrap (fetcher/session_bootstrap.py) guards its own
        # `import undetected_chromedriver` in a try/except and the default
        # run flow never calls it (see price_verifier/README.md) — exclude
        # both explicitly so PyInstaller doesn't pull in a real Chrome/
        # chromedriver dependency chain for a path this build doesn't use.
        "undetected_chromedriver",
        "selenium",
        # Only exclude known-unused heavy frameworks, matching
        # amazon_scraper_windows.spec's convention.
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "wx",
        "gi",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,          # bundled into single .exe (onefile mode)
    a.zipfiles,
    a.datas,
    exclude_binaries=False,
    name="PriceVerificationTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX disabled — reduces AV false-positive rate, same as amazon_scraper_windows.spec
    console=False,         # No cmd window — startup errors go to logs/startup.log + a message box (see app.py main())
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,             # Replace with an .ico path if one is added later
)

# COLLECT removed — onefile mode bundles everything inside the .exe,
# matching amazon_scraper_windows.spec's rationale (no "python311.dll not
# found" errors when the vendor runs it from inside a downloaded ZIP).
