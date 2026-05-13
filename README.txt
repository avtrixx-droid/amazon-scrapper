=====================================================
AMAZON SCRAPER — USER GUIDE (Amazon.in)
=====================================================

The scraper now comes with a graphical interface (UI).
No typing commands. Just fill in the form and click Start.


=====================================================
QUICK START
=====================================================

Step 1 — Make sure Google Chrome is installed.
         Download from: google.com/chrome

Step 2 — Launch the app:

  WINDOWS:  Double-click  run_windows.bat
  MAC:      Double-click  run_mac.sh
            (If Mac blocks it → right-click → Open → confirm)

Step 3 — The Amazon Scraper window will open.

Step 4 — Enter your ASINs and pincodes (two ways — see below).

Step 5 — Click  ▶ START SCRAPING

Step 6 — Wait. The Excel report opens automatically when done.


=====================================================
ENTERING YOUR ASINS AND PINCODES
=====================================================

You can choose either method using the toggle at the top of the app.

-----------------------------------------------------
METHOD A — Upload Files  (best for large lists)
-----------------------------------------------------
1. Prepare your ASINs file (any plain .txt file):

   One ASIN per line. Optional: add Item Name and internal code.
   Lines starting with # are category headers.

   Example file contents:
     # Webcams
     B09W9FND7M , Lapcare Webcam 720p , LC-WC-720

     # USB Accessories
     B08N5WRWNW , Lapcare USB Hub 4-Port , LC-HUB-4P
     B0DSKNKCYX

2. Prepare your Pincodes file (any plain .txt file):

   One pincode per line in the format:  pincode,City

   Example file contents:
     110001,Delhi
     400001,Mumbai
     560001,Bangalore
     600001,Chennai
     500001,Hyderabad
     411001,Pune

3. In the app, click Browse next to "ASINs file" and select your file.
4. Click Browse next to "Pincodes file" and select your file.

-----------------------------------------------------
METHOD B — Type / Paste Manually  (best for quick runs)
-----------------------------------------------------
1. Select "Type / paste manually" at the top of the app.
2. On the LEFT side, paste your ASINs (one per line):

   B09W9FND7M
   B08N5WRWNW,Lapcare USB Hub,LC-HUB-4P

3. On the RIGHT side, the pincodes box is pre-filled with defaults.
   Edit it to add, remove, or change any pincodes:

   110001,Delhi
   400001,Mumbai

   Each line must be:  pincode,City


=====================================================
SETTINGS
=====================================================

Parallel workers
  How many browsers run at the same time.
  4 is recommended for 100 ASINs x 8 pincodes (~1.5 hours).
  Use 1 if you want the safest, slowest run.

  Workers:  1      2      4 (default)      6      8

Headless
  Checked (default) = browsers run invisibly in the background.
  Uncheck if you want to watch a browser window while it runs.

Delay between requests
  Min / Max seconds to wait between each product page.
  Default: 3 – 8 seconds. Do NOT set Min below 3.
  Lower delays are faster but increase the chance of Amazon blocking.


=====================================================
DURING THE RUN
=====================================================

The Progress section shows:

  Overall bar     — how far through all combinations
  Worker strips   — what each parallel browser is currently doing
                    Green = last scrape OK
                    Red   = last scrape failed
  Live log        — detailed line-by-line output
  Stats           — success count / failed count / elapsed time

You can click  ■ STOP  at any time to end the run safely.


=====================================================
OUTPUT FILE
=====================================================

When the run finishes the Excel file opens automatically.

Default save location:  Desktop
Fallback (if Desktop is blocked):  AmazonScraper/output/

The file has 3 sheets:

  Results sheet (one row per product):
    Category | Item Name | Lapcare Item Code | ASIN Link |
    Current Price (Rs) | MRP (Rs) | Discount (%) | Product Ranking |
    Rating | Reviews | Seller | Scraped At |
    [one column per pincode — shows availability + delivery date]

  Pincode column colour coding:
    Green  = In Stock, delivered today or tomorrow
    Yellow = In Stock, 2–3 days
    Orange = In Stock, 4+ days
    Red    = Out of Stock
    Grey   = Scrape failed for this pincode

  Summary sheet:
    Total ASINs, success rate, average price, time taken, etc.

  Failed sheet:
    List of any combinations that could not be scraped.


=====================================================
CAPTCHA / AMAZON VERIFICATION
=====================================================

Sometimes Amazon shows a verification screen.
If that happens:
  - The affected worker pauses for 5 minutes automatically.
  - A message appears in the live log: "CAPTCHA — pausing 5 min"
  - After the pause it retries and continues.

If it keeps happening, try:
  1. Stopping the run and waiting a few hours before retrying.
  2. Switching to a different network (mobile hotspot works well).
  3. Increasing Min Delay to 5 or more seconds.


=====================================================
IF YOUR MAC SAYS "CANNOT BE OPENED" OR "DAMAGED"
=====================================================

This is a Mac security warning, not a real problem.
Do these steps ONE TIME after you first receive the app:

1. Find the AmazonScraper app in your Downloads or Desktop.
2. Right-click on it (or two-finger tap on trackpad).
3. Select "Open" from the menu.
4. A warning window appears — click "Open" again.

After doing this once, the app will open normally forever.

If the above does not work, contact your IT team and ask them to run:
   xattr -cr /path/to/AmazonScraper.app


=====================================================
IF SOMETHING GOES WRONG
=====================================================

  "Could not start browser"
    → Make sure Google Chrome is installed and up to date.
    → Help → About Google Chrome inside Chrome to update.

  "No valid ASINs found"
    → Each ASIN must be 10 characters starting with B (e.g. B09W9FND7M).

  "No valid pincodes found"
    → Each line must be:  6-digit-number,City  e.g.  110001,Delhi

  Browser opens but immediately closes
    → Chrome version may have updated. Re-run — it auto-fixes itself.

  Need to send a bug report?
    → Attach the log file:  AmazonScraper/logs/scraper_<date>.log


=====================================================
ADVANCED — COMMAND LINE (optional)
=====================================================

If you prefer the old terminal interface without the UI:

  Mac:     python3 scraper.py
  Windows: python scraper.py

Test mode (first 3 ASINs x first 2 pincodes only):
  Mac:     python3 scraper.py --test
  Windows: python scraper.py --test

Config-only settings (edit config.py directly):
  OUTPUT_FOLDER    Where to save the Excel file (default: Desktop)
  OUTPUT_FILENAME  Report name; {date} is replaced with today's date
  SEND_EMAIL       Set True to email the report (Gmail app password needed)
  PINCODES         Fallback pincode list used by the CLI mode only


=====================================================
FILE REFERENCE
=====================================================

  gui.py           The UI app — launched by the run scripts
  scraper.py       Core scraper engine — do not edit
  config.py        CLI-mode settings only
  asins.txt        Sample ASIN list for CLI mode
  pincodes.txt     Sample pincode list for CLI mode
  run_mac.sh       Double-click launcher (Mac)
  run_windows.bat  Double-click launcher (Windows)
  logs/            Debug logs (auto-created)
  output/          Fallback report folder (auto-created)
  progress/        Worker state and Chrome profiles (auto-created)

