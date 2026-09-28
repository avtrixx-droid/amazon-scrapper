"""Plain data carriers shared between fetcher, pipeline, and storage layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedProduct:
    """Result of parser.parse_product_page() — never raises; missing fields
    are None/PARSE_STATUS_* so the pipeline can decide OOS vs FAILED vs OK
    without the parser needing to know about run/DB concepts."""

    title: Optional[str] = None
    price: Optional[float] = None
    mrp: Optional[float] = None
    seller: Optional[str] = None
    availability_raw: Optional[str] = None
    page_kind: str = "unknown"  # product | captcha | not_found | blocked | unknown
    is_in_stock: Optional[bool] = None


@dataclass
class FetchResult:
    """What http_client.fetch_product_page() hands to the parser/pipeline."""

    asin: str
    status_code: Optional[int]
    html: Optional[str]
    error: Optional[str] = None  # network-level error, e.g. "timeout", "connection_reset"
