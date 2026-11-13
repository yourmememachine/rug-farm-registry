"""Dev history (v2.7): paste any dev wallet, get their full mint history.

The missing capability the registry never had: a per-dev view. The
live scan (v2.1) watches *new* creations globally; this module
answers "what has *this* dev launched before, and were any of them
rugs?" — the question you ask before trusting a new mint from a
wallet you have never seen.

- ``DevHistoryBuilder`` — given a dev wallet and a Helius client
  (or caller-supplied parsed transactions + outcomes for tests),
  collects every mint that dev created (signatures -> parse ->
  TokenCreation), labels each mint with ``src.labeller``, attaches
  Solscan / DexScreener / pump.fun / Helius evidence links per
  mint, and applies the registry rule: >= 2 rugged mints, each
  with evidence = ``flagged``; 1 rugged mint = ``under_review``;
  0 rugged mints with real data = ``clean``. Rugged mints already
  in the registry count toward the >= 2 rule, deduped by mint
  (same as the live scan).
- Missing market data (no DexScreener pair) or a failed outcome
  fetch means that mint is ``insufficient_data`` — never a
  verdict, never invented numbers. A dev with no usable data at
  all is ``insufficient_data``, not "clean": a clean verdict
  requires at least one mint with real data behind it.
- ``DevHistory.draft_entry()`` produces a draft registry entry
  (the exact shape ``src.registry.validate_entry`` expects) only
  when the flagged bar is met. It is a *draft*: this module never
  writes the registry itself.
- Reports render as plain text (``to_text``) or JSON (``to_dict``).

Run:
    python -m src.dev_history --wallet <address> [--limit N] [--json]
    # Exit 0 = clean / under review / insufficient data,
    #      1 = flagged,
    #      2 = invalid args / invalid address / no key / network error.
    # Without HELIUS_API_KEY it exits 2 and changes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from .indexer import HeliusIndexer, MintOutcome, TokenCreation
from .labeller import MintEvent, collapse_pct, label_outcome
from .live_scan import MISSING_KEY_MESSAGE, HeliusClient
from .registry import (
    DEFAULT_TTL_DAYS,
    MIN_RUGGED_MINTS_TO_FLAG,
    is_valid_solana_address,
    load_registry,
)

EXIT_OK = 0
EXIT_FLAGGED = 1
EXIT_BLOCKED = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"


def evidence_for(
    mint: str, dev_wallet: str | None, signature: str | None = None
) -> dict:
    """Evidence links for one mint (URLs only — no data is invented)."""
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
class MintRecord:
    """One mint in a dev's history, labelled, with its evidence."""

    mint: str
    dev_wallet: str
    outcome: str  # rugged | failed_but_honest | inconclusive | insufficient_data
    peak_market_cap_usd: float | None = None
    current_market_cap_usd: float | None = None
    lp_removed: bool = False
    dev_sold_pct: float = 0.0
    time_to_collapse_minutes: int | None = None
    evidence: dict = field(default_factory=dict)
    timestamp: str | None = None
    signature: str | None = None
    source: str = "scan"  # "scan" | "registry"
    data_status: str = "ok"  # "ok" | "insufficient_data"

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)

    @property
    def is_rugged(self) -> bool:
        """A rug only counts when it is labelled rugged AND has evidence."""
        return self.outcome == "rugged" and self.has_evidence

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "dev_wallet": self.dev_wallet,
            "outcome": self.outcome,
            "data_status": self.data_status,
            "peak_market_cap_usd": self.peak_market_cap_usd,
            "current_market_cap_usd": self.current_market_cap_usd,
            "collapse_pct": collapse_pct(
                MintEvent(
                    peak_market_cap_usd=self.peak_market_cap_usd,
                    current_market_cap_usd=self.current_market_cap_usd,
                )
            ),
            "lp_removed": self.lp_removed,
            "dev_sold_pct": self.dev_sold_pct,
            "time_to_collapse_minutes": self.time_to_collapse_minutes,
            "evidence": self.evidence,
            "timestamp": self.timestamp,
            "signature": self.signature,
            "source": self.source,
        }


def record_from_outcome(creation: TokenCreation, outcome: MintOutcome) -> MintRecord:
    """Label one scanned mint from its DexScreener outcome.

    No pair / no market-cap numbers -> ``insufficient_data``: the
    labeller cannot judge a mint it has no numbers for, and neither
    can we. Nothing is invented to fill the gap.
    """
    evidence = dict(outcome.evidence) if outcome.evidence else evidence_for(
        creation.mint, creation.dev_wallet, creation.signature
    )
    if (
        outcome.peak_market_cap_usd is None
        or outcome.current_market_cap_usd is None
    ):
        return MintRecord(
            mint=creation.mint,
            dev_wallet=creation.dev_wallet,
            outcome="insufficient_data",
            evidence=evidence,
            timestamp=creation.timestamp,
            signature=creation.signature,
            source="scan",
            data_status="insufficient_data",
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
    return MintRecord(
        mint=creation.mint,
        dev_wallet=creation.dev_wallet,
        outcome=label,
        peak_market_cap_usd=outcome.peak_market_cap_usd,
        current_market_cap_usd=outcome.current_market_cap_usd,
        lp_removed=outcome.lp_removed,
        dev_sold_pct=outcome.dev_sold_pct,
        time_to_collapse_minutes=outcome.time_to_collapse_minutes,
        evidence=evidence,
        timestamp=creation.timestamp,
        signature=creation.signature,
        source="scan",
        data_status="ok",
    )


def record_from_registry_mint(dev_wallet: str, mint: dict) -> MintRecord:
    """A rugged mint already in the registry, as a history record."""
    return MintRecord(
        mint=mint.get("mint"),
        dev_wallet=dev_wallet,
        outcome="rugged",
        peak_market_cap_usd=mint.get("peak_market_cap_usd"),
        current_market_cap_usd=mint.get("current_market_cap_usd"),
        lp_removed=bool(mint.get("lp_removed", False)),
        dev_sold_pct=float(mint.get("dev_sell_pct", 0.0) or 0.0),
        time_to_collapse_minutes=mint.get("time_to_collapse_minutes"),
        evidence=dict(mint.get("evidence") or {}),
        source="registry",
        data_status="ok",
    )


@dataclass
class DevHistory:
    """A dev wallet's full mint history and verdict."""

    dev_wallet: str
    verdict: str  # flagged | under_review | clean | insufficient_data
    mints: list[MintRecord] = field(default_factory=list)
    first_seen: str | None = None
    last_seen: str | None = None
    # Advisory ML score (see src.ml_model.score_dev_history), only
    # set when a model is supplied. It NEVER changes the verdict
    # above — the >= 2 rugged mints + evidence rule is the only
    # flagging path.
    ml_score: dict | None = None

    @classmethod
    def from_records(cls, dev_wallet: str, mints: list[MintRecord]) -> "DevHistory":
        rugged = [m for m in mints if m.is_rugged]
        if len(rugged) >= MIN_RUGGED_MINTS_TO_FLAG:
            verdict = "flagged"
        elif len(rugged) == 1:
            verdict = "under_review"
        elif any(m.data_status == "ok" for m in mints):
            verdict = "clean"
        else:
            # No usable data at all: not "clean", just unknown.
            verdict = "insufficient_data"
        dates = sorted(
            m.timestamp[:10] for m in mints if m.timestamp and len(m.timestamp) >= 10
        )
        return cls(
            dev_wallet=dev_wallet,
            verdict=verdict,
            mints=list(mints),
            first_seen=dates[0] if dates else None,
            last_seen=dates[-1] if dates else None,
        )

    @property
    def checked(self) -> int:
        return len(self.mints)

    @property
    def rugged_mint_count(self) -> int:
        return sum(1 for m in self.mints if m.is_rugged)

    @property
    def rugged_mints(self) -> list[MintRecord]:
        return [m for m in self.mints if m.is_rugged]

    def counts_by_outcome(self) -> dict:
        counts: dict[str, int] = {}
        for m in self.mints:
            counts[m.outcome] = counts.get(m.outcome, 0) + 1
        return counts

    def draft_entry(self, today: date | None = None) -> dict | None:
        """Draft registry entry — only when the flagged bar is met.

        Returns None unless the verdict is ``flagged``. The draft is
        validated by the caller against ``validate_entry`` before
        any use; this module never writes the registry itself.
        """
        if self.verdict != "flagged":
            return None
        today = today or date.today()
        rugged = [
            {
                "mint": m.mint,
                "outcome": "rugged",
                "peak_market_cap_usd": m.peak_market_cap_usd,
                "current_market_cap_usd": m.current_market_cap_usd,
                "evidence": dict(m.evidence),
                "lp_removed": m.lp_removed,
                "dev_sell_pct": m.dev_sold_pct,
                "time_to_collapse_minutes": m.time_to_collapse_minutes,
            }
            for m in self.rugged_mints
            # Defensive: a rugged mint must carry real numbers and
            # evidence, or it does not go in the draft.
            if m.peak_market_cap_usd is not None
            and m.current_market_cap_usd is not None
            and m.has_evidence
        ]
        if len(rugged) < MIN_RUGGED_MINTS_TO_FLAG:
            return None
        return {
            "wallet": self.dev_wallet,
            "cluster_id": f"cluster-{self.dev_wallet[:8].lower()}",
            "status": "flagged",
            "first_seen": self.first_seen or today.isoformat(),
            "last_seen": self.last_seen or today.isoformat(),
            "ttl_days": DEFAULT_TTL_DAYS,
            "rugged_mints": rugged,
        }

    def to_dict(self) -> dict:
        return {
            "dev_wallet": self.dev_wallet,
            "verdict": self.verdict,
            "checked": self.checked,
            "rugged_mint_count": self.rugged_mint_count,
            "counts_by_outcome": self.counts_by_outcome(),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "ml_score": self.ml_score,
            "mints": [m.to_dict() for m in self.mints],
            "draft_entry": self.draft_entry(),
        }

    def to_text(self) -> str:
        lines = [
            f"Dev wallet: {self.dev_wallet}",
            f"Verdict: {self.verdict.upper()}",
            f"Mints checked: {self.checked} "
            f"({self.rugged_mint_count} rugged with evidence)",
            f"First seen: {self.first_seen or '-'}",
            f"Last seen: {self.last_seen or '-'}",
            "",
        ]
        if not self.mints:
            lines.append("No mints found for this wallet.")
        for m in self.mints:
            collapse = collapse_pct(
                MintEvent(
                    peak_market_cap_usd=m.peak_market_cap_usd,
                    current_market_cap_usd=m.current_market_cap_usd,
                )
            )
            numbers = (
                f"peak ${m.peak_market_cap_usd:,.0f} -> "
                f"current ${m.current_market_cap_usd:,.0f} "
                f"(-{collapse:.1f}%)"
                if collapse is not None
                else "no market data"
            )
            lines.append(f"  [{m.outcome}] {m.mint} — {numbers}")
            if m.lp_removed:
                lines.append("    LP removed")
            if m.dev_sold_pct:
                lines.append(f"    Dev sold {m.dev_sold_pct:.0f}% of allocation")
            for key, url in sorted(m.evidence.items()):
                lines.append(f"    {key}: {url}")
        if self.verdict == "flagged":
            lines.append("")
            lines.append(
                "Draft registry entry available (see --json). "
                "Review it before adding anything to the registry."
            )
        if self.ml_score is not None:
            lines.append("")
            lines.append(
                "ML risk (advisory only — never changes the verdict "
                "above): "
                + (
                    f"{self.ml_score.get('band', '?').upper()} "
                    f"(max mint probability "
                    f"{self.ml_score.get('max_probability')})"
                    if self.ml_score.get("max_probability") is not None
                    else "no mint could be scored"
                )
            )
        if self.verdict == "insufficient_data":
            lines.append("")
            lines.append(
                "No usable market data for this wallet's mints — "
                "this is NOT a clean bill of health, just unknown."
            )
        lines.append("")
        lines.append(
            "This is a risk signal, not a verdict. "
            "Verify every evidence link yourself before acting on a flag."
        )
        return "\n".join(lines)


class DevHistoryBuilder:
    """Build a dev's mint history from a Helius client or supplied data.

    ``client`` is any object with ``enabled``, ``get_signatures``,
    ``get_transaction`` and ``fetch_mint_outcome`` (a real
    ``HeliusClient`` in production, a stub in tests). Pass
    ``creations`` / ``outcomes`` to build purely from
    caller-supplied data — then no key and no network are needed.
    ``registry_data`` is the loaded registry dict (``{}`` = none);
    rugged mints already in the registry count toward the >= 2
    rule, deduped by mint.
    """

    def __init__(self, client, registry_data: dict | None = None) -> None:
        self.client = client
        self.registry_data = registry_data or {}

    def build(
        self,
        dev_wallet: str,
        limit: int = 50,
        creations: list[TokenCreation] | None = None,
        outcomes: dict[str, MintOutcome] | None = None,
        ml_model=None,
    ) -> DevHistory:
        """Build the history; ``ml_model`` (a RugRiskModel, optional)
        adds an advisory ML score — it never changes the verdict."""
        if not is_valid_solana_address(dev_wallet):
            raise ValueError(f"'{dev_wallet}' is not a valid Solana address.")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if creations is None:
            if not self.client.enabled:
                raise RuntimeError(MISSING_KEY_MESSAGE)
            creations = self._fetch_creations(dev_wallet, limit)
        else:
            creations = [
                c for c in creations if c.dev_wallet == dev_wallet
            ][:limit]
        # Dedupe by mint, keep the first (newest) record.
        seen: set[str] = set()
        unique: list[TokenCreation] = []
        for creation in creations:
            if creation.mint in seen:
                continue
            seen.add(creation.mint)
            unique.append(creation)
        records: list[MintRecord] = []
        for creation in unique:
            if outcomes is not None:
                outcome = outcomes.get(creation.mint)
                if outcome is None:
                    # Caller-supplied data with no outcome for this
                    # mint: insufficient data, never a client call
                    # (the client may be disabled in tests).
                    records.append(
                        MintRecord(
                            mint=creation.mint,
                            dev_wallet=creation.dev_wallet,
                            outcome="insufficient_data",
                            evidence=evidence_for(
                                creation.mint,
                                creation.dev_wallet,
                                creation.signature,
                            ),
                            timestamp=creation.timestamp,
                            signature=creation.signature,
                            source="scan",
                            data_status="insufficient_data",
                        )
                    )
                    continue
            else:
                try:
                    outcome = self.client.fetch_mint_outcome(
                        creation.mint, dev_wallet=dev_wallet
                    )
                except Exception:
                    outcome = MintOutcome(
                        mint=creation.mint, dev_wallet=dev_wallet
                    )
            records.append(record_from_outcome(creation, outcome))
        records = self._merge_registry(dev_wallet, records)
        history = DevHistory.from_records(dev_wallet, records)
        if ml_model is not None:
            # Advisory only: the score is attached, the verdict
            # above is untouched (see src.ml_model.score_dev_history).
            from .ml_model import score_dev_history

            history.ml_score = score_dev_history(history, ml_model)
        return history

    def _fetch_creations(self, dev_wallet: str, limit: int) -> list[TokenCreation]:
        """Creations by this dev, from their Helius signature history.

        A single failed transaction fetch is skipped, not fatal —
        the signature listing itself failing *is* fatal.
        """
        signatures = self.client.get_signatures(dev_wallet, limit=limit)
        creations: list[TokenCreation] = []
        for entry in signatures[:limit]:
            sig = entry.get("signature") if isinstance(entry, dict) else None
            if not sig:
                continue
            try:
                tx = self.client.get_transaction(sig)
            except Exception:
                continue
            if tx is None:
                continue
            creation = HeliusIndexer.parse_transaction(tx)
            if creation is None or creation.dev_wallet != dev_wallet:
                continue
            creations.append(creation)
        return creations

    def _merge_registry(
        self, dev_wallet: str, records: list[MintRecord]
    ) -> list[MintRecord]:
        """Fold the dev's existing registry rugged mints into the history."""
        existing = None
        for entry in self.registry_data.get("entries", []) or []:
            if entry.get("wallet") == dev_wallet:
                existing = entry
                break
        if existing is None:
            return records
        known = {m.mint for m in records}
        merged = list(records)
        for mint in existing.get("rugged_mints", []) or []:
            if not isinstance(mint, dict):
                continue
            if mint.get("outcome") != "rugged":
                continue
            if mint.get("mint") in known:
                continue
            if not (mint.get("evidence") or {}):
                continue
            merged.append(record_from_registry_mint(dev_wallet, mint))
            known.add(mint.get("mint"))
        return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.dev_history",
        description="Dev history: paste a dev wallet, get their full mint history and verdict.",
    )
    parser.add_argument("--wallet", required=True, help="Dev wallet address")
    parser.add_argument(
        "--limit", type=int, default=50, help="Max creations to check (default: 50)"
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Registry JSON path to merge (default: data/registry.json if it exists)",
    )
    parser.add_argument(
        "--ml-model",
        type=str,
        default=None,
        help="Trained ML model JSON (src.ml_model) — adds an advisory risk score; never changes the verdict",
    )
    args = parser.parse_args(argv)

    if not is_valid_solana_address(args.wallet):
        print(
            f"ERROR: '{args.wallet}' is not a valid Solana address.",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    if args.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return EXIT_BLOCKED

    client = HeliusClient()
    if not client.enabled:
        print(f"ERROR: {MISSING_KEY_MESSAGE}", file=sys.stderr)
        return EXIT_BLOCKED

    registry_data: dict = {}
    registry_path = args.registry or str(DEFAULT_REGISTRY_PATH)
    if Path(registry_path).exists():
        try:
            registry_data = load_registry(registry_path)
        except (OSError, ValueError) as exc:
            print(
                f"ERROR: cannot load registry {registry_path}: {exc}",
                file=sys.stderr,
            )
            return EXIT_BLOCKED

    builder = DevHistoryBuilder(client, registry_data)
    ml_model = None
    if args.ml_model:
        try:
            from .ml_model import load_model

            ml_model = load_model(args.ml_model)
        except ValueError as exc:
            print(f"ERROR: cannot load ML model {args.ml_model}: {exc}", file=sys.stderr)
            return EXIT_BLOCKED
    try:
        history = builder.build(args.wallet, limit=args.limit, ml_model=ml_model)
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"ERROR: dev history failed: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.json:
        print(json.dumps(history.to_dict(), indent=2))
    else:
        print(history.to_text())
    return EXIT_FLAGGED if history.verdict == "flagged" else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
