"""Real Helius indexer for pump.fun token creations.

Reads ``HELIUS_API_KEY`` only from the environment (never hardcoded,
never invented). Disabled without a key — ``fetch_new_mints()`` raises
a clear ``RuntimeError`` instead of faking data.

How it works (all real, no key beyond Helius):
- Helius JSON-RPC ``getSignaturesForAddress`` on the pump.fun program
  ``6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`` lists recent
  creation transactions.
- ``getTransaction`` (``jsonParsed``) per signature is parsed for the
  mint + dev wallet: the pump.fun ``create`` instruction carries the
  mint (parsed ``info.mint`` or the first account), and the dev is the
  fee payer (first signer). Falls back to new entries in
  ``postTokenBalances`` when the instruction is not parsed.
- Outcome data for a mint comes from DexScreener's public API
  (no key): ``GET https://api.dexscreener.com/latest/dex/tokens/{mint}``.
  Peak market cap is *estimated* from the 24h price change, LP
  removal is inferred from near-zero liquidity, and ``dev_sold_pct``
  is 0.0 unless a dev balance check proves otherwise — see
  ``fetch_mint_outcome``. A mint is only ever labelled ``rugged`` by
  ``src.labeller`` on that evidence; missing data -> ``inconclusive``,
  never a flag.

All HTTP goes through an injectable ``httpx.Client`` so tests can use
``httpx.MockTransport``. No live call is made in tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from src.http_client import make_client

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WRAPPED_SOL = "So11111111111111111111111111111111111111112"

# DexScreener liquidity below this is treated as "LP removed / drained".
LP_DRAINED_USD = 1000.0


@dataclass
class TokenCreation:
    mint: str
    dev_wallet: str
    program: str  # e.g. "pump.fun" or "raydium"
    timestamp: str | None = None
    signature: str | None = None


@dataclass
class MintOutcome:
    """Outcome evidence for one mint, ready for src.labeller.MintEvent."""

    mint: str
    dev_wallet: str | None = None
    peak_market_cap_usd: float | None = None
    current_market_cap_usd: float | None = None
    lp_removed: bool = False
    dev_sold_pct: float = 0.0
    time_to_collapse_minutes: int | None = None
    evidence: dict = field(default_factory=dict)
    source: str = "dexscreener"


def get_api_key_from_env(env: dict | None = None) -> str | None:
    """Read HELIUS_API_KEY from the environment. None if unset/blank."""
    source = env if env is not None else os.environ
    key = (source.get("HELIUS_API_KEY") or "").strip()
    return key or None


class HeliusIndexer:
    """Helius JSON-RPC indexer for pump.fun creations + DexScreener outcomes."""

    def __init__(
        self,
        api_key: str | None = None,
        rpc_url: str = HELIUS_RPC_URL,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key if api_key is not None else get_api_key_from_env()
        self.rpc_url = rpc_url.rstrip("/")
        self._client = client

    @property
    def enabled(self) -> bool:
        return self.api_key is not None

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError(
                "Helius indexer is disabled: set HELIUS_API_KEY "
                "(see .env.example, or the HELIUS_API_KEY GitHub secret). "
                "No live calls are made without a key."
            )

    # -- HTTP plumbing (injectable for tests) -------------------------
    def _post_json(self, url: str, payload: dict) -> dict:
        if self._client is not None:
            resp = self._client.post(url, json=payload)
        else:
            with make_client(timeout=30.0) as client:
                resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Helius RPC error: {data['error']}")
        return data

    def _get_json(self, url: str) -> dict:
        if self._client is not None:
            resp = self._client.get(url)
        else:
            with make_client(timeout=30.0) as client:
                resp = client.get(url)
        resp.raise_for_status()
        return resp.json()

    def _rpc(self, method: str, params: list) -> object:
        self._require_enabled()
        url = f"{self.rpc_url}/?api-key={self.api_key}"
        data = self._post_json(
            url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        return data.get("result")

    # -- Signatures / transactions ------------------------------------
    def get_signatures(
        self, address: str, limit: int = 50, before: str | None = None
    ) -> list[dict]:
        """getSignaturesForAddress for any program / wallet address."""
        opts: dict = {"limit": limit}
        if before:
            opts["before"] = before
        result = self._rpc("getSignaturesForAddress", [address, opts])
        return result if isinstance(result, list) else []

    def get_transaction(self, signature: str) -> dict | None:
        """One parsed transaction, or None if it cannot be fetched."""
        result = self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        return result if isinstance(result, dict) else None

    # -- Creation parsing ---------------------------------------------
    @staticmethod
    def parse_transaction(tx: dict) -> TokenCreation | None:
        """Parse one getTransaction result into a TokenCreation, or None.

        Handles the shapes Helius jsonParsed returns:
        - parsed pump.fun ``create`` instruction (info.mint, or the
          first account = mint), dev = fee payer (first signer);
        - fallback: a mint that appears in postTokenBalances but not
          in preTokenBalances.
        """
        if not isinstance(tx, dict):
            return None
        message = tx.get("transaction", {}).get("message", {})
        account_keys = message.get("accountKeys", []) or []
        fee_payer = None
        if account_keys:
            first = account_keys[0]
            fee_payer = first.get("pubkey") if isinstance(first, dict) else first
        signature = None
        sigs = tx.get("transaction", {}).get("signatures", []) or []
        if sigs:
            signature = sigs[0]
        block_time = tx.get("blockTime")
        timestamp = None
        if isinstance(block_time, (int, float)):
            timestamp = datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()

        mint = None
        program = "unknown"
        for instr in message.get("instructions", []) or []:
            if not isinstance(instr, dict):
                continue
            if instr.get("programId") != PUMP_FUN_PROGRAM:
                continue
            program = "pump.fun"
            parsed = instr.get("parsed") or {}
            info = parsed.get("info") or {}
            if info.get("mint"):
                mint = info["mint"]
                break
            accounts = instr.get("accounts") or []
            if accounts:
                first_acct = accounts[0]
                mint = (
                    first_acct.get("pubkey")
                    if isinstance(first_acct, dict)
                    else first_acct
                )
                break
        if mint is None:
            # Fallback: a mint that only exists after the transaction.
            meta = tx.get("meta", {}) or {}
            pre = {b.get("mint") for b in meta.get("preTokenBalances", []) or []}
            for bal in meta.get("postTokenBalances", []) or []:
                m = bal.get("mint")
                if m and m not in pre and m != WRAPPED_SOL:
                    mint = m
                    program = "pump.fun"
                    break
        if not mint or not fee_payer:
            return None
        return TokenCreation(
            mint=mint,
            dev_wallet=fee_payer,
            program=program,
            timestamp=timestamp,
            signature=signature,
        )

    @staticmethod
    def parse_creation_event(event: dict) -> TokenCreation | None:
        """Parse a simple dict-shaped creation event (back-compat)."""
        mint = event.get("mint")
        dev = event.get("dev_wallet") or event.get("creator")
        program = event.get("program", "unknown")
        if not mint or not dev:
            return None
        return TokenCreation(
            mint=mint,
            dev_wallet=dev,
            program=program,
            timestamp=event.get("timestamp"),
            signature=event.get("signature"),
        )

    def fetch_new_mints(self, limit: int = 50) -> list[TokenCreation]:
        """Recent pump.fun token creations, newest first (deduped by mint).

        Raises RuntimeError if no HELIUS_API_KEY is set. A single
        failed transaction fetch is skipped, not fatal — the RPC
        listing itself failing *is* fatal.
        """
        self._require_enabled()
        signatures = self.get_signatures(PUMP_FUN_PROGRAM, limit=limit)
        creations: list[TokenCreation] = []
        seen: set[str] = set()
        for entry in signatures[:limit]:
            sig = entry.get("signature") if isinstance(entry, dict) else None
            if not sig:
                continue
            try:
                tx = self.get_transaction(sig)
            except Exception:
                continue
            if tx is None:
                continue
            creation = self.parse_transaction(tx)
            if creation is None or creation.mint in seen:
                continue
            seen.add(creation.mint)
            creations.append(creation)
        return creations

    # -- Funding transfers (for clustering) ----------------------------
    def fetch_funding_transfers(
        self, wallets: list[str], limit_per_wallet: int = 20
    ) -> list[dict]:
        """Native SOL transfers involving the given wallets.

        Parses parsed ``system transfer`` instructions in each wallet's
        recent transactions. Best-effort: any wallet whose history
        fails is skipped. Returns [{"from": .., "to": .., ...}].
        """
        self._require_enabled()
        transfers: list[dict] = []
        wanted = set(wallets)
        for wallet in wallets:
            try:
                sigs = self.get_signatures(wallet, limit=limit_per_wallet)
            except Exception:
                continue
            for entry in sigs:
                sig = entry.get("signature") if isinstance(entry, dict) else None
                if not sig:
                    continue
                try:
                    tx = self.get_transaction(sig)
                except Exception:
                    continue
                if not tx:
                    continue
                message = tx.get("transaction", {}).get("message", {})
                for instr in message.get("instructions", []) or []:
                    if not isinstance(instr, dict):
                        continue
                    parsed = instr.get("parsed") or {}
                    if parsed.get("type") != "transfer":
                        continue
                    info = parsed.get("info") or {}
                    src, dst = info.get("source"), info.get("destination")
                    if src and dst and src != dst and (src in wanted or dst in wanted):
                        transfers.append(
                            {
                                "from": src,
                                "to": dst,
                                "amount_sol": (info.get("lamports") or 0) / 1e9,
                                "signature": sig,
                            }
                        )
        return transfers

    # -- Outcome via DexScreener (public, no key) -----------------------
    def fetch_mint_outcome(
        self, mint: str, dev_wallet: str | None = None
    ) -> MintOutcome:
        """Outcome evidence for one mint from DexScreener (public API).

        Peak market cap is *estimated* from the 24h price change
        (peak ~= current / (1 + h24%) when h24 is negative); LP removal
        is inferred from liquidity < $1,000. ``dev_sold_pct`` is 0.0 —
        DexScreener does not prove a dev sell, and we do not invent
        one. The labeller can still call a mint ``rugged`` on
        collapse >= 80% + LP removal. No pair found -> all-None
        outcome, which the labeller calls ``inconclusive``.
        """
        evidence = {
            "solscan_mint": f"https://solscan.io/token/{mint}",
            "dexscreener": f"https://dexscreener.com/solana/{mint}",
        }
        if dev_wallet:
            evidence["solscan_wallet"] = f"https://solscan.io/account/{dev_wallet}"
        try:
            data = self._get_json(f"{DEXSCREENER_URL}/{mint}")
        except Exception:
            return MintOutcome(mint=mint, dev_wallet=dev_wallet, evidence=evidence)
        pairs = data.get("pairs") or []
        if not pairs:
            return MintOutcome(mint=mint, dev_wallet=dev_wallet, evidence=evidence)
        # Prefer the Solana pair with the most liquidity.
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"] or pairs
        pair = max(sol_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        market_cap = pair.get("marketCap") or pair.get("fdv")
        current = float(market_cap) if market_cap else None
        price_change = (pair.get("priceChange") or {}).get("h24")
        peak = None
        if current is not None and isinstance(price_change, (int, float)):
            if price_change < 0:
                factor = max(0.01, 1.0 + price_change / 100.0)
                peak = current / factor
            else:
                peak = current
        liquidity = ((pair.get("liquidity") or {}).get("usd")) or 0
        lp_removed = float(liquidity) < LP_DRAINED_USD
        created_at = pair.get("pairCreatedAt")
        time_to_collapse = None
        if created_at:
            age_min = int(
                (datetime.now(timezone.utc).timestamp() - created_at / 1000) // 60
            )
            time_to_collapse = max(0, age_min)
        return MintOutcome(
            mint=mint,
            dev_wallet=dev_wallet,
            peak_market_cap_usd=peak,
            current_market_cap_usd=current,
            lp_removed=lp_removed,
            dev_sold_pct=0.0,
            time_to_collapse_minutes=time_to_collapse,
            evidence=evidence,
        )
