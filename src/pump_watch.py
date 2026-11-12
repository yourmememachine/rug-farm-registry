"""Public, no-key pump.fun watchlist via DexScreener.

Fetches the latest boosted tokens from DexScreener's public API
(no key) and reports market cap, liquidity, 24h volume and 24h
change for the Solana ones, with a *watch* label only:

- ``collapsed``      — 24h change <= -70%
- ``thin_liquidity`` — liquidity < $1,000
- ``active``         — neither of the above

A watch label is NOT a rug verdict. Calling a token "rugged"
requires a dev wallet plus LP-removal / dev-sell evidence, which
this tool does not have — see ``src.labeller.label_outcome`` for
the real rug test. This tool never labels a token "rugged", never
invents a wallet, a mint, a holder count or an outcome, and never
touches the registry.

Endpoints (DexScreener public API, no key):
- ``GET https://api.dexscreener.com/token-boosts/latest/v1``
- ``GET https://api.dexscreener.com/tokens/v1/solana/<mints>``

Usage:
    python -m src.pump_watch [--limit 10] [--json]

All HTTP goes through an injectable ``httpx.Client`` so tests can
use ``httpx.MockTransport``. No live call is made in tests.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from src.http_client import make_client

TOKEN_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana"

COLLAPSE_WATCH_PCT = -70.0
THIN_LIQUIDITY_USD = 1000.0
DEFAULT_LIMIT = 10
MAX_MINTS_PER_REQUEST = 30


def watch_label(change_24h_pct: float | None, liquidity_usd: float | None) -> str:
    """Watch label only — never "rugged" (see module docstring)."""
    if change_24h_pct is not None and change_24h_pct <= COLLAPSE_WATCH_PCT:
        return "collapsed"
    if liquidity_usd is not None and liquidity_usd < THIN_LIQUIDITY_USD:
        return "thin_liquidity"
    return "active"


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_token_pair(pair: dict) -> dict | None:
    """Parse one DexScreener pair/token object into a watch entry.

    Handles both shapes the tokens endpoint can return: a pair
    object (``baseToken``, ``marketCap``, ``liquidity``, ``volume``,
    ``priceChange``) and a flat token object with the same fields
    at the top level. Returns None when the object has no mint.
    """
    if not isinstance(pair, dict):
        return None
    base = pair.get("baseToken") or {}
    mint = pair.get("tokenAddress") or base.get("address")
    if not mint:
        return None
    symbol = base.get("symbol") or pair.get("symbol") or "?"
    name = base.get("name") or pair.get("name")
    market_cap = _as_float(pair.get("marketCap") or pair.get("fdv"))
    liquidity = _as_float((pair.get("liquidity") or {}).get("usd"))
    volume_24h = _as_float((pair.get("volume") or {}).get("h24"))
    change_24h = _as_float((pair.get("priceChange") or {}).get("h24"))
    return {
        "mint": mint,
        "symbol": symbol,
        "name": name,
        "market_cap_usd": market_cap,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume_24h,
        "change_24h_pct": change_24h,
        "watch_label": watch_label(change_24h, liquidity),
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
    }


class PumpWatch:
    """No-key DexScreener watchlist for boosted Solana tokens."""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def _get_json(self, url: str) -> object:
        try:
            if self._client is not None:
                resp = self._client.get(url)
            else:
                with make_client(timeout=30.0) as client:
                    resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"DexScreener request failed ({url}): {exc}") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"DexScreener returned invalid JSON ({url})") from exc

    def fetch_boost_mints(self, limit: int = DEFAULT_LIMIT) -> list[str]:
        """Solana token addresses from the latest DexScreener boosts."""
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        data = self._get_json(TOKEN_BOOSTS_URL)
        if not isinstance(data, list):
            raise RuntimeError("DexScreener boosts response was not a list")
        mints: list[str] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            # Boosts cover all chains; keep Solana only.
            if item.get("chainId") not in (None, "solana"):
                continue
            mint = item.get("tokenAddress")
            if mint and mint not in seen:
                seen.add(mint)
                mints.append(mint)
            if len(mints) >= limit:
                break
        return mints

    def fetch_token_details(self, mints: list[str]) -> list[dict]:
        """Watch entries for the given mints, in request order."""
        entries: list[dict] = []
        for i in range(0, len(mints), MAX_MINTS_PER_REQUEST):
            chunk = mints[i : i + MAX_MINTS_PER_REQUEST]
            data = self._get_json(f"{TOKENS_URL}/{','.join(chunk)}")
            if not isinstance(data, list):
                raise RuntimeError("DexScreener tokens response was not a list")
            for item in data:
                entry = parse_token_pair(item)
                if entry is not None:
                    entries.append(entry)
        return entries

    def watch(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Latest boosted Solana tokens with watch labels."""
        mints = self.fetch_boost_mints(limit=limit)
        if not mints:
            return []
        return self.fetch_token_details(mints)


def format_entries(entries: list[dict]) -> str:
    """Human-readable watchlist, with the no-rug-verdict disclaimer."""
    if not entries:
        return "No boosted Solana tokens returned by DexScreener."
    lines = []
    for entry in entries:
        mc = entry["market_cap_usd"]
        liq = entry["liquidity_usd"]
        vol = entry["volume_24h_usd"]
        chg = entry["change_24h_pct"]
        lines.append(f"{entry['symbol']} ({entry['mint']})")
        lines.append(f"  market cap: ${mc:,.0f}" if mc is not None else "  market cap: n/a")
        lines.append(f"  liquidity: ${liq:,.0f}" if liq is not None else "  liquidity: n/a")
        lines.append(f"  24h volume: ${vol:,.0f}" if vol is not None else "  24h volume: n/a")
        lines.append(f"  24h change: {chg:+.1f}%" if chg is not None else "  24h change: n/a")
        lines.append(f"  watch: {entry['watch_label']}")
        lines.append(f"  {entry['dexscreener_url']}")
    lines.append("")
    lines.append(
        "Watch labels are not rug verdicts: 'collapsed' / "
        "'thin_liquidity' mean watch the token, not that its dev "
        "rugged it. A rug needs dev-wallet + LP/dev-sell evidence."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pump.fun watchlist (no key): latest boosted Solana tokens on DexScreener."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"How many boosted tokens to show (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the watchlist as JSON instead of text",
    )
    args = parser.parse_args(argv)

    if args.limit <= 0:
        print("ERROR: --limit must be a positive integer.", file=sys.stderr)
        return 2
    try:
        entries = PumpWatch().watch(limit=args.limit)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        print(format_entries(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
