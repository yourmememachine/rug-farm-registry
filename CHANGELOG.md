# Changelog

All notable changes to the Open Rug-Farm Registry.

## v1.3 — 2026-11-12 (unpushed, local)

- Added `src/pump_watch.py`: public, no-key pump.fun watchlist
  (`python -m src.pump_watch [--limit 10] [--json]`) — fetches the
  latest boosted tokens from DexScreener's public API (no key) and
  reports mint, symbol, market cap, liquidity, 24h volume and 24h
  change, with a watch label only: `collapsed` (24h <= -70%),
  `thin_liquidity` (liquidity < $1,000), `active` otherwise.
  It NEVER labels a token "rugged" — that needs dev-wallet +
  LP/dev-sell evidence, which this tool does not have. No holder
  data is invented; DexScreener returns none.
- Added `tests/test_pump_watch.py` (11 tests: label thresholds,
  pair parsing, collapsed / thin / active, non-Solana filtering,
  limit, empty boosts, API error, non-list response, bad limit,
  text disclaimer, JSON output). 57 tests total.
- No registry entries added by this change.

## v1.2 — 2026-11-12

- Fixed `src/registry.check_wallet`: a `--mint` that is NOT one of
  the dev's rugged mints no longer reads as if that mint were
  rugged. The verdict stays about the dev, and the result now
  carries `mint_in_registry` (true / false / null) plus a reason
  saying the mint is not one of the dev's rugged mints.
  Same fix applies to the unknown / expired / watch results.
- Added 2 tests in `tests/test_registry.py` (known mint confirmed,
  unrelated mint flagged as not-in-registry while the dev stays
  flagged). 46 tests total.
- No registry entries added: no wallet found in today's public
  research met the >= 2 rugged mints + evidence rule (see
  `data/alerts/2026-11-12.md` — the live scan is still blocked
  without `HELIUS_API_KEY`).

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
