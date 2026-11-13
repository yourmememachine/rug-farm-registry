"""Deep check (v2.12, keyless fallbacks v2.16): one command, one full card for a mint.

The manual check that used to need four tools — DexScreener for the
coin, Solscan for the dev wallet, the pump.fun profile for the dev's
launches, Bubblemaps for the holder cluster — now runs from one
command:

    python -m src.deep_check --mint <address> [--json]

1. **Coin** — live DexScreener data (price, market cap, liquidity,
   volume, price change, txns, pair age), no key needed. Labelled
   with ``src.labeller`` on the same peak estimate the indexer
   uses (peak ~= current / (1 + 24h%) when 24h is negative; LP
   removal inferred from liquidity < $1,000; dev sell is 0.0
   unless proven — DexScreener does not prove one). When
   DexScreener has no pair for the mint (never listed), the
   public Solana RPC fills the card instead: supply, holders,
   top-holder shares, mint/freeze authority, Metaplex
   name/symbol — no price on-chain, and the card says so.
   Nothing is invented: a missing piece stays missing.
2. **Creator / dev** — resolved via Helius DAS ``getAsset``
   (the Metaplex creators array) when a Helius key is set;
   otherwise the same creators array from the Metaplex metadata
   account via the public RPC, falling back to the fee payer
   of the mint's oldest fetched creation transaction. If
   ``HELIUS_API_KEY`` is missing, the dev half runs keyless
   instead of being skipped: creator from metadata, dev history
   from the dev's last 100 signatures, funding from the dev +
   top 5 early buyers. A creator is never invented: no creators
   found means dev unknown, full stop.
3. **Dev history** — the existing ``DevHistoryBuilder`` for the
   resolved dev (registry verdict, rugged-mint count, launches
   where known). The flagging rule is unchanged: ``flagged``
   still needs >= 2 rugged mints + evidence. Keyless, the
   history is bounded to the last 100 signatures — old rugs can
   be missed, and the report says so.
4. **Side wallets** — funding transfers for the dev (who funded
   the dev, who the dev funded) plus the mint's early buyers
   (token-balance diffs in the first signatures), run through
   ``StealthClusterDetector``. Each link carries its evidence:
   amount, date, signature. A suspicious pattern alone is a
   "high-risk pattern", never "flagged". Keyless, the funding
   scan covers the dev + top 5 early buyers (20 signatures
   each) — the report says so.
5. **Wash** — ``WashScore`` only when holder data is supplied
   (``--holders`` / ``--top10``); DexScreener does not publish
   holder shares, so without them the wash section is
   "insufficient data", never a guess. When the coin card came
   from the public RPC, its holder data (holder count, top-10
   shares) scores the wash section instead.

Honest limits, stated in the report, not hidden:
- The funding scan covers the most recent signatures per wallet
  (bounded). Funding from long ago — e.g. the wallet-creation
  funding of a dev — is out of reach without full-history
  paging, so an old funder link can be missed. Keyless, the
  bound is tighter (last 100 signatures for the dev history,
  20 per wallet for funding) and the report says so.
- Early buyers come from the fetched signature window on the
  mint; for an old mint the window may not reach launch.
- Holder-cluster data (Bubblemaps-style) has no public API, so
  the cluster section is funding clusters only.
- The public RPC rate-limits by IP. A throttled call is
  reported as missing data, never faked.

Exit codes: 0 = done (not flagged), 1 = flagged dev,
2 = invalid args / invalid address / network error / neither
DexScreener nor the public RPC had any data for the mint.
A missing Helius key is NOT an error for the whole command —
it degrades to the keyless path with a clear note.

Stdlib + httpx only, no wallet, no funds moved, no invented data.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .advanced import FundingEvent, StealthClusterDetector, wash_score
from .dev_check import parse_creation_any
from .dev_history import DevHistory, DevHistoryBuilder
from .indexer import HeliusIndexer, PUMP_FUN_PROGRAM
from .labeller import MintEvent, label_outcome
from .public_rpc import (
    PublicRpcClient,
    PublicRpcIndexer,
    fetch_rpc_coin,
)
from .pump_watch import TOKENS_URL, parse_token_pair
from .registry import check_wallet, is_valid_solana_address, load_registry

# Captured at import time: tests monkeypatch the module-global
# ``HeliusIndexer`` with a stub, and this staticmethod must keep
# working regardless.
_parse_transaction = HeliusIndexer.parse_transaction


def _as_float_str(value: object) -> float | None:
    """Float from a number or a numeric string (DexScreener's
    ``priceUsd`` is a string). None when it is neither."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None

EXIT_OK = 0
EXIT_FLAGGED = 1
EXIT_BLOCKED = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"

# DexScreener liquidity below this is treated as "LP removed / drained"
# (same threshold as src.indexer.LP_DRAINED_USD).
LP_DRAINED_USD = 1000.0

DEFAULT_HISTORY_LIMIT = 50
DEFAULT_BUYER_SIGNATURES = 50
DEFAULT_FUNDING_LIMIT = 20
# How many of the largest early buyers get a funding lookup.
MAX_FUNDING_WALLETS = 20
# Keyless (no Helius key) bounds for the public-RPC path. Every
# public-RPC transaction fetch is a paced call, so the keyless
# path is bounded harder than the keyed one — and the report
# says so wherever the bound bites.
KEYLESS_SIGNATURE_LIMIT = 100  # dev-history signature window
KEYLESS_BUYER_LIMIT = 20  # mint signatures scanned for early buyers
KEYLESS_MAX_FUNDING_WALLETS = 5  # dev + top N early buyers get a funding lookup
# Creation-tx paging budget: how many 100-signature pages the
# creator lookup walks back through. BagsBack needed 7 pages
# (661 signatures) — 10 covers 1,000.
MAX_CREATION_PAGES = 10


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _epoch_to_iso(epoch: int | float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


# ======================================================================
# Coin (DexScreener, no key)
# ======================================================================
def parse_coin(pair: dict) -> dict | None:
    """Rich coin card from one DexScreener pair object.

    Reuses ``src.pump_watch.parse_token_pair`` for the base fields
    and adds price, per-window changes, txns and pair age. Returns
    None when the object has no mint.
    """
    base = parse_token_pair(pair)
    if base is None:
        return None
    changes = pair.get("priceChange") or {}
    volumes = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}
    created_ms = pair.get("pairCreatedAt")
    created_iso = None
    age_minutes = None
    if isinstance(created_ms, (int, float)):
        created_iso = _epoch_to_iso(created_ms / 1000)
        age_minutes = max(
            0, int((datetime.now(timezone.utc).timestamp() - created_ms / 1000) // 60)
        )
    base.update(
        {
            "price_usd": _as_float_str(pair.get("priceUsd")),
            "price_change_m5_pct": _as_float(changes.get("m5")),
            "price_change_h1_pct": _as_float(changes.get("h1")),
            "price_change_h6_pct": _as_float(changes.get("h6")),
            "volume_h1_usd": _as_float(volumes.get("h1")),
            "volume_h6_usd": _as_float(volumes.get("h6")),
            "txns_24h_buys": h24.get("buys"),
            "txns_24h_sells": h24.get("sells"),
            "pair_created_at": created_iso,
            "pair_age_minutes": age_minutes,
            "pair_address": pair.get("pairAddress"),
            "dex_id": pair.get("dexId"),
        }
    )
    return base


def outcome_from_coin(coin: dict) -> str:
    """Labeller outcome for the coin itself, from its DexScreener data.

    Same peak estimate as ``src.indexer.fetch_mint_outcome``:
    peak ~= current / (1 + 24h%) when 24h is negative; LP removal
    from liquidity < $1,000; dev sell is 0.0 — DexScreener does not
    prove a dev sell, and we do not invent one. No pair data at
    all -> ``insufficient_data``.
    """
    current = coin.get("market_cap_usd")
    change_24h = coin.get("change_24h_pct")
    liquidity = coin.get("liquidity_usd")
    if current is None:
        return "insufficient_data"
    peak = None
    if isinstance(change_24h, (int, float)):
        if change_24h < 0:
            factor = max(0.01, 1.0 + change_24h / 100.0)
            peak = current / factor
        else:
            peak = current
    return label_outcome(
        MintEvent(
            peak_market_cap_usd=peak,
            current_market_cap_usd=current,
            lp_removed=bool(
                liquidity is not None and liquidity < LP_DRAINED_USD
            ),
            dev_sold_pct=0.0,
            time_to_collapse_minutes=coin.get("pair_age_minutes"),
        )
    )


class CoinFetcher:
    """Live DexScreener coin data for one mint (no key)."""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def fetch(self, mint: str) -> dict | None:
        """Rich coin card for the mint's most-liquid pair, or None
        when DexScreener has no pair for it. Raises RuntimeError on
        a network / bad-JSON failure."""
        url = f"{TOKENS_URL}/{mint}"
        try:
            if self._client is not None:
                resp = self._client.get(url)
            else:
                from .http_client import make_client

                with make_client(timeout=30.0) as client:
                    resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"DexScreener request failed ({url}): {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"DexScreener returned invalid JSON ({url})") from exc
        if not isinstance(data, list):
            raise RuntimeError("DexScreener tokens response was not a list")
        entries = []
        for item in data:
            parsed = parse_coin(item)
            if parsed is not None:
                entries.append(parsed)
        if not entries:
            return None
        # The most-liquid pair is the coin's main market.
        best = max(entries, key=lambda e: e.get("liquidity_usd") or 0)
        best["outcome"] = outcome_from_coin(best)
        return best


# ======================================================================
# Creator resolution (Helius DAS getAsset, creation-tx fallback)
# ======================================================================
def _oldest_creation_tx(indexer, mint: str, page_size: int = 100,
                        max_pages: int = MAX_CREATION_PAGES):
    """The mint's creation transaction, by paging its signatures
    backwards (bounded).

    The creation is the OLDEST signature, so each page's tail is
    checked: a full page whose tail is not the creation means
    older pages exist, so page back with ``before``. A short
    page's tail is the oldest signature overall. Strict: only a
    real pump.fun create instruction counts (see
    ``_is_creation_tx``) — a buy is never mistaken for a
    creation. Returns the transaction dict, or None when no
    creation was found in the fetched pages. Indexers without
    ``before`` support (old stubs) degrade to a single page.
    Raises when the signature listing itself fails — the caller
    turns that into a note.
    """
    seen_tails: set[str] = set()
    before = None
    for _ in range(max_pages):
        try:
            sigs = indexer.get_signatures(mint, limit=page_size, before=before)
        except TypeError:
            # Indexer without `before` support: one page only.
            sigs = indexer.get_signatures(mint, limit=page_size)
        if not sigs:
            return None
        tail = sigs[-1]
        tail_sig = tail.get("signature") if isinstance(tail, dict) else None
        if not tail_sig or tail_sig in seen_tails:
            return None
        seen_tails.add(tail_sig)
        try:
            tx = indexer.get_transaction(tail_sig)
        except Exception:  # noqa: BLE001 — one failed fetch is not fatal
            tx = None
        if tx and _is_creation_tx(tx, mint):
            return tx
        if len(sigs) < page_size:
            return None  # last page: its tail is the oldest signature
        before = tail_sig
    return None


def resolve_creator(
    indexer, mint: str, signature_limit: int = 50
) -> tuple[str | None, str, str | None]:
    """Resolve a mint's creator wallet.

    Returns ``(creator_or_None, method, note)`` where method is
    ``"das"`` (Metaplex creators array via Helius DAS getAsset),
    ``"creation_tx"`` (fee payer of the oldest fetched creation
    transaction), or ``"none"``. A creator is never invented: no
    creators found means (None, "none", reason). The creation-tx
    fallback pages the mint's signature history backwards
    (bounded, 10 pages) so an old mint's creation is reached even
    when the newest window does not contain it.
    """
    # Primary: DAS getAsset -> Metaplex creators array.
    try:
        result = indexer._rpc("getAsset", {"id": mint})
    except Exception as exc:  # noqa: BLE001 — DAS down is not fatal here
        result = None
        das_error = str(exc)
    else:
        das_error = None
    if isinstance(result, dict):
        creators = [
            c
            for c in (result.get("creators") or [])
            if isinstance(c, dict) and c.get("address")
        ]
        if creators:
            # Highest share wins; a verified creator wins ties.
            best = max(
                creators,
                key=lambda c: (bool(c.get("verified")), c.get("share") or 0),
            )
            return best["address"], "das", None
    # Fallback: fee payer of the oldest fetched creation transaction.
    # The signature history is paged backwards (bounded) so an old
    # mint's creation is reached even when the newest window does
    # not contain it.
    try:
        tx = _oldest_creation_tx(indexer, mint, page_size=signature_limit)
    except Exception as exc:  # noqa: BLE001 — signatures down is not fatal
        return None, "none", (
            "creator lookup failed: DAS returned no creators"
            + (f" ({das_error})" if das_error else "")
            + f"; signature lookup failed: {exc}"
        )
    if tx is None:
        return None, "none", (
            "creator lookup failed: DAS returned no creators"
            + (f" ({das_error})" if das_error else "")
            + "; no creation transaction found in the fetched signature window"
        )
    creation = parse_creation_any(tx) or _parse_transaction(tx)
    if creation is None or not creation.dev_wallet:
        return None, "none", "creator lookup failed: DAS returned no creators and the oldest transaction is not a creation"
    return (
        creation.dev_wallet,
        "creation_tx",
        "creator from the oldest fetched creation transaction (window-limited)",
    )


def _is_creation_tx(tx: dict, mint: str) -> bool:
    """True when the transaction carries a pump.fun create
    instruction for this mint.

    Strict on purpose: the indexer's balance-diff fallback would
    also call a first buy a "creation" (the mint appears only in
    post balances), which would falsely mark the early-buyer
    window as having reached creation. Two shapes count:
    the parsed ``create`` instruction (Helius data), and the raw
    one (public-RPC data — the public RPC does not parse pump.fun
    instructions, so the create arrives as base58 data starting
    with the create discriminator; see
    ``src.dev_check.parse_creation_any``).
    """
    if not isinstance(tx, dict):
        return False
    message = tx.get("transaction", {}).get("message", {})
    for instr in message.get("instructions", []) or []:
        if not isinstance(instr, dict):
            continue
        if instr.get("programId") != PUMP_FUN_PROGRAM:
            continue
        parsed = instr.get("parsed") or {}
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        if isinstance(info, dict) and info.get("mint") == mint:
            return True
        accounts = instr.get("accounts") or []
        if accounts:
            first = accounts[0]
            acct = first.get("pubkey") if isinstance(first, dict) else first
            if acct == mint:
                return True
    creation = parse_creation_any(tx)
    return creation is not None and creation.mint == mint


# ======================================================================
# Early buyers (token-balance diffs on the mint's signatures)
# ======================================================================
def fetch_early_buyers(
    indexer, mint: str, limit: int = DEFAULT_BUYER_SIGNATURES
) -> tuple[list[dict], bool, str | None]:
    """Early buyers of a mint from its signature history.

    Returns ``(buyers, reached_creation, note)``. Buyers are
    aggregated per wallet from post-minus-pre token balances
    (``uiAmount``), oldest first, sorted by first buy time.
    ``reached_creation`` is True when the fetched window reached
    the mint's creation transaction (or the signature list was
    exhausted) — False means the window may start after launch,
    so "early" is only early within the window. A failed
    signature listing returns ([], False, reason) instead of
    raising; a single failed transaction fetch is skipped.
    """
    try:
        sigs = indexer.get_signatures(mint, limit=limit)
    except Exception as exc:  # noqa: BLE001 — signatures down is not fatal
        return [], False, f"early-buyer lookup failed: {exc}"
    if not sigs:
        return [], False, "no signatures found for this mint"
    reached_creation = len(sigs) < limit
    buyers: dict[str, dict] = {}
    # Oldest first: signatures come newest-first.
    for entry in reversed(sigs):
        sig = entry.get("signature") if isinstance(entry, dict) else None
        if not sig:
            continue
        try:
            tx = indexer.get_transaction(sig)
        except Exception:  # noqa: BLE001 — one failed fetch is skipped
            continue
        if not tx:
            continue
        creation = _is_creation_tx(tx, mint)
        if creation:
            reached_creation = True
        meta = tx.get("meta", {}) or {}
        pre = {}
        for bal in meta.get("preTokenBalances", []) or []:
            if bal.get("mint") == mint and bal.get("owner"):
                pre[bal["owner"]] = (bal.get("uiTokenAmount") or {}).get("uiAmount") or 0
        block_time = tx.get("blockTime")
        for bal in meta.get("postTokenBalances", []) or []:
            if bal.get("mint") != mint:
                continue
            owner = bal.get("owner")
            if not owner:
                continue
            post = (bal.get("uiTokenAmount") or {}).get("uiAmount") or 0
            bought = post - pre.get(owner, 0)
            if bought <= 0:
                continue
            rec = buyers.setdefault(
                owner,
                {
                    "wallet": owner,
                    "amount": 0.0,
                    "bought_at": None,
                    "first_signature": sig,
                    "tx_count": 0,
                },
            )
            rec["amount"] += bought
            rec["tx_count"] += 1
            if block_time is not None and (
                rec["bought_at"] is None or block_time < rec["bought_at"]
            ):
                rec["bought_at"] = block_time
                rec["first_signature"] = sig
    ordered = sorted(
        buyers.values(), key=lambda b: (b["bought_at"] is None, b["bought_at"] or 0)
    )
    return ordered, reached_creation, None


# ======================================================================
# Side wallets (funding transfers + early buyers -> links + clusters)
# ======================================================================
def _block_time(indexer, signature: str | None, cache: dict) -> int | None:
    """Epoch block time for a signature, cached; None on any failure."""
    if not signature or signature in cache:
        return cache.get(signature) if signature else None
    try:
        tx = indexer.get_transaction(signature)
    except Exception:  # noqa: BLE001 — one failed fetch is not fatal
        tx = None
    block_time = (tx or {}).get("blockTime") if isinstance(tx, dict) else None
    cache[signature] = block_time if isinstance(block_time, (int, float)) else None
    return cache[signature]


def build_side_wallets(
    indexer,
    dev_wallet: str | None,
    mint: str,
    early_buyers: list[dict],
    launch_at: int | None,
    funding_limit: int = DEFAULT_FUNDING_LIMIT,
    max_funding_wallets: int = MAX_FUNDING_WALLETS,
) -> tuple[list[dict], list[FundingEvent], list[str]]:
    """Side-wallet links + stealth-cluster funding events.

    Funding transfers are fetched for the dev (when known) and the
    largest early buyers (capped at ``MAX_FUNDING_WALLETS``).
    Links, each with its evidence:
    - ``funded_dev`` — wallet sent SOL to the dev.
    - ``funded_by_dev`` — dev sent SOL to the wallet.
    - ``early_buyer`` — wallet bought the mint early.
    - ``funder_is_early_buyer`` — a wallet that funded the dev
      also bought the mint early (the POOR pattern).
    - ``dev_funded_early_buyer`` — the dev funded a wallet that
      bought the mint early.
    - ``shared_funder_with_dev`` — buyer and dev share a funder.

    The funding scan is bounded to the most recent signatures per
    wallet — old funding is out of reach without full-history
    paging, and the returned notes say so. A failed funding
    lookup returns empty links/events plus a note, never a guess.
    """
    notes: list[str] = []
    wallets = [w for w in [dev_wallet] if w]
    top_buyers = sorted(
        early_buyers, key=lambda b: b.get("amount") or 0, reverse=True
    )[:max_funding_wallets]
    wallets += [b["wallet"] for b in top_buyers]
    if not wallets:
        return [], [], ["no wallets to check funding for"]
    try:
        transfers = indexer.fetch_funding_transfers(
            wallets, limit_per_wallet=funding_limit
        )
    except Exception as exc:  # noqa: BLE001 — funding down is not fatal
        return [], [], [f"funding lookup failed: {exc}"]
    notes.append(
        "Funding scan covers the most recent "
        f"{funding_limit} signature(s) per wallet; funding from "
        "long ago can be missed."
    )
    funders_of: dict[str, list[tuple]] = {}
    funded_by: dict[str, list[str]] = {}
    for t in transfers:
        src, dst = t.get("from"), t.get("to")
        if not src or not dst or src == dst:
            continue
        funders_of.setdefault(dst, []).append(
            (src, t.get("amount_sol"), t.get("signature"))
        )
        funded_by.setdefault(src, []).append(dst)

    links: list[dict] = []
    if dev_wallet:
        for funder, amount, sig in funders_of.get(dev_wallet, []):
            links.append(
                {
                    "wallet": funder,
                    "link": "funded_dev",
                    "amount_sol": amount,
                    "signature": sig,
                    "evidence": f"https://explorer.helius.xyz/tx/{sig}"
                    if sig
                    else None,
                }
            )
        for wallet in funded_by.get(dev_wallet, []):
            amount = next(
                (a for (f, a, s) in funders_of.get(wallet, []) if f == dev_wallet),
                None,
            )
            links.append(
                {
                    "wallet": wallet,
                    "link": "funded_by_dev",
                    "amount_sol": amount,
                    "signature": None,
                    "evidence": f"https://solscan.io/account/{wallet}",
                }
            )
    dev_funders = {f for f, _, _ in funders_of.get(dev_wallet, [])} if dev_wallet else set()
    dev_funded = set(funded_by.get(dev_wallet, [])) if dev_wallet else set()
    for buyer in early_buyers:
        wallet = buyer["wallet"]
        links.append(
            {
                "wallet": wallet,
                "link": "early_buyer",
                "amount": buyer.get("amount"),
                "bought_at": _epoch_to_iso(buyer.get("bought_at")),
                "signature": buyer.get("first_signature"),
                "evidence": f"https://explorer.helius.xyz/tx/{buyer.get('first_signature')}"
                if buyer.get("first_signature")
                else None,
            }
        )
        if wallet in dev_funders:
            links.append(
                {
                    "wallet": wallet,
                    "link": "funder_is_early_buyer",
                    "amount_sol": next(
                        (a for (f, a, s) in funders_of.get(dev_wallet, []) if f == wallet),
                        None,
                    ),
                    "amount": buyer.get("amount"),
                    "bought_at": _epoch_to_iso(buyer.get("bought_at")),
                    "signature": buyer.get("first_signature"),
                    "evidence": "Funded the dev AND bought this mint early — "
                    "the side-wallet pattern.",
                }
            )
        if wallet in dev_funded:
            links.append(
                {
                    "wallet": wallet,
                    "link": "dev_funded_early_buyer",
                    "amount": buyer.get("amount"),
                    "bought_at": _epoch_to_iso(buyer.get("bought_at")),
                    "signature": buyer.get("first_signature"),
                    "evidence": "The dev funded this wallet, and it bought "
                    "this mint early.",
                }
            )
        shared = dev_funders & {
            f for f, _, _ in funders_of.get(wallet, [])
        }
        for funder in sorted(shared):
            links.append(
                {
                    "wallet": wallet,
                    "link": "shared_funder_with_dev",
                    "funder": funder,
                    "amount": buyer.get("amount"),
                    "bought_at": _epoch_to_iso(buyer.get("bought_at")),
                    "signature": buyer.get("first_signature"),
                    "evidence": f"Buyer and dev share funder {funder}.",
                }
            )

    # FundingEvents for the stealth-cluster detector: one per early
    # buyer, funder = the wallet that funded them most (if any).
    blocktime_cache: dict = {}
    events: list[FundingEvent] = []
    for buyer in early_buyers:
        wallet = buyer["wallet"]
        flist = funders_of.get(wallet, [])
        if not flist:
            events.append(
                FundingEvent(
                    wallet=wallet,
                    bought_at=buyer.get("bought_at"),
                    launch_at=launch_at,
                )
            )
            continue
        funder, amount, sig = max(flist, key=lambda x: x[1] or 0)
        events.append(
            FundingEvent(
                wallet=wallet,
                funder=funder,
                amount_sol=amount,
                funded_at=_block_time(indexer, sig, blocktime_cache),
                bought_at=buyer.get("bought_at"),
                launch_at=launch_at,
            )
        )
    return links, events, notes


# ======================================================================
# Report
# ======================================================================
@dataclass
class DeepCheckReport:
    """One full card for a mint: coin, dev, side wallets, verdict."""

    mint: str
    coin: dict | None
    dev_wallet: str | None
    dev_method: str  # "das" | "metadata" | "creation_tx" | "none"
    dev_note: str | None
    dev_history: DevHistory | None
    registry_check: dict | None
    coin_note: str | None = None
    side_links: list[dict] = field(default_factory=list)
    clusters: list[dict] = field(default_factory=list)
    wash: dict | None = None
    verdict: str = "unknown"
    pattern_signals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return self.verdict == "flagged"

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "verdict": self.verdict,
            "flagged": self.flagged,
            "pattern_signals": self.pattern_signals,
            "coin": self.coin,
            "coin_note": self.coin_note,
            "dev_wallet": self.dev_wallet,
            "dev_method": self.dev_method,
            "dev_note": self.dev_note,
            "dev_history": self.dev_history.to_dict() if self.dev_history else None,
            "registry_check": self.registry_check,
            "side_links": self.side_links,
            "clusters": self.clusters,
            "wash": self.wash,
            "notes": self.notes,
        }

    def to_text(self) -> str:
        lines = [f"Deep check: {self.mint}", ""]
        # --- Coin ---------------------------------------------------
        lines.append("Coin")
        if self.coin is None:
            lines.append("  No DexScreener pair found for this mint.")
        else:
            c = self.coin
            name = f"{c.get('name') or ''} ({c.get('symbol') or '?'})".strip()
            lines.append(f"  {name}")
            if c.get("source") == "public_rpc":
                lines.append(
                    "  source: public Solana RPC (no DexScreener pair — "
                    "no price, market cap or volume on-chain)"
                )
            lines.append(
                f"  price: ${c['price_usd']:,.8f}"
                if c.get("price_usd") is not None
                else "  price: n/a"
            )
            lines.append(
                f"  market cap: ${c['market_cap_usd']:,.0f}"
                if c.get("market_cap_usd") is not None
                else "  market cap: n/a"
            )
            lines.append(
                f"  liquidity: ${c['liquidity_usd']:,.0f}"
                if c.get("liquidity_usd") is not None
                else "  liquidity: n/a"
            )
            lines.append(
                f"  24h volume: ${c['volume_24h_usd']:,.0f}"
                if c.get("volume_24h_usd") is not None
                else "  24h volume: n/a"
            )
            lines.append(
                f"  24h change: {c['change_24h_pct']:+.1f}%"
                if c.get("change_24h_pct") is not None
                else "  24h change: n/a"
            )
            if c.get("supply") is not None:
                lines.append(f"  supply: {c['supply']:,.0f}")
            if c.get("mint_authority"):
                lines.append(f"  mint authority: {c['mint_authority']}")
            if c.get("freeze_authority"):
                lines.append(f"  freeze authority: {c['freeze_authority']}")
            if c.get("holder_count") is not None:
                lines.append(f"  holders: {c['holder_count']}")
            if c.get("top1_share") is not None:
                lines.append(f"  top holder: {c['top1_share']:.1%} of supply")
            if c.get("top10_share") is not None:
                lines.append(f"  top 10 holders: {c['top10_share']:.1%} of supply")
            buys, sells = c.get("txns_24h_buys"), c.get("txns_24h_sells")
            if buys is not None or sells is not None:
                lines.append(f"  txns 24h: {buys or 0} buys / {sells or 0} sells")
            lines.append(f"  watch: {c.get('watch_label', '?')}")
            lines.append(f"  outcome: {c.get('outcome', 'insufficient_data')}")
            if c.get("pair_created_at"):
                lines.append(f"  pair created: {c['pair_created_at']}")
            lines.append(f"  {c.get('dexscreener_url', '')}")
            if self.coin_note:
                lines.append(f"  Note: {self.coin_note}")
        lines.append("")
        # --- Dev ----------------------------------------------------
        lines.append("Dev")
        if self.dev_wallet is None:
            lines.append(f"  Dev unknown — {self.dev_note or 'no creator found'}.")
        else:
            lines.append(f"  Dev wallet: {self.dev_wallet}")
            lines.append(f"  Resolved via: {self.dev_method}")
            if self.dev_note:
                lines.append(f"  Note: {self.dev_note}")
            if self.dev_history is not None:
                h = self.dev_history
                lines.append(f"  Verdict: {h.verdict.upper()}")
                lines.append(
                    f"  Mints checked: {h.checked} "
                    f"({h.rugged_mint_count} rugged with evidence)"
                )
                if h.first_seen or h.last_seen:
                    lines.append(
                        f"  First seen: {h.first_seen or '-'}  "
                        f"Last seen: {h.last_seen or '-'}"
                    )
                for m in h.mints:
                    lines.append(f"    [{m.outcome}] {m.mint}")
            if self.registry_check is not None:
                lines.append(
                    f"  Registry: risk {self.registry_check.get('risk_level')}, "
                    f"flagged: {'YES' if self.registry_check.get('flagged') else 'no'}, "
                    f"rugged mints: {self.registry_check.get('rugged_mint_count')}"
                )
        lines.append("")
        # --- Side wallets -------------------------------------------
        lines.append("Side wallets")
        if not self.side_links:
            lines.append("  No side-wallet links found.")
        else:
            for link in self.side_links:
                wallet = link["wallet"]
                kind = link["link"]
                detail = ""
                if link.get("amount_sol") is not None:
                    detail += f" {link['amount_sol']} SOL"
                if link.get("amount") is not None:
                    detail += f" bought {link['amount']:,.0f}"
                if link.get("bought_at"):
                    detail += f" at {link['bought_at']}"
                if link.get("funder"):
                    detail += f" via funder {link['funder']}"
                lines.append(f"  - {wallet} [{kind}]{detail}")
                if link.get("evidence"):
                    lines.append(f"      {link['evidence']}")
        lines.append("")
        # --- Clusters + wash -----------------------------------------
        lines.append("Clusters")
        if not self.clusters:
            lines.append("  No stealth clusters found in the funding data.")
        else:
            for cluster in self.clusters:
                lines.append(
                    f"  - Cluster ({len(cluster['members'])} wallets, "
                    f"signals: {', '.join(cluster['signals'])}):"
                )
                for ev in cluster["evidence"]:
                    lines.append(f"      {ev}")
        lines.append("")
        lines.append("Wash")
        if self.wash is None or self.wash.get("insufficient_data"):
            lines.append("  Wash score: insufficient data — no holder data supplied.")
        else:
            lines.append(
                f"  Wash score: {self.wash['score']:.0f}/100 ({self.wash['level']})"
            )
            for reason in self.wash.get("reasons", []):
                lines.append(f"  - {reason}")
        lines.append("")
        # --- Verdict -------------------------------------------------
        lines.append(f"Verdict: {self.verdict.upper()}")
        if self.pattern_signals:
            lines.append("Pattern signals: " + ", ".join(self.pattern_signals))
        for note in self.notes:
            lines.append(f"Note: {note}")
        lines.append("")
        lines.append(
            "This is a risk signal, not a verdict. "
            "A pattern alone never flags a dev — flagging needs "
            ">= 2 rugged mints with evidence. Verify every "
            "evidence link yourself before acting."
        )
        return "\n".join(lines)


# ======================================================================
# Orchestrator
# ======================================================================
class DeepChecker:
    """Run all five deep-check halves for one mint.

    ``http_client`` is an injectable ``httpx.Client`` for the
    DexScreener half; ``indexer`` is a ``HeliusIndexer`` (or a
    stub with the same surface: ``enabled``, ``_rpc``,
    ``get_signatures``, ``get_transaction``,
    ``fetch_funding_transfers``); ``registry_data`` is the loaded
    registry dict (``{}`` = none); ``history_builder`` overrides
    the default ``DevHistoryBuilder`` (tests pass a stub).
    ``ml_model`` is an optional loaded ``RugRiskModel`` — advisory
    only, it never changes a verdict. ``rpc_client`` is an
    injectable ``PublicRpcClient`` for the keyless path (no
    Helius key / no DexScreener pair); when omitted a real one
    is created lazily — tests pass a stubbed one so no live
    call is ever made.
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        indexer: HeliusIndexer | None = None,
        registry_data: dict | None = None,
        history_builder=None,
        ml_model=None,
        rpc_client: PublicRpcClient | None = None,
    ):
        self._http_client = http_client
        self.indexer = indexer if indexer is not None else HeliusIndexer()
        self.registry_data = registry_data or {}
        self.history_builder = history_builder
        self.ml_model = ml_model
        self._rpc_client = rpc_client

    def _rpc(self) -> PublicRpcClient:
        """The public-RPC client for the keyless path (one per checker)."""
        if self._rpc_client is None:
            self._rpc_client = PublicRpcClient()
        return self._rpc_client

    def check(
        self,
        mint: str,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        buyer_limit: int = DEFAULT_BUYER_SIGNATURES,
        funding_limit: int = DEFAULT_FUNDING_LIMIT,
        holders: int | None = None,
        top10: list[float] | None = None,
    ) -> DeepCheckReport:
        """Run the full deep check for one mint.

        Raises ValueError for an invalid mint address and
        RuntimeError on a DexScreener network failure. Every
        Helius half degrades to the keyless public-RPC path
        instead of failing the whole check — a missing key only
        costs the full-history depth, and a missing DexScreener
        pair only costs the market fields.
        """
        if not is_valid_solana_address(mint):
            raise ValueError(f"'{mint}' is not a valid Solana address.")
        notes: list[str] = []

        # 1. Coin half (DexScreener, no key) — public-RPC fallback
        # when DexScreener has no pair for the mint.
        coin = CoinFetcher(self._http_client).fetch(mint)
        coin_note = None
        if coin is None:
            notes.append("DexScreener has no pair for this mint.")
            try:
                coin, rpc_note, _metadata = fetch_rpc_coin(self._rpc(), mint)
            except Exception as exc:  # noqa: BLE001 — RPC down is not fatal
                coin = None
                notes.append(f"Public-RPC coin fallback failed: {exc}")
            if coin is not None:
                coin_note = (
                    "No DexScreener pair — coin data from the public "
                    "Solana RPC. " + (rpc_note or "")
                ).strip()
                notes.append(coin_note)
            else:
                notes.append(
                    "Public Solana RPC has no data for this mint either."
                )

        # 2-4. Dev halves. Keyed (Helius) when a key is set;
        # keyless (public RPC) otherwise — same code path, the
        # keyless indexer answers getAsset from the Metaplex
        # metadata account and caps the history windows.
        keyless = not self.indexer.enabled
        indexer = PublicRpcIndexer(self._rpc()) if keyless else self.indexer
        if keyless:
            notes.append(
                "no Helius key set — dev half is keyless (public "
                "Solana RPC): creator from the Metaplex metadata "
                "account, dev history from the last "
                f"{KEYLESS_SIGNATURE_LIMIT} signatures; older history "
                "needs a Helius key."
            )
        dev_wallet: str | None = None
        dev_method = "none"
        dev_note: str | None = None
        history: DevHistory | None = None
        registry_check: dict | None = None
        side_links: list[dict] = []
        clusters: list[dict] = []
        early_buyers: list[dict] = []
        dev_wallet, dev_method, dev_note = resolve_creator(
            indexer,
            mint,
            signature_limit=KEYLESS_SIGNATURE_LIMIT if keyless else 50,
        )
        if keyless and dev_method == "das":
            # The keyless indexer answers getAsset from the
            # Metaplex metadata account — say so, don't call it DAS.
            dev_method = "metadata"
        if dev_wallet is None:
            notes.append(f"Creator could not be resolved: {dev_note}")
        else:
            registry_check = check_wallet(
                self.registry_data, dev_wallet, mint=mint
            )
            if self.history_builder is not None:
                builder = self.history_builder
                effective_limit = history_limit
            elif keyless:
                builder = DevHistoryBuilder(indexer, self.registry_data)
                effective_limit = min(history_limit, KEYLESS_SIGNATURE_LIMIT)
            else:
                builder = DevHistoryBuilder(self.indexer, self.registry_data)
                effective_limit = history_limit
            try:
                history = builder.build(
                    dev_wallet, limit=effective_limit, ml_model=self.ml_model
                )
            except Exception as exc:  # noqa: BLE001 — history down is not fatal
                history = None
                notes.append(f"Dev history failed: {exc}")
        # Early buyers are about the mint, not the dev — fetch
        # them on either path. Keyless, the window is capped:
        # every transaction fetch is a paced public-RPC call.
        early_buyers, reached, buyer_note = fetch_early_buyers(
            indexer,
            mint,
            limit=min(buyer_limit, KEYLESS_BUYER_LIMIT) if keyless else buyer_limit,
        )
        if buyer_note:
            notes.append(buyer_note)
        elif not reached:
            notes.append(
                "Early-buyer window did not reach the mint's creation — "
                "'early' means early within the fetched window."
            )
        launch_at = None
        if coin and coin.get("pair_created_at"):
            try:
                launch_at = int(
                    datetime.fromisoformat(
                        coin["pair_created_at"]
                    ).timestamp()
                )
            except ValueError:
                launch_at = None
        links, events, side_notes = build_side_wallets(
            indexer,
            dev_wallet,
            mint,
            early_buyers,
            launch_at,
            funding_limit=funding_limit,
            max_funding_wallets=(
                KEYLESS_MAX_FUNDING_WALLETS if keyless else MAX_FUNDING_WALLETS
            ),
        )
        side_links = links
        notes.extend(side_notes)
        if keyless:
            notes.append(
                "Keyless funding scan: dev + top "
                f"{KEYLESS_MAX_FUNDING_WALLETS} early buyers, "
                f"{funding_limit} signatures each; earlier history "
                "needs a Helius key."
            )
        if events:
            found = StealthClusterDetector().detect(events)
            clusters = [
                {
                    "members": c.members,
                    "funders": c.funders,
                    "signals": c.signals,
                    "evidence": c.evidence,
                }
                for c in found
            ]

        # 5. Wash — only when holder data is supplied, or when the
        # coin card came from the public RPC (it carries holder
        # data DexScreener does not publish).
        wash = None
        if holders is not None or top10:
            result = wash_score(
                holders,
                top10,
                None,
                coin.get("volume_24h_usd") if coin else None,
                coin.get("market_cap_usd") if coin else None,
            )
            wash = {
                "score": result.score,
                "level": result.level,
                "reasons": result.reasons,
                "insufficient_data": result.insufficient_data,
            }
        elif (
            coin is not None
            and coin.get("source") == "public_rpc"
            and (coin.get("top10_shares") or coin.get("top1_share") is not None)
        ):
            result = wash_score(
                coin.get("holder_count"),
                coin.get("top10_shares"),
                coin.get("top1_share"),
                None,
                None,
            )
            wash = {
                "score": result.score,
                "level": result.level,
                "reasons": result.reasons,
                "insufficient_data": result.insufficient_data,
            }

        # Verdict: the flagging rule is unchanged — only the dev
        # history (>= 2 rugged mints + evidence) can flag. A
        # suspicious pattern is "high-risk pattern", never flagged.
        pattern_signals: list[str] = []
        for link in side_links:
            if link["link"] in (
                "funder_is_early_buyer",
                "dev_funded_early_buyer",
                "shared_funder_with_dev",
            ):
                pattern_signals.append(link["link"])
        if clusters:
            pattern_signals.append(
                f"stealth_cluster:{sum(len(c['members']) for c in clusters)} wallets"
            )
        if wash and wash.get("level") == "high":
            pattern_signals.append("wash_high")
        pattern_signals = sorted(set(pattern_signals))

        if history is not None and history.verdict == "flagged":
            verdict = "flagged"
        elif history is not None and history.verdict == "under_review":
            verdict = "under_review"
        elif pattern_signals:
            verdict = "high-risk pattern"
        elif history is not None and history.verdict == "clean":
            verdict = "clean"
        elif coin is None:
            verdict = "insufficient_data"
        elif dev_wallet is None:
            verdict = "unknown"
        elif history is not None and history.verdict == "insufficient_data":
            verdict = "insufficient_data"
        else:
            verdict = "unknown"

        return DeepCheckReport(
            mint=mint,
            coin=coin,
            dev_wallet=dev_wallet,
            dev_method=dev_method,
            dev_note=dev_note,
            dev_history=history,
            registry_check=registry_check,
            coin_note=coin_note,
            side_links=side_links,
            clusters=clusters,
            wash=wash,
            verdict=verdict,
            pattern_signals=pattern_signals,
            notes=notes,
        )


# ======================================================================
# CLI
# ======================================================================
def _parse_top10(text: str | None) -> list[float]:
    if not text:
        return []
    try:
        return [float(p) for p in text.split(",") if p.strip()]
    except ValueError:
        raise ValueError(f"--top10 must be comma-separated numbers, got: {text!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.deep_check",
        description="Deep check: one command, one full card for a mint — coin, dev, side wallets, clusters.",
    )
    parser.add_argument("--mint", required=True, help="Token mint address")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    parser.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Registry JSON path (default: data/registry.json if it exists)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_HISTORY_LIMIT,
        help=f"Max dev-history mints to check (default: {DEFAULT_HISTORY_LIMIT})",
    )
    parser.add_argument(
        "--buyers",
        type=int,
        default=DEFAULT_BUYER_SIGNATURES,
        help=f"Max mint signatures to scan for early buyers (default: {DEFAULT_BUYER_SIGNATURES})",
    )
    parser.add_argument(
        "--holders",
        type=int,
        default=None,
        help="Holder count for the wash score (DexScreener does not publish it)",
    )
    parser.add_argument(
        "--top10",
        type=str,
        default=None,
        help="Comma-separated top-10 holder shares (0-1 or %) for the wash score",
    )
    parser.add_argument(
        "--ml-model",
        type=str,
        default=None,
        help="Trained ML model JSON (src.ml_model) — advisory score on the dev history; never changes the verdict",
    )
    args = parser.parse_args(argv)

    if not is_valid_solana_address(args.mint):
        print(f"ERROR: '{args.mint}' is not a valid Solana address.", file=sys.stderr)
        return EXIT_BLOCKED
    if args.limit < 1 or args.buyers < 1:
        print("ERROR: --limit and --buyers must be >= 1.", file=sys.stderr)
        return EXIT_BLOCKED
    try:
        top10 = _parse_top10(args.top10)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    registry_data: dict = {}
    registry_path = args.registry or str(DEFAULT_REGISTRY_PATH)
    if Path(registry_path).exists():
        try:
            registry_data = load_registry(registry_path)
        except (OSError, ValueError) as exc:
            print(
                f"ERROR: cannot load registry {registry_path}: {exc}",
                file=sys.stderr,
            )
            return EXIT_BLOCKED

    ml_model = None
    if args.ml_model:
        try:
            from .ml_model import load_model

            ml_model = load_model(args.ml_model)
        except ValueError as exc:
            print(
                f"ERROR: cannot load ML model {args.ml_model}: {exc}",
                file=sys.stderr,
            )
            return EXIT_BLOCKED

    checker = DeepChecker(registry_data=registry_data, ml_model=ml_model)
    try:
        report = checker.check(
            args.mint,
            history_limit=args.limit,
            buyer_limit=args.buyers,
            holders=args.holders,
            top10=top10,
        )
    except RuntimeError as exc:
        print(f"ERROR: deep check failed: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_text())
    if report.coin is None and report.dev_wallet is None:
        # Neither DexScreener nor the public RPC had anything
        # for this mint — say so with exit 2, never a guessed card.
        return EXIT_BLOCKED
    return EXIT_FLAGGED if report.flagged else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
