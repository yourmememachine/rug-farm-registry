"""Tests for v2.0 multi-chain (src/multichain.py).

Synthetic data only, no network: every RPC call is mocked.
No real rugged wallet or mint is invented anywhere.
"""

import json

import pytest

from src.multichain import (
    BaseAdapter,
    ChainCompare,
    MultiChainRegistry,
    SolanaAdapter,
    get_adapter,
    is_valid_evm_address,
    main,
    supported_chains,
)

# Well-known synthetic addresses only.
SOL_WALLET = "2tWpBrPeJJfnznZ7m9iYyKcCZyjJVW2uNzvpJ45QS914"
SOL_MINT = "77HqxiDCgYfXUsDXDD6YVefPhTTz1Z2n8jX4gTxipump"
BASE_WALLET = "0x0000000000000000000000000000000000000001"
BASE_CONTRACT = "0x00000000000000000000000000000000000000a0"
SAMPLE_FLAGGED_DEV = "SAMPLEDEV11111111111111111111111111111111"

PROBE_KEYS = {
    "chain", "address", "available", "kind", "balance", "balance_native",
    "balance_raw", "owner", "is_contract", "explorer", "dexscreener", "error",
}


def sol_rpc_mock(method, params):
    """Mocked Solana RPC: 18.5565 SOL, System Program owner, no supply."""
    if method == "getBalance":
        return {"value": 18_556_500_065}
    if method == "getAccountInfo":
        return {"value": {"owner": "System Program"}}
    if method == "getTokenSupply":
        raise RuntimeError("not a Token mint")
    raise AssertionError(method)


def base_rpc_mock(method, params):
    """Mocked Base RPC: 1 ETH, no contract code."""
    if method == "eth_getBalance":
        return "0xde0b6b3a7640000"  # 1 ETH in wei
    if method == "eth_getCode":
        return "0x"
    raise AssertionError(method)


def base_contract_rpc_mock(method, params):
    if method == "eth_getBalance":
        return "0x0"
    if method == "eth_getCode":
        return "0x60006000"
    raise AssertionError(method)


def failing_rpc_mock(method, params):
    raise RuntimeError("boom")


# --- adapters: routing and explorer URLs ---

def test_supported_chains():
    assert supported_chains() == ["base", "solana"]


def test_get_adapter_routing():
    assert isinstance(get_adapter("solana"), SolanaAdapter)
    assert isinstance(get_adapter("base"), BaseAdapter)


def test_get_adapter_unsupported_chain():
    with pytest.raises(ValueError, match="Unsupported chain"):
        get_adapter("ethereum")


def test_explorer_urls():
    assert get_adapter("solana").explorer_address(SOL_WALLET).startswith(
        "https://solscan.io/account/")
    assert get_adapter("base").explorer_address(BASE_WALLET).startswith(
        "https://basescan.org/address/")
    assert "dexscreener.com/solana/" in get_adapter("solana").dexscreener_url(SOL_MINT)
    assert "dexscreener.com/base/" in get_adapter("base").dexscreener_url(BASE_WALLET)


# --- address validation ---

def test_address_validation():
    assert get_adapter("solana").is_valid_address(SOL_WALLET)
    assert not get_adapter("solana").is_valid_address(BASE_WALLET)
    assert not get_adapter("solana").is_valid_address("not-an-address")
    assert is_valid_evm_address(BASE_WALLET)
    assert not is_valid_evm_address(SOL_WALLET)
    assert not is_valid_evm_address("0x123")


# --- Base probe ---

def test_base_probe_wallet_balance_parsing():
    result = BaseAdapter(rpc_call=base_rpc_mock).probe(BASE_WALLET)
    assert result["available"] is True
    assert result["kind"] == "wallet"
    assert result["balance"] == 1.0
    assert result["balance_raw"] == 10**18
    assert result["balance_native"] == 1.0
    assert result["is_contract"] is False
    assert result["owner"] is None
    assert result["error"] is None


def test_base_probe_contract():
    result = BaseAdapter(rpc_call=base_contract_rpc_mock).probe(BASE_CONTRACT)
    assert result["available"] is True
    assert result["kind"] == "contract"
    assert result["is_contract"] is True
    assert result["balance"] == 0.0


def test_base_probe_rpc_failure_is_honest():
    result = BaseAdapter(rpc_call=failing_rpc_mock).probe(BASE_WALLET)
    assert result["available"] is False
    assert result["balance"] is None
    assert result["balance_raw"] is None
    assert "Base RPC unavailable" in result["error"]


def test_base_probe_invalid_address():
    with pytest.raises(ValueError, match="Invalid Base"):
        BaseAdapter(rpc_call=base_rpc_mock).probe(SOL_WALLET)


# --- Solana probe + shape parity ---

def test_solana_probe_shape_parity_with_base():
    sol = SolanaAdapter(rpc_call=sol_rpc_mock).probe(SOL_WALLET)
    base = BaseAdapter(rpc_call=base_rpc_mock).probe(BASE_WALLET)
    assert set(sol) == PROBE_KEYS == set(base)
    assert sol["available"] is True
    assert sol["kind"] == "system_wallet"
    assert sol["balance"] == pytest.approx(18.556500065)
    assert sol["owner"] == "System Program"
    assert sol["balance_raw"] == 18_556_500_065


def test_solana_probe_rpc_failure_is_honest():
    result = SolanaAdapter(rpc_call=failing_rpc_mock).probe(SOL_WALLET)
    assert result["available"] is False
    assert result["balance"] is None
    assert "Solana RPC unavailable" in result["error"]


# --- MultiChainRegistry ---

def test_registry_solana_unknown_wallet():
    from src.web3_future import OnChainProbe

    probe = OnChainProbe(rpc_call=sol_rpc_mock)
    result = MultiChainRegistry(data={"entries": []}).check(SOL_WALLET, "solana", probe=probe)
    assert result["flagged"] is False
    assert result["exit_code"] == 0
    assert result["verdict"]["risk_level"] == "unknown"
    assert result["probe"]["available"] is True


def test_registry_solana_flagged_sample_dev():
    from src.web3_future import OnChainProbe

    probe = OnChainProbe(rpc_call=sol_rpc_mock)
    result = MultiChainRegistry().check(SAMPLE_FLAGGED_DEV, "solana", probe=probe)
    assert result["flagged"] is True
    assert result["exit_code"] == 1
    assert result["verdict"]["rugged_mint_count"] >= 2


def test_registry_base_has_no_coverage_but_probe_works():
    # Mocked adapter: no network, probe still runs, verdict stays
    # unknown because the registry has no Base coverage yet.
    class MockBase(BaseAdapter):
        def probe(self, address):
            out = self._base_result(address)
            out.update({"available": True, "kind": "wallet", "balance": 1.0,
                        "balance_native": 1.0, "balance_raw": 10**18,
                        "is_contract": False})
            return out

    reg = MultiChainRegistry(data={"entries": []},
                             adapters={"base": MockBase, "solana": SolanaAdapter})
    result = reg.check(BASE_WALLET, "base")
    assert result["probe"]["available"] is True
    assert result["verdict"]["risk_level"] == "unknown"
    assert result["flagged"] is False
    assert "No Base registry coverage yet" in result["verdict"]["reasons"][0]


def test_registry_base_probe_with_mocked_adapter():
    class MockBase(BaseAdapter):
        def probe(self, address):
            out = self._base_result(address)
            out.update({"available": True, "kind": "wallet", "balance": 2.5,
                        "balance_native": 2.5, "balance_raw": 2_500_000_000_000_000_000,
                        "is_contract": False})
            return out

    reg = MultiChainRegistry(data={"entries": []}, adapters={"base": MockBase, "solana": SolanaAdapter})
    result = reg.check(BASE_WALLET, "base")
    assert result["probe"]["available"] is True
    assert result["probe"]["balance"] == 2.5
    assert result["flagged"] is False


def test_registry_unsupported_chain():
    with pytest.raises(ValueError, match="Unsupported chain"):
        MultiChainRegistry().check(SOL_WALLET, "ethereum")


# --- ChainCompare ---

def test_compare_labels_and_winners():
    result = ChainCompare.compare(
        {"chain": "solana", "symbol": "GATO", "market_cap": 5_009_361,
         "liquidity": 237_713, "change_24h": -16.7},
        {"chain": "base", "symbol": "GATO", "market_cap": 120_000,
         "liquidity": 800, "change_24h": -91.5},
    )
    assert result["chain_a"]["label"] == "ok"
    assert result["chain_b"]["label"] == "watch"
    assert result["higher_liquidity"] == "solana"
    assert result["higher_market_cap"] == "solana"
    assert result["chain_a"]["label"] != "rugged"
    assert result["chain_b"]["label"] != "rugged"


def test_compare_missing_liquidity_is_watch():
    result = ChainCompare.compare(
        {"chain": "solana", "symbol": "X", "market_cap": None,
         "liquidity": None, "change_24h": None},
        {"chain": "base", "symbol": "X", "market_cap": None,
         "liquidity": 50_000, "change_24h": 5.0},
    )
    assert result["chain_a"]["label"] == "watch"
    assert result["chain_b"]["label"] == "ok"
    assert result["higher_liquidity"] is None


# --- CLI ---

def test_cli_list_chains(capsys):
    assert main(["--list-chains"]) == 0
    out = capsys.readouterr().out
    assert "solana" in out
    assert "base" in out


def test_cli_compare_negative_change(capsys):
    assert main([
        "--compare", "--chain-a", "solana", "--chain-b", "base",
        "--mc-a", "5000000", "--liquidity-a", "237713", "--change-a", "-16.7",
        "--mc-b", "120000", "--liquidity-b", "800", "--change-b", "-91.5",
    ]) == 0
    out = capsys.readouterr().out
    assert "solana: TOKEN — ok" in out
    assert "base: TOKEN — watch" in out


def test_cli_invalid_chain(capsys):
    assert main(["--chain", "ethereum", "--address", SOL_WALLET]) == 2


def test_cli_invalid_address(capsys):
    assert main(["--chain", "base", "--address", "not-an-address"]) == 2


def test_cli_no_args(capsys):
    assert main([]) == 2
