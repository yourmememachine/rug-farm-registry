"""Tests for src.chain_index — the registry's own chain index (v2.14).

Synthetic data only; the RPC layer is a stub, no network.
"""
import pytest

from src import db as db_mod
from src.chain_index import (
    ChainIndexer,
    IndexReport,
    main,
    parse_funding_transfers,
    parse_token_transfers,
)
from src.indexer import PUMP_FUN_PROGRAM, WRAPPED_SOL

MINT = "9oJk7eMccQchx3evoN5C5pKQD5Xodb4C1qkhemjjpump"  # POOR
DEV = "7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk"
FUNDER = "G4JXShDM6NqP1XpMo3gpY24wxPj8NVRqqKMTKv5gLNfY"
BUYER = "3gHrfi6t4xG2KEH4gga9HxLRYqws9iTKdkLG55NwaTZJ"
OTHER = "H6hPTUg73GDscH4Q9NAviJrUtMwUXE6PzZat2xWDszrJ"


def _create_tx(sig, mint=MINT, dev=DEV, block_time=1763050000):
    return {
        "slot": 100,
        "blockTime": block_time,
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": dev}],
                "instructions": [
                    {
                        "programId": PUMP_FUN_PROGRAM,
                        "parsed": {"type": "create", "info": {"mint": mint}},
                        "accounts": [mint],
                    }
                ],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
    }


def _transfer_tx(sig, mint=MINT, buyer=BUYER, amount=1_000_000.0, slot=101):
    return {
        "slot": slot,
        "blockTime": 1763050100,
        "transaction": {
            "signatures": [sig],
            "message": {"accountKeys": [{"pubkey": buyer}], "instructions": []},
        },
        "meta": {
            "preTokenBalances": [],
            "postTokenBalances": [
                {
                    "mint": mint,
                    "owner": buyer,
                    "uiTokenAmount": {"uiAmount": amount},
                }
            ],
        },
    }


def _funding_tx(sig, src=FUNDER, dst=DEV, lamports=3_000_000_000):
    return {
        "slot": 102,
        "blockTime": 1763050200,
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": src}],
                "instructions": [
                    {
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": src,
                                "destination": dst,
                                "lamports": lamports,
                            },
                        }
                    }
                ],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
    }


class FakeIndexer:
    """Stub HeliusIndexer: signatures and transactions from dicts."""

    api_key = "FAKEKEY"

    def __init__(self, txs_by_sig, sigs_by_addr):
        self._txs = txs_by_sig
        self._sigs = sigs_by_addr

    def get_signatures(self, address, limit=50, before=None):
        return [{"signature": s} for s in self._sigs.get(address, [])[:limit]]

    def get_transaction(self, signature):
        return self._txs.get(signature)


@pytest.fixture
def conn(tmp_path):
    connection = db_mod.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def test_parse_token_transfers_buy_and_sell():
    tx = {
        "slot": 5,
        "transaction": {"signatures": ["sig-x"]},
        "meta": {
            "preTokenBalances": [
                {"mint": MINT, "owner": BUYER, "uiTokenAmount": {"uiAmount": 100.0}},
                {"mint": MINT, "owner": OTHER, "uiTokenAmount": {"uiAmount": 50.0}},
            ],
            "postTokenBalances": [
                {"mint": MINT, "owner": BUYER, "uiTokenAmount": {"uiAmount": 250.0}},
                {"mint": MINT, "owner": OTHER, "uiTokenAmount": {"uiAmount": 10.0}},
                # WSOL is the payment leg, not a token position.
                {"mint": WRAPPED_SOL, "owner": BUYER, "uiTokenAmount": {"uiAmount": 1.0}},
            ],
        },
    }
    transfers = parse_token_transfers(tx)
    assert len(transfers) == 2
    by_wallet = {t["wallet"]: t for t in transfers}
    assert by_wallet[BUYER]["side"] == "buy"
    assert by_wallet[BUYER]["amount"] == 150.0
    assert by_wallet[OTHER]["side"] == "sell"
    assert by_wallet[OTHER]["amount"] == 40.0
    assert all(t["signature"] == "sig-x" for t in transfers)


def test_parse_token_transfers_skips_unreadable_balances():
    assert parse_token_transfers({}) == []
    assert parse_token_transfers({"meta": {}}) == []


def test_parse_funding_transfers():
    tx = _funding_tx("sig-f")
    edges = parse_funding_transfers(tx)
    assert edges == [
        {
            "from": FUNDER,
            "to": DEV,
            "amount_lamports": 3_000_000_000,
            "signature": "sig-f",
        }
    ]
    assert parse_funding_transfers({}) == []


def test_parse_creation_needs_the_create_instruction():
    # A first buy has the mint in post balances only — the balance-diff
    # fallback would call it a creation. The chain index must not.
    from src.chain_index import parse_creation

    assert parse_creation(_transfer_tx("sig-buy")) is None
    creation = parse_creation(_create_tx("sig-create"))
    assert creation is not None
    assert creation.mint == MINT
    assert creation.dev_wallet == DEV


def test_index_mint_writes_mint_dev_transfers_and_edges(conn):
    fake = FakeIndexer(
        {
            "sig-create": _create_tx("sig-create"),
            "sig-buy": _transfer_tx("sig-buy"),
            "sig-fund": _funding_tx("sig-fund"),
        },
        {MINT: ["sig-create", "sig-buy", "sig-fund"]},
    )
    report = ChainIndexer(indexer=fake).index_mint(MINT, conn)
    assert isinstance(report, IndexReport)
    assert report.signatures_seen == 3
    assert report.mints_upserted == 1
    assert report.transfers_recorded == 1
    assert report.funding_edges_recorded == 1
    assert db_mod.get_mint(conn, MINT)["creator"] == DEV
    assert db_mod.get_dev(conn, DEV) is not None
    assert db_mod.list_holders(conn, MINT)[0]["wallet"] == BUYER


def test_index_mint_is_idempotent(conn):
    fake = FakeIndexer(
        {"sig-create": _create_tx("sig-create"), "sig-buy": _transfer_tx("sig-buy")},
        {MINT: ["sig-create", "sig-buy"]},
    )
    indexer = ChainIndexer(indexer=fake)
    first = indexer.index_mint(MINT, conn)
    second = indexer.index_mint(MINT, conn)
    assert first.transfers_recorded == 1
    assert second.transfers_recorded == 0  # same signatures, deduped
    assert db_mod.count_table(conn, "transfers") == 1


def test_index_wallet_writes_funding_edges_and_creations(conn):
    fake = FakeIndexer(
        {
            "sig-create": _create_tx("sig-create"),
            "sig-fund": _funding_tx("sig-fund"),
        },
        {DEV: ["sig-create", "sig-fund"]},
    )
    report = ChainIndexer(indexer=fake).index_wallet(DEV, conn)
    assert report.mints_upserted == 1
    assert report.funding_edges_recorded == 1
    assert db_mod.get_mint(conn, MINT)["creator"] == DEV


def test_index_mint_skips_unparseable_transactions(conn):
    fake = FakeIndexer(
        {"sig-bad": {"not": "a transaction"}},
        {MINT: ["sig-bad"]},
    )
    report = ChainIndexer(indexer=fake).index_mint(MINT, conn)
    assert report.transactions_parsed == 0
    assert report.transactions_skipped == 1
    assert db_mod.count_table(conn, "transfers") == 0


def test_index_mint_rejects_bad_address(conn):
    with pytest.raises(ValueError):
        ChainIndexer(indexer=FakeIndexer({}, {})).index_mint("not-an-address", conn)


def test_no_key_raises_naming_helius_key(conn, monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    indexer = ChainIndexer(api_key="")
    with pytest.raises(RuntimeError, match="HELIUS_API_KEY"):
        indexer.index_mint(MINT, conn)


def test_cli_rejects_both_mint_and_wallet(tmp_path):
    assert main(["--mint", MINT, "--wallet", DEV, "--db", str(tmp_path / "t.db")]) == 2


def test_cli_rejects_bad_address(tmp_path):
    assert main(["--mint", "nope", "--db", str(tmp_path / "t.db")]) == 2


def test_cli_rejects_zero_limit(tmp_path):
    assert main(["--mint", MINT, "--limit", "0", "--db", str(tmp_path / "t.db")]) == 2


def test_cli_no_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert main(["--mint", MINT, "--db", str(tmp_path / "t.db")]) == 2
