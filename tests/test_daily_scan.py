"""Daily-scan tests. The indexer is faked — no network, no real key.
Proves the >= 2 rule, the no-invention rule, and the missing-key exit."""
from datetime import date

import pytest

from src.daily_scan import MISSING_KEY_MESSAGE, main, render_alerts, scan
from src.indexer import MintOutcome, TokenCreation
from src.registry import add_or_update_entry, get_wallet

TODAY = date(2026, 11, 12)
DEV = "RUGDEV111111111111111111111111111111111"
DEV2 = "RUGDEV222222222222222222222222222222222"
MINT1 = "RUGMNT111111111111111111111111111111111"
MINT2 = "RUGMNT222222222222222222222222222222222"
MINT3 = "RUGMNT333333333333333333333333333333333"


def _rugged_outcome(mint):
    return MintOutcome(
        mint=mint, dev_wallet=DEV,
        peak_market_cap_usd=100000, current_market_cap_usd=5000,
        lp_removed=True, dev_sold_pct=80.0, time_to_collapse_minutes=30,
        evidence={
            "solscan_mint": f"https://solscan.io/token/{mint}",
            "dexscreener": f"https://dexscreener.com/solana/{mint}",
        },
    )


def _honest_outcome(mint):
    return MintOutcome(
        mint=mint, dev_wallet=DEV,
        peak_market_cap_usd=100000, current_market_cap_usd=20000,
        lp_removed=False, dev_sold_pct=5.0, time_to_collapse_minutes=60,
        evidence={"solscan_mint": f"https://solscan.io/token/{mint}"},
    )


class FakeIndexer:
    def __init__(self, creations, outcomes):
        self._creations = creations
        self._outcomes = outcomes

    def fetch_new_mints(self, limit=50):
        return self._creations[:limit]

    def fetch_mint_outcome(self, mint, dev_wallet=None):
        return self._outcomes[mint]

    def fetch_funding_transfers(self, wallets, limit_per_wallet=20):
        return []


def _fresh_registry():
    return {"version": 1, "sample_data": False, "min_rugged_mints_to_flag": 2, "entries": []}


def test_scan_adds_dev_with_two_rugged_mints():
    creations = [
        TokenCreation(MINT1, DEV, "pump.fun"),
        TokenCreation(MINT2, DEV, "pump.fun"),
    ]
    indexer = FakeIndexer(creations, {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)})
    data = _fresh_registry()
    result = scan(indexer, data, TODAY)
    assert result.new_entries == [DEV]
    assert result.registry_changed is True
    entry = get_wallet(data, DEV)
    assert entry is not None and entry["status"] == "flagged"
    assert len(entry["rugged_mints"]) == 2
    assert "No new verified alerts" not in render_alerts(result)
    assert DEV in render_alerts(result)


def test_scan_does_not_flag_single_rug():
    creations = [TokenCreation(MINT1, DEV, "pump.fun")]
    indexer = FakeIndexer(creations, {MINT1: _rugged_outcome(MINT1)})
    data = _fresh_registry()
    result = scan(indexer, data, TODAY)
    assert result.new_entries == []
    assert result.under_review == [DEV]
    assert get_wallet(data, DEV) is None
    assert result.registry_changed is False
    assert "No new verified alerts" in render_alerts(result)


def test_scan_ignores_honest_failures():
    creations = [TokenCreation(MINT1, DEV, "pump.fun"), TokenCreation(MINT2, DEV, "pump.fun")]
    indexer = FakeIndexer(creations, {MINT1: _honest_outcome(MINT1), MINT2: _honest_outcome(MINT2)})
    data = _fresh_registry()
    result = scan(indexer, data, TODAY)
    assert result.new_entries == []
    assert get_wallet(data, DEV) is None


def test_scan_promotes_existing_under_review_entry():
    data = _fresh_registry()
    existing = {
        "wallet": DEV, "cluster_id": "cluster-abc", "status": "under_review",
        "first_seen": "2026-11-01", "last_seen": "2026-11-01", "ttl_days": 180,
        "rugged_mints": [{
            "mint": MINT1, "outcome": "rugged", "peak_market_cap_usd": 100000,
            "current_market_cap_usd": 5000,
            "evidence": {"solscan_mint": f"https://solscan.io/token/{MINT1}"},
        }],
        "dispute_notes": "",
    }
    changed, _ = add_or_update_entry(data, existing, today=TODAY)
    assert changed
    creations = [TokenCreation(MINT2, DEV, "pump.fun")]
    indexer = FakeIndexer(creations, {MINT2: _rugged_outcome(MINT2)})
    result = scan(indexer, data, TODAY)
    assert result.updated_entries == [DEV]
    entry = get_wallet(data, DEV)
    assert entry["status"] == "flagged"
    assert len(entry["rugged_mints"]) == 2


def test_scan_does_not_double_count_same_mint():
    creations = [TokenCreation(MINT1, DEV, "pump.fun"), TokenCreation(MINT1, DEV, "pump.fun")]
    indexer = FakeIndexer(creations, {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)})
    data = _fresh_registry()
    result = scan(indexer, data, TODAY)
    # Same mint twice = still only one distinct rug -> not flagged.
    assert result.new_entries == []
    assert result.under_review == [DEV]


def test_main_exits_2_without_key_and_changes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    registry = tmp_path / "registry.json"
    registry.write_text('{"version": 1, "sample_data": false, "min_rugged_mints_to_flag": 2, "entries": []}')
    alerts = tmp_path / "alerts"
    code = main(["--registry", str(registry), "--alerts-dir", str(alerts), "--date", "2026-11-12"])
    assert code == 2
    assert "HELIUS_API_KEY" in capsys.readouterr().err
    assert registry.read_text().count("SAMPLE") == 0
    assert not alerts.exists()
