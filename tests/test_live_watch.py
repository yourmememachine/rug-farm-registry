"""Tests for src.live_watch — the real-time watcher daemon (v2.14).

Synthetic data only; all backends are fakes, no network.
"""
from datetime import date

import pytest

from src import db as db_mod
from src.indexer import MintOutcome, TokenCreation
from src.live_watch import LiveWatcher, main

MINT = "9oJk7eMccQchx3evoN5C5pKQD5Xodb4C1qkhemjjpump"  # POOR
MINT2 = "7fAJ48SPtycJNLERz7G2ZDtfgwij6DhW3ArJ8BSnpump"  # BagsBack
DEV = "7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk"
DEV2 = "CGaqXYFcmjY5okNWkBPWjxQrdJ8oUcUCuSnnhVn8aTAR"
TODAY = date.today()


def _rugged_mint(mint):
    return {
        "mint": mint,
        "outcome": "rugged",
        "peak_market_cap_usd": 100000,
        "current_market_cap_usd": 1000,
        "evidence": {"solscan": f"https://solscan.io/token/{mint}"},
    }


def _registry_entry(dev, rugged_mints, status="under_review"):
    return {
        "wallet": dev,
        "cluster_id": "cluster-test",
        "status": status,
        "first_seen": TODAY.isoformat(),
        "last_seen": TODAY.isoformat(),
        "ttl_days": 180,
        "rugged_mints": rugged_mints,
    }


def _registry(entries):
    return {
        "min_rugged_mints_to_flag": 2,
        "sample_data": False,
        "entries": entries,
    }


def _rugged_outcome(mint):
    return MintOutcome(
        mint=mint,
        peak_market_cap_usd=100000.0,
        current_market_cap_usd=1000.0,
        lp_removed=True,
        dev_sold_pct=0.0,
        time_to_collapse_minutes=60,
        evidence={"solscan": f"https://solscan.io/token/{mint}"},
    )


def _flat_outcome(mint):
    return MintOutcome(
        mint=mint,
        peak_market_cap_usd=10000.0,
        current_market_cap_usd=9000.0,
        lp_removed=False,
        dev_sold_pct=0.0,
        time_to_collapse_minutes=None,
        evidence={"solscan": f"https://solscan.io/token/{mint}"},
    )


class FakeIndexer:
    """Stub HeliusIndexer with canned creations and outcomes."""

    def __init__(self, creations, outcomes, api_key="FAKEKEY", fail_fetch=False):
        self._creations = creations
        self._outcomes = outcomes
        self.api_key = api_key
        self._fail_fetch = fail_fetch

    @property
    def enabled(self):
        return bool(self.api_key)

    def fetch_new_mints(self, limit=50):
        if self._fail_fetch:
            raise RuntimeError("RPC down")
        return self._creations[:limit]

    def fetch_mint_outcome(self, mint, dev_wallet=None):
        return self._outcomes[mint]


class FakeWatch:
    """Stub PumpWatch with canned boosted entries."""

    def __init__(self, entries):
        self._entries = entries

    def watch(self, limit=10):
        return self._entries[:limit]

    def fetch_token_details(self, mints):
        return [e for e in self._entries if e["mint"] in mints]


def _watch_entry(mint, change=60.9, liquidity=15000.0, mc=50000.0):
    return {
        "mint": mint,
        "symbol": "POOR",
        "name": "Poor Coin",
        "market_cap_usd": mc,
        "liquidity_usd": liquidity,
        "volume_24h_usd": 200000.0,
        "change_24h_pct": change,
        "watch_label": "active",
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
    }


@pytest.fixture
def conn(tmp_path):
    connection = db_mod.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def _watcher(tmp_path, indexer, registry_data, watch=None):
    return LiveWatcher(
        db_path=tmp_path / "test.db",
        registry_path=tmp_path / "registry.json",
        alerts_dir=tmp_path / "alerts",
        indexer=indexer,
        watch=watch,
        registry_data=registry_data,
        today=TODAY,
    )


def test_run_once_flags_dev_crossing_threshold(conn, tmp_path):
    """1 prior rug in the registry + 1 new rugged mint = flagged."""
    prior = _rugged_mint("FtSzfXi3gG9eGBs9fs5oPNc6DcVnbBXKkd4dYBc2pump")
    registry_data = _registry([_registry_entry(DEV, [prior])])
    indexer = FakeIndexer(
        [TokenCreation(mint=MINT, dev_wallet=DEV, program="pump.fun")],
        {MINT: _rugged_outcome(MINT)},
    )
    watcher = _watcher(tmp_path, indexer, registry_data)
    report = watcher.run_once(conn=conn)
    assert report["mode"] == "helius"
    assert report["flagged"] == [MINT]
    assert report["alerts_written"] == [MINT]
    assert db_mod.has_alert(conn, MINT, "flagged")
    assert db_mod.get_outcome(conn, MINT)["label"] == "rugged"
    live_file = tmp_path / "alerts" / f"live-{TODAY.isoformat()}.md"
    assert live_file.is_file()
    assert MINT in live_file.read_text()


def test_run_once_under_review_no_alert(conn, tmp_path):
    """0 prior rugs + 1 new rugged mint = under review, never flagged."""
    registry_data = _registry([])
    indexer = FakeIndexer(
        [TokenCreation(mint=MINT, dev_wallet=DEV, program="pump.fun")],
        {MINT: _rugged_outcome(MINT)},
    )
    watcher = _watcher(tmp_path, indexer, registry_data)
    report = watcher.run_once(conn=conn)
    assert report["flagged"] == []
    assert report["under_review"] == [MINT]
    assert report["alerts_written"] == []
    assert not db_mod.has_alert(conn, MINT)
    assert db_mod.get_outcome(conn, MINT)["label"] == "rugged"


def test_run_once_non_rugged_no_alert(conn, tmp_path):
    indexer = FakeIndexer(
        [TokenCreation(mint=MINT, dev_wallet=DEV, program="pump.fun")],
        {MINT: _flat_outcome(MINT)},
    )
    watcher = _watcher(tmp_path, indexer, _registry([]))
    report = watcher.run_once(conn=conn)
    assert report["outcomes"] == {MINT: "inconclusive"}
    assert report["flagged"] == []
    assert report["alerts_written"] == []


def test_run_once_never_realerts_seen_mint(conn, tmp_path):
    prior = _rugged_mint("FtSzfXi3gG9eGBs9fs5oPNc6DcVnbBXKkd4dYBc2pump")
    registry_data = _registry([_registry_entry(DEV, [prior])])
    indexer = FakeIndexer(
        [TokenCreation(mint=MINT, dev_wallet=DEV, program="pump.fun")],
        {MINT: _rugged_outcome(MINT)},
    )
    watcher = _watcher(tmp_path, indexer, registry_data)
    first = watcher.run_once(conn=conn)
    second = watcher.run_once(conn=conn)
    assert first["alerts_written"] == [MINT]
    assert second["skipped_seen"] == 1
    assert second["alerts_written"] == []
    assert db_mod.count_table(conn, "alerts") == 1


def test_run_once_dexscreener_only_mode(conn, tmp_path):
    indexer = FakeIndexer([], {}, api_key=None)  # no key
    watch = FakeWatch([_watch_entry(MINT)])
    watcher = _watcher(tmp_path, indexer, _registry([]), watch=watch)
    report = watcher.run_once(conn=conn)
    assert report["mode"] == "dexscreener-only"
    assert any("DexScreener-only mode" in line for line in watcher.log_lines)
    assert report["outcomes"] == {MINT: "inconclusive"}
    assert report["flagged"] == []
    assert report["alerts_written"] == []
    assert db_mod.get_mint(conn, MINT) is not None


def test_run_once_fetch_failure_reports_error(conn, tmp_path):
    indexer = FakeIndexer([], {}, fail_fetch=True)
    watcher = _watcher(tmp_path, indexer, _registry([]))
    report = watcher.run_once(conn=conn)
    assert report["error"] == "RPC down"
    assert report["candidates"] == 0


def test_run_max_cycles_stops(tmp_path):
    indexer = FakeIndexer([], {})
    watcher = _watcher(tmp_path, indexer, _registry([]))
    assert watcher.run(interval=1, max_cycles=2) == 0
    assert watcher.stopped is False
    assert any("Live watch stopped." in line for line in watcher.log_lines)
    cycles = [line for line in watcher.log_lines if "Poll cycle done" in line]
    assert len(cycles) == 2


def test_request_stop(tmp_path):
    watcher = _watcher(tmp_path, FakeIndexer([], {}), _registry([]))
    assert not watcher.stopped
    watcher.request_stop()
    assert watcher.stopped


def test_write_live_alerts_file(conn, tmp_path):
    watcher = _watcher(tmp_path, FakeIndexer([], {}), _registry([]))
    db_mod.record_alert(conn, MINT, DEV, "flagged")
    path = watcher.write_live_alerts(conn)
    assert path.name == f"live-{TODAY.isoformat()}.md"
    text = path.read_text()
    assert MINT in text
    assert DEV in text


def test_cli_invalid_args_exit_2(tmp_path):
    assert main(["--interval", "0", "--db", str(tmp_path / "t.db")]) == 2
    assert main(["--limit", "0", "--db", str(tmp_path / "t.db")]) == 2


def test_cli_once_success_and_failure(tmp_path, monkeypatch):
    class StubWatcher:
        def __init__(self, *args, **kwargs):
            pass

        def run_once(self, limit=50):
            return {"error": None}

    monkeypatch.setattr("src.live_watch.LiveWatcher", StubWatcher)
    assert main(["--once", "--db", str(tmp_path / "t.db")]) == 0

    class FailingWatcher(StubWatcher):
        def run_once(self, limit=50):
            return {"error": "RPC down"}

    monkeypatch.setattr("src.live_watch.LiveWatcher", FailingWatcher)
    assert main(["--once", "--db", str(tmp_path / "t.db")]) == 1
