"""Dev-wallet-first check (v2.17): paste a dev wallet, get the dev
plus its bundled wallets — no mint needed.

The manual check this automates (POOR's dev, 2026-11-13): the dev
``7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk`` ("rarewhaleenforc")
was funded by ``G4JXShDM6NqP1XpMo3gpY24wxPj8NVRqqKMTKv5gLNfY``
(itself a launcher, funded by ``5gjpbw...``), while early buyers
SkarraOG (funded by ``EZiMRZ...``) and therealhimmy (funded by
``ZstLak...``) each had a different funder — four different
funders, so NO direct bundle by the shared-funder test, and a
Bubblemaps top cluster of 12 wallets / 3.92%. This module runs
exactly that check from one command:

    python -m src.dev_check --wallet <address> [--json]

1. **Dev profile** — SOL balance + token-account count from the
   public RPC (``getBalance`` / ``getTokenAccountsByOwner``), the
   pump.fun profile link. No pump.fun display name is fetched:
   there is no public profile API reachable from this environment
   (the frontend API 403s the VM egress), so the name is reported
   as unavailable, never guessed. Realized PnL is not reported:
   no existing code computes it, and this module does not invent
   one.
2. **Funder chain, two hops up** — who funded the dev, and who
   funded THAT wallet. Via Helius when ``HELIUS_API_KEY`` is set
   (pages back through the wallet's full history), via the public
   Solana RPC when it is not (bounded: 3 pages x 1000 signatures
   per wallet, documented). The first funder is the sender of the
   wallet's oldest funding transfer (a ``system transfer`` to the
   wallet, or a ``createAccount`` creating it). A hop that cannot
   be resolved is reported as unknown with its coverage — never
   invented.
3. **Bundled-wallet test (the core)** — the dev's recent coins
   (pump.fun ``create`` instructions in their recent signatures),
   their top early buyers (token-balance diffs in the creation
   transaction), and each buyer's first funder the same way. A
   wallet is "bundled" with the dev ONLY when it shares the dev's
   first funder, OR is funded by the dev, OR funded the dev.
   When the test finds nothing it says "no direct bundle found".
   A bundle alone never flags a dev — it is evidence, and the
   report says so.
4. **Cluster view** — ``StealthClusterDetector`` over the funding
   graph built (dev + early buyers). When the data is too thin
   to cluster, it says so.
5. **Verdict** — ``DevHistoryBuilder``'s verdict over the dev's
   recent mints (DexScreener outcomes, no key needed) merged with
   any registry rugged mints for the dev. The flagging rule is
   unchanged: ``flagged`` still needs >= 2 rugged mints +
   evidence. The verdict covers the scanned window only, and the
   report says how many mints that is.

Honest limits, stated in the report, not hidden:
- The public-RPC path only sees the most recent signatures per
  wallet (3 pages x 1000). A dev funded 1.5 years ago has its
  first-funding link far outside that window, so the funder
  chain and the bundle test come back "unresolved" without a
  Helius key — the check says so instead of claiming "no
  bundle". The manual POOR finding (four different funders)
  needs the full history a key unlocks.
- The public RPC does not parse pump.fun instructions, so
  creations are also recognised from raw instruction data
  (the ``create`` discriminator, ``parse_creation_any``) —
  a buy/sell instruction is never mistaken for a creation.
- Early buyers come from the creation transaction of the dev's
  most recent coins only; older coins are out of the window.
- The cluster view is funding clusters only (Bubblemaps has no
  public API), and a cluster is never a flag on its own.

Exit codes: 0 = done, 1 = flagged dev, 2 = invalid address /
no network. Stdlib + httpx only, no wallet, no funds moved.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .advanced import FundingEvent, StealthClusterDetector
from .chain_index import parse_creation, parse_funding_transfers
from .dev_history import DevHistoryBuilder
from .indexer import HeliusIndexer, TokenCreation
from .live_scan import HeliusClient
from .public_rpc import PublicRpcClient, RpcError
from .registry import is_valid_solana_address, load_registry

EXIT_OK = 0
EXIT_FLAGGED = 1
EXIT_BLOCKED = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Pacing for the shared public-RPC client (src.public_rpc), preserved
# from this module's original client: 2.5 s apart, 4 retries. The
# shared client's backoff tuning (2.0 s base / 30 s cap) applies —
# the old 5.0 s base turned one throttled method into a ~150 s stall.
DEFAULT_RPC_DELAY_SECONDS = 2.5
DEFAULT_MAX_RETRIES = 4

# Public-RPC funder walk: 3 pages x 1000 signatures per wallet.
# The first-funding link of an old wallet is outside this window —
# that is a stated limit, not a silent gap (see module docstring).
PUBLIC_FUNDER_MAX_PAGES = 3
PUBLIC_SIG_PAGE_SIZE = 1000
# Helius funder walk: full history paging, bounded so a wallet
# with ~70k signatures cannot run forever.
HELIUS_FUNDER_MAX_PAGES = 80
HELIUS_SIG_PAGE_SIZE = 1000

DEFAULT_SIG_LIMIT = 100  # signatures scanned for recent creations
DEFAULT_MAX_COINS = 3
DEFAULT_MAX_BUYERS = 5
DEFAULT_MAX_TX_FETCHES = 20  # tx fetches while hunting creations
DEFAULT_BUYER_TX_CANDIDATES = 5

# pump.fun `create` instruction discriminators: first 8 bytes of
# sha256("global:create") and sha256("global:create_v2") — the
# Anchor discriminators for the v1 and v2 create instructions
# (18 1e c8 28 05 1c 07 77 and d6 90 4c ec 5f 8b 31 b4).
# Pump.fun migrated to v2: mints launched after the migration
# (e.g. BagsBack, Nov 12 2026) are created by `create_v2`, so
# matching only v1 misses every recent launch. The public RPC
# does NOT parse pump.fun instructions (it only parses built-in
# programs), so a creation transaction from the public RPC
# carries the create instruction as raw base58 data, not a
# parsed "create" — the strict parse in src.chain_index misses
# it entirely. The raw fallback below matches the discriminator
# instead. Helius parses it, so the Helius path never needs this.
CREATE_DISCRIMINATOR = bytes.fromhex("181ec828051c0777")
CREATE_V2_DISCRIMINATOR = bytes.fromhex("d6904cec5f8b31b4")
CREATE_DISCRIMINATORS = (CREATE_DISCRIMINATOR, CREATE_V2_DISCRIMINATOR)

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(text: str) -> bytes:
    """Base58 decode (stdlib has none). Raises ValueError on bad input."""
    num = 0
    for ch in text:
        try:
            digit = _B58_ALPHABET.index(ch)
        except ValueError:
            raise ValueError(f"invalid base58 character: {ch!r}") from None
        num = num * 58 + digit
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + raw


def parse_creation_any(tx: dict):
    """A pump.fun TokenCreation from one transaction, either way
    the RPC returns it.

    First the strict parsed check (``src.chain_index.parse_creation``,
    which Helius data satisfies). Then the raw fallback for the
    public RPC: a top-level pump.fun instruction whose base58 data
    starts with a ``create`` discriminator (v1 or v2) — the mint
    is the instruction's first account, the dev is the fee payer.
    A pump.fun instruction with any other discriminator (buy, sell,
    ...) is not a creation and is ignored.
    """
    creation = parse_creation(tx)
    if creation is not None:
        return creation
    if not isinstance(tx, dict):
        return None
    message = (tx.get("transaction") or {}).get("message") or {}
    account_keys = message.get("accountKeys") or []
    fee_payer = None
    if account_keys:
        first = account_keys[0]
        fee_payer = first.get("pubkey") if isinstance(first, dict) else first
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    signature = sigs[0] if sigs else None
    block_time = tx.get("blockTime")
    timestamp = None
    if isinstance(block_time, (int, float)):
        timestamp = datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()
    if not fee_payer:
        return None
    for instr in message.get("instructions") or []:
        if not isinstance(instr, dict):
            continue
        if instr.get("programId") != PUMP_FUN_PROGRAM:
            continue
        data = instr.get("data")
        if not isinstance(data, str):
            continue
        try:
            decoded = _b58decode(data)
        except ValueError:
            continue
        if len(decoded) < 8 or decoded[:8] not in CREATE_DISCRIMINATORS:
            continue
        accounts = instr.get("accounts") or []
        if not accounts:
            continue
        first_acct = accounts[0]
        mint = (
            first_acct.get("pubkey")
            if isinstance(first_acct, dict)
            else first_acct
        )
        if not mint:
            continue
        return TokenCreation(
            mint=mint,
            dev_wallet=fee_payer,
            program="pump.fun",
            timestamp=timestamp,
            signature=signature,
        )
    return None


# ======================================================================
# Chain sources: Helius when a key is set, public RPC when it is not.
# Both expose the same four methods, so the checker never knows
# which transport it is on — and tests inject a stub source.
# ======================================================================
class HeliusSource:
    """Signatures/transactions via Helius; balances via public RPC
    (getBalance needs no key)."""

    name = "helius"

    def __init__(self, client: HeliusClient | None = None, rpc: PublicRpcClient | None = None):
        self._helius = client if client is not None else HeliusClient()
        self._indexer = HeliusIndexer(api_key=self._helius.api_key)
        self._rpc = rpc if rpc is not None else PublicRpcClient(
            delay=DEFAULT_RPC_DELAY_SECONDS, max_retries=DEFAULT_MAX_RETRIES
        )

    @property
    def enabled(self) -> bool:
        return self._helius.enabled

    def probe(self) -> bool:
        return self._rpc.probe()

    def get_signatures(self, address: str, limit: int = 50, before: str | None = None) -> list[dict]:
        result = self._indexer.get_signatures(address, limit=limit, before=before)
        return result if isinstance(result, list) else []

    def get_transaction(self, signature: str) -> dict | None:
        return self._indexer.get_transaction(signature)

    def get_balance(self, address: str) -> float | None:
        result = self._rpc.rpc("getBalance", [address])
        if not isinstance(result, dict):
            return None
        value = result.get("value")
        return float(value) / 1e9 if isinstance(value, (int, float)) else None

    def get_token_account_count(self, address: str) -> int | None:
        total = 0
        ok = False
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            try:
                result = self._rpc.rpc(
                    "getTokenAccountsByOwner",
                    [address, {"programId": program}, {"encoding": "jsonParsed"}],
                )
            except RpcError:
                continue
            if isinstance(result, dict) and isinstance(result.get("value"), list):
                total += len(result["value"])
                ok = True
        return total if ok else None


class PublicRpcSource:
    """Everything via the public Solana RPC (no key)."""

    name = "public-rpc"

    def __init__(self, rpc: PublicRpcClient | None = None):
        self._rpc = rpc if rpc is not None else PublicRpcClient(
            delay=DEFAULT_RPC_DELAY_SECONDS, max_retries=DEFAULT_MAX_RETRIES
        )

    @property
    def enabled(self) -> bool:
        return True

    def probe(self) -> bool:
        return self._rpc.probe()

    def get_signatures(self, address: str, limit: int = 50, before: str | None = None) -> list[dict]:
        opts: dict = {"limit": limit}
        if before:
            opts["before"] = before
        result = self._rpc.rpc("getSignaturesForAddress", [address, opts])
        if not isinstance(result, list):
            return []
        return [e for e in result if isinstance(e, dict)]

    def get_transaction(self, signature: str) -> dict | None:
        result = self._rpc.rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
        )
        return result if isinstance(result, dict) else None

    def get_balance(self, address: str) -> float | None:
        result = self._rpc.rpc("getBalance", [address])
        if not isinstance(result, dict):
            return None
        value = result.get("value")
        return float(value) / 1e9 if isinstance(value, (int, float)) else None

    def get_token_account_count(self, address: str) -> int | None:
        total = 0
        ok = False
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            try:
                result = self._rpc.rpc(
                    "getTokenAccountsByOwner",
                    [address, {"programId": program}, {"encoding": "jsonParsed"}],
                )
            except RpcError:
                continue
            if isinstance(result, dict) and isinstance(result.get("value"), list):
                total += len(result["value"])
                ok = True
        return total if ok else None


# ======================================================================
# Funder resolution
# ======================================================================
@dataclass
class FunderLink:
    """One hop of the funder chain.

    ``status`` is "found" only when the walk reached the wallet's
    oldest signature AND that transaction funds the wallet.
    Anything else is "unresolved" — the funder is unknown, not
    "none". ``coverage`` states how far the walk got.
    """

    wallet: str
    funder: str | None = None
    status: str = "unresolved"  # "found" | "unresolved"
    signature: str | None = None
    amount_sol: float | None = None
    funded_at: str | None = None
    signatures_scanned: int = 0
    pages_scanned: int = 0
    reached_oldest: bool = False
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "funder": self.funder,
            "status": self.status,
            "signature": self.signature,
            "amount_sol": self.amount_sol,
            "funded_at": self.funded_at,
            "signatures_scanned": self.signatures_scanned,
            "pages_scanned": self.pages_scanned,
            "reached_oldest": self.reached_oldest,
            "note": self.note,
        }


def _funding_sender_to_wallet(tx: dict, wallet: str) -> tuple[str, float] | None:
    """The sender of a SOL funding transfer into ``wallet`` in one tx.

    Two shapes count: a parsed ``system transfer`` with
    destination == wallet, and a parsed ``createAccount`` with
    account == wallet (the account's creation funding). Returns
    (sender, amount_sol) or None. The fee payer paying fees does
    NOT count — only an explicit transfer / creation.
    """
    if not isinstance(tx, dict):
        return None
    message = (tx.get("transaction") or {}).get("message") or {}
    for instr in message.get("instructions") or []:
        if not isinstance(instr, dict):
            continue
        parsed = instr.get("parsed") or {}
        info = parsed.get("info") or {}
        ptype = parsed.get("type")
        if ptype == "transfer" and info.get("destination") == wallet:
            src = info.get("source")
            if src and src != wallet:
                return src, float(info.get("lamports") or 0) / 1e9
        if ptype == "createAccount" and info.get("account") == wallet:
            src = info.get("source")
            if src and src != wallet:
                return src, float(info.get("lamports") or 0) / 1e9
    return None


def _tx_time(tx: dict) -> str | None:
    block_time = tx.get("blockTime")
    if isinstance(block_time, (int, float)):
        return datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()
    return None


def resolve_first_funder(source, wallet: str, max_pages: int | None = None) -> FunderLink:
    """Walk a wallet's history back to its first funding transfer.

    Pages ``getSignaturesForAddress`` newest-first until the oldest
    signature is reached (a short page ends the walk) or the page
    budget runs out. Only when the walk reaches the oldest
    signature is the funding sender of the oldest transactions
    the first funder. A truncated walk reports "unresolved" with
    its coverage — the funder is unknown, never guessed.
    """
    if max_pages is None:
        max_pages = (
            HELIUS_FUNDER_MAX_PAGES
            if getattr(source, "name", "") == "helius"
            else PUBLIC_FUNDER_MAX_PAGES
        )
    link = FunderLink(wallet=wallet)
    before: str | None = None
    oldest_entries: list[dict] = []
    try:
        for _ in range(max_pages):
            page = source.get_signatures(wallet, limit=PUBLIC_SIG_PAGE_SIZE, before=before)
            link.pages_scanned += 1
            link.signatures_scanned += len(page)
            if not page:
                link.reached_oldest = True
                break
            oldest_entries = page
            last = page[-1].get("signature") if isinstance(page[-1], dict) else None
            if len(page) < PUBLIC_SIG_PAGE_SIZE:
                link.reached_oldest = True
                break
            if not last:
                break
            before = last
    except Exception as exc:
        link.note = f"Signature walk failed: {exc}"
        return link
    if not link.reached_oldest:
        link.note = (
            f"Walk stopped after {link.pages_scanned} page(s) / "
            f"{link.signatures_scanned} signatures without reaching the "
            "wallet's oldest transaction — the first funder is outside "
            "the scanned window."
        )
        return link
    if not oldest_entries:
        link.note = "Wallet has no transactions — no funder exists."
        return link
    # Oldest first: the creation funding is the earliest transfer in.
    for entry in reversed(oldest_entries):
        sig = entry.get("signature") if isinstance(entry, dict) else None
        if not sig:
            continue
        try:
            tx = source.get_transaction(sig)
        except Exception:
            continue
        if not isinstance(tx, dict):
            continue
        found = _funding_sender_to_wallet(tx, wallet)
        if found is None:
            continue
        sender, amount = found
        link.funder = sender
        link.status = "found"
        link.signature = sig
        link.amount_sol = amount
        link.funded_at = _tx_time(tx)
        return link
    link.note = (
        "The wallet's oldest fetched transactions show no SOL "
        "funding transfer into it — the first funder could not be "
        "identified from them."
    )
    return link


# ======================================================================
# Recent creations + early buyers
# ======================================================================
def find_recent_creations(source, wallet: str, sig_limit: int = DEFAULT_SIG_LIMIT,
                          max_coins: int = DEFAULT_MAX_COINS,
                          max_tx_fetches: int = DEFAULT_MAX_TX_FETCHES) -> list:
    """The dev's most recent pump.fun creations, newest first.

    Fetches transactions for the wallet's most recent signatures
    (bounded) and keeps strict ``create``-instruction parses —
    a first buy is never called a creation.
    """
    try:
        sigs = source.get_signatures(wallet, limit=sig_limit)
    except Exception:
        return []
    creations = []
    seen: set[str] = set()
    fetched = 0
    for entry in sigs:
        if len(creations) >= max_coins or fetched >= max_tx_fetches:
            break
        sig = entry.get("signature") if isinstance(entry, dict) else None
        if not sig:
            continue
        try:
            tx = source.get_transaction(sig)
        except Exception:
            continue
        fetched += 1
        if not isinstance(tx, dict):
            continue
        creation = parse_creation_any(tx)
        if creation is None or creation.dev_wallet != wallet:
            continue
        if creation.mint in seen:
            continue
        seen.add(creation.mint)
        creations.append(creation)
    return creations


def _positive_balance_diffs(tx: dict, mint: str, exclude: set[str]) -> list[dict]:
    """Wallets that received the mint in one transaction.

    Compares pre/post token balances per (owner, mint): a positive
    diff is a buy. Returns [{"wallet", "amount"}].
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


def find_early_buyers(source, mint: str, dev: str,
                      limit: int = DEFAULT_MAX_BUYERS) -> list[dict]:
    """Top early buyers of a mint, from its creation transaction.

    Pages the mint's signatures, checks the oldest candidates for
    a strict pump.fun ``create`` instruction, and takes the
    token-balance diffs in that transaction (excluding the dev).
    [] when no creation is found in the window.
    """
    try:
        sigs = source.get_signatures(mint, limit=10)
    except Exception:
        return []
    for entry in reversed(sigs[:DEFAULT_BUYER_TX_CANDIDATES]):
        sig = entry.get("signature") if isinstance(entry, dict) else None
        if not sig:
            continue
        try:
            tx = source.get_transaction(sig)
        except Exception:
            continue
        if not isinstance(tx, dict):
            continue
        creation = parse_creation_any(tx)
        if creation is None or creation.mint != mint:
            continue
        buyers = _positive_balance_diffs(tx, mint, exclude={dev})
        buyers.sort(key=lambda b: -b["amount"])
        return buyers[:limit]
    return []


# ======================================================================
# Report
# ======================================================================
@dataclass
class BuyerCheck:
    wallet: str
    mint: str
    amount: float
    funder: FunderLink | None = None
    bundled: bool | None = None  # True | False | None = unknown
    bundle_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "mint": self.mint,
            "amount": self.amount,
            "funder": self.funder.to_dict() if self.funder else None,
            "bundled": self.bundled,
            "bundle_reason": self.bundle_reason,
        }


@dataclass
class DevCheckReport:
    dev_wallet: str
    source: str
    sol_balance: float | None = None
    sol_balance_source: str = "public RPC getBalance"
    token_account_count: int | None = None
    pumpfun_profile: str = ""
    pumpfun_name: str | None = None
    funder_chain: list[FunderLink] = field(default_factory=list)
    history_verdict: str = "insufficient_data"
    history_checked: int = 0
    history_rugged: int = 0
    history_counts: dict = field(default_factory=dict)
    history_window_note: str = ""
    recent_coins: list = field(default_factory=list)
    buyers: list[BuyerCheck] = field(default_factory=list)
    bundle_verdict: str = "inconclusive"
    bundle_note: str = ""
    clusters: list = field(default_factory=list)
    cluster_note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def bundled_wallets(self) -> list[str]:
        return [b.wallet for b in self.buyers if b.bundled is True]

    def to_dict(self) -> dict:
        return {
            "dev_wallet": self.dev_wallet,
            "source": self.source,
            "profile": {
                "sol_balance": self.sol_balance,
                "sol_balance_source": self.sol_balance_source,
                "token_account_count": self.token_account_count,
                "pumpfun_profile": self.pumpfun_profile,
                "pumpfun_name": self.pumpfun_name,
            },
            "funder_chain": [f.to_dict() for f in self.funder_chain],
            "history": {
                "verdict": self.history_verdict,
                "checked": self.history_checked,
                "rugged_mint_count": self.history_rugged,
                "counts_by_outcome": self.history_counts,
                "window_note": self.history_window_note,
            },
            "recent_coins": [
                {
                    "mint": c.mint,
                    "timestamp": c.timestamp,
                    "signature": c.signature,
                }
                for c in self.recent_coins
            ],
            "early_buyers": [b.to_dict() for b in self.buyers],
            "bundle": {
                "verdict": self.bundle_verdict,
                "bundled_wallets": self.bundled_wallets,
                "note": self.bundle_note,
            },
            "clusters": [
                {
                    "members": c.members,
                    "funders": c.funders,
                    "signals": c.signals,
                    "evidence": c.evidence,
                }
                for c in self.clusters
            ],
            "cluster_note": self.cluster_note,
            "notes": self.notes,
        }

    def to_text(self) -> str:
        lines = [
            f"Dev wallet: {self.dev_wallet}",
            f"Source: {self.source}",
            "",
            "Profile:",
        ]
        if self.sol_balance is not None:
            lines.append(f"  SOL balance: {self.sol_balance:,.3f} SOL ({self.sol_balance_source})")
        else:
            lines.append("  SOL balance: unknown (getBalance failed)")
        if self.token_account_count is not None:
            lines.append(f"  Token accounts: {self.token_account_count}")
        else:
            lines.append("  Token accounts: unknown")
        lines.append(f"  pump.fun profile: {self.pumpfun_profile}")
        lines.append(
            "  pump.fun name: unavailable (no public profile API reachable)"
            if self.pumpfun_name is None
            else f"  pump.fun name: {self.pumpfun_name}"
        )
        lines.append("")
        lines.append("Funder chain (two hops up):")
        if not self.funder_chain:
            lines.append("  No funder chain resolved.")
        for hop, link in enumerate(self.funder_chain, start=1):
            if link.status == "found":
                when = f" on {link.funded_at[:10]}" if link.funded_at else ""
                lines.append(
                    f"  Hop {hop}: {link.wallet[:12]}... funded by "
                    f"{link.funder}{when} ({link.amount_sol} SOL)"
                    if link.amount_sol is not None
                    else f"  Hop {hop}: {link.wallet[:12]}... funded by {link.funder}{when}"
                )
            else:
                lines.append(
                    f"  Hop {hop}: {link.wallet[:12]}... funder unknown — "
                    f"{link.note or 'unresolved'}"
                )
        lines.append("")
        lines.append(
            f"Dev history verdict: {self.history_verdict.upper()} "
            f"({self.history_checked} mints checked, {self.history_rugged} rugged with evidence)"
        )
        if self.history_window_note:
            lines.append(f"  {self.history_window_note}")
        if self.history_counts:
            lines.append(f"  Outcomes: {self.history_counts}")
        lines.append("")
        lines.append("Bundled-wallet test:")
        lines.append(f"  {self.bundle_verdict.upper()} — {self.bundle_note}")
        for b in self.buyers:
            tag = (
                "BUNDLED" if b.bundled is True
                else "not bundled" if b.bundled is False
                else "unknown"
            )
            funder = (
                b.funder.funder
                if b.funder is not None and b.funder.status == "found"
                else "funder unknown"
            )
            lines.append(
                f"  [{tag}] {b.wallet} (bought {b.amount:,.0f} of {b.mint[:12]}..., "
                f"funded by {funder})"
            )
            if b.bundle_reason:
                lines.append(f"    {b.bundle_reason}")
        if self.clusters:
            lines.append("")
            lines.append("Funding clusters:")
            for c in self.clusters:
                lines.append(
                    f"  Cluster ({len(c.members)} wallets, signals: {', '.join(c.signals)}):"
                )
                for ev in c.evidence:
                    lines.append(f"    - {ev}")
        elif self.cluster_note:
            lines.append("")
            lines.append(f"Cluster view: {self.cluster_note}")
        if self.notes:
            lines.append("")
            for note in self.notes:
                lines.append(f"Note: {note}")
        lines.append("")
        lines.append(
            "A bundle alone never flags a dev — it is evidence only. "
            "Flagging still needs >= 2 rugged mints with evidence."
        )
        return "\n".join(lines)


# ======================================================================
# Checker
# ======================================================================
class DevChecker:
    """Dev-wallet-first check over one chain source.

    ``source`` is any object with ``get_signatures``,
    ``get_transaction``, ``get_balance`` and
    ``get_token_account_count`` (a real ``HeliusSource`` /
    ``PublicRpcSource`` in production, a stub in tests).
    ``registry_data`` is the loaded registry dict (``{}`` = none);
    rugged mints already in the registry count toward the >= 2
    rule, deduped by mint — same as DevHistoryBuilder.
    ``outcome_fetcher`` is ``(mint, dev) -> MintOutcome``; the
    default is the live DexScreener fetch (no key needed). Tests
    inject a stub so no network is ever made.
    """

    def __init__(self, source, registry_data: dict | None = None,
                 outcome_fetcher=None) -> None:
        self.source = source
        self.registry_data = registry_data or {}
        self.outcome_fetcher = outcome_fetcher

    def check(
        self,
        dev_wallet: str,
        sig_limit: int = DEFAULT_SIG_LIMIT,
        max_coins: int = DEFAULT_MAX_COINS,
        max_buyers: int = DEFAULT_MAX_BUYERS,
    ) -> DevCheckReport:
        if not is_valid_solana_address(dev_wallet):
            raise ValueError(f"'{dev_wallet}' is not a valid Solana address.")
        report = DevCheckReport(
            dev_wallet=dev_wallet,
            source=getattr(self.source, "name", "unknown"),
            pumpfun_profile=f"https://pump.fun/profile/{dev_wallet}",
        )
        # -- profile ------------------------------------------------
        try:
            report.sol_balance = self.source.get_balance(dev_wallet)
        except Exception:
            report.sol_balance = None
        try:
            report.token_account_count = self.source.get_token_account_count(dev_wallet)
        except Exception:
            report.token_account_count = None

        # -- recent creations + history (DexScreener outcomes, no key) --
        creations = find_recent_creations(
            self.source, dev_wallet, sig_limit=sig_limit, max_coins=max_coins
        )
        report.recent_coins = creations
        outcomes: dict = {}
        fetcher = self.outcome_fetcher
        if fetcher is None:
            fetcher = HeliusIndexer(api_key=None).fetch_mint_outcome
        for creation in creations:
            try:
                outcomes[creation.mint] = fetcher(
                    creation.mint, dev_wallet=dev_wallet
                )
            except Exception:
                continue
        history = None
        if creations:
            try:
                builder = DevHistoryBuilder(HeliusClient(), self.registry_data)
                history = builder.build(
                    dev_wallet, limit=max_coins,
                    creations=creations, outcomes=outcomes,
                )
            except Exception as exc:
                report.notes.append(f"Dev history failed: {exc}")
        if history is not None:
            report.history_verdict = history.verdict
            report.history_checked = history.checked
            report.history_rugged = history.rugged_mint_count
            report.history_counts = history.counts_by_outcome()
            report.history_window_note = (
                f"Verdict covers the {len(creations)} most recent mint(s) "
                "in the scanned window, plus any registry rugged mints "
                "for this dev."
            )
        else:
            report.history_window_note = (
                "No recent creations found in the scanned signature "
                "window — the history verdict is insufficient_data, "
                "not a clean bill of health."
            )

        # -- funder chain, two hops up ------------------------------
        dev_funder = resolve_first_funder(self.source, dev_wallet)
        report.funder_chain.append(dev_funder)
        if dev_funder.status == "found" and dev_funder.funder:
            report.funder_chain.append(
                resolve_first_funder(self.source, dev_funder.funder)
            )

        # -- early buyers + bundle test ------------------------------
        seen_buyers: set[str] = set()
        for creation in creations:
            buyers = find_early_buyers(
                self.source, creation.mint, dev_wallet, limit=max_buyers
            )
            for buyer in buyers:
                wallet = buyer["wallet"]
                if wallet in seen_buyers:
                    continue
                seen_buyers.add(wallet)
                check = BuyerCheck(
                    wallet=wallet, mint=creation.mint, amount=buyer["amount"]
                )
                check.funder = resolve_first_funder(self.source, wallet)
                check.bundled, check.bundle_reason = self._bundle_test(
                    dev_wallet, dev_funder, check
                )
                report.buyers.append(check)
            if len(report.buyers) >= max_buyers:
                break
        report.buyers = report.buyers[:max_buyers]

        # -- bundle verdict ------------------------------------------
        if any(b.bundled is True for b in report.buyers):
            report.bundle_verdict = "bundle found"
            report.bundle_note = (
                f"{len(report.bundled_wallets)} early buyer(s) share a "
                "funding link with the dev (see below)."
            )
        elif report.buyers and all(b.bundled is False for b in report.buyers):
            report.bundle_verdict = "no direct bundle found"
            report.bundle_note = (
                "Every early buyer checked has a different first funder "
                "from the dev and no direct funding link to the dev."
            )
        elif not report.buyers:
            report.bundle_verdict = "inconclusive"
            report.bundle_note = (
                "No early buyers found in the scanned window, so the "
                "bundle test could not run."
            )
        else:
            report.bundle_verdict = "inconclusive"
            report.bundle_note = (
                "At least one funder could not be resolved in the "
                "scanned window, so the bundle test is incomplete — "
                "this is NOT a 'no bundle' finding."
            )

        # -- cluster view --------------------------------------------
        # Events are the dev + its early buyers only. The second
        # funder-chain hop is NOT an event: it would trivially
        # cluster the dev with its own funder (a 2-hop chain the
        # funder-chain section already shows). What matters here
        # is whether several wallets in the bundle graph share a
        # funder.
        events: list[FundingEvent] = []
        if dev_funder.status == "found":
            events.append(
                FundingEvent(
                    wallet=dev_wallet,
                    funder=dev_funder.funder,
                    amount_sol=dev_funder.amount_sol,
                    funded_at=self._epoch(dev_funder.funded_at),
                )
            )
        for b in report.buyers:
            if b.funder is not None and b.funder.status == "found":
                events.append(
                    FundingEvent(
                        wallet=b.wallet,
                        funder=b.funder.funder,
                        amount_sol=b.funder.amount_sol,
                        funded_at=self._epoch(b.funder.funded_at),
                    )
                )
        if len(events) >= 2 and any(e.funder for e in events):
            clusters = StealthClusterDetector().detect(events)
            report.clusters = clusters
            if not clusters:
                report.cluster_note = (
                    "Funding graph has no shared funder or 2-hop chain "
                    "among the wallets checked — no cluster."
                )
        else:
            report.cluster_note = (
                "Too thin to cluster: fewer than 2 wallets in the "
                "funding graph have a resolved funder."
            )
        return report

    @staticmethod
    def _bundle_test(dev_wallet: str, dev_funder: FunderLink, buyer: BuyerCheck):
        """Bundled ONLY on a proven funding link.

        True: buyer shares the dev's first funder, is funded by
        the dev, or funded the dev. False: both funders resolved
        and none of those hold. None: a funder is unresolved, so
        the test cannot run for this buyer.
        """
        if buyer.funder is None or buyer.funder.status != "found":
            return None, "Buyer's first funder unresolved — cannot test."
        buyer_funder = buyer.funder.funder
        if dev_funder.status == "found" and dev_funder.funder:
            if buyer_funder == dev_funder.funder:
                return True, f"Shares the dev's first funder {dev_funder.funder}."
            if buyer_funder == dev_wallet:
                return True, "Funded by the dev."
            if dev_funder.funder == buyer.wallet:
                return True, "Funded the dev."
            return False, (
                f"First funder {buyer_funder} differs from the dev's "
                f"first funder {dev_funder.funder}."
            )
        # Dev's funder unknown: only the direct links are testable.
        if buyer_funder == dev_wallet:
            return True, "Funded by the dev."
        return None, (
            "The dev's first funder is unresolved, so a shared-funder "
            "test cannot run for this buyer."
        )

    @staticmethod
    def _epoch(iso: str | None) -> int | None:
        if not iso:
            return None
        try:
            return int(datetime.fromisoformat(iso).timestamp())
        except ValueError:
            return None


# ======================================================================
# CLI
# ======================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.dev_check",
        description="Dev-wallet-first check: paste a dev wallet, get the dev + its bundled wallets.",
    )
    parser.add_argument("--wallet", required=True, help="Dev wallet address")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_SIG_LIMIT,
        help="Signatures scanned for recent creations (default: 100)",
    )
    parser.add_argument(
        "--coins", type=int, default=DEFAULT_MAX_COINS,
        help="Recent coins checked for early buyers (default: 3)",
    )
    parser.add_argument(
        "--buyers", type=int, default=DEFAULT_MAX_BUYERS,
        help="Top early buyers per coin (default: 5)",
    )
    parser.add_argument(
        "--registry", type=str, default=None,
        help="Registry JSON path to merge (default: data/registry.json if it exists)",
    )
    args = parser.parse_args(argv)

    if not is_valid_solana_address(args.wallet):
        print(
            f"ERROR: '{args.wallet}' is not a valid Solana address.",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    if args.limit < 1 or args.coins < 1 or args.buyers < 1:
        print("ERROR: --limit, --coins and --buyers must be >= 1", file=sys.stderr)
        return EXIT_BLOCKED

    helius = HeliusClient()
    if helius.enabled:
        source = HeliusSource(client=helius)
    else:
        source = PublicRpcSource()
        if not source.probe():
            print(
                "ERROR: public Solana RPC is unreachable on every "
                "endpoint — cannot run the check.",
                file=sys.stderr,
            )
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

    checker = DevChecker(source, registry_data)
    try:
        report = checker.check(
            args.wallet,
            sig_limit=args.limit,
            max_coins=args.coins,
            max_buyers=args.buyers,
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"ERROR: dev check failed: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_text())
    return EXIT_FLAGGED if report.history_verdict == "flagged" else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
