"""
sim_amazon.py — a local fake amazon.in for end-to-end tests.

Serves realistic desktop product pages and throttles clients the way the
real site did in the vendor's first live test: a cookieless or over-eager
client gets a burst of successful pages, then its identity is flagged and
every later request from that identity gets the 503 "Sorry! Something went
wrong!" robot page — until the client rotates to a fresh identity (new
session cookie). A per-IP ceiling additionally 503s anyone going too fast
overall, even across rotations.

Not a unit-test module (no test_ prefix): imported by test_e2e_sim.py and
usable standalone for manual runs:

    python -m price_verifier.tests.sim_amazon --port 8765 --asins 300
    PV_MARKETPLACE_BASE_URL=http://127.0.0.1:8765 python -m price_verifier.app
"""

from __future__ import annotations

import argparse
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

_DP_RE = re.compile(r"^/(?:[^/]+/)?dp/([A-Z0-9]{10})")
_AOD_RE = re.compile(r"^/gp/product/ajax/aodAjaxMain")
_ASIN_PARAM_RE = re.compile(r"[?&]asin=([A-Z0-9]{10})")


@dataclass
class SimProduct:
    asin: str
    title: str
    brand: str
    price: Optional[float]
    mrp: Optional[float]
    seller: Optional[str]
    kind: str = "in_stock"  # in_stock | unavailable | no_offer | not_found


@dataclass
class ThrottlePolicy:
    burst: int = 12              # requests an identity may make back-to-back
    sustained_rps: float = 3.0   # refill rate per identity after the burst
    anon_burst: int = 6          # cookieless clients are treated more harshly
    ip_ceiling_rps: float = 8.0  # hard cap across all identities (1 s window)
    latency_s: tuple[float, float] = (0.05, 0.15)


@dataclass
class _Bucket:
    tokens: float
    last: float
    flagged: bool = False


@dataclass
class SimStats:
    requests: int = 0
    product_ok: int = 0
    blocked: int = 0
    ceiling_blocked: int = 0
    identities: int = 0
    flagged_identities: int = 0
    homepage_hits: int = 0
    offers_ok: int = 0
    by_identity: dict = field(default_factory=dict)


def make_catalog(n: int, seed: int = 7, kinds: Optional[dict[str, float]] = None) -> dict[str, SimProduct]:
    """Deterministic catalog of n products. `kinds` maps kind -> share of the
    catalog (remainder is in_stock)."""
    rng = random.Random(seed)
    kinds = kinds or {}
    brands = ["GLOTY", "AVIGEAR", "TECHONTO", "Lapcare"]
    sellers = ["HPT GLOBAL INNOVATIONS", "Coco Blue Retail", "Amazon", "Appario Retail"]
    catalog: dict[str, SimProduct] = {}
    kind_list: list[str] = []
    for kind, share in kinds.items():
        kind_list += [kind] * int(round(n * share))
    kind_list += ["in_stock"] * max(0, n - len(kind_list))
    rng.shuffle(kind_list)
    for i in range(n):
        asin = "B0" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(8))
        brand = brands[i % len(brands)]
        price = float(rng.choice([129, 149, 185, 235, 249, 489, 599, 999, 1499]))
        catalog[asin] = SimProduct(
            asin=asin,
            title=f"{brand} Product {i} with a Long Descriptive Amazon Title (Black)",
            brand=brand,
            price=price,
            mrp=price + rng.choice([50, 100, 200, 500]),
            seller=sellers[i % len(sellers)],
            kind=kind_list[i],
        )
    return catalog


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def render_product(p: SimProduct, padding_kb: int = 200, hide_price: bool = False) -> str:
    """hide_price: an in-stock page whose price never rendered (what Amazon
    serves when the price block is filled in by JavaScript) — the pipeline
    must send it to the Chrome check."""
    carousel = "".join(
        f'<li class="a-carousel-card"><span class="a-price"><span class="a-offscreen">₹{_fmt(99 + k)}</span></span>'
        f'<span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">₹{_fmt(999 + k)}</span></span></li>'
        for k in range(8)
    )
    if p.kind == "in_stock":
        core = (
            f'<span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay" data-a-size="xl">'
            f'<span class="a-offscreen">₹{_fmt(p.price)}</span><span aria-hidden="true">'
            f'<span class="a-price-symbol">₹</span><span class="a-price-whole">{int(p.price):,}'
            f'<span class="a-price-decimal">.</span></span><span class="a-price-fraction">00</span></span></span>'
            f'<div class="a-section a-spacing-small aok-align-center"><span class="a-size-small aok-offscreen">'
            f' M.R.P.: ₹{_fmt(p.mrp)} </span><span class="a-price a-text-price" data-a-size="s" data-a-strike="true">'
            f'<span class="a-offscreen">₹{_fmt(p.mrp)}</span><span aria-hidden="true">₹{_fmt(p.mrp)}</span></span></div>'
        )
        buybox = (
            f'<div id="corePrice_feature_div"><div class="a-section a-spacing-mini">'
            f'<span class="a-price aok-align-center" data-a-size="xl" data-a-color="base">'
            f'<span class="a-offscreen">₹{_fmt(p.price)}</span><span aria-hidden="true">'
            f'<span class="a-price-symbol">₹</span><span class="a-price-whole">{int(p.price):,}'
            f'<span class="a-price-decimal">.</span></span><span class="a-price-fraction">00</span></span>'
            f'</span></div></div>'
            f'<div id="availability" class="a-section a-spacing-base"><span class="a-size-medium a-color-success">'
            f' In stock </span></div>'
            f'<input id="add-to-cart-button" name="submit.add-to-cart" type="submit" value="Add to Cart">'
            f'<input id="buy-now-button" name="submit.buy-now" type="submit">'
            f'<div id="merchantInfoFeature_feature_div"><div class="offer-display-feature-text">'
            f'<span class="a-size-small offer-display-feature-text-message">'
            f'<a id="sellerProfileTriggerId" href="/gp/help/seller/at-a-glance.html">{p.seller}</a></span></div></div>'
        )
        if hide_price:
            core = ""
            buybox = buybox[buybox.index('<div id="availability"'):]
    elif p.kind == "unavailable":
        core = ""
        buybox = (
            '<div id="availability" class="a-section a-spacing-base"><span class="a-size-medium a-color-price">'
            " Currently unavailable. </span><br><span>We don't know when or if this item will be back in stock."
            "</span></div>"
        )
    elif p.kind == "no_offer":
        core = ""
        buybox = (
            '<div id="buybox-see-all-buying-choices"><span class="a-button a-button-primary">'
            '<a class="a-button-text" href="/gp/offer-listing/">See All Buying Options</a></span></div>'
        )
    else:
        raise ValueError(p.kind)

    return f"""<!doctype html><html lang="en-in"><head><meta charset="utf-8">
<title>{p.title} : Amazon.in: Home &amp; Kitchen</title></head>
<body><div id="a-page"><div id="dp" class="dp-container">
<div id="centerCol" class="centerColAlign">
  <div id="bylineInfo_feature_div"><a id="bylineInfo" class="a-link-normal" href="/stores/{p.brand}">Visit the {p.brand} Store</a></div>
  <div id="title_feature_div"><h1 id="title" class="a-size-large a-spacing-none">
    <span id="productTitle" class="a-size-large product-title-word-break">        {p.title}       </span></h1></div>
  <div id="corePriceDisplay_desktop_feature_div">{core}</div>
</div>
<div id="rightCol"><div id="desktop_buybox"><div id="buybox">{buybox}</div></div></div>
<div id="sims-consolidated-1_feature_div"><ol class="a-carousel">{carousel}</ol></div>
<div id="customerReviews"><p>Ordered today, arrived tomorrow. Great value!</p></div>
<script>var P={{errorMsg:"Sorry, something went wrong. Please try again."}};</script>
<input type="hidden" id="ASIN" name="ASIN" value="{p.asin}">
<!-- {"x" * (padding_kb * 1024)} -->
</div></div></body></html>"""


def render_offers(p: SimProduct, layout: str = "standard") -> str:
    """Amazon's "all offers" panel (aodAjaxMain). layout="changed" renames
    every id, the way an Amazon redesign would — the pipeline must then
    refuse to use this page at all."""
    others = "".join(
        f'<div id="aod-offer" class="a-section"><div id="aod-offer-price"><span class="a-price">'
        f'<span class="a-offscreen">₹{_fmt(p.price + 30 + k)}</span></span></div>'
        f'<div id="aod-offer-soldBy"><span class="a-size-small">Sold by</span>'
        f'<a class="a-size-small a-link-normal" href="/gp/aag/main?seller=S{k}">Other Seller {k}</a></div></div>'
        for k in range(3)
    ) if p.price is not None else ""
    pinned = ""
    if p.kind == "in_stock":
        pinned = (
            f'<div id="aod-pinned-offer" class="a-section"><div id="aod-offer-price">'
            f'<span class="a-price" data-a-size="xl"><span class="a-offscreen">₹{_fmt(p.price)}</span></span>'
            f'<span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">₹{_fmt(p.mrp)}</span></span>'
            f'</div><div id="aod-offer-soldBy"><span class="a-size-small">Sold by</span>'
            f'<a class="a-size-small a-link-normal" href="/gp/aag/main?seller=X">{p.seller}</a></div></div>'
        )
    html = (
        f'<div id="aod-container" data-asin="{p.asin}"><div id="aod-asin-title">'
        f'<h5 id="aod-asin-title-text">{p.title}</h5></div>{pinned}'
        f'<div id="aod-offer-list">{others}</div></div>'
    )
    if layout == "changed":
        html = html.replace('id="aod-', 'id="offers2-')
    return html


ROBOT_PAGE = """<!DOCTYPE html><html><head><title dir="ltr">Sorry! Something went wrong!</title></head>
<body><div><a href="/ref=cs_503_logo"><img src="https://images-eu.ssl-images-amazon.com/images/G/31/x-locale/common/amazon-logo.png"></a>
<p>Sorry! Something went wrong on our end. Please go back and try again or go to Amazon's home page.</p>
<!-- To discuss automated access to Amazon data please contact api-services-support@amazon.com. -->
</div></body></html>"""

NOT_FOUND_PAGE = """<!DOCTYPE html><html><head><title>Page Not Found</title></head><body>
<div><img alt="Sorry! We couldn't find that page. Try searching or go to Amazon's home page." src="/dogs/dog.jpg">
<p>Looking for something?</p><p>We're sorry. The Web address you entered is not a functioning page on our site.</p></div></body></html>"""


class SimAmazon:
    """Owns the server thread, catalog, throttle state and stats."""

    def __init__(self, catalog: dict[str, SimProduct], policy: Optional[ThrottlePolicy] = None,
                 host: str = "127.0.0.1", port: int = 0, padding_kb: int = 200,
                 aod_layout: str = "standard", product_page_blocked: Optional[set] = None,
                 browser_only: Optional[set] = None):
        self.catalog = catalog
        self.aod_layout = aod_layout
        # ASINs whose /dp/ page is always a robot page (the offers page still works)
        self.product_page_blocked = set(product_page_blocked or ())
        # ASINs whose FIRST product-page request comes back without a price
        # (ambiguous -> the pipeline's Chrome check); later requests are normal.
        self.browser_only = set(browser_only or ())
        self._dp_hits: dict[str, int] = {}
        self.policy = policy or ThrottlePolicy()
        self.padding_kb = padding_kb
        self.stats = SimStats()
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._ceiling_window: list[float] = []
        sim = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def do_GET(self):
                sim._handle(self)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "SimAmazon":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── throttle model ─────────────────────────────────────────────────────
    def _identity(self, handler) -> tuple[str, bool]:
        cookie = SimpleCookie(handler.headers.get("Cookie", ""))
        if "session-id" in cookie:
            return "s:" + cookie["session-id"].value, False
        return "anon:" + handler.headers.get("User-Agent", "?"), True

    def _admit(self, identity: str, anon: bool) -> str:
        """Returns 'ok' | 'flagged' | 'ceiling'."""
        now = time.monotonic()
        pol = self.policy
        with self._lock:
            self._ceiling_window = [t for t in self._ceiling_window if now - t < 1.0]
            if len(self._ceiling_window) >= pol.ip_ceiling_rps:
                self.stats.ceiling_blocked += 1
                return "ceiling"
            self._ceiling_window.append(now)

            b = self._buckets.get(identity)
            if b is None:
                cap = pol.anon_burst if anon else pol.burst
                b = self._buckets[identity] = _Bucket(tokens=float(cap), last=now)
                self.stats.identities += 1
            if b.flagged:
                return "flagged"
            cap = pol.anon_burst if anon else pol.burst
            b.tokens = min(cap, b.tokens + (now - b.last) * pol.sustained_rps)
            b.last = now
            if b.tokens < 1.0:
                b.flagged = True
                self.stats.flagged_identities += 1
                return "flagged"
            b.tokens -= 1.0
            return "ok"

    def _send(self, handler, status: int, body: str, set_cookie: Optional[str] = None) -> None:
        data = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        if set_cookie:
            handler.send_header("Set-Cookie", set_cookie)
        handler.end_headers()
        handler.wfile.write(data)

    def _handle(self, handler) -> None:
        time.sleep(random.uniform(*self.policy.latency_s))
        with self._lock:
            self.stats.requests += 1
        path = handler.path.split("?", 1)[0]

        if path in ("/", ""):
            with self._lock:
                self.stats.homepage_hits += 1
            cookie = SimpleCookie(handler.headers.get("Cookie", ""))
            set_cookie = None
            if "session-id" not in cookie:
                set_cookie = f"session-id={uuid.uuid4().hex}; Path=/"
            self._send(handler, 200, "<html><head><title>Amazon.in</title></head><body>home</body></html>", set_cookie)
            return

        m = _DP_RE.match(path)
        aod = None if m else (_AOD_RE.match(path) and _ASIN_PARAM_RE.search(handler.path))
        if not m and not aod:
            self._send(handler, 404, NOT_FOUND_PAGE)
            return

        identity, anon = self._identity(handler)
        verdict = self._admit(identity, anon)
        with self._lock:
            self.stats.by_identity[identity] = self.stats.by_identity.get(identity, 0) + 1
        if verdict != "ok":
            with self._lock:
                self.stats.blocked += 1
            self._send(handler, 503, ROBOT_PAGE)
            return

        asin = (m or aod).group(1)
        product = self.catalog.get(asin)
        if product is None or product.kind == "not_found":
            self._send(handler, 404, NOT_FOUND_PAGE)
            return
        if aod:
            with self._lock:
                self.stats.offers_ok += 1
            self._send(handler, 200, render_offers(product, self.aod_layout))
            return
        if asin in self.product_page_blocked:
            with self._lock:
                self.stats.blocked += 1
            self._send(handler, 503, ROBOT_PAGE)
            return
        with self._lock:
            self.stats.product_ok += 1
            self._dp_hits[asin] = self._dp_hits.get(asin, 0) + 1
            hide = asin in self.browser_only and self._dp_hits[asin] == 1
        self._send(handler, 200, render_product(product, self.padding_kb, hide_price=hide))


def main() -> None:
    ap = argparse.ArgumentParser(description="Local fake amazon.in for testing price_verifier")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--asins", type=int, default=100)
    ap.add_argument("--csv", default="sim_batch.csv", help="write an upload-ready CSV of the catalog here")
    ap.add_argument("--aod-layout", default="standard", choices=("standard", "changed"))
    ap.add_argument("--browser-only", type=int, default=0,
                    help="this many in-stock ASINs only show their price on a second visit (exercises Chrome)")
    args = ap.parse_args()
    catalog = make_catalog(args.asins, kinds={"unavailable": 0.03, "no_offer": 0.02})
    browser_only = [a for a, p in catalog.items() if p.kind == "in_stock"][:args.browser_only]
    with open(args.csv, "w", encoding="utf-8") as f:
        f.write("ASIN No.,Brand Name,SP\n")
        for i, p in enumerate(catalog.values()):
            expected = p.price if i % 5 else p.price + 20  # every 5th row mismatches
            f.write(f"{p.asin},{p.brand},{expected:.0f}\n")
    sim = SimAmazon(catalog, port=args.port, aod_layout=args.aod_layout, browser_only=set(browser_only)).start()
    print(f"Fake Amazon at {sim.base_url} — {len(catalog)} products; upload file: {args.csv}")
    print(f"Run the app with: PV_MARKETPLACE_BASE_URL={sim.base_url} python -m price_verifier.app")
    try:
        while True:
            time.sleep(5)
            s = sim.stats
            print(f"requests={s.requests} ok={s.product_ok} blocked={s.blocked} "
                  f"(ceiling={s.ceiling_blocked}) identities={s.identities} flagged={s.flagged_identities}")
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
