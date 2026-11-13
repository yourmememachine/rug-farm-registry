"""Live-scan tests — all HTTP is mocked with httpx.MockTransport.
No live call is ever made here, and no real API key is used."""

import httpx
import pytest

from src.indexer import PUMP_FUN_PROGRAM, MintOutcome, TokenCreation
from src.live_scan import (
    MISSING_KEY_MESSAGE,
    EvidenceGatherer,
    HeliusClient,
    LiveScanner,
    main,
)

DEV1 = "RUGDEV111111111111111111111111111111111"
DEV2 = "RUGDEV222222222222222222222222222222222"
MINT1 = "RUGMNT111111111111111111111111111111111"
MINT2 = "RUGMNT222222222222222222222222222222222"
MINT3 = "RUGMNT333333333333333333333333333333333"
MINT4 = "RUGMNT444444444444444444444444444444444"


def _rugged_outcome(mint, dev, peak=1_000_000.0, current=50_000.0):
    """95% collapse + LP removed, fast -> labeller says 'rugged'."""
    return MintOutcome(
        mint=mint,
        dev_wallet=dev,
        peak_market_cap_usd=peak,
        current_market_cap_usd=current,
        lp_removed=True,
        dev_sold_pct=0.0,
        time_to_collapse_minutes=60,
        evidence={"dexscreener": f"https://dexscreener.com/solana/{mint}"},
    )


def _honest_fail_outcome(mint, dev):
    """75% collapse, no LP removal -> 'failed_but_honest', never a rug."""
    return MintOutcome(
        mint=mint,
        dev_wallet=dev,
        peak_market_cap_usd=1_000_000.0,
        current_market_cap_usd=250_000.0,
        lp_removed=False,
        dev_sold_pct=0.0,
        time_to_collapse_minutes=60,
    )


class FakeClient:
    """Stub HeliusClient: enabled, returns canned creations/outcomes."""

    def __init__(self, creations, outcomes, enabled=True):
        self._creations = creations
        self._outcomes = outcomes
        self.enabled = enabled
        self.calls = []

    def fetch_new_mints(self, limit=10):
        self.calls.append(("fetch_new_mints", limit))
        return self._creations[:limit]

    def fetch_mint_outcome(self, mint, dev_wallet=None):
        self.calls.append(("fetch_mint_outcome", mint))
        return self._outcomes[mint]


def _creation(mint, dev, sig=None):
    return TokenCreation(mint=mint, dev_wallet=dev, program="pump.fun", signature=sig)


# --- HeliusClient: key handling -----------------------------------------

def test_missing_key_is_disabled_and_message_is_clear():
    client = HeliusClient(api_key=None, env={})
    assert client.enabled is False
    with pytest.raises(RuntimeError, match="HELIUS_API_KEY is not set"):
        client.require_enabled()
    with pytest.raises(RuntimeError, match="HELIUS_API_KEY is not set"):
        client.fetch_new_mints()
    assert "scan blocked" in MISSING_KEY_MESSAGE


def test_key_read_from_env_and_never_in_message():
    client = HeliusClient(env={"HELIUS_API_KEY": "TESTKEY123"})
    assert client.enabled is True
    assert "TESTKEY123" not in MISSING_KEY_MESSAGE


def test_blank_key_counts_as_missing():
    assert HeliusClient(env={"HELIUS_API_KEY": "   "}).enabled is False


# --- HeliusClient: RPC over mocked HTTP ---------------------------------

def _mock_rpc(request):
    import json as _json

    payload = _json.loads(request.content.decode("utf-8"))
    method = payload.get("method")
    if method == "getSignaturesForAddress":
        return httpx.Response(200, json={"result": [{"signature": "sig1"}]})
    if method == "getTransaction":
        return httpx.Response(
            200,
            json={
                "result": {
                    "blockTime": 1790000000,
                    "transaction": {
                        "signatures": ["sig1"],
                        "message": {
                            "accountKeys": [
                                {"pubkey": DEV1, "signer": True},
                                {"pubkey": MINT1, "signer": False},
                            ],
                            "instructions": [
                                {
                                    "programId": PUMP_FUN_PROGRAM,
                                    "parsed": {"type": "create", "info": {"mint": MINT1}},
                                    "accounts": [MINT1, DEV1],
                                }
                            ],
                        },
                    },
                    "meta": {"preTokenBalances": [], "postTokenBalances": []},
                }
            },
        )
    return httpx.Response(200, json={"result": None})


def test_client_sends_key_in_rpc_url_and_parses_signatures():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return _mock_rpc(request)

    transport = httpx.MockTransport(handler)
    client = HeliusClient(
        api_key="TESTKEY123", client=httpx.Client(transport=transport)
    )
    sigs = client.get_signatures(PUMP_FUN_PROGRAM, limit=5)
    assert sigs == [{"signature": "sig1"}]
    assert "api-key=TESTKEY123" in seen[0]


def test_client_get_transaction_and_parse_mint_plus_dev():
    transport = httpx.MockTransport(_mock_rpc)
    client = HeliusClient(
        api_key="TESTKEY123", client=httpx.Client(transport=transport)
    )
    tx = client.get_transaction("sig1")
    creation = HeliusClient.parse_transaction(tx)
    assert creation is not None
    assert creation.mint == MINT1
    assert creation.dev_wallet == DEV1


def test_client_rpc_error_raises():
    def handler(request):
        return httpx.Response(200, json={"error": {"message": "bad key"}})

    client = HeliusClient(
        api_key="BADKEY", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(RuntimeError, match="Helius RPC error"):
        client.get_signatures(PUMP_FUN_PROGRAM)


# --- LiveScanner: verdicts ----------------------------------------------

def test_scan_blocked_without_key_returns_report_not_exception():
    client = FakeClient([], {}, enabled=False)
    report = LiveScanner(client).scan(limit=5)
    assert report.blocked is True
    assert report.checked == 0
    assert report.flagged == [] and report.under_review == []
    assert "HELIUS_API_KEY" in (report.blocked_reason or "")
    assert client.calls == []  # no calls were attempted


def test_scan_two_rugged_mints_same_dev_is_flagged():
    creations = [_creation(MINT1, DEV1), _creation(MINT2, DEV1)]
    outcomes = {MINT1: _rugged_outcome(MINT1, DEV1), MINT2: _rugged_outcome(MINT2, DEV1)}
    report = LiveScanner(FakeClient(creations, outcomes)).scan(limit=10)
    assert report.blocked is False
    assert report.checked == 2
    assert len(report.flagged) == 1
    assert report.flagged[0]["dev_wallet"] == DEV1
    assert report.flagged[0]["rugged_mint_count"] == 2
    assert report.under_review == []


def test_scan_one_rugged_mint_is_under_review_not_flagged():
    creations = [_creation(MINT1, DEV1)]
    outcomes = {MINT1: _rugged_outcome(MINT1, DEV1)}
    report = LiveScanner(FakeClient(creations, outcomes)).scan(limit=10)
    assert report.flagged == []
    assert len(report.under_review) == 1
    assert report.under_review[0]["rugged_mint_count"] == 1


def test_scan_merges_registry_rug_with_scan_rug_to_flag():
    registry = {
        "entries": [
            {
                "dev_wallet": DEV1,
                "rugged_mints": [
                    {"mint": MINT3, "evidence": {"dexscreener": "https://dexscreener.com/solana/" + MINT3}},
                ],
            }
        ]
    }
    creations = [_creation(MINT1, DEV1)]
    outcomes = {MINT1: _rugged_outcome(MINT1, DEV1)}
    report = LiveScanner(FakeClient(creations, outcomes), registry_data=registry).scan(limit=10)
    assert len(report.flagged) == 1
    assert report.flagged[0]["rugged_mint_count"] == 2
    assert {m["mint"] for m in report.flagged[0]["rugged_mints"]} == {MINT1, MINT3}


def test_scan_dedupes_registry_mint_already_found_in_scan():
    registry = {
        "entries": [
            {
                "dev_wallet": DEV1,
                "rugged_mints": [
                    {"mint": MINT1, "evidence": {"dexscreener": "https://dexscreener.com/solana/" + MINT1}},
                    {"mint": MINT2, "evidence": {"dexscreener": "https://dexscreener.com/solana/" + MINT2}},
                ],
            }
        ]
    }
    creations = [_creation(MINT1, DEV1), _creation(MINT2, DEV1)]
    outcomes = {MINT1: _rugged_outcome(MINT1, DEV1), MINT2: _rugged_outcome(MINT2, DEV1)}
    report = LiveScanner(FakeClient(creations, outcomes), registry_data=registry).scan(limit=10)
    assert report.flagged[0]["rugged_mint_count"] == 2  # not 4


def test_price_drop_alone_never_flags():
    creations = [_creation(MINT1, DEV1), _creation(MINT2, DEV1), _creation(MINT3, DEV2)]
    outcomes = {
        MINT1: _honest_fail_outcome(MINT1, DEV1),
        MINT2: _honest_fail_outcome(MINT2, DEV1),
        MINT3: MintOutcome(mint=MINT3, dev_wallet=DEV2),  # no pair -> inconclusive
    }
    report = LiveScanner(FakeClient(creations, outcomes)).scan(limit=10)
    assert report.flagged == []
    assert report.under_review == []
    assert len(report.not_flagged) == 3
    assert {e["outcome"] for e in report.not_flagged} == {"failed_but_honest", "inconclusive"}


def test_scan_limit_is_respected():
    creations = [_creation(MINT1, DEV1), _creation(MINT2, DEV1)]
    outcomes = {MINT1: _rugged_outcome(MINT1, DEV1), MINT2: _rugged_outcome(MINT2, DEV1)}
    fake = FakeClient(creations, outcomes)
    report = LiveScanner(fake).scan(limit=1)
    assert report.checked == 1
    assert fake.calls[0] == ("fetch_new_mints", 1)


def test_scan_invalid_limit_raises():
    fake = FakeClient([], {})
    with pytest.raises(ValueError):
        LiveScanner(fake).scan(limit=0)


# --- EvidenceGatherer ----------------------------------------------------

def _candidate(dev, mints):
    return {
        "dev_wallet": dev,
        "rugged_mint_count": len(mints),
        "rugged_mints": [
            {
                "mint": m,
                "evidence": {
                    "dexscreener": f"https://dexscreener.com/solana/{m}",
                    "solscan_mint": f"https://solscan.io/token/{m}",
                    "pumpfun": f"https://pump.fun/coin/{m}",
                    "solscan_wallet": f"https://solscan.io/account/{dev}",
                },
            }
            for m in mints
        ],
    }


def test_evidence_gatherer_sufficient_for_flagged_candidate():
    result = EvidenceGatherer.gather(_candidate(DEV1, [MINT1, MINT2]))
    assert result["sufficient"] is True
    assert result["rugged_mint_count"] == 2
    assert any("dexscreener.com" in link for link in result["links"])
    assert any("solscan.io/token" in link for link in result["links"])
    assert any("pump.fun/coin" in link for link in result["links"])
    assert "flagged" in result["summary"]


def test_evidence_gatherer_insufficient_with_one_mint():
    result = EvidenceGatherer.gather(_candidate(DEV1, [MINT1]))
    assert result["sufficient"] is False
    assert "insufficient evidence" in result["summary"]


def test_evidence_gatherer_insufficient_with_no_evidence_links():
    candidate = {"dev_wallet": DEV1, "rugged_mints": [
        {"mint": MINT1, "evidence": {}},
        {"mint": MINT2, "evidence": {}},
    ]}
    result = EvidenceGatherer.gather(candidate)
    assert result["sufficient"] is False
    assert result["links"] == []


def test_evidence_gatherer_includes_helius_tx_link_when_present():
    candidate = _candidate(DEV1, [MINT1, MINT2])
    candidate["rugged_mints"][0]["evidence"]["helius_tx"] = "https://explorer.helius.xyz/tx/sig1"
    result = EvidenceGatherer.gather(candidate)
    assert any("explorer.helius.xyz/tx/sig1" in link for link in result["links"])


# --- CLI ------------------------------------------------------------------

def test_cli_dry_run_makes_no_helius_call(monkeypatch, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert main(["--limit", "5", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "No Helius call was made" in out


def test_cli_missing_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert main(["--limit", "5"]) == 2
    err = capsys.readouterr().err
    assert "HELIUS_API_KEY is not set" in err


def test_cli_invalid_limit_exits_2():
    assert main(["--limit", "0"]) == 2
    assert main(["--limit", "-3"]) == 2


def test_cli_dry_run_json_flag_accepted(monkeypatch, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert main(["--dry-run", "--json"]) == 0
