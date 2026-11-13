"""Muse-run evidence tests — all HTTP is mocked with httpx.MockTransport.
No live call is ever made here, and no real wallet/mint is accused:
every address is synthetic."""

import json

import httpx
import pytest

from src.evidence import (
    EvidenceCollector,
    EvidenceReport,
    EvidenceVerifier,
    default_report_path,
    gather_and_verify,
    main,
)

DEV1 = "RUGDEV111111111111111111111111111111111"
DEV2 = "RUGDEV222222222222222222222222222222222"
MINT1 = "RUGMNT111111111111111111111111111111111"
MINT2 = "RUGMNT222222222222222222222222222222222"
MINT3 = "RUGMNT333333333333333333333333333333333"
BAD = "not-an-address!!"


def _dex_pair(mint, symbol="TKN", change=-95.0, liquidity=500.0, mc=50_000.0):
    return {
        "chainId": "solana",
        "baseToken": {"address": mint, "symbol": symbol, "name": f"{symbol} Coin"},
        "priceUsd": "0.001",
        "marketCap": mc,
        "fdv": mc,
        "liquidity": {"usd": liquidity},
        "volume": {"h24": 123_456.0},
        "priceChange": {"h24": change},
        "pairCreatedAt": 1790000000000,
    }


def _registry_entry(dev, mints):
    return {
        "entries": [
            {
                "wallet": dev,
                "cluster_id": "cluster-test",
                "status": "flagged",
                "rugged_mints": [
                    {
                        "mint": m,
                        "outcome": "rugged",
                        "collapse_pct": 98.1,
                        "peak_market_cap_usd": 1_000_000,
                        "current_market_cap_usd": 19_000,
                        "dev_sell_pct": 82.0,
                        "lp_removed": True,
                        "time_to_collapse_minutes": 35,
                        "evidence": {
                            "solscan_mint": f"https://solscan.io/token/{m}",
                            "solscan_wallet": f"https://solscan.io/account/{dev}",
                            "transaction": f"https://solscan.io/tx/SIG{m[-4:]}",
                        },
                    }
                    for m in mints
                ],
            }
        ]
    }


def _mock_client(pairs=None, solscan=None, solscan_status=200):
    """MockTransport client: DexScreener pairs by mint, Solscan account."""
    pairs = pairs or {}

    def handler(request):
        url = str(request.url)
        if "api.dexscreener.com" in url:
            mint = url.rsplit("/", 1)[-1]
            pair = pairs.get(mint)
            return httpx.Response(200, json={"pairs": [pair] if pair else []})
        if "public-api.solscan.io" in url:
            if solscan_status != 200:
                return httpx.Response(solscan_status, json={"error": "down"})
            return httpx.Response(200, json=solscan if solscan is not None else {})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _collector(pairs=None, solscan=None, solscan_status=200, registry=None):
    return EvidenceCollector(
        client=_mock_client(pairs, solscan, solscan_status),
        registry_data=registry if registry is not None else {"entries": []},
    )


SOLSCAN_OK = {"lamports": 18_556_500_065, "ownerProgram": "11111111111111111111111111111111"}


# --- EvidenceCollector: links --------------------------------------------

def test_collector_builds_all_links_for_dev_and_mint():
    collector = _collector(
        pairs={MINT1: _dex_pair(MINT1)}, solscan=SOLSCAN_OK
    )
    pack = collector.collect(DEV1, mints=[MINT1])
    assert pack["dev_wallet"] == DEV1
    entry = pack["mints"][0]
    assert entry["links"]["dexscreener"] == f"https://dexscreener.com/solana/{MINT1}"
    assert entry["links"]["solscan_mint"] == f"https://solscan.io/token/{MINT1}"
    assert entry["links"]["pumpfun"] == f"https://pump.fun/coin/{MINT1}"
    assert pack["wallet"]["links"]["solscan_wallet"] == f"https://solscan.io/account/{DEV1}"
    assert pack["wallet"]["links"]["pumpfun_profile"] == f"https://pump.fun/profile/{DEV1}"
    assert pack["links"]  # flat link list is non-empty


def test_collector_helius_tx_link_only_when_signature_given():
    collector = _collector(pairs={MINT1: _dex_pair(MINT1)}, solscan=SOLSCAN_OK)
    pack = collector.collect(DEV1, mints=[MINT1], tx_signatures={MINT1: "sigABC"})
    assert pack["mints"][0]["links"]["helius_tx"] == "https://explorer.helius.xyz/tx/sigABC"
    pack2 = collector.collect(DEV1, mints=[MINT1])
    assert "helius_tx" not in pack2["mints"][0]["links"]


def test_collector_tx_signatures_list_assigns_in_order():
    collector = _collector(
        pairs={MINT1: _dex_pair(MINT1), MINT2: _dex_pair(MINT2)}, solscan=SOLSCAN_OK
    )
    pack = collector.collect(DEV1, mints=[MINT1, MINT2], tx_signatures=["sig1", "sig2"])
    assert pack["mints"][0]["links"]["helius_tx"].endswith("/sig1")
    assert pack["mints"][1]["links"]["helius_tx"].endswith("/sig2")


# --- EvidenceCollector: numbers ------------------------------------------

def test_collector_parses_dexscreener_numbers():
    collector = _collector(
        pairs={MINT1: _dex_pair(MINT1, symbol="GATO", change=-16.7, liquidity=237_713.0, mc=5_009_361.0)},
        solscan=SOLSCAN_OK,
    )
    entry = collector.collect(DEV1, mints=[MINT1])["mints"][0]
    assert entry["symbol"] == "GATO"
    assert entry["market_cap_usd"] == 5_009_361.0
    assert entry["liquidity_usd"] == 237_713.0
    assert entry["volume_24h_usd"] == 123_456.0
    assert entry["change_24h_pct"] == -16.7
    assert entry["price_usd"] == 0.001


def test_collector_missing_numbers_are_none_and_listed_as_missing():
    collector = _collector(pairs={}, solscan=SOLSCAN_OK)  # no pair for MINT1
    pack = collector.collect(DEV1, mints=[MINT1])
    entry = pack["mints"][0]
    assert entry["market_cap_usd"] is None
    assert entry["liquidity_usd"] is None
    assert entry["change_24h_pct"] is None
    assert entry["outcome"] == "inconclusive"
    assert entry["collapse_pct"] is None
    assert f"{MINT1}:market_cap_usd" in pack["missing"]
    assert f"{MINT1}:liquidity_usd" in pack["missing"]


def test_collector_solscan_balance_parsed_from_lamports():
    collector = _collector(pairs={MINT1: _dex_pair(MINT1)}, solscan=SOLSCAN_OK)
    wallet = collector.collect(DEV1, mints=[MINT1])["wallet"]
    assert wallet["sol_balance_sol"] == pytest.approx(18.556500065)
    assert wallet["owner_program"] == "11111111111111111111111111111111"
    assert wallet["solscan_api_available"] is True


def test_collector_solscan_failure_means_balance_na_not_invented():
    collector = _collector(pairs={MINT1: _dex_pair(MINT1)}, solscan=None, solscan_status=500)
    pack = collector.collect(DEV1, mints=[MINT1])
    assert pack["wallet"]["sol_balance_sol"] is None
    assert pack["wallet"]["solscan_api_available"] is False
    assert "wallet:sol_balance_sol" in pack["missing"]
    # Links are still present even though the API is down.
    assert pack["wallet"]["links"]["solscan_wallet"].startswith("https://solscan.io/account/")


# --- EvidenceCollector: registry merge ------------------------------------

def test_collector_merges_registry_evidence_and_collapse():
    registry = _registry_entry(DEV1, [MINT1])
    collector = _collector(pairs={MINT1: _dex_pair(MINT1)}, solscan=SOLSCAN_OK, registry=registry)
    entry = collector.collect(DEV1, mints=[MINT1])["mints"][0]
    assert entry["outcome"] == "rugged"
    assert entry["collapse_pct"] == 98.1
    assert entry["collapse_pct_source"] == "registry"
    assert entry["links"]["solscan_tx"] == "https://solscan.io/tx/SIG1111"
    assert entry["dev_sell_pct"] == 82.0
    assert entry["lp_removed"] is True


def test_collector_defaults_mints_from_registry_when_none_given():
    registry = _registry_entry(DEV1, [MINT1, MINT2])
    collector = _collector(
        pairs={MINT1: _dex_pair(MINT1), MINT2: _dex_pair(MINT2)},
        solscan=SOLSCAN_OK,
        registry=registry,
    )
    pack = collector.collect(DEV1)
    assert [m["mint"] for m in pack["mints"]] == [MINT1, MINT2]


def test_collector_invalid_addresses_raise():
    collector = _collector()
    with pytest.raises(ValueError):
        collector.collect(BAD, mints=[MINT1])
    with pytest.raises(ValueError):
        collector.collect(DEV1, mints=[BAD])
    with pytest.raises(ValueError):
        collector.collect(DEV1, mints=[MINT1], tx_signatures="sigABC")


def test_collector_unknown_dev_with_no_mints_is_empty_but_valid():
    collector = _collector(solscan=SOLSCAN_OK)
    pack = collector.collect(DEV2)
    assert pack["mints"] == []
    assert pack["dev_wallet"] == DEV2
    assert pack["completeness_score"] <= 1.0


# --- Completeness ----------------------------------------------------------

def test_completeness_is_1_with_full_data():
    collector = _collector(pairs={MINT1: _dex_pair(MINT1)}, solscan=SOLSCAN_OK)
    pack = collector.collect(DEV1, mints=[MINT1])
    assert pack["links_found"] == pack["links_expected"]
    assert pack["completeness_score"] == 1.0


def test_completeness_drops_when_dexscreener_has_no_pair():
    collector = _collector(pairs={}, solscan=SOLSCAN_OK)
    pack = collector.collect(DEV1, mints=[MINT1])
    assert pack["completeness_score"] < 1.0
    assert pack["links_found"] < pack["links_expected"]


# --- EvidenceVerifier -------------------------------------------------------

def _pack_for_verifier(mints):
    return {"dev_wallet": DEV1, "mints": mints}


def _mint_entry(mint, outcome="rugged", collapse=98.1, links=None):
    return {
        "mint": mint,
        "outcome": outcome,
        "collapse_pct": collapse,
        "links": links if links is not None else {"dexscreener": f"https://dexscreener.com/solana/{mint}"},
    }


def test_verifier_verified_with_two_rugged_mints_with_link_and_collapse():
    result = EvidenceVerifier.verify(_pack_for_verifier([_mint_entry(MINT1), _mint_entry(MINT2)]))
    assert result["verdict"] == "verified"
    assert result["flagged"] is True
    assert result["verified_rugged_mint_count"] == 2
    assert result["verified_mints"] == [MINT1, MINT2]


def test_verifier_insufficient_with_one_rugged_mint():
    result = EvidenceVerifier.verify(_pack_for_verifier([_mint_entry(MINT1)]))
    assert result["verdict"] == "insufficient evidence"
    assert result["flagged"] is False
    assert result["verified_rugged_mint_count"] == 1


def test_verifier_links_alone_never_verify_without_collapse_number():
    entries = [
        _mint_entry(MINT1, collapse=None),
        _mint_entry(MINT2, collapse=None),
    ]
    result = EvidenceVerifier.verify(_pack_for_verifier(entries))
    assert result["verdict"] == "insufficient evidence"
    assert result["flagged"] is False
    assert result["verified_rugged_mint_count"] == 0


def test_verifier_links_alone_never_verify_without_rugged_outcome():
    entries = [
        _mint_entry(MINT1, outcome="failed_but_honest", collapse=75.0),
        _mint_entry(MINT2, outcome="inconclusive", collapse=50.0),
    ]
    result = EvidenceVerifier.verify(_pack_for_verifier(entries))
    assert result["verdict"] == "insufficient evidence"
    assert result["flagged"] is False


def test_verifier_insufficient_with_no_mints():
    result = EvidenceVerifier.verify(_pack_for_verifier([]))
    assert result["verdict"] == "insufficient evidence"
    assert result["flagged"] is False
    assert result["verified_rugged_mint_count"] == 0


# --- EvidenceReport ---------------------------------------------------------

def _verified_pack_and_verification():
    registry = _registry_entry(DEV1, [MINT1, MINT2])
    collector = _collector(
        pairs={MINT1: _dex_pair(MINT1), MINT2: _dex_pair(MINT2)},
        solscan=SOLSCAN_OK,
        registry=registry,
    )
    result = gather_and_verify(DEV1, collector=collector, registry_data=registry)
    return result["pack"], result["verification"]


def test_report_render_has_verdict_links_and_verified_by_muse_line():
    pack, verification = _verified_pack_and_verification()
    text = EvidenceReport.render(pack, verification, date_str="2026-11-13")
    assert "# Evidence report" in text
    assert "Verified by Muse — 2026-11-13 UTC" in text
    assert "verified" in text
    assert MINT1 in text and MINT2 in text
    assert "https://dexscreener.com/solana/" in text
    assert "https://solscan.io/token/" in text
    assert "no community submissions" in text


def test_report_render_shows_na_for_missing_numbers():
    collector = _collector(pairs={}, solscan=None, solscan_status=500)
    result = gather_and_verify(DEV1, mints=[MINT1], collector=collector, registry_data={"entries": []})
    text = EvidenceReport.render(result["pack"], result["verification"], date_str="2026-11-13")
    assert "n/a" in text
    assert "insufficient evidence" in text
    assert "Missing data" in text


def test_report_write_creates_file_at_given_path(tmp_path):
    pack, verification = _verified_pack_and_verification()
    out = tmp_path / "evidence" / "report.md"
    written = EvidenceReport.write(pack, verification, out, date_str="2026-11-13")
    assert written == out
    assert out.is_file()
    assert "Verified by Muse" in out.read_text(encoding="utf-8")


def test_report_write_refuses_registry_path(tmp_path):
    pack, verification = _verified_pack_and_verification()
    with pytest.raises(ValueError, match="registry.json"):
        EvidenceReport.write(pack, verification, tmp_path / "registry.json")


def test_default_report_path_format():
    path = default_report_path(DEV1)
    assert path.name == f"{DEV1}.md"
    assert path.parent.name == "evidence"


# --- gather_and_verify + CLI -------------------------------------------------

def test_gather_and_verify_end_to_end_flagged():
    pack, verification = _verified_pack_and_verification()
    assert verification["flagged"] is True
    assert verification["verdict"] == "verified"
    assert pack["completeness_score"] == 1.0


def test_cli_exit_1_when_verified(tmp_path):
    registry = _registry_entry(DEV1, [MINT1, MINT2])
    collector = _collector(
        pairs={MINT1: _dex_pair(MINT1), MINT2: _dex_pair(MINT2)},
        solscan=SOLSCAN_OK,
        registry=registry,
    )
    out = tmp_path / "out.md"
    assert main(
        ["--dev", DEV1, "--out", str(out), "--date", "2026-11-13"],
        collector=collector,
        registry_data=registry,
    ) == 1
    assert out.is_file()
    assert "verified" in out.read_text(encoding="utf-8")


def test_cli_exit_0_when_unknown_dev(tmp_path):
    collector = _collector(pairs={}, solscan=SOLSCAN_OK)
    out = tmp_path / "out.md"
    assert main(
        ["--dev", DEV2, "--out", str(out), "--date", "2026-11-13"],
        collector=collector,
        registry_data={"entries": []},
    ) == 0
    assert "insufficient evidence" in out.read_text(encoding="utf-8")


def test_cli_exit_2_on_invalid_args():
    assert main([]) == 2
    assert main(["--dev", BAD]) == 2
    assert main(["--dev", DEV1, "--mint", BAD]) == 2
    assert main(["--dev", DEV1, "--tx", "no-colon"]) == 2
    assert main(["--dev", DEV1, "--date", "13/11/2026"]) == 2


def test_cli_json_prints_valid_json(tmp_path, capsys):
    registry = _registry_entry(DEV1, [MINT1, MINT2])
    collector = _collector(
        pairs={MINT1: _dex_pair(MINT1), MINT2: _dex_pair(MINT2)},
        solscan=SOLSCAN_OK,
        registry=registry,
    )
    out = tmp_path / "out.md"
    code = main(
        ["--dev", DEV1, "--out", str(out), "--json", "--date", "2026-11-13"],
        collector=collector,
        registry_data=registry,
    )
    assert code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["verification"]["verdict"] == "verified"
    assert data["report_path"] == str(out)
