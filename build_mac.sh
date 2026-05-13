#!/bin/bash
# ============================================================
#  build_mac.sh — Build AmazonScraper.app for macOS
#  Run on a Mac with Python 3.9+ installed.
#  Output: dist/AmazonScraper.app
# ============================================================
set -e

echo ""
echo "====================================================="
echo "  Amazon Scraper — macOS Build Script"
echo "====================================================="
echo ""

# ── 0. Move to script directory ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. Check Python 3 ─────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found."
    echo "       Install from python.org or via Homebrew: brew install python"
    exit 1
fi
echo "[OK] $(python3 --version)"

# ── 2. Create / activate virtual environment ──────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv_build"
if [ ! -d "$VENV_DIR" ]; then
    echo ""
    echo "Creating build virtual environment..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
echo "[OK] Virtual environment ready"

# ── 3. Install dependencies ───────────────────────────────────────────────────
echo ""
echo "Installing dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "[OK] Dependencies installed"

# ── 4. Install PyInstaller ────────────────────────────────────────────────────
echo ""
echo "Installing PyInstaller..."
pip install pyinstaller pyinstaller-hooks-contrib --quiet
echo "[OK] PyInstaller installed"

# ── 5. Clean previous builds ──────────────────────────────────────────────────
echo ""
echo "Cleaning previous build..."
rm -rf build
rm -rf "dist/AmazonScraper.app"
rm -rf "dist/AmazonScraper"
rm -rf "dist/AmazonScraper_Mac_Release"

# ── 6. Build .app ─────────────────────────────────────────────────────────────
echo ""
echo "Building .app bundle (this takes 3-7 minutes)..."
echo ""
pyinstaller amazon_scraper_mac.spec --clean --noconfirm

echo ""
echo "[OK] Build complete"

# ── 7. Remove macOS quarantine flag from the .app ────────────────────────────
#  Without this, macOS Gatekeeper will block the app on the first launch.
#  This is safe for apps you built yourself — it just removes the "downloaded
#  from internet" flag that triggers the Gatekeeper warning.
echo ""
echo "Removing quarantine flag from .app bundle..."
xattr -cr "dist/AmazonScraper.app" 2>/dev/null || true
echo "[OK] Quarantine flag removed"

# ── 7b. Ad-hoc code signing (REQUIRED on macOS 12+ and all Apple Silicon) ─────
#  macOS LaunchServices error -47 (errFSBusyError) is caused by a missing code
#  signature. "-" means ad-hoc (self-signed) — no Apple Developer account needed.
#  This is sufficient for local/vendor distribution. For App Store or
#  notarization, a paid Apple Developer certificate is required.
echo ""
echo "Applying ad-hoc code signature..."
if command -v codesign &>/dev/null; then
    codesign --force --deep --sign - "dist/AmazonScraper.app" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "[OK] Ad-hoc code signature applied"
        codesign --verify --verbose "dist/AmazonScraper.app" 2>&1 | head -3
    else
        echo "[WARN] codesign failed — app may show error -47 on macOS 12+"
        echo "       Install Xcode Command Line Tools: xcode-select --install"
    fi
else
    echo "[WARN] codesign not found — install Xcode Command Line Tools:"
    echo "       xcode-select --install"
fi

# ── 8. Assemble distribution folder ──────────────────────────────────────────
echo ""
echo "Assembling distribution folder..."

DIST_DIR="dist/AmazonScraper_Mac_Release"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# Copy the .app bundle
cp -r "dist/AmazonScraper.app" "$DIST_DIR/"

# Copy vendor-facing files
[ -f "README_VENDOR_APP.txt" ] && cp "README_VENDOR_APP.txt" "$DIST_DIR/"
[ -f "asins.txt" ]             && cp "asins.txt" "$DIST_DIR/"
[ -f "pincodes.txt" ]          && cp "pincodes.txt" "$DIST_DIR/"

echo "[OK] Distribution folder ready: $DIST_DIR"

# ── 9. Create a helper "Open Scraper.command" double-click launcher ───────────
#  macOS double-click on .command files opens Terminal and runs the script.
#  This provides a fallback if .app Gatekeeper issues persist.
LAUNCHER="$DIST_DIR/Open Scraper.command"
cat > "$LAUNCHER" << 'LAUNCHER_EOF'
#!/bin/bash
# Fallback launcher — double-click this if AmazonScraper.app won't open
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
open "$APP_DIR/AmazonScraper.app"
LAUNCHER_EOF
chmod +x "$LAUNCHER"
echo "[OK] Fallback launcher created"

# ── 10. Deactivate venv ───────────────────────────────────────────────────────
deactivate

# ── 11. Summary ───────────────────────────────────────────────────────────────
echo ""
echo "====================================================="
echo "  BUILD SUCCESSFUL"
echo "====================================================="
echo ""
echo "  App bundle:  $DIST_DIR/AmazonScraper.app"
echo "  Send folder: $DIST_DIR/"
echo ""
echo "  VENDOR INSTRUCTIONS:"
echo "  1. Make sure Google Chrome is installed (google.com/chrome)"
echo "  2. Double-click AmazonScraper.app"
echo "  3. If macOS blocks it: right-click → Open → Open"
echo ""
