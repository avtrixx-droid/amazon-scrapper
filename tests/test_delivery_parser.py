"""Selenium-free tests for the delivery promise extractor.

Run from the project root with:  python -m unittest tests.test_delivery_parser
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

# Allow running directly from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import (  # noqa: E402
    _AMAZON_NOW_SELECTOR,
    _STANDARD_DELIVERY_SELECTOR,
    _build_amazon_now_display,
    _build_delivery_display,
    _build_standard_display,
    _clean_delivery_text,
    _extract_amazon_now,
    _extract_buybox_fallback,
    _extract_standard_delivery,
    _filter_delivery_lines,
    _format_delivery_date,
    _infer_channel,
    _is_free,
    _maybe_append_date,
    _normalize_delivery_text,
    _parse_delivery_phrase,
    _score_buy_box_candidate,
    extract_earliest_delivery_from_text,
    find_buy_box,
)


# Fixed reference "now" so the assertions are deterministic across timezones.
NOW = datetime(2026, 5, 30, 14, 0)  # Sat 30 May 2026, 14:00 IST


class ParsePhraseTests(unittest.TestCase):
    """Direct round-trips through `_parse_delivery_phrase`."""

    def test_minutes_full_word(self) -> None:
        self.assertEqual(
            _parse_delivery_phrase("delivery in 10 minutes", NOW),
            datetime(2026, 5, 30, 14, 10),
        )

    def test_minutes_abbreviation_mins(self) -> None:
        # Regression: used to return None because parser only matched "minute".
        self.assertEqual(
            _parse_delivery_phrase("FREE delivery in 30 mins", NOW),
            datetime(2026, 5, 30, 14, 30),
        )

    def test_minutes_abbreviation_min_singular(self) -> None:
        self.assertEqual(
            _parse_delivery_phrase("delivery in 15 min", NOW),
            datetime(2026, 5, 30, 14, 15),
        )

    def test_hours_full_word(self) -> None:
        self.assertEqual(
            _parse_delivery_phrase("delivery in 2 hours", NOW),
            datetime(2026, 5, 30, 16, 0),
        )

    def test_hours_abbreviation_hrs(self) -> None:
        # Regression: used to return None.
        self.assertEqual(
            _parse_delivery_phrase("delivery in 4 hrs", NOW),
            datetime(2026, 5, 30, 18, 0),
        )

    def test_hours_abbreviation_hr_singular(self) -> None:
        self.assertEqual(
            _parse_delivery_phrase("delivery in 1 hr", NOW),
            datetime(2026, 5, 30, 15, 0),
        )

    def test_today_keyword(self) -> None:
        # "Today" resolves to today at 20:00 (end-of-day) so that an afternoon
        # scrape doesn't accidentally rank "today" ahead of "10 minutes" /
        # "2 hours" candidates whose datetime is later than today-noon.
        self.assertEqual(
            _parse_delivery_phrase("Get it Today", NOW),
            datetime(2026, 5, 30, 20, 0),
        )

    def test_today_with_time_window(self) -> None:
        # Sorts to today-noon — fine for priority ordering (today < tomorrow).
        result = _parse_delivery_phrase("Today 6 PM - 10 PM", NOW)
        self.assertEqual(result.date(), datetime(2026, 5, 30).date())

    def test_tomorrow(self) -> None:
        self.assertEqual(
            _parse_delivery_phrase("FREE delivery Tomorrow", NOW),
            datetime(2026, 5, 31, 12, 0),
        )

    def test_tomorrow_with_specific_date(self) -> None:
        # "Tomorrow, 31 May" must land on 31 May, not be coerced to tomorrow's
        # generic noon (would be the same here but matters around clock skew /
        # different test clocks).
        self.assertEqual(
            _parse_delivery_phrase("FREE delivery Tomorrow, 31 May", NOW),
            datetime(2026, 5, 31, 12, 0),
        )

    def test_explicit_date_day_month(self) -> None:
        self.assertEqual(
            _parse_delivery_phrase("Get it by 2 June", NOW),
            datetime(2026, 6, 2, 12, 0),
        )

    def test_explicit_date_month_day(self) -> None:
        self.assertEqual(
            _parse_delivery_phrase("Delivery by June 2", NOW),
            datetime(2026, 6, 2, 12, 0),
        )

    def test_weekday(self) -> None:
        # NOW = Sat 30 May 2026 → next Monday = 1 June.
        self.assertEqual(
            _parse_delivery_phrase("Prime delivery Monday", NOW),
            datetime(2026, 6, 1, 12, 0),
        )

    def test_weekday_with_explicit_date_prefers_date(self) -> None:
        # "Tuesday, June 4" → June 4 wins over weekday inference.
        self.assertEqual(
            _parse_delivery_phrase("Delivery by Tuesday, June 4", NOW),
            datetime(2026, 6, 4, 12, 0),
        )

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(_parse_delivery_phrase("Soon", NOW))
        self.assertIsNone(_parse_delivery_phrase("", NOW))


class ChannelInferenceTests(unittest.TestCase):
    def test_amazon_now_from_minutes(self) -> None:
        self.assertEqual(_infer_channel("FREE delivery in 10 minutes", None), "Amazon Now")

    def test_amazon_now_from_phrase(self) -> None:
        self.assertEqual(_infer_channel("Amazon Now FREE delivery", None), "Amazon Now")

    def test_amazon_fresh(self) -> None:
        self.assertEqual(_infer_channel("Amazon Fresh delivery in 2 hours", None), "Amazon Fresh")

    def test_prime(self) -> None:
        self.assertEqual(_infer_channel("Prime delivery Monday", None), "Prime")

    def test_standard_fallback(self) -> None:
        self.assertEqual(_infer_channel("FREE delivery Tomorrow, 31 May", None), "Standard")

    def test_hint_wins(self) -> None:
        self.assertEqual(_infer_channel("FREE delivery Tomorrow", "Amazon Now"), "Amazon Now")


class FreeDetectionTests(unittest.TestCase):
    def test_free_keyword(self) -> None:
        self.assertTrue(_is_free("FREE delivery in 10 minutes"))

    def test_zero_rupee(self) -> None:
        self.assertTrue(_is_free("Delivery ₹0"))

    def test_paid(self) -> None:
        self.assertFalse(_is_free("Delivery ₹40 Tomorrow"))


class DisplayBuildTests(unittest.TestCase):
    def test_format(self) -> None:
        out = _build_delivery_display("Amazon Now", "FREE delivery in 10 minutes", True)
        self.assertEqual(out, "Amazon Now – 10 Minutes (Free)")

    def test_strips_fastest_prefix(self) -> None:
        out = _build_delivery_display("Standard", "Or fastest delivery Tomorrow, 31 May", True)
        self.assertEqual(out, "Standard – Tomorrow, 31 May (Free)")

    def test_strips_orders_over_suffix(self) -> None:
        out = _build_delivery_display("Amazon Now", "FREE delivery in 10 minutes on orders over ₹149", True)
        self.assertEqual(out, "Amazon Now – 10 Minutes (Free)")


class EarliestFromTextTests(unittest.TestCase):
    """The user's spec: scrape MUST always return the earliest promise."""

    def _earliest(self, text: str) -> tuple:
        r = extract_earliest_delivery_from_text(text, None, NOW)
        return r["earliest_display"], r["earliest_dt"]

    # ── User's bug-report scenario ──────────────────────────────────────────
    def test_user_reported_amazon_now_vs_tomorrow(self) -> None:
        block = (
            "Amazon Now\n"
            "₹1,099.00\n"
            "FREE delivery in 10 minutes on orders over ₹149\n"
            "Ships from: Kay Kay Overseas Corporation QCom\n"
            "Sold by: Kay Kay Overseas Corporation QCom\n"
            "One-time purchase\n"
            "₹1,099.00\n"
            "Fulfilled\n"
            "FREE delivery Tomorrow, 31 May. Details\n"
            "Or fastest delivery Tomorrow 6 am - 10 am.\n"
        )
        display, dt = self._earliest(block)
        self.assertIn("10 Minutes", display)
        self.assertEqual(dt, datetime(2026, 5, 30, 14, 10))

    # ── Per-priority ordering ───────────────────────────────────────────────
    def test_10min_beats_tomorrow(self) -> None:
        text = "FREE delivery in 10 minutes\nFREE delivery Tomorrow, 31 May"
        display, dt = self._earliest(text)
        self.assertIn("10 Minutes", display)
        self.assertEqual(dt, datetime(2026, 5, 30, 14, 10))

    def test_2hours_beats_tomorrow(self) -> None:
        text = "Amazon Fresh Delivery in 2 hours\nFREE delivery Tomorrow, 31 May"
        display, dt = self._earliest(text)
        self.assertIn("2 Hours", display)
        self.assertEqual(dt, datetime(2026, 5, 30, 16, 0))

    def test_today_beats_tomorrow(self) -> None:
        text = "Get it Today\nFREE delivery Tomorrow, 31 May"
        display, dt = self._earliest(text)
        self.assertIn("Today", display)
        self.assertEqual(dt.date(), datetime(2026, 5, 30).date())

    def test_today_beats_june2(self) -> None:
        text = "Get it Today\nDelivery by 2 June"
        display, dt = self._earliest(text)
        self.assertIn("Today", display)

    def test_tomorrow_beats_june2(self) -> None:
        text = "FREE delivery Tomorrow, 31 May\nDelivery by 2 June"
        display, dt = self._earliest(text)
        self.assertIn("Tomorrow", display)
        self.assertEqual(dt, datetime(2026, 5, 31, 12, 0))

    def test_scheduled_window_beats_tomorrow_full_day(self) -> None:
        # Today's "6 PM - 10 PM" still resolves to today-noon, which beats tomorrow.
        text = "Today 6 PM - 10 PM\nFREE delivery Tomorrow, 31 May"
        display, dt = self._earliest(text)
        self.assertIn("Today", display)

    # ── Amazon channel coverage ─────────────────────────────────────────────
    def test_amazon_now_minutes(self) -> None:
        display, dt = self._earliest("Amazon Now FREE delivery in 10 minutes")
        self.assertTrue(display.startswith("Amazon Now"), display)

    def test_amazon_fresh_hours(self) -> None:
        # "Amazon Fresh" hint takes effect via inference.
        display, dt = self._earliest("Amazon Fresh Delivery in 2 hours")
        self.assertTrue(display.startswith("Amazon Fresh"), display)

    def test_prime_weekday(self) -> None:
        display, dt = self._earliest("Prime delivery Monday")
        self.assertTrue(display.startswith("Prime"), display)
        self.assertEqual(dt, datetime(2026, 6, 1, 12, 0))

    def test_standard_marketplace(self) -> None:
        display, dt = self._earliest("FREE delivery Tomorrow, 31 May")
        self.assertTrue(display.startswith("Standard"), display)

    # ── Edge cases ──────────────────────────────────────────────────────────
    def test_multiple_blocks_all_considered(self) -> None:
        text = (
            "Block A: FREE delivery Tomorrow, 31 May\n"
            "Block B: Delivery by 2 June\n"
            "Block C: FREE delivery in 30 mins on orders over ₹149\n"
            "Block D: Get it Today\n"
        )
        display, dt = self._earliest(text)
        self.assertIn("30 Mins", display)
        self.assertEqual(dt, datetime(2026, 5, 30, 14, 30))

    def test_no_delivery_returns_not_available(self) -> None:
        display, dt = self._earliest("Currently unavailable.")
        self.assertEqual(display, "Not Available")
        self.assertIsNone(dt)

    def test_empty_text(self) -> None:
        display, dt = self._earliest("")
        self.assertEqual(display, "Not Available")
        self.assertIsNone(dt)

    def test_minutes_abbreviation_in_buy_box(self) -> None:
        # Regression: "30 mins" used to be silently dropped.
        text = "FREE delivery in 30 mins\nFREE delivery Tomorrow, 31 May"
        display, dt = self._earliest(text)
        self.assertIn("30 Mins", display)
        self.assertEqual(dt, datetime(2026, 5, 30, 14, 30))


class FakeElement:
    """Stand-in for a Selenium WebElement for unit tests.

    Stores:
      * `_descendants` — `{css_selector: [FakeElement, …]}` so
        `find_elements(By.CSS_SELECTOR, sel)` returns matching descendants.
      * `_text` — `el.text` value.
      * `_id` — `el.get_attribute('id')` value.
      * `_attrs` — additional attributes (`textContent`, `innerText`, etc.).
    """

    def __init__(self, descendants_by_selector=None, text="", attrs=None):
        self._descendants = descendants_by_selector or {}
        self.text = text
        self._attrs = {"textContent": text, "innerText": text, **(attrs or {})}

    def find_elements(self, by, selector):
        return self._descendants.get(selector, [])

    def find_element(self, by, selector):
        match = self.find_elements(by, selector)
        if match:
            return match[0]
        raise LookupError(f"no element for selector={selector!r}")

    def get_attribute(self, name):
        return self._attrs.get(name)


class FakeDriver:
    """Stand-in for the Selenium driver — only implements ``find_element(By.ID, …)``."""

    def __init__(self, elements_by_id):
        self._by_id = elements_by_id

    def find_element(self, by, ident):
        if ident in self._by_id:
            return self._by_id[ident]
        raise KeyError(f"no element with id={ident}")


class BuyBoxScoringTests(unittest.TestCase):
    def test_full_buybox_scores_high(self) -> None:
        # CTA + price + quantity + sold-by + delivery = score 6
        el = FakeElement({
            "#add-to-cart-button, #buy-now-button, "
            "input[name='submit.add-to-cart'], input[name='submit.buy-now'], "
            "#one-click-button, #buyNow_feature_div input": [object()],
            "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, "
            "#priceblock_ourprice, #priceblock_dealprice, #priceblock_saleprice, "
            "#price_inside_buybox": [object()],
            "#quantity, #quantitySelect, [data-action='a-dropdown-button']": [object()],
            "#tabular-buybox, .tabular-buybox-text, #merchant-info, "
            "#sellerProfileTriggerId": [object()],
            "[id*='DELIVERY_BLOCK'], #alm-delivery-message, "
            "#freshDeliveryMessage_feature_div, #qcomBuyBoxRow_feature_div, "
            "#almOfferDisplay_feature_div, #mbc, [id^='newAccordionRow_']": [object()],
        })
        self.assertGreaterEqual(_score_buy_box_candidate(el), 4)

    def test_empty_container_scores_zero(self) -> None:
        self.assertEqual(_score_buy_box_candidate(FakeElement({})), 0)

    def test_oos_buybox_still_scores(self) -> None:
        # OOS items have no CTA but have price + availability + sold-by.
        el = FakeElement({
            "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, "
            "#priceblock_ourprice, #priceblock_dealprice, #priceblock_saleprice, "
            "#price_inside_buybox": [object()],
            "#availability, #outOfStock, #exports-desktop-out-of-stock-message": [object()],
            "#tabular-buybox, .tabular-buybox-text, #merchant-info, "
            "#sellerProfileTriggerId": [object()],
        })
        self.assertGreaterEqual(_score_buy_box_candidate(el), 2)


class BuyBoxFinderTests(unittest.TestCase):
    def _log(self):
        import logging as _log
        log = _log.getLogger("test")
        log.addHandler(_log.NullHandler())
        return log

    def test_picks_highest_scoring_candidate(self) -> None:
        cta_only = FakeElement({
            "#add-to-cart-button, #buy-now-button, "
            "input[name='submit.add-to-cart'], input[name='submit.buy-now'], "
            "#one-click-button, #buyNow_feature_div input": [object()],
        })
        full_buybox = FakeElement({
            "#add-to-cart-button, #buy-now-button, "
            "input[name='submit.add-to-cart'], input[name='submit.buy-now'], "
            "#one-click-button, #buyNow_feature_div input": [object()],
            "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, "
            "#priceblock_ourprice, #priceblock_dealprice, #priceblock_saleprice, "
            "#price_inside_buybox": [object()],
            "#tabular-buybox, .tabular-buybox-text, #merchant-info, "
            "#sellerProfileTriggerId": [object()],
        })
        driver = FakeDriver({
            "buybox": cta_only,           # score 2
            "rightCol": full_buybox,      # score 4 — should win
        })
        result = find_buy_box(driver, self._log())
        self.assertIs(result, full_buybox)

    def test_returns_none_when_no_candidate(self) -> None:
        driver = FakeDriver({})
        self.assertIsNone(find_buy_box(driver, self._log()))


class FalsePositiveScopeTests(unittest.TestCase):
    """Document WHY scoping matters: feeding the parser unscoped page text
    produces wrong answers. These tests pin the contract: callers must pass
    buy-box-scoped text only.
    """

    BUY_BOX_ONLY = (
        "Amazon Now\n"
        "FREE delivery in 10 minutes on orders over ₹149\n"
        "Add to Cart  Buy Now\n"
        "Sold by: Kay Kay Overseas\n"
    )

    REVIEW_NOISE = (
        "Customer reviews\n"
        "★★★★★ Fast shipping! Today my package arrived on time.\n"
        "★★★★☆ I ordered last Monday and it came in 3 days.\n"
        "Frequently bought together: Get yours today!\n"
    )

    def test_buybox_only_gives_correct_answer(self) -> None:
        r = extract_earliest_delivery_from_text(self.BUY_BOX_ONLY, None, NOW)
        self.assertIn("10 Minutes", r["earliest_display"])
        self.assertEqual(r["earliest_dt"], datetime(2026, 5, 30, 14, 10))

    def test_unscoped_text_produces_false_positive(self) -> None:
        # Combined buy box + reviews — the review text's "today" / "Monday"
        # would race the real promise. The parser does NOT know which text
        # came from the buy box; it's the caller's job to scope.
        combined = self.BUY_BOX_ONLY + "\n\n" + self.REVIEW_NOISE
        r = extract_earliest_delivery_from_text(combined, None, NOW)
        # 10 Minutes still wins here because (now + 10 min) is the earliest
        # possible time of day. The dangerous case is when the buy-box promise
        # is "Tomorrow, 31 May" and the review says "Today" — then the false
        # positive WINS:
        from_review_only = (
            "FREE delivery Tomorrow, 31 May\n"  # buy box says tomorrow
            + self.REVIEW_NOISE
        )
        r2 = extract_earliest_delivery_from_text(from_review_only, None, NOW)
        # Bug-by-design when scoping is skipped: "today" from a review wins.
        self.assertIn("Today", r2["earliest_display"])
        # And the documented contract: scoped buy-box text gives the right one.
        r3 = extract_earliest_delivery_from_text(
            "FREE delivery Tomorrow, 31 May", None, NOW
        )
        self.assertIn("Tomorrow", r3["earliest_display"])


class NewDisplayBuilderTests(unittest.TestCase):
    """The tiered extractor's user-confirmed output format."""

    def test_clean_strips_free_delivery_prefix(self) -> None:
        self.assertEqual(
            _clean_delivery_text("FREE delivery in 10 minutes on orders over ₹149"),
            "10 Minutes",
        )

    def test_clean_strips_or_fastest_prefix(self) -> None:
        self.assertEqual(
            _clean_delivery_text("Or fastest delivery Tomorrow, 31 May. Details"),
            "Tomorrow, 31 May",
        )

    def test_clean_strips_get_it_by_prefix(self) -> None:
        self.assertEqual(_clean_delivery_text("Get it by Today"), "Today")
        self.assertEqual(
            _clean_delivery_text("Get it by Monday, June 3"),
            "Monday, June 3",
        )

    def test_amazon_now_display_format(self) -> None:
        # No "(Free)" suffix in the new format.
        self.assertEqual(
            _build_amazon_now_display("FREE delivery in 10 minutes"),
            "Amazon Now – 10 Minutes",
        )

    def test_standard_display_no_channel_prefix(self) -> None:
        self.assertEqual(
            _build_standard_display("FREE delivery Tomorrow, 31 May. Details"),
            "Tomorrow, 31 May",
        )

    def test_standard_display_explicit_date(self) -> None:
        self.assertEqual(_build_standard_display("Delivery by 2 June"), "2 June")

    def test_standard_display_weekday(self) -> None:
        self.assertEqual(
            _build_standard_display("Prime delivery Monday, June 3"),
            "Prime Delivery Monday, June 3",
        )


class _TierLogger:
    """Discard-everything logger so the tier helpers don't blow up on log calls."""

    def info(self, *_a, **_k): pass
    def warning(self, *_a, **_k): pass
    def debug(self, *_a, **_k): pass
    def error(self, *_a, **_k): pass


class Tier1AmazonNowTests(unittest.TestCase):
    """Tier 1 short-circuits as soon as an Amazon Now container has text."""

    def test_alm_delivery_message_short_circuits(self) -> None:
        alm = FakeElement(text="FREE delivery in 10 minutes on orders over ₹149")
        scope = FakeElement({
            _AMAZON_NOW_SELECTOR: [alm],
            "img[alt*='Amazon Now' i]": [],
        })
        result = _extract_amazon_now(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_display"], "Amazon Now – 10 Minutes")
        self.assertTrue(result["is_free"])

    def test_returns_none_when_no_amazon_now(self) -> None:
        scope = FakeElement({
            _AMAZON_NOW_SELECTOR: [],
            "img[alt*='Amazon Now' i]": [],
        })
        result = _extract_amazon_now(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNone(result)

    def test_skips_unparseable_amazon_now_element(self) -> None:
        # Amazon Now container present but text doesn't have a parseable promise.
        empty_msg = FakeElement(text="Amazon Now is unavailable in your area")
        scope = FakeElement({
            _AMAZON_NOW_SELECTOR: [empty_msg],
            "img[alt*='Amazon Now' i]": [],
        })
        result = _extract_amazon_now(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNone(result)

    def test_picks_first_parseable_amazon_now_element(self) -> None:
        empty = FakeElement(text="Amazon Now logo")
        real = FakeElement(text="FREE delivery in 30 mins")
        scope = FakeElement({
            _AMAZON_NOW_SELECTOR: [empty, real],
            "img[alt*='Amazon Now' i]": [],
        })
        result = _extract_amazon_now(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_display"], "Amazon Now – 30 Mins")


class Tier2StandardDeliveryTests(unittest.TestCase):
    """Tier 2 reads all standard slots and returns the earliest."""

    def test_primary_only(self) -> None:
        primary = FakeElement(text="FREE delivery Tomorrow, 31 May. Details")
        scope = FakeElement({_STANDARD_DELIVERY_SELECTOR: [primary]})
        result = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_display"], "Tomorrow, 31 May")
        self.assertTrue(result["is_free"])

    def test_picks_earlier_of_primary_and_secondary(self) -> None:
        # PRIMARY = free + slower (Wed 3 June), SECONDARY = paid + faster (Sun 31 May).
        primary = FakeElement(text="FREE delivery Wednesday, 3 June")
        secondary = FakeElement(
            text="Or fastest delivery Sunday, 31 May. ₹40 shipping"
        )
        scope = FakeElement({_STANDARD_DELIVERY_SELECTOR: [primary, secondary]})
        result = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        # Earliest is the paid SECONDARY option per user spec.
        self.assertEqual(result["earliest_display"], "Sunday, 31 May")
        self.assertFalse(result["is_free"])

    def test_picks_earlier_when_one_element_has_both(self) -> None:
        # #deliveryBlockMessage wraps both children and its text contains both.
        wrapper = FakeElement(
            text="FREE delivery Wednesday, 3 June. Or fastest delivery Sunday, 31 May"
        )
        scope = FakeElement({_STANDARD_DELIVERY_SELECTOR: [wrapper]})
        result = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_display"], "Sunday, 31 May")

    def test_returns_none_when_no_parseable_text(self) -> None:
        scope = FakeElement({_STANDARD_DELIVERY_SELECTOR: []})
        result = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNone(result)

    def test_dedupes_same_phrase_in_parent_and_child(self) -> None:
        # Parent wraps a single child with identical text.
        text = "FREE delivery Tomorrow, 31 May"
        parent = FakeElement(text=text)
        child = FakeElement(text=text)
        scope = FakeElement({_STANDARD_DELIVERY_SELECTOR: [parent, child]})
        result = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        # Only one entry in all_options after dedup.
        self.assertEqual(len(result["all_options"]), 1)
        self.assertEqual(result["earliest_display"], "Tomorrow, 31 May")


class Tier3FallbackTests(unittest.TestCase):
    """Tier 3 runs only when Tiers 1+2 missed."""

    def test_extracts_amazon_now_from_innertext(self) -> None:
        buy_box = FakeElement(
            attrs={"innerText": "Add to Cart\nFREE delivery in 10 minutes on orders over ₹149"},
        )
        result = _extract_buybox_fallback(buy_box, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_display"], "Amazon Now – 10 Minutes")

    def test_extracts_standard_from_innertext(self) -> None:
        buy_box = FakeElement(
            attrs={"innerText": "Add to Cart\nFREE delivery Tomorrow, 31 May"},
        )
        result = _extract_buybox_fallback(buy_box, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_display"], "Tomorrow, 31 May")

    def test_returns_none_when_empty(self) -> None:
        buy_box = FakeElement()
        result = _extract_buybox_fallback(buy_box, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNone(result)


class DealBadgeRejectionTests(unittest.TestCase):
    """Promotional badges ("Today's Deal", "Limited Time Deal", "Deal of the
    Day") must not be parsed as delivery dates. This is the regression for the
    "Standard – Todays Deal" Excel output the vendor reported.
    """

    def test_filter_drops_deal_badge_line(self) -> None:
        text = "Today's Deal\nFREE delivery Tomorrow, 31 May"
        filtered = _filter_delivery_lines(text)
        self.assertNotIn("Today's Deal", filtered)
        self.assertIn("FREE delivery Tomorrow", filtered)

    def test_filter_drops_only_badge_buybox(self) -> None:
        # Pathological: buy box contains nothing but promotional badges.
        text = (
            "Today's Deal\n"
            "Limited Time Offer\n"
            "Deal of the Day\n"
            "Hurry, today only!\n"
        )
        self.assertEqual(_filter_delivery_lines(text), "")

    def test_filter_keeps_real_delivery_promises(self) -> None:
        for promise in [
            "FREE delivery Tomorrow, 31 May",
            "Or fastest delivery Today by 8 PM",
            "Get it by Monday, June 3",
            "Ships from Amazon",
            "Delivery in 10 minutes on orders over ₹149",
        ]:
            self.assertEqual(_filter_delivery_lines(promise), promise, promise)

    def test_tier3_rejects_deal_badge(self) -> None:
        # Tier 3 must NOT return "Today" when the only "today" in the buy box
        # is the "Today's Deal" badge.
        buy_box = FakeElement(attrs={"innerText": (
            "Today's Deal\n"
            "-20%\n"
            "₹1,099\n"
            "M.R.P.: ₹1,500\n"
            "Quantity: 1\n"
            "Add to Cart\n"
            "Buy Now\n"
            "Sold by: Cloudtail India\n"
        )})
        result = _extract_buybox_fallback(buy_box, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNone(result)

    def test_tier3_picks_real_delivery_over_badge(self) -> None:
        # Real promise present alongside a deal badge → real promise wins.
        buy_box = FakeElement(attrs={"innerText": (
            "Today's Deal\n"
            "-20%\n"
            "₹1,099\n"
            "FREE delivery Tomorrow, 31 May. Details\n"
            "Add to Cart\n"
        )})
        result = _extract_buybox_fallback(buy_box, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_display"], "Tomorrow, 31 May")


class TieredFlowIntegrationTests(unittest.TestCase):
    """Tier 1 always wins over Tier 2 when present (user-confirmed: trust Amazon)."""

    def test_amazon_now_beats_standard_even_if_standard_faster(self) -> None:
        # Pathological case the user accepted: trust Amazon's channel over
        # arithmetic. Standard says "Today" (today @ 20:00); Amazon Now says
        # "2 hours" (16:00). Tier 1 short-circuits before Tier 2 ever runs.
        alm = FakeElement(text="FREE delivery in 2 hours")
        primary = FakeElement(text="FREE delivery Today")
        scope = FakeElement({
            _AMAZON_NOW_SELECTOR: [alm],
            "img[alt*='Amazon Now' i]": [],
            _STANDARD_DELIVERY_SELECTOR: [primary],
        })
        t1 = _extract_amazon_now(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(t1)
        self.assertEqual(t1["earliest_display"], "Amazon Now – 2 Hours")
        # Tier 2 isn't called in the real flow because Tier 1 returned early,
        # but for documentation: it WOULD return "Today – 30 May" if asked
        # (relative-day text has the resolved date appended).
        t2 = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertEqual(t2["earliest_display"], "Today – 30 May")


class CutoffStrippingTests(unittest.TestCase):
    """Regression for "Order within X hrs Y mins" cutoff countdowns leaking
    into the delivery date column.
    """

    def test_order_within_stripped(self) -> None:
        text = "Or fastest delivery Tomorrow 8 am - 12 pm. Order within 2 hrs 37 mins"
        self.assertEqual(
            _normalize_delivery_text(text),
            "Or fastest delivery Tomorrow 8 am - 12 pm",
        )

    def test_order_within_stripped_no_period(self) -> None:
        text = "Tomorrow 8 am - 12 pm Order within 2 hrs 37 mins"
        out = _normalize_delivery_text(text)
        self.assertNotIn("Order within", out)
        self.assertNotIn("2 hrs", out)

    def test_ends_in_stripped(self) -> None:
        out = _normalize_delivery_text("Lightning deal Ends in 4 hours")
        self.assertNotIn("4 hours", out)

    def test_expires_in_stripped(self) -> None:
        out = _normalize_delivery_text("Expires in 30 minutes")
        self.assertNotIn("30 minutes", out)

    def test_left_remaining_stripped(self) -> None:
        self.assertNotIn("30 mins", _normalize_delivery_text("Hurry — 30 mins left"))
        self.assertNotIn("2 hours", _normalize_delivery_text("2 hours remaining"))

    def test_real_delivery_in_preserved(self) -> None:
        # "delivery in X" is a real promise — must NOT be stripped.
        self.assertIn(
            "10 minutes",
            _normalize_delivery_text("FREE delivery in 10 minutes"),
        )

    def test_cutoff_only_returns_no_candidate(self) -> None:
        # Parser sees only the cutoff after stripping — nothing parseable.
        for s in [
            "Order within 1 hr 30 mins",
            "Ends in 4 hours",
            "30 mins left",
            "2 hours remaining",
        ]:
            normalized = _normalize_delivery_text(s)
            dt = _parse_delivery_phrase(normalized, NOW) if normalized else None
            self.assertIsNone(dt, f"Should be None for {s!r}, got {dt}")

    def test_extractor_picks_tomorrow_over_cutoff(self) -> None:
        # The full bug repro: SECONDARY text with cutoff.
        text = "Or fastest delivery Tomorrow 8 am - 12 pm. Order within 2 hrs 37 mins"
        r = extract_earliest_delivery_from_text(text, "Standard", NOW)
        # earliest_dt is tomorrow noon, not now + 2 hours.
        self.assertEqual(r["earliest_dt"], datetime(2026, 5, 31, 12, 0))
        # No "2 hrs" candidate exists in the option list.
        for opt in r["all_options"]:
            self.assertNotIn("2 Hrs", opt["display_text"])
            self.assertNotIn("37 Mins", opt["display_text"])


class DateInjectionTests(unittest.TestCase):
    """Format B: when raw text has 'Tomorrow' / 'Today' / weekday without an
    explicit date, append ' – {day} {Mon}' computed from the resolved datetime.
    """

    def test_format_delivery_date(self) -> None:
        # No leading zero on the day, short month abbreviation. Windows-safe.
        self.assertEqual(_format_delivery_date(datetime(2026, 5, 31)), "31 May")
        self.assertEqual(_format_delivery_date(datetime(2026, 6, 2)), "2 Jun")
        self.assertEqual(_format_delivery_date(datetime(2026, 1, 9)), "9 Jan")

    def test_maybe_append_no_dt(self) -> None:
        self.assertEqual(_maybe_append_date("Tomorrow", None), "Tomorrow")

    def test_maybe_append_already_dated(self) -> None:
        # Already has month name — don't double up.
        self.assertEqual(
            _maybe_append_date("Tomorrow, 31 May", datetime(2026, 5, 31)),
            "Tomorrow, 31 May",
        )

    def test_maybe_append_no_relative_word(self) -> None:
        # Pure date phrase, no relative-day word — nothing to anchor.
        self.assertEqual(
            _maybe_append_date("2 June", datetime(2026, 6, 2)),
            "2 June",
        )

    def test_maybe_append_tomorrow(self) -> None:
        self.assertEqual(
            _maybe_append_date("Tomorrow 8 AM - 12 PM", datetime(2026, 5, 31)),
            "Tomorrow 8 AM - 12 PM – 31 May",
        )

    def test_maybe_append_today(self) -> None:
        self.assertEqual(
            _maybe_append_date("Today", datetime(2026, 5, 30, 20, 0)),
            "Today – 30 May",
        )

    def test_maybe_append_weekday(self) -> None:
        self.assertEqual(
            _maybe_append_date("Monday", datetime(2026, 6, 1)),
            "Monday – 1 Jun",
        )

    def test_build_standard_display_with_dt(self) -> None:
        # The user's exact failing scenario, end-to-end through the builder.
        out = _build_standard_display(
            "Or fastest delivery Tomorrow 8 am - 12 pm",
            datetime(2026, 5, 31, 12, 0),
        )
        self.assertEqual(out, "Tomorrow 8 AM - 12 PM – 31 May")

    def test_build_standard_display_preserves_existing_date(self) -> None:
        out = _build_standard_display(
            "FREE delivery Tomorrow, 31 May. Details",
            datetime(2026, 5, 31, 12, 0),
        )
        self.assertEqual(out, "Tomorrow, 31 May")

    def test_am_pm_uppercased(self) -> None:
        # .title() would produce "Am"/"Pm" — we want "AM"/"PM".
        self.assertIn("AM", _clean_delivery_text("Tomorrow 8 am - 12 pm"))
        self.assertIn("PM", _clean_delivery_text("Tomorrow 8 am - 12 pm"))
        self.assertNotIn("Am", _clean_delivery_text("Tomorrow 8 am - 12 pm"))
        self.assertNotIn("Pm", _clean_delivery_text("Tomorrow 8 am - 12 pm"))


class EndToEndCutoffFixTests(unittest.TestCase):
    """Full Tier 2 flow with the user-reported failing text."""

    def test_secondary_slot_with_cutoff(self) -> None:
        primary = FakeElement(text="FREE delivery Tomorrow, 31 May. Details")
        secondary = FakeElement(text=(
            "Or fastest delivery Tomorrow 8 am - 12 pm. "
            "Order within 2 hrs 37 mins"
        ))
        scope = FakeElement({_STANDARD_DELIVERY_SELECTOR: [primary, secondary]})
        result = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        # The earliest_display must be one of the real promises, NEVER "2 Hrs".
        self.assertNotIn("2 Hrs", result["earliest_display"])
        self.assertNotIn("37 Mins", result["earliest_display"])
        # Both candidates resolve to tomorrow noon. Either wins the dedup.
        self.assertIn("Tomorrow", result["earliest_display"])

    def test_only_secondary_with_cutoff(self) -> None:
        # If only SECONDARY is present (PRIMARY missing), we still get the
        # right promise, with the computed date appended.
        secondary = FakeElement(text=(
            "Or fastest delivery Tomorrow 8 am - 12 pm. "
            "Order within 2 hrs 37 mins"
        ))
        scope = FakeElement({_STANDARD_DELIVERY_SELECTOR: [secondary]})
        result = _extract_standard_delivery(scope, NOW, _TierLogger(), "B0X", "110001")
        self.assertIsNotNone(result)
        self.assertEqual(
            result["earliest_display"], "Tomorrow 8 AM - 12 PM – 31 May"
        )


if __name__ == "__main__":
    unittest.main()
