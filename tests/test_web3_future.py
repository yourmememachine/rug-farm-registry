"""Tests for src.web3_future — synthetic data only, no network."""

import json

import pytest

from src.web3_future import (
    EXIT_INVALID,
    EXIT_OK,
    FutureChain,
    OnChainProbe,
    RoadmapAgent,
    future_risk,
    is_valid_address,
    main,
)

# Synthetic, well-known base58 addresses (no real dev wallet here).
SYSTEM_WALLET = "11111111111111111111111111111111"
OTHER_WALLET = "22222222222222222222222222222222"
MINT = "So11111111111111111111111111111111111111112"


def fake_rpc_system(method, params):
    if method == "getBalance":
        return {"value": 18556500065}
    if method == "getAccountInfo":
        return {"value": {"owner": "System Program"}}
    if method == "getTokenSupply":
        raise RuntimeError("not a Token mint")
    raise AssertionError(method)


def fake_rpc_mint(method, params):
    if method == "getBalance":
        return {"value": 0}
    if method == "getAccountInfo":
        return {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}
    if method == "getTokenSupply":
        return {"value": {"amount": "1000000", "decimals": 6}}
    raise AssertionError(method)


def failing_rpc(method, params):
    raise ConnectionError("network down")


# --- FutureChain -------------------------------------------------------

def test_chain_ids_include_solana_and_base():
    chain = FutureChain()
    assert "solana" in chain.chain_ids()
    assert "base" in chain.chain_ids()


def test_solana_live_base_planned():
    chain = FutureChain()
    assert chain.is_live("solana") is True
    assert chain.is_live("base") is False
    assert chain.live_chain_ids() == ["solana"]


def test_require_live_rejects_base():
    with pytest.raises(ValueError, match="not live yet"):
        FutureChain().require_live("base")


def test_unknown_chain_error():
    with pytest.raises(ValueError, match="Unknown chain"):
        FutureChain().get("ethereum")


def test_explorer_urls():
    chain = FutureChain()
    assert chain.explorer_address("solana", SYSTEM_WALLET) == (
        "https://solscan.io/account/" + SYSTEM_WALLET
    )
    assert chain.explorer_address("base", SYSTEM_WALLET) == (
        "https://basescan.org/address/" + SYSTEM_WALLET
    )
    assert chain.explorer_tx("solana", "abc") == "https://solscan.io/tx/abc"


# --- Address validation -------------------------------------------------

def test_valid_and_invalid_addresses():
    assert is_valid_address(SYSTEM_WALLET) is True
    assert is_valid_address(MINT) is True
    assert is_valid_address("not an address") is False
    assert is_valid_address("0" * 32) is False  # 0 is not base58
    assert is_valid_address("") is False
    assert is_valid_address("short") is False


# --- OnChainProbe -------------------------------------------------------

def test_probe_system_wallet():
    probe = OnChainProbe(rpc_call=fake_rpc_system)
    result = probe.probe_wallet(SYSTEM_WALLET)
    assert result["available"] is True
    assert result["kind"] == "system_wallet"
    assert result["owner"] == "System Program"
    assert result["lamports"] == 18556500065
    assert result["sol_balance"] == pytest.approx(18.556500065)
    assert result["explorer"].startswith("https://solscan.io/account/")


def test_probe_token_mint():
    probe = OnChainProbe(rpc_call=fake_rpc_mint)
    result = probe.probe_wallet(MINT)
    assert result["available"] is True
    assert result["kind"] == "token_mint"


def test_probe_rpc_failure_is_honest():
    probe = OnChainProbe(rpc_call=failing_rpc)
    result = probe.probe_wallet(SYSTEM_WALLET)
    assert result["available"] is False
    assert result["sol_balance"] is None
    assert result["kind"] is None
    assert "unavailable" in result["error"].lower()


def test_probe_invalid_address_and_chain():
    probe = OnChainProbe(rpc_call=fake_rpc_system)
    with pytest.raises(ValueError, match="Invalid Solana address"):
        probe.probe_wallet("nope")
    with pytest.raises(ValueError, match="not live yet"):
        probe.probe_wallet(SYSTEM_WALLET, chain="base")


# --- FutureWatch --------------------------------------------------------

def test_future_risk_ok():
    risk = future_risk({"liquidity": 237713, "change_24h": -16.7})
    assert risk["label"] == "ok"
    assert risk["signals"] == []


def test_future_risk_collapsed_is_watch_not_rugged():
    risk = future_risk({"liquidity": 2752, "change_24h": -91.5})
    assert risk["label"] == "watch"
    assert "collapsed" in risk["signals"]
    assert "rugged" not in json.dumps(risk).lower()


def test_future_risk_thin_and_missing_liquidity():
    thin = future_risk({"liquidity": 458, "change_24h": 37.6})
    assert thin["label"] == "watch"
    assert "thin_liquidity" in thin["signals"]
    missing = future_risk({"liquidity": None, "change_24h": 2.9})
    assert missing["label"] == "watch"
    assert "no_liquidity_reported" in missing["signals"]


# --- RoadmapAgent -------------------------------------------------------

def test_roadmap_has_three_versions_and_tbd_dates():
    text = RoadmapAgent().roadmap({"tests_passing": 132, "commits": 34})
    assert "v2.0" in text and "v2.1" in text and "v2.2" in text
    assert "Multi-chain" in text
    assert "Helius" in text
    assert "Muse-run evidence" in text
    assert "TBD" in text
    assert "132" in text
    assert "never auto-flag" in text.lower() or "never auto-flag" in text


# --- CLI ----------------------------------------------------------------

def test_cli_roadmap(capsys):
    assert main(["--roadmap"]) == EXIT_OK
    assert "v2.0" in capsys.readouterr().out


def test_cli_wallet_with_mock_probe(capsys):
    probe = OnChainProbe(rpc_call=fake_rpc_system)
    assert main(["--wallet", SYSTEM_WALLET], probe=probe) == EXIT_OK
    out = capsys.readouterr().out
    assert "system_wallet" in out
    assert "18.556500065" in out


def test_cli_wallet_json(capsys):
    probe = OnChainProbe(rpc_call=fake_rpc_system)
    assert main(["--wallet", SYSTEM_WALLET, "--json"], probe=probe) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "system_wallet"


def test_cli_risk(capsys):
    assert main(["--mc", "2903", "--liquidity", "2752", "--change", "-91.5"]) == EXIT_OK
    assert "watch" in capsys.readouterr().out


def test_cli_invalid():
    assert main([]) == EXIT_INVALID
    assert main(["--wallet", "nope"]) == EXIT_INVALID
    assert main(["--wallet", SYSTEM_WALLET, "--chain", "base"]) == EXIT_INVALID
