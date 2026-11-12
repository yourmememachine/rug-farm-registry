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
src/api.py              FastAPI app
CHANGELOG.md            v1.0 / v1.1 release notes
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
