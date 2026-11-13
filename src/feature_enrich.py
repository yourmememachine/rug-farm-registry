"""ML feature enrichment for the rug-risk model — v2.15.

The v2.13 real-data model trained (12 rugged / 27 survived,
held-out 1.00) but its weights show it learned only "low
liquidity -> rugged-like" (liquidity weight -2.605, collapse
+0.036, the other 8 features 0.000): on DexScreener data only 2
of the model's 10 features exist — collapse_pct and
liquidity_usd (143-145 of 206 records). The 8 always-missing
features are dev_sold_pct, lp_removed, time_to_collapse_minutes,
wash_score, top1_share, top10_share, holder_count, cluster_size.
This module fills them from real on-chain data so the model
stops being a one-feature model.

No Helius key is available locally (HELIUS_API_KEY exists only
as a GitHub secret for CI), so enrichment runs on the PUBLIC
Solana RPC (api.mainnet-beta.solana.com, fallback
solana-rpc.publicnode.com) and DexScreener market facts already
on the record. What the public RPC gives, and what it does not:

- getTokenLargestAccounts + getTokenSupply -> top1 / top10
  share. getTokenLargestAccounts is a heavy method and the
  public endpoints rate-limit it hard from this environment
  (persistent 429) — the module retries with backoff, then
  falls back to getProgramAccounts on the mint's token program
  (classic Token program AND Token-2022; a mint's program comes
  from getAccountInfo, because a Token-2022 mint has no classic
  token accounts at all).
- getProgramAccounts (owner + amount slice) -> holder count
  (distinct owners) and top shares when largest-accounts is
  throttled. Token-2022 mints need the mint's program from
  getAccountInfo (a Token-2022 mint has no classic token
  accounts at all), and the query must NOT carry a dataSize
  165 filter — Token-2022 accounts with extensions are larger,
  so the filter misses almost all of them (live probe: 60 of
  16,676) and the dataSize+dataSlice combination is rejected
  with "WrongSize" on Token-2022.
- getSignaturesForAddress + getTransaction (jsonParsed,
  maxSupportedTransactionVersion 1) -> the pump.fun creation
  transaction (strict create-instruction check, same as
  src/chain_index.parse_creation), the dev's initial balance,
  the mint's last on-chain activity, and early-buyer funding
  transfers for the stealth-cluster detector.
- getTokenAccountsByOwner(dev, mint) -> the dev's CURRENT
  balance, so dev_sold_pct = (initial - current) / initial.
- lp_removed is inferred from the stored DexScreener liquidity
  (liquidity < $1,000, the same LP_DRAINED_USD the indexer
  uses) — a real observable, documented as inferred, not an
  on-chain LP read.
- time_to_collapse_minutes is a documented proxy: for rugged
  records, creation -> last on-chain activity (when trading
  died); for survived records, age at enrichment (no collapse
  observed). The basis is stored on the record.

It is slow by design: the public endpoints rate-limit by IP
(default 2.5 s between calls, 429 retries honouring
Retry-After, endpoint fallback, and a per-record call budget).
A run covers the labelled records (39) first, writes a
checkpoint file after every record, and resumes from it — a
killed run loses nothing. A method that 429s on every endpoint
after full retries is marked dead for the rest of the run and
skipped, so one throttled method cannot burn the whole budget.

Never invent a feature value: if a feature cannot be fetched
for a record, it stays missing (the model imputes it with the
training mean) and the coverage report counts it. The dataset
is backed up to /tmp before the first write, old fields are
untouched, and the labelling rule version stays real-v2.

    python -m src.feature_enrich [--dataset data/ml_real_dataset.json]
                                 [--checkpoint data/feature_enrich_checkpoint.json]
                                 [--limit N] [--max-calls-per-record N]
                                 [--rpc-delay S] [--max-retries N]
                                 [--retry-failed] [--json]

Exit 0 = done (per-record failures are recorded, not fatal),
2 = invalid args / unreadable dataset / RPC unreachable
(the getSlot probe fails on every endpoint after retries).
Stdlib + httpx only, no wallet, no funds moved.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.advanced import (
    FundingEvent,
    StealthClusterDetector,
    wash_score,
)
from src.chain_index import parse_creation
from src.public_rpc import PublicRpcClient, RpcError
from src.real_data import (
    DEFAULT_OUT,
    save_dataset,
    save_summary,
    summarize,
    summary_path,
)
from src.registry import is_valid_solana_address

EXIT_OK = 0
EXIT_INVALID = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "ml_real_dataset.json"
DEFAULT_CHECKPOINT_PATH = PROJECT_ROOT / "data" / "feature_enrich_checkpoint.json"
BACKUP_PATH = Path("/tmp/ml_real_dataset_pre_enrich_backup.json")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Verified live 2026-11-13 via getAccountInfo (executable, BPF-loader
# owned). Do NOT "correct" this to the commonly misremembered tail
# ...EPF3SfwV1CZ5LwU4MsS — that address holds NO account.
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WRAPPED_SOL = "So11111111111111111111111111111111111111112"

# Same LP-drained inference as src/indexer.LP_DRAINED_USD.
LP_DRAINED_USD = 1_000.0

DEFAULT_RPC_DELAY_SECONDS = 2.5
DEFAULT_MAX_RETRIES = 4
DEFAULT_MAX_CALLS_PER_RECORD = 40
DEFAULT_SIG_PAGES = 3
DEFAULT_SIG_PAGE_SIZE = 1_000
DEFAULT_CREATION_TX_CANDIDATES = 5
DEFAULT_EARLY_BUYERS = 5
DEFAULT_FUNDER_SIG_LIMIT = 10
DEFAULT_FUNDER_TX_LIMIT = 5

FEATURE_NAMES = (
    "collapse_pct",
    "dev_sold_pct",
    "lp_removed",
    "time_to_collapse_minutes",
    "wash_score",
    "top1_share",
    "top10_share",
    "holder_count",
    "cluster_size",
    "liquidity_usd",
)
ENRICHABLE_FEATURES = (
    "dev_sold_pct",
    "lp_removed",
    "time_to_collapse_minutes",
    "wash_score",
    "top1_share",
    "top10_share",
    "holder_count",
    "cluster_size",
)


# ======================================================================
# Raw RPC fetch helpers (all return parsed data or None / raise RpcError)
# ======================================================================
def get_mint_info(rpc: PublicRpcClient, mint: str) -> dict | None:
    """getAccountInfo (jsonParsed): owner program + decimals.

    Returns {"owner": program, "decimals": int} or None when the
    account is missing / unparsable — never guessed.
    """
    result = rpc.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    owner = value.get("owner")
    data = value.get("data") or {}
    info = (data.get("parsed") or {}).get("info") or {}
    decimals = info.get("decimals")
    return {
        "owner": owner if isinstance(owner, str) else None,
        "decimals": decimals if isinstance(decimals, int) else None,
    }


def get_token_supply(rpc: PublicRpcClient, mint: str) -> float | None:
    """getTokenSupply -> total supply in UI units, or None."""
    result = rpc.rpc("getTokenSupply", [mint])
    if not isinstance(result, dict):
        return None
    value = result.get("value") or {}
    amount = value.get("uiAmount")
    return float(amount) if isinstance(amount, (int, float)) else None


def get_largest_accounts(rpc: PublicRpcClient, mint: str) -> list[dict] | None:
    """getTokenLargestAccounts -> [{address, uiAmount}] or None."""
    result = rpc.rpc("getTokenLargestAccounts", [mint])
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, list):
        return None
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        amount = item.get("uiAmount")
        if isinstance(amount, (int, float)):
            out.append({"address": item.get("address"), "amount": float(amount)})
    return out or None


def _decode_owner_amount_slice(b64: str) -> tuple[bytes, int] | None:
    """A 40-byte owner+amount slice: owner (32 bytes) + amount (u64 LE)."""
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if len(raw) != 40:
        return None
    return raw[:32], int.from_bytes(raw[32:40], "little")


def get_token_accounts_for_mint(
    rpc: PublicRpcClient, program: str, mint: str
) -> list[dict] | None:
    """getProgramAccounts for one mint: [{owner, amount_raw}].

    One contiguous dataSlice (offset 32, length 40) returns the
    owner (32 bytes) + amount (u64 LE) per token account, so the
    response stays small. NO dataSize filter: Token-2022 accounts
    with extensions are larger than 165 bytes, so a dataSize 165
    filter misses almost all of them (live probe 2026-11-13: 60
    of 16,676 accounts for one Token-2022 mint), and the
    dataSize+dataSlice combination is rejected by the public
    node with "WrongSize" on Token-2022. Returns None when the
    program rejects the call — the caller then leaves holder
    features missing.
    """
    result = rpc.rpc(
        "getProgramAccounts",
        [
            program,
            {
                "filters": [{"memcmp": {"offset": 0, "bytes": mint}}],
                "dataSlice": {"offset": 32, "length": 40},
                "encoding": "base64",
            },
        ],
    )
    if not isinstance(result, list):
        return None
    accounts: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        account = item.get("account") or {}
        data = account.get("data")
        if not isinstance(data, list) or not data:
            continue
        decoded = _decode_owner_amount_slice(data[0])
        if decoded is None:
            continue
        owner, amount = decoded
        accounts.append({"owner": owner, "amount_raw": amount})
    return accounts


def get_owner_token_balance(
    rpc: PublicRpcClient, owner: str, mint: str
) -> float | None:
    """getTokenAccountsByOwner -> owner's summed UI balance for a mint.

    An empty account list is a real 0.0 (the owner holds none) —
    not missing. None only when the call itself fails.
    """
    result = rpc.rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, list):
        return None
    total = 0.0
    for item in value:
        if not isinstance(item, dict):
            continue
        account = item.get("account") or {}
        data = account.get("data") or {}
        info = (data.get("parsed") or {}).get("info") or {}
        amount = (info.get("tokenAmount") or {}).get("uiAmount")
        if isinstance(amount, (int, float)):
            total += float(amount)
    return total


def get_signatures(
    rpc: PublicRpcClient,
    address: str,
    limit: int = DEFAULT_SIG_PAGE_SIZE,
    before: str | None = None,
) -> list[dict]:
    """getSignaturesForAddress, newest first. [] when none."""
    opts: dict = {"limit": limit}
    if before:
        opts["before"] = before
    result = rpc.rpc("getSignaturesForAddress", [address, opts])
    if not isinstance(result, list):
        return []
    return [e for e in result if isinstance(e, dict)]


def get_transaction(rpc: PublicRpcClient, signature: str) -> dict | None:
    """One jsonParsed transaction, or None when unavailable.

    maxSupportedTransactionVersion 1: version-0-only requests
    fail on v1 transactions with -32015 on the public node.
    """
    result = rpc.rpc(
        "getTransaction",
        [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1},
        ],
    )
    return result if isinstance(result, dict) else None

# ======================================================================
# Per-record enrichment
# ======================================================================
def _launch_time(record: dict) -> float | None:
    """Creation epoch seconds, derived from fetched_at - pair_age_days.

    The record stores pair_age_days (rounded to 2 decimals) and
    fetched_at, not the creation timestamp itself — so this is a
    derived time, accurate to ~15 minutes, and every feature
    that uses it says so in its note.
    """
    fetched_at = record.get("fetched_at")
    age_days = record.get("pair_age_days")
    if not isinstance(age_days, (int, float)):
        return None
    try:
        fetched = datetime.fromisoformat(str(fetched_at))
    except (ValueError, TypeError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return fetched.timestamp() - float(age_days) * 86_400.0


def _dev_balance_in_tx(tx: dict, dev: str, mint: str) -> float | None:
    """The dev's post-transaction balance for a mint, or None."""
    meta = tx.get("meta") or {}
    for bal in meta.get("postTokenBalances") or []:
        if not isinstance(bal, dict):
            continue
        if bal.get("mint") != mint or bal.get("owner") != dev:
            continue
        amount = (bal.get("uiTokenAmount") or {}).get("uiAmount")
        if isinstance(amount, (int, float)):
            return float(amount)
    return None


def _positive_balance_diffs(tx: dict, mint: str, exclude: set[str]) -> list[dict]:
    """Wallets that received the mint in one transaction.

    Compares pre/post token balances per (owner, mint): a
    positive diff is a buy. Returns [{"wallet", "amount"}].
    """
    meta = tx.get("meta") or {}
    pre: dict[str, float] = {}
    post: dict[str, float] = {}
    for key, target in (("preTokenBalances", pre), ("postTokenBalances", post)):
        for bal in meta.get(key) or []:
            if not isinstance(bal, dict):
                continue
            if bal.get("mint") != mint:
                continue
            owner = bal.get("owner")
            amount = (bal.get("uiTokenAmount") or {}).get("uiAmount")
            if owner and isinstance(amount, (int, float)):
                target[owner] = float(amount)
    out: list[dict] = []
    for owner, amount in post.items():
        if owner in exclude:
            continue
        diff = amount - pre.get(owner, 0.0)
        if diff > 0:
            out.append({"wallet": owner, "amount": diff})
    return out


def find_creation(
    rpc: PublicRpcClient,
    mint: str,
    max_pages: int = DEFAULT_SIG_PAGES,
) -> tuple[object | None, float | None, list[dict]]:
    """The mint's pump.fun creation transaction, oldest-first.

    Pages getSignaturesForAddress (newest first) until the
    oldest page, then checks the oldest few transactions for a
    strict pump.fun create instruction (src/chain_index
    .parse_creation — the balance-diff fallback is NOT used, so
    a first buy is never called a creation). Returns
    (creation, dev_initial_balance, early_buyers) where
    early_buyers are the wallets that received the mint in the
    creation transaction (excluding the dev). (None, None, [])
    when no creation is found within the page budget.
    """
    before: str | None = None
    oldest_page: list[dict] = []
    for _ in range(max_pages):
        page = get_signatures(rpc, mint, limit=DEFAULT_SIG_PAGE_SIZE, before=before)
        if not page:
            break
        oldest_page = page
        before = page[-1].get("signature")
        if len(page) < DEFAULT_SIG_PAGE_SIZE:
            break  # reached the oldest page
    if not oldest_page:
        return None, None, []
    for entry in reversed(oldest_page[-DEFAULT_CREATION_TX_CANDIDATES:]):
        sig = entry.get("signature")
        if not sig:
            continue
        tx = get_transaction(rpc, sig)
        if tx is None:
            continue
        creation = parse_creation(tx)
        if creation is None or creation.mint != mint:
            continue
        dev = creation.dev_wallet
        initial = _dev_balance_in_tx(tx, dev, mint)
        buyers = _positive_balance_diffs(tx, mint, exclude={dev})
        return creation, initial, buyers
    return None, None, []


def _incoming_sol_funder(
    rpc: PublicRpcClient,
    wallet: str,
    before_ts: float | None,
) -> tuple[str | None, float | None]:
    """The wallet that funded ``wallet`` with SOL before a time.

    Scans the wallet's newest signatures (bounded) for a parsed
    system transfer INTO the wallet with a blockTime before
    ``before_ts``. Best-effort: the funding transfer may be
    older than the scanned window, in which case (None, None)
    is returned — an unknown funder is never invented.
    """
    try:
        sigs = get_signatures(rpc, wallet, limit=DEFAULT_FUNDER_SIG_LIMIT)
    except RpcError:
        return None, None
    for entry in sigs:
        block_time = entry.get("blockTime")
        if (
            before_ts is not None
            and isinstance(block_time, (int, float))
            and block_time >= before_ts
        ):
            continue
        sig = entry.get("signature")
        if not sig:
            continue
        try:
            tx = get_transaction(rpc, sig)
        except RpcError:
            continue
        if tx is None:
            continue
        message = (tx.get("transaction") or {}).get("message") or {}
        for instr in message.get("instructions") or []:
            if not isinstance(instr, dict):
                continue
            parsed = instr.get("parsed") or {}
            if parsed.get("type") != "transfer":
                continue
            info = parsed.get("info") or {}
            if info.get("destination") == wallet and info.get("source"):
                return info["source"], block_time
    return None, None


@dataclass
class EnrichResult:
    """What enriching one record produced."""

    mint: str
    features: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)
    calls: int = 0
    status: str = "done"  # done | partial | failed
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "status": self.status,
            "features": dict(self.features),
            "notes": dict(self.notes),
            "calls": self.calls,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EnrichResult":
        return cls(
            mint=data.get("mint", ""),
            features=dict(data.get("features") or {}),
            notes=dict(data.get("notes") or {}),
            calls=int(data.get("calls") or 0),
            status=str(data.get("status") or "done"),
            error=data.get("error"),
        )


def enrich_record(
    record: dict,
    rpc: PublicRpcClient,
    max_calls: int = DEFAULT_MAX_CALLS_PER_RECORD,
    early_buyers: int = DEFAULT_EARLY_BUYERS,
) -> EnrichResult:
    """Fill the 8 missing features for one labelled record.

    Steps, each guarded by the per-record call budget (a spent
    budget stops the record as "partial" with what was filled):
    lp_removed from stored liquidity (no call); last activity +
    creation + dev balance + holder data + funding events from
    the public RPC. A feature that cannot be fetched stays
    absent — never invented, never zero-filled as if real.
    """
    mint = record.get("mint", "")
    result = EnrichResult(mint=mint)
    calls_start = rpc.calls

    def _budget_left() -> int:
        return max_calls - (rpc.calls - calls_start)

    def _note(feature: str, source: str, basis: str) -> None:
        result.notes[feature] = {"source": source, "basis": basis}

    # --- lp_removed: inferred from stored DexScreener liquidity ---
    liquidity = record.get("liquidity_usd")
    if isinstance(liquidity, (int, float)):
        result.features["lp_removed"] = bool(liquidity < LP_DRAINED_USD)
        _note(
            "lp_removed",
            "dexscreener_liquidity",
            f"liquidity_usd {liquidity} vs LP_DRAINED_USD {LP_DRAINED_USD} "
            "(same inference as src/indexer)",
        )

    launch_at = _launch_time(record)
    outcome = record.get("outcome")

    # --- last on-chain activity (1 call) -> time_to_collapse proxy ---
    last_activity: float | None = None
    try:
        if _budget_left() > 0:
            newest = get_signatures(rpc, mint, limit=1)
            if newest:
                block_time = newest[0].get("blockTime")
                if isinstance(block_time, (int, float)):
                    last_activity = float(block_time)
    except RpcError as exc:
        result.error = f"signatures: {exc}"
    if launch_at is not None:
        if outcome == "rugged" and last_activity is not None:
            result.features["time_to_collapse_minutes"] = max(
                0, int((last_activity - launch_at) // 60)
            )
            _note(
                "time_to_collapse_minutes",
                "rpc_signatures",
                "launch estimate (from record) -> last on-chain "
                "activity (proxy: when trading died, not a priced "
                "collapse time)",
            )
        elif outcome == "survived":
            result.features["time_to_collapse_minutes"] = max(
                0, int((time.time() - launch_at) // 60)
            )
            _note(
                "time_to_collapse_minutes",
                "derived_age",
                "age at enrichment (no collapse observed)",
            )

    # --- holder data: largest accounts, fallback program accounts ---
    supply: float | None = None
    top10_shares: list[float] = []
    top1: float | None = None
    holder_count: int | None = None
    holder_source: str | None = None
    try:
        if _budget_left() > 0:
            supply = get_token_supply(rpc, mint)
    except RpcError as exc:
        result.error = f"token_supply: {exc}"
    try:
        if _budget_left() > 0:
            largest = get_largest_accounts(rpc, mint)
            if largest and supply:
                amounts = sorted(
                    (a["amount"] for a in largest), reverse=True
                )
                top1 = amounts[0] / supply
                top10_shares = [a / supply for a in amounts[:10]]
                holder_source = "rpc_largest_accounts"
    except RpcError:
        largest = None  # throttled — fall through to program accounts
    if top1 is None and _budget_left() > 0:
        try:
            info = get_mint_info(rpc, mint)
            program = (info or {}).get("owner")
            # The mint account's owner IS its token program
            # (classic Token program or Token-2022) — use it as
            # returned, never a hardcoded guess. A wrong constant
            # here silently skipped every Token-2022 mint (v2.15
            # live run, 2026-11-13) — the owner is authoritative.
            if program:
                accounts = get_token_accounts_for_mint(rpc, program, mint)
                if accounts:
                    amounts_raw = sorted(
                        (a["amount_raw"] for a in accounts), reverse=True
                    )
                    total_raw = sum(amounts_raw)
                    if total_raw > 0:
                        top1 = amounts_raw[0] / total_raw
                        top10_shares = [
                            a / total_raw for a in amounts_raw[:10]
                        ]
                        holder_source = "rpc_program_accounts"
                    holder_count = len({a["owner"] for a in accounts})
                    _note(
                        "holder_count",
                        "rpc_program_accounts",
                        f"{len(accounts)} token accounts, "
                        f"{holder_count} distinct owners "
                        f"(program {program})",
                    )
        except RpcError as exc:
            result.error = f"program_accounts: {exc}"
    if top1 is not None:
        result.features["top1_share"] = round(top1, 6)
        _note(
            "top1_share",
            holder_source or "unknown",
            "largest holder / total supply",
        )
    if top10_shares:
        result.features["top10_share"] = round(sum(top10_shares), 6)
        _note(
            "top10_share",
            holder_source or "unknown",
            "top 10 holders / total supply",
        )
    if holder_count is not None:
        result.features["holder_count"] = holder_count

    # --- wash score: only when real holder data exists ---
    if top10_shares or top1 is not None:
        scored = wash_score(
            holder_count,
            top10_shares or None,
            top1,
            record.get("volume_24h_usd"),
            record.get("market_cap_usd"),
        )
        if not scored.insufficient_data and scored.score is not None:
            result.features["wash_score"] = round(scored.score, 1)
            _note(
                "wash_score",
                "advanced.wash_score",
                f"score {scored.score:.0f}/100 ({scored.level}) from "
                "fetched holder shares",
            )

    # --- dev + creation + early buyers + cluster ---
    try:
        if _budget_left() > 0:
            creation, initial, buyers = find_creation(rpc, mint)
        else:
            creation, initial, buyers = None, None, []
    except RpcError as exc:
        creation, initial, buyers = None, None, []
        result.error = f"creation: {exc}"
    if creation is not None:
        dev = creation.dev_wallet
        # The bonding-curve vault also "receives" tokens in the
        # creation transaction (the majority of supply) — it is
        # not an early buyer, so drop any buyer holding > 50% of
        # supply when the supply is known.
        if supply:
            buyers = [
                b for b in buyers if b.get("amount", 0.0) <= 0.5 * supply
            ]
        try:
            if _budget_left() > 0:
                current = get_owner_token_balance(rpc, dev, mint)
                if (
                    initial is not None
                    and initial > 0
                    and current is not None
                ):
                    result.features["dev_sold_pct"] = round(
                        max(0.0, min(100.0, (initial - current) / initial * 100.0)),
                        2,
                    )
                    _note(
                        "dev_sold_pct",
                        "rpc_creation_tx+owner_balance",
                        f"dev initial {initial} -> current {current} "
                        "(creation-tx post balance vs current balance)",
                    )
        except RpcError as exc:
            result.error = f"dev_balance: {exc}"
        # Cluster: funding events for the dev + early buyers.
        events: list[FundingEvent] = []
        buy_times: dict[str, float | None] = {}
        for buyer in buyers[:early_buyers]:
            buy_times[buyer["wallet"]] = None
        # The creation tx's blockTime is the buy time for its buyers.
        if launch_at is not None:
            for wallet in buy_times:
                buy_times[wallet] = launch_at
        wallets = ([dev] if dev else []) + list(buy_times)
        for wallet in wallets:
            if _budget_left() <= 0:
                break
            buy_at = buy_times.get(wallet)
            funder, funded_at = _incoming_sol_funder(
                rpc, wallet, before_ts=buy_at
            )
            events.append(
                FundingEvent(
                    wallet=wallet,
                    funder=funder,
                    funded_at=int(funded_at) if funded_at else None,
                    bought_at=int(buy_at) if buy_at else None,
                    launch_at=int(launch_at) if launch_at else None,
                )
            )
        if events:
            clusters = StealthClusterDetector().detect(events)
            result.features["cluster_size"] = max(
                (len(c.members) for c in clusters), default=0
            )
            _note(
                "cluster_size",
                "advanced.StealthClusterDetector",
                f"{len(events)} funding events fetched, "
                f"{len(clusters)} cluster(s); largest "
                f"{result.features['cluster_size']} wallet(s)",
            )

    result.calls = rpc.calls - calls_start
    if result.error and not result.features:
        result.status = "failed"
    elif _budget_left() <= 0 and len(result.features) < len(ENRICHABLE_FEATURES):
        result.status = "partial"
    elif len(result.features) < len(ENRICHABLE_FEATURES):
        result.status = "partial"
    return result

# ======================================================================
# Checkpoint, dataset merge, coverage report
# ======================================================================
def load_checkpoint(path: str | Path) -> dict:
    """Checkpoint JSON; {} when the file is absent."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Checkpoint {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Checkpoint {p} must hold a JSON object.")
    return data


def save_checkpoint(path: str | Path, data: dict) -> None:
    """Write the checkpoint (after every record — a killed run resumes)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_dataset(path: str | Path) -> list[dict]:
    """The dataset JSON list; ValueError on a bad file."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Dataset {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Dataset {p} must hold a JSON list.")
    return data


def apply_enrichment(
    records: list[dict], results: list[EnrichResult]
) -> list[dict]:
    """Merge enriched features into dataset records (new dicts).

    Only the filled feature keys are set, plus an "enrichment"
    metadata block per enriched record (sources/basis per
    feature, calls, status). Every other field — mint, outcome,
    market facts, labelling rule — is untouched, and the rule
    version stays real-v2.
    """
    by_mint = {r.mint: r for r in results}
    enriched_at = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    for record in records:
        new = dict(record)
        result = by_mint.get(record.get("mint", ""))
        if result is not None and result.features:
            for key, value in result.features.items():
                new[key] = value
            new["enrichment"] = {
                "enriched_at": enriched_at,
                "status": result.status,
                "calls": result.calls,
                "features": dict(result.notes),
            }
        out.append(new)
    return out


def coverage_report(
    before_records: list[dict], after_records: list[dict]
) -> dict:
    """Per-feature coverage before/after, over labelled records only."""
    labelled_before = [
        r for r in before_records if r.get("outcome") in ("rugged", "survived")
    ]
    labelled_after = [
        r for r in after_records if r.get("outcome") in ("rugged", "survived")
    ]
    n = len(labelled_after)
    per_feature: dict[str, dict] = {}
    for feature in FEATURE_NAMES:
        per_feature[feature] = {
            "before": sum(
                1 for r in labelled_before if r.get(feature) is not None
            ),
            "after": sum(
                1 for r in labelled_after if r.get(feature) is not None
            ),
            "n_labelled": n,
        }
    return {
        "n_labelled": n,
        "n_total": len(after_records),
        "per_feature": per_feature,
    }


def format_coverage_report(report: dict) -> str:
    """Human-readable before/after coverage."""
    lines = [
        f"Feature coverage on {report['n_labelled']} labelled records "
        f"(of {report['n_total']} total):"
    ]
    for feature, counts in report["per_feature"].items():
        lines.append(
            f"  {feature}: {counts['before']} -> {counts['after']} "
            f"/ {counts['n_labelled']}"
        )
    return "\n".join(lines)


def enrich_dataset(
    records: list[dict],
    rpc: PublicRpcClient,
    checkpoint_path: str | Path,
    limit: int | None = None,
    max_calls: int = DEFAULT_MAX_CALLS_PER_RECORD,
    retry_failed: bool = False,
) -> tuple[list[EnrichResult], dict]:
    """Enrich the labelled records, checkpointing after each one.

    Records already "done" in the checkpoint are skipped (resume);
    "failed" / "partial" records are retried only with
    --retry-failed. Returns (results, checkpoint-data).
    """
    checkpoint = load_checkpoint(checkpoint_path)
    done = checkpoint.get("records") or {}
    if not isinstance(done, dict):
        done = {}
    labelled = [
        r for r in records if r.get("outcome") in ("rugged", "survived")
    ]
    if limit is not None:
        labelled = labelled[:limit]
    results: list[EnrichResult] = []
    for record in labelled:
        mint = record.get("mint", "")
        prev = done.get(mint)
        if (
            isinstance(prev, dict)
            and prev.get("status") == "done"
            and not retry_failed
        ):
            results.append(EnrichResult.from_dict(prev))
            continue
        result = enrich_record(record, rpc, max_calls=max_calls)
        done[mint] = result.to_dict()
        checkpoint = {
            "dataset": "data/ml_real_dataset.json",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "records": done,
        }
        save_checkpoint(checkpoint_path, checkpoint)
        results.append(result)
    return results, checkpoint


# ======================================================================
# CLI
# ======================================================================
def main(argv: list[str] | None = None, rpc: PublicRpcClient | None = None) -> int:
    """Feature-enrichment CLI.

    ``python -m src.feature_enrich [--dataset ...] [--checkpoint ...]
    [--limit N] [--max-calls-per-record N] [--rpc-delay S]
    [--max-retries N] [--retry-failed] [--json]``
    Exit 0 = done (per-record failures are recorded, not fatal),
    2 = invalid args / unreadable dataset / RPC unreachable.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm feature enrichment: fill the ML model's 8 missing features from the public Solana RPC (no key)."
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=str(DEFAULT_DATASET_PATH),
        help="Labelled dataset JSON (default: data/ml_real_dataset.json)",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=str(DEFAULT_CHECKPOINT_PATH),
        help="Checkpoint JSON for resume (default: data/feature_enrich_checkpoint.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Enrich at most N labelled records (default: all)",
    )
    parser.add_argument(
        "--max-calls-per-record",
        type=int,
        default=DEFAULT_MAX_CALLS_PER_RECORD,
        help=f"RPC call budget per record (default: {DEFAULT_MAX_CALLS_PER_RECORD})",
    )
    parser.add_argument(
        "--rpc-delay",
        type=float,
        default=DEFAULT_RPC_DELAY_SECONDS,
        help=f"Seconds between RPC calls (default: {DEFAULT_RPC_DELAY_SECONDS})",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Retries per RPC call (default: {DEFAULT_MAX_RETRIES})",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry checkpoint records that failed or were partial",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit <= 0:
        print("ERROR: --limit must be a positive integer.", file=sys.stderr)
        return EXIT_INVALID
    if args.max_calls_per_record <= 0:
        print("ERROR: --max-calls-per-record must be positive.", file=sys.stderr)
        return EXIT_INVALID
    if args.rpc_delay < 0 or args.max_retries < 0:
        print("ERROR: --rpc-delay / --max-retries must not be negative.", file=sys.stderr)
        return EXIT_INVALID

    try:
        records = load_dataset(args.dataset)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if not all(isinstance(r, dict) for r in records):
        print("ERROR: dataset must hold a JSON list of objects.", file=sys.stderr)
        return EXIT_INVALID

    client = rpc or PublicRpcClient(
        delay=args.rpc_delay, max_retries=args.max_retries
    )
    if not client.probe():
        print(
            "ERROR: public Solana RPC is unreachable after retries "
            "(tried api.mainnet-beta.solana.com and "
            "solana-rpc.publicnode.com). Nothing was enriched.",
            file=sys.stderr,
        )
        return EXIT_INVALID

    before = coverage_report(records, records)
    try:
        results, _checkpoint = enrich_dataset(
            records,
            client,
            args.checkpoint,
            limit=args.limit,
            max_calls=args.max_calls_per_record,
            retry_failed=args.retry_failed,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID

    enriched_records = apply_enrichment(records, results)
    if not BACKUP_PATH.exists():
        BACKUP_PATH.write_text(
            json.dumps(records, indent=2), encoding="utf-8"
        )
    save_dataset(args.dataset, enriched_records)
    save_summary(summary_path(args.dataset), enriched_records)
    after = coverage_report(records, enriched_records)

    done = sum(1 for r in results if r.status == "done")
    partial = sum(1 for r in results if r.status == "partial")
    failed = sum(1 for r in results if r.status == "failed")
    report = {
        "dataset": args.dataset,
        "records_enriched": len(results),
        "done": done,
        "partial": partial,
        "failed": failed,
        "rpc_calls": client.calls,
        "dead_methods": sorted(client.dead_methods),
        "coverage": after,
        "results": [r.to_dict() for r in results],
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"Enriched {len(results)} labelled record(s): {done} done, "
            f"{partial} partial, {failed} failed "
            f"({client.calls} RPC calls)."
        )
        if client.dead_methods:
            print(
                "Methods marked dead this run (throttled everywhere): "
                + ", ".join(sorted(client.dead_methods))
            )
        print(format_coverage_report(after))
        print(f"Dataset saved to {args.dataset}")
        print(f"Summary saved to {summary_path(args.dataset)}")
        print(f"Checkpoint saved to {args.checkpoint}")
        print("Labels are observable-market-fact labels, not verified "
              "rug verdicts — see src/real_data.py before quoting "
              "any metric.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
