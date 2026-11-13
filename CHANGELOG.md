# Changelog

All notable changes to the Open Rug-Farm Registry.

## v2.14 — 2026-11-13 — Pro

- Fixes every ordinary part named in the Nov 13 review: JSON-file
  storage, once-a-day scanning, leaning on Helius/DexScreener
  instead of own chain data, and demo-only ML training.
- **SQLite database (`src/db.py`).** Versioned schema with
  migrations (v1 tables, v2 indexes), `init_db` / `migrate`
  upgrades old databases in place. Tables: `mints`, `devs`,
  `transfers` (deduped by signature), `funding_edges` (one row
  per from/to/kind), `outcomes`, `alerts` (one per mint+verdict
  — never re-alerted), `model_runs`, plus a `holders` VIEW
  derived from transfer diffs. All writers are idempotent; a
  second identical write changes nothing. The JSON registry is
  untouched — the daily scan keeps working on it.
- **Own chain index (`src/chain_index.py`).** Reads raw
  transactions through the existing Helius RPC transport
  (transport only — parsing and tables are ours), scoped to
  watched mints and dev wallets; full-chain indexing is
  explicitly out of scope. Parses pump.fun `create`
  instructions (strictly — the balance-diff fallback that would
  falsely call a first buy a "creation" is not used), token
  balance diffs into buy/sell transfers, and system transfers
  into funding edges, writing `mints` / `transfers` /
  `funding_edges` / `holders` into SQLite. No Helius key =
  clear error naming the key, exit 2, nothing written. CLI:
  `python -m src.chain_index --mint <address> | --wallet
  <address>`.
- **Live watcher (`src/live_watch.py`).** Real-time polling
  daemon, default 60s interval (`--interval`), `--once` for
  tests/CI. Helius mode runs the full verdict path (labeller +
  VerdictAgent); without a key it degrades to DexScreener-only
  mode and says so in the log — watch labels only, never a rug
  verdict. The alert decision combines the registry count with
  the newly labelled mint, so a dev with 1 prior rug + 1 new
  rugged mint crosses the >= 2 threshold (the registry itself
  is never mutated — that stays the daily scan's job). Seen
  mints are skipped, alerts are deduped in the DB, alerts go
  to the DB and to `data/alerts/live-YYYY-MM-DD.md` (separate
  from the daily scan's file). Clean SIGINT/SIGTERM exit; a
  failed poll is logged, never kills the daemon. Live smoke
  test: `--once` ran a real DexScreener-only cycle — 27
  candidates, 27 new, 0 alerts (correct: no dev wallets in
  that mode).
- **Proper ML training job (`src/ml_train.py`).** Loads the real
  dataset, rejects any `synthetic` record outright, does a
  deterministic stratified train/validation/test split (a class
  with < 3 examples goes entirely to train, documented in the
  report), trains the existing from-scratch model on the train
  split only, reports real accuracy/precision/recall per split,
  and persists the model as `data/models/<run_id>.json` plus a
  `model_runs` DB row. Run id is a hash of (rule version, seed,
  dataset mints) — re-runs are deterministic updates, not
  duplicates. Refuses with exit 2 and exact counts when data is
  below the model's minimums. First run on the real-v2 dataset:
  train 0.96 accuracy / 0.89 precision / 1.00 recall, val 0.71
  / 0.50 / 1.00, test 1.00 / 1.00 / 1.00 (7 records — small
  sample, honest numbers, not a victory lap).
- 52 new tests (`tests/test_db.py`, `tests/test_chain_index.py`,
  `tests/test_live_watch.py`, `tests/test_ml_train.py` — mocked
  data only, no network). Full suite: **520 tests, all
  passing**.

## v2.13 — 2026-11-13

- Registry rule update: the real-v1 labelling rule was too
  strict for real rugs (v2.11 proved it — all 7 verified
  documented rugs labelled "excluded"), so it is loosened to
  **real-v2**, with a documented justification for every
  changed threshold, grounded in the verified Nov 13 numbers.
  Old vs new, exactly:
  - Branch A (fresh collapse): 24h change <= -70% -> **-90%**
    (TIGHTENED, not loosened — v1's -70% proxy was looser than
    the registry's own >= 90% collapse standard in
    `src/labeller.py`, and a -70% day is ordinary meme
    volatility). Liquidity < $1,000, volume < $1,000, age >= 1
    day unchanged.
  - Branch B (long-dead): liquidity < $100 -> **< $2,500**
    (the 4 verified small rugs hold $1,606-$1,907 residual
    liquidity — real rugged pools almost never drain below
    $100, so the v1 branch almost never fired; $2,500 covers
    the observed range with ~30% headroom, still far below the
    survived floor of $10,000); volume < $100 -> **< $500**
    ("ser" had $413.56 24h volume, so v1's $100 cap excluded
    a verified dead rug); age >= 7 days -> **>= 5 days** (the
    4 small rugs are 5.67-5.94 days old, so v2 catches them
    now, 2026-11-13, instead of ~Nov 19-20; a 5-day floor
    still requires the token to have been dead for days, not
    hours); market cap < $5,000 UNCHANGED (all 4 are
    $1,520-$1,797). Survived rule unchanged. A mint is never
    labelled rugged on liquidity alone — branch B also needs a
    tiny market cap, near-zero volume AND age.
  - The famous three (LIBRA, HAWK, CAR) are STILL excluded:
    they hold $4.4k-$201k liquidity and $870k-$2.9M market
    cap, so no liquidity/market-cap branch catches them — they
    were rugged by insider selling, not pool drainage, and
    catching them needs dev-sell / LP-removal evidence that
    DexScreener does not provide. The rule is not forced to
    catch them.
- `src/labeller.py` (daily-scan labelling) was checked and
  needs NO alignment: it labels on collapse + dev sell % + LP
  removal + time, with no liquidity input at all, so the v1
  liquidity flaw does not apply there. The >= 2 rugged mints +
  evidence flagging rule in `src/registry.py` is UNCHANGED.
- Re-labelled the full dataset with real-v2 (`python -m
  src.real_data --relabel`, new `relabel_records` in
  `src/real_data.py` — re-runs the stored market facts through
  the current rule, no re-fetch, facts untouched): 206
  records, **0 -> 12 rugged-like, 27 survived, 179 -> 167
  excluded** (39 labelled). The 12: the 4 documented small
  rugs (ABDUL, FOMOGIVING, LIQUID, ser) plus 8 DexScreener
  dead clones the v1 rule missed (4x WIF, 2x MOON, 2x FROG;
  market caps $330-$2,179, liquidity $589-$2,493, volume
  $0.50-$85.72, ages 24-1,021 days). Honest sensitivity note:
  one of the 12 (FROG, liquidity $2,492.78) sits within $8 of
  the $2,500 line, so the count is sensitive to that threshold
  choice.
- **First real-data training run.** The model now trains on
  real data: `python -m src.ml_model --train
  data/ml_real_dataset.json --model data/ml_real_model.json`
  — 31 train / 8 held out, loss 0.6931 -> 0.2026,
  **held-out accuracy 1.00, precision 1.00, recall 1.00 (4
  TP, 0 FP, 0 FN, 4 TN)**, model saved to
  `data/ml_real_model.json`. Read honestly: the test set is
  only 8 records (one flip would drop accuracy to 0.875),
  and the weights show what the model actually learned —
  `liquidity_usd` weight -2.605, `collapse_pct` +0.036, the
  other 8 features 0.000 (always missing on DexScreener
  data). It learned "low liquidity -> rugged-like" because
  the label IS largely the liquidity threshold — the metrics
  prove the pipeline works end-to-end on real tokens, NOT
  that the model detects actual rugs. Labels remain
  observable-market-fact labels, not verified rug verdicts.
- 15 new tests in `tests/test_real_v2_rule.py` (mocked data
  only, no network): branch A -90% boundary and the deliberate
  -70% tightening, branch B thresholds one by one, the 7
  documented rugs with the expected outcome per rug,
  survived rule unchanged, missing data never qualifies,
  `relabel_records` + `--relabel` CLI, and the flagging rule
  (>= 2 rugged + evidence) and labeller (no liquidity input,
  collapse 90%) pinned unchanged. Existing branch-A tests
  updated from -70% / -88% to -90% / -92%. Full suite: **468
  tests, all passing**.

## v2.12 — 2026-11-13

- Deep check: the whole manual POOR-coin check as one command
  (`src/deep_check.py`, `python -m src.deep_check --mint
  <address> [--json] [--registry <path>]`). One full card per
  mint: live DexScreener coin data (price, market cap,
  liquidity, 24h volume, 24h change, buy/sell txns, pair age —
  no key, reusing `src/pump_watch.py` patterns), creator/dev
  resolution via Helius DAS `getAsset` with a creation-transaction
  fallback, dev history via the existing `DevHistoryBuilder`,
  side wallets from `fetch_funding_transfers` plus early buyers
  from Helius signatures on the mint (first minutes,
  token-balance diffs), `StealthClusterDetector` over the
  funding events, and `WashScore` only when holder data is
  supplied. The flagging rule is unchanged: `flagged` still
  needs >= 2 rugged mints + evidence; a suspicious pattern
  alone is `high-risk pattern`, never `flagged`. Exit 1 =
  flagged dev, 0 = done (not flagged), 2 = invalid args /
  invalid address / network error. A missing HELIUS_API_KEY
  is NOT an error: the dev half reports "dev unknown — no
  Helius key" and the command completes with the coin half.
  Never invents a creator; honest limits are in the report
  (funding scan covers only the most recent signatures per
  wallet; the early-buyer window may not reach creation; no
  Bubblemaps API — funding clusters only).
- 32 new tests in `tests/test_deep_check.py` (mocked data
  only, no network): coin parsing, creator resolution from
  mocked DAS metadata and from the creation-transaction
  fallback, early-buyer aggregation, side-wallet links
  (including the funder-is-early-buyer POOR pattern),
  cluster wiring, verdicts, report rendering (text + JSON),
  CLI exit codes, and no-key degradation. Full suite: **453
  tests, all passing**.
- Live hand-check: `python -m src.deep_check --mint
  9oJk7eMccQchx3evoN5C5pKQD5Xodb4C1qkhemjjpump` (POOR) with
  no key returns the live coin card (price $0.00003984, market
  cap $39,783, liquidity $14,127, 24h volume $252,694, +25.4%,
  pair created 2026-11-13T18:43:41Z) and "Dev unknown — no
  Helius key", exit 0.

## v2.11 — 2026-11-13

- Added a third real data source for the ML model: documented
  rugged mints (`src/real_data.py` — `load_documented_list`,
  `verify_documented_mints`, `format_documented_summary`),
  because discovery feeds never surface dead rugs. A
  caller-supplied JSON list of {mint, source} pairs (each
  address verbatim from a public source, `source` = that
  source's URL) is verified live via the DexScreener /tokens/
  lookup and labelled with the SAME fixed "real-v1" rule — no
  threshold changes. Invalid addresses are rejected without a
  fetch; mints with no pair yield no record. CLI `python -m
  src.real_data --verify-documented <list.json> [--out
  data/ml_real_dataset.json]`, exit 0 = ok, 2 = invalid args /
  network error / bad list.
- **Researched 14 documented rugged mints**
  (`data/documented_rugged_mints.json`, each with its public
  source URL): LIBRA (Milei, CoinTelegraph/Arkham-documented),
  HAWK (overHere launch, Blockchain Reporter), CAR (Decrypt),
  plus 10 pump.fun mints documented as rugged by
  loopholetape's public rule and 1 graduated control (KNTQ).
  Two famous mints were NOT used: the Gen Z Quant (QUANT)
  mint never appears verbatim in any public source found, and
  an older "Libra" (7Zt2…) on solanatracker is a different
  token from the Milei LIBRA (pair created July 2024,
  Astrofolio) — caught by the DexScreener cross-check and
  dropped.
- **Honest result: still 0 rugged-like, so the model still
  refuses to train and there are still NO real held-out
  metrics.** Live verification on 2026-11-13: 7 of 14 verified
  with a DexScreener pair, 7 dropped (no pair at all —
  confirmed independently for PEACE and MEME, which
  DexScreener returns [] for despite having graduated), 0
  rejected. All 7 verified labelled "excluded" by the fixed
  rule: the famous three still hold real liquidity
  ($4.4k–$201k) and market cap ($870k–$2.9M), so neither
  rugged branch fires; the four loopholetape rugs are nearly
  dead (market cap ~$1.5–1.8k, volume $0.09–$414) but their
  migrated pools still hold ~$1.6–1.9k liquidity (branch B
  needs < $100) and they are 5.7–5.9 days old (branch B needs
  ≥ 7 days). Merged dataset is now 206 records (0
  rugged-like, 27 survived, 179 excluded). No synthetic data
  was used to fake a "real" claim. What would actually
  produce rugged examples under real-v1: a documented rug
  re-checked once its pool drains below $100 liquidity and it
  is ≥ 7 days old, or a rug caught mid-collapse (24h change
  ≤ −70% with < $1k liquidity) — both are timing, not new
  sources.
- Added `tests/test_documented_rugs.py` (12 tests, mocked HTTP
  only, no network). 421 tests total.

## v2.10 — 2026-11-13

- Added a second real data source for the ML model: pump.fun
  launches (`src/real_data.py` — `PumpFunFetcher`,
  `parse_pumpfun_coin`, `fetch_pumpfun_records`,
  `merge_datasets`), because DexScreener discovery only surfaces
  tokens alive enough to be listed. Launches are paged at
  increasing offsets for an age spread, enriched via the
  DexScreener /tokens/ lookup, and labelled with the SAME fixed
  "real-v1" rule — the source only changes which mints are
  sampled, never the labelling. Merge dedupes by mint (existing
  records win) and writes a `<name>.summary.json` sidecar with
  per-source counts. CLI `python -m src.real_data
  --fetch-pumpfun [--pumpfun-limit N] --out
  data/ml_real_dataset.json`, exit 0 = ok, 2 = invalid args /
  network error / too few usable tokens.
- **Honest result: the pump.fun frontend API is BLOCKED from
  this environment, so the dataset is unchanged and the model
  still refuses to train.** Two live fetch attempts on
  2026-11-13: (1) transport error, "peer closed connection
  without sending complete message body (incomplete chunked
  read)"; (2) HTTP 403 Forbidden on `GET /coins?offset=0
  &limit=50&sort=created_timestamp&order=DESC
  &includeNsfw=false`. No records were fetched, nothing was
  merged, and no synthetic data was used to fake a "real" claim.
  `data/ml_real_dataset.json` stays at 199 real tokens (0
  rugged-like, 27 survived, 172 excluded) and `python -m
  src.ml_model --train data/ml_real_dataset.json` still refuses
  with "need at least 2 rugged examples" (exit 2). There are
  still NO real held-out metrics. A real proof needs rugged
  examples from a source reachable from here — e.g. Helius
  dev-wallet histories, which use the already-working Helius key
  instead of a blocked public API.
- Added `tests/test_pumpfun_data.py` (20 tests, mocked HTTP only,
  no network). 409 tests total.

## v2.9 — 2026-11-13

- Added `src/real_data.py`: real-world validation data for the
  ML model — fetches real Solana meme coins from DexScreener's
  public API (no key) and labels them from real observable
  market facts. CLI `python -m src.real_data --fetch [--limit
  N] --out <path> [--json]`, exit 0 = ok, 2 = invalid args /
  network error / too few usable tokens.
- **Real-world result: INSUFFICIENT REAL DATA — the model is
  still unproven on real data, and this release says so
  plainly instead of flattering it.** Dataset
  `data/ml_real_dataset.json` (committed, auditable): 199 real
  tokens fetched 2026-11-13 from token profiles + boosts + 15
  search queries, batched /tokens/ lookups. Labelled by rule
  "real-v1" (fixed BEFORE the fetch, never tuned after seeing
  the data): **0 rugged-like, 27 survived, 172 excluded**
  (ambiguous middle). `python -m src.ml_model --train
  data/ml_real_dataset.json` refuses with "need at least 2
  rugged examples" (exit 2), so there are NO real held-out
  metrics to report — no accuracy, precision or recall on real
  data, and no synthetic data was used to fake a "real" claim.
  Why 0 rugged: DexScreener's public discovery (profiles,
  boosts, search) only surfaces tokens alive enough to be
  listed or searched — dead tokens are invisible to it. All 16
  tokens that fell >= 70% in 24h were under 1 day old (launch
  noise, correctly excluded by the rule's age floor) or still
  liquid and trading; only 1 of 199 tokens had liquidity under
  $1,000, and 0 had liquidity under $100. Feature coverage on
  real data: only collapse % (24h-fall proxy, 138/199) and
  liquidity (138/199) are present — the other 8 of the model's
  10 features (dev sell, LP removed, time-to-collapse, wash,
  holder shares, holder count, cluster size) are always
  missing on DexScreener data, never invented. Next step for a
  real proof: rugged examples need a source that surfaces dead
  tokens (e.g. Helius dev-wallet histories), not more
  DexScreener discovery.
- Added `tests/test_real_data.py` (23 tests, mocked HTTP only,
  no network). 389 tests total.

## v2.8 — 2026-11-13

- Added `src/ml_model.py`: a machine-learning rug-risk model —
  logistic regression implemented from scratch, stdlib only (no
  sklearn, no numpy). A fixed 10-feature vector per mint
  (collapse %, dev sell %, LP removed, time-to-collapse, wash
  score, top-1 / top-10 holder share, holder count, cluster
  size, liquidity), built from mint events + the v2.5 advanced
  detectors. Missing features are flagged as missing, never
  silently zero-filled: the model imputes the training mean, so
  a missing feature contributes exactly 0 to the log-odds.
  `predict_risk` returns a probability + band (low / medium /
  high) + the top contributing features, so a human can see WHY
  the score is what it is — no black box. Trains on
  OutcomeTracker JSON records (verdict + later outcome) with a
  deterministic 80/20 held-out split and accuracy / precision /
  recall reporting; under 10 labelled records (or under 2 per
  class) it reports "insufficient training data" instead of
  pretending the model is good. A small synthetic labelled
  dataset ships ONLY for tests/demo, clearly labelled synthetic.
  Model persistence to a caller-given JSON file (weights +
  feature stats); never writes to the registry. CLI
  `python -m src.ml_model --train <outcomes.json> --model
  <out.json> | --demo | --predict <mint.json> | --evaluate`,
  exit 0 = ok, 2 = invalid args / insufficient data / bad file.
  Held-out on synthetic demo data: accuracy 1.00, precision
  1.00, recall 1.00 (16 held out; 1.00 on a fresh 80-record
  synthetic set too) — synthetic data is clearly separable, so
  this is a pipeline check, not real-world performance; real
  OutcomeTracker data is still too small to train on.
- Minimal integration: `DevHistory` gains an optional advisory
  ML score (`--ml-model <model.json>` on the dev-history CLI,
  or `ml_model=` on the builder) — per-mint probabilities +
  max risk, shown next to the rule verdict. It NEVER changes
  any rule-based verdict: >= 2 rugged mints + evidence stays
  the only flagging path.
- Added `tests/test_ml_model.py` (31 tests, synthetic data
  only, no network). 366 tests total.

## v2.7 — 2026-11-13

- Added `src/dev_history.py`: dev history — paste any dev wallet,
  get their full mint history and verdict. Stdlib + httpx only,
  no wallet, no funds. DevHistoryBuilder collects every mint a
  dev created (Helius signatures -> parse -> TokenCreation),
  labels each mint with `src.labeller` from DexScreener data,
  attaches Solscan / DexScreener / pump.fun / Helius evidence
  links per mint, and applies the registry rule: >= 2 rugged
  mints, each with evidence = flagged; 1 = under review; 0
  rugged with real data = clean. Rugged mints already in the
  registry count toward the >= 2 rule, deduped by mint. Missing
  market data or a failed outcome fetch means that mint is
  `insufficient_data` — never a verdict, never invented numbers;
  a dev with no usable data at all is `insufficient_data`, not
  "clean". `DevHistory.draft_entry()` produces a draft registry
  entry (validated shape) only when the flagged bar is met, and
  never writes the registry. Reports render as plain text or
  JSON. CLI `python -m src.dev_history --wallet <address>
  [--limit N] [--json]`, exit 1 = flagged, 0 = otherwise,
  2 = invalid args / invalid address / no key / network error.
  Without HELIUS_API_KEY it exits 2 and changes nothing.
- Added `tests/test_dev_history.py` (30 tests, synthetic data
  only, no network). 335 tests total.

## v2.6 — 2026-11-13

- Fixed three bugs found in a full code review (302 tests green,
  no crashes, no secrets — all three were non-crashing):
  `src/agents.py` — the daily alert said "(scan did not run — no
  Helius key)" and "0 rugged mints today" even on days the scan
  ran and flagged devs; those lines are now conditional on the
  scan actually being blocked, and the count is real.
  `src/labeller.py` — a slow bleed with a large dev sell was
  labelled "failed_but_honest"; it now falls through to
  "inconclusive", matching the documented rule (honest failure
  needs no LP removal and dev sold < 20%).
  `src/registry.py` — a failed merge mutated the existing entry
  before validation; merges are now validated on a copy first,
  so a rejected merge leaves the entry untouched.
- Added regression tests for all three fixes. 305 tests total.

## v2.5 — 2026-11-13

- Added `src/advanced.py`: advanced detection, stdlib + httpx
  only, no wallet, no funds. Built from a GitHub survey of
  other developers' rug-check code: the best detection lives
  in closed services, open repos mostly wrap RugCheck / GMGN /
  Helius, same-block bundler checks are evaded by pre-funding
  + delayed buys, and nobody open-sources the four things
  built here. StealthClusterDetector (pre-funded wallets,
  delayed buys, shared funding sources across 2+ hops, from
  caller-supplied funding data only — never invents a funder),
  ExitSimulator (true exit at your size: constant-product
  price impact + slippage from real liquidity, "unreliable"
  when liquidity is missing — never invents liquidity),
  WashScore (HHI-style concentration, top-1 / top-10 share,
  volume-vs-holders anomaly; "insufficient data" when holder
  data is missing), OutcomeTracker (record a verdict + later
  outcome to a caller-given JSON file, never the registry;
  precision / recall / accuracy over time). CLI
  `python -m src.advanced --exit / --wash / --cluster /
  --track`, exit 0 = ok, 2 = invalid args.
- Added `tests/test_advanced.py` (31 tests, synthetic data
  only, no network). 302 tests total.

## v2.4 — 2026-11-13

- Added `src/kol_audit.py`: KOL / fake-PnL auditor, stdlib +
  httpx only, no wallet, no funds. PnLAuditor compares a
  caller's claimed PnL (profit, win rate, trades) against
  verified on-chain data from an injected client; numbers
  that cannot be verified are marked "unverified", never
  invented. Verdicts: consistent (within 10%), exaggerated
  (claimed > 2x verified, or claimed profit on a verified
  loss), unverifiable (no data — never accused on no data).
  Markdown audit report with claimed-vs-verified table and
  Solscan / DexScreener links. CLI
  `python -m src.kol_audit --wallet <wallet> --claimed-profit <usd> --claimed-winrate <pct>`,
  exit 0 = consistent / unverifiable, 1 = exaggerated,
  2 = invalid args.
- Added `tests/test_kol_audit.py` (25 tests, mocked HTTP only,
  synthetic data only). 271 tests total.

## v2.3 — 2026-11-13

- Added `src/watchtower.py`: dev watchtower, stdlib-only, no
  wallet, no funds. Watchlist add / remove / list (in-memory
  + JSON file at a caller-given path, never the registry),
  base58 validation. Watchtower alerts only on NEW mints from
  watched devs — a seen mint never alerts twice. Levels:
  urgent (flagged dev launches again), warning
  (under-review dev), info (new mint, not flagged). A price
  drop alone never alerts. CLI
  `python -m src.watchtower --watch <wallet> --check`,
  exit 0 = ok / no urgent alert, 1 = urgent alert found,
  2 = invalid args.
- Added `tests/test_watchtower.py` (22 tests, mocked HTTP only,
  synthetic data only). 271 tests total (with v2.2 + v2.4).

## v2.2 — 2026-11-13

- Added `src/evidence.py`: Muse-run evidence gathering, stdlib
  + httpx only, no wallet, no funds moved, NO community
  dependency — Muse gathers all evidence itself. EvidenceCollector
  collects DexScreener pair data (price, market cap, liquidity,
  24h volume/change), Solscan account data via its public API
  where available (otherwise links only), pump.fun coin/profile
  links, and Helius tx links when a signature is given; merges
  the dev's registry rugged mints (evidence links + collapse
  numbers); numbers not found are None ("n/a"), never invented;
  completeness score = links found / links expected (a link
  counts only when its source confirmed the resource).
  EvidenceVerifier verifies only with >= 2 rugged mints, each
  with at least one evidence link AND a collapse number — links
  alone never verify, a price drop alone never flags.
  EvidenceReport writes a markdown report to a caller-given
  path (default `data/evidence/<wallet>.md`), never to the
  registry, with a "Verified by Muse" UTC timestamp line
  (caller-supplied date or today's real UTC date). Demo CLI
  `python -m src.evidence --dev <wallet> [--mint ...]
  [--tx <mint>:<signature> ...] [--out <path>] [--json]`;
  exit 1 = verified/flagged, 0 = otherwise, 2 = invalid args.
- Added `tests/test_evidence.py` (28 tests, mocked HTTP only,
  no network, synthetic data only: collector links/numbers/
  missing-as-n/a, Solscan balance + failure, registry merge,
  tx links, completeness, verifier verified/insufficient,
  links-alone never verifies, report render/write/refusal,
  gather-and-verify end-to-end, CLI exit codes + JSON).
  271 tests total.
- README: new "Muse-run evidence (v2.2)" section.

## v2.1 — 2026-11-13

- Added `src/live_scan.py`: live Helius scan, stdlib + httpx
  only, no wallet, no funds moved. HeliusClient reads
  `HELIUS_API_KEY` only from the environment (never hardcoded,
  never logged, never committed) and wraps the existing Helius
  indexer (pump.fun signatures, transaction details, mint +
  dev parsing, clear "HELIUS_API_KEY is not set — scan blocked"
  error without a key). LiveScanner runs a bounded scan (N
  recent creations, DexScreener outcome + labeller per mint)
  and flags a dev only with >= 2 rugged mints, each with
  evidence links (scan + registry, deduped by mint); 1 rug =
  under review; a price drop alone never flags. EvidenceGatherer
  is Muse-run (no community): DexScreener / Solscan / pump.fun /
  Helius links + summary from real data only, "insufficient
  evidence" when data is missing. Demo CLI
  `python -m src.live_scan --limit 10 [--dry-run] [--json]`;
  exit 0 = ok, 2 = blocked / invalid args; --dry-run makes no
  Helius call.
- Added `tests/test_live_scan.py` (22 tests, mocked HTTP only,
  no network, no real key: missing-key blocked, client
  headers/URL, tx parsing mint + dev, RPC error, flagged /
  under_review / not_flagged, registry merge + dedupe,
  price-drop-alone never flags, evidence links, dry-run no
  network, CLI exit codes). 196 tests total.
- README: new "Live scan (v2.1)" section — how to set
  `HELIUS_API_KEY` locally in .env and as a GitHub secret, and
  that the key is never committed.

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
