# =====================================================
# AMAZON SCRAPER SETTINGS
# Edit only this file. Do not touch anything else.
# =====================================================

# ---- YOUR PINCODES ----
# Add or remove pincodes below
# Format: "pincode": "City Name"
PINCODES = {
    "110001": "Delhi",
    "400001": "Mumbai",
    "560001": "Bangalore",
    "600001": "Chennai",
    "500001": "Hyderabad",
    "411001": "Pune",
}

# ---- OUTPUT SETTINGS ----
# Where to save the Excel file
# Default: saves to Desktop
OUTPUT_FOLDER = "Desktop"

# What to name the output file
# {date} is replaced with the date and time of the run (e.g. 13May2026_143022)
# Each run creates a NEW file — old reports are never overwritten
OUTPUT_FILENAME = "Amazon_Report_{date}.xlsx"

# ---- EMAIL SETTINGS (optional) ----
# Set SEND_EMAIL = True to email the report when done
SEND_EMAIL = False
EMAIL_FROM = "your@gmail.com"
EMAIL_PASSWORD = "your-app-password"   
EMAIL_TO = "vendor@email.com"
EMAIL_SUBJECT = "Amazon Report - {date}"

# ---- SCRAPER SETTINGS ----
# Delay between requests in seconds (do not set below 3)
# Higher = safer but slower
MIN_DELAY = 3
MAX_DELAY = 8

# How many times to retry a failed scrape
MAX_RETRIES = 2

# Run browser in background (True) or visible (False)
# Set False if you want to watch the browser work
HEADLESS = True

