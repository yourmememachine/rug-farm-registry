# Changelog

All notable changes to the Open Rug-Farm Registry.

## v1.1 — 2026-11-12

- Added `src/check_cli.py`: command-line pre-trade check
  (`python -m src.check_cli <dev_wallet> [--mint ...]`) — prints
  flagged / watch / expired / unknown with reasons and evidence
  links, exit 1 for flagged, 0 for unknown / watch / expired,
  2 for an invalid address. Reuses `src/registry.check_wallet`;
  no duplicated flagging rules.
- Added `tests/test_check_cli.py` (7 tests: flagged, flagged with
  `--mint`, unknown, under review, expired, invalid address,
  invalid mint).
- Daily status `data/alerts/2026-11-12.md`: no new verified alerts —
  the live scan is blocked because `HELIUS_API_KEY` is not set, so
  no wallets were checked or added. No sample wallet is presented
  as real.

## v1.0 — 2026-11-12

- Initial release: git-versioned registry (`data/registry.json`,
  sample data only), labeller, funding-graph clusterer, Helius
  pump.fun indexer + DexScreener outcomes, FastAPI
  (`/health`, `/registry`, `/wallets/{address}`, `/check`,
  `/clusters/{cluster_id}`), daily scan CLI + GitHub Actions
  workflow (daily 09:00 UTC), 37 tests.
