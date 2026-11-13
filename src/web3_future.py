"""Web3 future — chain-agnostic design for the rug-farm registry, v1.9.

A future-facing module, stdlib-only, no new dependency. It does
not move SOL, does not connect a wallet, does not hold a private
key, and never invents a number, a verdict, or a date.

Today the registry is Solana-only. This module is structured so
a second chain (e.g. Base / EVM) can be added later without
rewriting the verdict rules — verdicts stay chain-agnostic, only
the chain adapter changes.

    FutureChain  — chain registry: ids, names, explorer URLs,
                   and a "solana-only for now" guard
    OnChainProbe — public Solana RPC probe of a wallet address:
                   SOL balance, account owner, token mint vs
                   system wallet; honest "unavailable" on failure
    FutureWatch  — forward-looking risk signals from real
                   DexScreener data only: watch / ok, never "rugged"
    RoadmapAgent — the next 3 versions as markdown, dates TBD

    python -m src.web3_future --wallet <address> [--chain solana]
    python -m src.web3_future --roadmap
    python -m src.web3_future --mc 2903 --liquidity 2752 --change -91.5

Exit codes: 0 = success, 2 = invalid args / invalid address /
unsupported chain.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_INVALID = 2

SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
LAMPORTS_PER_SOL = 1_000_000_000
REPO_URL = "https://github.com/yourmememachine/rug-farm-registry"

# Base58 alphabet for Solana addresses (no 0, O, I, l).
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Chain registry. Only Solana is live today; Base is a planned
# second chain — its adapter is a stub, never used for verdicts
# until it is wired up and tested.
CHAINS = {
    "solana": {
        "chain_id": "solana",
        "chain_name": "Solana",
        "live": True,
        "explorer": "https://solscan.io",
        "address_path": "/account/{address}",
        "tx_path": "/tx/{tx}",
        "note": "Live today: verdicts, watchlist and probes run on Solana.",
    },
    "base": {
        "chain_id": "base",
        "chain_name": "Base",
        "live": False,
        "explorer": "https://basescan.org",
        "address_path": "/address/{address}",
        "tx_path": "/tx/{tx}",
        "note": "Planned: EVM adapter not wired up yet — no verdicts on Base.",
    },
}

# Future-risk thresholds, from real data only.
THIN_LIQUIDITY_USD = 1_000
COLLAPSED_CHANGE_PCT = -70


def is_valid_address(address: str) -> bool:
    """True for a plausible Solana base58 address (32-44 chars)."""
    return isinstance(address, str) and bool(_ADDRESS_RE.match(address))


class FutureChain:
    """Chain-agnostic registry design: one chain today, room for more.

    Verdicts never depend on the chain — only the explorer URLs
    and the live guard differ per chain.
    """

    def __init__(self, chains: dict | None = None) -> None:
        self._chains = dict(CHAINS if chains is None else chains)

    def chain_ids(self) -> list[str]:
        return sorted(self._chains)

    def live_chain_ids(self) -> list[str]:
        return sorted(cid for cid, c in self._chains.items() if c.get("live"))

    def get(self, chain_id: str) -> dict:
        """Chain record, or a clear error for an unknown chain."""
        chain = self._chains.get(chain_id)
        if chain is None:
            raise ValueError(
                f"Unknown chain: {chain_id!r}. Known chains: "
                + ", ".join(self.chain_ids())
            )
        return dict(chain)

    def is_live(self, chain_id: str) -> bool:
        return bool(self.get(chain_id).get("live"))

    def require_live(self, chain_id: str) -> dict:
        """Return the chain record, or raise if it is not live yet."""
        chain = self.get(chain_id)
        if not chain.get("live"):
            raise ValueError(
                f"Chain {chain_id!r} is not live yet — "
                "Solana only for now. See the roadmap (v2.0) for Base."
            )
        return chain

    def explorer_address(self, chain_id: str, address: str) -> str:
        chain = self.get(chain_id)
        return chain["explorer"] + chain["address_path"].format(address=address)

    def explorer_tx(self, chain_id: str, tx: str) -> str:
        chain = self.get(chain_id)
        return chain["explorer"] + chain["tx_path"].format(tx=tx)

    def summary(self) -> str:
        lines = ["Chains:"]
        for cid in self.chain_ids():
            chain = self._chains[cid]
            status = "live" if chain.get("live") else "planned"
            lines.append(f"  - {chain['chain_name']} ({cid}): {status}")
        lines.append("Solana only for now — verdicts are chain-agnostic,")
        lines.append("so a second chain can be added without rewriting them.")
        return "\n".join(lines)


def _default_rpc_call(rpc_url: str, method: str, params: list) -> dict:
    """One JSON-RPC call over stdlib urllib. Raises on any failure."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body.get("result", {})


class OnChainProbe:
    """Probe a dev wallet with the public Solana RPC (no key).

    The RPC client is injectable, so tests never touch the network.
    On any RPC failure the probe returns available=False and an
    error message — it never invents a balance or an owner.
    """

    def __init__(self, rpc_url: str = SOLANA_RPC_URL, rpc_call=None) -> None:
        self.rpc_url = rpc_url
        self._rpc_call = rpc_call or (
            lambda method, params: _default_rpc_call(rpc_url, method, params)
        )

    def probe_wallet(self, address: str, chain: str = "solana") -> dict:
        """Balance, owner and mint-vs-wallet for one address."""
        FutureChain().require_live(chain)
        if not is_valid_address(address):
            raise ValueError(f"Invalid Solana address: {address!r}")
        result: dict = {
            "address": address,
            "chain": chain,
            "available": False,
            "kind": None,
            "sol_balance": None,
            "lamports": None,
            "owner": None,
            "error": None,
        }
        try:
            balance = self._rpc_call("getBalance", [address])
            account = self._rpc_call(
                "getAccountInfo", [address, {"encoding": "jsonParsed"}]
            )
            supply = None
            try:
                supply = self._rpc_call("getTokenSupply", [address])
            except Exception:
                # Not a token mint — that is a normal outcome, not
                # a failure: a dev wallet has no token supply.
                supply = None
        except Exception as exc:  # noqa: BLE001 - RPC is untrusted input
            result["error"] = f"Solana RPC unavailable: {exc}"
            return result

        lamports = balance.get("value") if isinstance(balance, dict) else None
        owner = None
        if isinstance(account, dict):
            value = account.get("value") or {}
            owner = value.get("owner")
        result["available"] = True
        result["lamports"] = lamports
        result["sol_balance"] = (
            lamports / LAMPORTS_PER_SOL if isinstance(lamports, (int, float)) else None
        )
        result["owner"] = owner
        if supply is not None:
            result["kind"] = "token_mint"
        elif owner == "System Program":
            result["kind"] = "system_wallet"
        elif owner:
            result["kind"] = "program_wallet"
        else:
            result["kind"] = "unknown"
        result["explorer"] = FutureChain().explorer_address(chain, address)
        return result


def future_risk(data: dict) -> dict:
    """Forward-looking risk signals from real DexScreener data only.

    Labels are only "watch" or "ok". A token is never called
    "rugged" here — that needs dev-wallet + LP / dev-sell evidence.
    """
    signals: list[str] = []
    reasons: list[str] = []
    liquidity = data.get("liquidity")
    change_24h = data.get("change_24h")
    if liquidity is None:
        signals.append("no_liquidity_reported")
        reasons.append("No liquidity reported — exit risk unknown, watch it.")
    elif isinstance(liquidity, (int, float)) and liquidity < THIN_LIQUIDITY_USD:
        signals.append("thin_liquidity")
        reasons.append(f"Thin liquidity: ${liquidity:,.0f} (< $1,000).")
    if isinstance(change_24h, (int, float)) and change_24h <= COLLAPSED_CHANGE_PCT:
        signals.append("collapsed")
        reasons.append(f"Collapsed: {change_24h:.1f}% in 24h (<= -70%).")
    label = "watch" if signals else "ok"
    if not reasons:
        reasons.append("No future-risk signals in the reported data.")
    return {
        "label": label,
        "signals": signals,
        "reasons": reasons,
        "note": "Watch / ok only — a price drop alone is not a rug verdict.",
    }


class RoadmapAgent:
    """Generate the future roadmap from the current repo state.

    Dates are always TBD — this agent promises directions, never
    dates. No version is claimed as done until it ships.
    """

    VERSIONS = (
        {
            "version": "v2.0 — Multi-chain",
            "date": "TBD",
            "items": (
                "Wire up the Base (EVM) adapter in FutureChain",
                "Explorer + RPC adapters per chain, verdicts unchanged",
                "Same 2+ rugged mints + evidence rule, per chain",
            ),
        },
        {
            "version": "v2.1 — Live scan",
            "date": "TBD",
            "items": (
                "Helius live scan runs when HELIUS_API_KEY is set",
                "Daily alerts become real verified alerts, not blocked",
                "OnChainProbe feeds the scanner with live balances",
            ),
        },
        {
            "version": "v2.2 — Muse-run evidence",
            "date": "TBD",
            "items": (
                "Muse-run automated evidence gathering with links, verified by Muse itself",
                "No community dependency — evidence never auto-flags, 2+ verified mints still required",
                "Public evidence trail per flagged wallet",
            ),
        },
    )

    def roadmap(self, state: dict | None = None) -> str:
        state = state or {}
        lines = ["# Rug-Farm Registry — future roadmap", ""]
        lines.append("Current state:")
        lines.append(f"- Tests passing: {state.get('tests_passing', 'unknown')}")
        lines.append(f"- Commits: {state.get('commits', 'unknown')}")
        lines.append(f"- Chains live: {', '.join(state.get('chains_live', ['solana']))}")
        lines.append("- Solana only for now — verdicts are chain-agnostic.")
        lines.append("")
        for version in self.VERSIONS:
            lines.append(f"## {version['version']} (date: {version['date']})")
            for item in version["items"]:
                lines.append(f"- {item}")
            lines.append("")
        lines.append("Dates are TBD — directions, not promises.")
        lines.append(f"Repo: {REPO_URL}")
        return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None, probe: OnChainProbe | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.web3_future",
        description="Web3 future: chain design, on-chain probe, roadmap.",
    )
    parser.add_argument("--wallet", help="Solana wallet / mint address to probe")
    parser.add_argument("--chain", default="solana", help="Chain id (default: solana)")
    parser.add_argument("--roadmap", action="store_true", help="Print the roadmap")
    parser.add_argument("--mc", type=float, help="Market cap in USD (for risk check)")
    parser.add_argument("--liquidity", type=float, help="Liquidity in USD")
    parser.add_argument("--change", type=float, help="24h change in percent")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    if args.roadmap:
        print(RoadmapAgent().roadmap())
        return EXIT_OK

    if args.wallet:
        try:
            probe = probe or OnChainProbe()
            result = probe.probe_wallet(args.wallet, chain=args.chain)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Address: {result['address']} ({result['chain']})")
            if not result["available"]:
                print(f"Probe unavailable: {result['error']}")
                print("No numbers invented — try again later.")
            else:
                print(f"Kind: {result['kind']}")
                print(f"SOL balance: {result['sol_balance']}")
                print(f"Owner: {result['owner']}")
                print(f"Explorer: {result.get('explorer')}")
        return EXIT_OK

    if args.mc is not None or args.liquidity is not None or args.change is not None:
        risk = future_risk(
            {
                "market_cap": args.mc,
                "liquidity": args.liquidity,
                "change_24h": args.change,
            }
        )
        if args.json:
            print(json.dumps(risk, indent=2))
        else:
            print(f"Future risk: {risk['label']}")
            for reason in risk["reasons"]:
                print(f"  - {reason}")
            print(f"  {risk['note']}")
        return EXIT_OK

    parser.print_usage(sys.stderr)
    print(
        "Error: give --wallet <address>, --roadmap, or risk data "
        "(--mc / --liquidity / --change).",
        file=sys.stderr,
    )
    return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
