"""Plain data carriers shared between fetcher, pipeline, and storage layers.

These are the contract between fetcher/ (produces them) and pipeline/
(consumes them). Add fields with defaults only — both sides are developed
and tested independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedProduct:
    """Result of parser.parse_product_page() — never raises; missing fields
    are None so the pipeline can decide the row's status without the parser
    knowing about run/DB concepts."""

    title: Optional[str] = None
    price: Optional[float] = None
    mrp: Optional[float] = None
    seller: Optional[str] = None           # None when the page doesn't show one — never guessed
    brand: Optional[str] = None            # from the page byline ("Visit the X Store" / "Brand: X")
    availability_raw: Optional[str] = None
    page_kind: str = "unknown"             # product | captcha | not_found | blocked | unknown
    is_in_stock: Optional[bool] = None     # True / False (explicit) / None (couldn't tell)
    no_featured_offer: bool = False        # product exists but no buy-box winner ("See All Buying Options")


@dataclass
class FetchResult:
    """What a fetcher (FetchSession or the Chrome fallback) hands to the
    parser/pipeline. Fetchers never raise — failures land in `error`."""

    asin: str
    status_code: Optional[int]
    html: Optional[str]
    error: Optional[str] = None   # network-level error: "timeout", "connection_error", "throttled_or_server_error", ...
    elapsed_ms: float = 0.0
    source: str = "http"          # "http" | "browser" — which fetcher produced this
