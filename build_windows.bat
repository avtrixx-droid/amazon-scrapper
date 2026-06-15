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

REM ── 5. Compile Cython extensions ──────────────────────────────────────────────
echo.
echo Compiling Cython extensions (license.py + scraper.py → native .pyd)...
pip install "Cython>=3.0" --quiet
python setup_cython.py build_ext --inplace
if errorlevel 1 (
    echo ERROR: Cython compilation failed
    pause
    exit /b 1
)
echo [OK] Cython extensions compiled

REM ── 5b. Move .py source OUT during the build ────────────────────────────────
REM  Guarantee no Python source for license/scraper ships in the bundle: move
REM  the .py files aside so only the compiled .pyd remains while PyInstaller
REM  runs. They are restored immediately after PyInstaller, success or fail.
if not exist ".src_backup" mkdir ".src_backup"
move /y license.py .src_backup\license.py >nul
move /y scraper.py .src_backup\scraper.py >nul
echo [OK] Python source moved aside - only compiled .pyd will be bundled

REM ── 6. Clean previous build artifacts ───────────────────────────────────────
echo.
echo Cleaning previous build...
if exist build rd /s /q build
if exist "dist\AmazonScraper" rd /s /q "dist\AmazonScraper"

REM ── 7. Run PyInstaller ───────────────────────────────────────────────────────
echo.
echo Building executable (this takes 2-5 minutes)...
echo.
pyinstaller amazon_scraper_windows.spec --clean --noconfirm
set "PYI_RESULT=%errorlevel%"

REM ── Restore .py source IMMEDIATELY (regardless of build result) ──────────────
move /y .src_backup\license.py license.py >nul
move /y .src_backup\scraper.py scraper.py >nul
rd /s /q .src_backup 2>nul

if not "%PYI_RESULT%"=="0" (
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

REM (Python source already restored immediately after PyInstaller, above.)

REM ── 9. Deactivate venv ──────────────────────────────────────────────────────
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
