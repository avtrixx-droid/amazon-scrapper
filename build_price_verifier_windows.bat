@echo off
REM ============================================================
REM  build_price_verifier_windows.bat — Build PriceVerificationTool.exe
REM  Run this on a Windows machine with Python 3.9+ installed.
REM  Output: dist\PriceVerificationTool_Windows_Release\
REM
REM  This is the local equivalent of what
REM  .github/workflows/price_verifier_build.yml runs in CI on every push
REM  that touches price_verifier/** — use this to reproduce a CI build
REM  failure locally, or to build without waiting on CI.
REM ============================================================
setlocal EnableDelayedExpansion

echo.
echo =====================================================
echo  Price Verification Tool — Windows Build Script
echo =====================================================
echo.

REM ── 0. Move to script directory ─────────────────────────────────────────
cd /d "%~dp0"

REM ── 1. Check Python ──────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.9+ from python.org
    echo        Make sure "Add Python to PATH" is checked during install.
    pause
    exit /b 1
)
echo [OK] Python found
python --version

REM ── 2. Create / activate virtual environment ─────────────────────────────
if not exist ".venv_build_pv" (
    echo.
    echo Creating build virtual environment...
    python -m venv .venv_build_pv
)
call .venv_build_pv\Scripts\activate.bat
echo [OK] Virtual environment ready

REM ── 3. Install dependencies ───────────────────────────────────────────────
echo.
echo Installing dependencies...
pip install --upgrade pip --quiet
pip install -r price_verifier\requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: Failed to install price_verifier\requirements.txt
    pause
    exit /b 1
)
echo [OK] Dependencies installed

REM ── 4. Install PyInstaller ────────────────────────────────────────────────
echo.
echo Installing PyInstaller...
pip install pyinstaller pyinstaller-hooks-contrib --quiet
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller
    pause
    exit /b 1
)
echo [OK] PyInstaller installed

REM ── 5. Run the offline test suite as a build gate ─────────────────────────
echo.
echo Running offline test suite (price_verifier\tests)...
python -m unittest discover -s price_verifier\tests -p "test_*.py"
if errorlevel 1 (
    echo ERROR: Tests failed — not building on top of a broken change.
    pause
    exit /b 1
)
echo [OK] All tests passed

REM ── 6. Clean previous build artifacts ─────────────────────────────────────
echo.
echo Cleaning previous build...
if exist build rd /s /q build
if exist "dist\PriceVerificationTool.exe" del /q "dist\PriceVerificationTool.exe"

REM ── 7. Run PyInstaller ─────────────────────────────────────────────────────
echo.
echo Building executable (this takes 2-5 minutes)...
echo.
pyinstaller price_verifier_windows.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. Check output above for details.
    pause
    exit /b 1
)
echo.
echo [OK] Build complete

REM ── 8. Assemble distribution folder ───────────────────────────────────────
echo.
echo Assembling distribution folder...

set DIST_DIR=dist\PriceVerificationTool_Windows_Release

if exist "%DIST_DIR%" rd /s /q "%DIST_DIR%"
mkdir "%DIST_DIR%"

copy "dist\PriceVerificationTool.exe" "%DIST_DIR%\"
copy "price_verifier\README.md"        "%DIST_DIR%\"

echo [OK] Distribution folder ready: %DIST_DIR%

REM ── 9. Deactivate venv ─────────────────────────────────────────────────────
call deactivate

REM ── 10. Summary ────────────────────────────────────────────────────────────
echo.
echo =====================================================
echo  BUILD SUCCESSFUL
echo =====================================================
echo.
echo  Executable:   %DIST_DIR%\PriceVerificationTool.exe
echo  Send folder:  %DIST_DIR%\
echo.
echo  This build has NOT been run against live Amazon yet — see
echo  price_verifier\README.md "Known gaps" before treating its output
echo  as trustworthy.
echo.
pause
