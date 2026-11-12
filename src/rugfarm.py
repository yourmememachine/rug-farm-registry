"""Single-file, stdlib-only client for the Open Rug-Farm Registry.

Copy this one file into your own project — it has no dependencies
beyond the Python standard library — point it at a copy of
``data/registry.json`` (or pass the loaded dict), and check dev
wallets and mints:

    from rugfarm import RugFarm

    rf = RugFarm()  # loads data/registry.json next to the repo
    rf.check_dev("SAMPLEDEV11111111111111111111111111111111")
    rf.check_mint("SAMPLEMNT1111111111111111111111111111111")
    rf.is_flagged_dev("SAMPLEDEV11111111111111111111111111111111")
    rf.watch(limit=5)  # live DexScreener boosts, no key

Rules (same as the rest of the repo, see ``src/registry.py``):

- A wallet is flagged ONLY with >= 2 rugged mints, each with
  on-chain evidence, and an unexpired entry. One rug =
  ``under_review``, never flagged.
- Watch labels are ``collapsed`` / ``thin_liquidity`` / ``active``
  only. A token is NEVER called "rugged" without a registry entry
  backed by dev-wallet + LP/dev-sell evidence.
- Sample data is clearly marked: every result carries
  ``sample_data`` (True while the registry is the shipped sample
  file). Sample wallets/mints are synthetic and are NOT accusations.
- Nothing here invents a wallet, a mint, a holder count or an
  outcome. An unknown address is reported as ``unknown`` — not a
  clean bill of health — and a failed DexScreener request falls
  back to registry-only data instead of faking market numbers.

Demo:
    python -m src.rugfarm <wallet-or-mint> [--json] [--registry PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

__all__ = [
    "RugFarm",
    "check_dev",
    "check_mint",
    "is_flagged_dev",
    "is_rugged_mint",
    "watch",
    "load_registry",
    "is_valid_solana_address",
    "watch_label",
    "main",
]

BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
MIN_RUGGED_MINTS_TO_FLAG = 2
DEFAULT_TTL_DAYS = 180
COLLAPSE_WATCH_PCT = -70.0
THIN_LIQUIDITY_USD = 1000.0
DEXSCREENER_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana"

# Exit codes, mirroring src/check_cli.py.
EXIT_NOT_FLAGGED = 0
EXIT_FLAGGED = 1
EXIT_INVALID = 2


def is_valid_solana_address(address: object) -> bool:
    """Solana addresses are 32-44 base58 characters (no 0, O, I, l)."""
    if not isinstance(address, str):
        return False
    return 32 <= len(address) <= 44 and all(c in BASE58_ALPHABET for c in address)


def watch_label(change_24h_pct: float | None, liquidity_usd: float | None) -> str:
    """Watch label only — never "rugged" (see module docstring)."""
    if change_24h_pct is not None and change_24h_pct <= COLLAPSE_WATCH_PCT:
        return "collapsed"
    if liquidity_usd is not None and liquidity_usd < THIN_LIQUIDITY_USD:
        return "thin_liquidity"
    return "active"


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def load_registry(path: str | Path) -> dict:
    """Load a registry JSON file.

    Raises ``FileNotFoundError`` for a missing file and
    ``ValueError`` for malformed JSON or a registry whose
    ``min_rugged_mints_to_flag`` is not 2. Entry-level validation
    lives in ``src/registry.py``; the SDK re-checks the flagging
    rule itself before trusting any entry (see ``_is_flagged``).
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("registry: must be a JSON object")
    if data.get("min_rugged_mints_to_flag", MIN_RUGGED_MINTS_TO_FLAG) != MIN_RUGGED_MINTS_TO_FLAG:
        raise ValueError("registry: min_rugged_mints_to_flag must be 2")
    if not isinstance(data.get("entries", []), list):
        raise ValueError("registry: entries must be a list")
    return data


def _urllib_fetch_json(url: str) -> object:
    """Default DexScreener fetcher (stdlib only, no key)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "rug-farm-registry-sdk/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
        raise RuntimeError(f"DexScreener request failed ({url}): {exc}") from exc


def _parse_token_pair(pair: dict) -> dict | None:
    """Parse one DexScreener pair/token object into a market-data dict.

    Returns None when the object has no mint. Handles both the pair
    shape (``baseToken`` / ``marketCap`` / ``liquidity`` / ``volume`` /
    ``priceChange``) and a flat token object. Mirrors
    ``src/pump_watch.parse_token_pair``.
    """
    if not isinstance(pair, dict):
        return None
    base = pair.get("baseToken") or {}
    mint = pair.get("tokenAddress") or base.get("address")
    if not mint:
        return None
    market_cap = _as_float(pair.get("marketCap") or pair.get("fdv"))
    liquidity = _as_float((pair.get("liquidity") or {}).get("usd"))
    volume_24h = _as_float((pair.get("volume") or {}).get("h24"))
    change_24h = _as_float((pair.get("priceChange") or {}).get("h24"))
    return {
        "mint": mint,
        "symbol": base.get("symbol") or pair.get("symbol") or "?",
        "name": base.get("name") or pair.get("name"),
        "market_cap_usd": market_cap,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume_24h,
        "change_24h_pct": change_24h,
        "watch_label": watch_label(change_24h, liquidity),
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
    }


def _rugged_mints(entry: dict) -> list[dict]:
    return [
        m
        for m in entry.get("rugged_mints", [])
        if isinstance(m, dict) and m.get("outcome") == "rugged"
    ]


def _is_expired(entry: dict, today: date) -> bool:
    last_seen = _parse_date(entry.get("last_seen"))
    ttl = entry.get("ttl_days", DEFAULT_TTL_DAYS)
    if last_seen is None or not isinstance(ttl, int):
        return True
    return today > last_seen + timedelta(days=ttl)


def _has_evidence(mint: dict) -> bool:
    evidence = mint.get("evidence")
    return (
        isinstance(evidence, dict)
        and bool(evidence)
        and all(
            isinstance(url, str) and url.startswith(("http://", "https://"))
            for url in evidence.values()
        )
    )


def _is_flagged(entry: dict, today: date) -> bool:
    """Flagged = status flagged + >=2 rugged mints, each with evidence,
    + not expired. Re-checked here instead of trusting ``status``."""
    if entry.get("status") != "flagged":
        return False
    rugged = _rugged_mints(entry)
    if len(rugged) < MIN_RUGGED_MINTS_TO_FLAG:
        return False
    if not all(_has_evidence(m) for m in rugged):
        return False
    return not _is_expired(entry, today)


class RugFarm:
    """Registry-backed dev/mint checker + no-key DexScreener watch.

    ``registry`` is a dict as loaded by :func:`load_registry`
    (pass it when embedding the SDK without a file on disk);
    ``registry_path`` loads that file; otherwise the SDK looks for
    ``data/registry.json`` next to the repo root (parent of this
    file's directory) and then in the current working directory,
    falling back to an empty registry when no file is found.

    ``fetcher`` is a callable ``(url) -> parsed JSON`` used for all
    DexScreener calls — inject a stub in tests to avoid the network.
    ``today`` fixes the date used for TTL expiry checks (defaults
    to today); pass an explicit date in tests so they do not rot.
    """

    def __init__(
        self,
        registry: dict | None = None,
        registry_path: str | Path | None = None,
        fetcher=None,
        today: date | None = None,
    ):
        if registry is not None:
            self.data = registry
        elif registry_path is not None:
            self.data = load_registry(registry_path)
        else:
            self.data = self._load_default_registry()
        self._fetcher = fetcher or _urllib_fetch_json
        self.today = today or date.today()

    @staticmethod
    def _load_default_registry() -> dict:
        candidates = [
            Path(__file__).resolve().parent.parent / "data" / "registry.json",
            Path.cwd() / "data" / "registry.json",
        ]
        for path in candidates:
            if path.is_file():
                return load_registry(path)
        return {"sample_data": True, "entries": []}

    @property
    def sample_data(self) -> bool:
        """True while the loaded registry is the shipped sample file."""
        return bool(self.data.get("sample_data"))

    # -- registry lookups -------------------------------------------------

    def _get_wallet_entry(self, wallet: str) -> dict | None:
        for entry in self.data.get("entries", []):
            if isinstance(entry, dict) and entry.get("wallet") == wallet:
                return entry
        return None

    def _find_mint(self, mint: str) -> tuple[dict | None, dict | None]:
        """Return (entry, mint-record) for a mint in the registry."""
        for entry in self.data.get("entries", []):
            if not isinstance(entry, dict):
                continue
            for record in entry.get("rugged_mints", []):
                if isinstance(record, dict) and record.get("mint") == mint:
                    return entry, record
        return None, None

    # -- dev checks --------------------------------------------------------

    def check_dev(self, wallet: str, mint: str | None = None, today: date | None = None) -> dict:
        """Check a dev wallet.

        Returns a dict with ``verdict`` (``flagged`` /
        ``under_review`` / ``not_flagged`` / ``unknown``),
        ``rugged_mint_count``, ``rugged_mints`` (each with evidence
        links), ``reason`` / ``reasons``, ``exit_code`` (1 = flagged,
        0 = anything else — same mapping as ``src/check_cli.py``)
        and ``sample_data``. Raises ``ValueError`` for an invalid
        wallet or mint address. ``mint`` (optional) is checked
        against the dev's rugged mints: the verdict is about the
        dev, and ``mint_in_registry`` says whether that mint is
        actually one of the dev's rugged mints.
        """
        if not is_valid_solana_address(wallet):
            raise ValueError(f"'{wallet}' is not a valid Solana address")
        if mint is not None and not is_valid_solana_address(mint):
            raise ValueError(f"mint '{mint}' is not a valid Solana address")
        today = today or self.today
        entry = self._get_wallet_entry(wallet)
        if entry is None:
            result = {
                "dev_wallet": wallet,
                "verdict": "unknown",
                "risk_level": "unknown",
                "flagged": False,
                "rugged_mint_count": 0,
                "rugged_mints": [],
                "cluster_id": None,
                "mint": mint,
                "mint_in_registry": None if mint is None else False,
                "reason": "No record for this dev wallet in the registry.",
                "reasons": ["No record for this dev wallet in the registry."],
                "evidence": [],
                "sample_data": self.sample_data,
                "exit_code": EXIT_NOT_FLAGGED,
            }
            return result

        rugged = _rugged_mints(entry)
        rugged_mints = [
            {
                "mint": m.get("mint"),
                "symbol": m.get("symbol"),
                "outcome": m.get("outcome"),
                "collapse_pct": m.get("collapse_pct"),
                "peak_market_cap_usd": m.get("peak_market_cap_usd"),
                "current_market_cap_usd": m.get("current_market_cap_usd"),
                "evidence": m.get("evidence", {}),
            }
            for m in rugged
        ]
        evidence = [m.get("evidence", {}) for m in rugged]
        mint_in_registry = (
            None
            if mint is None
            else any(m.get("mint") == mint for m in rugged)
        )
        mint_note = (
            None
            if mint_in_registry in (None, True)
            else f"Mint {mint} is not one of this dev's rugged mints in "
            "the registry — this verdict is about the dev, not that mint."
        )
        reasons: list[str] = []
        if _is_expired(entry, today):
            verdict, risk_level = "not_flagged", "expired"
            reasons.append(
                f"Entry expired (last seen {entry.get('last_seen')}, "
                f"TTL {entry.get('ttl_days')} days). Re-verify before trusting it."
            )
        elif _is_flagged(entry, today):
            verdict, risk_level = "flagged", "high"
            reasons.append(
                f"Flagged serial rugger: {len(rugged)} rugged mints "
                "with on-chain evidence."
            )
            reasons.append(f"Cluster: {entry.get('cluster_id')}.")
        else:
            verdict, risk_level = "under_review", "watch"
            reasons.append(
                f"Under review: {len(rugged)} rugged mint(s) — below the "
                f"{MIN_RUGGED_MINTS_TO_FLAG}-rug threshold to flag."
            )
        if mint_note:
            reasons.append(mint_note)
        return {
            "dev_wallet": wallet,
            "verdict": verdict,
            "risk_level": risk_level,
            "flagged": verdict == "flagged",
            "rugged_mint_count": len(rugged),
            "rugged_mints": rugged_mints,
            "cluster_id": entry.get("cluster_id"),
            "mint": mint,
            "mint_in_registry": mint_in_registry,
            "reason": reasons[0],
            "reasons": reasons,
            "evidence": evidence,
            "sample_data": self.sample_data,
            "exit_code": EXIT_FLAGGED if verdict == "flagged" else EXIT_NOT_FLAGGED,
        }

    def is_flagged_dev(self, wallet: str) -> bool:
        """True only for a flagged dev (>= 2 rugged mints + evidence,
        unexpired). Invalid addresses and unknown wallets are False."""
        try:
            return self.check_dev(wallet)["verdict"] == "flagged"
        except ValueError:
            return False

    # -- mint checks -------------------------------------------------------

    def fetch_market_data(self, mint: str) -> dict | None:
        """Live market data for a mint from DexScreener (no key).

        Returns the parsed pair dict, or None when DexScreener has
        no pair for the mint or the request fails. Never raises for
        a network error — callers fall back to registry-only data.
        """
        try:
            data = self._fetcher(f"{DEXSCREENER_TOKENS_URL}/{mint}")
        except Exception:
            return None
        if not isinstance(data, list):
            return None
        for item in data:
            parsed = _parse_token_pair(item)
            if parsed is not None:
                return parsed
        return None

    def check_mint(self, mint: str, today: date | None = None) -> dict:
        """Check a coin mint.

        Registry-known mints return the registry record: ``verdict``
        is the recorded outcome (``rugged`` / ``failed_but_honest`` /
        ``inconclusive``), with the dev wallet, collapse % and
        evidence links. Mints not in the registry are checked live
        against DexScreener: ``verdict`` is ``not_in_registry`` and
        the result carries a ``watch_label`` (``collapsed`` /
        ``thin_liquidity`` / ``active``) — a watch label, never a
        rug verdict. If DexScreener is unreachable, market fields
        are None and ``watch_label`` is ``unknown`` instead of
        invented numbers. Raises ``ValueError`` for an invalid mint.
        """
        if not is_valid_solana_address(mint):
            raise ValueError(f"'{mint}' is not a valid Solana address")
        today = today or self.today
        entry, record = self._find_mint(mint)
        if record is not None:
            return {
                "mint": mint,
                "verdict": record.get("outcome"),
                "name": None,
                "symbol": record.get("symbol"),
                "dev_wallet": entry.get("wallet") if entry else None,
                "cluster_id": entry.get("cluster_id") if entry else None,
                "watch_label": (
                    "collapsed"
                    if (record.get("collapse_pct") or 0) >= 70
                    else "active"
                ),
                "collapse_pct": record.get("collapse_pct"),
                "market_cap_usd": record.get("current_market_cap_usd"),
                "peak_market_cap_usd": record.get("peak_market_cap_usd"),
                "liquidity_usd": None,
                "volume_24h_usd": None,
                "change_24h_pct": None,
                "evidence": record.get("evidence", {}),
                "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
                "reason": (
                    f"Mint is in the registry as '{record.get('outcome')}' "
                    f"(dev {entry.get('wallet') if entry else 'unknown'})."
                    if record.get("outcome") == "rugged"
                    else f"Mint is in the registry as '{record.get('outcome')}' — "
                    "not counted as a rug."
                ),
                "sample_data": self.sample_data,
            }
        market = self.fetch_market_data(mint)
        if market is None:
            return {
                "mint": mint,
                "verdict": "not_in_registry",
                "name": None,
                "symbol": None,
                "dev_wallet": None,
                "cluster_id": None,
                "watch_label": "unknown",
                "collapse_pct": None,
                "market_cap_usd": None,
                "peak_market_cap_usd": None,
                "liquidity_usd": None,
                "volume_24h_usd": None,
                "change_24h_pct": None,
                "evidence": {},
                "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
                "reason": (
                    "Mint is not in the registry and DexScreener returned "
                    "no market data (no pair, or the request failed) — "
                    "no verdict, not a clean bill of health."
                ),
                "sample_data": self.sample_data,
            }
        return {
            "mint": mint,
            "verdict": "not_in_registry",
            "name": market.get("name"),
            "symbol": market.get("symbol"),
            "dev_wallet": None,
            "cluster_id": None,
            "watch_label": market["watch_label"],
            "collapse_pct": None,
            "market_cap_usd": market.get("market_cap_usd"),
            "peak_market_cap_usd": None,
            "liquidity_usd": market.get("liquidity_usd"),
            "volume_24h_usd": market.get("volume_24h_usd"),
            "change_24h_pct": market.get("change_24h_pct"),
            "evidence": {},
            "dexscreener_url": market["dexscreener_url"],
            "reason": (
                "Mint is not in the registry. Live DexScreener data only — "
                f"watch label is '{market['watch_label']}', which is not a "
                "rug verdict. A rug needs dev-wallet + LP/dev-sell evidence."
            ),
            "sample_data": self.sample_data,
        }

    def is_rugged_mint(self, mint: str) -> bool:
        """True only if the mint is in the registry as ``rugged``.

        Registry-only, no network call. Invalid addresses and
        mints not in the registry are False.
        """
        if not is_valid_solana_address(mint):
            return False
        _, record = self._find_mint(mint)
        return record is not None and record.get("outcome") == "rugged"

    # -- live watch --------------------------------------------------------

    def watch(self, limit: int = 10) -> list[dict]:
        """Latest boosted Solana tokens from DexScreener (no key).

        Each entry has mint, symbol, name, market cap, liquidity,
        24h volume/change and a watch label (``collapsed`` /
        ``thin_liquidity`` / ``active``) — never "rugged". On a
        network failure this returns ``[]`` instead of raising, so
        callers can fall back to a cached list. Raises
        ``ValueError`` for a non-positive limit.
        """
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        try:
            boosts = self._fetcher(DEXSCREENER_BOOSTS_URL)
            if not isinstance(boosts, list):
                return []
            mints: list[str] = []
            seen: set[str] = set()
            for item in boosts:
                if not isinstance(item, dict):
                    continue
                if item.get("chainId") not in (None, "solana"):
                    continue
                mint = item.get("tokenAddress")
                if mint and mint not in seen:
                    seen.add(mint)
                    mints.append(mint)
                if len(mints) >= limit:
                    break
            if not mints:
                return []
            details = self._fetcher(f"{DEXSCREENER_TOKENS_URL}/{','.join(mints)}")
            if not isinstance(details, list):
                return []
            entries = []
            for item in details:
                parsed = _parse_token_pair(item)
                if parsed is not None:
                    entries.append(parsed)
            return entries
        except Exception:
            return []


# -- module-level convenience wrappers (default registry) -----------------

_default_client: RugFarm | None = None


def _default() -> RugFarm:
    global _default_client
    if _default_client is None:
        _default_client = RugFarm()
    return _default_client


def check_dev(wallet: str, mint: str | None = None) -> dict:
    """Check a dev wallet with the default registry (see RugFarm.check_dev)."""
    return _default().check_dev(wallet, mint=mint)


def check_mint(mint: str) -> dict:
    """Check a coin mint with the default registry (see RugFarm.check_mint)."""
    return _default().check_mint(mint)


def is_flagged_dev(wallet: str) -> bool:
    """True only for a flagged dev (see RugFarm.is_flagged_dev)."""
    return _default().is_flagged_dev(wallet)


def is_rugged_mint(mint: str) -> bool:
    """True only for a registry-recorded rugged mint (no network)."""
    return _default().is_rugged_mint(mint)


def watch(limit: int = 10) -> list[dict]:
    """Latest boosted Solana tokens (see RugFarm.watch)."""
    return _default().watch(limit=limit)


def _format_dev(result: dict) -> str:
    lines = [
        f"Dev wallet: {result['dev_wallet']}",
        f"Verdict: {result['verdict']} (risk: {result['risk_level']})",
        f"Flagged: {'YES' if result['flagged'] else 'no'}",
        f"Rugged mint count: {result['rugged_mint_count']}",
        f"Cluster: {result['cluster_id'] or '-'}",
    ]
    if result.get("mint"):
        lines.append(
            f"Mint in registry: {result['mint_in_registry']}"
            + (f" ({result['mint']})" if result["mint_in_registry"] is not None else "")
        )
    lines.append("")
    for reason in result["reasons"]:
        lines.append(f"  - {reason}")
    for i, mint in enumerate(result["rugged_mints"], start=1):
        lines.append("")
        lines.append(f"Rugged mint {i}: {mint['mint']} ({mint.get('symbol')})")
        lines.append(f"  collapse: {mint.get('collapse_pct')}%")
        for key, url in sorted((mint.get("evidence") or {}).items()):
            lines.append(f"  evidence {key}: {url}")
    if result.get("sample_data"):
        lines.append("")
        lines.append("NOTE: SAMPLE DATA ONLY — sample wallets are synthetic, not accusations.")
    return "\n".join(lines)


def _format_mint(result: dict) -> str:
    lines = [
        f"Mint: {result['mint']}",
        f"Verdict: {result['verdict']}",
        f"Symbol: {result.get('symbol') or '-'}",
        f"Dev wallet: {result.get('dev_wallet') or 'unknown'}",
        f"Watch label: {result['watch_label']}",
    ]
    if result.get("market_cap_usd") is not None:
        lines.append(f"Market cap: ${result['market_cap_usd']:,.0f}")
    if result.get("liquidity_usd") is not None:
        lines.append(f"Liquidity: ${result['liquidity_usd']:,.0f}")
    if result.get("change_24h_pct") is not None:
        lines.append(f"24h change: {result['change_24h_pct']:+.1f}%")
    if result.get("collapse_pct") is not None:
        lines.append(f"Collapse: {result['collapse_pct']}%")
    lines.append("")
    lines.append(f"  - {result['reason']}")
    for key, url in sorted((result.get("evidence") or {}).items()):
        lines.append(f"  evidence {key}: {url}")
    lines.append(f"  {result['dexscreener_url']}")
    if result.get("sample_data"):
        lines.append("")
        lines.append("NOTE: SAMPLE DATA ONLY — sample mints are synthetic, not accusations.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Demo CLI: ``python -m src.rugfarm <wallet-or-mint>``.

    Detects a registry wallet first, then a registry mint, then a
    live DexScreener pair (mint) — anything else is reported as an
    unknown wallet. Exit 1 when the address is a flagged dev or a
    rugged mint, 0 otherwise, 2 for an invalid address.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm SDK demo: check a dev wallet or coin mint."
    )
    parser.add_argument("address", help="Dev wallet or coin mint address")
    parser.add_argument("--registry", type=str, default=None, help="Registry JSON path")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args(argv)

    if not is_valid_solana_address(args.address):
        print(f"ERROR: '{args.address}' is not a valid Solana address.", file=sys.stderr)
        return EXIT_INVALID

    client = RugFarm(registry_path=args.registry) if args.registry else RugFarm()
    entry = client._get_wallet_entry(args.address)
    _, record = client._find_mint(args.address)
    if entry is not None:
        result = client.check_dev(args.address)
        print(json.dumps(result, indent=2) if args.json else _format_dev(result))
        return result["exit_code"]
    if record is not None:
        result = client.check_mint(args.address)
        print(json.dumps(result, indent=2) if args.json else _format_mint(result))
        return EXIT_FLAGGED if result["verdict"] == "rugged" else EXIT_NOT_FLAGGED
    market = client.fetch_market_data(args.address)
    if market is not None:
        result = client.check_mint(args.address)
        print(json.dumps(result, indent=2) if args.json else _format_mint(result))
        return EXIT_NOT_FLAGGED
    result = client.check_dev(args.address)
    print(json.dumps(result, indent=2) if args.json else _format_dev(result))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
