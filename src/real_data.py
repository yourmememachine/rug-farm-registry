"""Real-world validation data for the ML rug-risk model — v2.9.

The v2.8 model is honest but unproven: its 1.00 held-out scores
are on synthetic demo data only, because real OutcomeTracker data
is still too small to train on. This module fetches REAL Solana
meme coins from DexScreener's public API (no key) and builds a
labelled dataset from real observable market facts, so the model
can be trained and evaluated on the real world instead of on
synthetic data.

Labelling rule "real-v2" (v2.13) — the v2.9 "real-v1" rule,
loosened where v2.11's verified evidence proved it too strict.
v1 was fixed before any fetch and never tuned after seeing the
metrics; v2 changes ONLY the thresholds the verified numbers
demand, and every change is justified below. A token is labelled
from real observable market facts only:

- "rugged" (rugged-like) if EITHER branch holds:
    A. Fresh collapse: 24h change <= -90% AND liquidity < $1,000
       AND 24h volume < $1,000 AND pair age >= 1 day.
    B. Long-dead: liquidity < $2,500 AND 24h volume < $500 AND
       market cap < $5,000 AND pair age >= 5 days.
- "survived" if ALL hold: liquidity >= $10,000 AND 24h volume
  >= $5,000 AND 24h change > -30% AND pair age >= 7 days.
- Otherwise "excluded" — the ambiguous middle is excluded, not
  forced into either class. A missing value never satisfies a
  threshold (missing data means the condition is not met).

v1 -> v2, threshold by threshold:
- Branch A collapse -70% -> -90%: TIGHTENED, not loosened.
  v1's -70% proxy was looser than the registry's own >= 90%
  collapse standard (labeller.py COLLAPSE_RUG_PCT = 90.0), and
  a -70% day is ordinary meme-coin volatility, not a collapse.
  v2 aligns the observable proxy with the 90% bar. The
  loosening in v2 is in liquidity and age, never in collapse.
- Branch B liquidity $100 -> $2,500: the 4 verified small
  rugs (v2.11) hold $1,606-$1,907 residual liquidity — real
  rugged pools almost never drain below $100, so the v1 branch
  almost never fired. $2,500 covers the observed range with
  ~30% headroom and is still far below the survived floor
  ($10,000).
- Branch B volume $100 -> $500: "ser" had $413.56 24h volume,
  so v1's $100 cap excluded a verified dead rug. $500 covers
  the observed max ($413.56) with headroom while still meaning
  "nobody is trading this".
- Branch B age 7 days -> 5 days: the 4 small rugs are 5.67-5.94
  days old, so v2 catches them NOW (2026-11-13) instead of
  ~Nov 19-20. A 5-day floor still requires the token to have
  been dead for days, not hours — branch A (1-day floor) is the
  one that handles fresh collapses.
- Branch B market cap < $5,000: UNCHANGED. All 4 small rugs
  are $1,520-$1,797, well under; no evidence to move it.
- Branch A liquidity / volume / age: UNCHANGED. No evidence
  to move them.

What v2 still does NOT catch, stated plainly: the famous
three (LIBRA, HAWK, CAR) still hold real liquidity ($4.4k-
$201k) and market cap ($870k-$2.9M), so no liquidity/market-cap
branch catches them — they were rugged by insider selling, not
by draining the pool to zero, and catching them needs
dev-sell / LP-removal evidence that DexScreener does not
provide. The rule is NOT forced to catch them. Never label a
mint rugged on liquidity alone: branch B also requires a tiny
market cap, near-zero volume AND age — a live token with a
small pool but real trading is excluded, not rugged.

What "rugged-like" means here — read this before quoting the
metrics: these labels are OBSERVABLE-MARKET-FACT labels, not
verified rug verdicts. DexScreener does not provide dev-wallet,
LP-removal or dev-sell evidence, so no token in this dataset is
proven rugged the way the registry proves it (>= 2 rugged mints
+ evidence). The held-out metrics therefore measure whether the
model can reproduce the observable labelling rule on unseen real
tokens — a real-world pipeline validation, not a proof that the
model detects actual rugs.

Two further honest limits, stated up front:
- Feature coverage: DexScreener provides market cap, liquidity,
  24h volume, 24h change, txns and pair age. It does NOT provide
  dev sell %, LP removal, time-to-collapse, wash score, holder
  shares, holder count or stealth-cluster size — those 8 of the
  model's 10 features are missing on real data and are flagged
  as missing, never invented or zero-filled as if real. The
  model imputes them with the training mean (contributing 0 to
  the log-odds), so real-world scoring effectively runs on the
  2 features DexScreener provides.
- Collapse proxy: DexScreener gives 24h price change, not
  peak-to-current collapse. The model's ``collapse_pct`` feature
  is therefore the 24h fall (``max(0, -change_24h)``), a proxy
  for the peak-to-current collapse the labeller uses. The proxy
  is documented on every record.
- Circularity: the label is defined by collapse + liquidity +
  volume + age, and the model sees collapse + liquidity. The
  label also depends on volume and age, which the model does NOT
  see, so the evaluation is not purely circular — but the two
  visible features do most of the separating, and the metrics
  must be read with that in mind.

Endpoints (DexScreener public API, no key, ~300 req/min — this
module stays far under it by batching 30 mints per call and
sleeping briefly between calls):
- GET https://api.dexscreener.com/token-profiles/latest/v1
- GET https://api.dexscreener.com/token-boosts/latest/v1
- GET https://api.dexscreener.com/latest/dex/search?q=<term>
- GET https://api.dexscreener.com/tokens/v1/solana/<mints>

Second source (v2.10) — pump.fun launches, because DexScreener
discovery only surfaces tokens alive enough to be listed:
- GET https://frontend-api.pump.fun/coins?offset=&limit=
  &sort=created_timestamp&order=DESC&includeNsfw=false
  Pages at increasing offsets give a spread of ages (recent
  launches first, older launches further back); most pump.fun
  coins die within hours, which is exactly the rugged-like
  population DexScreener hides. Each launch is enriched via the
  DexScreener /tokens/ lookup (batched, 30 per call) and labelled
  with the SAME fixed "real-v1" rule — the source only changes
  which mints are sampled, never the labelling. A launch with no
  DexScreener pair at all yields no record (no market facts to
  label from — never invented). Rate limits: 0.5 s between
  pump.fun pages, up to 3 retries with backoff on HTTP 429.

Third source (v2.11) — documented rugged mints, because
discovery feeds never surface dead rugs: a caller-supplied JSON
list of {"mint", "source"} pairs, where each mint address
appears verbatim in a public source (a news post-mortem, a
RugCheck/Solscan writeup, a public thread, or a public
rule-based rug tracker) and "source" is that source's URL.
Each mint is verified live via the DexScreener /tokens/ lookup
(batched, 30 per call) and labelled with the SAME fixed
"real-v2" rule — the source only changes which mints are
sampled, never the labelling. A mint with no DexScreener pair
yields no record; an address that is not a valid Solana address
is rejected without a fetch. CLI:
python -m src.real_data --verify-documented <list.json>
  [--out data/ml_real_dataset.json]
  (--verify-documented merges into the existing file at --out,
  deduped by mint, and writes a <name>.summary.json sidecar.)

Usage:
    python -m src.real_data --fetch [--limit 150] [--out data/ml_real_dataset.json] [--json]
    python -m src.real_data --fetch-pumpfun [--pumpfun-limit 300] [--out data/ml_real_dataset.json] [--json]
      (--fetch-pumpfun merges into the existing file at --out,
      deduped by mint, and writes a <name>.summary.json sidecar.)
    python -m src.real_data --verify-documented <list.json> [--out data/ml_real_dataset.json] [--json]
      (--verify-documented verifies a caller-supplied list of
      documented rugged mints and merges the verified records
      into the existing file at --out, deduped by mint.)
    python -m src.real_data --relabel [--out data/ml_real_dataset.json] [--json]
      (--relabel re-labels the existing dataset at --out with the
      CURRENT rule (real-v2) from the stored market facts — no
      re-fetch — and rewrites the dataset + summary sidecar.
      Used when the rule itself changes, v2.13.)

Exit 0 = ok, 2 = invalid args / network error / too few usable
tokens (< 10 labelled records — the model needs at least 10 to
train, and the file is still saved so the run is auditable).
The saved file is a JSON list, so the existing ml_model CLI
trains and evaluates on it directly:
    python -m src.ml_model --train data/ml_real_dataset.json --model data/ml_real_model.json

All HTTP goes through an injectable ``httpx.Client`` so tests can
use ``httpx.MockTransport``. No live call is made in tests.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.http_client import make_client
from src.registry import is_valid_solana_address

TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKEN_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana"

# Broadening sources: profiles and boosts are the fresh-token
# lists; the searches add established tokens (the survived class
# needs tokens that have lived a week or more) and the clone-heavy
# terms surface dead clones (the rugged-like class needs tokens
# that actually died). Same fixed rule for every token, whatever
# the source — the sources only change the sample size, never
# the labelling.
SEARCH_QUERIES = (
    "pump", "SOL", "BONK", "PEPE", "DOGE", "SHIB", "TRUMP",
    "WIF", "POPCAT", "MAGA", "ELON", "MOON", "INU", "CAT", "FROG",
)

DEFAULT_LIMIT = 150
DEFAULT_OUT = "data/ml_real_dataset.json"
MAX_MINTS_PER_REQUEST = 30
REQUEST_SLEEP_SECONDS = 0.2

# pump.fun frontend API (public, no key) — v2.10 second source.
PUMPFUN_COINS_URL = "https://frontend-api.pump.fun/coins"
PUMPFUN_PAGE_LIMIT = 50
# Increasing offsets = increasing age: recent launches first,
# older launches further back. Stops early on an empty page.
PUMPFUN_PAGE_OFFSETS = (0, 100, 300, 600, 1000, 2000, 4000)
PUMPFUN_REQUEST_SLEEP_SECONDS = 0.5
PUMPFUN_MAX_429_RETRIES = 3
PUMPFUN_429_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
DEFAULT_PUMPFUN_LIMIT = 300

# Labelling rule "real-v2" — see the module docstring for the
# v1 -> v2 justification of every changed threshold.
LABELLING_RULE_VERSION = "real-v2"
RUGGED_COLLAPSE_PCT = -90.0
RUGGED_LIQUIDITY_USD_MAX = 1_000.0
RUGGED_VOLUME_24H_USD_MAX = 1_000.0
RUGGED_PAIR_AGE_DAYS_MIN = 1.0
DEAD_LIQUIDITY_USD_MAX = 2_500.0
DEAD_VOLUME_24H_USD_MAX = 500.0
DEAD_MARKET_CAP_USD_MAX = 5_000.0
DEAD_PAIR_AGE_DAYS_MIN = 5.0
SURVIVED_LIQUIDITY_USD_MIN = 10_000.0
SURVIVED_VOLUME_24H_USD_MIN = 5_000.0
SURVIVED_CHANGE_24H_PCT_MIN = -30.0
SURVIVED_PAIR_AGE_DAYS_MIN = 7.0

# The ml_model needs at least 10 labelled records to train.
MIN_LABELLED_RECORDS = 10

EXIT_OK = 0
EXIT_INVALID = 2


def labelling_rule() -> dict:
    """The fixed labelling rule, as a structured dict (also saved on every record)."""
    return {
        "version": LABELLING_RULE_VERSION,
        "rugged_branch_a_fresh_collapse": {
            "change_24h_pct_max": RUGGED_COLLAPSE_PCT,
            "liquidity_usd_max": RUGGED_LIQUIDITY_USD_MAX,
            "volume_24h_usd_max": RUGGED_VOLUME_24H_USD_MAX,
            "pair_age_days_min": RUGGED_PAIR_AGE_DAYS_MIN,
        },
        "rugged_branch_b_long_dead": {
            "liquidity_usd_max": DEAD_LIQUIDITY_USD_MAX,
            "volume_24h_usd_max": DEAD_VOLUME_24H_USD_MAX,
            "market_cap_usd_max": DEAD_MARKET_CAP_USD_MAX,
            "pair_age_days_min": DEAD_PAIR_AGE_DAYS_MIN,
        },
        "survived": {
            "liquidity_usd_min": SURVIVED_LIQUIDITY_USD_MIN,
            "volume_24h_usd_min": SURVIVED_VOLUME_24H_USD_MIN,
            "change_24h_pct_min": SURVIVED_CHANGE_24H_PCT_MIN,
            "pair_age_days_min": SURVIVED_PAIR_AGE_DAYS_MIN,
        },
        "note": (
            "Labels are observable-market-fact labels, not verified "
            "rug verdicts. Missing data never satisfies a threshold."
        ),
    }


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _present(value: float | None, test) -> bool:
    """A threshold test that fails when the value is missing."""
    return value is not None and test(value)


def label_token(obs: dict) -> str:
    """Label one token observation: "rugged" | "survived" | "excluded".

    Pure function of the fixed "real-v2" rule (see module
    docstring). ``obs`` holds change_24h_pct, liquidity_usd,
    volume_24h_usd, market_cap_usd and pair_age_days (any may be
    None). Missing data never satisfies a threshold.
    """
    change = obs.get("change_24h_pct")
    liquidity = obs.get("liquidity_usd")
    volume = obs.get("volume_24h_usd")
    market_cap = obs.get("market_cap_usd")
    age = obs.get("pair_age_days")

    # Branch A — fresh collapse.
    if (
        _present(change, lambda c: c <= RUGGED_COLLAPSE_PCT)
        and _present(liquidity, lambda l: l < RUGGED_LIQUIDITY_USD_MAX)
        and _present(volume, lambda v: v < RUGGED_VOLUME_24H_USD_MAX)
        and _present(age, lambda a: a >= RUGGED_PAIR_AGE_DAYS_MIN)
    ):
        return "rugged"
    # Branch B — long-dead.
    if (
        _present(liquidity, lambda l: l < DEAD_LIQUIDITY_USD_MAX)
        and _present(volume, lambda v: v < DEAD_VOLUME_24H_USD_MAX)
        and _present(market_cap, lambda m: m < DEAD_MARKET_CAP_USD_MAX)
        and _present(age, lambda a: a >= DEAD_PAIR_AGE_DAYS_MIN)
    ):
        return "rugged"
    # Survived — established, liquid, trading, no steep collapse.
    if (
        _present(liquidity, lambda l: l >= SURVIVED_LIQUIDITY_USD_MIN)
        and _present(volume, lambda v: v >= SURVIVED_VOLUME_24H_USD_MIN)
        and _present(change, lambda c: c > SURVIVED_CHANGE_24H_PCT_MIN)
        and _present(age, lambda a: a >= SURVIVED_PAIR_AGE_DAYS_MIN)
    ):
        return "survived"
    return "excluded"


def parse_pair(pair: dict) -> dict | None:
    """Parse one DexScreener pair object into a token observation.

    Handles the pair shape (``baseToken``, ``marketCap``,
    ``liquidity``, ``volume``, ``priceChange``, ``txns``,
    ``pairCreatedAt``). Returns None when the object has no mint.
    Liquidity / volume / change are None when DexScreener omits
    them — never guessed.
    """
    if not isinstance(pair, dict):
        return None
    base = pair.get("baseToken") or {}
    mint = pair.get("tokenAddress") or base.get("address")
    if not mint:
        return None
    liquidity = _as_float((pair.get("liquidity") or {}).get("usd"))
    volume = _as_float((pair.get("volume") or {}).get("h24"))
    change = _as_float((pair.get("priceChange") or {}).get("h24"))
    txns = pair.get("txns") or {}
    txns_h24 = txns.get("h24") or {}
    buys = _as_float(txns_h24.get("buys"))
    sells = _as_float(txns_h24.get("sells"))
    created_at = _as_float(pair.get("pairCreatedAt"))
    return {
        "mint": mint,
        "symbol": base.get("symbol") or pair.get("symbol") or "?",
        "name": base.get("name") or pair.get("name"),
        "market_cap_usd": _as_float(pair.get("marketCap") or pair.get("fdv")),
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "change_24h_pct": change,
        "txns_24h": (
            int(buys + sells)
            if buys is not None and sells is not None
            else None
        ),
        "pair_created_at_ms": created_at,
    }


def pick_main_pair(pairs: list[dict]) -> dict | None:
    """The main pair for a mint: the one with the most liquidity.

    DexScreener returns every pair a token trades on; the
    highest-liquidity pair is the one that matters. Returns None
    when no pair in the list parses.
    """
    parsed = [p for p in (parse_pair(pair) for pair in pairs) if p]
    if not parsed:
        return None
    return max(parsed, key=lambda p: p["liquidity_usd"] if p["liquidity_usd"] is not None else -1.0)


def build_record(obs: dict, fetched_at: datetime, source: str = "dexscreener") -> dict:
    """A dataset record from one token observation.

    Real ml_model features are included only when DexScreener
    actually provided them; the other 8 features of the 10-vector
    are simply absent (``extract_features`` marks them missing).
    ``collapse_pct`` is the documented 24h-fall proxy for the
    peak-to-current collapse the labeller uses. ``source`` is
    "dexscreener" (v2.9 discovery) or "pumpfun" (v2.10 pump.fun
    launches enriched via DexScreener). When the DexScreener pair
    has no creation time, the pump.fun launch time (``created_at_ms``,
    when the observation carries it) is the age fallback — a real
    launch timestamp, never guessed.
    """
    age_days = None
    created_at = obs.get("pair_created_at_ms")
    if created_at is None:
        created_at = obs.get("created_at_ms")
    if created_at is not None:
        age_days = (fetched_at.timestamp() * 1000.0 - created_at) / 86_400_000.0
    labelled_obs = {
        "change_24h_pct": obs.get("change_24h_pct"),
        "liquidity_usd": obs.get("liquidity_usd"),
        "volume_24h_usd": obs.get("volume_24h_usd"),
        "market_cap_usd": obs.get("market_cap_usd"),
        "pair_age_days": age_days,
    }
    record: dict = {
        "mint": obs["mint"],
        "symbol": obs.get("symbol"),
        "name": obs.get("name"),
        "outcome": label_token(labelled_obs),
        "source": source,
        "market_cap_usd": obs.get("market_cap_usd"),
        "volume_24h_usd": obs.get("volume_24h_usd"),
        "change_24h_pct": obs.get("change_24h_pct"),
        "txns_24h": obs.get("txns_24h"),
        "pair_age_days": (
            round(age_days, 2) if age_days is not None else None
        ),
        "dexscreener_url": f"https://dexscreener.com/solana/{obs['mint']}",
        "fetched_at": fetched_at.isoformat(),
        "labelling_rule": labelling_rule(),
        "labelling_rule_version": LABELLING_RULE_VERSION,
        "synthetic": False,
    }
    if source == "pumpfun":
        record["pumpfun_url"] = f"https://pump.fun/coin/{obs['mint']}"
    # Real features for the ml_model 10-vector — only when
    # DexScreener provided them. Absent keys are missing features,
    # never zero-filled as if real.
    change = obs.get("change_24h_pct")
    if change is not None:
        record["collapse_pct"] = round(max(0.0, -change), 2)
    if obs.get("liquidity_usd") is not None:
        record["liquidity_usd"] = obs["liquidity_usd"]
    return record


def summarize(records: list[dict]) -> dict:
    """Counts, class balance and feature coverage for a dataset."""
    rugged = sum(1 for r in records if r.get("outcome") == "rugged")
    survived = sum(1 for r in records if r.get("outcome") == "survived")
    excluded = sum(1 for r in records if r.get("outcome") == "excluded")
    coverage = {}
    for feature in ("collapse_pct", "liquidity_usd"):
        coverage[feature] = sum(
            1 for r in records if r.get(feature) is not None
        )
    missing_features = [
        name
        for name in (
            "dev_sold_pct",
            "lp_removed",
            "time_to_collapse_minutes",
            "wash_score",
            "top1_share",
            "top10_share",
            "holder_count",
            "cluster_size",
        )
        if not any(name in r for r in records)
    ]
    by_source: dict[str, int] = {}
    for record in records:
        source = record.get("source", "unknown")
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "n_total": len(records),
        "n_rugged": rugged,
        "n_survived": survived,
        "n_excluded": excluded,
        "n_labelled": rugged + survived,
        "by_source": by_source,
        "feature_coverage": coverage,
        "features_always_missing": missing_features,
    }


def relabel_records(records: list[dict]) -> list[dict]:
    """Re-label existing dataset records with the CURRENT rule (real-v2).

    Used when the labelling rule itself changes (v2.13): the
    stored market facts (change, liquidity, volume, market cap,
    pair age — as observed at fetch time) are re-run through
    ``label_token`` and the outcome + labelling-rule fields are
    updated. Nothing is re-fetched and no fact is changed: the
    mint, source, fetched_at, DexScreener URL and feature values
    stay exactly as recorded. Records missing an observation
    field are labelled from what they have — missing data never
    satisfies a threshold, so a record with no pair age becomes
    "excluded" unless the (unchanged) survived rule still holds.
    Returns new dicts; the input list is not mutated.
    """
    relabelled: list[dict] = []
    for record in records:
        new = dict(record)
        obs = {
            "change_24h_pct": record.get("change_24h_pct"),
            "liquidity_usd": record.get("liquidity_usd"),
            "volume_24h_usd": record.get("volume_24h_usd"),
            "market_cap_usd": record.get("market_cap_usd"),
            "pair_age_days": record.get("pair_age_days"),
        }
        new["outcome"] = label_token(obs)
        new["labelling_rule"] = labelling_rule()
        new["labelling_rule_version"] = LABELLING_RULE_VERSION
        relabelled.append(new)
    return relabelled


def save_dataset(path: str | Path, records: list[dict]) -> None:
    """Write the dataset (a JSON list) to a caller-given file."""
    p = Path(path)
    if p.name == "registry.json":
        raise ValueError("Refusing to write to registry.json.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2), encoding="utf-8")


class RealDataFetcher:
    """No-key DexScreener fetch for the real-world dataset."""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def _get_json(self, url: str) -> object:
        try:
            if self._client is not None:
                resp = self._client.get(url)
            else:
                with make_client(timeout=30.0) as client:
                    resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"DexScreener request failed ({url}): {exc}"
            ) from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"DexScreener returned invalid JSON ({url})"
            ) from exc

    def _sleep(self) -> None:
        # Injected clients (tests) skip the polite delay.
        if self._client is None:
            time.sleep(REQUEST_SLEEP_SECONDS)

    def fetch_profile_mints(self) -> list[str]:
        """Solana token addresses from the latest token profiles."""
        data = self._get_json(TOKEN_PROFILES_URL)
        if not isinstance(data, list):
            raise RuntimeError("DexScreener profiles response was not a list")
        mints: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("chainId") not in (None, "solana"):
                continue
            mint = item.get("tokenAddress")
            if mint:
                mints.append(mint)
        return mints

    def fetch_boost_mints(self) -> list[str]:
        """Solana token addresses from the latest token boosts."""
        data = self._get_json(TOKEN_BOOSTS_URL)
        if not isinstance(data, list):
            raise RuntimeError("DexScreener boosts response was not a list")
        mints: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("chainId") not in (None, "solana"):
                continue
            mint = item.get("tokenAddress")
            if mint:
                mints.append(mint)
        return mints

    def fetch_search_mints(self, query: str) -> list[str]:
        """Solana token addresses from a DexScreener pair search."""
        data = self._get_json(f"{SEARCH_URL}?q={query}")
        pairs = data.get("pairs") if isinstance(data, dict) else data
        if not isinstance(pairs, list):
            raise RuntimeError("DexScreener search response was not a pair list")
        mints: list[str] = []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            if pair.get("chainId") != "solana":
                continue
            base = pair.get("baseToken") or {}
            mint = base.get("address")
            if mint:
                mints.append(mint)
        return mints

    def fetch_token_details(self, mints: list[str]) -> list[dict]:
        """One observation per mint: its highest-liquidity pair."""
        observations: list[dict] = []
        for i in range(0, len(mints), MAX_MINTS_PER_REQUEST):
            chunk = mints[i : i + MAX_MINTS_PER_REQUEST]
            data = self._get_json(f"{TOKENS_URL}/{','.join(chunk)}")
            pairs = data.get("pairs") if isinstance(data, dict) else data
            if not isinstance(pairs, list):
                raise RuntimeError("DexScreener tokens response was not a pair list")
            by_mint: dict[str, list[dict]] = {}
            for pair in pairs:
                if not isinstance(pair, dict):
                    continue
                base = pair.get("baseToken") or {}
                mint = pair.get("tokenAddress") or base.get("address")
                if mint:
                    by_mint.setdefault(mint, []).append(pair)
            for mint in chunk:
                main = pick_main_pair(by_mint.get(mint, []))
                if main is not None:
                    observations.append(main)
            self._sleep()
        return observations

    def fetch(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Fetch the real-world dataset: mints -> observations -> records."""
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        mints: list[str] = []
        seen: set[str] = set()

        def _add(new_mints: list[str]) -> None:
            for mint in new_mints:
                if mint not in seen:
                    seen.add(mint)
                    mints.append(mint)

        _add(self.fetch_profile_mints())
        self._sleep()
        _add(self.fetch_boost_mints())
        self._sleep()
        for query in SEARCH_QUERIES:
            _add(self.fetch_search_mints(query))
            self._sleep()
            if len(mints) >= limit:
                break
        mints = mints[:limit]
        if not mints:
            return []
        fetched_at = datetime.now(timezone.utc)
        observations = self.fetch_token_details(mints)
        return [build_record(obs, fetched_at) for obs in observations]


def parse_pumpfun_coin(coin: dict) -> dict | None:
    """Parse one pump.fun frontend-API coin into a launch.

    Only the mint and the launch timestamp are needed (the mint
    list is what pump.fun is for; market facts come from the
    DexScreener enrichment). Returns None when the object has no
    mint or no launch timestamp — never guessed.
    """
    if not isinstance(coin, dict):
        return None
    mint = coin.get("mint")
    created = _as_float(coin.get("created_timestamp"))
    if not mint or created is None:
        return None
    return {
        "mint": mint,
        "symbol": coin.get("symbol"),
        "name": coin.get("name"),
        "created_at_ms": created,
    }


class PumpFunFetcher:
    """No-key pump.fun frontend-API fetch for real launches.

    The frontend API lists launches newest-first; paging at
    increasing offsets gives a spread of ages. Most pump.fun
    coins die within hours, which is exactly the rugged-like
    population DexScreener discovery hides.
    """

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def _sleep(self, seconds: float) -> None:
        # Injected clients (tests) skip the polite delay / backoff.
        if self._client is None:
            time.sleep(seconds)

    def _get_json(self, params: dict) -> object:
        attempts = 0
        while True:
            try:
                if self._client is not None:
                    resp = self._client.get(PUMPFUN_COINS_URL, params=params)
                else:
                    with make_client(timeout=30.0) as client:
                        resp = client.get(PUMPFUN_COINS_URL, params=params)
                if resp.status_code == 429 and attempts < PUMPFUN_MAX_429_RETRIES:
                    attempts += 1
                    self._sleep(PUMPFUN_429_BACKOFF_SECONDS[attempts - 1])
                    continue
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"pump.fun request failed ({PUMPFUN_COINS_URL}): {exc}"
                ) from exc
            try:
                return resp.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"pump.fun returned invalid JSON ({PUMPFUN_COINS_URL})"
                ) from exc

    def fetch_launches(self, limit: int = DEFAULT_PUMPFUN_LIMIT) -> list[dict]:
        """Launches newest-first, deduped by mint, up to ``limit``."""
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        launches: list[dict] = []
        seen: set[str] = set()
        for offset in PUMPFUN_PAGE_OFFSETS:
            if len(launches) >= limit:
                break
            data = self._get_json(
                {
                    "offset": offset,
                    "limit": PUMPFUN_PAGE_LIMIT,
                    "sort": "created_timestamp",
                    "order": "DESC",
                    "includeNsfw": "false",
                }
            )
            if not isinstance(data, list):
                raise RuntimeError("pump.fun coins response was not a list")
            if not data:
                break
            for coin in data:
                parsed = parse_pumpfun_coin(coin)
                if parsed and parsed["mint"] not in seen:
                    seen.add(parsed["mint"])
                    launches.append(parsed)
            self._sleep(PUMPFUN_REQUEST_SLEEP_SECONDS)
            # Only an empty page stops paging: a short page may just
            # be a thin slice, and the next offset may still have
            # coins (the API caps total results, it does not promise
            # full pages). An empty page means there is nothing more.
        return launches[:limit]


def fetch_pumpfun_records(
    client: httpx.Client | None = None,
    limit: int = DEFAULT_PUMPFUN_LIMIT,
) -> list[dict]:
    """Pump.fun launches -> DexScreener enrichment -> real-v2 records.

    The labelling is the SAME fixed rule as the DexScreener path
    (``label_token`` via ``build_record``) — the source only
    changes which mints are sampled. A launch with no DexScreener
    pair yields no record: no market facts to label from.
    """
    launches = PumpFunFetcher(client=client).fetch_launches(limit=limit)
    if not launches:
        return []
    created_by_mint = {item["mint"]: item["created_at_ms"] for item in launches}
    mints = [item["mint"] for item in launches]
    observations = RealDataFetcher(client=client).fetch_token_details(mints)
    fetched_at = datetime.now(timezone.utc)
    records: list[dict] = []
    for obs in observations:
        obs = dict(obs)
        obs.setdefault("created_at_ms", created_by_mint.get(obs["mint"]))
        records.append(build_record(obs, fetched_at, source="pumpfun"))
    return records


def load_documented_list(path: str | Path) -> list[dict]:
    """Load a caller-supplied list of documented rugged mints.

    The file must hold a JSON list of objects, each with "mint"
    (a Solana mint address, verbatim from a public source) and
    "source" (that source's URL; "source_url" is accepted as an
    alias). Extra keys (e.g. "name") are kept for readability but
    ignored by verification. Raises ValueError on a bad file or
    a bad entry — never silently drops an entry here; whether
    the address itself is a valid Solana address is checked in
    verify_documented_mints.
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Documented list {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Documented list {p} must hold a JSON list.")
    entries: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Documented list entry {i} must be an object.")
        mint = item.get("mint")
        source = item.get("source", item.get("source_url"))
        if not isinstance(mint, str) or not mint:
            raise ValueError(f"Documented list entry {i} needs a 'mint' string.")
        if not isinstance(source, str) or not source:
            raise ValueError(f"Documented list entry {i} needs a 'source' URL.")
        entry = {"mint": mint, "source": source}
        if item.get("name") is not None:
            entry["name"] = item["name"]
        entries.append(entry)
    return entries


def verify_documented_mints(
    entries: list[dict],
    client: httpx.Client | None = None,
) -> dict:
    """Verify documented rugged mints via DexScreener + real-v2.

    Each entry is {"mint": <Solana address>, "source": <public
    source URL>}. Duplicate mints are deduped (first entry
    wins). Addresses that are not valid Solana addresses are
    rejected without a fetch; mints with no DexScreener pair are
    dropped (no market facts to label from — never invented).
    The rest are labelled with the SAME fixed "real-v2" rule via
    build_record, with the public source URL on every record.
    Returns {"records", "verified", "dropped_no_pair",
    "rejected_invalid", "fetched_at"}.
    """
    seen: set[str] = set()
    valid: list[dict] = []
    rejected_invalid: list[dict] = []
    for entry in entries:
        mint = entry.get("mint")
        if not isinstance(mint, str) or not mint:
            rejected_invalid.append(
                {"mint": mint, "reason": "missing mint"}
            )
            continue
        if mint in seen:
            continue  # dedupe: first entry wins
        seen.add(mint)
        if not is_valid_solana_address(mint):
            rejected_invalid.append(
                {"mint": mint, "reason": "not a valid Solana address"}
            )
            continue
        valid.append(entry)
    fetched_at = datetime.now(timezone.utc)
    if not valid:
        return {
            "records": [],
            "verified": [],
            "dropped_no_pair": [],
            "rejected_invalid": rejected_invalid,
            "fetched_at": fetched_at.isoformat(),
        }
    source_by_mint = {entry["mint"]: entry["source"] for entry in valid}
    mints = [entry["mint"] for entry in valid]
    observations = RealDataFetcher(client=client).fetch_token_details(mints)
    records = [
        build_record(obs, fetched_at, source=source_by_mint[obs["mint"]])
        for obs in observations
    ]
    observed_mints = {obs["mint"] for obs in observations}
    return {
        "records": records,
        "verified": [obs["mint"] for obs in observations],
        "dropped_no_pair": [m for m in mints if m not in observed_mints],
        "rejected_invalid": rejected_invalid,
        "fetched_at": fetched_at.isoformat(),
    }


def format_documented_summary(
    result: dict, n_existing: int, merged: list[dict]
) -> str:
    """Human-readable summary of a documented-rugs merge run."""
    summary = summarize(merged)
    new_summary = summarize(result["records"])
    lines = [
        f"Documented rugs: {len(result['verified'])} verified with a "
        f"DexScreener pair ({new_summary['n_rugged']} rugged-like, "
        f"{new_summary['n_survived']} survived, "
        f"{new_summary['n_excluded']} excluded), "
        f"{len(result['dropped_no_pair'])} dropped (no pair), "
        f"{len(result['rejected_invalid'])} rejected (invalid address).",
        f"Merged dataset: {summary['n_total']} records "
        f"({summary['n_rugged']} rugged-like, "
        f"{summary['n_survived']} survived, "
        f"{summary['n_excluded']} excluded; "
        f"{summary['n_labelled']} labelled).",
        "Labels are observable-market-fact labels, not verified "
        "rug verdicts — see the module docstring before quoting "
        "any metric.",
    ]
    return "\n".join(lines)


def merge_datasets(existing: list[dict], new: list[dict]) -> list[dict]:
    """Merge new records into an existing dataset, deduped by mint.

    Existing records win on mint collisions (the already-committed
    DexScreener records stay untouched). Records without a
    "source" are backfilled as "dexscreener" when they carry a
    dexscreener_url. Existing order is kept first.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for record in existing:
        mint = record.get("mint")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        record = dict(record)
        if "source" not in record and record.get("dexscreener_url"):
            record["source"] = "dexscreener"
        merged.append(record)
    for record in new:
        mint = record.get("mint")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        merged.append(dict(record))
    return merged


def _load_existing_dataset(path: str | Path) -> list[dict]:
    """Existing dataset records at ``path``; [] when the file is absent."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Existing dataset {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Existing dataset {p} must hold a JSON list.")
    return data


def summary_path(out: str | Path) -> Path:
    """Sidecar path for a dataset file: <name>.summary.json."""
    p = Path(out)
    return p.with_name(f"{p.stem}.summary.json")


def save_summary(path: str | Path, records: list[dict]) -> None:
    """Write the dataset summary/metadata sidecar (counts per source)."""
    summary = summarize(records)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "labelling_rule_version": LABELLING_RULE_VERSION,
        "sources": summary["by_source"],
        "summary": summary,
        "note": (
            "Labels are observable-market-fact labels (rule "
            f"{LABELLING_RULE_VERSION}), not verified rug verdicts. "
            "Per-record 'source': 'dexscreener' (v2.9 discovery), "
            "'pumpfun' (v2.10 pump.fun launches enriched via "
            "DexScreener), or a public source URL for a documented "
            "rug (v2.11)."
        ),
    }
    p = Path(path)
    if p.name == "registry.json":
        raise ValueError("Refusing to write to registry.json.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def format_merge_summary(
    n_existing: int, new_records: list[dict], merged: list[dict]
) -> str:
    """Human-readable summary of a pump.fun merge run."""
    summary = summarize(merged)
    new_summary = summarize(new_records)
    lines = [
        f"Pump.fun launches added {len(new_records)} new records "
        f"({new_summary['n_rugged']} rugged-like, "
        f"{new_summary['n_survived']} survived, "
        f"{new_summary['n_excluded']} excluded) to {n_existing} "
        f"existing records.",
        f"Merged dataset: {summary['n_total']} records "
        f"({summary['n_rugged']} rugged-like, "
        f"{summary['n_survived']} survived, "
        f"{summary['n_excluded']} excluded; "
        f"{summary['n_labelled']} labelled).",
        "By source: "
        + ", ".join(
            f"{source} {count}"
            for source, count in sorted(summary["by_source"].items())
        )
        + ".",
        "Labels are observable-market-fact labels, not verified "
        "rug verdicts — see the module docstring before quoting "
        "any metric.",
    ]
    return "\n".join(lines)


def format_relabel_summary(
    before_records: list[dict], after_records: list[dict]
) -> str:
    """Human-readable before/after summary of a --relabel run.

    Lists every mint whose outcome changed, so the re-label is
    auditable from the output alone.
    """
    before = summarize(before_records)
    after = summarize(after_records)
    before_by_mint = {r.get("mint"): r for r in before_records}
    lines = [
        f"Re-labelled with rule {LABELLING_RULE_VERSION}: "
        f"{before['n_total']} records, "
        f"{before['n_rugged']} rugged-like -> {after['n_rugged']}, "
        f"{before['n_survived']} survived -> {after['n_survived']}, "
        f"{before['n_excluded']} excluded -> {after['n_excluded']} "
        f"({after['n_labelled']} labelled).",
    ]
    changed = [
        r
        for r in after_records
        if before_by_mint.get(r.get("mint"), {}).get("outcome")
        != r.get("outcome")
    ]
    if not changed:
        lines.append("No record changed outcome.")
    else:
        lines.append(f"{len(changed)} record(s) changed outcome:")
        for record in changed:
            old = before_by_mint.get(record.get("mint"), {}).get("outcome")
            lines.append(
                f"  - {record.get('symbol') or '?'} "
                f"{record.get('mint')}: {old} -> {record.get('outcome')}"
            )
    return "\n".join(lines)


def format_summary(records: list[dict]) -> str:
    """Human-readable summary of a fetched dataset."""
    summary = summarize(records)
    lines = [
        f"Fetched {summary['n_total']} real tokens from DexScreener.",
        f"Labelled (rule {LABELLING_RULE_VERSION}): "
        f"{summary['n_rugged']} rugged-like, "
        f"{summary['n_survived']} survived, "
        f"{summary['n_excluded']} excluded (ambiguous middle).",
    ]
    for feature, count in summary["feature_coverage"].items():
        lines.append(
            f"  feature {feature}: present on {count}/{summary['n_total']} records"
        )
    lines.append(
        "  features always missing on real DexScreener data: "
        + ", ".join(summary["features_always_missing"])
    )
    lines.append(
        "Labels are observable-market-fact labels, not verified "
        "rug verdicts — see the module docstring before quoting "
        "any metric."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    """Real-world dataset CLI.

    ``python -m src.real_data --fetch [--limit N] [--out path] [--json]``
    ``python -m src.real_data --fetch-pumpfun [--pumpfun-limit N] [--out path] [--json]``
    ``python -m src.real_data --verify-documented <list.json> [--out path] [--json]``
    ``python -m src.real_data --relabel [--out path] [--json]``
    Exit 0 = ok, 2 = invalid args / network error / too few usable
    tokens (< 10 labelled records). The file is still saved when
    the fetch itself succeeded, so the run stays auditable.
    """
    parser = argparse.ArgumentParser(
        description="Fetch real Solana meme coins from DexScreener and build the labelled real-world dataset for the ML rug-risk model."
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch the dataset from DexScreener",
    )
    parser.add_argument(
        "--fetch-pumpfun",
        action="store_true",
        help="Fetch pump.fun launches, enrich via DexScreener, and merge into the dataset at --out",
    )
    parser.add_argument(
        "--verify-documented",
        type=str,
        default=None,
        metavar="LIST_JSON",
        help="Verify a JSON list of documented rugged mints ({mint, source} pairs) via DexScreener and merge the verified records into the dataset at --out",
    )
    parser.add_argument(
        "--relabel",
        action="store_true",
        help="Re-label the existing dataset at --out with the current rule (real-v2) from the stored market facts — no re-fetch",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"How many tokens to fetch (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--pumpfun-limit",
        type=int,
        default=DEFAULT_PUMPFUN_LIMIT,
        help=f"How many pump.fun launches to consider (default: {DEFAULT_PUMPFUN_LIMIT})",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=DEFAULT_OUT,
        help=f"Where to write the dataset JSON list (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the dataset as JSON instead of a text summary",
    )
    args = parser.parse_args(argv)

    if sum([args.fetch, args.fetch_pumpfun, args.relabel, bool(args.verify_documented)]) != 1:
        print(
            "ERROR: nothing to do — pass exactly one of --fetch, "
            "--fetch-pumpfun, --verify-documented or --relabel.",
            file=sys.stderr,
        )
        return EXIT_INVALID
    if args.limit <= 0 or args.pumpfun_limit <= 0:
        print("ERROR: --limit / --pumpfun-limit must be positive integers.", file=sys.stderr)
        return EXIT_INVALID
    try:
        if args.relabel:
            existing = _load_existing_dataset(args.out)
            records = relabel_records(existing)
            save_dataset(args.out, records)
            save_summary(summary_path(args.out), records)
            if args.json:
                print(json.dumps(records, indent=2))
            else:
                print(format_relabel_summary(existing, records))
                print(f"Saved to {args.out}")
                print(f"Summary to {summary_path(args.out)}")
        elif args.verify_documented:
            existing = _load_existing_dataset(args.out)
            entries = load_documented_list(args.verify_documented)
            result = verify_documented_mints(entries, client=client)
            records = merge_datasets(existing, result["records"])
            save_dataset(args.out, records)
            save_summary(summary_path(args.out), records)
            if args.json:
                print(json.dumps(records, indent=2))
            else:
                print(format_documented_summary(result, len(existing), records))
                print(f"Saved to {args.out}")
                print(f"Summary to {summary_path(args.out)}")
        elif args.fetch_pumpfun:
            existing = _load_existing_dataset(args.out)
            new_records = fetch_pumpfun_records(
                client=client, limit=args.pumpfun_limit
            )
            records = merge_datasets(existing, new_records)
            save_dataset(args.out, records)
            save_summary(summary_path(args.out), records)
            if args.json:
                print(json.dumps(records, indent=2))
            else:
                print(format_merge_summary(len(existing), new_records, records))
                print(f"Saved to {args.out}")
                print(f"Summary to {summary_path(args.out)}")
        else:
            records = RealDataFetcher(client=client).fetch(limit=args.limit)
            save_dataset(args.out, records)
            if args.json:
                print(json.dumps(records, indent=2))
            else:
                print(format_summary(records))
                print(f"Saved to {args.out}")
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID
    summary = summarize(records)
    if summary["n_labelled"] < MIN_LABELLED_RECORDS:
        print(
            f"ERROR: too few usable tokens: {summary['n_labelled']} "
            f"labelled records, need at least {MIN_LABELLED_RECORDS} "
            f"to train the model. The file was still saved for audit.",
            file=sys.stderr,
        )
        return EXIT_INVALID
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
