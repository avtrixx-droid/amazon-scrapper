#!/bin/bash
# Fallback launcher — double-click this if AmazonScraper.app won't open
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
open "$APP_DIR/AmazonScraper.app"
