"""Multi-chain — v2.0, the first roadmap item from web3_future.

The registry goes multi-chain without rewriting its verdicts.
One adapter per chain, one shared return shape, one flagging
rule that never depends on the chain:

    ChainAdapter   — base class: explorer links, address checks,
                     on-chain probe, all in one shared shape
    SolanaAdapter  — DexScreener Solana, Solscan links, public
                     Solana RPC probe (reuses web3_future)
    BaseAdapter    — DexScreener Base, Basescan links, public
                     Base RPC (https://mainnet.base.org, no key):
                     eth_getBalance / eth_getCode
    MultiChainRegistry — route a check to the right adapter and
                     add the registry verdict (chain-independent)
    ChainCompare   — same token data on two chains, side by side

Stdlib only, no new dependency, no wallet, no funds moved, no
private key. Network calls are injectable, so tests never touch
the network. A probe that cannot reach its RPC says so honestly
(available=False) instead of inventing a balance.

The registry itself is still Solana sample data only. A Base
check therefore always reports its registry verdict as unknown
("no Base registry coverage yet") — the on-chain probe still
works, the verdict does not pretend otherwise.

    python -m src.multichain --chain solana --address <address>
    python -m src.multichain --chain base --address 0x...
    python -m src.multichain --compare --chain-a solana --chain-b base \
        --mc-a 5000000 --liquidity-a 237713 --change-a -16.7 \
        --mc-b 120000 --liquidity-b 800 --change-b -91.5
    python -m src.multichain --list-chains

Exit codes: 0 = ok, 2 = invalid args / chain / address.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from src import registry as registry_mod
from src.web3_future import (
    SOLANA_RPC_URL,
    OnChainProbe,
    _default_rpc_call,
    future_risk,
    is_valid_address as is_valid_solana_address,
)

EXIT_OK = 0
EXIT_INVALID = 2

BASE_RPC_URL = "https://mainnet.base.org"
WEI_PER_ETH = 10**18
REPO_URL = "https://github.com/yourmememachine/rug-farm-registry"

# EVM address: 0x + 40 hex chars.
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# DexScreener chain slugs, one per supported chain.
DEXSCREENER_CHAIN = {"solana": "solana", "base": "base"}


def is_valid_evm_address(address: str) -> bool:
    """True for a 0x-prefixed 40-hex-char EVM address."""
    return isinstance(address, str) and bool(_EVM_ADDRESS_RE.match(address))


def _base_rpc_call(rpc_url: str, method: str, params: list) -> dict:
    """One Base JSON-RPC call (eth_* methods return hex strings)."""
    result = _default_rpc_call(rpc_url, method, params)
    return result


class ChainAdapter:
    """Base class: one adapter per chain, one shared return shape.

    Every probe returns the same keys, so callers never have to
    branch on the chain to read a result:

        chain, address, available, kind, balance, balance_native,
        balance_raw, owner, is_contract, explorer, dexscreener,
        error

    ``balance`` is always in the chain's native token (SOL / ETH),
    ``balance_raw`` in the smallest unit (lamports / wei).
    ``kind`` is "wallet", "contract", "token_mint", "system_wallet"
    or "program_wallet". ``owner`` is None on EVM chains, which
    have no account-owner field; ``is_contract`` is None on
    Solana, where the concept does not apply the same way.
    """

    chain_id: str = ""
    chain_name: str = ""
    native_symbol: str = ""
    rpc_url: str = ""
    explorer: str = ""

    def __init__(self, rpc_url: str | None = None, rpc_call=None) -> None:
        if rpc_url is not None:
            self.rpc_url = rpc_url
        self._rpc_call = rpc_call

    def is_valid_address(self, address: str) -> bool:
        raise NotImplementedError

    def explorer_address(self, address: str) -> str:
        path = "/account/{address}" if self.chain_id == "solana" else "/address/{address}"
        return self.explorer + path.format(address=address)

    def explorer_tx(self, tx: str) -> str:
        return self.explorer + f"/tx/{tx}"

    def dexscreener_url(self, address: str) -> str:
        slug = DEXSCREENER_CHAIN[self.chain_id]
        return f"https://dexscreener.com/{slug}/{address}"

    def probe(self, address: str) -> dict:
        raise NotImplementedError

    def _base_result(self, address: str) -> dict:
        return {
            "chain": self.chain_id,
            "address": address,
            "available": False,
            "kind": None,
            "balance": None,
            "balance_native": None,
            "balance_raw": None,
            "owner": None,
            "is_contract": None,
            "explorer": self.explorer_address(address),
            "dexscreener": self.dexscreener_url(address),
            "error": None,
        }


class SolanaAdapter(ChainAdapter):
    """Solana adapter: public Solana RPC, Solscan links."""

    chain_id = "solana"
    chain_name = "Solana"
    native_symbol = "SOL"
    rpc_url = SOLANA_RPC_URL
    explorer = "https://solscan.io"

    def is_valid_address(self, address: str) -> bool:
        return is_valid_solana_address(address)

    def probe(self, address: str, probe: OnChainProbe | None = None) -> dict:
        result = self._base_result(address)
        if not self.is_valid_address(address):
            raise ValueError(f"Invalid Solana address: {address!r}")
        raw = (probe or OnChainProbe(rpc_url=self.rpc_url, rpc_call=self._rpc_call)).probe_wallet(
            address, chain="solana"
        )
        if not raw.get("available"):
            result["error"] = raw.get("error") or "Solana RPC unavailable."
            return result
        result["available"] = True
        result["kind"] = raw.get("kind")
        result["balance"] = raw.get("sol_balance")
        result["balance_native"] = raw.get("sol_balance")
        result["balance_raw"] = raw.get("lamports")
        result["owner"] = raw.get("owner")
        # EVM-style contract flag does not apply on Solana.
        result["is_contract"] = None
        return result


class BaseAdapter(ChainAdapter):
    """Base adapter: public Base RPC (no key), Basescan links.

    Balance via eth_getBalance (wei -> ETH), contract detection
    via eth_getCode (anything other than "0x" is a contract).
    """

    chain_id = "base"
    chain_name = "Base"
    native_symbol = "ETH"
    rpc_url = BASE_RPC_URL
    explorer = "https://basescan.org"

    def is_valid_address(self, address: str) -> bool:
        return is_valid_evm_address(address)

    def probe(self, address: str) -> dict:
        result = self._base_result(address)
        if not self.is_valid_address(address):
            raise ValueError(f"Invalid Base (EVM) address: {address!r}")
        call = self._rpc_call or (
            lambda method, params: _base_rpc_call(self.rpc_url, method, params)
        )
        try:
            balance_hex = call("eth_getBalance", [address, "latest"])
            code = call("eth_getCode", [address, "latest"])
        except Exception as exc:  # noqa: BLE001 - RPC is untrusted input
            result["error"] = f"Base RPC unavailable: {exc}"
            return result
        try:
            wei = int(balance_hex, 16) if isinstance(balance_hex, str) else None
        except (TypeError, ValueError):
            wei = None
        is_contract = isinstance(code, str) and code not in ("0x", "0x0", "")
        result["available"] = True
        result["kind"] = "contract" if is_contract else "wallet"
        result["balance_raw"] = wei
        result["balance"] = wei / WEI_PER_ETH if isinstance(wei, int) else None
        result["balance_native"] = result["balance"]
        result["owner"] = None
        result["is_contract"] = is_contract
        return result


# Adapters in registry order. Base is live as a probe in v2.0;
# the *registry verdict* on Base is still unknown (see below).
ADAPTERS: dict[str, type[ChainAdapter]] = {
    "solana": SolanaAdapter,
    "base": BaseAdapter,
}


def supported_chains() -> list[str]:
    return sorted(ADAPTERS)


def get_adapter(chain: str, rpc_call=None) -> ChainAdapter:
    """Adapter for a chain, or a clear error for an unknown one."""
    cls = ADAPTERS.get(chain)
    if cls is None:
        raise ValueError(
            f"Unsupported chain: {chain!r}. Supported chains: "
            + ", ".join(supported_chains())
        )
    return cls(rpc_call=rpc_call)


def _default_registry_data() -> dict:
    """Load data/registry.json if present, else an empty registry."""
    path = Path(__file__).resolve().parent.parent / "data" / "registry.json"
    try:
        return registry_mod.load_registry(path)
    except Exception:
        return {"entries": []}


class MultiChainRegistry:
    """Check a wallet/mint on a named chain, verdicts unchanged.

    The flagging rule is chain-independent: flagged only when the
    registry says so (>= 2 rugged mints, each with evidence).
    Because the registry is Solana sample data only, a Base check
    always reports registry verdict "unknown" with the reason
    "no Base registry coverage yet" — the probe still runs, the
    verdict never pretends Base coverage exists.
    """

    def __init__(self, data: dict | None = None, adapters: dict | None = None) -> None:
        self._data = data if data is not None else _default_registry_data()
        self._adapters = dict(ADAPTERS if adapters is None else adapters)

    def supported_chains(self) -> list[str]:
        return sorted(self._adapters)

    def check(self, address: str, chain: str, mint: str | None = None,
              probe: object | None = None) -> dict:
        """Probe + registry verdict for one address on one chain."""
        cls = self._adapters.get(chain)
        if cls is None:
            raise ValueError(
                f"Unsupported chain: {chain!r}. Supported chains: "
                + ", ".join(self.supported_chains())
            )
        adapter = cls() if probe is None else cls(rpc_call=getattr(probe, "_rpc_call", None))
        if chain == "solana" and probe is not None and isinstance(probe, OnChainProbe):
            probe_result = adapter.probe(address, probe=probe)
        elif chain == "solana":
            probe_result = adapter.probe(address)
        else:
            probe_result = adapter.probe(address)
        if chain == "solana":
            verdict = registry_mod.check_wallet(self._data, address, mint=mint)
        else:
            verdict = {
                "dev_wallet": address,
                "mint": mint,
                "mint_in_registry": None if mint is None else False,
                "flagged": False,
                "risk_level": "unknown",
                "rugged_mint_count": 0,
                "cluster_id": None,
                "reasons": [
                    "No Base registry coverage yet — the registry is "
                    "Solana sample data only. This is a probe, not a verdict."
                ],
                "evidence": [],
            }
        return {
            "chain": chain,
            "address": address,
            "mint": mint,
            "probe": probe_result,
            "verdict": verdict,
            "flagged": bool(verdict.get("flagged")),
            "exit_code": 1 if verdict.get("flagged") else 0,
            "note": "Verdicts are chain-independent: flagged only on "
                    ">= 2 rugged mints with evidence, on any chain.",
        }


class ChainCompare:
    """Same token data on two chains, side by side.

    Labels are only watch / ok per chain (from real numbers, via
    future_risk). A token is never called "rugged" here — that
    needs dev-wallet + LP / dev-sell evidence.
    """

    @staticmethod
    def compare(chain_a: dict, chain_b: dict) -> dict:
        risk_a = future_risk(
            {"liquidity": chain_a.get("liquidity"), "change_24h": chain_a.get("change_24h")}
        )
        risk_b = future_risk(
            {"liquidity": chain_b.get("liquidity"), "change_24h": chain_b.get("change_24h")}
        )
        def _num(value):
            return value if isinstance(value, (int, float)) else None
        liq_a, liq_b = _num(chain_a.get("liquidity")), _num(chain_b.get("liquidity"))
        mc_a, mc_b = _num(chain_a.get("market_cap")), _num(chain_b.get("market_cap"))
        ch_a, ch_b = _num(chain_a.get("change_24h")), _num(chain_b.get("change_24h"))
        higher_liquidity = None
        if liq_a is not None and liq_b is not None and liq_a != liq_b:
            higher_liquidity = chain_a.get("chain") if liq_a > liq_b else chain_b.get("chain")
        higher_mc = None
        if mc_a is not None and mc_b is not None and mc_a != mc_b:
            higher_mc = chain_a.get("chain") if mc_a > mc_b else chain_b.get("chain")
        return {
            "chain_a": {
                "chain": chain_a.get("chain"),
                "symbol": chain_a.get("symbol"),
                "market_cap": mc_a,
                "liquidity": liq_a,
                "change_24h": ch_a,
                "label": risk_a["label"],
                "reasons": risk_a["reasons"],
            },
            "chain_b": {
                "chain": chain_b.get("chain"),
                "symbol": chain_b.get("symbol"),
                "market_cap": mc_b,
                "liquidity": liq_b,
                "change_24h": ch_b,
                "label": risk_b["label"],
                "reasons": risk_b["reasons"],
            },
            "higher_liquidity": higher_liquidity,
            "higher_market_cap": higher_mc,
            "note": "Watch / ok only — a price drop alone is not a rug verdict.",
        }


def _join_dash_values(argv: list[str]) -> list[str]:
    """Rewrite `--opt -80` as `--opt=-80` (argparse rejects it)."""
    float_opts = {
        "--mc-a", "--liquidity-a", "--change-a",
        "--mc-b", "--liquidity-b", "--change-b",
    }
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in float_opts and i + 1 < len(argv) and argv[i + 1].startswith("-"):
            out.append(f"{token}={argv[i + 1]}")
            i += 2
        else:
            out.append(token)
            i += 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.multichain",
        description="Multi-chain rug check: Solana + Base, same verdict rule.",
    )
    parser.add_argument("--chain", default="solana", help="Chain: solana or base")
    parser.add_argument("--address", help="Wallet / mint address to check")
    parser.add_argument("--mint", help="Optional mint to check against the registry")
    parser.add_argument("--list-chains", action="store_true", help="List supported chains")
    parser.add_argument("--compare", action="store_true", help="Compare two chains")
    parser.add_argument("--chain-a", default="solana")
    parser.add_argument("--chain-b", default="base")
    parser.add_argument("--symbol-a", default="TOKEN")
    parser.add_argument("--symbol-b", default="TOKEN")
    parser.add_argument("--mc-a", type=float, default=None)
    parser.add_argument("--mc-b", type=float, default=None)
    parser.add_argument("--liquidity-a", type=float, default=None)
    parser.add_argument("--liquidity-b", type=float, default=None)
    parser.add_argument("--change-a", type=float, default=None)
    parser.add_argument("--change-b", type=float, default=None)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(_join_dash_values(list(argv) if argv is not None else sys.argv[1:]))

    if args.list_chains:
        print("Supported chains: " + ", ".join(supported_chains()))
        for chain in supported_chains():
            adapter = get_adapter(chain)
            print(f"  - {adapter.chain_name} ({chain}): {adapter.explorer}")
        return EXIT_OK

    if args.compare:
        result = ChainCompare.compare(
            {"chain": args.chain_a, "symbol": args.symbol_a,
             "market_cap": args.mc_a, "liquidity": args.liquidity_a,
             "change_24h": args.change_a},
            {"chain": args.chain_b, "symbol": args.symbol_b,
             "market_cap": args.mc_b, "liquidity": args.liquidity_b,
             "change_24h": args.change_b},
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for side in ("chain_a", "chain_b"):
                entry = result[side]
                print(f"{entry['chain']}: {entry['symbol']} — {entry['label']}")
                for reason in entry["reasons"]:
                    print(f"  - {reason}")
            if result["higher_liquidity"]:
                print(f"Higher liquidity: {result['higher_liquidity']}")
            if result["higher_market_cap"]:
                print(f"Higher market cap: {result['higher_market_cap']}")
            print(f"  {result['note']}")
        return EXIT_OK

    if args.address:
        try:
            result = MultiChainRegistry().check(args.address, args.chain, mint=args.mint)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            probe = result["probe"]
            verdict = result["verdict"]
            print(f"Address: {result['address']} ({result['chain']})")
            if not probe["available"]:
                print(f"Probe unavailable: {probe['error']}")
                print("No numbers invented — try again later.")
            else:
                print(f"Kind: {probe['kind']}")
                print(f"Balance: {probe['balance']} {get_adapter(args.chain).native_symbol}")
                print(f"Explorer: {probe['explorer']}")
            print(f"Verdict: {verdict['risk_level']} (flagged: {result['flagged']})")
            for reason in verdict["reasons"]:
                print(f"  - {reason}")
            print(f"  {result['note']}")
        return EXIT_OK

    parser.print_usage(sys.stderr)
    print("Error: give --address <address>, --compare, or --list-chains.", file=sys.stderr)
    return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
