"""
parser.py — ALL Amazon HTML parsing lives here (spec section 12: "isolate all
parsing selectors in one module so a layout change is a one-file fix").

Reads static HTML from a plain HTTP GET of an amazon.in desktop product page
(no JS execution). Selectors started as ports of this repo's live-proven
Selenium extractors in scraper.py (extract_price / extract_mrp /
extract_availability / extract_seller / detect_block) and were then corrected
against the first real price_verifier run (30 live ASINs):

- **MRP lives in the centre column, not the buy box.** On desktop amazon.in
  the struck-through "M.R.P.: ₹999" (``.basisPrice`` / ``.a-text-price``) is
  in ``#corePriceDisplay_desktop_feature_div`` inside ``#centerCol``. The old
  buy-box-scoped lookup returned None for every row. MRP is now searched
  across the whole document but ONLY inside the known price containers
  (``_CORE_PRICE_CONTAINERS``) — never page-wide, so an MRP from a
  recommendation carousel can't leak in.
- **Seller is never guessed.** The old code defaulted to "Amazon" when no
  seller node matched, which mislabelled no-featured-offer colour variants.
  ``seller`` is now None unless the page names one.
- **Classification checks product markers first.** A real product page embeds
  plenty of incidental strings ("Sorry! Something went wrong!" in JS error
  handlers, etc.); those only mean "blocked" on a page that ISN'T a product
  page.

Design rules for this module:
- Every read is scoped to a named container (id), never a page-wide class
  sweep — sponsored / "customers also bought" carousels reuse the same
  ``.a-price`` / ``.a-text-price`` / ``.basisPrice`` markup with other
  products' prices.
- Speed: a real product page is ~1-2 MB. selectolax's ``css_first`` walks the
  whole tree on every call, so all top-level anchors are collected in ONE
  traversal (``_index``) and every further query runs inside a small
  container subtree. No regex over the whole document except the linear
  marker search in ``classify_page``.
- ``parse_product_page`` never raises; unknown fields are None and the
  pipeline decides what that means (ambiguous availability goes to a
  real-Chrome double-check).
"""

from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser

from price_verifier.fetcher.models import ParsedProduct

log = logging.getLogger(__name__)

# ── Text helpers ─────────────────────────────────────────────────────────────
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_money(text: str | None) -> float | None:
    """'₹1,499.00' -> 1499.0. None for missing/zero/unparseable."""
    if not text:
        return None
    cleaned = text.replace("₹", "").replace(",", "").strip()
    m = _NUMBER_RE.search(cleaned)
    if not m:
        return None
    try:
        val = float(m.group(1))
        return val if val > 0 else None
    except ValueError:
        return None


# Zero-width / bidi marks Amazon puts in detail tables ("Brand <RLM> : <LRM> X").
_INVISIBLE_CHARS = "".join(chr(c) for c in (
    0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0xFEFF,
))
_INVISIBLE_RE = re.compile("[" + re.escape(_INVISIBLE_CHARS) + "]")
_WS_RE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _INVISIBLE_RE.sub("", text.replace("\xa0", " "))).strip()


def _node_text(node) -> str:
    if node is None:
        return ""
    try:
        return _clean(node.text(deep=True, separator=" "))
    except Exception:
        return ""


def _classes(node) -> list[str]:
    try:
        return ((node.attributes or {}).get("class") or "").split()
    except Exception:
        return []


def _has_ancestor_class(node, cls: str, stop=None, max_hops: int = 12) -> bool:
    cur = node.parent
    hops = 0
    while cur is not None and hops < max_hops:
        if stop is not None and cur == stop:
            return False
        if cls in _classes(cur):
            return True
        cur = cur.parent
        hops += 1
    return False


def _label_key(text: str) -> str:
    return _clean(text).lower().rstrip(" :")


# ── Anchor index: one tree traversal for every top-level id we care about ───
# Where Amazon renders the featured offer's price block. The centre-column
# desktop block comes first; the rest are layout/AB-test variants. Price
# (preferred reads) and MRP are confined to these.
_CORE_PRICE_IDS = (
    "corePriceDisplay_desktop_feature_div",
    "corePrice_desktop",
    "corePrice_feature_div",
    "apex_desktop",
    "apex_offerDisplay_desktop",
)
# The right-hand offer panel, most- to least-specific.
_BUY_BOX_IDS = ("buybox", "desktop_buybox", "qualifiedBuybox", "unifiedBuyBox", "rightCol")
_TITLE_IDS = ("productTitle", "title")
_LEGACY_PRICE_IDS = (
    "priceblock_dealprice", "priceblock_ourprice", "priceblock_saleprice",
    "price_inside_buybox", "newBuyBoxPrice", "tp_price_block_total_price_ww",
)
_AVAILABILITY_IDS = (
    "availability", "availabilityInsideBuyBox_feature_div", "outOfStock", "almAvailability_feature_div",
)
_CTA_IDS = (
    "add-to-cart-button", "buy-now-button", "freshAddToCartButton",
    "add-to-cart-button-ubb", "submit.add-to-cart-ubb",
)
_SEE_ALL_IDS = (
    "buybox-see-all-buying-choices", "buybox-see-all-buying-choices-announce",
    "buybox-see-all-buying-choices_feature_div",
)
_SELLER_IDS = (
    "sellerProfileTriggerId", "merchant-info", "merchantInfoFeature_feature_div",
    "tabular-buybox", "offerDisplayFeatures_desktop",
)
_BRAND_IDS = ("bylineInfo", "detailBullets_feature_div")
_DETAIL_TABLE_IDS = (
    "productDetails_techSpec_section_1", "productDetails_techSpec_section_2",
    "productDetails_detailBullets_sections1", "technicalSpecifications_section_1", "prodDetails",
)
_ALL_IDS = frozenset(
    _CORE_PRICE_IDS + _BUY_BOX_IDS + _TITLE_IDS + _LEGACY_PRICE_IDS + _AVAILABILITY_IDS
    + _CTA_IDS + _SEE_ALL_IDS + _SELLER_IDS + _BRAND_IDS + _DETAIL_TABLE_IDS
)
_PO_BRAND = "__po-brand"


# A single cheap "[id]" match beats a 45-way "#a, #b, ..." selector list by
# ~30x on node-dense pages (the engine tests every selector against every node).
_INDEX_SELECTOR = "[id], tr.po-brand"


def _index(tree: HTMLParser) -> dict:
    """{id: first node with that id} for every anchor above, plus the
    product-overview brand row — all in a single traversal."""
    idx: dict = {}
    for node in tree.css(_INDEX_SELECTOR):
        nid = node.id
        if nid in _ALL_IDS:
            if nid not in idx:
                idx[nid] = node
        elif node.tag == "tr" and _PO_BRAND not in idx and "po-brand" in _classes(node):
            idx[_PO_BRAND] = node
    return idx


def _nodes(idx: dict, ids: tuple[str, ...]) -> list:
    return [idx[i] for i in ids if i in idx]


def _find_buy_box(idx: dict):
    nodes = _nodes(idx, _BUY_BOX_IDS)
    return nodes[0] if nodes else None


# ── Title ─────────────────────────────────────────────────────────────────────
def _extract_title(idx: dict) -> str | None:
    for node in _nodes(idx, _TITLE_IDS):
        text = _node_text(node)
        if text:
            return text
    return None


# ── Price ─────────────────────────────────────────────────────────────────────
_PAY_OFFSCREEN = ".priceToPay .a-offscreen, .apexPriceToPay .a-offscreen"
_PAY_BLOCKS = ".priceToPay, .apexPriceToPay"


def _whole_fraction(node) -> float | None:
    """'.a-price-whole' + '.a-price-fraction' inside one .a-price. Amazon's
    whole span contains the decimal-point child ("1,499."), so strip it
    before joining — otherwise the fraction is silently dropped."""
    whole_node = node.css_first(".a-price-whole")
    if whole_node is None:
        return None
    whole = _node_text(whole_node).replace(",", "").replace(" ", "").rstrip(".")
    if not whole.isdigit():
        return None
    frac = _node_text(node.css_first(".a-price-fraction"))
    return parse_money(f"{whole}.{frac if frac.isdigit() else '00'}")


def _price_to_pay_in(container) -> float | None:
    for n in container.css(_PAY_OFFSCREEN):
        val = parse_money(_node_text(n))
        if val:
            return val
    # Newer layouts leave .a-offscreen blank and render whole/fraction only.
    for block in container.css(_PAY_BLOCKS):
        val = _whole_fraction(block)
        if val:
            return val
    return None


def _is_strike_or_unit_price(price_node) -> bool:
    attrs = price_node.attributes or {}
    if "a-text-price" in _classes(price_node) or attrs.get("data-a-strike") == "true":
        return True
    return attrs.get("data-a-size") == "mini"  # per-unit price "(₹5 / count)"


def _generic_price_in(container) -> float | None:
    """First non-struck, non-unit .a-price inside a core price container."""
    for price_node in container.css(".a-price"):
        if _is_strike_or_unit_price(price_node):
            continue
        if _has_ancestor_class(price_node, "basisPrice", stop=container):
            continue
        val = parse_money(_node_text(price_node.css_first(".a-offscreen")))
        if not val:
            val = _whole_fraction(price_node)
        if val:
            return val
    return None


def _extract_price(idx: dict, core: list, buy_box) -> float | None:
    # 1. The explicit "price to pay" inside the core price containers.
    for container in core:
        val = _price_to_pay_in(container)
        if val:
            return val
    # 2. "Price to pay" inside the buy box (layouts without a core block).
    if buy_box is not None:
        val = _price_to_pay_in(buy_box)
        if val:
            return val
    # 3. Any non-struck price inside the core containers (e.g. the buy box's
    #    #corePrice_feature_div ".a-price" with no priceToPay class).
    for container in core:
        val = _generic_price_in(container)
        if val:
            return val
    # 4. Legacy ids. Deliberately NO page-wide ".a-price-whole" fallback —
    #    that's exactly how a carousel's ₹99 would become our price.
    for node in _nodes(idx, _LEGACY_PRICE_IDS):
        off = node.css_first(".a-offscreen")
        val = parse_money(_node_text(off if off is not None else node))
        if val:
            return val
    return None


# ── MRP ───────────────────────────────────────────────────────────────────────
_MRP_SELECTORS = (
    ".basisPrice .a-offscreen",
    '[data-a-strike="true"] .a-offscreen',
    ".a-text-price .a-offscreen",
)
# "M.R.P.: ₹1,999.00" / "MRP ₹1,999" / "M.R.P.: Rs. 1,999". Applied only to
# the (small) text of a core price container, never the whole document.
_MRP_TEXT_RE = re.compile(
    r"\bM\.?\s?R\.?\s?P\.?\s*:?\s*(?:₹|Rs\.?|INR)?\s*(\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def _mrp_candidate_ok(off_node, container) -> bool:
    price_node = off_node.parent
    if price_node is not None and (price_node.attributes or {}).get("data-a-size") == "mini":
        return False  # per-unit price
    return not (_has_ancestor_class(off_node, "priceToPay", stop=container)
                or _has_ancestor_class(off_node, "apexPriceToPay", stop=container))


def _extract_mrp(core: list, price: float | None) -> float | None:
    def plausible(val: float | None) -> bool:
        # MRP is the legal maximum retail price — never below what's charged.
        return bool(val) and (price is None or val >= price)

    for container in core:
        for sel in _MRP_SELECTORS:
            for off in container.css(sel):
                if not _mrp_candidate_ok(off, container):
                    continue
                val = parse_money(_node_text(off))
                if plausible(val):
                    return val
    for container in core:
        for m in _MRP_TEXT_RE.finditer(_node_text(container)):
            val = parse_money(m.group(1))
            if plausible(val):
                return val
    return None


# ── Availability ──────────────────────────────────────────────────────────────
_CTA_IN_BUY_BOX = "input[name='submit.add-to-cart'], input[name='submit.buy-now']"
_UNAVAILABLE_PHRASES = (
    "currently unavailable",
    "not available",
    "we don't know when or if",
    "sign up to be notified",
    "item under review",
    "out of stock",
)


def _availability_text(idx: dict) -> str | None:
    for container in _nodes(idx, _AVAILABILITY_IDS):
        if container.attributes.get("id") == "almAvailability_feature_div":
            container = container.css_first(".primary-availability-message") or container
        # Prefer the first non-empty span (the status line) over the whole
        # block, which can also hold "Details"/"Ships from" links.
        for span in container.css("span"):
            text = _node_text(span)
            if text:
                return text[:300]
        text = _node_text(container)
        if text:
            return text[:300]
    return None


def _has_cta(idx: dict, buy_box) -> bool:
    if any(i in idx for i in _CTA_IDS):
        return True
    return buy_box is not None and buy_box.css_first(_CTA_IN_BUY_BOX) is not None


def _classify_availability(text: str | None, cta: bool) -> bool | None:
    """True / False / None. Cross-checks the text against actual CTA presence
    (same rationale as scraper.py: "In stock" text with no seller able to
    fulfil is not a buyable listing)."""
    lower = (text or "").lower()
    if any(p in lower for p in _UNAVAILABLE_PHRASES):
        return False
    if cta:
        return True  # includes "Only 3 left in stock" with a working CTA
    return None      # ambiguous → pipeline double-checks with real Chrome


# ── No featured offer ("See All Buying Options") ─────────────────────────────
_NO_FEATURED_PHRASES = ("see all buying options", "no featured offers available")


def _detect_no_featured_offer(idx: dict, buy_box, cta: bool) -> bool:
    if cta:
        return False  # a real buy box exists; any "see all" link is secondary
    if any(i in idx for i in _SEE_ALL_IDS):
        return True
    if buy_box is not None:
        lower = _node_text(buy_box).lower()
        return any(p in lower for p in _NO_FEATURED_PHRASES)
    return False


# ── Seller ────────────────────────────────────────────────────────────────────
# Matters for this tool specifically: the vendor emails the discrepancy to
# whichever seller is showing the wrong price, so WHO holds the buy box is as
# important as the price. Never guess — None when the page doesn't say.
_AMAZON_SELLER_NAMES = {"amazon", "amazon.in", "amazon india", "amazon.com"}
_SELLER_PREFIX_RE = re.compile(r"^(?:ships\s+from\s+and\s+sold\s+by|sold\s+by)\s*:?\s*", re.IGNORECASE)
_SELLER_SUFFIX_RE = re.compile(
    r"\s+and\s+(?:is\s+)?(?:fulfilled|delivered|shipped)\s+by\b.*$", re.IGNORECASE
)


def _normalize_seller(text: str | None) -> str | None:
    text = _clean(text)
    if not text:
        return None
    text = _SELLER_PREFIX_RE.sub("", text)
    text = _SELLER_SUFFIX_RE.sub("", text)
    text = text.strip(" .,:;")
    if not text or _label_key(text) == "sold by":
        return None
    if text.lower() in _AMAZON_SELLER_NAMES:
        return "Amazon"
    return text


def _next_element_text(node) -> str:
    sib = node.next
    while sib is not None:
        if sib.tag not in ("-text", "-comment"):
            text = _node_text(sib)
            if text:
                return text
        sib = sib.next
    return ""


def _value_after_label(container, label_selector: str) -> str | None:
    """'Sold by | X' layouts: find a label whose text is exactly 'Sold by'
    and return the next element beside it (or beside its parent, for label
    spans wrapped in a cell/div)."""
    for label in container.css(label_selector):
        if _label_key(_node_text(label)) != "sold by":
            continue
        text = _next_element_text(label)
        if not text and label.parent is not None:
            text = _next_element_text(label.parent)
        if text:
            return text
    return None


def _feature_value(feature) -> str | None:
    """Value text of an offer-display feature block, skipping its label."""
    for n in feature.css(".offer-display-feature-text-message, .offer-display-feature-text"):
        if "offer-display-feature-label" in _classes(n) \
                or _has_ancestor_class(n, "offer-display-feature-label", stop=feature):
            continue
        text = _node_text(n)
        if text and _label_key(text) != "sold by":
            return text
    return None


def _seller_from_merchant_info(node) -> str | None:
    for a in node.css("a"):
        text = _node_text(a)
        low = text.lower()
        if not text or "fulfilled" in low or "details" in low:
            continue
        return text
    text = _node_text(node)
    idx = text.lower().find("sold by")
    return text[idx:] if idx >= 0 else None  # _normalize_seller trims prefix/tail


def _seller_candidates(idx: dict):
    """Yields raw seller strings, most specific source first (lazily, so the
    common case — #sellerProfileTriggerId — costs one lookup)."""
    if "sellerProfileTriggerId" in idx:
        yield _node_text(idx["sellerProfileTriggerId"])
    if "merchant-info" in idx:
        yield _seller_from_merchant_info(idx["merchant-info"])
    if "merchantInfoFeature_feature_div" in idx:
        yield _feature_value(idx["merchantInfoFeature_feature_div"])
    node = idx.get("tabular-buybox")
    if node is not None:
        attr = node.css_first('[tabular-attribute-name="Sold by"]')
        if attr is not None:
            yield _node_text(attr.css_first(".tabular-buybox-text-message") or attr)
        yield _value_after_label(node, ".tabular-buybox-label, .tabular-buybox-text, td, th, span.a-color-tertiary")
    node = idx.get("offerDisplayFeatures_desktop")
    if node is not None:
        feat = node.css_first('[offer-display-feature-name="desktop-merchant-info"]')
        if feat is not None:
            yield _feature_value(feat)
        yield _value_after_label(node, ".offer-display-feature-label, .offer-display-feature-label-text")


def _extract_seller(idx: dict) -> str | None:
    for cand in _seller_candidates(idx):
        seller = _normalize_seller(cand)
        if seller:
            return seller
    return None


# ── Brand ─────────────────────────────────────────────────────────────────────
_BYLINE_VISIT_RE = re.compile(r"^visit\s+the\s+(.+?)\s+store$", re.IGNORECASE)
_BYLINE_BRAND_RE = re.compile(r"^brand\s*:\s*(.+)$", re.IGNORECASE)
_BRAND_LABELS = {"brand", "brand name"}


def _brand_from_byline(idx: dict) -> str | None:
    text = _node_text(idx.get("bylineInfo"))
    if not text:
        return None
    for rx in (_BYLINE_VISIT_RE, _BYLINE_BRAND_RE):
        m = rx.match(text)
        if m:
            return _clean(m.group(1)) or None
    return None  # e.g. a book's "by Author (Author)" — not a brand


def _brand_from_overview(idx: dict) -> str | None:
    row = idx.get(_PO_BRAND)
    if row is None:
        return None
    cells = row.css("td")
    return (_node_text(cells[-1]) or None) if len(cells) >= 2 else None


def _brand_from_detail_tables(idx: dict) -> str | None:
    for table in _nodes(idx, _DETAIL_TABLE_IDS):
        for row in table.css("tr"):
            th = row.css_first("th")
            td = row.css_first("td")
            if th is not None and td is not None and _label_key(_node_text(th)) in _BRAND_LABELS:
                val = _node_text(td)
                if val:
                    return val
    bullets = idx.get("detailBullets_feature_div")
    if bullets is not None:
        for bold in bullets.css("li .a-text-bold"):
            if _label_key(_node_text(bold)) in _BRAND_LABELS:
                val = _next_element_text(bold)
                if val:
                    return val
    return None


def _extract_brand(idx: dict) -> str | None:
    for fn in (_brand_from_byline, _brand_from_overview, _brand_from_detail_tables):
        try:
            val = fn(idx)
        except Exception:
            log.debug("brand extractor %s failed", fn.__name__, exc_info=True)
            val = None
        if val:
            return val
    return None


# ── Page classification ──────────────────────────────────────────────────────
# Product markers are checked FIRST: a real product page can contain any of
# the block/not-found phrases incidentally (JS error handlers, review text).
_PRODUCT_MARKERS_EXACT = (
    'id="productTitle"', "id='productTitle'",
    'id="add-to-cart-button"', "id='add-to-cart-button'",
    'id="outOfStock"',
)
_PRODUCT_MARKER_RE = re.compile(
    r"""id\s*=\s*["']?(?:producttitle|add-to-cart-button|outofstock)["'\s>]"""
)

_CAPTCHA_INDICATORS = (
    "/errors/validatecaptcha",
    "captchacharacters",
    "type the characters you see in this image",
    "enter the characters you see below",
)
_BLOCK_INDICATORS = (
    "api-services-support@amazon.com",
    "to discuss automated access",
    "automated access to amazon data",
    "sorry, we just need to make sure you're not a robot",
    "something went wrong on our end",
)
_BLOCK_TITLE_PREFIXES = ("sorry! something went wrong", "503 - service unavailable")
_NOT_FOUND_INDICATORS = (
    "sorry! we couldn't find that page",
    "sorry! we couldn&#39;t find that page",
    "looking for something?",
    "the web address you entered is not a functioning page",
    "page-not-found",
)


def _page_title(lower: str) -> str:
    start = lower.find("<title")
    if start < 0 or start > 200_000:
        return ""
    gt = lower.find(">", start)
    end = lower.find("</title>", gt) if gt >= 0 else -1
    if end < 0 or end - gt > 500:
        return ""
    return _clean(lower[gt + 1:end])


def _looks_like_product(html: str, asin: str) -> bool:
    # Fast path on the raw HTML (lowercasing a 1.5 MB page costs ~8 ms):
    # Amazon's own markup uses these exact spellings and upper-case ASINs.
    if asin and (asin in html or asin.upper() in html):
        if any(m in html for m in _PRODUCT_MARKERS_EXACT):
            return True
    return False


def classify_page(html: str, asin: str) -> str:
    """Returns 'product' | 'captcha' | 'blocked' | 'not_found' | 'unknown'."""
    if not html:
        return "unknown"
    asin = (asin or "").strip()
    if _looks_like_product(html, asin):
        return "product"

    lower = html.lower()
    asin_l = asin.lower()
    if asin_l and asin_l in lower and _PRODUCT_MARKER_RE.search(lower):
        return "product"

    title = _page_title(lower)
    if "robot check" in title or any(p in lower for p in _CAPTCHA_INDICATORS):
        return "captcha"
    # Blocked before not_found: misreading a throttle page as a (terminal,
    # never-retried) "not found" is the costlier mistake.
    if title.startswith(_BLOCK_TITLE_PREFIXES) or any(p in lower for p in _BLOCK_INDICATORS):
        return "blocked"
    if "page not found" in title or any(p in lower for p in _NOT_FOUND_INDICATORS):
        return "not_found"
    return "unknown"


def parse_product_page(html: str, asin: str) -> ParsedProduct:
    """Never raises. Any failure surfaces as None fields + page_kind, letting
    the pipeline decide the row's terminal status."""
    try:
        kind = classify_page(html, asin)
    except Exception:
        log.warning("classify_page failed for %s", asin, exc_info=True)
        return ParsedProduct(page_kind="unknown")
    if kind != "product":
        return ParsedProduct(page_kind=kind)

    try:
        idx = _index(HTMLParser(html))
    except Exception:
        log.warning("HTML parse failed for %s", asin, exc_info=True)
        return ParsedProduct(page_kind="unknown")

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception:
            log.warning("parser: %s failed for %s", fn.__name__, asin, exc_info=True)
            return None

    result = ParsedProduct(page_kind="product")
    core = _nodes(idx, _CORE_PRICE_IDS)
    buy_box = _find_buy_box(idx)
    cta = bool(safe(_has_cta, idx, buy_box))

    result.title = safe(_extract_title, idx)
    result.brand = safe(_extract_brand, idx)
    result.seller = safe(_extract_seller, idx)
    result.availability_raw = safe(_availability_text, idx)
    result.no_featured_offer = bool(safe(_detect_no_featured_offer, idx, buy_box, cta))

    if result.no_featured_offer:
        # No buy-box winner: any price on the page belongs to "other sellers",
        # not a featured offer. Stock can't be judged from this page.
        result.price = None
        result.is_in_stock = None
    else:
        result.price = safe(_extract_price, idx, core, buy_box)
        result.is_in_stock = safe(_classify_availability, result.availability_raw, cta)

    result.mrp = safe(_extract_mrp, core, result.price)
    return result
