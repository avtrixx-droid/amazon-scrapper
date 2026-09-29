# price_verifier_windows.spec
# PyInstaller spec for building PriceVerificationTool.exe on Windows.
#
# Run with:  pyinstaller price_verifier_windows.spec --clean --noconfirm
# Output:    dist\PriceVerificationTool.exe
#
# Separate from amazon_scraper_windows.spec on purpose — price_verifier/ is
# an independently-architected tool (see CLAUDE.md's price_verifier/ note):
# no Cython step, no license gate.
#
# Native pieces that static import analysis misses and that only fail at
# RUNTIME (not at build time) — each is collected explicitly below:
#   - certifi's CA bundle (TLS verification for httpx)
#   - selectolax's compiled HTML parser
#   - curl_cffi's compiled wrapper + bundled libcurl-impersonate (Chrome-
#     identical TLS fingerprint — the fix for Amazon throttling)
#   - undetected-chromedriver + selenium for the final "check in real
#     Chrome" pass (Chrome itself must be installed on the machine;
#     chromedriver is downloaded to data/uc_cache on first use)

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

try:
    import certifi
    certifi_datas = [(certifi.where(), "certifi")]
except ImportError:
    certifi_datas = []

datas, binaries, hidden = list(certifi_datas), [], []
for pkg in ("selectolax", "curl_cffi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h

hidden += collect_submodules("price_verifier", filter=lambda name: ".tests" not in name)
hidden += collect_submodules("undetected_chromedriver")
hidden += collect_submodules("selenium.webdriver.chrome")
hidden += collect_submodules("selenium.webdriver.chromium")
hidden += collect_submodules("selenium.webdriver.common")
hidden += collect_submodules("selenium.webdriver.remote")
hidden += collect_submodules("selenium.webdriver.support")

a = Analysis(
    ["price_verifier/app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + [
        # Flask / Werkzeug / Jinja2 (templates are inline Python source —
        # price_verifier/templates_inline.py — so no templates/ datas entry)
        "flask", "flask.json", "flask.logging", "flask.helpers", "flask.wrappers",
        "werkzeug", "werkzeug.serving", "werkzeug.exceptions", "werkzeug.routing",
        "jinja2", "jinja2.ext", "click", "itsdangerous", "blinker",
        # HTTP stacks
        "curl_cffi", "curl_cffi.requests",
        "httpx", "httpcore", "h11", "h2", "hpack", "hyperframe",
        "certifi", "idna", "sniffio", "anyio", "anyio._backends._asyncio",
        # Excel
        "openpyxl", "openpyxl.styles", "openpyxl.utils", "openpyxl.worksheet", "et_xmlfile",
        # Chrome fallback extras
        "websockets", "requests", "winreg",
        # stdlib reached only through dynamic paths
        "sqlite3", "asyncio", "webbrowser", "logging.handlers", "ctypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas", "scipy",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "gi",
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
    a.binaries,          # onefile: everything inside the .exe
    a.zipfiles,
    a.datas,
    exclude_binaries=False,
    name="PriceVerificationTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX disabled — reduces antivirus false positives
    console=False,       # no cmd window; errors go to logs/app.log + a message box
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
