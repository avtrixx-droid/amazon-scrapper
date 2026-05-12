# amazon_scraper_windows.spec
# PyInstaller spec for building AmazonScraper.exe on Windows
#
# Run with:  pyinstaller amazon_scraper_windows.spec
# Output:    dist\AmazonScraper\AmazonScraper.exe

import sys
import os
from pathlib import Path

block_cipher = None

# ── Collect certifi CA bundle (needed by requests / selenium SSL) ──────────────
try:
    import certifi
    certifi_datas = [(certifi.where(), "certifi")]
except ImportError:
    certifi_datas = []

# ── Collect selenium webdriver data files ──────────────────────────────────────
try:
    import selenium
    selenium_dir = str(Path(selenium.__file__).parent)
    selenium_datas = [(selenium_dir, "selenium")]
except ImportError:
    selenium_datas = []

all_datas = certifi_datas + selenium_datas

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=[],
    datas=all_datas,
    hiddenimports=[
        # ── Flask / Werkzeug / Jinja2 ──
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
        "itsdangerous.url_safe",
        # ── undetected-chromedriver ──
        "undetected_chromedriver",
        "undetected_chromedriver.patcher",
        "undetected_chromedriver.cdp",
        "undetected_chromedriver.options",
        "undetected_chromedriver.reactor",
        "undetected_chromedriver.dprocess",
        "undetected_chromedriver.webelement",
        # ── Selenium ──
        "selenium",
        "selenium.webdriver",
        "selenium.webdriver.chrome",
        "selenium.webdriver.chrome.options",
        "selenium.webdriver.chrome.service",
        "selenium.webdriver.chrome.webdriver",
        "selenium.webdriver.common",
        "selenium.webdriver.common.by",
        "selenium.webdriver.common.keys",
        "selenium.webdriver.common.action_chains",
        "selenium.webdriver.support",
        "selenium.webdriver.support.ui",
        "selenium.webdriver.support.expected_conditions",
        "selenium.webdriver.support.wait",
        "selenium.common",
        "selenium.common.exceptions",
        "selenium.webdriver.remote.webdriver",
        "selenium.webdriver.remote.webelement",
        "selenium.webdriver.remote.command",
        # ── openpyxl ──
        "openpyxl",
        "openpyxl.styles",
        "openpyxl.styles.alignment",
        "openpyxl.styles.fonts",
        "openpyxl.styles.fills",
        "openpyxl.utils",
        "openpyxl.utils.dataframe",
        "openpyxl.utils.cell",
        "openpyxl.writer",
        "openpyxl.reader",
        "openpyxl.workbook",
        "openpyxl.worksheet",
        "openpyxl.drawing",
        "openpyxl.chart",
        # ── Requests / networking ──
        "requests",
        "requests.adapters",
        "requests.auth",
        "requests.sessions",
        "urllib3",
        "urllib3.util",
        "urllib3.util.retry",
        "urllib3.util.ssl_",
        "certifi",
        "charset_normalizer",
        "idna",
        # ── Multiprocessing ──
        "multiprocessing",
        "multiprocessing.queues",
        "multiprocessing.managers",
        "multiprocessing.pool",
        "multiprocessing.process",
        "multiprocessing.spawn",
        # ── Standard lib helpers ──
        "smtplib",
        "email",
        "email.message",
        "email.mime",
        "email.mime.text",
        "email.mime.multipart",
        "email.mime.base",
        "ssl",
        "logging.handlers",
        "webbrowser",
        "pkg_resources",
        "packaging",
        "packaging.version",
        "packaging.requirements",
        # ── Local modules ──
        "scraper",
        "config",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Only exclude known-unused heavy GUI frameworks
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
    [],
    exclude_binaries=True,
    name="AmazonScraper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX disabled — reduces AV false-positive rate
    console=False,       # No cmd window (windowed mode)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,           # Replace with "icon.ico" if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AmazonScraper",
)
