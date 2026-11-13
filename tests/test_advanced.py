"""Tests for src/advanced.py — v2.5. Synthetic data only, no network."""

import json

import pytest

from src.advanced import (
    FundingEvent,
    StealthClusterDetector,
    detect_stealth_clusters,
    main,
    outcome_accuracy,
    record_outcome,
    simulate_exit,
    wash_score,
)

LAUNCH = 1_700_000_000
DAY = 24 * 60 * 60
HOUR = 60 * 60


def _event(wallet, funder=None, funded=None, bought=None, launch=LAUNCH):
    return FundingEvent(
        wallet=wallet,
        funder=funder,
        amount_sol=1.0,
        funded_at=funded,
        bought_at=bought,
        launch_at=launch,
    )


# --- StealthClusterDetector ----------------------------------------------

def test_stealth_prefunded_cluster():
    events = [
        _event("WalletA111", "FunderX", funded=LAUNCH - 3 * DAY, bought=LAUNCH + 2 * HOUR),
        _event("WalletB222", "FunderX", funded=LAUNCH - 3 * DAY, bought=LAUNCH + 2 * HOUR),
    ]
    clusters = detect_stealth_clusters(events)
    assert len(clusters) == 1
    c = clusters[0]
    assert set(c.members) == {"WalletA111", "WalletB222"}
    assert "shared_funder" in c.signals
    assert "prefunded" in c.signals
    assert "delayed_buy" in c.signals
    assert c.funders == ["FunderX"]
    assert c.evidence


def test_stealth_delayed_buys_only():
    events = [
        _event("WalletA111", "FunderX", funded=LAUNCH, bought=LAUNCH + 3 * HOUR),
        _event("WalletB222", "FunderX", funded=LAUNCH, bought=LAUNCH + 3 * HOUR),
    ]
    clusters = detect_stealth_clusters(events)
    assert len(clusters) == 1
    assert "delayed_buy" in clusters[0].signals
    assert "prefunded" not in clusters[0].signals


def test_stealth_two_hop_funding_chain():
    # FunderA funds WalletB; WalletB funds WalletC — a 2-hop chain
    # no same-block check can see.
    events = [
        _event("WalletB222", "FunderA", funded=LAUNCH - 2 * DAY, bought=LAUNCH + 2 * HOUR),
        _event("WalletC333", "WalletB222", funded=LAUNCH - 2 * DAY, bought=LAUNCH + 2 * HOUR),
    ]
    clusters = detect_stealth_clusters(events)
    assert len(clusters) == 1
    assert "two_hop" in clusters[0].signals
    assert set(clusters[0].members) == {"WalletB222", "WalletC333"}


def test_stealth_no_cluster_for_unrelated_wallets():
    events = [
        _event("WalletA111", "FunderX", funded=LAUNCH, bought=LAUNCH),
        _event("WalletB222", "FunderY", funded=LAUNCH, bought=LAUNCH),
    ]
    assert detect_stealth_clusters(events) == []


def test_stealth_same_block_style_buys_not_flagged():
    # Funded just before launch, bought at launch: exactly what a
    # same-block bundler check would catch — and what this detector
    # does NOT need to flag on its own.
    events = [_event("WalletA111", "FunderX", funded=LAUNCH - HOUR, bought=LAUNCH)]
    assert detect_stealth_clusters(events) == []


def test_stealth_lone_prefunded_delayed_wallet():
    events = [_event("WalletA111", "FunderX", funded=LAUNCH - 3 * DAY, bought=LAUNCH + 5 * HOUR)]
    clusters = detect_stealth_clusters(events)
    assert len(clusters) == 1
    assert clusters[0].members == ["WalletA111"]


def test_stealth_lone_prefunded_only_not_a_cluster():
    events = [_event("WalletA111", "FunderX", funded=LAUNCH - 3 * DAY, bought=LAUNCH)]
    assert detect_stealth_clusters(events) == []


def test_stealth_unknown_funder_never_invented():
    events = [
        _event("WalletA111", None, funded=LAUNCH, bought=LAUNCH),
        _event("WalletB222", None, funded=LAUNCH, bought=LAUNCH),
    ]
    assert detect_stealth_clusters(events) == []
    assert detect_stealth_clusters([]) == []
    assert StealthClusterDetector().detect([]) == []


# --- ExitSimulator ---------------------------------------------------------

def test_exit_small_sell_low_impact():
    r = simulate_exit(1_000_000, 50_000, 1_000)
    assert r.reliable is True
    # pool side 25,000: 1000 * 25000 / 26000 = 961.54
    assert r.estimated_received_usd == pytest.approx(961.54, abs=0.01)
    assert r.price_impact_pct == pytest.approx(3.85, abs=0.01)
    assert r.slippage_pct == r.price_impact_pct


def test_exit_large_sell_severe_impact():
    r = simulate_exit(1_000_000, 50_000, 25_000)
    assert r.estimated_received_usd == pytest.approx(12_500.0, abs=0.01)
    assert r.price_impact_pct == pytest.approx(50.0, abs=0.01)
    assert any("Severe" in reason for reason in r.reasons)


def test_exit_unreliable_missing_liquidity():
    for liq in (None, 0, -100):
        r = simulate_exit(1_000_000, liq, 1_000)
        assert r.reliable is False
        assert r.estimated_received_usd is None
        assert r.price_impact_pct is None
        assert "missing" in r.reasons[0].lower()


def test_exit_invalid_size():
    for size in (0, -5):
        with pytest.raises(ValueError):
            simulate_exit(1_000_000, 50_000, size)


def test_exit_low_volume_warning():
    r = simulate_exit(1_000_000, 50_000, 1_000, volume_24h_usd=1_500)
    assert any("volume" in reason.lower() for reason in r.reasons)


# --- WashScore -------------------------------------------------------------

def test_wash_high_concentration():
    r = wash_score(120, [0.6, 0.1, 0.05, 0.03, 0.02, 0.02, 0.01, 0.01, 0.005, 0.005])
    assert r.level == "high"
    assert r.score is not None and r.score >= 60
    assert any("Top holder" in reason for reason in r.reasons)


def test_wash_low_concentration():
    r = wash_score(5_000, [0.02] * 10)
    assert r.level == "low"
    assert r.score == 0
    assert r.insufficient_data is False


def test_wash_insufficient_data():
    r = wash_score(None, None)
    assert r.insufficient_data is True
    assert r.score is None
    assert r.level == "insufficient_data"
    r2 = wash_score(100, [])
    assert r2.insufficient_data is True


def test_wash_percent_inputs_normalised():
    r = wash_score(100, [40, 20, 10])
    assert r.level in ("medium", "high")
    assert r.score is not None and r.score > 0


def test_wash_volume_anomaly():
    r = wash_score(20, [0.05] * 10, volume_24h_usd=1_000_000, market_cap_usd=100_000)
    assert any("washed" in reason for reason in r.reasons)


def test_wash_top1_only():
    r = wash_score(None, None, top1_share=0.6)
    assert r.level in ("medium", "high")
    assert r.insufficient_data is False


# --- OutcomeTracker --------------------------------------------------------

def test_tracker_accuracy_counts():
    records = [
        {"mint": "M1", "verdict": "flagged", "outcome": "rugged"},
        {"mint": "M2", "verdict": "flagged", "outcome": "rugged"},
        {"mint": "M3", "verdict": "flagged", "outcome": "survived"},
        {"mint": "M4", "verdict": "not_flagged", "outcome": "rugged"},
        {"mint": "M5", "verdict": "not_flagged", "outcome": "survived"},
        {"mint": "M6", "verdict": "under_review", "outcome": "rugged"},
        {"mint": "M7", "verdict": "flagged", "outcome": "unknown"},
    ]
    stats = outcome_accuracy(records)
    assert stats["total"] == 7
    assert stats["tracked"] == 6
    assert stats["true_positives"] == 2
    assert stats["false_positives"] == 1
    assert stats["false_negatives"] == 1
    assert stats["true_negatives"] == 1
    assert stats["precision"] == pytest.approx(2 / 3)
    assert stats["recall"] == pytest.approx(2 / 3)
    assert stats["accuracy"] == pytest.approx(3 / 6)


def test_tracker_empty_no_rates():
    stats = outcome_accuracy([])
    assert stats["precision"] is None
    assert stats["recall"] is None
    assert stats["accuracy"] is None
    assert stats["total"] == 0


def test_tracker_record_roundtrip_and_refusals(tmp_path):
    path = tmp_path / "outcomes.json"
    entry = record_outcome(path, "MintABC", "flagged", "rugged", 95.0)
    assert entry["mint"] == "MintABC"
    assert entry["collapse_pct"] == 95.0
    data = json.loads(path.read_text())
    assert len(data) == 1
    with pytest.raises(ValueError):
        record_outcome(path, "MintABC", "maybe", "rugged")
    with pytest.raises(ValueError):
        record_outcome(path, "MintABC", "flagged", "maybe")
    with pytest.raises(ValueError):
        record_outcome(tmp_path / "registry.json", "MintABC", "flagged", "rugged")


# --- CLI ---------------------------------------------------------------------

def test_cli_exit_simulation(capsys):
    assert main(["--exit", "--mc", "1000000", "--liquidity", "50000", "--size", "1000"]) == 0
    assert "receive" in capsys.readouterr().out


def test_cli_exit_missing_size():
    assert main(["--exit", "--mc", "1000000", "--liquidity", "50000"]) == 2


def test_cli_exit_unreliable(capsys):
    assert main(["--exit", "--mc", "1000000", "--size", "1000"]) == 0
    assert "unreliable" in capsys.readouterr().out.lower()


def test_cli_wash_insufficient(capsys):
    assert main(["--wash", "--holders", "100"]) == 0
    assert "insufficient" in capsys.readouterr().out.lower()


def test_cli_cluster_from_file(tmp_path, capsys):
    events = [
        {"wallet": "WalletA111", "funder": "FunderX",
         "funded_at": LAUNCH - 3 * DAY, "bought_at": LAUNCH + 2 * HOUR, "launch_at": LAUNCH},
        {"wallet": "WalletB222", "funder": "FunderX",
         "funded_at": LAUNCH - 3 * DAY, "bought_at": LAUNCH + 2 * HOUR, "launch_at": LAUNCH},
    ]
    f = tmp_path / "events.json"
    f.write_text(json.dumps(events))
    assert main(["--cluster", "--funding", str(f)]) == 0
    assert "Cluster" in capsys.readouterr().out


def test_cli_cluster_missing_file():
    assert main(["--cluster", "--funding", "/nonexistent/events.json"]) == 2


def test_cli_track_records(tmp_path, capsys):
    path = tmp_path / "outcomes.json"
    assert main(["--track", "--verdict", "flagged", "--outcome", "rugged",
                 "--mint", "MintABC", "--file", str(path)]) == 0
    assert "Recorded" in capsys.readouterr().out
    assert len(json.loads(path.read_text())) == 1


def test_cli_invalid_modes():
    assert main([]) == 2
    assert main(["--exit", "--wash"]) == 2
    assert main(["--track", "--verdict", "bad", "--outcome", "rugged", "--mint", "M"]) == 2


def test_cli_json_output(capsys):
    assert main(["--exit", "--mc", "1000000", "--liquidity", "50000",
                 "--size", "1000", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reliable"] is True
    assert payload["price_impact_pct"] == pytest.approx(3.85, abs=0.01)
