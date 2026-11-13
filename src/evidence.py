"""Muse-run evidence gathering — v2.2.

Muse does all evidence gathering itself: no community submissions,
no waiting on anyone else. For a dev wallet (+ optionally specific
mints) this module collects the evidence for a rug verdict from the
sources Muse can reach, checks it against the registry rule, and
writes a markdown evidence report. Nothing is invented:

- ``EvidenceCollector`` — DexScreener pair data (price, market
  cap, liquidity, 24h volume/change), Solscan account data via
  its public API where available (otherwise links only),
  pump.fun coin/profile links, and Helius tx links when a
  transaction signature is given. Numbers that cannot be found
  are ``None`` ("n/a" in the report), never invented.
- ``EvidenceVerifier`` — verified only when >= 2 rugged mints
  each have at least one evidence link AND a collapse number.
  Links alone never verify anything, and a price drop alone
  never flags.
- ``EvidenceReport`` — writes a markdown report to a
  caller-given path (default ``data/evidence/<wallet>.md``),
  never to the registry. The report carries a "Verified by
  Muse" timestamp line (UTC, caller-supplied date or today's
  real UTC date — never a fake date).

Run:
    python -m src.evidence --dev <wallet> [--mint <mint> ...]
        [--tx <mint>:<signature> ...] [--out <path>] [--json]
    Exit 1 = verified/flagged, 0 = otherwise, 2 = invalid args.

All HTTP goes through an injectable ``httpx.Client``, so tests
use ``httpx.MockTransport`` and never make a live call.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from src import registry as registry_mod
from src.http_client import make_client
from src.indexer import DEXSCREENER_URL
from src.labeller import MintEvent, collapse_pct, label_outcome

EXIT_NOT_FLAGGED = 0
EXIT_FLAGGED = 1
EXIT_INVALID = 2

SOLSCAN_ACCOUNT_URL = "https://public-api.solscan.io/account"
LP_DRAINED_USD = 1000.0
LAMPORTS_PER_SOL = 1_000_000_000

EXPECTED_LINKS_PER_MINT = ("dexscreener", "solscan_mint", "pumpfun")
EXPECTED_WALLET_LINKS = ("solscan_wallet", "pumpfun_profile")

DISCLAIMER = (
    "Evidence gathered by Muse — no community submissions. "
    "Verify every evidence link yourself before acting on a flag. "
    "A price drop alone is not a rug verdict."
)


def _default_registry() -> dict:
    """Load data/registry.json, or an empty registry when missing."""
    path = Path(__file__).resolve().parent.parent / "data" / "registry.json"
    if path.is_file():
        try:
            return registry_mod.load_registry(path)
        except (ValueError, OSError):
            return {"sample_data": True, "entries": []}
    return {"sample_data": True, "entries": []}


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _fmt_money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "n/a"


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.1f}%" if value is not None else "n/a"


def _fmt_number(value: float | None) -> str:
    return f"{value:,.4g}" if value is not None else "n/a"


def _mint_links(mint: str) -> dict:
    return {
        "dexscreener": f"https://dexscreener.com/solana/{mint}",
        "solscan_mint": f"https://solscan.io/token/{mint}",
        "pumpfun": f"https://pump.fun/coin/{mint}",
    }


def _wallet_links(wallet: str) -> dict:
    return {
        "solscan_wallet": f"https://solscan.io/account/{wallet}",
        "pumpfun_profile": f"https://pump.fun/profile/{wallet}",
    }


def _parse_dexscreener_pair(data: object, mint: str) -> dict | None:
    """Best Solana pair for a mint from a DexScreener tokens response.

    Returns None when the response has no usable pair — the caller
    records every number as missing instead of inventing one.
    """
    if not isinstance(data, dict):
        return None
    pairs = data.get("pairs") or []
    if not isinstance(pairs, list) or not pairs:
        return None
    sol_pairs = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"] or [
        p for p in pairs if isinstance(p, dict)
    ]
    if not sol_pairs:
        return None
    pair = max(sol_pairs, key=lambda p: _as_float((p.get("liquidity") or {}).get("usd")) or 0.0)
    base = pair.get("baseToken") or {}
    market_cap = _as_float(pair.get("marketCap") or pair.get("fdv"))
    liquidity = _as_float((pair.get("liquidity") or {}).get("usd"))
    volume = _as_float((pair.get("volume") or {}).get("h24"))
    change = _as_float((pair.get("priceChange") or {}).get("h24"))
    return {
        "mint": mint,
        "symbol": base.get("symbol") or pair.get("symbol"),
        "name": base.get("name") or pair.get("name"),
        "price_usd": _as_float(pair.get("priceUsd")),
        "market_cap_usd": market_cap,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "change_24h_pct": change,
    }


def _estimate_peak_and_collapse(numbers: dict) -> tuple[float | None, float | None]:
    """Peak market cap + collapse % estimated from DexScreener data.

    Same estimation the indexer uses: peak ~= current / (1 + h24%)
    when the 24h change is negative, else peak = current. Returns
    (peak, collapse_pct), both None when the numbers are missing —
    never a made-up collapse.
    """
    current = numbers.get("market_cap_usd")
    change = numbers.get("change_24h_pct")
    if current is None or change is None:
        return None, None
    if change < 0:
        factor = max(0.01, 1.0 + change / 100.0)
        peak = current / factor
    else:
        peak = current
    collapse = collapse_pct(
        MintEvent(peak_market_cap_usd=peak, current_market_cap_usd=current)
    )
    return peak, collapse


class EvidenceCollector:
    """Collect Muse-run evidence for a dev wallet (+ mints).

    ``client`` is an injected ``httpx.Client`` (a real one built
    with ``src.http_client.make_client`` by default, a
    ``MockTransport`` client in tests). ``registry_data`` is the
    loaded registry dict — registry rugged mints for the dev are
    merged in (evidence links + collapse numbers), and when no
    ``--mint`` is given the dev's registry mints are the mint list.
    """

    def __init__(self, client: httpx.Client | None = None, registry_data: dict | None = None):
        self._client = client
        self.registry_data = registry_data if registry_data is not None else _default_registry()

    # -- HTTP plumbing ------------------------------------------------
    def _get_json(self, url: str) -> object | None:
        """GET + parse JSON; None on any failure (never raises)."""
        try:
            if self._client is not None:
                resp = self._client.get(url)
            else:
                with make_client(timeout=30.0) as client:
                    resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _fetch_dexscreener(self, mint: str) -> dict | None:
        return _parse_dexscreener_pair(self._get_json(f"{DEXSCREENER_URL}/{mint}"), mint)

    def _fetch_solscan_wallet(self, wallet: str) -> dict:
        """Solscan public API account data; {} when unavailable.

        The Solscan public API may be down or require a key — a
        failure just means the SOL balance is "n/a". The Solscan
        links are always included regardless.
        """
        data = self._get_json(f"{SOLSCAN_ACCOUNT_URL}/{wallet}")
        if not isinstance(data, dict):
            return {}
        lamports = _as_float(data.get("lamports"))
        return {
            "sol_balance_sol": (lamports / LAMPORTS_PER_SOL) if lamports is not None else None,
            "owner_program": data.get("ownerProgram"),
        }

    # -- collection ----------------------------------------------------
    def collect(
        self,
        dev_wallet: str,
        mints: list[str] | None = None,
        tx_signatures: dict[str, str] | list[str] | None = None,
    ) -> dict:
        if not registry_mod.is_valid_solana_address(dev_wallet):
            raise ValueError(f"'{dev_wallet}' is not a valid Solana address")
        for mint in mints or []:
            if not registry_mod.is_valid_solana_address(mint):
                raise ValueError(f"mint '{mint}' is not a valid Solana address")
        if isinstance(tx_signatures, str):
            raise ValueError("tx_signatures must be a dict {mint: signature} or a list of signatures")
        if tx_signatures is not None and not isinstance(tx_signatures, (dict, list)):
            raise ValueError("tx_signatures must be a dict {mint: signature} or a list of signatures")

        entry = registry_mod.get_wallet(self.registry_data, dev_wallet)
        registry_mints = (entry or {}).get("rugged_mints", []) or []
        registry_by_mint = {
            m.get("mint"): m for m in registry_mints if isinstance(m, dict) and m.get("mint")
        }
        if mints is None:
            mint_list = [m for m in registry_by_mint]
        else:
            mint_list = []
            for mint in mints:
                if mint not in mint_list:
                    mint_list.append(mint)

        # Normalise tx signatures to {mint: signature}.
        sig_by_mint: dict[str, str] = {}
        if isinstance(tx_signatures, dict):
            sig_by_mint = {k: v for k, v in tx_signatures.items() if v}
        elif isinstance(tx_signatures, list):
            for mint, sig in zip(mint_list, tx_signatures):
                if sig:
                    sig_by_mint[mint] = sig

        wallet_data = self._fetch_solscan_wallet(dev_wallet)
        wallet_links = _wallet_links(dev_wallet)
        wallet_has_registry_link = bool(
            any(
                (m.get("evidence") or {}).get("solscan_wallet")
                for m in registry_mints
                if isinstance(m, dict)
            )
        )
        solscan_confirmed = wallet_data.get("sol_balance_sol") is not None or bool(
            wallet_data.get("owner_program")
        )

        mint_entries: list[dict] = []
        missing: list[str] = []
        links_found = 0
        # Helius tx links are only expected for mints actually collected.
        links_expected = len(EXPECTED_WALLET_LINKS) + len(
            [m for m in mint_list if m in sig_by_mint]
        )
        # Wallet links count as found when their source confirmed the
        # wallet (Solscan API data or a registry evidence link).
        if solscan_confirmed or wallet_has_registry_link:
            links_found += len(EXPECTED_WALLET_LINKS)
        if wallet_data.get("sol_balance_sol") is None:
            missing.append("wallet:sol_balance_sol")

        for mint in mint_list:
            links_expected += len(EXPECTED_LINKS_PER_MINT)
            links = _mint_links(mint)
            dex = self._fetch_dexscreener(mint)
            reg = registry_by_mint.get(mint) or {}
            reg_evidence = reg.get("evidence") or {}

            # Merge registry evidence links (solscan tx link etc.).
            if reg_evidence.get("solscan_mint"):
                links["solscan_mint"] = reg_evidence["solscan_mint"]
            if reg_evidence.get("transaction"):
                links["solscan_tx"] = reg_evidence["transaction"]
            if mint in sig_by_mint:
                links["helius_tx"] = f"https://explorer.helius.xyz/tx/{sig_by_mint[mint]}"

            # A link counts as found when its source confirmed the
            # resource: DexScreener returned a pair, or the registry
            # supplied that evidence link.
            if dex is not None:
                links_found += len(EXPECTED_LINKS_PER_MINT)
            elif reg_evidence.get("solscan_mint"):
                links_found += 1  # only the registry-backed link

            numbers = {
                "symbol": dex.get("symbol") if dex else None,
                "name": dex.get("name") if dex else None,
                "price_usd": dex.get("price_usd") if dex else None,
                "market_cap_usd": dex.get("market_cap_usd") if dex else None,
                "liquidity_usd": dex.get("liquidity_usd") if dex else None,
                "volume_24h_usd": dex.get("volume_24h_usd") if dex else None,
                "change_24h_pct": dex.get("change_24h_pct") if dex else None,
            }
            for field in ("market_cap_usd", "liquidity_usd", "volume_24h_usd", "change_24h_pct", "price_usd"):
                if numbers[field] is None:
                    missing.append(f"{mint}:{field}")

            # Outcome + collapse: registry first (verified numbers),
            # otherwise the labeller on DexScreener-derived data.
            if reg.get("outcome"):
                outcome = reg["outcome"]
                collapse = _as_float(reg.get("collapse_pct"))
                collapse_source = "registry" if collapse is not None else None
                peak = _as_float(reg.get("peak_market_cap_usd"))
                current = _as_float(reg.get("current_market_cap_usd"))
            else:
                peak, collapse = _estimate_peak_and_collapse(numbers)
                collapse_source = "estimated" if collapse is not None else None
                current = numbers["market_cap_usd"]
                event = MintEvent(
                    peak_market_cap_usd=peak,
                    current_market_cap_usd=current,
                    lp_removed=bool(
                        numbers["liquidity_usd"] is not None
                        and numbers["liquidity_usd"] < LP_DRAINED_USD
                    ),
                    dev_sold_pct=0.0,  # DexScreener does not prove a dev sell.
                    time_to_collapse_minutes=None,
                )
                outcome = label_outcome(event) if dex is not None else "inconclusive"

            mint_entries.append(
                {
                    "mint": mint,
                    "symbol": numbers["symbol"],
                    "name": numbers["name"],
                    "outcome": outcome,
                    "collapse_pct": collapse,
                    "collapse_pct_source": collapse_source,
                    "peak_market_cap_usd": peak,
                    "current_market_cap_usd": current,
                    "price_usd": numbers["price_usd"],
                    "market_cap_usd": numbers["market_cap_usd"],
                    "liquidity_usd": numbers["liquidity_usd"],
                    "volume_24h_usd": numbers["volume_24h_usd"],
                    "change_24h_pct": numbers["change_24h_pct"],
                    "lp_removed": bool(
                        numbers["liquidity_usd"] is not None
                        and numbers["liquidity_usd"] < LP_DRAINED_USD
                    )
                    or bool(reg.get("lp_removed")),
                    "dev_sell_pct": _as_float(reg.get("dev_sell_pct")),
                    "time_to_collapse_minutes": reg.get("time_to_collapse_minutes"),
                    "links": links,
                    "registry_evidence": reg_evidence,
                }
            )

        all_links = sorted(
            {url for entry in mint_entries for url in entry["links"].values()}
            | set(wallet_links.values())
        )
        links_expected_total = links_expected
        completeness = (
            round(links_found / links_expected_total, 3) if links_expected_total else 0.0
        )
        return {
            "dev_wallet": dev_wallet,
            "collector": "EvidenceCollector (Muse-run, no community submissions)",
            "mints": mint_entries,
            "wallet": {
                "sol_balance_sol": wallet_data.get("sol_balance_sol"),
                "owner_program": wallet_data.get("owner_program"),
                "solscan_api_available": solscan_confirmed,
                "links": wallet_links,
            },
            "links": all_links,
            "missing": missing,
            "links_found": links_found,
            "links_expected": links_expected_total,
            "completeness_score": completeness,
        }


class EvidenceVerifier:
    """Check an evidence pack against the registry rule.

    Verified only when >= 2 rugged mints each have at least one
    evidence link AND a collapse number. Links alone never verify
    anything — a mint with links but no rugged outcome or no
    collapse number does not count — and a price drop alone
    never flags.
    """

    @staticmethod
    def verify(pack: dict) -> dict:
        dev = pack.get("dev_wallet")
        mints = pack.get("mints") or []
        verified_mints: list[dict] = []
        rugged_total = 0
        for mint in mints:
            if mint.get("outcome") != "rugged":
                continue
            rugged_total += 1
            links = mint.get("links") or {}
            has_link = any(
                isinstance(url, str) and url.startswith(("http://", "https://"))
                for url in links.values()
            )
            has_collapse = mint.get("collapse_pct") is not None
            if has_link and has_collapse:
                verified_mints.append(mint)
        verified = len(verified_mints) >= registry_mod.MIN_RUGGED_MINTS_TO_FLAG
        if verified:
            reasons = [
                f"Verified: {len(verified_mints)} rugged mints, each with "
                "at least one evidence link and a collapse number."
            ]
        elif verified_mints:
            reasons = [
                f"Only {len(verified_mints)} rugged mint(s) with evidence "
                f"+ collapse number — need >= {registry_mod.MIN_RUGGED_MINTS_TO_FLAG} to verify."
            ]
        elif rugged_total:
            reasons = [
                f"{rugged_total} rugged mint(s) found, but none with both "
                "an evidence link and a collapse number — insufficient evidence."
            ]
        else:
            reasons = [
                "No rugged mint with evidence + collapse number — insufficient evidence."
            ]
        return {
            "dev_wallet": dev,
            "verdict": "verified" if verified else "insufficient evidence",
            "flagged": verified,
            "verified_rugged_mint_count": len(verified_mints),
            "rugged_mint_count": rugged_total,
            "verified_mints": [m.get("mint") for m in verified_mints],
            "reasons": reasons,
        }


def default_report_path(dev_wallet: str, base: Path | None = None) -> Path:
    """Default report location: data/evidence/<wallet>.md."""
    root = base or (Path(__file__).resolve().parent.parent / "data" / "evidence")
    return root / f"{dev_wallet}.md"


class EvidenceReport:
    """Render + write a markdown evidence report (never to the registry)."""

    @staticmethod
    def render(pack: dict, verification: dict, date_str: str | None = None) -> str:
        today = date_str or datetime.now(timezone.utc).date().isoformat()
        dev = pack.get("dev_wallet")
        wallet = pack.get("wallet") or {}
        wallet_links = wallet.get("links") or {}
        lines = [
            f"# Evidence report — {dev}",
            "",
            f"Verified by Muse — {today} UTC",
            "",
            "## Verdict",
            "",
            f"- Verdict: **{verification.get('verdict')}**",
            f"- Flagged: {'YES' if verification.get('flagged') else 'NO'}",
            f"- Rugged mints with evidence + collapse number: {verification.get('verified_rugged_mint_count', 0)}",
            f"- Rugged mints total: {verification.get('rugged_mint_count', 0)}",
            f"- Completeness score: {pack.get('completeness_score', 0.0)} "
            f"({pack.get('links_found', 0)}/{pack.get('links_expected', 0)} links found)",
            "",
        ]
        for reason in verification.get("reasons", []):
            lines.append(f"- {reason}")
        lines += [
            "",
            "## Wallet",
            "",
            f"- Dev wallet: `{dev}`",
            f"- SOL balance: {_fmt_number(wallet.get('sol_balance_sol'))} SOL",
            f"- Owner program: {wallet.get('owner_program') or 'n/a'}",
            f"- Solscan: {wallet_links.get('solscan_wallet', 'n/a')}",
            f"- pump.fun profile: {wallet_links.get('pumpfun_profile', 'n/a')}",
            "",
            "## Mints",
            "",
        ]
        mints = pack.get("mints") or []
        if not mints:
            lines.append("No mints checked — no registry mints and none given.")
            lines.append("")
        for entry in mints:
            links = entry.get("links") or {}
            lines += [
                f"### {entry.get('mint')}",
                "",
                f"- Symbol: {entry.get('symbol') or 'n/a'}",
                f"- Outcome: {entry.get('outcome')}",
                f"- Collapse: {_fmt_pct(entry.get('collapse_pct'))}"
                + (
                    f" ({entry.get('collapse_pct_source')})"
                    if entry.get("collapse_pct_source")
                    else ""
                ),
                f"- Price: {_fmt_number(entry.get('price_usd'))} USD",
                f"- Market cap: {_fmt_money(entry.get('market_cap_usd'))}",
                f"- Liquidity: {_fmt_money(entry.get('liquidity_usd'))}",
                f"- 24h volume: {_fmt_money(entry.get('volume_24h_usd'))}",
                f"- 24h change: {_fmt_pct(entry.get('change_24h_pct'))}",
                f"- LP removed: {'yes' if entry.get('lp_removed') else 'no'}",
                f"- Dev sell: {_fmt_pct(entry.get('dev_sell_pct'))}",
                "- Evidence links:",
            ]
            for kind, url in sorted(links.items()):
                lines.append(f"  - {kind}: {url}")
            lines.append("")
        lines += [
            "## Missing data",
            "",
        ]
        missing = pack.get("missing") or []
        if missing:
            for item in missing:
                lines.append(f"- {item}: n/a (not found, never invented)")
        else:
            lines.append("None — all numbers found.")
        lines += [
            "",
            "---",
            "",
            DISCLAIMER,
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def write(
        pack: dict, verification: dict, path: str | Path, date_str: str | None = None
    ) -> Path:
        """Write the report to ``path``. Refuses the registry file."""
        target = Path(path)
        if target.name == "registry.json":
            raise ValueError("refusing to write an evidence report over registry.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            EvidenceReport.render(pack, verification, date_str=date_str), encoding="utf-8"
        )
        return target


def gather_and_verify(
    dev_wallet: str,
    mints: list[str] | None = None,
    tx_signatures: dict[str, str] | list[str] | None = None,
    collector: EvidenceCollector | None = None,
    registry_data: dict | None = None,
) -> dict:
    """Collect evidence for a dev, then verify it. No file is written."""
    collector = collector or EvidenceCollector(registry_data=registry_data)
    pack = collector.collect(dev_wallet, mints=mints, tx_signatures=tx_signatures)
    verification = EvidenceVerifier.verify(pack)
    return {"pack": pack, "verification": verification}


def _parse_tx_arg(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise ValueError(f"--tx must be MINT:SIGNATURE, got '{value}'")
    mint, sig = value.split(":", 1)
    if not mint or not sig:
        raise ValueError(f"--tx must be MINT:SIGNATURE, got '{value}'")
    return mint, sig


def main(
    argv: list[str] | None = None,
    collector: EvidenceCollector | None = None,
    registry_data: dict | None = None,
    date_str: str | None = None,
) -> int:
    """Demo CLI.

    ``python -m src.evidence --dev <wallet> [--mint <mint> ...]
    [--tx <mint>:<signature> ...] [--out <path>] [--json]``
    Exit 1 = verified/flagged, 0 = otherwise, 2 = invalid args.
    """
    parser = argparse.ArgumentParser(
        description="Muse-run evidence gathering for a dev wallet (no community)."
    )
    parser.add_argument("--dev", type=str, default=None, help="Dev wallet address")
    parser.add_argument("--mint", action="append", default=None, help="Coin mint address (repeatable)")
    parser.add_argument("--tx", action="append", default=None, help="Tx signature as MINT:SIGNATURE (repeatable)")
    parser.add_argument("--out", type=str, default=None, help="Report output path")
    parser.add_argument("--registry", type=str, default=None, help="Registry JSON path")
    parser.add_argument("--json", action="store_true", help="Print the pack + verification as JSON")
    parser.add_argument("--date", type=str, default=None, help="Report date YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args(argv)

    if not args.dev:
        print("ERROR: --dev <wallet> is required.", file=sys.stderr)
        return EXIT_INVALID
    if not registry_mod.is_valid_solana_address(args.dev):
        print(f"ERROR: '{args.dev}' is not a valid Solana address.", file=sys.stderr)
        return EXIT_INVALID
    for mint in args.mint or []:
        if not registry_mod.is_valid_solana_address(mint):
            print(f"ERROR: mint '{mint}' is not a valid Solana address.", file=sys.stderr)
            return EXIT_INVALID
    tx_signatures: dict[str, str] = {}
    for tx_arg in args.tx or []:
        try:
            mint, sig = _parse_tx_arg(tx_arg)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if not registry_mod.is_valid_solana_address(mint):
            print(f"ERROR: mint '{mint}' in --tx is not a valid Solana address.", file=sys.stderr)
            return EXIT_INVALID
        tx_signatures[mint] = sig
    if args.date is not None:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: --date must be YYYY-MM-DD, got '{args.date}'.", file=sys.stderr)
            return EXIT_INVALID
    if args.registry:
        try:
            data = registry_mod.load_registry(args.registry)
        except (ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_INVALID
    else:
        data = registry_data if registry_data is not None else _default_registry()

    try:
        result = gather_and_verify(
            args.dev,
            mints=args.mint,
            tx_signatures=tx_signatures or None,
            collector=collector,
            registry_data=data,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID

    pack, verification = result["pack"], result["verification"]
    out_path = Path(args.out) if args.out else default_report_path(args.dev)
    try:
        EvidenceReport.write(pack, verification, out_path, date_str=args.date or date_str)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID

    if args.json:
        print(json.dumps({**result, "report_path": str(out_path)}, indent=2))
    else:
        print(f"Dev: {args.dev}")
        print(f"Verdict: {verification['verdict']} (exit code {EXIT_FLAGGED if verification['flagged'] else EXIT_NOT_FLAGGED})")
        for reason in verification.get("reasons", []):
            print(f"  - {reason}")
        print(
            f"Completeness: {pack['completeness_score']} "
            f"({pack['links_found']}/{pack['links_expected']} links found)"
        )
        print(f"Missing numbers: {len(pack['missing'])} (shown as n/a, never invented)")
        print(f"Evidence report written to {out_path}")
        print("")
        print(DISCLAIMER)
    return EXIT_FLAGGED if verification["flagged"] else EXIT_NOT_FLAGGED


if __name__ == "__main__":
    raise SystemExit(main())
