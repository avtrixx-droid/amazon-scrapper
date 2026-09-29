"""
Offline parser tests — no network, no browser. Run:
  python -m unittest price_verifier.tests.test_parser

Fixtures in tests/fixtures/ mirror the structure of real amazon.in desktop
pages (ids/classes as Amazon serves them: #centerCol with
#corePriceDisplay_desktop_feature_div, #rightCol buy box, #bylineInfo,
offer-display seller block, recommendation carousels, reviews, inline JS).
Text content is synthetic. They prove the parser's logic and scoping; they
do NOT prove the selectors still match live amazon.in today — see README.md
"Known gaps".

Also covers fetcher/debug_dump.py (DebugDumpTests at the bottom).
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from price_verifier import config
from price_verifier.fetcher import debug_dump
from price_verifier.fetcher.parser import classify_page, parse_money, parse_product_page

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def page(asin: str, center: str = "", right: str = "", extra: str = "", title: str = "Some Item") -> str:
    """Minimal-but-structurally-real product page for focused variant tests."""
    return f"""<!doctype html><html><head><title>{title} : Amazon.in</title></head><body>
<div id="dp-container">
  <div id="centerCol">
    <h1 id="title"><span id="productTitle">{title}</span></h1>
    {center}
  </div>
  <div id="rightCol"><div id="buybox">{right}</div></div>
  {extra}
  <input type="hidden" id="ASIN" name="ASIN" value="{asin}">
</div></body></html>"""


IN_STOCK_RIGHT = """
  <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">₹799.00</span></span></div>
  <div id="availability"><span>In stock</span></div>
  <input id="add-to-cart-button" name="submit.add-to-cart" type="submit">
"""


class ParseMoneyTests(unittest.TestCase):
    def test_rupee_symbol_and_commas(self):
        self.assertEqual(parse_money("₹1,499.00"), 1499.0)

    def test_plain_number(self):
        self.assertEqual(parse_money("999.50"), 999.5)

    def test_not_found_returns_none(self):
        self.assertIsNone(parse_money("Not Found"))

    def test_none_input(self):
        self.assertIsNone(parse_money(None))

    def test_zero_treated_as_missing(self):
        self.assertIsNone(parse_money("₹0.00"))


class LegacySampleFixtureTests(unittest.TestCase):
    """The original minimal fixture must keep working."""

    def setUp(self):
        self.html = fixture("sample_product_page.html")

    def test_classifies_as_product(self):
        self.assertEqual(classify_page(self.html, "B09W9FND7M"), "product")

    def test_extracts_title_price_availability(self):
        p = parse_product_page(self.html, "B09W9FND7M")
        self.assertEqual(p.page_kind, "product")
        self.assertEqual(p.title, "Lapcare Webcam 720p with Built-in Microphone")
        self.assertEqual(p.price, 1499.0)
        self.assertEqual(p.mrp, 1999.0)
        self.assertTrue(p.is_in_stock)

    def test_seller_absent_is_none_not_amazon(self):
        self.assertIsNone(parse_product_page(self.html, "B09W9FND7M").seller)

    def test_asin_not_present_is_unknown(self):
        self.assertEqual(classify_page(self.html, "B0000000ZZ"), "unknown")


class FullDesktopPageTests(unittest.TestCase):
    """product_full.html — current desktop layout with carousels, variant
    swatches, reviews, and incidental 'something went wrong' JS strings."""

    @classmethod
    def setUpClass(cls):
        cls.html = fixture("product_full.html")
        cls.p = parse_product_page(cls.html, "B0GLOTY001")

    def test_incidental_error_strings_still_product(self):
        self.assertIn("Sorry! Something went wrong!", self.html)
        self.assertIn("something went wrong on our end", self.html)
        self.assertEqual(classify_page(self.html, "B0GLOTY001"), "product")

    def test_title(self):
        self.assertEqual(self.p.title, "GLOTY Wireless Mouse 2.4GHz Silent Click, 1600 DPI, Ergonomic (Black)")

    def test_price_is_buybox_price_not_carousel_or_unit_price(self):
        self.assertEqual(self.p.price, 499.0)

    def test_mrp_read_from_center_column(self):
        # The regression from the live run: MRP sits in centerCol's
        # #corePriceDisplay_desktop_feature_div, outside the buy box.
        self.assertEqual(self.p.mrp, 999.0)

    def test_carousel_and_swatch_mrps_ignored(self):
        self.assertNotIn(self.p.mrp, (5999.0, 3999.0, 2499.0, 1299.0, 1499.0))

    def test_seller(self):
        self.assertEqual(self.p.seller, "HPT GLOBAL INNOVATIONS")

    def test_brand_from_visit_the_store_byline(self):
        self.assertEqual(self.p.brand, "GLOTY")

    def test_in_stock(self):
        self.assertTrue(self.p.is_in_stock)
        self.assertEqual(self.p.availability_raw, "In stock")
        self.assertFalse(self.p.no_featured_offer)

    def test_mrp_ignored_when_only_in_carousel(self):
        html = self.html.replace("basisPrice", "notTheMrp").replace('data-a-strike="true" data-a-color="secondary"', "")
        # Remove the centre MRP entirely: remaining MRP-looking values live
        # only in carousels/swatches and must not be picked up.
        start = html.index('<div class="a-section a-spacing-small aok-align-center">')
        end = html.index("</div>", start) + len("</div>")
        html = html[:start] + html[end:]
        p = parse_product_page(html, "B0GLOTY001")
        self.assertEqual(p.price, 499.0)
        self.assertIsNone(p.mrp)

    def test_price_with_blank_offscreen_uses_whole_fraction(self):
        html = self.html.replace('<span class="a-offscreen">₹499.00</span>', '<span class="a-offscreen"> </span>')
        html = html.replace('<span class="a-price-whole">499<span class="a-price-decimal">.</span></span></span></span>',
                            '<span class="a-price-whole">499<span class="a-price-decimal">.</span></span>'
                            '<span class="a-price-fraction">50</span></span></span>', 1)
        p = parse_product_page(html, "B0GLOTY001")
        self.assertEqual(p.price, 499.5)

    def test_parse_1_5mb_page_fast(self):
        unit = ('<li class="a-carousel-card"><div data-asin="B0FILL0001"><span class="a-price"><span class="a-offscreen">'
                '₹199.00</span></span><span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">'
                '₹999.00</span></span><div data-hook="review-body">Nice. Something went wrong? No.</div></div></li>\n')
        script = '<script type="a-state">' + '{"asin":"B0FILL0002","price":"₹149"},' * 12000 + "</script>"
        filler = script + '<div id="fillerCarousel"><ol>' + unit * (1_000_000 // len(unit)) + "</ol></div>"
        big = self.html.replace('<div id="customerReviews"', filler + '<div id="customerReviews"')
        self.assertGreater(len(big.encode("utf-8")), 1_400_000)
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            p = parse_product_page(big, "B0GLOTY001")
            best = min(best, time.perf_counter() - t0)
        self.assertEqual((p.price, p.mrp, p.seller), (499.0, 999.0, "HPT GLOBAL INNOVATIONS"))
        # Target is < 50 ms (measured ~30-45 ms locally on this node-dense
        # worst case); the assertion leaves headroom for slow CI runners.
        self.assertLess(best, 0.25, f"parse took {best * 1000:.1f} ms")


class MrpTableLayoutTests(unittest.TestCase):
    """Older #corePrice_desktop table: price-to-pay carries a-text-price and
    the MRP has no .a-offscreen — only the 'M.R.P.:' text fallback reads it."""

    @classmethod
    def setUpClass(cls):
        cls.p = parse_product_page(fixture("product_mrp_table_layout.html"), "B0PORTRON1")

    def test_price(self):
        self.assertEqual(self.p.price, 1299.0)

    def test_mrp_text_fallback(self):
        self.assertEqual(self.p.mrp, 2499.0)

    def test_you_save_and_fbt_not_mistaken_for_mrp(self):
        self.assertNotIn(self.p.mrp, (1200.0, 9999.0))

    def test_merchant_info_seller(self):
        self.assertEqual(self.p.seller, "Appario Retail Private Ltd")

    def test_brand_colon_byline(self):
        self.assertEqual(self.p.brand, "Portronics")

    def test_low_stock_with_cta_is_in_stock(self):
        self.assertTrue(self.p.is_in_stock)
        self.assertEqual(self.p.availability_raw, "Only 3 left in stock.")


class NoFeaturedOfferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = parse_product_page(fixture("product_no_featured_offer.html"), "B0GLOTY002")

    def test_variant_page_is_product(self):
        self.assertEqual(self.p.page_kind, "product")
        self.assertIn("(White)", self.p.title)

    def test_flags_no_featured_offer(self):
        self.assertTrue(self.p.no_featured_offer)

    def test_no_price_no_stock_verdict(self):
        # ₹529 "(2 new offers)" and the ₹499 sibling swatch are NOT this
        # listing's featured price.
        self.assertIsNone(self.p.price)
        self.assertIsNone(self.p.is_in_stock)

    def test_no_seller_guessed(self):
        self.assertIsNone(self.p.seller)

    def test_brand_from_product_overview(self):
        self.assertEqual(self.p.brand, "GLOTY")

    def test_text_only_see_all_buying_options(self):
        html = page("B0SEEALL01", right='<span class="a-button"><a href="/gp/offer-listing/B0SEEALL01">See All Buying Options</a></span>')
        p = parse_product_page(html, "B0SEEALL01")
        self.assertTrue(p.no_featured_offer)
        self.assertIsNone(p.price)

    def test_see_all_link_alongside_real_buy_box_is_not_flagged(self):
        html = page("B0SEEALL02", right=IN_STOCK_RIGHT + '<div id="buybox-see-all-buying-choices">See All Buying Options</div>')
        p = parse_product_page(html, "B0SEEALL02")
        self.assertFalse(p.no_featured_offer)
        self.assertEqual(p.price, 799.0)


class AvailabilityTests(unittest.TestCase):
    def test_currently_unavailable_fixture(self):
        p = parse_product_page(fixture("product_unavailable.html"), "B0LAPHUB04")
        self.assertEqual(p.page_kind, "product")
        self.assertFalse(p.is_in_stock)
        self.assertIn("unavailable", p.availability_raw.lower())
        self.assertIsNone(p.price)
        self.assertIsNone(p.seller)
        self.assertEqual(p.brand, "Lapcare")  # detail-bullets "Brand ‏ : ‎ Lapcare"

    def test_out_of_stock_phrase(self):
        html = page("B0OOSTEST1", right='<div id="availability"><span>Currently unavailable.</span></div>')
        p = parse_product_page(html, "B0OOSTEST1")
        self.assertFalse(p.is_in_stock)

    def test_no_cta_no_explicit_phrase_is_ambiguous(self):
        html = page("B0AMBIGUOU", right='<div id="availability"><span>Ships in 2-3 weeks.</span></div>')
        self.assertIsNone(parse_product_page(html, "B0AMBIGUOU").is_in_stock)

    def test_in_stock_text_without_cta_is_ambiguous(self):
        html = page("B0NOCTA001", right='<div id="availability"><span>In stock</span></div>')
        self.assertIsNone(parse_product_page(html, "B0NOCTA001").is_in_stock)

    def test_cta_without_text_is_in_stock(self):
        html = page("B0CTAONLY1", right='<input id="add-to-cart-button" type="submit">')
        self.assertTrue(parse_product_page(html, "B0CTAONLY1").is_in_stock)

    def test_unavailable_text_beats_cta(self):
        html = page("B0BOTH0001", right='<div id="availability"><span>Temporarily out of stock.</span></div>'
                                         '<input id="add-to-cart-button" type="submit">')
        self.assertFalse(parse_product_page(html, "B0BOTH0001").is_in_stock)


class FalsePositiveScopeTests(unittest.TestCase):
    """Sponsored / unrelated prices elsewhere on the page must never win."""

    def test_price_outside_buybox_is_ignored(self):
        html = """
        <html><body>
        <input type="hidden" id="ASIN" value="B0TARGETXX">
        <div id="sponsoredProducts">
          <span class="a-price"><span class="a-offscreen">₹99.00</span></span>
        </div>
        <div id="rightCol">
          <span id="productTitle">Target Product</span>
          <div id="corePriceDisplay_desktop_feature_div">
            <span class="a-price apexPriceToPay"><span class="a-offscreen">₹2,499.00</span></span>
          </div>
          <div id="availability"><span>In stock.</span></div>
          <input id="add-to-cart-button" name="submit.add-to-cart" type="submit">
        </div>
        </body></html>
        """
        p = parse_product_page(html, "B0TARGETXX")
        self.assertEqual(p.price, 2499.0, "must read the buy-box price, not the sponsored ₹99 one")

    def test_no_price_container_means_no_price(self):
        # Only carousel prices on the page → None, never a guessed carousel price.
        html = page("B0NOPRICE1", right='<div id="availability"><span>In stock</span></div>',
                    extra='<div id="sp_detail"><span class="a-price"><span class="a-price-whole">99</span></span>'
                          '<span class="priceToPay"><span class="a-offscreen">₹79</span></span></div>')
        p = parse_product_page(html, "B0NOPRICE1")
        self.assertIsNone(p.price)
        self.assertIsNone(p.mrp)

    def test_mrp_below_price_rejected(self):
        # A per-unit "(₹4.99 / count)" style a-text-price must not become MRP.
        center = ('<div id="corePriceDisplay_desktop_feature_div"><span class="a-price priceToPay">'
                  '<span class="a-offscreen">₹499.00</span></span><span class="a-price a-text-price" data-a-size="s">'
                  '<span class="a-offscreen">₹4.99</span></span></div>')
        p = parse_product_page(page("B0UNITPR01", center=center, right=IN_STOCK_RIGHT), "B0UNITPR01")
        self.assertEqual(p.price, 499.0)
        self.assertIsNone(p.mrp)


class SellerVariantTests(unittest.TestCase):
    def seller_for(self, right: str) -> str | None:
        return parse_product_page(page("B0SELLER01", right=IN_STOCK_RIGHT + right), "B0SELLER01").seller

    def test_seller_profile_trigger(self):
        self.assertEqual(self.seller_for('<a id="sellerProfileTriggerId" href="#">Clicktech Retail Private Ltd</a>'),
                         "Clicktech Retail Private Ltd")

    def test_merchant_info_link(self):
        self.assertEqual(self.seller_for(
            '<div id="merchant-info">Sold by <a href="/gp/help/seller?seller=X"><span>RetailEZ Pvt Ltd</span></a> '
            'and <a href="/fba">Fulfilled by Amazon</a>.</div>'), "RetailEZ Pvt Ltd")

    def test_merchant_info_ships_from_and_sold_by_amazon(self):
        self.assertEqual(self.seller_for('<div id="merchant-info">Ships from and sold by Amazon.in.</div>'), "Amazon")

    def test_merchant_info_plain_text_sold_by(self):
        self.assertEqual(self.seller_for(
            '<div id="merchant-info">Sold by Coco Blue Retail and Fulfilled by Amazon.</div>'), "Coco Blue Retail")

    def test_merchant_info_feature_message(self):
        self.assertEqual(self.seller_for(
            '<div id="merchantInfoFeature_feature_div">'
            '<div class="offer-display-feature-label"><span class="offer-display-feature-text-message">Sold by</span></div>'
            '<div class="offer-display-feature-text"><span class="offer-display-feature-text-message">Darshita Etel</span></div>'
            '</div>'), "Darshita Etel")

    def test_merchant_info_feature_sold_by_amazon(self):
        self.assertEqual(self.seller_for(
            '<div id="merchantInfoFeature_feature_div">'
            '<div class="offer-display-feature-label"><span class="offer-display-feature-text-message">Sold by</span></div>'
            '<div class="offer-display-feature-text"><span class="offer-display-feature-text-message">Amazon</span></div>'
            '</div>'), "Amazon")

    def test_ships_from_row_not_mistaken_for_seller(self):
        # "Ships from: Amazon" precedes "Sold by: X" — the old generic
        # ".offer-display-feature-text" selector would have returned Amazon.
        self.assertEqual(self.seller_for(
            '<div id="offerDisplayFeatures_desktop">'
            '<div offer-display-feature-name="desktop-fulfiller-info"><div class="offer-display-feature-label">Ships from</div>'
            '<div class="offer-display-feature-text"><span class="offer-display-feature-text-message">Amazon</span></div></div>'
            '<div offer-display-feature-name="desktop-merchant-info"><div class="offer-display-feature-label">Sold by</div>'
            '<div class="offer-display-feature-text"><span class="offer-display-feature-text-message">Techno Kart</span></div></div>'
            '</div>'), "Techno Kart")

    def test_offer_display_label_sibling(self):
        self.assertEqual(self.seller_for(
            '<div id="offerDisplayFeatures_desktop"><div class="a-row">'
            '<span class="offer-display-feature-label">Ships from</span><span>Amazon</span></div><div class="a-row">'
            '<span class="offer-display-feature-label">Sold by</span><span>Gadget Hub India</span></div></div>'),
            "Gadget Hub India")

    def test_tabular_buybox_attribute(self):
        self.assertEqual(self.seller_for(
            '<div id="tabular-buybox"><div class="tabular-buybox-container">'
            '<div class="tabular-buybox-text" tabular-attribute-name="Ships from"><span class="tabular-buybox-text-message">Amazon</span></div>'
            '<div class="tabular-buybox-text" tabular-attribute-name="Sold by"><span class="tabular-buybox-text-message">'
            '<a href="#">Appario Retail Private Ltd</a></span></div></div></div>'), "Appario Retail Private Ltd")

    def test_tabular_buybox_table_rows(self):
        self.assertEqual(self.seller_for(
            '<div id="tabular-buybox"><table>'
            '<tr><td><span class="a-color-tertiary">Ships from</span></td><td><span>Amazon</span></td></tr>'
            '<tr><td><span class="a-color-tertiary">Sold by</span></td><td><span>Cloudtail India</span></td></tr>'
            '</table></div>'), "Cloudtail India")

    def test_seller_absent_is_none(self):
        self.assertIsNone(self.seller_for(""))

    def test_seller_whitespace_collapsed(self):
        self.assertEqual(self.seller_for('<a id="sellerProfileTriggerId">\n   HPT   GLOBAL\n INNOVATIONS  </a>'),
                         "HPT GLOBAL INNOVATIONS")


class BrandVariantTests(unittest.TestCase):
    def brand_for(self, center: str = "", extra: str = "") -> str | None:
        return parse_product_page(page("B0BRAND001", center=center, right=IN_STOCK_RIGHT, extra=extra), "B0BRAND001").brand

    def test_visit_the_store(self):
        self.assertEqual(self.brand_for('<a id="bylineInfo">Visit the GLOTY Store</a>'), "GLOTY")

    def test_multi_word_store(self):
        self.assertEqual(self.brand_for('<a id="bylineInfo">Visit the Coco Blue Store</a>'), "Coco Blue")

    def test_brand_colon(self):
        self.assertEqual(self.brand_for('<a id="bylineInfo">Brand: GLOTY</a>'), "GLOTY")

    def test_product_overview_row(self):
        self.assertEqual(self.brand_for(
            '<table><tr class="a-spacing-small po-brand"><td class="a-span3"><span>Brand</span></td>'
            '<td class="a-span9"><span class="po-break-word">Lapcare</span></td></tr></table>'), "Lapcare")

    def test_tech_spec_table(self):
        self.assertEqual(self.brand_for(extra=(
            '<table id="productDetails_techSpec_section_1"><tr><th>Manufacturer</th><td>ACME</td></tr>'
            '<tr><th> Brand </th><td>‎Zebronics</td></tr></table>')), "Zebronics")

    def test_detail_bullets(self):
        self.assertEqual(self.brand_for(extra=(
            '<div id="detailBullets_feature_div"><ul><li><span class="a-list-item">'
            '<span class="a-text-bold">Brand ‏ : ‎</span><span>boAt</span></span></li></ul></div>')), "boAt")

    def test_unrecognised_byline_falls_through_to_overview(self):
        self.assertEqual(self.brand_for(
            '<a id="bylineInfo">by Some Author (Author)</a>'
            '<table><tr class="po-brand"><td>Brand</td><td>Penguin</td></tr></table>'), "Penguin")

    def test_brand_absent(self):
        self.assertIsNone(self.brand_for())


class PageClassificationTests(unittest.TestCase):
    def test_captcha_fixture(self):
        self.assertEqual(classify_page(fixture("captcha.html"), "B0GLOTY001"), "captcha")

    def test_robot_503_fixture_is_blocked(self):
        html = fixture("robot_503.html")
        self.assertEqual(classify_page(html, "B0GLOTY001"), "blocked")
        self.assertEqual(parse_product_page(html, "B0GLOTY001").page_kind, "blocked")

    def test_dog_404_fixture_is_not_found(self):
        self.assertEqual(classify_page(fixture("dog_404.html"), "B0GLOTY001"), "not_found")

    def test_minimal_captcha(self):
        html = "<html><body><form action='/errors/validateCaptcha'><input id='captchacharacters'></form></body></html>"
        self.assertEqual(classify_page(html, "B09W9FND7M"), "captcha")

    def test_minimal_robot_text_is_blocked(self):
        html = "<html><body>Sorry, we just need to make sure you're not a robot.</body></html>"
        self.assertEqual(classify_page(html, "B09W9FND7M"), "blocked")

    def test_block_title_only(self):
        html = "<html><head><title>Sorry! Something went wrong!</title></head><body></body></html>"
        self.assertEqual(classify_page(html, "B09W9FND7M"), "blocked")

    def test_minimal_not_found(self):
        html = ("<html><body><h1>Looking for something?</h1><p>We're sorry. The Web address you entered "
                "is not a functioning page on our site.</p></body></html>")
        self.assertEqual(classify_page(html, "B09W9FND7M"), "not_found")

    def test_product_page_with_block_email_in_js_is_product(self):
        html = page("B0INCIDENT", right=IN_STOCK_RIGHT,
                    extra="<script>var help='api-services-support@amazon.com'; var t='Looking for something?';</script>")
        self.assertEqual(classify_page(html, "B0INCIDENT"), "product")

    def test_product_markers_without_asin_is_unknown(self):
        self.assertEqual(classify_page(page("B0SOMEASIN", right=IN_STOCK_RIGHT), "B0OTHERASN"), "unknown")

    def test_case_variant_marker_still_product(self):
        html = "<html><body><span ID='productTitle'>X</span><input value='B0CASEVAR1'></body></html>"
        self.assertEqual(classify_page(html, "B0CASEVAR1"), "product")

    def test_empty_and_garbage(self):
        self.assertEqual(classify_page("", "B09W9FND7M"), "unknown")
        self.assertEqual(parse_product_page("", "B09W9FND7M").page_kind, "unknown")
        self.assertEqual(parse_product_page("<<<>>>\x00", "B09W9FND7M").page_kind, "unknown")
        self.assertEqual(parse_product_page(None, "B09W9FND7M").page_kind, "unknown")  # type: ignore[arg-type]

    def test_never_raises_on_broken_markup(self):
        html = '<span id="productTitle">B0BROKEN01<div id="corePriceDisplay_desktop_feature_div"><span class="priceToPay">'
        p = parse_product_page(html, "B0BROKEN01")
        self.assertEqual(p.page_kind, "product")
        self.assertIsNone(p.price)


class DebugDumpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_debug_test_"))
        self._patch = mock.patch.object(config, "DEBUG_HTML_DIR", self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_saves_into_day_dir_with_sanitized_name(self):
        path = debug_dump.save_debug_html("B0GLOTY001", "<html>x</html>", "soft block/503?", source="http")
        self.assertIsNotNone(path)
        self.assertEqual(path.parent, self.tmp / datetime.now().strftime("%Y%m%d"))
        self.assertTrue(path.name.startswith("B0GLOTY001_soft-block-503_http_"), path.name)
        self.assertTrue(path.name.endswith(".html"))
        self.assertEqual(path.read_text(encoding="utf-8"), "<html>x</html>")

    def test_path_traversal_in_parts_is_neutralised(self):
        path = debug_dump.save_debug_html("../../etc/passwd", "x", "..\\..\\evil", source="../x")
        self.assertIsNotNone(path)
        self.assertEqual(path.parent.parent, self.tmp)
        self.assertNotIn("..", path.name)

    def test_none_html_writes_placeholder(self):
        path = debug_dump.save_debug_html("B0NOBODY01", None, "timeout")
        self.assertIn("no HTML body", path.read_text(encoding="utf-8"))

    def test_same_second_collisions_get_unique_names(self):
        a = debug_dump.save_debug_html("B0SAME0001", "a", "blocked")
        b = debug_dump.save_debug_html("B0SAME0001", "b", "blocked")
        self.assertNotEqual(a, b)
        self.assertEqual(a.read_text(encoding="utf-8"), "a")
        self.assertEqual(b.read_text(encoding="utf-8"), "b")

    def test_daily_cap(self):
        day = self.tmp / datetime.now().strftime("%Y%m%d")
        day.mkdir(parents=True)
        for i in range(debug_dump.MAX_FILES_PER_DAY):
            (day / f"f{i}.html").write_text("x")
        self.assertIsNone(debug_dump.save_debug_html("B0CAPPED01", "x", "blocked"))

    def test_never_raises_when_dir_unwritable(self):
        blocker = self.tmp / "file_not_dir"
        blocker.write_text("x")
        with mock.patch.object(config, "DEBUG_HTML_DIR", blocker):
            self.assertIsNone(debug_dump.save_debug_html("B0NOWRITE1", "x", "blocked"))
            self.assertEqual(debug_dump.prune_debug_html(), 0)

    def test_prune_removes_only_old_day_dirs(self):
        old = self.tmp / (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        recent = self.tmp / (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
        other = self.tmp / "keep_me"
        for d in (old, recent, other):
            d.mkdir()
            (d / "a.html").write_text("x")
        self.assertEqual(debug_dump.prune_debug_html(max_age_days=7), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists())

    def test_prune_missing_root(self):
        with mock.patch.object(config, "DEBUG_HTML_DIR", self.tmp / "does_not_exist"):
            self.assertEqual(debug_dump.prune_debug_html(), 0)


if __name__ == "__main__":
    unittest.main()
