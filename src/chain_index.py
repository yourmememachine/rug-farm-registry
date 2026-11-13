"""The registry's own chain index (v2.14 "Pro").

The registry used to lean on Helius and DexScreener for everything
it knew about a coin. This module is the first step toward owning
the data: it reads raw transactions through the RPC transport
(``src.indexer.HeliusIndexer`` — transport only; the parsing and
the tables are ours) and writes what it finds into the SQLite
database from ``src.db``.

Scope, stated plainly: this indexes what the registry watches —
specific mints and dev wallets — not the whole chain. Full-chain
indexing is out of scope; a watched-mint index is what the
registry needs, and it is honest about that limit.

What it records per transaction:
- pump.fun ``create`` instructions -> the mint + its dev wallet
  (``HeliusIndexer.parse_transaction``), upserted into ``mints``
  and ``devs``;
- token-balance diffs (``meta.preTokenBalances`` vs
  ``postTokenBalances``) -> one ``transfers`` row per wallet whose
  balance moved: a positive diff is a buy (received tokens), a
  negative diff is a sell. Holders are derived from those rows
  (see the ``holders`` view in ``src.db``);
- parsed ``system transfer`` instructions -> ``funding_edges``
  between wallets (the raw material for the stealth-cluster
  detector).

No Helius key is a hard error, not a silent empty result:
``ChainIndexer`` raises ``RuntimeError`` naming ``HELIUS_API_KEY``
and the CLI exits 2. Nothing is ever invented — a transaction
that cannot be parsed is skipped and counted, never faked.

    python -m src.chain_index --mint <address> [--db <path>] [--limit 50]
    python -m src.chain_index --wallet <address> [--db <path>] [--limit 50]

Exit codes: 0 = done, 2 = no key / invalid address / invalid args.
Stdlib + httpx only, no wallet, no funds moved.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import db as db_mod
from .indexer import (
    HeliusIndexer,
    PUMP_FUN_PROGRAM,
    WRAPPED_SOL,
    get_api_key_from_env,
)
from .registry import is_valid_solana_address

EXIT_OK = 0
EXIT_BLOCKED = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "rugfarm.db"
DEFAULT_LIMIT = 50

MISSING_KEY_MESSAGE = (
    "ERROR: HELIUS_API_KEY is not set.\n"
    "The chain index reads raw transactions through the Helius RPC, "
    "so it cannot run without a key. Set HELIUS_API_KEY locally "
    "(cp .env.example .env) or as the HELIUS_API_KEY GitHub secret. "
    "No data was written."
)


@dataclass
class IndexReport:
    """What one indexing run did (all counts are real, from the DB)."""

    target: str
    kind: str  # "mint" | "wallet"
    signatures_seen: int = 0
    transactions_parsed: int = 0
    transactions_skipped: int = 0
    mints_upserted: int = 0
    transfers_recorded: int = 0
    funding_edges_recorded: int = 0

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "kind": self.kind,
            "signatures_seen": self.signatures_seen,
            "transactions_parsed": self.transactions_parsed,
            "transactions_skipped": self.transactions_skipped,
            "mints_upserted": self.mints_upserted,
            "transfers_recorded": self.transfers_recorded,
            "funding_edges_recorded": self.funding_edges_recorded,
        }


def parse_token_transfers(tx: dict) -> list[dict]:
    """Token transfers in one parsed transaction, from balance diffs.

    Compares ``meta.preTokenBalances`` with ``meta.postTokenBalances``
    per (account, mint): a positive diff is a buy (the wallet
    received tokens), a negative diff is a sell. WSOL is ignored —
    it is the payment leg, not a token position. Returns
    [{"mint", "wallet", "side", "amount", "slot", "signature"}].
    A balance that cannot be read is skipped, never guessed.
    """
    if not isinstance(tx, dict):
        return []
    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    def _balance_map(balances: list) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for bal in balances:
            if not isinstance(bal, dict):
                continue
            mint = bal.get("mint")
            owner = bal.get("owner")
            amount = (bal.get("uiTokenAmount") or {}).get("uiAmount")
            if not mint or not owner or not isinstance(amount, (int, float)):
                continue
            out[(owner, mint)] = float(amount)
        return out

    pre_map = _balance_map(pre)
    post_map = _balance_map(post)
    slot = tx.get("slot")
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    signature = sigs[0] if sigs else None

    transfers: list[dict] = []
    for (owner, mint), post_amount in post_map.items():
        if mint == WRAPPED_SOL:
            continue
        diff = post_amount - pre_map.get((owner, mint), 0.0)
        if diff == 0:
            continue
        transfers.append(
            {
                "mint": mint,
                "wallet": owner,
                "side": "buy" if diff > 0 else "sell",
                "amount": abs(diff),
                "slot": slot if isinstance(slot, int) else None,
                "signature": signature,
            }
        )
    return transfers


def parse_funding_transfers(tx: dict) -> list[dict]:
    """Native SOL transfers in one parsed transaction.

    Parsed ``system transfer`` instructions only; the indexer
    already does this shape in ``fetch_funding_transfers`` — this
    is the same parsing, extracted so the chain index can store
    the edges itself. Returns [{"from", "to", "amount_lamports",
    "signature"}].
    """
    if not isinstance(tx, dict):
        return []
    message = (tx.get("transaction") or {}).get("message") or {}
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    signature = sigs[0] if sigs else None
    edges: list[dict] = []
    for instr in message.get("instructions") or []:
        if not isinstance(instr, dict):
            continue
        parsed = instr.get("parsed") or {}
        if parsed.get("type") != "transfer":
            continue
        info = parsed.get("info") or {}
        src, dst = info.get("source"), info.get("destination")
        if not src or not dst or src == dst:
            continue
        edges.append(
            {
                "from": src,
                "to": dst,
                "amount_lamports": int(info.get("lamports") or 0),
                "signature": signature,
            }
        )
    return edges


def parse_creation(tx: dict):
    """A TokenCreation only when the pump.fun create instruction is present.

    ``HeliusIndexer.parse_transaction`` falls back to a balance-diff
    heuristic, which would falsely call a wallet's first buy of a
    mint a "creation". A creation is only real when the create
    instruction is in the transaction — everything else is a
    transfer, not a launch.
    """
    if not isinstance(tx, dict):
        return None
    message = (tx.get("transaction") or {}).get("message") or {}
    for instr in message.get("instructions") or []:
        if not isinstance(instr, dict):
            continue
        if instr.get("programId") != PUMP_FUN_PROGRAM:
            continue
        parsed = instr.get("parsed") or {}
        if parsed.get("type") == "create":
            return HeliusIndexer.parse_transaction(tx)
    return None


class ChainIndexer:
    """Index raw transactions for one mint or one wallet into SQLite.

    ``indexer`` is any object with ``get_signatures`` and
    ``get_transaction`` (a real ``HeliusIndexer`` by default, a
    stub in tests). The database connection is owned by the caller
    and is never closed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        indexer: HeliusIndexer | None = None,
    ):
        if indexer is not None:
            self._indexer = indexer
            self.api_key = getattr(indexer, "api_key", None)
        else:
            self.api_key = (
                api_key if api_key is not None else get_api_key_from_env()
            )
            self._indexer = HeliusIndexer(api_key=self.api_key, client=client)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError(MISSING_KEY_MESSAGE)

    def _fetch_transactions(
        self, address: str, limit: int
    ) -> tuple[list[dict], int]:
        """Parsed transactions for an address, newest first.

        Returns (transactions, signatures_seen). A single failed
        transaction fetch is skipped, not fatal — the RPC listing
        itself failing is fatal.
        """
        signatures = self._indexer.get_signatures(address, limit=limit)
        seen = len(signatures)
        transactions: list[dict] = []
        for entry in signatures:
            sig = entry.get("signature") if isinstance(entry, dict) else None
            if not sig:
                continue
            try:
                tx = self._indexer.get_transaction(sig)
            except Exception:
                continue
            if isinstance(tx, dict):
                transactions.append(tx)
        return transactions, seen

    def index_mint(
        self, mint: str, conn, limit: int = DEFAULT_LIMIT
    ) -> IndexReport:
        """Index a mint's transactions: creation, transfers, funding."""
        self._require_enabled()
        if not is_valid_solana_address(mint):
            raise ValueError(f"'{mint}' is not a valid Solana address")
        report = IndexReport(target=mint, kind="mint")
        transactions, seen = self._fetch_transactions(mint, limit)
        report.signatures_seen = seen
        for tx in transactions:
            creation = parse_creation(tx)
            if creation is not None and creation.mint == mint:
                db_mod.upsert_mint(
                    conn,
                    creation.mint,
                    creator=creation.dev_wallet,
                    created_at=creation.timestamp,
                )
                db_mod.upsert_dev(conn, creation.dev_wallet)
                report.mints_upserted += 1
            parsed = 0
            for transfer in parse_token_transfers(tx):
                if transfer["mint"] != mint:
                    continue
                if db_mod.record_transfer(
                    conn,
                    transfer["mint"],
                    transfer["wallet"],
                    transfer["side"],
                    transfer["amount"],
                    slot=transfer["slot"],
                    signature=transfer["signature"],
                ):
                    report.transfers_recorded += 1
                parsed += 1
            for edge in parse_funding_transfers(tx):
                if db_mod.record_funding_edge(
                    conn, edge["from"], edge["to"], edge["amount_lamports"]
                ):
                    report.funding_edges_recorded += 1
            if creation is not None or parsed:
                report.transactions_parsed += 1
            else:
                report.transactions_skipped += 1
        return report

    def index_wallet(
        self, wallet: str, conn, limit: int = DEFAULT_LIMIT
    ) -> IndexReport:
        """Index a wallet's transactions: creations, funding edges."""
        self._require_enabled()
        if not is_valid_solana_address(wallet):
            raise ValueError(f"'{wallet}' is not a valid Solana address")
        report = IndexReport(target=wallet, kind="wallet")
        transactions, seen = self._fetch_transactions(wallet, limit)
        report.signatures_seen = seen
        for tx in transactions:
            creation = parse_creation(tx)
            if creation is not None and creation.dev_wallet == wallet:
                db_mod.upsert_mint(
                    conn,
                    creation.mint,
                    creator=creation.dev_wallet,
                    created_at=creation.timestamp,
                )
                db_mod.upsert_dev(conn, creation.dev_wallet)
                report.mints_upserted += 1
            parsed = 0
            for edge in parse_funding_transfers(tx):
                if db_mod.record_funding_edge(
                    conn, edge["from"], edge["to"], edge["amount_lamports"]
                ):
                    report.funding_edges_recorded += 1
                parsed += 1
            for transfer in parse_token_transfers(tx):
                if db_mod.record_transfer(
                    conn,
                    transfer["mint"],
                    transfer["wallet"],
                    transfer["side"],
                    transfer["amount"],
                    slot=transfer["slot"],
                    signature=transfer["signature"],
                ):
                    report.transfers_recorded += 1
                parsed += 1
            if creation is not None or parsed:
                report.transactions_parsed += 1
            else:
                report.transactions_skipped += 1
        return report


def main(argv: list[str] | None = None) -> int:
    """Chain index CLI.

    ``python -m src.chain_index --mint <address> [--db <path>] [--limit 50]``
    ``python -m src.chain_index --wallet <address> [--db <path>] [--limit 50]``
    Exit 0 = done, 2 = no key / invalid address / invalid args.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm chain index: index one mint or wallet into SQLite."
    )
    parser.add_argument("--mint", type=str, default=None, help="Mint address to index")
    parser.add_argument("--wallet", type=str, default=None, help="Wallet address to index")
    parser.add_argument(
        "--db", type=str, default=str(DEFAULT_DB_PATH), help="SQLite database path"
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="Max signatures to fetch"
    )
    args = parser.parse_args(argv)

    if bool(args.mint) == bool(args.wallet):
        print("ERROR: give exactly one of --mint or --wallet.", file=sys.stderr)
        return EXIT_BLOCKED
    target = args.mint or args.wallet
    if not is_valid_solana_address(target):
        print(f"ERROR: '{target}' is not a valid Solana address.", file=sys.stderr)
        return EXIT_BLOCKED
    if args.limit <= 0:
        print("ERROR: --limit must be a positive integer.", file=sys.stderr)
        return EXIT_BLOCKED

    indexer = ChainIndexer()
    if not indexer.enabled:
        print(MISSING_KEY_MESSAGE, file=sys.stderr)
        return EXIT_BLOCKED
    conn = db_mod.init_db(args.db)
    try:
        if args.mint:
            report = indexer.index_mint(args.mint, conn, limit=args.limit)
        else:
            report = indexer.index_wallet(args.wallet, conn, limit=args.limit)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    finally:
        conn.close()
    print(
        f"Indexed {report.kind} {report.target}: "
        f"{report.signatures_seen} signatures, "
        f"{report.transactions_parsed} parsed, "
        f"{report.transactions_skipped} skipped, "
        f"{report.mints_upserted} mint(s), "
        f"{report.transfers_recorded} new transfer(s), "
        f"{report.funding_edges_recorded} new funding edge(s)."
    )
    print(f"Database: {args.db}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
