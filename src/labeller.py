"""Outcome labeller: was a mint rugged, an honest failure, or unclear?

Pure functions — no network, no state — so they are easy to unit-test.

Rules (documented thresholds, all in one place so reviewers can argue
about them in a PR instead of in code):
- RUGGED: market cap collapses >= 90% within 24h AND (LP removed OR
  dev sold >= 50% of its allocation), OR collapse >= 95% with a dev
  sell >= 20%, OR LP removed with collapse >= 80%.
- FAILED_BUT_HONEST: big collapse (>= 70%) but NO LP removal and dev
  sold < 20% — the token died, but there is no evidence the dev
  pulled the rug. Also: slow bleed over > 24h with no LP/dev event.
- INCONCLUSIVE: missing peak/current market cap, or none of the
  above patterns fits.
"""
from __future__ import annotations

from dataclasses import dataclass

COLLAPSE_RUG_PCT = 90.0
COLLAPSE_RUG_WITH_DEV_SELL_PCT = 95.0
COLLAPSE_LP_REMOVED_PCT = 80.0
COLLAPSE_HONEST_FAILURE_PCT = 70.0
DEV_SELL_RUG_PCT = 50.0
DEV_SELL_RUG_WITH_BIG_COLLAPSE_PCT = 20.0
DEV_SELL_HONEST_MAX_PCT = 20.0
FAST_WINDOW_MINUTES = 24 * 60


@dataclass
class MintEvent:
    peak_market_cap_usd: float | None = None
    current_market_cap_usd: float | None = None
    lp_removed: bool = False
    dev_sold_pct: float = 0.0  # % of the dev's allocation sold
    time_to_collapse_minutes: int | None = None


def collapse_pct(event: MintEvent) -> float | None:
    """% fall from peak market cap. None if peak is missing/zero."""
    if event.peak_market_cap_usd is None or event.peak_market_cap_usd <= 0:
        return None
    if event.current_market_cap_usd is None:
        return None
    return 100.0 * (1.0 - event.current_market_cap_usd / event.peak_market_cap_usd)


def label_outcome(event: MintEvent) -> str:
    """Return 'rugged' | 'failed_but_honest' | 'inconclusive'."""
    collapse = collapse_pct(event)
    if collapse is None:
        return "inconclusive"

    fast = (
        event.time_to_collapse_minutes is not None
        and event.time_to_collapse_minutes <= FAST_WINDOW_MINUTES
    )

    # --- Rugged -------------------------------------------------------
    if fast and collapse >= COLLAPSE_RUG_PCT and (
        event.lp_removed or event.dev_sold_pct >= DEV_SELL_RUG_PCT
    ):
        return "rugged"
    if collapse >= COLLAPSE_RUG_WITH_DEV_SELL_PCT and (
        event.dev_sold_pct >= DEV_SELL_RUG_WITH_BIG_COLLAPSE_PCT
    ):
        return "rugged"
    if event.lp_removed and collapse >= COLLAPSE_LP_REMOVED_PCT:
        return "rugged"

    # --- Failed but honest -------------------------------------------
    # A big collapse with no LP removal and only a small dev sell is an
    # honest failure — including a slow bleed over > 24h, which the
    # branch above already covers (it has no speed condition). A slow
    # bleed with a large dev sell is NOT honest: the dev sold, so it
    # falls through to "inconclusive" instead.
    if collapse >= COLLAPSE_HONEST_FAILURE_PCT and (
        not event.lp_removed and event.dev_sold_pct < DEV_SELL_HONEST_MAX_PCT
    ):
        return "failed_but_honest"

    return "inconclusive"


def label_from_dict(data: dict) -> str:
    """Convenience wrapper for JSON-shaped mint events."""
    return label_outcome(
        MintEvent(
            peak_market_cap_usd=data.get("peak_market_cap_usd"),
            current_market_cap_usd=data.get("current_market_cap_usd"),
            lp_removed=bool(data.get("lp_removed", False)),
            dev_sold_pct=float(data.get("dev_sell_pct", 0.0) or 0.0),
            time_to_collapse_minutes=data.get("time_to_collapse_minutes"),
        )
    )
