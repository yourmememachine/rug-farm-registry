"""Indexer tests — all HTTP is mocked with httpx.MockTransport.
No live call is ever made here."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from src.indexer import HeliusIndexer, PUMP_FUN_PROGRAM
from src.labeller import MintEvent, label_outcome

DEV1 = "RUGDEV111111111111111111111111111111111"
DEV2 = "RUGDEV222222222222222222222222222222222"
MINT1 = "RUGMNT111111111111111111111111111111111"
MINT2 = "RUGMNT222222222222222222222222222222222"


def _tx_create(signature, mint, dev, block_time=1790000000):
    return {
        "blockTime": block_time,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [
                    {"pubkey": dev, "signer": True},
                    {"pubkey": mint, "signer": False},
                ],
                "instructions": [
                    {
                        "programId": PUMP_FUN_PROGRAM,
                        "parsed": {"type": "create", "info": {"mint": mint}},
                        "accounts": [mint, dev],
                    }
                ],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
    }


def _tx_post_balances(signature, mint, dev, block_time=1790000000):
    return {
        "blockTime": block_time,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [{"pubkey": dev, "signer": True}],
                "instructions": [{"programId": "11111111111111111111111111111111"}],
            },
        },
        "meta": {
            "preTokenBalances": [],
            "postTokenBalances": [{"mint": mint, "owner": dev, "uiTokenAmount": {"amount": "100"}}],
        },
    }


def _mock_client():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "helius-rpc.com" in url:
            body = json.loads(request.content)
            method = body.get("method")
            if method == "getSignaturesForAddress":
                return httpx.Response(200, json={"jsonrpc": "2.0", "result": [
                    {"signature": "sig1"}, {"signature": "sig2"},
                ], "id": 1})
            if method == "getTransaction":
                sig = body["params"][0]
                if sig == "sig1":
                    return httpx.Response(200, json={"jsonrpc": "2.0", "result": _tx_create("sig1", MINT1, DEV1), "id": 1})
                return httpx.Response(200, json={"jsonrpc": "2.0", "result": _tx_post_balances("sig2", MINT2, DEV2), "id": 1})
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": [], "id": 1})
        if "dexscreener.com" in url:
            created_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 30 * 60 * 1000
            return httpx.Response(200, json={"pairs": [{
                "chainId": "solana",
                "marketCap": 5000,
                "liquidity": {"usd": 100},
                "priceChange": {"h24": -95},
                "pairCreatedAt": created_ms,
            }]})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    indexer = HeliusIndexer()
    assert indexer.enabled is False
    with pytest.raises(RuntimeError, match="HELIUS_API_KEY"):
        indexer.fetch_new_mints()


def test_fetch_new_mints_parses_create_and_fallback():
    indexer = HeliusIndexer(api_key="TESTKEY", client=_mock_client())
    creations = indexer.fetch_new_mints(limit=10)
    assert len(creations) == 2
    by_mint = {c.mint: c for c in creations}
    assert by_mint[MINT1].dev_wallet == DEV1
    assert by_mint[MINT1].program == "pump.fun"
    assert by_mint[MINT2].dev_wallet == DEV2


def test_parse_transaction_returns_none_without_mint_or_dev():
    assert HeliusIndexer.parse_transaction({}) is None
    assert HeliusIndexer.parse_transaction({"transaction": {"message": {"accountKeys": []}, "signatures": []}, "meta": {}}) is None


def test_fetch_mint_outcome_rugged_via_dexscreener():
    indexer = HeliusIndexer(api_key="TESTKEY", client=_mock_client())
    outcome = indexer.fetch_mint_outcome(MINT1, DEV1)
    assert outcome.current_market_cap_usd == 5000
    # Peak estimated from -95% in 24h: 5000 / 0.05 = 100000.
    assert outcome.peak_market_cap_usd == pytest.approx(100000, rel=0.01)
    assert outcome.lp_removed is True  # liquidity $100 < $1000
    event = MintEvent(
        peak_market_cap_usd=outcome.peak_market_cap_usd,
        current_market_cap_usd=outcome.current_market_cap_usd,
        lp_removed=outcome.lp_removed,
        dev_sold_pct=outcome.dev_sold_pct,
        time_to_collapse_minutes=outcome.time_to_collapse_minutes,
    )
    assert label_outcome(event) == "rugged"
    assert outcome.evidence["dexscreener"].endswith(MINT1)


def test_fetch_mint_outcome_no_pair_is_inconclusive():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": []})

    indexer = HeliusIndexer(api_key="TESTKEY", client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = indexer.fetch_mint_outcome(MINT1, DEV1)
    assert outcome.peak_market_cap_usd is None
    assert label_outcome(MintEvent(outcome.peak_market_cap_usd, outcome.current_market_cap_usd)) == "inconclusive"
