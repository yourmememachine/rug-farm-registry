"""Documented-rugs verification tests — all HTTP is mocked with
httpx.MockTransport. No live call is ever made here. The mints
below are synthetic test values, and the labels are
observable-market-fact labels ("rugged" means rugged-like by the
fixed "real-v2" rule), never verified rug verdicts. The
documented-rugs path is tested against the SAME rule as the
DexScreener and pump.fun paths — the source only changes which
mints are sampled, never the labelling.
"""
import json
from datetime import datetime, timezone

import httpx
import pytest

from src.real_data import (
    LABELLING_RULE_VERSION,
    load_documented_list,
    main,
    merge_datasets,
    summarize,
    summary_path,
    verify_documented_mints,
)

NOW = datetime(2026, 11, 13, 18, 0, 0, tzinfo=timezone.utc)
DAY_MS = 86_400_000

DEAD_MINT = "DDEAD11111111111111111111111111111111111"
RUG_MINT = "DDRUG11111111111111111111111111111111111"
OK_MINT = "DDUP111111111111111111111111111111111111"
MID_MINT = "DDMED11111111111111111111111111111111111"
GHOST_MINT = "DDGHST1111111111111111111111111111111111"


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


def _mock_client(pairs):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tokens/v1/solana" in url:
            requested = url.rsplit("/solana/", 1)[-1].split(",")
            return httpx.Response(
                200,
                json=[p for p in pairs if p["baseToken"]["address"] in requested],
            )
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _entries():
    return [
        {"mint": DEAD_MINT, "source": "https://example.com/dead-rug"},
        {"mint": OK_MINT, "source": "https://example.com/ok-token"},
        {"mint": MID_MINT, "source": "https://example.com/mid-token"},
        {"mint": GHOST_MINT, "source": "https://example.com/ghost"},
    ]


def _pairs():
    return [
        _pair(DEAD_MINT, "DED", 2000, 50, 10, -2.0, 30.0),
        _pair(OK_MINT, "OK", 5_000_000, 250_000, 800_000, 12.0, 60.0),
        _pair(MID_MINT, "MID", 100_000, 5000, 2000, -10.0, 2.0),
    ]


# -- Verification ----------------------------------------------------------

def test_verify_good_pairs_labelled_by_shared_rule():
    result = verify_documented_mints(_entries(), client=_mock_client(_pairs()))
    by_mint = {r["mint"]: r for r in result["records"]}
    assert set(by_mint) == {DEAD_MINT, OK_MINT, MID_MINT}
    assert by_mint[DEAD_MINT]["outcome"] == "rugged"  # branch B, long-dead
    assert by_mint[OK_MINT]["outcome"] == "survived"
    assert by_mint[MID_MINT]["outcome"] == "excluded"
    for record in result["records"]:
        assert record["synthetic"] is False
        assert record["labelling_rule_version"] == LABELLING_RULE_VERSION
        assert record["dexscreener_url"].endswith(record["mint"])
    assert by_mint[DEAD_MINT]["source"] == "https://example.com/dead-rug"
    assert by_mint[OK_MINT]["source"] == "https://example.com/ok-token"
    assert result["verified"] == [DEAD_MINT, OK_MINT, MID_MINT]


def test_verify_no_pair_mint_dropped():
    result = verify_documented_mints(_entries(), client=_mock_client(_pairs()))
    assert result["dropped_no_pair"] == [GHOST_MINT]
    assert GHOST_MINT not in {r["mint"] for r in result["records"]}


def test_verify_bad_address_rejected_without_fetch():
    entries = [
        {"mint": "not-an-address", "source": "https://example.com/a"},
        {"mint": "0x1234567890abcdef", "source": "https://example.com/b"},
        {"mint": "ABC", "source": "https://example.com/c"},
        {"mint": RUG_MINT, "source": "https://example.com/d"},
    ]
    pairs = [_pair(RUG_MINT, "RUG", 8000, 300, 150, -92.0, 3.0)]
    result = verify_documented_mints(entries, client=_mock_client(pairs))
    assert {r["mint"] for r in result["records"]} == {RUG_MINT}
    assert result["records"][0]["outcome"] == "rugged"  # branch A
    rejected = {r["mint"] for r in result["rejected_invalid"]}
    assert rejected == {"not-an-address", "0x1234567890abcdef", "ABC"}
    assert all(r["reason"] == "not a valid Solana address" for r in result["rejected_invalid"])
    assert result["dropped_no_pair"] == []


def test_verify_dedupes_mints_first_entry_wins():
    entries = [
        {"mint": DEAD_MINT, "source": "https://example.com/first"},
        {"mint": DEAD_MINT, "source": "https://example.com/second"},
    ]
    result = verify_documented_mints(entries, client=_mock_client(_pairs()))
    assert len(result["records"]) == 1
    assert result["records"][0]["source"] == "https://example.com/first"
    assert result["rejected_invalid"] == []


def test_verify_empty_list_returns_empty_result():
    result = verify_documented_mints([], client=_mock_client([]))
    assert result["records"] == []
    assert result["verified"] == []
    assert result["dropped_no_pair"] == []
    assert result["rejected_invalid"] == []


# -- Merge -----------------------------------------------------------------

def test_merge_documented_keeps_existing_record():
    existing = [
        {"mint": DEAD_MINT, "outcome": "survived", "source": "dexscreener",
         "dexscreener_url": f"https://dexscreener.com/solana/{DEAD_MINT}"},
    ]
    result = verify_documented_mints(
        [{"mint": DEAD_MINT, "source": "https://example.com/dead-rug"}],
        client=_mock_client(_pairs()),
    )
    merged = merge_datasets(existing, result["records"])
    assert len(merged) == 1
    assert merged[0]["outcome"] == "survived"  # existing wins
    assert merged[0]["source"] == "dexscreener"


# -- List loading ----------------------------------------------------------

def test_load_documented_list_ok_and_source_url_alias(tmp_path):
    path = tmp_path / "list.json"
    path.write_text(json.dumps([
        {"mint": DEAD_MINT, "source": "https://example.com/a", "name": "DED"},
        {"mint": OK_MINT, "source_url": "https://example.com/b"},
    ]), encoding="utf-8")
    entries = load_documented_list(path)
    assert entries[0] == {"mint": DEAD_MINT, "source": "https://example.com/a", "name": "DED"}
    assert entries[1]["source"] == "https://example.com/b"


def test_load_documented_list_rejects_bad_files(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ValueError):
        load_documented_list(missing)
    not_a_list = tmp_path / "obj.json"
    not_a_list.write_text(json.dumps({"mint": DEAD_MINT}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_documented_list(not_a_list)
    no_source = tmp_path / "nosource.json"
    no_source.write_text(json.dumps([{"mint": DEAD_MINT}]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_documented_list(no_source)
    not_json = tmp_path / "bad.json"
    not_json.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_documented_list(not_json)


# -- CLI ---------------------------------------------------------------------

def _existing_labelled(path, n_rugged=6, n_survived=6):
    records = [
        {"mint": f"EXRUG{i:03d}" + "1" * 29, "outcome": "rugged"}
        for i in range(n_rugged)
    ] + [
        {"mint": f"EXOK{i:03d}" + "1" * 30, "outcome": "survived"}
        for i in range(n_survived)
    ]
    path.write_text(json.dumps(records), encoding="utf-8")
    return records


def test_cli_verify_documented_merges_and_exits_0(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    _existing_labelled(out)
    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([
        {"mint": DEAD_MINT, "source": "https://example.com/dead-rug"},
        {"mint": OK_MINT, "source": "https://example.com/ok-token"},
        {"mint": GHOST_MINT, "source": "https://example.com/ghost"},
    ]), encoding="utf-8")
    code = main(
        ["--verify-documented", str(list_path), "--out", str(out)],
        client=_mock_client(_pairs()),
    )
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 14  # 12 existing + 2 verified new
    by_mint = {r["mint"]: r for r in data}
    assert by_mint[DEAD_MINT]["outcome"] == "rugged"
    assert by_mint[DEAD_MINT]["source"] == "https://example.com/dead-rug"
    assert by_mint[OK_MINT]["outcome"] == "survived"
    assert GHOST_MINT not in by_mint
    assert summarize(data)["n_rugged"] == 7
    assert summarize(data)["n_survived"] == 7
    summary = json.loads(summary_path(out).read_text(encoding="utf-8"))
    assert summary["summary"]["n_total"] == 14
    assert "Documented rugs: 2 verified" in capsys.readouterr().out


def test_cli_verify_documented_no_pair_only_keeps_existing(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    _existing_labelled(out)
    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([
        {"mint": GHOST_MINT, "source": "https://example.com/ghost"},
    ]), encoding="utf-8")
    code = main(
        ["--verify-documented", str(list_path), "--out", str(out)],
        client=_mock_client(_pairs()),
    )
    assert code == 0  # existing 12 labelled records still satisfy the minimum
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 12
    captured = capsys.readouterr()
    assert "0 verified" in captured.out
    assert "1 dropped (no pair)" in captured.out


def test_cli_verify_documented_bad_list_exits_2(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    _existing_labelled(out)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"mint": DEAD_MINT}]), encoding="utf-8")
    code = main(
        ["--verify-documented", str(bad), "--out", str(out)],
        client=_mock_client(_pairs()),
    )
    assert code == 2
    assert "ERROR" in capsys.readouterr().err
    code = main(
        ["--verify-documented", str(tmp_path / "missing.json"), "--out", str(out)],
        client=_mock_client(_pairs()),
    )
    assert code == 2


def test_cli_verify_documented_and_fetch_together_exits_2(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([]), encoding="utf-8")
    code = main(
        ["--fetch", "--verify-documented", str(list_path), "--out", str(out)],
        client=_mock_client(_pairs()),
    )
    assert code == 2
    assert "exactly one" in capsys.readouterr().err
