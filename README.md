# Open Rug-Farm / Serial-Rugger Registry

A public, git-versioned dataset and free API listing dev wallets and
funding-linked wallet clusters with a proven history of rugged
launches — each entry linked to on-chain evidence.

> **SAMPLE DATA ONLY.** Every wallet, mint and cluster shipped in
> `data/registry.json` is synthetic / invented for testing. **No real
> wallet is accused of anything.** Real entries are added only via a
> reviewed pull request with evidence (see `CONTRIBUTING.md`).

## Why this is untapped

The single most predictive signal for a meme-coin rug is the dev's
history — but that data is kept private. Scorp Trader keeps its
`dev_history` table private (~2,100 wallets flagged, 87% of the
tokens it skipped were repeat offenders), RugCheck exposes no
dev-history database, and GORZ found 65 rug-farm groups across
27,759 coins with no open dataset. Existing tools are point-in-time
scanners: they check *this* token, not *who launched it before*.

This project turns that private moat into open infrastructure:
every scanner, bot and wallet can query one shared list instead of
each rebuilding a private one — and a public, evidence-linked
registry is auditable and appealable in a way a closed score is not.

Scale (SolRugDetector, H1 2025): 100,063 Solana tokens screened,
76,469 flagged as rug candidates, ≥ $151m direct losses, median
~35 minutes from creation to first profit-taking. 60,402 were
pump-and-dumps vs only 461 freeze-authority abuses — so authority
checks alone miss the main attack.

Sources: see the research report that motivated this project
(`research_notes/untapped-crypto-ideas-20261112-1415/report.md`
in the original workspace) and the links in `CONTRIBUTING.md`.

## What it does

- **Registry** (`data/registry.json`): git-versioned entries — wallet,
  cluster, rugged mints (≥ 2 required to flag), evidence links per
  mint (mint, outcome, peak vs current market cap, LP removal /
  dev sell), first/last seen, TTL, status, dispute notes.
- **Labeller** (`src/labeller.py`): pure functions that label a mint
  `rugged` / `failed_but_honest` / `inconclusive` from collapse,
  LP removal and dev-sell data.
- **Clusterer** (`src/cluster.py`): funding-graph clustering, 1–2 hops
  from funding transactions.
- **Indexer** (`src/indexer.py`): real Helius wiring —
  `getSignaturesForAddress` + parsed `getTransaction` on the
  pump.fun program, DexScreener outcomes. Disabled without a key.
- **Daily scan** (`src/daily_scan.py` + `.github/workflows/daily-scan.yml`):
  daily 09:00 UTC scan, writes `data/alerts/YYYY-MM-DD.md`, commits
  only verified ≥ 2-rug wallets.
- **API** (`src/api.py`): FastAPI, JSON only — see below.
- **Pump.fun watch** (`src/pump_watch.py`): no-key watchlist of
  the latest boosted Solana tokens from DexScreener's public API
  — market cap, liquidity, 24h volume/change, watch label only
  (`collapsed` / `thin_liquidity` / `active`, never "rugged").
- **Single-file SDK** (`src/rugfarm.py`): stdlib-only client
  library developers can copy into their own projects — see
  [Clone & use in your project](#clone--use-in-your-project).

No custody, no trading, no private keys. Read-only registry + API.

## False-positive controls

- **≥ 2 rugged mints to flag.** One rug = `under_review`, never flagged.
- **Evidence required per mint.** An entry with missing evidence is
  invalid and cannot be flagged.
- **TTL expiry.** Entries expire `ttl_days` after `last_seen`
  (default 180 days); expired entries are reported as `expired`,
  not flagged.
- **Honest failures are labelled, not flagged.** A token that died
  without LP removal or a large dev sell is `failed_but_honest`.
- **Disputes / appeals.** See `CONTRIBUTING.md` — any wallet can
  dispute an entry by PR; disputed entries are marked `disputed`
  while under review.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the tests
pytest -q

# Run the API
uvicorn src.api:app --reload --port 8000
```

## Query the API

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","version":1,"sample_data":true,"entries":4,"flagged":2}

curl http://127.0.0.1:8000/registry

curl http://127.0.0.1:8000/wallets/SAMPLEDEV11111111111111111111111111111111

# Pre-trade check: is this dev a known serial rugger?
curl "http://127.0.0.1:8000/check?dev=SAMPLEDEV11111111111111111111111111111111"
# {"dev_wallet":"...","flagged":true,"risk_level":"high",
#  "rugged_mint_count":3,"cluster_id":"cluster-sample-a1",
#  "reasons":[...],"evidence":[...]}

# Unknown dev — no record, not a clean bill of health
curl "http://127.0.0.1:8000/check?dev=Unknown111111111111111111111111111111"
# {"flagged":false,"risk_level":"unknown",...}

curl http://127.0.0.1:8000/clusters/cluster-sample-a1
```

Risk levels: `high` (flagged, ≥ 2 rugs), `watch` (under review,
1 rug), `expired` (entry past TTL), `unknown` (no record).

## Pre-trade check (CLI)

No API server needed — check a dev wallet straight from the terminal:

```bash
python -m src.check_cli SAMPLEDEV11111111111111111111111111111111
# Risk level: high / Flagged: YES / Rugged mint count: 3
# ... reasons and evidence links ...

python -m src.check_cli SAMPLEDEV11111111111111111111111111111111 \
    --mint SAMPLEMNT1111111111111111111111111111111

python -m src.check_cli UnknownDev1111111111111111111111111
# Risk level: unknown — no record, not a clean bill of health
```

Exit codes: `1` flagged, `0` unknown / watch / expired (not
currently flagged), `2` invalid address or mint. Same rules as
`GET /check` — the CLI reuses `src/registry.check_wallet`, so the
flagging logic lives in exactly one place.

## Pump.fun watch (no key)

No Helius key needed — watch the latest boosted Solana tokens
straight from DexScreener's public API:

```bash
python -m src.pump_watch
# LEAD (mint...) — market cap, liquidity, 24h volume, 24h change
#   watch: active

python -m src.pump_watch --limit 5 --json
```

Watch labels: `collapsed` (24h change <= -70%),
`thin_liquidity` (liquidity < $1,000), `active` otherwise.
These are **watch labels, not rug verdicts** — a collapsed or
thin token is not proven rugged. Calling a token rugged needs
a dev wallet plus LP-removal / dev-sell evidence (see
`src/labeller.py`), which this tool does not have, so it never
says "rugged". No holder data is shown: DexScreener's public
API returns none, and we do not invent any.

## AI agents

Four small, deterministic agents in `src/agents.py` that work
together as a pipeline — Scanner → Evidence → Verdict → Alert.
Stdlib only, no new dependency, no invented data:

- **ScannerAgent** — live DexScreener data (via `src.pump_watch`)
  for a list of mints: market cap, liquidity, 24h volume/change
  and a watch label (`collapsed` / `thin_liquidity` / `active`).
  Holder counts are never invented, and no token is ever
  labelled "rugged".
- **EvidenceAgent** — for a dev wallet + mint, collects the
  evidence links (DexScreener, Solscan, pump.fun) plus the
  registry check, and says "insufficient evidence" when the
  data is missing instead of guessing.
- **VerdictAgent** — applies the registry rule only: flagged
  only with ≥ 2 rugged mints, each with evidence, unexpired;
  1 rug = under review; anything else = not flagged / unknown.
  A price drop alone never flags.
- **AlertAgent** — formats the daily-alert markdown from a
  list of verdicts, and is honest when the scan is blocked
  (no Helius key = 0 checked, no invented wallets).

```bash
python -m src.agents --mint <mint> [--dev <wallet>]
python -m src.agents --watch --limit 5
# Exit 1 = flagged, 0 = otherwise, 2 = invalid address/args
```

## Creativity agents

Four more small, deterministic agents in `src/creativity.py`
that present rug-check findings in original, human-readable
ways. Stdlib only, no new dependency, no invented data — they
only re-present numbers and verdicts you already have:

- **NameAgent** — 3 original nickname ideas for a watchlist
  entry, deterministic from name/ticker/idea + seed, always
  distinct from the input name, with a profanity blocklist.
- **StoryAgent** — a 2–3 sentence plain-English story of a
  verdict: what happened, what the verdict means, what to
  watch. Never claims a rug without a flagged verdict — any
  other verdict gets an honest "not proven a rug" line.
- **VisualAgent** — a coin-card badge spec (label, colour
  name, emoji, one-line summary: collapsed = red, thin =
  amber, active = green) plus an ASCII sparkline drawn from
  your own price-change numbers.
- **PostAgent** — a ready-to-post X draft, always ≤ 280
  chars, always with `#memes #rug #pumpfun` and the repo link,
  with a variant for flagged / watch-only / active. It never
  invents a price or a mint.

```bash
python -m src.creativity --verdict flagged --symbol XYZ --mc 1000000 --change -80
python -m src.creativity --verdict active --symbol GATO --mc 5009361 --change -16.7 --post
# Modes: --story / --post / --badge / --names (default: all). Exit 0 = ok, 2 = invalid args.
```

## Web3 future

A future-facing module in `src/web3_future.py` — think ahead,
build with web3. Stdlib only, no new dependency, no wallet, no
SOL spent, no private key, no invented data:

- **FutureChain** — chain-agnostic registry design: Solana is
  live today, Base is a planned second chain (explorer URLs
  ready, adapter not wired up). Verdicts never depend on the
  chain, so a new chain can be added without rewriting them.
- **OnChainProbe** — probes a dev wallet with the public
  Solana RPC (no key): SOL balance, account owner, and whether
  it is a token mint or a system wallet. On RPC failure it
  says "unavailable" instead of inventing a number.
- **FutureWatch** — forward-looking risk signals from real
  DexScreener data only: thin liquidity (< $1,000), collapsed
  (≤ -70% in 24h), no liquidity reported. Labels are only
  watch / ok — never "rugged".
- **RoadmapAgent** — the next 3 versions as markdown (v2.0
  multi-chain, v2.1 Helius live scan, v2.2 Muse-run evidence
  gathering). Dates are always TBD — directions, not promises.

```bash
python -m src.web3_future --wallet <address> [--chain solana]
python -m src.web3_future --roadmap
python -m src.web3_future --mc 2903 --liquidity 2752 --change -91.5
# Exit 0 = ok, 2 = invalid args / invalid address / unsupported chain
```

## Multi-chain (v2.0)

The registry is now multi-chain in `src/multichain.py` — the
first item from the web3 roadmap. Stdlib only, no new
dependency, no wallet, no funds moved, no private key:

- **ChainAdapter** — one adapter per chain, one shared return
  shape (chain, address, balance, kind, owner, contract flag,
  explorer + DexScreener links, available / error). Same keys
  on Solana and Base, so callers never branch on the chain.
- **SolanaAdapter** — public Solana RPC probe (reuses
  web3_future), Solscan links, DexScreener Solana.
- **BaseAdapter** — public Base RPC (https://mainnet.base.org,
  no key): `eth_getBalance` (wei → ETH) and `eth_getCode`
  for contract detection. Basescan links, DexScreener Base.
- **MultiChainRegistry** — routes a check to the right
  adapter and adds the registry verdict. Verdicts are
  chain-independent: flagged only on ≥ 2 rugged mints with
  evidence, on any chain. The registry is still Solana sample
  data only, so a Base check honestly reports its registry
  verdict as unknown ("no Base registry coverage yet") —
  the on-chain probe still works.
- **ChainCompare** — the same token data on two chains, side
  by side: liquidity, market cap, 24h change, watch / ok per
  chain. Never "rugged" — a price drop alone is not a verdict.

Future evidence work is done by Muse itself — automated
research plus Helius / DexScreener evidence gathering —
not by the community, and never by submissions. No
community dependency anywhere in the roadmap.

```bash
python -m src.multichain --chain solana --address <address>
python -m src.multichain --chain base --address 0x...
python -m src.multichain --compare --chain-a solana --chain-b base \
  --mc-a 5000000 --liquidity-a 237713 --change-a -16.7 \
  --mc-b 120000 --liquidity-b 800 --change-b -91.5
python -m src.multichain --list-chains
# Exit 0 = ok, 2 = invalid args / chain / address
```

## Live scan (v2.1)

`src/live_scan.py` runs a bounded live scan of recent pump.fun
creations — the second item from the web3 roadmap. Stdlib +
httpx only, no new dependency, no wallet, no funds moved:

- **HeliusClient** — reads `HELIUS_API_KEY` only from the
  environment (never hardcoded, never logged, never committed).
  Wraps the existing Helius indexer: pump.fun program
  signatures, transaction details, mint + dev parsing.
- **LiveScanner** — fetches N recent creations, gets each mint's
  DexScreener outcome, labels it with `src/labeller`. A dev is
  flagged **only** with ≥ 2 rugged mints, each with evidence
  links (scan findings + registry record, deduped by mint).
  One rugged mint = under review. A price drop alone never
  flags — a mint that merely collapsed with no LP removal /
  dev sell is `failed_but_honest`, and a mint with no pair is
  `inconclusive`.
- **EvidenceGatherer** — Muse-run evidence (no community):
  builds DexScreener / Solscan / pump.fun / Helius links and a
  short summary from real data only. Missing data =
  "insufficient evidence", never a silent pass.

```bash
python -m src.live_scan --limit 10 [--dry-run] [--json]
# --dry-run prints the plan and makes NO Helius call.
# Exit 0 = ok, 2 = blocked (no key) or invalid args.
```

### Setting up the key

Get a free key at https://www.helius.dev (free tier is enough).

Locally:
```bash
cp .env.example .env   # then set HELIUS_API_KEY=<your key> in .env
```

On GitHub (so the daily 09:00 UTC workflow can run real scans):
**Settings → Secrets and variables → Actions → New repository
secret**, name `HELIUS_API_KEY`, value = your key.

The key is never committed — `.env` is git-ignored and the
workflow reads the secret from the environment. Without a key
the scan exits 2 and reports "blocked"; it never invents data.

## Clone & use in your project

Developers can clone the repo and use the registry from their own
bot, scanner or wallet — no API server needed:

```bash
git clone https://github.com/yourmememachine/rug-farm-registry
cd rug-farm-registry
pip install -r requirements.txt   # full project (API, indexer, tests)
# ...or copy just src/rugfarm.py into your project: it is
# stdlib-only and needs no install at all.
```

### Check a dev wallet

```python
from src.rugfarm import RugFarm

rf = RugFarm()  # loads data/registry.json from the repo
result = rf.check_dev("SAMPLEDEV11111111111111111111111111111111")
# verdict: "flagged", rugged_mint_count: 3, exit_code: 1
# rugged_mints: [{mint, symbol, collapse_pct, evidence: {solscan_mint, ...}}, ...]

if rf.is_flagged_dev(dev_wallet):
    skip_trade()  # >= 2 rugged mints with evidence, unexpired
```

### Check a coin mint

```python
result = rf.check_mint("SAMPLEMNT1111111111111111111111111111111")
# verdict: "rugged", symbol: "SAMPLE1",
# dev_wallet: "SAMPLEDEV11111111111111111111111111111111",
# collapse_pct: 98.1, evidence: {...}

if rf.is_rugged_mint(mint):
    skip_trade()  # registry-only, no network call
```

A mint not in the registry is checked live against DexScreener
(no key) and returns `verdict: "not_in_registry"` with a watch
label (`collapsed` / `thin_liquidity` / `active`) and market data —
never a "rugged" verdict without registry evidence. If DexScreener
is unreachable, market fields come back `None` instead of
invented numbers.

### Live watchlist

```python
for entry in rf.watch(limit=5):
    print(entry["symbol"], entry["market_cap_usd"], entry["watch_label"])
# watch labels only — a collapsed token is not proven rugged.
```

Or from the terminal:

```bash
python -m src.rugfarm <wallet-or-mint>          # exit 1 = flagged / rugged
python examples/use_rugfarm.py                  # copy-paste demo
```

### Rules (enforced in the SDK, same as the rest of the repo)

- **Flag only with >= 2 rugged mints + evidence per mint.**
  One rug = `under_review`, never flagged; expired entries are
  `not_flagged`.
- **Never invent a wallet, a mint, a holder count or an outcome.**
  Unknown addresses return `unknown` — not a clean bill of health.
- **Sample data is clearly marked.** Every result carries
  `sample_data: true` while the shipped sample registry is loaded;
  sample wallets/mints are synthetic and are NOT accusations.
- **No custody, no trading, no private keys.** Read-only checks.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Status, entry / flagged counts |
| GET | `/registry` | Full registry JSON |
| GET | `/wallets/{address}` | One entry (+ `currently_flagged`) |
| GET | `/check?dev=…&mint=…` | Pre-trade check with reasons + evidence |
| GET | `/clusters/{cluster_id}` | All entries in a cluster |

## Indexer (real Helius wiring)

`src/indexer.py` implements `HeliusIndexer`:

- `getSignaturesForAddress` on the pump.fun program
  (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`) lists recent
  creation transactions, then `getTransaction` (`jsonParsed`) per
  signature is parsed for mint + dev wallet (pump.fun `create`
  instruction, fee payer = dev; falls back to new entries in
  `postTokenBalances`).
- `fetch_mint_outcome()` gets outcome evidence from DexScreener's
  public API (no key): current market cap, 24h price change,
  liquidity. Peak market cap is *estimated* from the 24h change,
  LP removal is inferred from liquidity < $1,000, and
  `dev_sold_pct` is 0.0 unless proven otherwise — DexScreener does
  not prove a dev sell, and we do not invent one. A mint with no
  DexScreener pair is `inconclusive`, never flagged.
- `fetch_funding_transfers()` parses native SOL transfers in a
  wallet's recent history, for the funding-graph clusterer.

It is **disabled without `HELIUS_API_KEY`** — `fetch_new_mints()`
raises a clear error instead of faking data. All HTTP goes through
an injectable `httpx.Client`, so tests use `httpx.MockTransport`
and never make a live call.

```bash
cp .env.example .env   # then set HELIUS_API_KEY=...
```

## Daily scan — new alerts every day

`src/daily_scan.py` runs the full pipeline and writes
`data/alerts/YYYY-MM-DD.md`:

1. Fetch recent pump.fun creations via the Helius indexer.
2. Label each mint's outcome via `src/labeller.py`
   (DexScreener evidence).
3. Cluster funding-linked dev wallets via `src/cluster.py`.
4. Add / promote a wallet **only** with ≥ 2 rugged mints, each
   with evidence links. One rug = under review, never flagged.
5. Write the day's alert file — "no new verified alerts" if
   nothing met the rule. Never invent a wallet.

```bash
# Local run (needs HELIUS_API_KEY in the environment / .env)
python -m src.daily_scan --limit 50
python -m src.daily_scan --dry-run   # scan, print, change nothing

# Without a key it exits 2 and changes nothing:
#   ERROR: HELIUS_API_KEY is not set.
```

### Setting up the daily GitHub Action

`.github/workflows/daily-scan.yml` runs daily at **09:00 UTC**,
plus on demand (`workflow_dispatch`):

1. Get a free Helius key at https://www.helius.dev.
2. In the GitHub repo: **Settings → Secrets and variables →
   Actions → New repository secret**, name `HELIUS_API_KEY`,
   value = your key. Never commit a key — `.env` is git-ignored
   and the workflow reads the secret from the environment.
3. The workflow installs deps, runs `pytest -q`, runs
   `python -m src.daily_scan`, and **only commits when
   `data/registry.json` or `data/alerts/` actually changed**,
   with message `daily alerts: YYYY-MM-DD`.
4. If the secret is missing, the scan exits 2 and the workflow
   fails loudly — it never commits faked data.

### Rules for daily alerts (enforced in code + tests)

- Never commit an unverified / single-rug wallet.
- Never commit sample data as real — adding the first real entry
  flips `sample_data` to `false` in `data/registry.json`.
- Never invent a wallet, mint, or outcome. Inconclusive or
  honest-failure mints are never counted as rugs.
- Disputed entries stay `disputed` and are never re-flagged by
  the scan (the scan only adds/promotes via `add_or_update_entry`,
  which validates every entry).

## Project layout

```
data/registry.json      git-versioned registry (sample data)
data/alerts/            daily alert files, one per day (YYYY-MM-DD.md)
src/registry.py         load / validate / flag (>=2 rule, TTL, evidence)
src/labeller.py         rugged / failed_but_honest / inconclusive
src/cluster.py          funding-graph clustering, 1-2 hops
src/indexer.py          Helius pump.fun indexer + DexScreener outcomes
src/daily_scan.py       daily scan CLI: fetch -> label -> cluster -> alert
src/check_cli.py        pre-trade check CLI (exit 1 = flagged)
src/pump_watch.py       pump.fun watchlist, no key (collapsed / thin / active)
src/rugfarm.py          single-file stdlib-only SDK for developers (clone-ready)
examples/use_rugfarm.py copy-paste SDK demo
src/api.py              FastAPI app
CHANGELOG.md            v1.0 / v1.1 / v1.2 / v1.3 release notes
.github/workflows/daily-scan.yml   daily 09:00 UTC scan + commit
tests/                  pytest: labeller, cluster, registry, API, indexer, daily scan
```

## Caveats

- "No open-source implementation found" in the original research
  means none found in those web searches — not proof of
  non-existence. Do a direct GitHub code search before claiming
  novelty.
- Closed competitors' stats (Scorp, GORZ, SolRugDetector) are
  directional, from their own pages / write-ups, not independently
  audited.
- This is a risk signal, not a verdict. Always verify the evidence
  links yourself before acting on a flag.

## License

MIT — see `LICENSE`.
