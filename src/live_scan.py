"""Live Helius scan (v2.1): bounded scan of recent pump.fun creations.

Runs the real scan the daily GitHub Action is built around, as a
reusable module + CLI:

- ``HeliusClient`` — thin wrapper over ``src.indexer.HeliusIndexer``.
  Reads ``HELIUS_API_KEY`` only from the environment (never hardcoded,
  never logged, never committed). Disabled without a key: every live
  method raises a clear ``RuntimeError`` instead of faking data.
  All HTTP goes through an injectable ``httpx.Client``, so tests use
  ``httpx.MockTransport`` and never make a live call.
- ``LiveScanner`` — bounded scan: fetch N recent pump.fun creations,
  get each mint's DexScreener outcome, label it with
  ``src.labeller``. A dev is flagged **only** with >= 2 rugged mints,
  each with evidence links (scan findings + registry record combined,
  deduped by mint). One rugged mint = under review, never flagged.
  A price drop alone never flags — the labeller needs collapse +
  LP removal / dev sell, and a mint with no pair is inconclusive.
- ``EvidenceGatherer`` — Muse-run evidence (no community): for a
  flagged candidate, builds DexScreener / Solscan / pump.fun /
  Helius links and a short summary from real data only. Missing
  data -> "insufficient evidence", never a silent pass.

Run:
    python -m src.live_scan --limit 10 [--dry-run] [--json]
    # --dry-run prints the plan and makes NO Helius call.
    # Without HELIUS_API_KEY it exits 2 and changes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field

import httpx

from .indexer import (
    DEXSCREENER_URL,
    PUMP_FUN_PROGRAM,
    HeliusIndexer,
    MintOutcome,
    TokenCreation,
    get_api_key_from_env,
)
from .labeller import MintEvent, label_outcome
from .registry import MIN_RUGGED_MINTS_TO_FLAG

MISSING_KEY_MESSAGE = (
    "HELIUS_API_KEY is not set — scan blocked. "
    "Set it in the environment / .env, or as the HELIUS_API_KEY "
    "GitHub secret. No live calls are made without a key, and no "
    "data is invented."
)

EXIT_OK = 0
EXIT_BLOCKED = 2


class HeliusClient:
    """Helius JSON-RPC client for pump.fun creations.

    The API key lives only in the environment. It is passed to Helius
    in the request URL (as Helius requires) but is never printed,
    logged, or stored in any file by this module.
    """

    def __init__(
        self,
        api_key: str | None = None,
        env: dict | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        source = env if env is not None else os.environ
        self.api_key = api_key if api_key is not None else get_api_key_from_env(source)
        self._indexer = HeliusIndexer(api_key=self.api_key, client=client)

    @property
    def enabled(self) -> bool:
        return self.api_key is not None

    def require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError(MISSING_KEY_MESSAGE)

    # -- Signatures / transactions ------------------------------------
    def get_signatures(self, address: str, limit: int = 50) -> list[dict]:
        """getSignaturesForAddress for a program / wallet address."""
        self.require_enabled()
        return self._indexer.get_signatures(address, limit=limit)

    def get_transaction(self, signature: str) -> dict | None:
        """One parsed transaction, or None if it cannot be fetched."""
        self.require_enabled()
        return self._indexer.get_transaction(signature)

    @staticmethod
    def parse_transaction(tx: dict) -> TokenCreation | None:
        """Parse a getTransaction result into mint + dev wallet."""
        return HeliusIndexer.parse_transaction(tx)

    def fetch_new_mints(self, limit: int = 50) -> list[TokenCreation]:
        """Recent pump.fun token creations, newest first, deduped."""
        self.require_enabled()
        return self._indexer.fetch_new_mints(limit=limit)

    def fetch_mint_outcome(
        self, mint: str, dev_wallet: str | None = None
    ) -> MintOutcome:
        """Outcome evidence for one mint from DexScreener (public API)."""
        return self._indexer.fetch_mint_outcome(mint, dev_wallet=dev_wallet)


def _evidence_for(mint: str, dev_wallet: str | None, signature: str | None) -> dict:
    evidence = {
        "dexscreener": f"https://dexscreener.com/solana/{mint}",
        "solscan_mint": f"https://solscan.io/token/{mint}",
        "pumpfun": f"https://pump.fun/coin/{mint}",
    }
    if dev_wallet:
        evidence["solscan_wallet"] = f"https://solscan.io/account/{dev_wallet}"
    if signature:
        evidence["helius_tx"] = f"https://explorer.helius.xyz/tx/{signature}"
    return evidence


@dataclass
class ScanReport:
    checked: int = 0
    blocked: bool = False
    blocked_reason: str | None = None
    flagged: list[dict] = field(default_factory=list)
    under_review: list[dict] = field(default_factory=list)
    not_flagged: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "flagged": self.flagged,
            "under_review": self.under_review,
            "not_flagged": self.not_flagged,
        }


class LiveScanner:
    """Bounded live scan over recent pump.fun creations.

    ``client`` is any object with ``enabled``, ``fetch_new_mints`` and
    ``fetch_mint_outcome`` (a real ``HeliusClient`` in production, a
    stub in tests). ``registry_data`` is the loaded registry dict
    (``{}`` = no registry record); rugged mints already in the
    registry count toward the >= 2 rule, deduped by mint.
    """

    def __init__(self, client: HeliusClient, registry_data: dict | None = None) -> None:
        self.client = client
        self.registry_data = registry_data or {}

    def scan(self, limit: int = 10) -> ScanReport:
        if not self.client.enabled:
            return ScanReport(blocked=True, blocked_reason=MISSING_KEY_MESSAGE)
        if limit < 1:
            raise ValueError("limit must be >= 1")
        creations = self.client.fetch_new_mints(limit=limit)
        report = ScanReport(checked=len(creations))
        # Group rugged-labelled mints by dev wallet.
        by_dev: dict[str, list[dict]] = {}
        for creation in creations:
            outcome = self.client.fetch_mint_outcome(
                creation.mint, dev_wallet=creation.dev_wallet
            )
            label = label_outcome(
                MintEvent(
                    peak_market_cap_usd=outcome.peak_market_cap_usd,
                    current_market_cap_usd=outcome.current_market_cap_usd,
                    lp_removed=outcome.lp_removed,
                    dev_sold_pct=outcome.dev_sold_pct,
                    time_to_collapse_minutes=outcome.time_to_collapse_minutes,
                )
            )
            entry = {
                "mint": creation.mint,
                "dev_wallet": creation.dev_wallet,
                "outcome": label,
                "market_cap_usd": outcome.current_market_cap_usd,
                "liquidity_usd": None,
                "evidence": _evidence_for(
                    creation.mint, creation.dev_wallet, creation.signature
                ),
            }
            if label != "rugged":
                # A price drop alone is not a rug: failed_but_honest /
                # inconclusive mints are recorded, never flagged.
                report.not_flagged.append(entry)
                continue
            by_dev.setdefault(creation.dev_wallet, []).append(entry)
        for dev, rugged in by_dev.items():
            # Merge with the registry record for this dev, deduped.
            raw = None
            for e in self.registry_data.get("entries", []) or []:
                if e.get("dev_wallet") == dev:
                    raw = e
                    break
            registry_mints = [
                {
                    "mint": m.get("mint"),
                    "dev_wallet": dev,
                    "outcome": "rugged",
                    "market_cap_usd": None,
                    "liquidity_usd": None,
                    "evidence": m.get("evidence", {}),
                }
                for m in (raw or {}).get("rugged_mints", []) or []
            ]
            seen = {r["mint"] for r in rugged}
            merged = list(rugged) + [m for m in registry_mints if m["mint"] not in seen]
            candidate = {
                "dev_wallet": dev,
                "rugged_mint_count": len(merged),
                "rugged_mints": merged,
            }
            if len(merged) >= MIN_RUGGED_MINTS_TO_FLAG:
                report.flagged.append(candidate)
            else:
                report.under_review.append(candidate)
        return report


class EvidenceGatherer:
    """Muse-run evidence for a flagged candidate (no community).

    Builds the evidence links + summary from real data only. A
    candidate with no usable evidence is reported as insufficient,
    never silently passed.
    """

    @staticmethod
    def gather(candidate: dict) -> dict:
        dev = candidate.get("dev_wallet")
        mints = candidate.get("rugged_mints") or []
        links: list[str] = []
        for m in mints:
            evidence = m.get("evidence") or {}
            for key in ("dexscreener", "solscan_mint", "pumpfun", "helius_tx"):
                if evidence.get(key):
                    links.append(evidence[key])
            if evidence.get("solscan_wallet"):
                links.append(evidence["solscan_wallet"])
        links = sorted(set(links))
        has_mint_evidence = any(
            (m.get("evidence") or {}).get("dexscreener")
            and (m.get("evidence") or {}).get("solscan_mint")
            for m in mints
        )
        sufficient = bool(dev) and len(mints) >= MIN_RUGGED_MINTS_TO_FLAG and has_mint_evidence
        if sufficient:
            summary = (
                f"Dev {dev} is flagged: {len(mints)} rugged mints, "
                f"each with DexScreener + Solscan evidence ({len(links)} links)."
            )
        elif not mints:
            summary = f"No rugged mints with evidence for dev {dev or 'unknown'} — insufficient evidence."
        else:
            summary = (
                f"Dev {dev or 'unknown'} has {len(mints)} rugged mint(s) but "
                "insufficient evidence — do not flag."
            )
        return {
            "dev_wallet": dev,
            "rugged_mint_count": len(mints),
            "links": links,
            "sufficient": sufficient,
            "summary": summary,
        }


def _print_report(report: ScanReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return
    if report.blocked:
        print(f"Scan blocked: {report.blocked_reason}")
        return
    print(f"Checked {report.checked} recent pump.fun creation(s).")
    print(f"Flagged serial ruggers: {len(report.flagged)}")
    for c in report.flagged:
        print(f"  FLAGGED {c['dev_wallet']}: {c['rugged_mint_count']} rugged mints")
    print(f"Under review (1 rugged mint): {len(report.under_review)}")
    for c in report.under_review:
        print(f"  REVIEW {c['dev_wallet']}: {c['rugged_mint_count']} rugged mint(s)")
    print(f"Not flagged: {len(report.not_flagged)} mint(s) — a price drop alone is not a rug.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.live_scan",
        description="Live Helius scan of recent pump.fun creations.",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        print("error: --limit must be >= 1", file=sys.stderr)
        return EXIT_BLOCKED
    if args.dry_run:
        print(
            f"Dry run: would scan the {args.limit} most recent pump.fun "
            "creation(s) via Helius. No Helius call was made."
        )
        return EXIT_OK
    client = HeliusClient()
    if not client.enabled:
        print(f"ERROR: {MISSING_KEY_MESSAGE}", file=sys.stderr)
        return EXIT_BLOCKED
    scanner = LiveScanner(client)
    report = scanner.scan(limit=args.limit)
    _print_report(report, args.json)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
