"""Copy-paste example: use the Rug-Farm SDK in your own project.

Run after cloning the repo (from the repo root):

    python examples/use_rugfarm.py

``src/rugfarm.py`` is stdlib-only, so you can also copy that one
file into your own project and import it directly — no ``pip
install`` needed for the SDK itself. Point it at your own copy of
``data/registry.json`` (or pass the loaded dict) with
``RugFarm(registry_path="path/to/registry.json")``.

The sample registry below is SAMPLE DATA ONLY — the sample wallets
are synthetic and are NOT accusations. Real entries need >= 2
rugged mints, each with on-chain evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root: `python examples/use_rugfarm.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.rugfarm import RugFarm  # noqa: E402

# Sample dev wallets from data/registry.json (synthetic, for demo only).
FLAGGED_DEV = "SAMPLEDEV11111111111111111111111111111111"  # 3 rugs -> flagged
UNDER_REVIEW_DEV = "SAMPLEDEV33333333333333333333333333333333"  # 1 rug
RUGGED_MINT = "SAMPLEMNT1111111111111111111111111111111"


def main() -> int:
    rf = RugFarm()  # loads data/registry.json next to the repo

    # 1. Check a dev wallet before trading its coin.
    dev = rf.check_dev(FLAGGED_DEV)
    print(f"Dev {FLAGGED_DEV[:12]}... -> verdict: {dev['verdict']}, "
          f"rugged mints: {dev['rugged_mint_count']}, "
          f"exit code: {dev['exit_code']}")
    for mint in dev["rugged_mints"]:
        print(f"  rugged mint {mint['mint'][:12]}... "
              f"collapse {mint['collapse_pct']}% "
              f"evidence: {mint['evidence']['solscan_mint']}")

    # One-liner helpers for `if` checks in your bot / scanner.
    if rf.is_flagged_dev(UNDER_REVIEW_DEV):
        print("Under-review dev is flagged (unexpected!)")
    else:
        print("Under-review dev (1 rug) is NOT flagged — the >= 2 rule holds.")

    # 2. Check a coin mint: registry verdict + dev wallet, if known.
    coin = rf.check_mint(RUGGED_MINT)
    print(f"Mint {RUGGED_MINT[:12]}... -> verdict: {coin['verdict']}, "
          f"symbol: {coin['symbol']}, dev: {coin['dev_wallet']}, "
          f"collapse: {coin['collapse_pct']}%")

    # 3. Live watchlist (DexScreener public API, no key).
    # Falls back to [] when offline — never invents numbers.
    try:
        for entry in rf.watch(limit=5):
            print(f"Watch {entry['symbol']} ({entry['mint'][:12]}...) "
                  f"MC ${entry['market_cap_usd']:,.0f} "
                  f"watch: {entry['watch_label']}")
    except Exception as exc:  # network is optional for the demo
        print(f"Watch unavailable (offline?): {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
