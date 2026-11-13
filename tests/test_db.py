"""Tests for src.db — SQLite storage (v2.14).

Synthetic data only; every test uses a fresh temp database.
"""
import sqlite3

import pytest

from src import db

MINT = "9oJk7eMccQchx3evoN5C5pKQD5Xodb4C1qkhemjjpump"  # POOR
DEV = "7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk"
FUNDER = "G4JXShDM6NqP1XpMo3gpY24wxPj8NVRqqKMTKv5gLNfY"


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def test_init_creates_current_schema(conn):
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for expected in (
        "mints",
        "devs",
        "transfers",
        "funding_edges",
        "outcomes",
        "alerts",
        "model_runs",
    ):
        assert expected in tables


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_schema_version_fresh_db_is_zero(tmp_path):
    fresh = sqlite3.connect(str(tmp_path / "empty.db"))
    assert db.schema_version(fresh) == 0
    fresh.close()


def test_upsert_mint_fills_fields_without_overwriting(conn):
    db.upsert_mint(conn, MINT, creator=DEV)
    row = db.get_mint(conn, MINT)
    assert row["creator"] == DEV
    assert row["name"] is None
    first_seen = row["first_seen"]
    db.upsert_mint(conn, MINT, name="Poor Coin", symbol="POOR")
    row = db.get_mint(conn, MINT)
    assert row["name"] == "Poor Coin"
    assert row["symbol"] == "POOR"
    assert row["creator"] == DEV  # kept
    assert row["first_seen"] == first_seen  # never moved


def test_upsert_dev_counts_are_absolute_and_kept(conn):
    db.upsert_dev(conn, DEV)
    assert db.get_dev(conn, DEV)["launches"] == 0
    db.upsert_dev(conn, DEV, launches=7458, rugged_count=0, verdict="unknown")
    row = db.get_dev(conn, DEV)
    assert row["launches"] == 7458
    assert row["verdict"] == "unknown"
    db.upsert_dev(conn, DEV, verdict="under_review")  # no counts given
    row = db.get_dev(conn, DEV)
    assert row["launches"] == 7458  # not overwritten with 0
    assert row["rugged_count"] == 0
    assert row["verdict"] == "under_review"


def test_record_transfer_dedups_by_signature(conn):
    assert db.record_transfer(conn, MINT, DEV, "buy", 100.0, signature="sig-1")
    assert not db.record_transfer(conn, MINT, DEV, "buy", 100.0, signature="sig-1")
    assert db.count_table(conn, "transfers") == 1


def test_record_transfer_without_signature_is_always_recorded(conn):
    assert db.record_transfer(conn, MINT, DEV, "buy", 5.0)
    assert db.record_transfer(conn, MINT, DEV, "buy", 5.0)
    assert db.count_table(conn, "transfers") == 2


def test_record_transfer_validates_side_and_amount(conn):
    with pytest.raises(ValueError):
        db.record_transfer(conn, MINT, DEV, "hold", 1.0)
    with pytest.raises(ValueError):
        db.record_transfer(conn, MINT, DEV, "buy", -1.0)


def test_record_funding_edge_dedups(conn):
    assert db.record_funding_edge(conn, FUNDER, DEV, 3_000_000_000)
    assert not db.record_funding_edge(conn, FUNDER, DEV, 3_000_000_000)
    assert db.count_table(conn, "funding_edges") == 1
    # Self-funding is not an edge.
    assert not db.record_funding_edge(conn, DEV, DEV, 100)


def test_record_outcome_replaces(conn):
    db.record_outcome(conn, MINT, "inconclusive", rule_version="real-v1")
    assert db.get_outcome(conn, MINT)["label"] == "inconclusive"
    db.record_outcome(conn, MINT, "rugged", rule_version="real-v2")
    row = db.get_outcome(conn, MINT)
    assert row["label"] == "rugged"
    assert row["rule_version"] == "real-v2"
    assert db.count_table(conn, "outcomes") == 1


def test_record_outcome_rejects_unknown_label(conn):
    with pytest.raises(ValueError):
        db.record_outcome(conn, MINT, "probably_rugged")


def test_record_alert_never_realerts_same_mint(conn):
    assert db.record_alert(conn, MINT, DEV, "flagged")
    assert not db.record_alert(conn, MINT, DEV, "flagged")
    assert db.has_alert(conn, MINT)
    assert db.has_alert(conn, MINT, "flagged")
    assert not db.has_alert(conn, MINT, "under_review")
    assert db.count_table(conn, "alerts") == 1


def test_holders_view_derives_net_balances(conn):
    db.record_transfer(conn, MINT, "WalletA111111111111111111111111111111", "buy", 100.0, signature="s1")
    db.record_transfer(conn, MINT, "WalletA111111111111111111111111111111", "sell", 40.0, signature="s2")
    db.record_transfer(conn, MINT, "WalletB111111111111111111111111111111", "buy", 10.0, signature="s3")
    db.record_transfer(conn, MINT, "WalletB111111111111111111111111111111", "sell", 10.0, signature="s4")
    holders = db.list_holders(conn, MINT)
    assert len(holders) == 1  # B netted to zero — not a holder
    assert holders[0]["wallet"] == "WalletA111111111111111111111111111111"
    assert holders[0]["net_amount"] == 60.0


def test_list_flagged_devs_and_model_runs(conn):
    db.upsert_dev(conn, DEV, verdict="flagged")
    db.upsert_dev(conn, FUNDER, verdict="unknown")
    flagged = db.list_flagged_devs(conn)
    assert [d["wallet"] for d in flagged] == [DEV]
    db.record_model_run(conn, "ml-abc123", "real-v1", 20, metrics_json="{}")
    runs = db.list_model_runs(conn)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "ml-abc123"
    # Same run id replaces, never duplicates.
    db.record_model_run(conn, "ml-abc123", "real-v2", 25, metrics_json="{}")
    assert db.count_table(conn, "model_runs") == 1
    assert db.list_model_runs(conn)[0]["rule_version"] == "real-v2"


def test_count_table_rejects_unknown_table(conn):
    with pytest.raises(ValueError):
        db.count_table(conn, "registry")
