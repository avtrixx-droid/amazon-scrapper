"""retry.py — pure backoff-delay math, kept separate from the retry loop
itself (that lives in runner.py, where it can see both the fetch outcome and
the parse outcome to decide retryable-or-not) so the timing curve is
independently testable."""

from __future__ import annotations

import random

from price_verifier import config


def backoff_delay(attempt: int, base: float = config.RETRY_BASE_DELAY_SECONDS) -> float:
    """attempt is 1-based (first retry = attempt 1). Exponential + jitter,
    same shape as scraper.py's @retry decorator."""
    return base * (2 ** (attempt - 1)) + random.uniform(0, base)
