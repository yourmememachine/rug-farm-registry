"""Pump-watch tests — all HTTP is mocked with httpx.MockTransport.
No live call is ever made here. No real wallet is accused: the
mints below are synthetic test values, and the watch labels are
'collapsed' / 'thin_liquidity' / 'active' only — never 'rugged'."""
import json

import httpx
import pytest

from src.pump_watch import PumpWatch, format_entries, parse_token_pair, watch_label

MINT_COLLAPSED = "COLLAPSE1111111111111111111111111111111"
MINT_THIN = "THINLIQ111111111111111111111111111111111"
MINT_ACTIVE = "ACTIVE1111111111111111111111111111111111"
MINT_ETH = "ETHBOOST11111111111111111111111111111111"


def _pair(mint, symbol, market_cap, liquidity, volume, change):
    return {
        "chainId": "solana",
        "baseToken": {"address": mint, "symbol": symbol, "name": symbol},
        "marketCap": market_cap,
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume},
        "priceChange": {"h24": change},
    }


def _mock_client(boosts=None, pairs=None, status=200):
    boosts = boosts if boosts is not None else [
        {"tokenAddress": MINT_COLLAPSED, "chainId": "solana"},
        {"tokenAddress": MINT_THIN, "chainId": "solana"},
        {"tokenAddress": MINT_ACTIVE, "chainId": "solana"},
        {"tokenAddress": MINT_ETH, "chainId": "ethereum"},
    ]
    pairs = pairs if pairs is not None else [
        _pair(MINT_COLLAPSED, "CRASH", 8000, 5564, 435653, -76.7),
        _pair(MINT_THIN, "THIN", 1824, 340, 7012, 22.0),
        _pair(MINT_ACTIVE, "LEAD", 12428325, 303552, 1718059, 122.0),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "token-boosts" in url:
            return httpx.Response(status, json=boosts)
        if "tokens/v1/solana" in url:
            # Only return pairs for the mints actually requested.
            requested = url.rsplit("/solana/", 1)[-1].split(",")
            wanted = {p["baseToken"]["address"] for p in pairs}
            return httpx.Response(
                status,
                json=[p for p in pairs if p["baseToken"]["address"] in requested],
            )
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_watch_label_thresholds():
    assert watch_label(-70.0, 100000) == "collapsed"
    assert watch_label(-76.7, 5564) == "collapsed"
    assert watch_label(-69.9, 100000) == "active"
    assert watch_label(22.0, 340) == "thin_liquidity"
    assert watch_label(22.0, 999.99) == "thin_liquidity"
    assert watch_label(22.0, 1000.0) == "active"
    assert watch_label(None, None) == "active"
    # Collapsed takes precedence over thin liquidity.
    assert watch_label(-80.0, 100) == "collapsed"
    # No label is ever "rugged" from this data alone.
    for chg in (-99.0, 0.0, 100.0):
        for liq in (0.0, 100000.0):
            assert watch_label(chg, liq) != "rugged"


def test_parse_token_pair_handles_pair_shape():
    entry = parse_token_pair(_pair(MINT_ACTIVE, "LEAD", 12428325, 303552, 1718059, 122.0))
    assert entry["mint"] == MINT_ACTIVE
    assert entry["symbol"] == "LEAD"
    assert entry["market_cap_usd"] == 12428325
    assert entry["liquidity_usd"] == 303552
    assert entry["volume_24h_usd"] == 1718059
    assert entry["change_24h_pct"] == 122.0
    assert entry["watch_label"] == "active"
    assert entry["dexscreener_url"].endswith(MINT_ACTIVE)


def test_parse_token_pair_returns_none_without_mint():
    assert parse_token_pair({"baseToken": {"symbol": "X"}}) is None
    assert parse_token_pair({}) is None


def test_watch_collapsed_thin_and_active():
    entries = PumpWatch(client=_mock_client()).watch(limit=10)
    by_mint = {e["mint"]: e for e in entries}
    assert by_mint[MINT_COLLAPSED]["watch_label"] == "collapsed"
    assert by_mint[MINT_COLLAPSED]["change_24h_pct"] == -76.7
    assert by_mint[MINT_THIN]["watch_label"] == "thin_liquidity"
    assert by_mint[MINT_THIN]["liquidity_usd"] == 340
    assert by_mint[MINT_ACTIVE]["watch_label"] == "active"
    assert by_mint[MINT_ACTIVE]["market_cap_usd"] == 12428325
    # Never a rug verdict from watch data.
    assert all(e["watch_label"] != "rugged" for e in entries)


def test_watch_filters_non_solana_boosts_and_respects_limit():
    entries = PumpWatch(client=_mock_client()).watch(limit=2)
    assert [e["mint"] for e in entries] == [MINT_COLLAPSED, MINT_THIN]
    assert MINT_ETH not in {e["mint"] for e in entries}


def test_watch_empty_boosts_returns_empty_list():
    entries = PumpWatch(client=_mock_client(boosts=[], pairs=[])).watch(limit=10)
    assert entries == []
    assert format_entries(entries).startswith("No boosted Solana tokens")


def test_watch_api_error_raises_runtime_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    watcher = PumpWatch(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="DexScreener request failed"):
        watcher.watch(limit=5)


def test_watch_non_list_boosts_response_raises_runtime_error():
    client = _mock_client(boosts={"not": "a list"})
    with pytest.raises(RuntimeError, match="not a list"):
        PumpWatch(client=client).watch(limit=5)


def test_watch_limit_must_be_positive():
    with pytest.raises(ValueError, match="positive integer"):
        PumpWatch(client=_mock_client()).watch(limit=0)


def test_format_entries_includes_disclaimer():
    entries = PumpWatch(client=_mock_client()).watch(limit=10)
    text = format_entries(entries)
    assert "collapsed" in text
    assert "thin_liquidity" in text
    assert "Watch labels are not rug verdicts" in text
    assert "rugged it" in text  # disclaimer explains what a rug would need


def test_cli_json_output(monkeypatch, capsys):
    # main() with --json prints valid JSON; the client is mocked by
    # patching PumpWatch to use the mock transport.
    import src.pump_watch as pw

    monkeypatch.setattr(pw, "PumpWatch", lambda: PumpWatch(client=_mock_client()))
    assert pw.main(["--limit", "2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["mint"] for d in data] == [MINT_COLLAPSED, MINT_THIN]
