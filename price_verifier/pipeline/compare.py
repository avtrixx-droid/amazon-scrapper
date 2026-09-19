"""
compare.py — pure price-comparison logic, deliberately dependency-free so it
can be unit-tested without touching the DB, HTTP client, or event loop.

Spec section 10 flags match tolerance as an open question; this implements
both an absolute (±₹) and a percentage (±%) tolerance, combined as "either
one passing is a match" — whichever the team confirms, both are already
wired as run settings (see config.DEFAULT_TOLERANCE_ABS/_PCT), so answering
that question later is a settings change, not a code change.
"""

from __future__ import annotations


def prices_match(expected: float, actual: float, tolerance_abs: float, tolerance_pct: float) -> bool:
    diff = abs(expected - actual)
    if tolerance_abs and diff <= tolerance_abs:
        return True
    if tolerance_pct and expected > 0 and (diff / expected) * 100 <= tolerance_pct:
        return True
    if not tolerance_abs and not tolerance_pct:
        return diff == 0
    return False
