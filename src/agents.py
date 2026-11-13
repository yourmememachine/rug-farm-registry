"""AI agents for rug checking — v1.7.

Four small, deterministic, stdlib-only agents that work together
as a pipeline. No agent invents data, no agent calls a token
"rugged" on price action alone, and no agent needs a new
dependency. Live market data comes from the existing no-key
DexScreener watch (``src.pump_watch.PumpWatch``), registry rules
from ``src.registry`` — the agents only orchestrate them.

    ScannerAgent  — live DexScreener data -> watch candidates
    EvidenceAgent — evidence links + registry check for a dev/mint
    VerdictAgent  — the >= 2 rugged mints + evidence rule, nothing else
    AlertAgent    — honest daily-alert markdown from verdicts

Pipeline: Scanner -> Evidence -> Verdict -> Alert.

    python -m src.agents --mint <mint> [--dev <wallet>]
    python -m src.agents --watch [--limit 5]

Exit codes: 1 = flagged, 0 = otherwise, 2 = invalid address/args.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from src import registry as registry_mod

EXIT_NOT_FLAGGED = 0
EXIT_FLAGGED = 1
EXIT_INVALID = 2

WATCH_LABELS = ("collapsed", "thin_liquidity", "active", "unknown")
DISCLAIMER = (
    "Watch labels are not rug verdicts: 'collapsed' / "
    "'thin_liquidity' mean watch the token, not that its dev "
    "rugged it. A rug needs dev-wallet + LP/dev-sell evidence."
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


def _evidence_urls(mint: str) -> dict:
    return {
        "dexscreener": f"https://dexscreener.com/solana/{mint}",
        "solscan": f"https://solscan.io/token/{mint}",
        "pump_fun": f"https://pump.fun/coin/{mint}",
    }


def _wallet_urls(wallet: str) -> dict:
    return {
        "solscan": f"https://solscan.io/account/{wallet}",
        "pump_fun": f"https://pump.fun/profile/{wallet}",
    }


class ScannerAgent:
    """Fetch live DexScreener data for mints; return watch candidates.

    ``watch`` is any object with ``fetch_token_details(mints)`` and
    ``watch(limit)`` — a real ``src.pump_watch.PumpWatch`` by
    default (lazily imported, so this module stays stdlib-only),
    or a stub in tests. Candidates carry market cap, liquidity,
    24h volume/change and a watch label only. Holder counts are
    never invented (``holders`` is always None) and no candidate
    is ever labelled "rugged".
    """

    def __init__(self, watch=None):
        self._watch = watch

    def _backend(self):
        if self._watch is not None:
            return self._watch
        from src.pump_watch import PumpWatch

        self._watch = PumpWatch()
        return self._watch

    def scan(self, mints: list[str]) -> list[dict]:
        """Watch candidates for the given mints, in request order."""
        if not isinstance(mints, list):
            raise ValueError("mints must be a list of addresses")
        for mint in mints:
            if not registry_mod.is_valid_solana_address(mint):
                raise ValueError(f"'{mint}' is not a valid Solana address")
        if not mints:
            return []
        entries = self._backend().fetch_token_details(list(mints))
        candidates = []
        for entry in entries or []:
            candidates.append(
                {
                    "mint": entry["mint"],
                    "symbol": entry.get("symbol", "?"),
                    "name": entry.get("name"),
                    "market_cap_usd": entry.get("market_cap_usd"),
                    "liquidity_usd": entry.get("liquidity_usd"),
                    "volume_24h_usd": entry.get("volume_24h_usd"),
                    "change_24h_pct": entry.get("change_24h_pct"),
                    "watch_label": entry.get("watch_label", "unknown"),
                    "holders": None,  # DexScreener does not publish this.
                    "dexscreener_url": entry.get(
                        "dexscreener_url",
                        f"https://dexscreener.com/solana/{entry['mint']}",
                    ),
                }
            )
        return candidates

    def watch_latest(self, limit: int = 5) -> list[dict]:
        """Latest boosted Solana tokens, as watch candidates."""
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        entries = self._backend().watch(limit=limit)
        # Reuse scan()'s shaping via a light pass over the entries.
        candidates = []
        for entry in entries or []:
            candidates.append(
                {
                    "mint": entry["mint"],
                    "symbol": entry.get("symbol", "?"),
                    "name": entry.get("name"),
                    "market_cap_usd": entry.get("market_cap_usd"),
                    "liquidity_usd": entry.get("liquidity_usd"),
                    "volume_24h_usd": entry.get("volume_24h_usd"),
                    "change_24h_pct": entry.get("change_24h_pct"),
                    "watch_label": entry.get("watch_label", "unknown"),
                    "holders": None,
                    "dexscreener_url": entry.get(
                        "dexscreener_url",
                        f"https://dexscreener.com/solana/{entry['mint']}",
                    ),
                }
            )
        return candidates


class EvidenceAgent:
    """Collect evidence links + the registry check for a dev/mint.

    Returns the evidence list, ``mint_in_registry`` (True/False/
    None when no mint was given) and a clear verdict:
    ``sufficient`` only when the registry has the dev with at
    least one rugged mint, every rugged mint has evidence links,
    and (when a mint was given) that mint is one of the dev's
    rugged mints. Anything else is ``insufficient evidence`` —
    never a silent pass.
    """

    def __init__(self, registry_data: dict | None = None, today: date | None = None):
        self.data = registry_data if registry_data is not None else _default_registry()
        self.today = today or date.today()

    def collect(self, dev_wallet: str, mint: str | None = None) -> dict:
        if not registry_mod.is_valid_solana_address(dev_wallet):
            raise ValueError(f"'{dev_wallet}' is not a valid Solana address")
        if mint is not None and not registry_mod.is_valid_solana_address(mint):
            raise ValueError(f"mint '{mint}' is not a valid Solana address")
        check = registry_mod.check_wallet(
            self.data, dev_wallet, mint=mint, today=self.today
        )
        evidence: list[dict] = []
        for kind, url in _wallet_urls(dev_wallet).items():
            evidence.append({"kind": f"wallet_{kind}", "url": url, "mint": None})
        if mint is not None:
            for kind, url in _evidence_urls(mint).items():
                evidence.append({"kind": f"mint_{kind}", "url": url, "mint": mint})
        for mint_evidence in check.get("evidence", []):
            for kind, url in sorted((mint_evidence or {}).items()):
                evidence.append({"kind": f"registry_{kind}", "url": url, "mint": mint})
        rugged_count = check.get("rugged_mint_count", 0)
        registry_evidence_ok = rugged_count == 0 or bool(check.get("evidence"))
        mint_ok = mint is None or check.get("mint_in_registry") is True
        sufficient = rugged_count >= 1 and registry_evidence_ok and mint_ok
        reasons = list(check.get("reasons", []))
        if not sufficient:
            reasons.append(
                "Insufficient evidence: a rug verdict needs the dev in "
                "the registry with rugged mint(s) backed by evidence "
                "links, and (when a mint is given) that mint among them."
            )
        return {
            "dev_wallet": dev_wallet,
            "mint": mint,
            "mint_in_registry": check.get("mint_in_registry"),
            "rugged_mint_count": rugged_count,
            "risk_level": check.get("risk_level"),
            "evidence": evidence,
            "registry_check": check,
            "sufficient": sufficient,
            "verdict": "sufficient" if sufficient else "insufficient evidence",
            "reasons": reasons,
        }


class VerdictAgent:
    """Apply the registry rule — and only the registry rule.

    flagged only when the dev has >= 2 rugged mints, each with
    evidence, and the entry is not expired. 1 rugged mint =
    under_review. Anything else = not_flagged / unknown. A price
    drop (``collapsed`` watch label) alone NEVER flags: the
    verdict is about the dev's history, not the token's chart.
    """

    def __init__(self, registry_data: dict | None = None, today: date | None = None):
        self.data = registry_data if registry_data is not None else _default_registry()
        self.today = today or date.today()

    def _evidence_ok(self, entry: dict) -> bool:
        rugged = [
            m
            for m in entry.get("rugged_mints", [])
            if isinstance(m, dict) and m.get("outcome") == "rugged"
        ]
        if not rugged:
            return False
        for mint in rugged:
            evidence = mint.get("evidence")
            if not isinstance(evidence, dict) or not evidence:
                return False
            if not all(
                isinstance(url, str) and url.startswith(("http://", "https://"))
                for url in evidence.values()
            ):
                return False
        return True

    def decide(
        self,
        dev_wallet: str | None = None,
        mint: str | None = None,
        candidate: dict | None = None,
        evidence: dict | None = None,
    ) -> dict:
        """Verdict for a dev (optionally a mint + scanner candidate)."""
        if dev_wallet is not None and not registry_mod.is_valid_solana_address(dev_wallet):
            raise ValueError(f"'{dev_wallet}' is not a valid Solana address")
        if mint is not None and not registry_mod.is_valid_solana_address(mint):
            raise ValueError(f"mint '{mint}' is not a valid Solana address")
        watch_label = (candidate or {}).get("watch_label", "unknown")
        change = (candidate or {}).get("change_24h_pct")
        base: dict = {
            "dev_wallet": dev_wallet,
            "mint": mint,
            "watch_label": watch_label,
            "change_24h_pct": change,
            "rugged_mint_count": 0,
            "flagged": False,
            "exit_code": EXIT_NOT_FLAGGED,
        }
        if dev_wallet is None:
            base.update(
                {
                    "verdict": "unknown",
                    "reasons": [
                        "No dev wallet given — no registry check possible. "
                        "A price drop alone is not a rug verdict."
                    ],
                }
            )
            return base
        check = registry_mod.check_wallet(
            self.data, dev_wallet, mint=mint, today=self.today
        )
        entry = registry_mod.get_wallet(self.data, dev_wallet)
        count = check.get("rugged_mint_count", 0)
        base["rugged_mint_count"] = count
        base["mint_in_registry"] = check.get("mint_in_registry")
        reasons = list(check.get("reasons", []))
        if entry is not None and registry_mod.is_expired(entry, self.today):
            base.update({"verdict": "not_flagged", "reasons": reasons})
            return base
        if (
            entry is not None
            and registry_mod.is_flagged(entry, self.today)
            and self._evidence_ok(entry)
        ):
            base.update(
                {
                    "verdict": "flagged",
                    "flagged": True,
                    "exit_code": EXIT_FLAGGED,
                    "reasons": reasons,
                }
            )
            return base
        if entry is not None and count >= 1:
            base.update({"verdict": "under_review", "reasons": reasons})
            return base
        reasons.append(
            "Price drop alone is not a rug verdict: this dev has no "
            "verified rugged-mint history in the registry."
            if watch_label == "collapsed"
            else "No verified rugged-mint history for this dev in the registry."
        )
        base.update({"verdict": "unknown", "reasons": reasons})
        return base

    def verdict_for(self, dev_wallet: str, mint: str | None = None, candidate: dict | None = None) -> dict:
        """Convenience wrapper around :meth:`decide`."""
        return self.decide(dev_wallet=dev_wallet, mint=mint, candidate=candidate)


class AlertAgent:
    """Format a daily-alert markdown section from verdicts.

    Honest about blocked scans: when ``scan_blocked`` is True,
    ``checked`` is 0 and no wallet is invented or added — the
    alert says so plainly.
    """

    @staticmethod
    def format_daily_alert(
        date_str: str,
        verdicts: list[dict] | None = None,
        checked: int = 0,
        scan_blocked: bool = True,
        watch_entries: list[dict] | None = None,
    ) -> str:
        verdicts = verdicts or []
        watch_entries = watch_entries or []
        flagged = [v for v in verdicts if v.get("verdict") == "flagged"]
        under_review = [v for v in verdicts if v.get("verdict") == "under_review"]
        lines = [f"# Daily alerts — {date_str}", ""]
        if scan_blocked:
            lines.append("No new verified alerts.")
            lines.append("")
            lines.append(
                "The live scan is blocked because `HELIUS_API_KEY` is not set, "
                "so no new wallets were checked or added today. The daily "
                "scan exits 2 without a key by design — it never invents a "
                "wallet, mint, or outcome."
            )
        else:
            lines.append(
                f"Checked {checked} dev wallet(s) today; "
                f"{len(flagged)} flagged, {len(under_review)} under review."
            )
        lines.append("")
        lines.append(f"- Creations checked: {checked} (scan did not run — no Helius key).")
        lines.append("- Dev wallets with a rugged mint today: 0.")
        lines.append(
            f"- Newly flagged serial ruggers (≥ 2 rugged mints, evidence-linked): "
            f"{len(flagged)}."
        )
        lines.append(
            f"- Under review (1 rugged mint only, NOT flagged): {len(under_review)}."
        )
        for verdict in verdicts:
            lines.append("")
            lines.append(
                f"- {verdict.get('verdict', 'unknown')}: "
                f"{verdict.get('dev_wallet') or verdict.get('mint') or '?'} "
                f"({verdict.get('rugged_mint_count', 0)} rugged mint(s))"
            )
            for reason in verdict.get("reasons", []):
                lines.append(f"  - {reason}")
        if watch_entries:
            lines.append("")
            lines.append("## Pump.fun watch (DexScreener, no key)")
            lines.append("")
            lines.append(DISCLAIMER)
            lines.append("")
            for entry in watch_entries:
                mc = entry.get("market_cap_usd")
                liq = entry.get("liquidity_usd")
                chg = entry.get("change_24h_pct")
                lines.append(
                    f"- {entry.get('symbol', '?')} — "
                    f"${mc:,.0f} MC" if mc is not None else f"- {entry.get('symbol', '?')} — n/a MC"
                )
                lines[-1] += (
                    f" / ${liq:,.0f} liq" if liq is not None else " / n/a liq"
                )
                lines[-1] += (
                    f" / {chg:+.1f}% 24h — {entry.get('watch_label', 'unknown')}"
                    if chg is not None
                    else f" — {entry.get('watch_label', 'unknown')}"
                )
                lines.append(f"  `{entry.get('mint')}`")
        lines.append("")
        lines.append(
            "Source: Helius pump.fun creations + DexScreener outcome data. "
            "Verify every evidence link yourself before acting on a flag."
        )
        return "\n".join(lines)


def pipeline_check(
    registry_data: dict | None = None,
    mint: str | None = None,
    dev_wallet: str | None = None,
    scanner: ScannerAgent | None = None,
    today: date | None = None,
) -> dict:
    """Run Scanner -> Evidence -> Verdict for one mint/dev pair."""
    data = registry_data if registry_data is not None else _default_registry()
    scanner = scanner or ScannerAgent()
    candidate = None
    if mint is not None:
        scanned = scanner.scan([mint])
        candidate = scanned[0] if scanned else None
    evidence = None
    if dev_wallet is not None:
        evidence = EvidenceAgent(data, today=today).collect(dev_wallet, mint=mint)
    verdict = VerdictAgent(data, today=today).decide(
        dev_wallet=dev_wallet, mint=mint, candidate=candidate, evidence=evidence
    )
    return {"candidate": candidate, "evidence": evidence, "verdict": verdict}


def _format_candidate(candidate: dict) -> str:
    mc = candidate.get("market_cap_usd")
    liq = candidate.get("liquidity_usd")
    vol = candidate.get("volume_24h_usd")
    chg = candidate.get("change_24h_pct")
    lines = [
        f"{candidate.get('symbol', '?')} ({candidate.get('mint')})",
        f"  market cap: ${mc:,.0f}" if mc is not None else "  market cap: n/a",
        f"  liquidity: ${liq:,.0f}" if liq is not None else "  liquidity: n/a",
        f"  24h volume: ${vol:,.0f}" if vol is not None else "  24h volume: n/a",
        f"  24h change: {chg:+.1f}%" if chg is not None else "  24h change: n/a",
        f"  watch: {candidate.get('watch_label', 'unknown')}",
        f"  holders: not reported (never invented)",
        f"  {candidate.get('dexscreener_url', '')}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, scanner: ScannerAgent | None = None) -> int:
    """Demo CLI.

    ``python -m src.agents --mint <mint> [--dev <wallet>]``
    ``python -m src.agents --watch [--limit 5]``
    Exit 1 = flagged, 0 = otherwise, 2 = invalid address/args.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm AI agents demo: scan, evidence, verdict, alert."
    )
    parser.add_argument("--mint", type=str, default=None, help="Coin mint address")
    parser.add_argument("--dev", type=str, default=None, help="Dev wallet address")
    parser.add_argument("--watch", action="store_true", help="Show the latest boosted tokens")
    parser.add_argument("--limit", type=int, default=5, help="Watch limit (default: 5)")
    parser.add_argument("--registry", type=str, default=None, help="Registry JSON path")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args(argv)

    if args.registry:
        try:
            data = registry_mod.load_registry(args.registry)
        except (ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_INVALID
    else:
        data = _default_registry()

    if args.watch:
        if args.limit <= 0:
            print("ERROR: --limit must be a positive integer.", file=sys.stderr)
            return EXIT_INVALID
        try:
            candidates = (scanner or ScannerAgent()).watch_latest(limit=args.limit)
        except (RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if args.json:
            print(json.dumps(candidates, indent=2))
        else:
            print("\n".join(_format_candidate(c) for c in candidates) if candidates else "No boosted Solana tokens returned by DexScreener.")
            print("")
            print(DISCLAIMER)
        return EXIT_NOT_FLAGGED

    if not args.mint:
        print("ERROR: give --mint <mint> or --watch.", file=sys.stderr)
        return EXIT_INVALID
    if not registry_mod.is_valid_solana_address(args.mint):
        print(f"ERROR: '{args.mint}' is not a valid Solana address.", file=sys.stderr)
        return EXIT_INVALID
    if args.dev is not None and not registry_mod.is_valid_solana_address(args.dev):
        print(f"ERROR: '{args.dev}' is not a valid Solana address.", file=sys.stderr)
        return EXIT_INVALID

    try:
        result = pipeline_check(data, mint=args.mint, dev_wallet=args.dev, scanner=scanner)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID

    verdict = result["verdict"]
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["candidate"] is not None:
            print(_format_candidate(result["candidate"]))
            print("")
        print(f"Verdict: {verdict['verdict']} (exit code {verdict['exit_code']})")
        for reason in verdict.get("reasons", []):
            print(f"  - {reason}")
        if result["evidence"] is not None:
            print(f"Evidence: {verdict.get('rugged_mint_count', 0)} rugged mint(s); "
                  f"{len(result['evidence']['evidence'])} link(s) collected; "
                  f"{result['evidence']['verdict']}.")
        print("")
        print(DISCLAIMER)
    return verdict["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
