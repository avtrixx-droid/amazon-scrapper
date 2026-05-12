@echo off
REM ============================================================
REM  build_windows.bat — Build AmazonScraper.exe for Windows
REM  Run this on a Windows machine with Python 3.9+ installed.
REM  Output: dist\AmazonScraper\  (folder + .exe inside)
REM ============================================================
setlocal EnableDelayedExpansion

echo.
echo =====================================================
echo  Amazon Scraper — Windows Build Script
echo =====================================================
echo.

REM ── 0. Move to script directory ───────────────────────────────────────────────
cd /d "%~dp0"

REM ── 1. Check Python ───────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.9+ from python.org
    echo        Make sure "Add Python to PATH" is checked during install.
    pause
    exit /b 1
)
echo [OK] Python found
python --version

REM ── 2. Create / activate virtual environment ──────────────────────────────────
if not exist ".venv_build" (
    echo.
    echo Creating build virtual environment...
    python -m venv .venv_build
)
call .venv_build\Scripts\activate.bat
echo [OK] Virtual environment ready

REM ── 3. Install / upgrade dependencies ────────────────────────────────────────
echo.
echo Installing dependencies...
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: Failed to install requirements.txt
    pause
    exit /b 1
)
echo [OK] Dependencies installed

REM ── 4. Install PyInstaller and hooks ──────────────────────────────────────────
echo.
echo Installing PyInstaller...
pip install pyinstaller pyinstaller-hooks-contrib --quiet
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller
    pause
    exit /b 1
)
echo [OK] PyInstaller installed

REM ── 5. Clean previous build artifacts ────────────────────────────────────────
echo.
echo Cleaning previous build...
if exist build rd /s /q build
if exist "dist\AmazonScraper" rd /s /q "dist\AmazonScraper"

REM ── 6. Run PyInstaller ────────────────────────────────────────────────────────
echo.
echo Building executable (this takes 2-5 minutes)...
echo.
pyinstaller amazon_scraper_windows.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. Check output above for details.
    pause
    exit /b 1
)
echo.
echo [OK] Build complete

REM ── 7. Assemble distribution folder ──────────────────────────────────────────
echo.
echo Assembling distribution folder...

set DIST_DIR=dist\AmazonScraper_Windows_Release

if exist "%DIST_DIR%" rd /s /q "%DIST_DIR%"
mkdir "%DIST_DIR%"

REM Copy the entire PyInstaller output folder
xcopy /s /e /q "dist\AmazonScraper" "%DIST_DIR%\"

REM Copy vendor-facing files (NOT source code)
copy "README_VENDOR_APP.txt" "%DIST_DIR%\"
copy "asins.txt"              "%DIST_DIR%\"    2>nul
copy "pincodes.txt"           "%DIST_DIR%\"    2>nul

echo [OK] Distribution folder ready: %DIST_DIR%

REM ── 8. Deactivate venv ────────────────────────────────────────────────────────
call deactivate

REM ── 9. Summary ───────────────────────────────────────────────────────────────
echo.
echo =====================================================
echo  BUILD SUCCESSFUL
echo =====================================================
echo.
echo  Executable:   %DIST_DIR%\AmazonScraper.exe
echo  Send folder:  %DIST_DIR%\
echo.
echo  IMPORTANT — Vendor must have Google Chrome installed.
echo  Tell them: google.com/chrome
echo.
pause
