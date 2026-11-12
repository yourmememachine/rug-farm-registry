"""Command-line pre-trade check for the Open Rug-Farm Registry.

Usage:
    python -m src.check_cli <dev_wallet> [--mint <mint> ...]
    python -m src.check_cli <dev_wallet> --registry data/registry.json

Prints the registry verdict for a dev wallet — flagged / watch /
expired / unknown — with the reasons and evidence links behind it,
then exits:

    0  unknown or watch (or expired: not currently flagged)
    1  flagged (serial rugger, >= 2 rugged mints with evidence)
    2  invalid address, invalid mint, or unreadable registry

All flagging logic lives in ``src.registry.check_wallet`` — this
module only validates addresses, formats the result and maps it to
an exit code. It never invents a wallet, a mint or an outcome.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .registry import check_wallet, is_valid_solana_address, load_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"


def format_result(result: dict, sample_data: bool) -> str:
    """Human-readable verdict, with reasons and evidence links."""
    lines = [
        f"Dev wallet: {result['dev_wallet']}",
        f"Risk level: {result['risk_level']}",
        f"Flagged: {'YES' if result['flagged'] else 'no'}",
        f"Rugged mint count: {result['rugged_mint_count']}",
        f"Cluster: {result['cluster_id'] or '-'}",
    ]
    if result.get("mint"):
        lines.append(f"Mint: {result['mint']}")
    lines.append("")
    lines.append("Reasons:")
    for reason in result["reasons"]:
        lines.append(f"  - {reason}")
    evidence = result.get("evidence") or []
    if evidence:
        lines.append("")
        lines.append("Evidence:")
        for i, mint_evidence in enumerate(evidence, start=1):
            for key, url in sorted(mint_evidence.items()):
                lines.append(f"  [{i}] {key}: {url}")
    if sample_data:
        lines.append("")
        lines.append(
            "NOTE: this registry currently contains SAMPLE DATA ONLY — "
            "sample wallets are synthetic and are NOT accusations."
        )
    lines.append("")
    lines.append(
        "This is a risk signal, not a verdict. "
        "Verify every evidence link yourself before acting on a flag."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-trade check: is this dev wallet a known serial rugger?"
    )
    parser.add_argument("dev_wallet", help="Dev wallet address to check")
    parser.add_argument(
        "--mint",
        dest="mints",
        nargs="+",
        action="append",
        default=None,
        help="Mint address to check against (repeatable, optional)",
    )
    parser.add_argument(
        "--registry",
        type=str,
        default=str(DEFAULT_REGISTRY_PATH),
        help="Registry JSON path (default: data/registry.json)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Check date YYYY-MM-DD for TTL expiry (default: today)",
    )
    args = parser.parse_args(argv)

    if not is_valid_solana_address(args.dev_wallet):
        print(
            f"ERROR: '{args.dev_wallet}' is not a valid Solana address.",
            file=sys.stderr,
        )
        return 2

    mints: list[str] = [
        mint for group in (args.mints or []) for mint in group
    ]
    for mint in mints:
        if not is_valid_solana_address(mint):
            print(
                f"ERROR: mint '{mint}' is not a valid Solana address.",
                file=sys.stderr,
            )
            return 2

    try:
        today = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print(f"ERROR: --date '{args.date}' is not a YYYY-MM-DD date.", file=sys.stderr)
        return 2

    try:
        data = load_registry(args.registry)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot load registry {args.registry}: {exc}", file=sys.stderr)
        return 2

    result = check_wallet(
        data,
        args.dev_wallet,
        mint=mints[0] if mints else None,
        today=today,
    )
    print(format_result(result, sample_data=bool(data.get("sample_data"))))
    return 1 if result["flagged"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
