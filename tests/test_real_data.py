"""Real-world dataset tests — all HTTP is mocked with httpx.MockTransport.
No live call is ever made here. The mints below are synthetic test
values, and the labels are observable-market-fact labels ("rugged"
means rugged-like by the fixed rule), never verified rug verdicts.
"""
import json
from datetime import datetime, timezone

import httpx
import pytest

from src.real_data import (
    LABELLING_RULE_VERSION,
    RealDataFetcher,
    build_record,
    label_token,
    labelling_rule,
    main,
    parse_pair,
    pick_main_pair,
    save_dataset,
    summarize,
)

NOW = datetime(2026, 11, 13, 18, 0, 0, tzinfo=timezone.utc)
DAY_MS = 86_400_000


def _obs(**overrides):
    obs = {
        "change_24h_pct": None,
        "liquidity_usd": None,
        "volume_24h_usd": None,
        "market_cap_usd": None,
        "pair_age_days": None,
    }
    obs.update(overrides)
    return obs


# -- Labelling rule (fixed "real-v2", each branch) -----------------------

def test_label_rugged_branch_a_fresh_collapse():
    assert label_token(_obs(
        change_24h_pct=-92.0, liquidity_usd=400.0,
        volume_24h_usd=200.0, pair_age_days=3.0,
    )) == "rugged"
    # Exactly at the thresholds still counts.
    assert label_token(_obs(
        change_24h_pct=-90.0, liquidity_usd=999.99,
        volume_24h_usd=999.99, pair_age_days=1.0,
    )) == "rugged"


def test_label_rugged_branch_b_long_dead():
    assert label_token(_obs(
        change_24h_pct=-2.0, liquidity_usd=50.0,
        volume_24h_usd=10.0, market_cap_usd=2000.0,
        pair_age_days=30.0,
    )) == "rugged"


def test_label_survived():
    assert label_token(_obs(
        change_24h_pct=12.0, liquidity_usd=250_000.0,
        volume_24h_usd=800_000.0, market_cap_usd=5_000_000.0,
        pair_age_days=60.0,
    )) == "survived"


def test_label_excluded_ambiguous_middle():
    # Steep collapse but healthy liquidity: ambiguous, not forced.
    assert label_token(_obs(
        change_24h_pct=-80.0, liquidity_usd=50_000.0,
        volume_24h_usd=100_000.0, pair_age_days=10.0,
    )) == "excluded"
    # Thin liquidity but no collapse and decent volume.
    assert label_token(_obs(
        change_24h_pct=-10.0, liquidity_usd=500.0,
        volume_24h_usd=50_000.0, pair_age_days=10.0,
    )) == "excluded"


def test_label_missing_data_never_satisfies_a_threshold():
    # Collapse + thin liquidity + low volume, but no pair age.
    assert label_token(_obs(
        change_24h_pct=-90.0, liquidity_usd=100.0,
        volume_24h_usd=100.0, pair_age_days=None,
    )) == "excluded"
    # Everything missing.
    assert label_token(_obs()) == "excluded"
    # Fresh token (age < 1 day) that just crashed: excluded, not rugged.
    assert label_token(_obs(
        change_24h_pct=-95.0, liquidity_usd=100.0,
        volume_24h_usd=100.0, pair_age_days=0.1,
    )) == "excluded"


def test_labelling_rule_is_versioned_and_structured():
    rule = labelling_rule()
    assert rule["version"] == LABELLING_RULE_VERSION
    assert rule["rugged_branch_a_fresh_collapse"]["change_24h_pct_max"] == -90.0
    assert rule["survived"]["liquidity_usd_min"] == 10_000.0


# -- Pair parsing ----------------------------------------------------------

def _pair(mint, symbol, market_cap, liquidity, volume, change, age_days, txns=100):
    created = int(NOW.timestamp() * 1000 - age_days * DAY_MS)
    return {
        "chainId": "solana",
        "pairAddress": f"PAIR-{symbol}",
        "baseToken": {"address": mint, "name": symbol, "symbol": symbol},
        "marketCap": market_cap,
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume},
        "priceChange": {"h24": change},
        "txns": {"h24": {"buys": txns // 2, "sells": txns - txns // 2}},
        "pairCreatedAt": created,
    }


RUGGED_MINT = "RUGGED11111111111111111111111111111111"
SURVIVED_MINT = "SURVIVED111111111111111111111111111111"
DEAD_MINT = "DEAD1111111111111111111111111111111111"
MID_MINT = "MIDDLE11111111111111111111111111111111"


def test_parse_pair_captures_age_and_txns():
    obs = parse_pair(_pair(RUGGED_MINT, "RUG", 8000, 300, 150, -88.0, 3.0, txns=40))
    assert obs["mint"] == RUGGED_MINT
    assert obs["market_cap_usd"] == 8000
    assert obs["liquidity_usd"] == 300
    assert obs["volume_24h_usd"] == 150
    assert obs["change_24h_pct"] == -88.0
    assert obs["txns_24h"] == 40
    assert obs["pair_created_at_ms"] is not None


def test_parse_pair_returns_none_without_mint():
    assert parse_pair({"baseToken": {"symbol": "X"}}) is None
    assert parse_pair({}) is None


def test_parse_pair_missing_fields_are_none_not_zero():
    pair = {
        "chainId": "solana",
        "baseToken": {"address": MID_MINT, "symbol": "MID"},
    }
    obs = parse_pair(pair)
    assert obs["liquidity_usd"] is None
    assert obs["volume_24h_usd"] is None
    assert obs["change_24h_pct"] is None
    assert obs["txns_24h"] is None
    assert obs["pair_created_at_ms"] is None


def test_pick_main_pair_is_highest_liquidity():
    thin = _pair(RUGGED_MINT, "RUG", 8000, 100, 50, -88.0, 3.0)
    main = _pair(RUGGED_MINT, "RUG", 9000, 9000, 5000, -88.0, 3.0)
    assert pick_main_pair([thin, main])["liquidity_usd"] == 9000
    assert pick_main_pair([]) is None


# -- Record building -------------------------------------------------------

def test_build_record_rugged_has_real_features_only():
    obs = parse_pair(_pair(RUGGED_MINT, "RUG", 8000, 300, 150, -92.0, 3.0))
    record = build_record(obs, NOW)
    assert record["outcome"] == "rugged"
    assert record["collapse_pct"] == 92.0  # 24h-fall proxy, documented
    assert record["liquidity_usd"] == 300
    assert record["synthetic"] is False
    assert record["dexscreener_url"].endswith(RUGGED_MINT)
    assert record["labelling_rule_version"] == LABELLING_RULE_VERSION
    assert record["labelling_rule"]["version"] == LABELLING_RULE_VERSION
    # The 8 features DexScreener does not provide are ABSENT, never zero-filled.
    for feature in (
        "dev_sold_pct", "lp_removed", "time_to_collapse_minutes",
        "wash_score", "top1_share", "top10_share",
        "holder_count", "cluster_size",
    ):
        assert feature not in record


def test_build_record_missing_change_means_missing_collapse_feature():
    pair = _pair(MID_MINT, "MID", 100000, 5000, 2000, None, 2.0)
    pair["priceChange"] = {}
    record = build_record(parse_pair(pair), NOW)
    assert "collapse_pct" not in record
    assert record["outcome"] == "excluded"


def test_build_record_positive_change_is_zero_collapse_not_missing():
    obs = parse_pair(_pair(SURVIVED_MINT, "OK", 5_000_000, 250_000, 800_000, 12.0, 60.0))
    record = build_record(obs, NOW)
    assert record["outcome"] == "survived"
    assert record["collapse_pct"] == 0.0


# -- Full fetch with mocked HTTP -------------------------------------------

def _mock_client():
    profiles = [
        {"chainId": "solana", "tokenAddress": RUGGED_MINT},
        {"chainId": "solana", "tokenAddress": SURVIVED_MINT},
        {"chainId": "ethereum", "tokenAddress": "ETH111111111111111111111111111111111"},
    ]
    boosts = [
        {"chainId": "solana", "tokenAddress": DEAD_MINT},
        {"chainId": "solana", "tokenAddress": MID_MINT},
        {"chainId": "solana", "tokenAddress": RUGGED_MINT},  # duplicate
    ]
    pairs = [
        _pair(RUGGED_MINT, "RUG", 8000, 300, 150, -92.0, 3.0),
        _pair(SURVIVED_MINT, "OK", 5_000_000, 250_000, 800_000, 12.0, 60.0),
        _pair(DEAD_MINT, "DED", 2000, 50, 10, -2.0, 30.0),
        _pair(MID_MINT, "MID", 100_000, 5000, 2000, -10.0, 2.0),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "token-profiles" in url:
            return httpx.Response(200, json=profiles)
        if "token-boosts" in url:
            return httpx.Response(200, json=boosts)
        if "dex/search" in url:
            return httpx.Response(200, json={"pairs": []})
        if "tokens/v1/solana" in url:
            requested = url.rsplit("/solana/", 1)[-1].split(",")
            wanted = {p["baseToken"]["address"] for p in pairs}
            return httpx.Response(
                200, json=[p for p in pairs if p["baseToken"]["address"] in requested and p["baseToken"]["address"] in wanted],
            )
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_builds_labelled_dataset_from_fake_dexscreener():
    records = RealDataFetcher(client=_mock_client()).fetch(limit=150)
    by_mint = {r["mint"]: r for r in records}
    assert set(by_mint) == {RUGGED_MINT, SURVIVED_MINT, DEAD_MINT, MID_MINT}
    assert by_mint[RUGGED_MINT]["outcome"] == "rugged"
    assert by_mint[DEAD_MINT]["outcome"] == "rugged"  # branch B, long-dead
    assert by_mint[SURVIVED_MINT]["outcome"] == "survived"
    assert by_mint[MID_MINT]["outcome"] == "excluded"
    summary = summarize(records)
    assert summary["n_total"] == 4
    assert summary["n_rugged"] == 2
    assert summary["n_survived"] == 1
    assert summary["n_excluded"] == 1
    assert summary["n_labelled"] == 3
    assert summary["feature_coverage"]["collapse_pct"] == 4
    assert summary["feature_coverage"]["liquidity_usd"] == 4
    assert "dev_sold_pct" in summary["features_always_missing"]
    assert "holder_count" in summary["features_always_missing"]


def test_fetch_dedupes_mints_and_respects_limit():
    records = RealDataFetcher(client=_mock_client()).fetch(limit=2)
    assert len(records) == 2  # RUGGED + SURVIVED (profiles order, boosts deduped)
    assert records[0]["mint"] == RUGGED_MINT
    assert records[1]["mint"] == SURVIVED_MINT


def test_fetch_network_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    fetcher = RealDataFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError):
        fetcher.fetch(limit=10)


def test_fetch_empty_lists_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    records = RealDataFetcher(client=httpx.Client(transport=httpx.MockTransport(handler))).fetch(limit=10)
    assert records == []


# -- Dataset file shape ------------------------------------------------------

def test_save_dataset_writes_a_json_list(tmp_path):
    records = RealDataFetcher(client=_mock_client()).fetch(limit=150)
    path = tmp_path / "dataset.json"
    save_dataset(path, records)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 4
    for record in data:
        assert record["mint"]
        assert record["outcome"] in ("rugged", "survived", "excluded")
        assert record["dexscreener_url"].startswith("https://dexscreener.com/solana/")
        assert record["fetched_at"]
        assert record["labelling_rule_version"] == LABELLING_RULE_VERSION
        assert record["synthetic"] is False


def test_save_dataset_refuses_registry(tmp_path):
    with pytest.raises(ValueError):
        save_dataset(tmp_path / "registry.json", [])


# -- CLI ---------------------------------------------------------------------

def test_cli_no_fetch_exits_2(capsys):
    assert main([]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_cli_bad_limit_exits_2(capsys):
    assert main(["--fetch", "--limit", "0"]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_cli_too_few_labelled_exits_2_but_saves(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    code = main(
        ["--fetch", "--limit", "150", "--out", str(out)],
        client=_mock_client(),
    )
    assert code == 2  # only 3 labelled records in the fake data
    assert "too few usable tokens" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8"))  # file still saved


def test_cli_success_exits_0_when_enough_labelled(tmp_path, capsys):
    # A fake DexScreener with 12 labelled mints (6 rugged-like, 6
    # survived) must exit 0 and save the dataset.
    big_profiles = [
        {"chainId": "solana", "tokenAddress": f"RUGGED{i:03d}" + "1" * 28}
        for i in range(6)
    ] + [
        {"chainId": "solana", "tokenAddress": f"SURVIVED{i:02d}" + "1" * 28}
        for i in range(6)
    ]
    big_pairs = [
        _pair(f"RUGGED{i:03d}" + "1" * 28, f"R{i}", 8000, 300, 150, -92.0, 3.0)
        for i in range(6)
    ] + [
        _pair(f"SURVIVED{i:02d}" + "1" * 28, f"S{i}", 5_000_000, 250_000, 800_000, 12.0, 60.0)
        for i in range(6)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "token-profiles" in url:
            return httpx.Response(200, json=big_profiles)
        if "token-boosts" in url:
            return httpx.Response(200, json=[])
        if "dex/search" in url:
            return httpx.Response(200, json={"pairs": []})
        if "tokens/v1/solana" in url:
            requested = url.rsplit("/solana/", 1)[-1].split(",")
            return httpx.Response(
                200,
                json=[p for p in big_pairs if p["baseToken"]["address"] in requested],
            )
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = tmp_path / "dataset.json"
    code = main(
        ["--fetch", "--limit", "150", "--out", str(out)],
        client=client,
    )
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 12
    assert summarize(data)["n_labelled"] == 12
    assert "rugged-like" in capsys.readouterr().out
