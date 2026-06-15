@echo off
REM AmazonScraper — Windows Build Script
REM Run from project root: build_exe.bat
REM Requires: pip install pyinstaller  (and all requirements.txt)

echo.
echo ==================================================
echo  AmazonScraper Windows Build
echo ==================================================
echo.

REM ── Verify we are in the right directory ─────────────────────────────────────
if not exist "scraper.py" (
    echo ERROR: Run this from the AmazonScraper project root.
    pause
    exit /b 1
)

REM ── Install / verify all dependencies first ───────────────────────────────────
echo [1/5] Installing dependencies...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: pip install failed. Check your Python/pip setup.
    pause
    exit /b 1
)
pip install pyinstaller --quiet
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed.
    pause
    exit /b 1
)

REM ── Verify critical imports before building ───────────────────────────────────
echo [2/5] Verifying critical imports...
python -c "import psutil, selenium, undetected_chromedriver, openpyxl, requests, flask" 2>&1
if errorlevel 1 (
    echo ERROR: One or more required modules failed to import.
    echo Run: pip install -r requirements.txt
    pause
    exit /b 1
)
echo    All imports OK.

REM ── Verify psutil platform backend (most common missing piece) ────────────────
python -c "import psutil._psutil_windows; print('psutil backend OK')" 2>&1
if errorlevel 1 (
    echo WARNING: psutil Windows backend not found — this will cause ModuleNotFoundError in the .exe
    echo Try: pip install --upgrade psutil
)

REM ── Clean previous build artifacts ───────────────────────────────────────────
echo [3/5] Cleaning previous build...
if exist "build" rmdir /s /q "build"
if exist "dist\AmazonScraper" rmdir /s /q "dist\AmazonScraper"
if exist "dist\AmazonScraper.exe" del /f "dist\AmazonScraper.exe"

REM ── Build using .spec file ────────────────────────────────────────────────────
echo [4/5] Building executable via amazon_scraper_windows.spec...
pyinstaller amazon_scraper_windows.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo BUILD FAILED. Check output above for errors.
    pause
    exit /b 1
)

REM ── Verify the output exists ──────────────────────────────────────────────────
echo [5/5] Verifying output...
if exist "dist\AmazonScraper\AmazonScraper.exe" (
    echo    Found: dist\AmazonScraper\AmazonScraper.exe
    goto :success
)
if exist "dist\AmazonScraper.exe" (
    echo    Found: dist\AmazonScraper.exe
    goto :success
)

echo ERROR: Build completed but AmazonScraper.exe not found in dist\
pause
exit /b 1

:success
echo.
echo ==================================================
echo  BUILD SUCCESSFUL
echo  Output: dist\AmazonScraper\  (or dist\AmazonScraper.exe)
echo ==================================================
pause
