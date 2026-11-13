"""Pump.fun second-source tests — all HTTP is mocked with httpx.MockTransport.
No live call is ever made here. The mints below are synthetic test
values, and the labels are observable-market-fact labels ("rugged"
means rugged-like by the fixed "real-v1" rule), never verified rug
verdicts. The pump.fun fetcher is tested against the SAME rule as
the DexScreener path — the source only changes which mints are
sampled, never the labelling.
"""
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.real_data import (
    LABELLING_RULE_VERSION,
    PumpFunFetcher,
    build_record,
    fetch_pumpfun_records,
    main,
    merge_datasets,
    parse_pumpfun_coin,
    save_summary,
    summarize,
    summary_path,
)

NOW = datetime(2026, 11, 13, 18, 0, 0, tzinfo=timezone.utc)
DAY_MS = 86_400_000

DEAD_MINT = "DEADPUMP11111111111111111111111111111111"
RUG_MINT = "RUGPUMP111111111111111111111111111111111"
OK_MINT = "OKPUMP1111111111111111111111111111111111"
GHOST_MINT = "GHOSTPUMP111111111111111111111111111111"


def _coin(mint, symbol, age_days):
    created = int(NOW.timestamp() * 1000 - age_days * DAY_MS)
    return {
        "mint": mint,
        "name": symbol,
        "symbol": symbol,
        "created_timestamp": created,
    }


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


# -- Coin parsing ----------------------------------------------------------

def test_parse_pumpfun_coin_keeps_mint_and_launch_time():
    parsed = parse_pumpfun_coin(_coin(DEAD_MINT, "DED", 30.0))
    assert parsed["mint"] == DEAD_MINT
    assert parsed["symbol"] == "DED"
    assert parsed["created_at_ms"] == _coin(DEAD_MINT, "DED", 30.0)["created_timestamp"]


def test_parse_pumpfun_coin_returns_none_without_mint_or_timestamp():
    assert parse_pumpfun_coin({"symbol": "X", "created_timestamp": 123}) is None
    assert parse_pumpfun_coin({"mint": DEAD_MINT}) is None
    assert parse_pumpfun_coin({}) is None
    assert parse_pumpfun_coin("not a dict") is None


# -- Launch paging ---------------------------------------------------------

def _mock_client(pages, pairs):
    """pages: {offset: [coins]}; pairs: DexScreener pair dicts."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "frontend-api.pump.fun" in url:
            qs = parse_qs(urlparse(url).query)
            offset = int(qs.get("offset", ["0"])[0])
            return httpx.Response(200, json=pages.get(offset, []))
        if "tokens/v1/solana" in url:
            requested = url.rsplit("/solana/", 1)[-1].split(",")
            return httpx.Response(
                200,
                json=[p for p in pairs if p["baseToken"]["address"] in requested],
            )
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_launches_pages_offsets_and_dedupes():
    pages = {
        0: [_coin(RUG_MINT, "RUG", 3.0), _coin(OK_MINT, "OK", 60.0)],
        100: [_coin(RUG_MINT, "RUG", 3.0), _coin(DEAD_MINT, "DED", 30.0)],
        300: [],
    }
    launches = PumpFunFetcher(client=_mock_client(pages, [])).fetch_launches(limit=10)
    assert [item["mint"] for item in launches] == [RUG_MINT, OK_MINT, DEAD_MINT]


def test_fetch_launches_continues_past_short_page():
    # A short first page does not stop paging — only an empty page
    # does. (The mock's page 0 has 1 coin; offset 100 still gets
    # fetched and its coin is included.)
    pages = {
        0: [_coin(RUG_MINT, "RUG", 3.0)],
        100: [_coin(DEAD_MINT, "DED", 30.0)],
    }
    launches = PumpFunFetcher(client=_mock_client(pages, [])).fetch_launches(limit=300)
    assert [item["mint"] for item in launches] == [RUG_MINT, DEAD_MINT]


def test_fetch_launches_respects_limit():
    pages = {
        0: [_coin(f"MINT{i:03d}" + "1" * 30, f"C{i}", 1.0) for i in range(50)],
    }
    launches = PumpFunFetcher(client=_mock_client(pages, [])).fetch_launches(limit=3)
    assert len(launches) == 3


def test_fetch_launches_429_raises_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    fetcher = PumpFunFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="pump.fun request failed"):
        fetcher.fetch_launches(limit=10)


def test_fetch_launches_403_blocked_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    fetcher = PumpFunFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="pump.fun request failed"):
        fetcher.fetch_launches(limit=10)


def test_fetch_launches_non_list_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"coins": []})

    fetcher = PumpFunFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="not a list"):
        fetcher.fetch_launches(limit=10)


# -- Enrichment + labelling via the SAME rule ------------------------------

def _full_mock_client():
    pages = {
        0: [
            _coin(DEAD_MINT, "DED", 30.0),
            _coin(RUG_MINT, "RUG", 3.0),
            _coin(OK_MINT, "OK", 60.0),
            _coin(GHOST_MINT, "GHO", 5.0),  # no DexScreener pair -> no record
        ],
    }
    pairs = [
        _pair(DEAD_MINT, "DED", 2000, 50, 10, -2.0, 30.0),
        _pair(RUG_MINT, "RUG", 8000, 300, 150, -88.0, 3.0),
        _pair(OK_MINT, "OK", 5_000_000, 250_000, 800_000, 12.0, 60.0),
    ]
    return _mock_client(pages, pairs)


def test_fetch_pumpfun_records_labels_with_shared_rule():
    records = fetch_pumpfun_records(client=_full_mock_client(), limit=10)
    by_mint = {r["mint"]: r for r in records}
    # GHOST has no DexScreener pair, so it yields no record at all.
    assert set(by_mint) == {DEAD_MINT, RUG_MINT, OK_MINT}
    assert by_mint[DEAD_MINT]["outcome"] == "rugged"  # branch B, long-dead
    assert by_mint[RUG_MINT]["outcome"] == "rugged"  # branch A, fresh collapse
    assert by_mint[OK_MINT]["outcome"] == "survived"
    for record in records:
        assert record["source"] == "pumpfun"
        assert record["pumpfun_url"] == f"https://pump.fun/coin/{record['mint']}"
        assert record["dexscreener_url"].startswith("https://dexscreener.com/solana/")
        assert record["labelling_rule_version"] == LABELLING_RULE_VERSION
        assert record["synthetic"] is False


def test_build_record_pumpfun_age_falls_back_to_launch_time():
    obs = {
        "mint": RUG_MINT,
        "symbol": "RUG",
        "market_cap_usd": 8000,
        "liquidity_usd": 300,
        "volume_24h_usd": 150,
        "change_24h_pct": -88.0,
        "txns_24h": 40,
        "pair_created_at_ms": None,
        "created_at_ms": int(NOW.timestamp() * 1000 - 3.0 * DAY_MS),
    }
    record = build_record(obs, NOW, source="pumpfun")
    assert record["pair_age_days"] == 3.0
    assert record["outcome"] == "rugged"
    assert record["source"] == "pumpfun"
    assert record["pumpfun_url"].endswith(RUG_MINT)


def test_build_record_default_source_has_no_pumpfun_url():
    obs = {
        "mint": OK_MINT,
        "symbol": "OK",
        "market_cap_usd": 5_000_000,
        "liquidity_usd": 250_000,
        "volume_24h_usd": 800_000,
        "change_24h_pct": 12.0,
        "txns_24h": 100,
        "pair_created_at_ms": int(NOW.timestamp() * 1000 - 60.0 * DAY_MS),
    }
    record = build_record(obs, NOW)
    assert record["source"] == "dexscreener"
    assert "pumpfun_url" not in record


# -- Merge -----------------------------------------------------------------

def test_merge_datasets_dedupes_and_backfills_source():
    existing = [
        {"mint": RUG_MINT, "outcome": "rugged",
         "dexscreener_url": f"https://dexscreener.com/solana/{RUG_MINT}"},
        {"mint": OK_MINT, "outcome": "survived",
         "dexscreener_url": f"https://dexscreener.com/solana/{OK_MINT}"},
    ]
    new = [
        {"mint": RUG_MINT, "outcome": "rugged", "source": "pumpfun"},  # dup: existing wins
        {"mint": DEAD_MINT, "outcome": "rugged", "source": "pumpfun"},
    ]
    merged = merge_datasets(existing, new)
    assert [r["mint"] for r in merged] == [RUG_MINT, OK_MINT, DEAD_MINT]
    assert merged[0]["source"] == "dexscreener"  # backfilled, existing kept
    assert merged[0].get("pumpfun_url") is None
    assert merged[2]["source"] == "pumpfun"


def test_merge_datasets_skips_records_without_mint():
    merged = merge_datasets([{"outcome": "survived"}], [{"mint": RUG_MINT, "outcome": "rugged"}])
    assert [r["mint"] for r in merged] == [RUG_MINT]


def test_summarize_counts_by_source():
    records = [
        {"mint": "A", "outcome": "rugged", "source": "pumpfun"},
        {"mint": "B", "outcome": "survived", "source": "dexscreener"},
        {"mint": "C", "outcome": "excluded", "source": "pumpfun"},
    ]
    summary = summarize(records)
    assert summary["by_source"] == {"pumpfun": 2, "dexscreener": 1}


def test_save_summary_sidecar(tmp_path):
    records = fetch_pumpfun_records(client=_full_mock_client(), limit=10)
    out = tmp_path / "dataset.json"
    sidecar = summary_path(out)
    assert str(sidecar) == str(tmp_path / "dataset.summary.json")
    save_summary(sidecar, records)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["labelling_rule_version"] == LABELLING_RULE_VERSION
    assert data["sources"] == {"pumpfun": 3}
    assert data["summary"]["n_rugged"] == 2
    assert data["summary"]["n_survived"] == 1


# -- CLI ---------------------------------------------------------------------

def _existing_survived(n):
    return [
        {
            "mint": f"OLD{i:03d}" + "1" * 30,
            "outcome": "survived",
            "dexscreener_url": f"https://dexscreener.com/solana/OLD{i:03d}",
        }
        for i in range(n)
    ]


def test_cli_fetch_pumpfun_merges_and_exits_0(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    out.write_text(json.dumps(_existing_survived(9)), encoding="utf-8")
    code = main(
        ["--fetch-pumpfun", "--pumpfun-limit", "10", "--out", str(out)],
        client=_full_mock_client(),
    )
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 12  # 9 existing + 3 new (GHOST yields no record)
    summary = summarize(data)
    assert summary["n_labelled"] == 12
    assert summary["n_rugged"] == 2
    assert summary["by_source"] == {"dexscreener": 9, "pumpfun": 3}
    sidecar = json.loads((tmp_path / "dataset.summary.json").read_text(encoding="utf-8"))
    assert sidecar["sources"] == {"dexscreener": 9, "pumpfun": 3}
    assert "Pump.fun launches added 3 new records" in capsys.readouterr().out


def test_cli_fetch_pumpfun_too_few_labelled_exits_2_but_saves(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    code = main(
        ["--fetch-pumpfun", "--pumpfun-limit", "10", "--out", str(out)],
        client=_full_mock_client(),
    )
    assert code == 2  # only 3 labelled records in the fake data
    assert "too few usable tokens" in capsys.readouterr().err
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 3  # file still saved for audit
    assert (tmp_path / "dataset.summary.json").is_file()


def test_cli_both_fetch_modes_exits_2(capsys):
    assert main(["--fetch", "--fetch-pumpfun"]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_cli_bad_pumpfun_limit_exits_2(capsys):
    assert main(["--fetch-pumpfun", "--pumpfun-limit", "0"]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_cli_fetch_pumpfun_network_error_exits_2(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = tmp_path / "dataset.json"
    code = main(
        ["--fetch-pumpfun", "--pumpfun-limit", "10", "--out", str(out)],
        client=client,
    )
    assert code == 2
    assert "ERROR" in capsys.readouterr().err
    assert not out.is_file()  # nothing saved when the fetch itself failed
