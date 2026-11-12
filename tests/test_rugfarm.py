"""Tests for the single-file SDK (src/rugfarm.py).

Uses the shipped sample registry with a fixed date (2026-11-12, so
TTL checks do not rot) and a stub DexScreener fetcher — no test
makes a live network call.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.rugfarm import (
    RugFarm,
    is_valid_solana_address,
    load_registry,
    main,
    watch_label,
)

TODAY = date(2026, 11, 12)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"

FLAGGED_3 = "SAMPLEDEV11111111111111111111111111111111"  # 3 rugged mints
FLAGGED_2 = "SAMPLEDEV22222222222222222222222222222222"  # 2 rugged mints
UNDER_REVIEW = "SAMPLEDEV33333333333333333333333333333333"  # 1 rugged mint
EXPIRED = "SAMPLEDEV44444444444444444444444444444444"  # last seen 2025-02-01
UNKNOWN_DEV = "UnknownDev1111111111111111111111111111"
MINT_1 = "SAMPLEMNT1111111111111111111111111111111"  # rugged, SAMPLE1, dev FLAGGED_3
MINT_2 = "SAMPLEMNT2222222222222222222222222222222"  # rugged, dev FLAGGED_3
MINT_6 = "SAMPLEMNT6666666666666666666666666666666"  # rugged, dev UNDER_REVIEW
LIVE_MINT = "LiveMint1111111111111111111111111111"


def _pair(mint, symbol="LIVE", change=-5.0, liquidity=50_000.0, mc=100_000.0):
    return {
        "baseToken": {"address": mint, "symbol": symbol, "name": f"{symbol} Coin"},
        "marketCap": mc,
        "liquidity": {"usd": liquidity},
        "volume": {"h24": 10_000.0},
        "priceChange": {"h24": change},
    }


def _fetcher_for(pairs):
    """Stub fetcher: boosts return the pair mints, tokens return the pairs."""
    by_mint = {p["baseToken"]["address"]: p for p in pairs}

    def fetch(url):
        if "token-boosts" in url:
            return [
                {"chainId": "solana", "tokenAddress": p["baseToken"]["address"]}
                for p in pairs
            ] + [{"chainId": "ethereum", "tokenAddress": "EthMint1111111111111111111111111111"}]
        if "tokens/v1/solana" in url:
            wanted = url.rsplit("/", 1)[-1].split(",")
            return [by_mint[m] for m in wanted if m in by_mint]
        raise AssertionError(f"unexpected URL: {url}")

    return fetch


def _failing_fetcher(url):
    raise RuntimeError("network down (stub)")


@pytest.fixture()
def client():
    return RugFarm(registry_path=REGISTRY_PATH, today=TODAY)


@pytest.fixture()
def live_client():
    pairs = [
        _pair("LiveMint1111111111111111111111111111", "CRASH", change=-80.0),
        _pair("LiveMint2222222222222222222222222222", "THIN", change=-5.0, liquidity=500.0),
        _pair("LiveMint3333333333333333333333333333", "OKAY", change=12.0),
    ]
    return RugFarm(
        registry_path=REGISTRY_PATH, fetcher=_fetcher_for(pairs), today=TODAY
    )


# -- flagged / under-review / expired / unknown ----------------------------

def test_flagged_dev_three_rugs(client):
    result = client.check_dev(FLAGGED_3)
    assert result["verdict"] == "flagged"
    assert result["flagged"] is True
    assert result["rugged_mint_count"] == 3
    assert result["exit_code"] == 1
    assert result["risk_level"] == "high"
    assert result["cluster_id"] == "cluster-sample-a1"
    assert len(result["rugged_mints"]) == 3
    # Every rugged mint carries evidence links.
    for mint in result["rugged_mints"]:
        assert mint["evidence"]["solscan_mint"].startswith("https://solscan.io/token/")
    assert result["sample_data"] is True


def test_flagged_dev_two_rugs_is_enough(client):
    result = client.check_dev(FLAGGED_2)
    assert result["verdict"] == "flagged"
    assert result["rugged_mint_count"] == 2
    assert result["exit_code"] == 1


def test_under_review_dev_one_rug_is_not_flagged(client):
    result = client.check_dev(UNDER_REVIEW)
    assert result["verdict"] == "under_review"
    assert result["flagged"] is False
    assert result["rugged_mint_count"] == 1
    assert result["exit_code"] == 0
    assert "below the 2-rug threshold" in result["reason"]


def test_expired_dev_is_not_flagged(client):
    result = client.check_dev(EXPIRED)
    assert result["verdict"] == "not_flagged"
    assert result["risk_level"] == "expired"
    assert result["flagged"] is False
    assert result["exit_code"] == 0


def test_unknown_dev(client):
    result = client.check_dev(UNKNOWN_DEV)
    assert result["verdict"] == "unknown"
    assert result["rugged_mint_count"] == 0
    assert result["flagged"] is False
    assert result["exit_code"] == 0
    assert "No record" in result["reason"]


def test_invalid_dev_raises(client):
    with pytest.raises(ValueError):
        client.check_dev("not-an-address!!")
    with pytest.raises(ValueError):
        client.check_dev("")


# -- check_dev with a mint -------------------------------------------------

def test_check_dev_with_known_mint(client):
    result = client.check_dev(FLAGGED_3, mint=MINT_1)
    assert result["mint_in_registry"] is True
    assert result["verdict"] == "flagged"


def test_check_dev_with_unrelated_mint(client):
    # MINT_6 belongs to the under-review dev, not to FLAGGED_3.
    result = client.check_dev(FLAGGED_3, mint=MINT_6)
    assert result["mint_in_registry"] is False
    assert result["verdict"] == "flagged"  # verdict stays about the dev
    assert any("not one of this dev's rugged mints" in r for r in result["reasons"])


def test_check_dev_with_invalid_mint_raises(client):
    with pytest.raises(ValueError):
        client.check_dev(FLAGGED_3, mint="bad!!")


# -- mint checks -----------------------------------------------------------

def test_known_mint_is_rugged_with_dev_and_evidence(client):
    result = client.check_mint(MINT_1)
    assert result["verdict"] == "rugged"
    assert result["dev_wallet"] == FLAGGED_3
    assert result["symbol"] == "SAMPLE1"
    assert result["collapse_pct"] == 98.1
    assert result["evidence"]["solscan_mint"].startswith("https://solscan.io/token/")
    assert result["sample_data"] is True


def test_known_mint_of_under_review_dev_is_still_rugged(client):
    # The mint is rugged; only the *dev* is under review (1 rug).
    result = client.check_mint(MINT_6)
    assert result["verdict"] == "rugged"
    assert result["dev_wallet"] == UNDER_REVIEW


def test_invalid_mint_raises(client):
    with pytest.raises(ValueError):
        client.check_mint("!!!")
    assert is_valid_solana_address("!!!") is False
    assert is_valid_solana_address(MINT_1) is True


def test_live_mint_collapsed_is_watch_label_not_rug_verdict(live_client):
    result = live_client.check_mint(LIVE_MINT)
    assert result["verdict"] == "not_in_registry"
    assert result["verdict"] != "rugged"
    assert result["watch_label"] == "collapsed"
    assert result["symbol"] == "CRASH"
    assert result["change_24h_pct"] == -80.0
    assert result["market_cap_usd"] == 100_000.0
    assert result["dev_wallet"] is None


def test_live_mint_thin_liquidity_label(live_client):
    result = live_client.check_mint("LiveMint2222222222222222222222222222")
    assert result["watch_label"] == "thin_liquidity"
    assert result["verdict"] == "not_in_registry"


def test_live_mint_active_label(live_client):
    result = live_client.check_mint("LiveMint3333333333333333333333333333")
    assert result["watch_label"] == "active"


def test_check_mint_no_network_falls_back_without_inventing(client):
    offline = RugFarm(
        registry_path=REGISTRY_PATH, fetcher=_failing_fetcher, today=TODAY
    )
    result = offline.check_mint(LIVE_MINT)
    assert result["verdict"] == "not_in_registry"
    assert result["watch_label"] == "unknown"
    assert result["market_cap_usd"] is None
    assert result["liquidity_usd"] is None


# -- helpers ---------------------------------------------------------------

def test_helpers_flagged_and_rugged(client):
    assert client.is_flagged_dev(FLAGGED_3) is True
    assert client.is_flagged_dev(UNDER_REVIEW) is False
    assert client.is_flagged_dev(UNKNOWN_DEV) is False
    assert client.is_flagged_dev("bad!!") is False
    assert client.is_rugged_mint(MINT_1) is True
    assert client.is_rugged_mint(MINT_2) is True
    assert client.is_rugged_mint(LIVE_MINT) is False
    assert client.is_rugged_mint("bad!!") is False


def test_helpers_make_no_network_call():
    # is_rugged_mint / is_flagged_dev are registry-only.
    def boom(url):
        raise AssertionError("network call made")

    offline = RugFarm(registry_path=REGISTRY_PATH, fetcher=boom, today=TODAY)
    assert offline.is_rugged_mint(MINT_1) is True
    assert offline.is_flagged_dev(FLAGGED_3) is True


# -- watch -----------------------------------------------------------------

def test_watch_labels_and_never_rugged(live_client):
    entries = live_client.watch(limit=3)
    assert len(entries) == 3
    by_symbol = {e["symbol"]: e for e in entries}
    assert by_symbol["CRASH"]["watch_label"] == "collapsed"
    assert by_symbol["THIN"]["watch_label"] == "thin_liquidity"
    assert by_symbol["OKAY"]["watch_label"] == "active"
    assert all(e["watch_label"] != "rugged" for e in entries)
    # Non-Solana boost filtered out; all entries carry a DexScreener URL.
    assert all(e["dexscreener_url"].startswith("https://dexscreener.com/solana/")
               for e in entries)


def test_watch_no_network_returns_empty_list():
    offline = RugFarm(
        registry_path=REGISTRY_PATH, fetcher=_failing_fetcher, today=TODAY
    )
    assert offline.watch(limit=5) == []


def test_watch_invalid_limit(client):
    with pytest.raises(ValueError):
        client.watch(limit=0)
    with pytest.raises(ValueError):
        client.watch(limit=-3)


def test_watch_label_thresholds():
    assert watch_label(-70.0, 50_000.0) == "collapsed"
    assert watch_label(-69.9, 50_000.0) == "active"
    assert watch_label(None, 999.0) == "thin_liquidity"
    assert watch_label(None, 1_000.0) == "active"
    assert watch_label(None, None) == "active"


# -- registry loading / sample marking -------------------------------------

def test_load_registry_marks_sample_data():
    data = load_registry(REGISTRY_PATH)
    assert data["sample_data"] is True
    assert len(data["entries"]) == 4
    client = RugFarm(registry_path=REGISTRY_PATH, today=TODAY)
    assert client.sample_data is True


def test_load_registry_missing_file():
    with pytest.raises(FileNotFoundError):
        load_registry("does/not/exist.json")


def test_load_registry_rejects_wrong_flag_threshold(tmp_path):
    bad = tmp_path / "registry.json"
    bad.write_text(json.dumps({"min_rugged_mints_to_flag": 1, "entries": []}))
    with pytest.raises(ValueError):
        load_registry(bad)


# -- demo CLI ---------------------------------------------------------------

def test_main_demo_exit_codes(capsys):
    assert main([FLAGGED_3, "--registry", str(REGISTRY_PATH)]) == 1
    assert main([MINT_1, "--registry", str(REGISTRY_PATH)]) == 1
    assert main([UNDER_REVIEW, "--registry", str(REGISTRY_PATH)]) == 0
    assert main(["not-an-address!!"]) == 2
    out = capsys.readouterr().out
    assert "Flagged" in out
    assert "SAMPLE1" in out
