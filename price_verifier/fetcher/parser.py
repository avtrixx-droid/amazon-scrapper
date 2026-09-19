"""
parser.py — ALL Amazon HTML parsing lives here (spec section 12: "isolate all
parsing selectors in one module so a layout change is a one-file fix").

Selectors for price/MRP/availability/title and the CAPTCHA/block/not-found
indicators are ported from this repo's scraper.py, which has them validated
against live amazon.in over many runs (see CLAUDE.md's extraction table and
"Known Issues" log). Two things are NOT a straight port and need real-page
validation once network access is available (see README.md "Known gaps"):

1. scraper.py reads these via Selenium's `.text`/`textContent` on a live,
   JS-hydrated DOM. This module reads the same CSS-addressable nodes out of
   *static* HTML returned by a plain HTTP GET. `.a-offscreen` price spans are
   server-rendered (confirmed by scraper.py's own text-content fallback logic
   needing to work even when `.text` returns empty for CSS-hidden elements —
   that only makes sense if the text is in the HTML, not injected by JS), so
   this should work, but it is unverified against a live response in this
   environment (outbound access to amazon.in is blocked here — see README).
2. Buy-box scoping: scraper.py's `find_buy_box` scores live DOM elements by
   which interactive children they contain. This module does a lighter,
   ID-anchored version suited to static HTML (see `_BUY_BOX_CONTAINER_IDS`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from price_verifier.fetcher.models import ParsedProduct

# ── Money parsing (ported from scraper.py's parse_money) ──────────────────────
_MONEY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def parse_money(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = text.replace("₹", "").replace(",", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if not m:
        return None
    try:
        val = float(m.group(1))
        return val if val > 0 else None
    except ValueError:
        return None


# ── Buy-box anchor (static-HTML analogue of scraper.py's find_buy_box) ────────
# Amazon wraps the offer panel in one of these ids depending on layout/AB test.
# Ordered most- to least-specific; first match wins.
_BUY_BOX_CONTAINER_IDS = (
    "#buybox", "#desktop_buybox", "#apex_desktop", "#unifiedBuyBox",
    "#rightCol", "#centerCol_feature_div", "#centerCol",
)


def _find_buy_box(tree: HTMLParser):
    for sel in _BUY_BOX_CONTAINER_IDS:
        node = tree.css_first(sel)
        if node is not None:
            return node
    return None


# ── Title ───────────────────────────────────────────────────────────────────
_TITLE_SELECTORS = "#productTitle, .product-title, h1.a-size-large"


def _extract_title(tree: HTMLParser) -> str | None:
    node = tree.css_first(_TITLE_SELECTORS)
    if node is None:
        return None
    text = node.text(strip=True)
    return text or None


# ── Price (ported from scraper.py extract_price) ──────────────────────────────
_PRICE_OFFSCREEN_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .apexPriceToPay span.a-offscreen, "
    "#corePriceDisplay_desktop_feature_div .priceToPay span.a-offscreen, "
    "#corePrice_feature_div .apexPriceToPay span.a-offscreen, "
    "#corePrice_feature_div .priceToPay span.a-offscreen, "
    ".apexPriceToPay span.a-offscreen, "
    ".priceToPay span.a-offscreen"
)
_PRICE_WHOLE_CONTAINERS = (
    "#corePriceDisplay_desktop_feature_div",
    "#corePrice_feature_div",
    ".apexPriceToPay",
    ".priceToPay",
    "",  # bare fallback — any .a-price-whole on the page
)
_PRICE_LEGACY_SELECTORS = (
    "#priceblock_dealprice, #priceblock_ourprice, #price_inside_buybox, "
    "#tp_price_block_total_price_ww span.a-offscreen"
)

# "Lowest across all sellers" (config.DEFAULT_PRICE_SOURCE == "lowest") needs the
# separate /gp/offer-listing page — a product page only ever exposes the Buy Box
# price. That page isn't modeled here yet; see README "Known gaps" item 2.
_MRP_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .basisPrice span.a-offscreen, "
    "#corePrice_feature_div .basisPrice span.a-offscreen, "
    "#corePriceDisplay_desktop_feature_div .a-price.a-text-price span.a-offscreen, "
    "#corePrice_feature_div .a-price.a-text-price span.a-offscreen, "
    ".basisPrice span.a-offscreen, .a-price.a-text-price span.a-offscreen, "
    "span.a-text-price > span.a-offscreen, #priceblock_ourprice"
)


def _extract_buybox_price(scope) -> float | None:
    node = scope.css_first(_PRICE_OFFSCREEN_SELECTORS)
    if node is not None:
        val = parse_money(node.text())
        if val:
            return val

    for container in _PRICE_WHOLE_CONTAINERS:
        whole_sel = f"{container} .a-price-whole".strip()
        whole_node = scope.css_first(whole_sel)
        if whole_node is None:
            continue
        whole = whole_node.text(strip=True)
        if not whole:
            continue
        frac = "00"
        frac_node = scope.css_first(f"{container} .a-price-fraction".strip())
        if frac_node is not None:
            frac = frac_node.text(strip=True) or "00"
        combined = f"{whole}.{frac}".replace(",", "").replace(" ", "").strip(".")
        val = parse_money(combined)
        if val:
            return val

    legacy_node = scope.css_first(_PRICE_LEGACY_SELECTORS)
    if legacy_node is not None:
        return parse_money(legacy_node.text())
    return None


def _extract_mrp(scope) -> float | None:
    node = scope.css_first(_MRP_SELECTORS)
    return parse_money(node.text()) if node is not None else None


# ── Availability (ported from scraper.py extract_availability) ────────────────
_AVAILABILITY_SELECTORS = (
    "#availability span, #availabilityInsideBuyBox_feature_div span, "
    "#outOfStock span, #almAvailability_feature_div span.primary-availability-message"
)
_BUY_BOX_ACTIVE_SELECTORS = (
    "#add-to-cart-button, #buy-now-button, #freshAddToCartButton, "
    "input[name='submit.add-to-cart'], #submit\\.add-to-cart-ubb"
)
_UNAVAILABLE_PHRASES = (
    "currently unavailable",
    "not available",
    "we don't know when or if",
    "sign up to be notified",
    "item under review",
    "out of stock",
)


def _extract_availability(scope, tree: HTMLParser) -> tuple[str | None, bool | None]:
    """Returns (raw_text, is_in_stock). Cross-checks the availability text
    against actual buy-box CTA presence, same rationale as scraper.py: the
    catalog text alone can say "In Stock" with no seller actually able to
    fulfill it."""
    node = scope.css_first(_AVAILABILITY_SELECTORS) or tree.css_first(_AVAILABILITY_SELECTORS)
    text = node.text(strip=True) if node is not None else None
    text_lower = (text or "").lower()

    buy_box_active = tree.css_first(_BUY_BOX_ACTIVE_SELECTORS) is not None
    explicit_unavailable = any(p in text_lower for p in _UNAVAILABLE_PHRASES)

    if explicit_unavailable:
        return text, False
    if buy_box_active:
        return text, True
    if text:
        # Text present but no CTA and no explicit-unavailable phrase — treat
        # as unknown rather than guessing; the pipeline maps this to
        # UNAVAILABLE rather than a false OK.
        return text, None
    return None, None


# ── Page classification (ported from scraper.py detect_captcha / detect_block /
#    validate_page_is_product) ────────────────────────────────────────────────
_BLOCK_INDICATORS = (
    "something went wrong on our end",
    "to discuss automated access",
    "automated access to amazon data",
    "api-services-support@amazon.com",
    "sorry, we just need to make sure you're not a robot",
    "enter the characters you see below",
    "type the characters you see in this image",
    "request could not be completed",
    "we're sorry, but something went wrong",
)


def classify_page(html: str, asin: str) -> str:
    """Returns 'product' | 'captcha' | 'blocked' | 'not_found' | 'unknown'.

    Mirrors scraper.py's validate_page_is_product + detect_captcha + detect_block,
    adapted to a raw HTML string (no URL/title from a live browser to check, so
    this leans more on body content than the Selenium version does).
    """
    lower = html.lower()

    if "captchacharacters" in lower or "/errors/validatecaptcha" in lower or "robot check" in lower:
        return "captcha"
    if "page-not-found" in lower or "page not found" in lower or "looking for something" in lower:
        return "not_found"
    if any(p in lower for p in _BLOCK_INDICATORS):
        return "blocked"
    if asin.lower() not in lower:
        return "unknown"
    if 'id="producttitle"' in lower or 'id="add-to-cart-button"' in lower or 'id="outofstock"' in lower:
        return "product"
    return "unknown"


def parse_product_page(html: str, asin: str) -> ParsedProduct:
    """Never raises. Any failure surfaces as None fields + page_kind, letting
    the pipeline decide the row's terminal status."""
    kind = classify_page(html, asin)
    if kind != "product":
        return ParsedProduct(page_kind=kind)

    tree = HTMLParser(html)
    scope = _find_buy_box(tree) or tree

    title = _extract_title(tree)
    price = _extract_buybox_price(scope)
    availability_raw, is_in_stock = _extract_availability(scope, tree)

    return ParsedProduct(
        title=title,
        price=price,
        availability_raw=availability_raw,
        page_kind="product",
        is_in_stock=is_in_stock,
    )
