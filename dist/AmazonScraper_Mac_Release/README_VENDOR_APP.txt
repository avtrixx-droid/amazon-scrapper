=====================================================
AMAZON SCRAPER — QUICK START GUIDE
=====================================================

Welcome! This app scrapes Amazon.in product data for
any ASINs and pincodes you provide, then saves a
detailed Excel report to your Desktop.


=====================================================
BEFORE YOU START — REQUIREMENTS
=====================================================

  1. Google Chrome must be installed on your computer.
     (Download from: google.com/chrome)

  2. Internet connection required during use.

  3. That's it. No Python, no pip, no setup needed.


=====================================================
LAUNCH THE APP
=====================================================

WINDOWS:
  Double-click  AmazonScraper.exe

  If Windows Defender shows a warning:
    → Click "More info" → "Run anyway"
    (This is a safety popup for unsigned apps — it is safe)

MAC:
  Double-click  AmazonScraper.app

  If macOS blocks it (first time only):
    → Right-click the app → Open → click Open in the dialog
    You only need to do this once.

  If nothing happens after double-clicking:
    → Double-click  "Open Scraper.command"  instead
      (This is a backup launcher in the same folder)


=====================================================
WHAT HAPPENS NEXT
=====================================================

  1. Your default web browser opens automatically to:
         http://127.0.0.1:5050

  2. The Amazon Scraper page loads in the browser.

  3. Fill in your ASINs and pincodes (see below).

  4. Click  ▶ START SCRAPING

  5. Wait. The Excel report opens automatically when done.
     Default save location: your Desktop.


=====================================================
ENTERING ASINS AND PINCODES
=====================================================

You have two options — use the toggle at the top of the page.

--------------------------------------------
OPTION A — Upload Files  (best for big lists)
--------------------------------------------

  Prepare an ASINs file (.txt):
    One ASIN per line. Optional: add name and item code.
    Lines starting with # are category labels.

    Example:
      # Webcams
      B09W9FND7M , Lapcare Webcam 720p , LC-WC-720
      B08N5WRWNW

  Prepare a Pincodes file (.txt):
    One pincode per line, format:  pincode,City

    Example:
      110001,Delhi
      400001,Mumbai
      560001,Bangalore

  Click Browse next to each field and select your file.

--------------------------------------------
OPTION B — Type / Paste  (best for quick runs)
--------------------------------------------

  Select "Type / Paste" at the top.
  Paste your ASINs on the LEFT and pincodes on the RIGHT.


=====================================================
SETTINGS
=====================================================

  Parallel workers
    How many browsers run at the same time.
    4 = recommended (about 1.5 hours for 100 ASINs × 8 pincodes)
    Use 1 for safest / slowest.

  Delay between requests (Min / Max seconds)
    Default: 3 – 8 seconds.
    Do NOT set Min below 3 — Amazon may block you.

  Headless
    Checked = browsers run silently in the background (default).
    Uncheck = watch a browser window while it works.


=====================================================
OUTPUT FILE — WHAT'S IN THE EXCEL REPORT
=====================================================

  Sheet 1 — Results
    One row per product. Columns include:
    Category | Item Name | Lapcare Code | Amazon Link |
    Price | MRP | Discount % | Product Ranking |
    Rating | Reviews | Seller | Scraped At |
    [one column per pincode — availability + delivery date]

    Pincode column colours:
      Green  = In Stock, delivered today or tomorrow
      Yellow = In Stock, 2–3 days delivery
      Orange = In Stock, 4+ days delivery
      Red    = Out of Stock
      Grey   = Could not scrape this pincode

  Sheet 2 — Summary
    Success rate, average price, total time taken.

  Sheet 3 — Failed
    List of any combinations that failed — for re-checking.


=====================================================
IF THE APP WON'T START
=====================================================

  Browser did not open?
    → Wait 10 seconds, then manually open your browser
      and go to:  http://127.0.0.1:5050

  "Could not start browser" error on the page?
    → Make sure Google Chrome is installed and up to date.
      In Chrome: click the 3-dot menu → Help → About Google Chrome

  Windows Defender blocked the app?
    → Click "More info" on the Defender popup, then "Run anyway"

  macOS won't open the app?
    → Right-click → Open → Open
    → Or double-click "Open Scraper.command" in the same folder

  Port already in use?
    → Make sure no other copy of the app is already running.
      Close any existing AmazonScraper window first.


=====================================================
IF SOMETHING GOES WRONG DURING SCRAPING
=====================================================

  CAPTCHA / Amazon verification
    → The worker pauses 5 minutes automatically, then retries.
    → If it keeps happening: stop the run, wait a few hours,
      then run again from a different network (mobile hotspot).

  Too many failures
    → Increase Min Delay to 5+ seconds and try again.
    → Run during off-peak hours (late night / early morning).

  App crashes
    → Check the log file in:
        Windows: [folder where AmazonScraper.exe is]\logs\
        Mac:     ~/Library/Application Support/AmazonScraper/logs/
    → Send the startup.log and crash.log files to your developer.


=====================================================
IMPORTANT NOTES
=====================================================

  • Always keep Google Chrome installed and up to date.
    The app downloads a matching Chrome driver automatically
    on the first run — this needs an internet connection.

  • Do NOT open multiple copies of the app at the same time.

  • Keep Min Delay at 3 seconds or higher to stay safe.

  • The app saves your Excel report to your Desktop by default.
    If Desktop is not writable, it saves to the logs folder.

  • No data is sent anywhere — everything runs locally on
    your computer.


=====================================================
NEED HELP?
=====================================================

  Send these files to your developer:

    Windows:
      [exe folder]\logs\startup.log
      [exe folder]\logs\crash.log  (if it exists)

    Mac:
      ~/Library/Application Support/AmazonScraper/logs/startup.log
      ~/Library/Application Support/AmazonScraper/logs/crash.log

=====================================================
