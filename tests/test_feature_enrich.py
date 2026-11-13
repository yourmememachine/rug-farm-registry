"""Tests for src/feature_enrich.py (v2.15).

All RPC/HTTP is mocked (httpx.MockTransport) — no live call is
made in tests. The mock world is one pump.fun mint:
- creation tx (strict pump.fun create instruction, fee payer
  DEV) with DEV holding 1,000 tokens afterwards and the
  bonding-curve vault holding 800,000 of 1,000,000 supply;
- DEV and BUYER1 both funded by FUNDER 48h before launch
  (shared funder -> stealth cluster of 2);
- top holders from getTokenLargestAccounts (or, when that is
  throttled, from getProgramAccounts slices).
"""
from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone

import httpx
import pytest

from src.feature_enrich import (
    DEFAULT_MAX_CALLS_PER_RECORD,
    PUMP_FUN_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    EnrichResult,
    apply_enrichment,
    coverage_report,
    enrich_dataset,
    enrich_record,
    get_token_accounts_for_mint,
    load_checkpoint,
    main,
)
from src.public_rpc import PublicRpcClient, RpcError
from src.real_data import summarize

MINT = "DmVgtmA4qj5qNog5gx2vSLrn3VWi9vywYhHYbQ3ZzCRd"
DEV = "7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk"
BUYER1 = "3gHrfi6t4xG2KEH4gga9HxLRYqws9iTKdkLG55NwaTZJ"
CURVE = "Dn9s38KhZt2SpcGVWQJczkqfqJwX4x8MzrxJMqxPbhcr"
FUNDER = "G4JXShDM6NqP1XpMo3gpY24wxPj8NVRqqKMTKv5gLNfY"
BUYER2 = "H6hPTUg73GDscH4Q9NAviJrUtMwUXE6PzZat2xWDszrJ"
FUNDER2 = "ApijrsL2NWdsK1EHyNex3X2uE9HGQdDXmfyXG6aLAo9L"

CREATE_SIG = "createSig11111111111111111111111111111111111111111111111111111111"
NEWEST_SIG = "newestSig1111111111111111111111111111111111111111111111111111111"
MID_SIG = "midSig11111111111111111111111111111111111111111111111111111111111"
FUND_SIG_DEV = "fundSigDev11111111111111111111111111111111111111111111111111111"
FUND_SIG_BUYER1 = "fundSigBuyer11111111111111111111111111111111111111111111111111"

# Launch Nov 3 2026 00:00 UTC; funding Nov 1 (48h before launch);
# last activity Nov 5 (2 days after launch).
LAUNCH_TS = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp())
FUND_TS = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
LAST_TS = int(datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp())
FETCHED_AT = "2026-11-13T00:00:00+00:00"  # pair_age_days 10 -> launch Nov 3

SUPPLY = 1_000_000.0
TOP_AMOUNTS = [300_000.0, 150_000.0, 100_000.0, 50_000.0, 40_000.0,
               30_000.0, 20_000.0, 10_000.0, 5_000.0, 5_000.0]


def _rugged_record(**overrides) -> dict:
    record = {
        "mint": MINT,
        "symbol": "WIF",
        "outcome": "rugged",
        "market_cap_usd": 1899.0,
        "volume_24h_usd": 2.47,
        "change_24h_pct": None,
        "liquidity_usd": 2155.73,
        "pair_age_days": 10.0,
        "fetched_at": FETCHED_AT,
        "labelling_rule_version": "real-v2",
        "synthetic": False,
    }
    record.update(overrides)
    return record


def _survived_record(**overrides) -> dict:
    record = _rugged_record(
        outcome="survived",
        market_cap_usd=488711.0,
        volume_24h_usd=59861.53,
        change_24h_pct=12.62,
        liquidity_usd=100821.37,
        pair_age_days=331.65,
    )
    record.update(overrides)
    return record


def _creation_tx() -> dict:
    """Strict pump.fun create tx: DEV pays, holds 1,000 after."""
    return {
        "slot": 100,
        "blockTime": LAUNCH_TS,
        "meta": {
            "preTokenBalances": [],
            "postTokenBalances": [
                {"mint": MINT, "owner": DEV,
                 "uiTokenAmount": {"uiAmount": 1000.0, "amount": "1000000000", "decimals": 6}},
                {"mint": MINT, "owner": BUYER1,
                 "uiTokenAmount": {"uiAmount": 500.0, "amount": "500000000", "decimals": 6}},
                {"mint": MINT, "owner": CURVE,
                 "uiTokenAmount": {"uiAmount": 800000.0, "amount": "800000000000", "decimals": 6}},
            ],
        },
        "transaction": {
            "signatures": [CREATE_SIG],
            "message": {
                "accountKeys": [{"pubkey": DEV}, {"pubkey": MINT}],
                "instructions": [
                    {"programId": PUMP_FUN_PROGRAM,
                     "parsed": {"type": "create", "info": {"mint": MINT}},
                     "accounts": [{"pubkey": MINT}]},
                ],
            },
        },
    }


def _funding_tx(destination: str, funder: str, block_time: int) -> dict:
    return {
        "slot": 90,
        "blockTime": block_time,
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
        "transaction": {
            "signatures": ["fundsig"],
            "message": {
                "accountKeys": [{"pubkey": funder}, {"pubkey": destination}],
                "instructions": [
                    {"parsed": {"type": "transfer",
                                "info": {"source": funder, "destination": destination,
                                         "lamports": 3_000_000_000}}},
                ],
            },
        },
    }


def _minimal_tx() -> dict:
    return {
        "slot": 101,
        "blockTime": LAST_TS,
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
        "transaction": {
            "signatures": [MID_SIG],
            "message": {"accountKeys": [{"pubkey": DEV}], "instructions": []},
        },
    }


def _slice_b64(owner_bytes: bytes, amount_raw: int) -> str:
    return base64.b64encode(owner_bytes + amount_raw.to_bytes(8, "little")).decode()


def _mock_handler(state: dict):
    """A mock public-RPC world. ``state`` toggles failure modes."""
    calls: list[str] = state.setdefault("calls", [])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        params = body.get("params") or []
        calls.append(method)

        # Failure modes (per-test).
        mode = state.get("mode")
        if mode == "always_429":
            return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "0"})
        if mode in ("largest_429", "no_holders") and method == "getTokenLargestAccounts":
            return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "0"})
        if mode == "first_429" and method == "getSlot" and calls.count("getSlot") <= 2:
            return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "0"})
        if mode in ("rpc_error", "no_holders") and method == "getProgramAccounts":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "error": {"code": -32602, "message": "Invalid param: WrongSize"}})
        if mode == "no_signatures" and method == "getSignaturesForAddress":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

        if method == "getSlot":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 12345})
        if method == "getAccountInfo":
            owner = state.get("mint_owner", TOKEN_PROGRAM)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "context": {"slot": 1},
                "value": {"owner": owner,
                          "data": {"parsed": {"info": {"decimals": 6}}, "program": "spl-token", "space": 82}}}})
        if method == "getTokenSupply":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "context": {"slot": 1},
                "value": {"amount": "1000000000000", "decimals": 6, "uiAmount": SUPPLY,
                          "uiAmountString": "1000000"}}})
        if method == "getTokenLargestAccounts":
            value = [{"address": f"holder{i}", "amount": str(int(a * 1e6)),
                      "decimals": 6, "uiAmount": a} for i, a in enumerate(TOP_AMOUNTS)]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "context": {"slot": 1}, "value": value}})
        if method == "getProgramAccounts":
            # All token accounts: the top 10 plus 290 small accounts
            # of 1,000 each, so the accounts sum to the full
            # 1,000,000 supply and the top 10 are the TOP_AMOUNTS.
            all_amounts = list(TOP_AMOUNTS) + [1_000.0] * 290
            accounts = []
            for i, amount in enumerate(all_amounts):
                owner = bytes([(i % 250) + 1, (i // 250) + 1]) + bytes(30)
                accounts.append({"pubkey": f"acct{i}",
                                 "account": {"data": [_slice_b64(owner, int(amount * 1e6)), "base64"],
                                             "owner": TOKEN_PROGRAM}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": accounts})
        if method == "getTokenAccountsByOwner":
            owner = params[0]
            balance = 250.0 if owner == DEV else 0.0
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "context": {"slot": 1},
                "value": [{"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"uiAmount": balance}}}}}}]}})
        if method == "getSignaturesForAddress":
            address = params[0]
            limit = (params[1] or {}).get("limit", 1000)
            if address == MINT:
                if limit == 1:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [
                        {"signature": NEWEST_SIG, "blockTime": LAST_TS, "slot": 200}]})
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [
                    {"signature": NEWEST_SIG, "blockTime": LAST_TS, "slot": 200},
                    {"signature": MID_SIG, "blockTime": LAUNCH_TS + 3600, "slot": 150},
                    {"signature": CREATE_SIG, "blockTime": LAUNCH_TS, "slot": 100}]})
            if address == DEV:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [
                    {"signature": FUND_SIG_DEV, "blockTime": FUND_TS, "slot": 90}]})
            if address == BUYER1:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [
                    {"signature": FUND_SIG_BUYER1, "blockTime": FUND_TS, "slot": 90}]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})
        if method == "getTransaction":
            sig = params[0]
            if sig == CREATE_SIG:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _creation_tx()})
            if sig == FUND_SIG_DEV:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": _funding_tx(DEV, FUNDER, FUND_TS)})
            if sig == FUND_SIG_BUYER1:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": _funding_tx(BUYER1, FUNDER, FUND_TS)})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _minimal_tx()})
        return httpx.Response(404, json={"error": f"unknown method {method}"})

    return handler


def _client(state: dict | None = None) -> PublicRpcClient:
    state = state or {}
    transport = httpx.MockTransport(_mock_handler(state))
    return PublicRpcClient(client=httpx.Client(transport=transport))


# ======================================================================
# RPC client: retries, fallback, dead methods
# ======================================================================
def test_rpc_retries_429_then_succeeds():
    state = {"mode": "first_429"}
    rpc = _client(state)
    assert rpc.rpc("getSlot", []) == 12345
    assert state["calls"].count("getSlot") == 3  # 2 x 429, then 200


def test_rpc_raises_after_retries_and_marks_method_dead():
    state = {"mode": "always_429"}
    rpc = _client(state)
    with pytest.raises(RpcError):
        rpc.rpc("getSlot", [])
    assert "getSlot" in rpc.dead_methods
    before = len(state["calls"])
    with pytest.raises(RpcError):
        rpc.rpc("getSlot", [])
    assert len(state["calls"]) == before  # dead: no new request


def test_rpc_falls_back_to_second_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        if "publicnode" in str(request.url):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 999})
        return httpx.Response(500, json={"error": "boom"})

    rpc = PublicRpcClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert rpc.rpc("getSlot", []) == 999


def test_rpc_error_is_deterministic_no_retry():
    state = {"mode": "rpc_error"}
    rpc = _client(state)
    with pytest.raises(RpcError, match="WrongSize"):
        rpc.rpc("getProgramAccounts", [TOKEN_PROGRAM, {}])
    assert state["calls"].count("getProgramAccounts") == 1


# ======================================================================
# Feature computation (mocked RPC world)
# ======================================================================
def test_dev_sold_pct_from_creation_and_current_balance():
    rpc = _client()
    result = enrich_record(_rugged_record(), rpc)
    assert result.features["dev_sold_pct"] == 75.0  # 1000 -> 250
    assert result.notes["dev_sold_pct"]["source"] == "rpc_creation_tx+owner_balance"


def test_dev_sold_missing_when_no_creation_found():
    rpc = _client({"mode": "no_signatures"})
    result = enrich_record(_rugged_record(), rpc)
    assert "dev_sold_pct" not in result.features
    assert "cluster_size" not in result.features


def test_lp_removed_from_stored_liquidity():
    rpc = _client()
    assert enrich_record(_rugged_record(liquidity_usd=500.0), rpc).features["lp_removed"] is True
    assert enrich_record(_rugged_record(liquidity_usd=5000.0), rpc).features["lp_removed"] is False
    assert "lp_removed" not in enrich_record(_rugged_record(liquidity_usd=None), rpc).features


def test_time_to_collapse_rugged_uses_last_activity():
    rpc = _client()
    result = enrich_record(_rugged_record(), rpc)
    assert result.features["time_to_collapse_minutes"] == 2 * 24 * 60  # Nov 3 -> Nov 5
    assert "last on-chain activity" in result.notes["time_to_collapse_minutes"]["basis"]


def test_time_to_collapse_survived_uses_age():
    rpc = _client()
    result = enrich_record(_survived_record(), rpc)
    # Age at enrichment: fetched Nov 13 + pair age 331.65d -> ~331.65 days.
    assert result.features["time_to_collapse_minutes"] >= 331 * 24 * 60
    assert "no collapse observed" in result.notes["time_to_collapse_minutes"]["basis"]


def test_top_shares_from_largest_accounts():
    rpc = _client()
    result = enrich_record(_rugged_record(), rpc)
    assert result.features["top1_share"] == pytest.approx(0.30)
    assert result.features["top10_share"] == pytest.approx(0.71)
    assert result.notes["top1_share"]["source"] == "rpc_largest_accounts"


def test_program_accounts_request_has_no_datasize_filter():
    # Regression (live probe 2026-11-13): a dataSize 165 filter
    # misses almost all Token-2022 accounts (60 of 16,676 — the
    # rest have extensions) and the dataSize+dataSlice combination
    # is rejected with "WrongSize". The query must be memcmp +
    # dataSlice only.
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "getProgramAccounts":
            seen.append(body["params"][1])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})
        return _mock_handler({})(request)

    rpc = PublicRpcClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert get_token_accounts_for_mint(rpc, TOKEN_PROGRAM, MINT) == []
    assert len(seen) == 1
    filters = seen[0]["filters"]
    assert not any("dataSize" in f for f in filters)
    assert {"memcmp": {"offset": 0, "bytes": MINT}} in filters
    assert seen[0]["dataSlice"] == {"offset": 32, "length": 40}


def test_token2022_mint_gets_holder_data_via_owner_program():
    # Regression (live run 2026-11-13): the Token-2022 program
    # constant was wrong (...EPF3SfwV1CZ5LwU4MsS holds NO account),
    # so Token-2022 mints silently skipped the program-accounts
    # fallback. The fallback now uses the owner returned by
    # getAccountInfo, which is authoritative.
    state = {"mode": "largest_429", "mint_owner": TOKEN_2022_PROGRAM}
    rpc = _client(state)
    result = enrich_record(_rugged_record(), rpc)
    assert result.features["top1_share"] == pytest.approx(0.30)
    assert result.features["top10_share"] == pytest.approx(0.71)
    assert result.features["holder_count"] == 300
    assert TOKEN_2022_PROGRAM == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def test_program_accounts_fallback_when_largest_throttled():
    rpc = _client({"mode": "largest_429"})
    result = enrich_record(_rugged_record(), rpc)
    assert result.features["top1_share"] == pytest.approx(0.30)
    assert result.features["top10_share"] == pytest.approx(0.71)
    assert result.features["holder_count"] == 300
    assert result.notes["top1_share"]["source"] == "rpc_program_accounts"


def test_program_accounts_rpc_error_raises():
    with pytest.raises(RpcError, match="WrongSize"):
        get_token_accounts_for_mint(
            _client({"mode": "rpc_error"}), TOKEN_PROGRAM, MINT
        )


def test_holder_features_missing_when_both_sources_fail():
    rpc = _client({"mode": "no_holders"})
    result = enrich_record(_rugged_record(), rpc)
    assert "top1_share" not in result.features
    assert "top10_share" not in result.features
    assert "holder_count" not in result.features
    assert "wash_score" not in result.features


def test_wash_score_from_fetched_shares():
    rpc = _client()
    result = enrich_record(_rugged_record(), rpc)
    assert "wash_score" in result.features
    assert 0.0 <= result.features["wash_score"] <= 100.0


def test_wash_score_missing_without_holder_data():
    from src.advanced import wash_score as _ws

    assert _ws(None, None, None).insufficient_data


def test_cluster_size_shared_funder():
    rpc = _client()
    result = enrich_record(_rugged_record(), rpc)
    assert result.features["cluster_size"] == 2  # DEV + BUYER1 share FUNDER
    assert result.notes["cluster_size"]["source"] == "advanced.StealthClusterDetector"


def test_cluster_size_zero_when_no_shared_funder():
    # BUYER1 funded by a different funder: no shared funder, no cluster.
    def handler(request: httpx.Request) -> httpx.Response:
        base = _mock_handler({})

        def _patched(req: httpx.Request) -> httpx.Response:
            resp = base(req)
            if json.loads(req.content).get("method") == "getTransaction":
                sig = json.loads(req.content)["params"][0]
                if sig == FUND_SIG_BUYER1:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                     "result": _funding_tx(BUYER1, FUNDER2, FUND_TS)})
            return resp

        return _patched(request)

    rpc = PublicRpcClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = enrich_record(_rugged_record(), rpc)
    assert result.features["cluster_size"] == 0


def test_bonding_curve_vault_is_not_an_early_buyer():
    rpc = _client()
    result = enrich_record(_rugged_record(), rpc)
    # CURVE holds 80% of supply in the creation tx; it must not
    # become a funding-event wallet (cluster is DEV + BUYER1 = 2).
    assert result.features["cluster_size"] == 2


# ======================================================================
# Budget, checkpoint, merge, CLI
# ======================================================================
def test_call_budget_stops_record_as_partial():
    rpc = _client()
    result = enrich_record(_rugged_record(liquidity_usd=500.0), rpc, max_calls=2)
    assert result.status == "partial"
    # lp_removed needs no call, so it is still filled.
    assert result.features.get("lp_removed") is True
    assert "dev_sold_pct" not in result.features


def test_enrich_dataset_checkpoints_and_resumes(tmp_path):
    # Mode largest_429: holder data comes from program accounts,
    # so all 8 features fill and the record is "done" (resumable).
    checkpoint = tmp_path / "ckpt.json"
    rpc = _client({"mode": "largest_429"})
    records = [_rugged_record(), _rugged_record()]
    results, _ = enrich_dataset(records, rpc, checkpoint)
    assert len(results) == 2
    assert results[0].status == "done"
    assert load_checkpoint(checkpoint)["records"][MINT]["status"] == "done"
    calls_after_first = rpc.calls
    # Second run with the same checkpoint: done records are skipped.
    results2, _ = enrich_dataset(records, rpc, checkpoint)
    assert rpc.calls == calls_after_first  # no new RPC calls for done records
    assert len(results2) == 2


def test_apply_enrichment_preserves_fields_and_rule_version():
    rpc = _client()
    result = enrich_record(_rugged_record(), rpc)
    records = apply_enrichment([_rugged_record()], [result])
    assert records[0]["outcome"] == "rugged"
    assert records[0]["labelling_rule_version"] == "real-v2"
    assert records[0]["market_cap_usd"] == 1899.0
    assert records[0]["dev_sold_pct"] == 75.0
    assert records[0]["enrichment"]["status"] in ("done", "partial")
    assert "dev_sold_pct" in records[0]["enrichment"]["features"]


def test_coverage_report_before_after():
    before = [_rugged_record(), _survived_record()]
    rpc = _client()
    results = [enrich_record(r, rpc) for r in before]
    after = apply_enrichment(before, results)
    report = coverage_report(before, after)
    assert report["n_labelled"] == 2
    assert report["per_feature"]["dev_sold_pct"]["before"] == 0
    assert report["per_feature"]["dev_sold_pct"]["after"] == 2
    assert report["per_feature"]["lp_removed"]["after"] == 2
    assert report["per_feature"]["liquidity_usd"]["before"] == 2


def test_summarize_counts_all_ten_features():
    summary = summarize([_rugged_record(), _survived_record()])
    assert set(summary["feature_coverage"]) == {
        "collapse_pct", "dev_sold_pct", "lp_removed",
        "time_to_collapse_minutes", "wash_score", "top1_share",
        "top10_share", "holder_count", "cluster_size", "liquidity_usd",
    }
    assert "dev_sold_pct" in summary["features_always_missing"]
    enriched = apply_enrichment(
        [_rugged_record()], [enrich_record(_rugged_record(), _client())]
    )
    summary2 = summarize(enriched)
    assert summary2["feature_coverage"]["dev_sold_pct"] == 1
    assert "dev_sold_pct" not in summary2["features_always_missing"]


def test_cli_invalid_args_exit_2(tmp_path):
    assert main(["--limit", "0"]) == 2
    assert main(["--dataset", str(tmp_path / "nope.json")]) == 2
    assert main(["--max-calls-per-record", "0"]) == 2


def test_cli_unreachable_rpc_exits_2(tmp_path):
    dataset = tmp_path / "ds.json"
    dataset.write_text(json.dumps([_rugged_record()]), encoding="utf-8")
    rpc = _client({"mode": "always_429"})
    assert main(["--dataset", str(dataset)], rpc=rpc) == 2


def test_cli_enriches_and_writes_dataset_and_summary(tmp_path, capsys, monkeypatch):
    # The CLI writes the pre-enrichment backup to /tmp by default;
    # point it at tmp_path so tests never touch the real backup.
    import src.feature_enrich as fe

    monkeypatch.setattr(fe, "BACKUP_PATH", tmp_path / "backup.json")
    dataset = tmp_path / "ds.json"
    dataset.write_text(
        json.dumps([_rugged_record(), _survived_record()]), encoding="utf-8"
    )
    rpc = _client()
    rc = main(
        ["--dataset", str(dataset), "--checkpoint", str(tmp_path / "ckpt.json"),
         "--json"],
        rpc=rpc,
    )
    assert rc == 0
    saved = json.loads(dataset.read_text(encoding="utf-8"))
    assert saved[0]["dev_sold_pct"] == 75.0
    summary_path = tmp_path / "ds.summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["summary"]["feature_coverage"]["dev_sold_pct"] == 2
    out = json.loads(capsys.readouterr().out)
    assert out["records_enriched"] == 2
    assert out["coverage"]["per_feature"]["dev_sold_pct"]["after"] == 2
