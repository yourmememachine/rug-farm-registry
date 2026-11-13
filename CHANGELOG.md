# Changelog

All notable changes to the Open Rug-Farm Registry.

## v2.0 — 2026-11-13

- Added `src/multichain.py`: multi-chain support, stdlib-only,
  no wallet, no funds moved, no private key. ChainAdapter base
  class with one shared probe return shape (chain, address,
  balance, kind, owner, contract flag, explorer + DexScreener
  links, available / error); SolanaAdapter (public Solana RPC
  probe, Solscan links) and BaseAdapter (public Base RPC
  https://mainnet.base.org, no key: eth_getBalance wei -> ETH,
  eth_getCode for contract detection, Basescan links).
  MultiChainRegistry routes a check to the right adapter and
  adds the registry verdict — chain-independent, flagged only
  on >= 2 rugged mints with evidence, on any chain. The
  registry is still Solana sample data only, so a Base check
  honestly reports its registry verdict as unknown ("no Base
  registry coverage yet") while the probe still works.
  ChainCompare puts the same token data on two chains side by
  side (liquidity / market cap / 24h change, watch / ok per
  chain, never "rugged"). Demo CLI
  `python -m src.multichain --chain solana|base --address <addr>`
  / `--compare` / `--list-chains`; exit 0 = ok, 2 = invalid
  args / chain / address.
- Added `tests/test_multichain.py` (23 tests, synthetic data
  only, no network: adapter routing, Base explorer URLs, Base
  balance parsing, Solana vs Base shape parity, unsupported
  chain, RPC failure honesty, compare labels, CLI exit codes).
- README: new "Multi-chain (v2.0)" section.
- Roadmap note: future evidence work is done by Muse itself —
  automated research plus Helius / DexScreener evidence
  gathering — not by the community. No community dependency
  anywhere in the roadmap.
- No registry entries added: registry is still sample data only.

## v1.9 — 2026-11-13

- Added `src/web3_future.py`: future-facing web3 module,
  stdlib-only, no wallet, no SOL spent, no private key.
  FutureChain (chain-agnostic registry design: Solana live
  today, Base planned with explorer URLs ready, verdicts never
  depend on the chain), OnChainProbe (public Solana RPC, no
  key: SOL balance, account owner, token mint vs system
  wallet, honest "unavailable" on RPC failure), FutureWatch
  (forward-looking risk signals from real DexScreener data
  only: thin liquidity < $1,000, collapsed <= -70% 24h, no
  liquidity reported — labels watch / ok only, never
  "rugged"), RoadmapAgent (next 3 versions as markdown: v2.0
  multi-chain, v2.1 Helius live scan, v2.2 Muse-run evidence
  gathering (no community dependency); dates always TBD). Demo CLI
  `python -m src.web3_future --wallet <address>` / `--roadmap`
  / risk flags; exit 0 = ok, 2 = invalid args.
- Added `tests/test_web3_future.py` (19 tests, synthetic data
  only, no network: chain explorer URLs, solana-only guard,
  probe balance / owner / mint-vs-wallet, RPC failure
  handling, future-risk labels, roadmap contents, CLI exit
  codes). 151 tests total.
- README: new "Web3 future" section.
- No registry entries added: registry is still sample data only.
- Note: the user message "eeb3" was unclear; interpreted as
  "build with web3, think the future" and built this module.

## v1.8 — 2026-11-13

- Added `src/creativity.py`: 4 small, deterministic, stdlib-only
  creativity agents — NameAgent (3 original nickname ideas,
  deterministic from name/ticker/idea + seed, distinct from the
  input name, profanity blocklist), StoryAgent (2-3 sentence
  plain-English story of a verdict, honest "not proven a rug"
  line unless flagged), VisualAgent (badge spec: label, colour
  name, emoji, summary — collapsed red / thin amber / active
  green — plus an ASCII sparkline from your own price-change
  numbers), PostAgent (ready-to-post X draft, always <= 280
  chars, always #memes #rug #pumpfun + repo link, variants for
  flagged / watch-only / active, never invents a price or mint).
  Demo CLI `python -m src.creativity --verdict flagged --symbol
  XYZ --mc 1000000 --change -80` with --story / --post / --badge
  / --names modes; exit 0 = ok, 2 = invalid args. Negative
  numbers work as option values (e.g. --change -80).
- Added `tests/test_creativity.py` (23 tests, synthetic data only,
  no network): name distinctness / determinism / profanity,
  story never claims a rug when not flagged, badge colours,
  sparkline, post length / hashtags / repo link, CLI exit codes.
  132 tests total.
- README: new "Creativity agents" section.
- No registry entries added: registry is still sample data only.

## v1.7 — 2026-11-13

- Added `src/agents.py`: 4 small, deterministic, stdlib-only AI
  agents that work together — ScannerAgent (live DexScreener
  data via `src.pump_watch`, watch labels only, never invents
  holder data, never labels "rugged"), EvidenceAgent (evidence
  links + registry check, clear "insufficient evidence" verdict
  when data is missing), VerdictAgent (registry rule only:
  flagged only with >= 2 rugged mints with evidence per mint,
  1 rug = under_review, price drop alone never flags), AlertAgent
  (daily-alert markdown, honest about blocked scans: no Helius
  key = 0 checked, no invented wallets). Demo CLI
  `python -m src.agents --mint <mint> [--dev <wallet>]` and
  `--watch --limit 5`; exit 1 = flagged, 0 = otherwise,
  2 = invalid.
- Added `tests/test_agents.py` (24 tests, all mocked, no network):
  scanner labels, evidence missing-data, verdict
  flagged/under_review/not_flagged, price-drop-alone never
  flagged, alert formatting, demo CLI exit codes. 109 tests total.
- README: new "AI agents" section.
- No registry entries added: registry is still sample data only.

## v1.6 — 2026-11-13

- Daily alert for 2026-11-13: no new verified alerts — live scan
  still blocked without `HELIUS_API_KEY`, by design, no wallets
  invented or added. Pump.fun watch (DexScreener, 07:18 UTC):
  GATO $5.0M MC leads, NIHAOSON -91.5% collapsed (watch only),
  SKT $458 liquidity thin (watch only). 85 tests passed.
- No registry entries added: registry is still sample data only.

## v1.5 — 2026-11-12

- Added `src/rugfarm.py`: single-file, stdlib-only SDK developers
  can copy into their own projects (no extra dependencies).
  `RugFarm.check_dev(wallet)` returns verdict
  (flagged / under_review / not_flagged / unknown), rugged mint
  count, rugged mints with evidence links, reasons and the
  check_cli-style exit code (1 = flagged, 0 = otherwise, 2 =
  invalid address). `check_mint(mint)` returns the registry
  verdict + dev wallet + collapse % for known mints, or live
  DexScreener market data (no key) with a watch label only
  (`collapsed` / `thin_liquidity` / `active`) for unknown mints —
  never "rugged" without registry evidence. `is_flagged_dev` /
  `is_rugged_mint` one-liner helpers (registry-only, no network).
  `watch(limit=10)` returns the live boosted-token watchlist,
  `[]` on network failure instead of invented numbers.
  Demo: `python -m src.rugfarm <wallet-or-mint>`.
- Added `examples/use_rugfarm.py`: short copy-paste demo
  (check_dev / check_mint / watch), verified live — the watch
  section returns real boosted tokens (Huang, CBUCKS, WSB,
  POPVAX collapsed, ZEN).
- Added `tests/test_rugfarm.py` (26 tests: flagged 3-rug and
  2-rug devs, under-review 1-rug dev, expired dev, unknown dev,
  known / unrelated mint, live collapsed / thin / active labels,
  no-network fallback, invalid addresses, sample-data marking,
  demo CLI exit codes). 85 tests total (59 baseline + 26 new).
- README: new "Clone & use in your project" section
  (`git clone https://github.com/yourmememachine/rug-farm-registry`,
  3 code snippets, the flagging rules).
- No registry entries added: registry is still sample data only.

## v1.4 — 2026-11-12 (unpushed, local)

- Fixed a live crash in v1.3: `python -m src.pump_watch` crashed
  before any request with `httpx.InvalidURL: Invalid port: ':1]'`
  whenever `NO_PROXY` contained bracketed IPv6 entries (e.g.
  `[::1]`), even though all 57 tests passed (tests inject a
  MockTransport client and never build a real one). New shared
  `src/http_client.py::make_client` builds the real client with
  `trust_env=False`, the proxy passed explicitly from
  `https_proxy`/`http_proxy`, and the TLS bundle from
  `SSL_CERT_FILE` when set. Used by `src/pump_watch.py` and both
  live paths in `src/indexer.py`. Verified live after the fix:
  `python -m src.pump_watch --limit 5` returns real boosted tokens
  (Huang, CBUCKS, WSB, POPVAX collapsed -77%, ZEN).
- Added `tests/test_http_client.py` (2 tests: builds with bracketed
  IPv6 `NO_PROXY`, explicit proxy + timeout). 59 tests total.
- No registry entries added: registry is still sample data only;
  real entries still need `HELIUS_API_KEY` (see v1.0–v1.3 notes).

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
