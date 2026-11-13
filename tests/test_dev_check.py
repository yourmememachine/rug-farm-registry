"""Dev-check tests — all data is synthetic and caller-supplied.

No network is ever made here, and no real API key is used. The
POOR fixture below replays the manual browser check of 2026-11-13
(dev 7kGAX... funded by G4JX..., early buyers SkarraOG and
therealhimmy each funded by a different wallet): the module must
reproduce the "no direct bundle found" finding from those four
different funders.
"""

import json

import httpx
import pytest

from src import dev_check
from src.dev_check import (
    BuyerCheck,
    DevCheckReport,
    DevChecker,
    FunderLink,
    main,
    resolve_first_funder,
)
from src.public_rpc import PublicRpcClient, RpcError
from src.indexer import PUMP_FUN_PROGRAM, MintOutcome, TokenCreation

DEV = "7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk"
FUNDER = "G4JXShDM6NqP1XpMo3gpY24wxPj8NVRqqKMTKv5gLNfY"
FUNDER2 = "5gjpbwfqfArJh9cmXPNeFQeaB26vomsvHnHR2mN91Atz"
SKARRA = "3gHrfi6t4xG2KEH4gga9HxLRYqws9iTKdkLG55NwaTZJ"
SKARRA_FUNDER = "EZiMRZsiG2JaJAgYxN7mdBdM8mpFw8GfxELsBhNzk8Fd"
HIMMY = "H6hPTUg73GDscH4Q9NAviJrUtMwUXE6PzZat2xWDszrJ"
HIMMY_FUNDER = "ZstLakrCLjwTZwMwmULfuLH7ThaT8jueuCyWgZBnoFX"
POOR = "9oJk7eMccQchx3evoN5C5pKQD5Xodb4C1qkhemjjpump"
OTHER = "RUGDEV222222222222222222222222222222222"
MINT2 = "RUGMNT222222222222222222222222222222222"


# ------------------------------------------------------------------
# Stub chain source (emulates getSignaturesForAddress paging)
# ------------------------------------------------------------------
class StubSource:
    name = "stub"

    def __init__(self, sigs=None, txs=None, balance=100.0, token_count=3):
        self._sigs = sigs or {}
        self._txs = txs or {}
        self._balance = balance
        self._token_count = token_count

    def get_signatures(self, address, limit=50, before=None):
        entries = list(self._sigs.get(address, []))
        start = 0
        if before:
            for i, e in enumerate(entries):
                if e.get("signature") == before:
                    start = i + 1
                    break
        return entries[start:start + limit]

    def get_transaction(self, signature):
        return self._txs.get(signature)

    def get_balance(self, address):
        return self._balance

    def get_token_account_count(self, address):
        return self._token_count

    def probe(self):
        return True


def _transfer_tx(sig, src, dst, lamports=3_000_000_000, block_time=1747000000):
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": src}],
                "instructions": [
                    {
                        "programId": "11111111111111111111111111111111",
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": src,
                                "destination": dst,
                                "lamports": lamports,
                            },
                        },
                    }
                ],
            },
        },
        "blockTime": block_time,
    }


def _create_account_tx(sig, src, account, lamports=2_000_000, block_time=1747000000):
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": src}],
                "instructions": [
                    {
                        "programId": "11111111111111111111111111111111",
                        "parsed": {
                            "type": "createAccount",
                            "info": {
                                "source": src,
                                "account": account,
                                "lamports": lamports,
                            },
                        },
                    }
                ],
            },
        },
        "blockTime": block_time,
    }


def _creation_tx(sig, dev, mint, buyers, block_time=1763000000):
    post = [
        {"mint": mint, "owner": dev, "uiTokenAmount": {"uiAmount": 1000.0}}
    ]
    for wallet, amount in buyers:
        post.append(
            {"mint": mint, "owner": wallet, "uiTokenAmount": {"uiAmount": float(amount)}}
        )
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": dev}],
                "instructions": [
                    {
                        "programId": PUMP_FUN_PROGRAM,
                        "parsed": {"type": "create", "info": {"mint": mint}},
                    }
                ],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": post},
        "blockTime": block_time,
    }


def _sig(sig):
    return {"signature": sig}


def _honest_outcome(mint, dev=DEV):
    return MintOutcome(
        mint=mint, dev_wallet=dev,
        peak_market_cap_usd=1_000_000.0, current_market_cap_usd=250_000.0,
        lp_removed=False, dev_sold_pct=0.0, time_to_collapse_minutes=60,
        evidence={"dexscreener": f"https://dexscreener.com/solana/{mint}"},
    )


def _rugged_outcome(mint, dev=DEV):
    return MintOutcome(
        mint=mint, dev_wallet=dev,
        peak_market_cap_usd=1_000_000.0, current_market_cap_usd=50_000.0,
        lp_removed=True, dev_sold_pct=0.0, time_to_collapse_minutes=60,
        evidence={"dexscreener": f"https://dexscreener.com/solana/{mint}"},
    )


def _poor_source():
    """Stub replaying the manual POOR check: four different funders."""
    sigs = {
        DEV: [_sig("sig-create-poor"), _sig("sig-fund-dev")],
        FUNDER: [_sig("sig-fund-funder")],
        SKARRA: [_sig("sig-fund-skarra")],
        HIMMY: [_sig("sig-fund-himmy")],
        POOR: [_sig("sig-create-poor")],
    }
    txs = {
        "sig-create-poor": _creation_tx(
            "sig-create-poor", DEV, POOR,
            [(SKARRA, 29_000_000), (HIMMY, 18_000_000)],
        ),
        "sig-fund-dev": _transfer_tx("sig-fund-dev", FUNDER, DEV),
        "sig-fund-funder": _transfer_tx("sig-fund-funder", FUNDER2, FUNDER),
        "sig-fund-skarra": _transfer_tx("sig-fund-skarra", SKARRA_FUNDER, SKARRA),
        "sig-fund-himmy": _transfer_tx("sig-fund-himmy", HIMMY_FUNDER, HIMMY),
    }
    return StubSource(sigs=sigs, txs=txs, balance=186.3, token_count=221)


def _poor_checker():
    return DevChecker(
        _poor_source(),
        outcome_fetcher=lambda mint, dev_wallet=None: _honest_outcome(mint),
    )


# ------------------------------------------------------------------
# Funder resolution
# ------------------------------------------------------------------
def test_resolve_first_funder_found():
    link = resolve_first_funder(_poor_source(), DEV)
    assert link.status == "found"
    assert link.funder == FUNDER
    assert link.amount_sol == 3.0
    assert link.reached_oldest is True


def test_funder_chain_two_hops():
    report = _poor_checker().check(DEV)
    assert len(report.funder_chain) == 2
    assert report.funder_chain[0].funder == FUNDER
    assert report.funder_chain[1].funder == FUNDER2
    assert report.funder_chain[1].wallet == FUNDER


def test_create_account_funding_detected():
    source = StubSource(
        sigs={"WALLETAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-ca")]},
        txs={"sig-ca": _create_account_tx("sig-ca", FUNDER, "WALLETAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")},
    )
    link = resolve_first_funder(source, "WALLETAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert link.status == "found"
    assert link.funder == FUNDER


def test_funder_unresolved_when_truncated():
    # A full 1000-entry page with no next page fetched (budget 1):
    # the walk cannot reach the oldest signature.
    sigs = {"BIGWALLETAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig(f"sig{i:04d}") for i in range(1000)]}
    source = StubSource(sigs=sigs, txs={})
    link = resolve_first_funder(source, "BIGWALLETAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", max_pages=1)
    assert link.status == "unresolved"
    assert link.funder is None
    assert link.reached_oldest is False
    assert "outside the scanned window" in (link.note or "")


def test_funder_unresolved_when_no_transactions():
    source = StubSource(sigs={}, txs={})
    link = resolve_first_funder(source, DEV)
    assert link.status == "unresolved"
    assert "no transactions" in (link.note or "").lower()


def test_rate_limit_degradation_still_reports():
    class FailingSource(StubSource):
        def get_signatures(self, address, limit=50, before=None):
            raise RuntimeError("429 Too Many Requests")

    checker = DevChecker(FailingSource(), outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m))
    report = checker.check(DEV)
    assert report.history_verdict == "insufficient_data"
    assert report.bundle_verdict == "inconclusive"
    assert report.funder_chain[0].status == "unresolved"


# ------------------------------------------------------------------
# Bundle test
# ------------------------------------------------------------------
def test_poor_fixture_no_direct_bundle():
    report = _poor_checker().check(DEV)
    assert report.bundle_verdict == "no direct bundle found"
    assert report.bundled_wallets == []
    assert len(report.buyers) == 2
    assert all(b.bundled is False for b in report.buyers)
    funders = {b.wallet: b.funder.funder for b in report.buyers}
    assert funders[SKARRA] == SKARRA_FUNDER
    assert funders[HIMMY] == HIMMY_FUNDER


def test_text_says_no_direct_bundle_found():
    report = _poor_checker().check(DEV)
    assert "NO DIRECT BUNDLE FOUND" in report.to_text()


def test_shared_funder_bundle_detected():
    sigs = {
        DEV: [_sig("sig-create"), _sig("sig-fund-dev")],
        "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-fund-buyer")],
        "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-create")],
    }
    txs = {
        "sig-create": _creation_tx(
            "sig-create", DEV, "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            [("BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 100)],
        ),
        "sig-fund-dev": _transfer_tx("sig-fund-dev", FUNDER, DEV),
        "sig-fund-buyer": _transfer_tx("sig-fund-buyer", FUNDER, "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    }
    checker = DevChecker(
        StubSource(sigs=sigs, txs=txs),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert report.bundle_verdict == "bundle found"
    assert report.bundled_wallets == ["BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]
    assert "Shares the dev's first funder" in report.buyers[0].bundle_reason


def test_buyer_funded_by_dev_is_bundled():
    sigs = {
        DEV: [_sig("sig-create"), _sig("sig-fund-dev")],
        "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-fund-buyer")],
        "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-create")],
    }
    txs = {
        "sig-create": _creation_tx(
            "sig-create", DEV, "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            [("BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 100)],
        ),
        "sig-fund-dev": _transfer_tx("sig-fund-dev", FUNDER, DEV),
        "sig-fund-buyer": _transfer_tx("sig-fund-buyer", DEV, "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    }
    checker = DevChecker(
        StubSource(sigs=sigs, txs=txs),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert report.buyers[0].bundled is True
    assert "Funded by the dev" in report.buyers[0].bundle_reason


def test_buyer_who_funded_dev_is_bundled():
    sigs = {
        DEV: [_sig("sig-create"), _sig("sig-fund-dev")],
        "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-fund-buyer")],
        "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-create")],
    }
    txs = {
        "sig-create": _creation_tx(
            "sig-create", DEV, "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            [("BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 100)],
        ),
        # The buyer funded the dev.
        "sig-fund-dev": _transfer_tx(
            "sig-fund-dev", "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", DEV
        ),
        "sig-fund-buyer": _transfer_tx("sig-fund-buyer", FUNDER2, "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    }
    checker = DevChecker(
        StubSource(sigs=sigs, txs=txs),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert report.buyers[0].bundled is True
    assert "Funded the dev" in report.buyers[0].bundle_reason


def test_bundle_inconclusive_when_funder_unresolved():
    sigs = {
        DEV: [_sig("sig-create"), _sig("sig-fund-dev")],
        "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [],
        "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-create")],
    }
    txs = {
        "sig-create": _creation_tx(
            "sig-create", DEV, "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            [("BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 100)],
        ),
        "sig-fund-dev": _transfer_tx("sig-fund-dev", FUNDER, DEV),
    }
    checker = DevChecker(
        StubSource(sigs=sigs, txs=txs),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert report.bundle_verdict == "inconclusive"
    assert report.buyers[0].bundled is None
    assert "NOT a 'no bundle' finding" in report.bundle_note


# ------------------------------------------------------------------
# Profile, history, clusters
# ------------------------------------------------------------------
def test_dev_profile_balance_and_token_count():
    report = _poor_checker().check(DEV)
    assert report.sol_balance == 186.3
    assert report.token_account_count == 221
    assert report.pumpfun_profile == f"https://pump.fun/profile/{DEV}"
    assert report.pumpfun_name is None


def test_balance_failure_still_runs():
    class NoBalanceSource(StubSource):
        def get_balance(self, address):
            raise RuntimeError("getBalance failed")

        def get_token_account_count(self, address):
            raise RuntimeError("getTokenAccountsByOwner failed")

    checker = DevChecker(
        NoBalanceSource(sigs=_poor_source()._sigs, txs=_poor_source()._txs),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert report.sol_balance is None
    assert report.token_account_count is None
    assert report.bundle_verdict == "no direct bundle found"


def test_insufficient_data_when_no_creations():
    checker = DevChecker(
        StubSource(),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert report.history_verdict == "insufficient_data"
    assert report.history_checked == 0
    assert report.bundle_verdict == "inconclusive"
    assert report.cluster_note.startswith("Too thin")


def test_flagged_verdict_two_rugged_mints():
    mint1 = "MINT111111111111111111111111111111111"
    sigs = {
        DEV: [_sig("sig-c1"), _sig("sig-c2"), _sig("sig-fund-dev")],
        mint1: [_sig("sig-c1")],
        MINT2: [_sig("sig-c2")],
    }
    txs = {
        "sig-c1": _creation_tx("sig-c1", DEV, mint1, []),
        "sig-c2": _creation_tx("sig-c2", DEV, MINT2, []),
        "sig-fund-dev": _transfer_tx("sig-fund-dev", FUNDER, DEV),
    }
    checker = DevChecker(
        StubSource(sigs=sigs, txs=txs),
        outcome_fetcher=lambda m, dev_wallet=None: _rugged_outcome(m),
    )
    report = checker.check(DEV)
    assert report.history_verdict == "flagged"
    assert report.history_rugged == 2


def test_cluster_shared_funder_reported():
    report = _poor_checker().check(DEV)
    # POOR fixture: four different funders, no shared funder.
    assert report.clusters == []
    assert "no shared funder" in report.cluster_note


# ------------------------------------------------------------------
# Raw pump.fun create parsing (public RPC does not parse it)
# ------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + (out or "")


def _raw_creation_tx(sig, dev, mint, buyers=()):
    from src.dev_check import CREATE_DISCRIMINATOR

    data = _b58encode(CREATE_DISCRIMINATOR + b"\x00" * 32)
    post = [{"mint": mint, "owner": dev, "uiTokenAmount": {"uiAmount": 1000.0}}]
    for wallet, amount in buyers:
        post.append(
            {"mint": mint, "owner": wallet, "uiTokenAmount": {"uiAmount": float(amount)}}
        )
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": dev}],
                "instructions": [
                    {
                        "programId": PUMP_FUN_PROGRAM,
                        "data": data,
                        "accounts": [mint, dev],
                    }
                ],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": post},
        "blockTime": 1763000000,
    }


def test_raw_create_instruction_detected():
    from src.dev_check import parse_creation_any

    tx = _raw_creation_tx("sig-raw", DEV, POOR, [(SKARRA, 50)])
    creation = parse_creation_any(tx)
    assert creation is not None
    assert creation.mint == POOR
    assert creation.dev_wallet == DEV
    assert creation.program == "pump.fun"


def test_raw_non_create_instruction_ignored():
    from src.dev_check import parse_creation_any

    # Same shape, wrong discriminator (a buy, not a create).
    data = _b58encode(b"\x00" * 8 + b"\x00" * 32)
    tx = {
        "transaction": {
            "signatures": ["sig-buy"],
            "message": {
                "accountKeys": [{"pubkey": DEV}],
                "instructions": [
                    {
                        "programId": PUMP_FUN_PROGRAM,
                        "data": data,
                        "accounts": [POOR, DEV],
                    }
                ],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
        "blockTime": 1763000000,
    }
    assert parse_creation_any(tx) is None


def test_raw_create_found_in_recent_creations():
    sigs = {DEV: [_sig("sig-raw")]}
    txs = {"sig-raw": _raw_creation_tx("sig-raw", DEV, POOR)}
    checker = DevChecker(
        StubSource(sigs=sigs, txs=txs),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert [c.mint for c in report.recent_coins] == [POOR]
    assert report.history_verdict == "clean"


def test_cluster_detected_when_shared_funder():
    # Dev + buyer share a funder: the detector must surface it.
    sigs = {
        DEV: [_sig("sig-create"), _sig("sig-fund-dev")],
        "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-fund-buyer")],
        "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": [_sig("sig-create")],
    }
    txs = {
        "sig-create": _creation_tx(
            "sig-create", DEV, "MINTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            [("BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 100)],
        ),
        "sig-fund-dev": _transfer_tx("sig-fund-dev", FUNDER, DEV),
        "sig-fund-buyer": _transfer_tx("sig-fund-buyer", FUNDER, "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    }
    checker = DevChecker(
        StubSource(sigs=sigs, txs=txs),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    report = checker.check(DEV)
    assert len(report.clusters) == 1
    assert set(report.clusters[0].members) == {
        DEV, "BUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    }
    assert "shared_funder" in report.clusters[0].signals


def test_json_output_shape():
    report = _poor_checker().check(DEV)
    data = json.loads(json.dumps(report.to_dict()))
    assert data["dev_wallet"] == DEV
    assert data["bundle"]["verdict"] == "no direct bundle found"
    assert data["history"]["verdict"] == "clean"
    assert data["profile"]["sol_balance"] == 186.3
    assert len(data["funder_chain"]) == 2
    assert len(data["early_buyers"]) == 2


def test_helius_source_name_reported():
    class HeliusNamedSource(StubSource):
        name = "helius"

    checker = DevChecker(
        HeliusNamedSource(),
        outcome_fetcher=lambda m, dev_wallet=None: _honest_outcome(m),
    )
    assert checker.check(DEV).source == "helius"


# ------------------------------------------------------------------
# Public RPC client (MockTransport — no network)
# ------------------------------------------------------------------
def _rpc_response(result=None, error=None, status=200):
    body = {"jsonrpc": "2.0", "id": 1}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return httpx.Response(status, json=body)


def test_public_rpc_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return _rpc_response(result=42)

    client = PublicRpcClient(
        endpoints=("https://a.example",),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.rpc("getSlot", []) == 42
    assert calls["n"] == 2


def test_public_rpc_429_everywhere_marks_method_dead():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})

    client = PublicRpcClient(
        endpoints=("https://a.example",),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RpcError):
        client.rpc("getSlot", [])
    assert "getSlot" in client.dead_methods
    with pytest.raises(RpcError, match="marked dead"):
        client.rpc("getSlot", [])


def test_public_rpc_error_is_immediate():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _rpc_response(error={"code": -32602, "message": "bad params"})

    client = PublicRpcClient(
        endpoints=("https://a.example",),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RpcError, match="RPC error"):
        client.rpc("getBalance", ["x"])
    assert calls["n"] == 1


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def test_main_invalid_address_exit_2(capsys):
    assert main(["--wallet", "not-an-address"]) == 2
    assert "not a valid Solana address" in capsys.readouterr().err


def test_main_bad_limit_exit_2(capsys):
    assert main(["--wallet", DEV, "--limit", "0"]) == 2


def test_main_json_no_network(monkeypatch, capsys):
    """main() with a stubbed source prints valid JSON, exit 0."""
    monkeypatch.setattr(dev_check, "HeliusClient", lambda: _DisabledHelius())
    monkeypatch.setattr(dev_check, "HeliusSource", lambda client=None: StubSource())
    monkeypatch.setattr(dev_check, "PublicRpcSource", lambda: StubSource())
    assert main(["--wallet", DEV, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dev_wallet"] == DEV
    assert data["history"]["verdict"] == "insufficient_data"


def test_main_flagged_exit_1(monkeypatch, capsys):
    """main() returns 1 when the dev history verdict is flagged."""
    monkeypatch.setattr(dev_check, "HeliusClient", lambda: _DisabledHelius())
    monkeypatch.setattr(dev_check, "HeliusSource", lambda client=None: StubSource())

    class FakeChecker:
        def __init__(self, source, registry_data):
            pass

        def check(self, *args, **kwargs):
            return DevCheckReport(
                dev_wallet=DEV, source="stub", history_verdict="flagged",
            )

    monkeypatch.setattr(dev_check, "DevChecker", FakeChecker)
    assert main(["--wallet", DEV]) == 1
    assert "FLAGGED" in capsys.readouterr().out


class _DisabledHelius:
    enabled = False
    api_key = None
