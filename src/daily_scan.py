"""Daily scan: fetch recent pump.fun creations, label outcomes, and
commit only *verified* new serial ruggers.

Rules (non-negotiable, enforced in code and in tests):
- No HELIUS_API_KEY -> exit 2 with a clear message, no file changes.
  A scheduler must report "blocked", never fake data.
- A wallet is added / promoted ONLY with >= 2 rugged mints, each
  with evidence links (Solscan + DexScreener). One rug = under
  review, never flagged, never committed as a new alert.
- A mint labelled ``inconclusive`` or ``failed_but_honest`` is never
  counted as a rug.
- Never invent a wallet or a mint. If nothing verified is found,
  write the day's alert file saying "no new verified alerts".
- Sample data is never committed as real: adding the first real
  entry flips ``sample_data`` to False (see src/registry.py).

Run:
    python -m src.daily_scan [--limit 50] [--dry-run]
    python -m src.daily_scan --help
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .cluster import find_cluster
from .indexer import HeliusIndexer, MintOutcome, TokenCreation
from .labeller import MintEvent, label_outcome
from .registry import (
    DEFAULT_TTL_DAYS,
    MIN_RUGGED_MINTS_TO_FLAG,
    add_or_update_entry,
    get_wallet,
    load_registry,
    rugged_mint_count,
    save_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"
DEFAULT_ALERTS_DIR = PROJECT_ROOT / "data" / "alerts"

MISSING_KEY_MESSAGE = (
    "ERROR: HELIUS_API_KEY is not set.\n"
    "The daily scan cannot fetch real creations without it.\n"
    "Set it locally (cp .env.example .env) or as the HELIUS_API_KEY "
    "GitHub secret — see README 'Daily scan'. No files were changed."
)


@dataclass
class ScanResult:
    today: str
    creations_checked: int = 0
    rugged_by_dev: dict = field(default_factory=dict)  # dev -> [mint dicts]
    new_entries: list = field(default_factory=list)  # wallets newly flagged
    updated_entries: list = field(default_factory=list)  # wallets updated
    under_review: list = field(default_factory=list)  # devs with 1 rug only
    registry_changed: bool = False


def outcome_to_mint_dict(
    creation: TokenCreation, outcome: MintOutcome
) -> dict:
    """A labelled rugged mint, in registry.json shape, with evidence."""
    event = MintEvent(
        peak_market_cap_usd=outcome.peak_market_cap_usd,
        current_market_cap_usd=outcome.current_market_cap_usd,
        lp_removed=outcome.lp_removed,
        dev_sold_pct=outcome.dev_sold_pct,
        time_to_collapse_minutes=outcome.time_to_collapse_minutes,
    )
    collapse = None
    if outcome.peak_market_cap_usd and outcome.current_market_cap_usd is not None:
        collapse = round(
            100.0
            * (1.0 - outcome.current_market_cap_usd / outcome.peak_market_cap_usd),
            1,
        )
    return {
        "mint": creation.mint,
        "outcome": label_outcome(event),
        "peak_market_cap_usd": outcome.peak_market_cap_usd,
        "current_market_cap_usd": outcome.current_market_cap_usd,
        "collapse_pct": collapse,
        "lp_removed": outcome.lp_removed,
        "dev_sell_pct": outcome.dev_sold_pct,
        "dev_sold": outcome.dev_sold_pct > 0,
        "time_to_collapse_minutes": outcome.time_to_collapse_minutes,
        "evidence": outcome.evidence,
    }


def scan(indexer: HeliusIndexer, data: dict, today: date, limit: int = 50) -> ScanResult:
    """One scan pass over recent creations. Pure-ish: takes an indexer
    (real or fake in tests) and a loaded registry dict; returns what
    would change, and mutates ``data`` only via add_or_update_entry
    for verified >= 2-rug wallets. Caller decides whether to save."""
    result = ScanResult(today=today.isoformat())
    creations = indexer.fetch_new_mints(limit=limit)
    result.creations_checked = len(creations)

    # Group rugged mints by dev wallet, deduped by mint.
    rugged_by_dev: dict[str, list[dict]] = {}
    seen_mints: set[str] = set()
    for creation in creations:
        if creation.mint in seen_mints:
            continue
        seen_mints.add(creation.mint)
        outcome = indexer.fetch_mint_outcome(creation.mint, creation.dev_wallet)
        mint_dict = outcome_to_mint_dict(creation, outcome)
        if mint_dict["outcome"] != "rugged":
            continue
        rugged_by_dev.setdefault(creation.dev_wallet, []).append(mint_dict)
    result.rugged_by_dev = rugged_by_dev

    # Funding transfers for clustering (best-effort; empty on failure).
    devs = sorted(rugged_by_dev)
    transfers: list[dict] = []
    if devs:
        try:
            transfers = indexer.fetch_funding_transfers(devs)
        except Exception:
            transfers = []

    for dev in devs:
        new_rugs = rugged_by_dev[dev]
        existing = get_wallet(data, dev)
        already = (
            [m for m in existing.get("rugged_mints", []) if m.get("outcome") == "rugged"]
            if existing
            else []
        )
        known_mints = {m.get("mint") for m in already}
        fresh = [m for m in new_rugs if m.get("mint") not in known_mints]
        total_rugged = len(already) + len(fresh)
        if total_rugged < MIN_RUGGED_MINTS_TO_FLAG:
            result.under_review.append(dev)
            continue
        cluster = find_cluster(transfers, dev)
        cluster_id = cluster["cluster_id"] if len(cluster["members"]) > 1 else f"cluster-{dev[:8].lower()}"
        entry = {
            "wallet": dev,
            "cluster_id": cluster_id,
            "status": "flagged",
            # first_seen is the first time *we* recorded the wallet.
            "first_seen": existing.get("first_seen") if existing else today.isoformat(),
            "last_seen": today.isoformat(),
            "ttl_days": DEFAULT_TTL_DAYS,
            "rugged_mints": already + fresh,
            "notes": (
                "Added by daily scan: "
                f"{total_rugged} rugged mints with on-chain evidence "
                f"(DexScreener + Solscan). Verified {today.isoformat()}."
            ),
            "dispute_notes": "",
        }
        changed, reason = add_or_update_entry(data, entry, today=today)
        if not changed:
            continue
        result.registry_changed = True
        if existing is None:
            result.new_entries.append(dev)
        else:
            result.updated_entries.append(dev)
    return result


def render_alerts(result: ScanResult) -> str:
    """Markdown for data/alerts/YYYY-MM-DD.md — never invents a wallet."""
    lines = [
        f"# Daily alerts — {result.today}",
        "",
        f"Creations checked: {result.creations_checked}.",
        f"Dev wallets with a rugged mint today: {len(result.rugged_by_dev)}.",
        "",
    ]
    if result.new_entries:
        lines.append("## Newly flagged serial ruggers (≥ 2 rugged mints, evidence-linked)")
        lines.append("")
        for dev in result.new_entries:
            rugs = result.rugged_by_dev.get(dev, [])
            lines.append(f"- `{dev}` — {len(rugs)} rugged mint(s) in this scan:")
            for m in rugs:
                lines.append(
                    f"  - mint `{m['mint']}` — collapse {m.get('collapse_pct')}%"
                    f", LP removed: {m.get('lp_removed')}"
                    f", evidence: {m.get('evidence', {}).get('dexscreener', '')}"
                )
        lines.append("")
    if result.updated_entries:
        lines.append("## Updated entries (new rugged mint added to an existing wallet)")
        lines.append("")
        for dev in result.updated_entries:
            lines.append(f"- `{dev}`")
        lines.append("")
    if result.under_review:
        lines.append("## Under review — 1 rugged mint only, NOT flagged")
        lines.append("")
        for dev in result.under_review:
            lines.append(f"- `{dev}` — needs a second confirmed rug before flagging.")
        lines.append("")
    if not result.new_entries and not result.updated_entries:
        lines.append("No new verified alerts today.")
        lines.append("")
        lines.append(
            "No wallet met the ≥ 2 rugged mints + evidence rule. "
            "Single-rug wallets are listed under review above, if any."
        )
        lines.append("")
    lines.append(
        "Source: Helius pump.fun creations + DexScreener outcome data. "
        "Verify every evidence link yourself before acting on a flag."
    )
    lines.append("")
    return "\n".join(lines)


def write_alerts(result: ScanResult, alerts_dir: Path) -> Path:
    alerts_dir.mkdir(parents=True, exist_ok=True)
    path = alerts_dir / f"{result.today}.md"
    path.write_text(render_alerts(result), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily rug-farm scan.")
    parser.add_argument("--limit", type=int, default=50, help="Max creations to check")
    parser.add_argument(
        "--registry", type=str, default=str(DEFAULT_REGISTRY_PATH),
        help="Registry JSON path",
    )
    parser.add_argument(
        "--alerts-dir", type=str, default=str(DEFAULT_ALERTS_DIR),
        help="Directory for daily alert markdown files",
    )
    parser.add_argument(
        "--date", type=str, default=None, help="Scan date YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and print the result, but do not save registry/alerts",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        print(MISSING_KEY_MESSAGE, file=sys.stderr)
        return 2

    today = date.fromisoformat(args.date) if args.date else date.today()
    registry_path = Path(args.registry)
    data = load_registry(registry_path)
    indexer = HeliusIndexer(api_key=api_key)
    try:
        result = scan(indexer, data, today, limit=args.limit)
    except Exception as exc:  # noqa: BLE001 — report, don't fake a run
        print(f"ERROR: daily scan failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(render_alerts(result))
        print(f"(dry run — registry {'would change' if result.registry_changed else 'unchanged'})")
        return 0

    if result.registry_changed:
        save_registry(data, registry_path, today=today)
    alert_path = write_alerts(result, Path(args.alerts_dir))
    print(f"Scan {result.today}: checked {result.creations_checked} creations, "
          f"new flagged: {len(result.new_entries)}, "
          f"updated: {len(result.updated_entries)}, "
          f"under review: {len(result.under_review)}.")
    print(f"Alerts written to {alert_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
