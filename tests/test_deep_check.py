"""Tests for src.deep_check — one-command deep check for a mint.

Synthetic / mocked data only, no network. The motivating case
(POOR) is used as a shape, with synthetic addresses.
"""
import json

import httpx
import pytest

from src.advanced import FundingEvent
from src.deep_check import (
    CoinFetcher,
    DeepChecker,
    build_side_wallets,
    fetch_early_buyers,
    main,
    outcome_from_coin,
    parse_coin,
    resolve_creator,
)
from src.dev_history import DevHistory, MintRecord
from src.indexer import PUMP_FUN_PROGRAM

MINT = "9oJk7eMccQchx3evoN5C5pKQD5Xodb4C1qkhemjjpump"
DEV = "7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk"
FUNDER = "G4JXShDM6NqP1XpMo3gpY24wxPj8NVRqqKMTKv5gLNfY"
BUYER2 = "ApijrsL2NWdsK1EHyNex3X2uE9HGQdDXmfyXG6aLAo9L"
BUYER3 = "3gHrfi6t4xG2KEH4gga9HxLRYqws9iTKdkLG55NwaTZJ"
OTHER = "H9TMtTxBL84rqpX8uqC3TpTgM9BmE3bJDPzStFk8U1Th"

LAUNCH = 1794595421  # pair creation, epoch seconds
BUY_T = LAUNCH + 60
OLD_T = LAUNCH - 100000  # > 24h before launch


def _pair(mint=MINT, mc=43905, liq=14654, vol=138955, chg=38.4,
          price="0.00004397", created_ms=LAUNCH * 1000):
    return {
        "chainId": "solana",
        "dexId": "pumpswap",
        "pairAddress": "E" * 44,
        "baseToken": {"address": mint, "name": "Poor Coin", "symbol": "POOR"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
        "priceUsd": price,
        "txns": {"h24": {"buys": 2571, "sells": 1915}},
        "volume": {"h24": vol, "h6": vol, "h1": vol},
        "priceChange": {"h24": chg, "h6": chg, "h1": chg, "m5": -14.95},
        "liquidity": {"usd": liq},
        "fdv": mc,
        "marketCap": mc,
        "pairCreatedAt": created_ms,
    }


def _dex_transport(pairs=None, status=200):
    payload = pairs if pairs is not None else [_pair()]

    def handler(request):
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _create_tx(mint, dev, sig, block_time=LAUNCH):
    """Minimal pump.fun create transaction (mirrors test_dev_history)."""
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": dev}],
                "instructions": [{
                    "programId": PUMP_FUN_PROGRAM,
                    "parsed": {"info": {"mint": mint}},
                    "accounts": [mint],
                }],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
        "blockTime": block_time,
    }


def _buy_tx(sig, buyer, amount, block_time, mint=MINT, pre=0):
    """A token-balance-diff buy: post exceeds pre by ``amount``."""
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": buyer}],
                "instructions": [],
            },
        },
        "meta": {
            "preTokenBalances": (
                [{"owner": buyer, "mint": mint,
                  "uiTokenAmount": {"uiAmount": pre}}] if pre else []
            ),
            "postTokenBalances": [
                {"owner": buyer, "mint": mint,
                 "uiTokenAmount": {"uiAmount": pre + amount}}
            ],
        },
        "blockTime": block_time,
    }


class StubIndexer:
    """Stub HeliusIndexer: canned DAS, signatures, txs, transfers."""

    def __init__(self, enabled=True, das=None, signatures=None, txs=None,
                 transfers=None, das_error=None):
        self.enabled = enabled
        self._das = das
        self._das_error = das_error
        self._signatures = signatures or []
        self._txs = txs or {}
        self._transfers = transfers or []

    def _rpc(self, method, params):
        if self._das_error is not None:
            raise RuntimeError(self._das_error)
        assert method == "getAsset"
        return self._das

    def get_signatures(self, address, limit=50):
        return self._signatures[:limit]

    def get_transaction(self, signature):
        return self._txs.get(signature)

    def fetch_funding_transfers(self, wallets, limit_per_wallet=20):
        return list(self._transfers)


class StubBuilder:
    """Stub DevHistoryBuilder returning a canned history."""

    def __init__(self, history):
        self.history = history

    def build(self, dev_wallet, limit=50, ml_model=None):
        return self.history


def _record(mint, dev, outcome="rugged"):
    return MintRecord(
        mint=mint,
        dev_wallet=dev,
        outcome=outcome,
        peak_market_cap_usd=100000,
        current_market_cap_usd=1000,
        evidence={"dexscreener": f"https://dexscreener.com/solana/{mint}"},
    )


def _history(dev, outcomes):
    records = [_record(f"{'R' * 40}{i:04d}", dev, o) for i, o in enumerate(outcomes)]
    return DevHistory.from_records(dev, records)


def _checker(indexer=None, builder=None, registry_data=None):
    return DeepChecker(
        http_client=httpx.Client(transport=_dex_transport()),
        indexer=indexer if indexer is not None else StubIndexer(),
        registry_data=registry_data or {},
        history_builder=builder,
    )


# --- Coin parsing -------------------------------------------------------

def test_parse_coin_rich_fields():
    coin = parse_coin(_pair())
    assert coin["mint"] == MINT
    assert coin["symbol"] == "POOR"
    assert coin["price_usd"] == pytest.approx(0.00004397)
    assert coin["market_cap_usd"] == 43905
    assert coin["liquidity_usd"] == 14654
    assert coin["volume_24h_usd"] == 138955
    assert coin["change_24h_pct"] == 38.4
    assert coin["txns_24h_buys"] == 2571
    assert coin["txns_24h_sells"] == 1915
    assert coin["pair_created_at"] is not None
    assert coin["pair_age_minutes"] is not None and coin["pair_age_minutes"] >= 0
    assert coin["dex_id"] == "pumpswap"


def test_parse_coin_without_mint_is_none():
    assert parse_coin({"baseToken": {}}) is None
    assert parse_coin("not a dict") is None


def test_coin_fetcher_picks_most_liquid_pair():
    low = _pair(mc=1000, liq=100)
    high = _pair(mc=50000, liq=90000)
    fetcher = CoinFetcher(httpx.Client(transport=_dex_transport([low, high])))
    coin = fetcher.fetch(MINT)
    assert coin["liquidity_usd"] == 90000


def test_coin_fetcher_no_pair_returns_none():
    fetcher = CoinFetcher(httpx.Client(transport=_dex_transport([])))
    assert fetcher.fetch(MINT) is None


def test_coin_fetcher_http_error_raises():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    fetcher = CoinFetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError):
        fetcher.fetch(MINT)


def test_outcome_fresh_pumping_coin_is_inconclusive():
    assert outcome_from_coin(parse_coin(_pair())) == "inconclusive"


def test_outcome_collapsed_thin_liquidity_is_rugged():
    old = _pair(chg=-95.0, liq=500, created_ms=(LAUNCH - 3 * 24 * 3600) * 1000)
    assert outcome_from_coin(parse_coin(old)) == "rugged"


def test_outcome_no_market_cap_is_insufficient():
    coin = parse_coin(_pair(mc=None))
    assert outcome_from_coin(coin) == "insufficient_data"


# --- Creator resolution -------------------------------------------------

def test_resolve_creator_via_das_picks_highest_share():
    indexer = StubIndexer(das={"creators": [
        {"address": OTHER, "share": 5, "verified": False},
        {"address": DEV, "share": 95, "verified": True},
    ]})
    creator, method, note = resolve_creator(indexer, MINT)
    assert creator == DEV
    assert method == "das"
    assert note is None


def test_resolve_creator_falls_back_to_creation_tx():
    txs = {"sig-create": _create_tx(MINT, DEV, "sig-create")}
    indexer = StubIndexer(
        das={"creators": []},
        signatures=[{"signature": "sig-later"}, {"signature": "sig-create"}],
        txs=txs,
    )
    creator, method, note = resolve_creator(indexer, MINT)
    assert creator == DEV
    assert method == "creation_tx"
    assert note is not None


def test_resolve_creator_none_when_nothing_found():
    indexer = StubIndexer(das={}, signatures=[])
    creator, method, note = resolve_creator(indexer, MINT)
    assert creator is None
    assert method == "none"
    assert note


def test_resolve_creator_das_error_falls_back():
    txs = {"sig-create": _create_tx(MINT, DEV, "sig-create")}
    indexer = StubIndexer(
        das=None,
        das_error="401 Unauthorized",
        signatures=[{"signature": "sig-create"}],
        txs=txs,
    )
    creator, method, _ = resolve_creator(indexer, MINT)
    assert creator == DEV
    assert method == "creation_tx"


# --- Early buyers -------------------------------------------------------

def test_early_buyers_aggregate_by_wallet():
    txs = {
        "sig1": _buy_tx("sig1", BUYER2, 1000, BUY_T),
        "sig2": _buy_tx("sig2", BUYER2, 500, BUY_T + 30),
        "sig3": _buy_tx("sig3", FUNDER, 2000, BUY_T + 10),
    }
    indexer = StubIndexer(
        signatures=[{"signature": s} for s in ("sig3", "sig2", "sig1")],
        txs=txs,
    )
    buyers, reached, note = fetch_early_buyers(indexer, MINT, limit=50)
    assert note is None
    assert reached is True  # fewer signatures than the limit: full window
    by_wallet = {b["wallet"]: b for b in buyers}
    assert by_wallet[BUYER2]["amount"] == 1500
    assert by_wallet[BUYER2]["tx_count"] == 2
    assert by_wallet[FUNDER]["amount"] == 2000
    # Oldest buy first.
    assert buyers[0]["wallet"] == BUYER2


def test_early_buyers_ignore_sells_and_other_mints():
    txs = {
        # A sell: post < pre -> not a buy.
        "sig1": _buy_tx("sig1", BUYER2, -500, BUY_T, pre=1000),
        # A different mint entirely.
        "sig2": _buy_tx("sig2", FUNDER, 700, BUY_T, mint="X" * 44),
    }
    indexer = StubIndexer(
        signatures=[{"signature": s} for s in ("sig2", "sig1")],
        txs=txs,
    )
    buyers, _, _ = fetch_early_buyers(indexer, MINT, limit=50)
    assert buyers == []


def test_early_buyers_window_not_reaching_creation():
    txs = {"sig1": _buy_tx("sig1", BUYER2, 1000, BUY_T)}
    indexer = StubIndexer(
        signatures=[{"signature": "sig1"}] * 50,
        txs=txs,
    )
    _, reached, _ = fetch_early_buyers(indexer, MINT, limit=50)
    assert reached is False


# --- Side wallets -------------------------------------------------------

def test_side_wallets_funder_is_early_buyer_link():
    transfers = [
        {"from": FUNDER, "to": DEV, "amount_sol": 3.0, "signature": "sigF"},
        {"from": DEV, "to": BUYER2, "amount_sol": 1.0, "signature": "sigD"},
    ]
    txs = {"sigF": {"blockTime": OLD_T}, "sigD": {"blockTime": OLD_T}}
    indexer = StubIndexer(transfers=transfers, txs=txs)
    buyers = [
        {"wallet": FUNDER, "amount": 18_000_000, "bought_at": BUY_T,
         "first_signature": "sigB1", "tx_count": 2},
        {"wallet": BUYER2, "amount": 5_000_000, "bought_at": BUY_T + 5,
         "first_signature": "sigB2", "tx_count": 1},
    ]
    links, events, notes = build_side_wallets(
        indexer, DEV, MINT, buyers, LAUNCH
    )
    kinds = {(l["wallet"], l["link"]) for l in links}
    assert (FUNDER, "funded_dev") in kinds
    assert (BUYER2, "funded_by_dev") in kinds
    assert (FUNDER, "early_buyer") in kinds
    assert (FUNDER, "funder_is_early_buyer") in kinds  # the POOR pattern
    assert (BUYER2, "dev_funded_early_buyer") in kinds
    # Funding events carry the funder + funded_at for the detector.
    by_wallet = {e.wallet: e for e in events}
    assert by_wallet[BUYER2].funder == DEV
    assert by_wallet[BUYER2].funded_at == OLD_T
    assert by_wallet[BUYER2].bought_at == BUY_T + 5
    assert notes  # bounded-window note is always present


def test_side_wallets_shared_funder_link():
    transfers = [
        {"from": FUNDER, "to": DEV, "amount_sol": 3.0, "signature": "sigF"},
        {"from": FUNDER, "to": BUYER2, "amount_sol": 2.0, "signature": "sigG"},
    ]
    indexer = StubIndexer(transfers=transfers)
    buyers = [
        {"wallet": BUYER2, "amount": 100, "bought_at": BUY_T,
         "first_signature": "sigB", "tx_count": 1},
    ]
    links, _, _ = build_side_wallets(indexer, DEV, MINT, buyers, LAUNCH)
    shared = [l for l in links if l["link"] == "shared_funder_with_dev"]
    assert len(shared) == 1
    assert shared[0]["wallet"] == BUYER2
    assert shared[0]["funder"] == FUNDER


def test_side_wallets_no_dev_no_funding_links():
    indexer = StubIndexer(transfers=[])
    buyers = [
        {"wallet": BUYER2, "amount": 100, "bought_at": BUY_T,
         "first_signature": "sigB", "tx_count": 1},
    ]
    links, events, _ = build_side_wallets(indexer, None, MINT, buyers, LAUNCH)
    assert all(l["link"] == "early_buyer" for l in links)
    assert events[0].funder is None


# --- Full check / verdicts ----------------------------------------------

def _full_indexer():
    """Stub with DAS -> DEV, a creation tx + one buy tx on the mint."""
    txs = {
        "sig-create": _create_tx(MINT, DEV, "sig-create"),
        "sig-buy": _buy_tx("sig-buy", FUNDER, 18_000_000, BUY_T),
    }
    return StubIndexer(
        das={"creators": [{"address": DEV, "share": 100, "verified": True}]},
        signatures=[{"signature": "sig-buy"}, {"signature": "sig-create"}],
        txs=txs,
        transfers=[
            {"from": FUNDER, "to": DEV, "amount_sol": 3.0, "signature": "sigF"},
        ],
    )


def test_check_flagged_dev_exits_flagged():
    checker = _checker(
        indexer=_full_indexer(),
        builder=StubBuilder(_history(DEV, ["rugged", "rugged"])),
    )
    report = checker.check(MINT)
    assert report.verdict == "flagged"
    assert report.flagged is True
    assert report.dev_wallet == DEV
    assert report.dev_method == "das"
    assert report.dev_history.rugged_mint_count == 2
    assert report.dev_history.draft_entry() is not None


def test_check_pattern_without_flag_is_high_risk():
    checker = _checker(
        indexer=_full_indexer(),
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    # FUNDER funded the dev AND bought early -> pattern, not flagged.
    assert report.verdict == "high-risk pattern"
    assert report.flagged is False
    assert "funder_is_early_buyer" in report.pattern_signals
    assert report.dev_history.draft_entry() is None


def test_check_clean_dev_no_signals_is_clean():
    indexer = StubIndexer(
        das={"creators": [{"address": DEV, "share": 100, "verified": True}]},
        signatures=[],
        txs={},
        transfers=[],
    )
    checker = _checker(
        indexer=indexer,
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    assert report.verdict == "clean"
    assert report.pattern_signals == []
    assert report.clusters == []


def test_check_no_key_degrades_to_coin_half():
    checker = _checker(
        indexer=StubIndexer(enabled=False),
        builder=StubBuilder(_history(DEV, ["rugged", "rugged"])),
    )
    report = checker.check(MINT)
    assert report.coin is not None
    assert report.coin["symbol"] == "POOR"
    assert report.dev_wallet is None
    assert report.verdict == "unknown"
    assert any("no Helius key" in n for n in report.notes)
    assert report.dev_history is None


def test_check_no_pair_is_insufficient_data():
    checker = DeepChecker(
        http_client=httpx.Client(transport=_dex_transport([])),
        indexer=StubIndexer(
            das={}, signatures=[], txs={}, transfers=[]
        ),
        registry_data={},
        history_builder=StubBuilder(_history(DEV, [])),
    )
    report = checker.check(MINT)
    assert report.coin is None
    assert report.verdict == "insufficient_data"


def test_check_invalid_mint_raises():
    with pytest.raises(ValueError):
        _checker().check("not-an-address")


def test_check_cluster_wiring_finds_shared_funder():
    transfers = [
        {"from": DEV, "to": BUYER2, "amount_sol": 1.0, "signature": "sigD"},
        {"from": DEV, "to": BUYER3, "amount_sol": 1.0, "signature": "sigE"},
    ]
    txs = {
        "sigD": {"blockTime": OLD_T},
        "sigE": {"blockTime": OLD_T},
        "sig-b2": _buy_tx("sig-b2", BUYER2, 100, BUY_T),
        "sig-b3": _buy_tx("sig-b3", BUYER3, 100, BUY_T),
    }
    indexer = StubIndexer(
        das={"creators": [{"address": DEV, "share": 100, "verified": True}]},
        signatures=[
            {"signature": "sig-b3"}, {"signature": "sig-b2"},
            {"signature": "sig-create"},
        ],
        txs={**txs, "sig-create": _create_tx(MINT, DEV, "sig-create")},
        transfers=transfers,
    )
    checker = _checker(
        indexer=indexer,
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    assert len(report.clusters) == 1
    cluster = report.clusters[0]
    assert set(cluster["members"]) == {BUYER2, BUYER3}
    assert "shared_funder" in cluster["signals"]
    assert report.verdict == "high-risk pattern"


def test_report_json_round_trips():
    checker = _checker(
        indexer=_full_indexer(),
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    data = json.loads(json.dumps(report.to_dict()))
    assert data["mint"] == MINT
    assert data["verdict"] == "high-risk pattern"
    assert data["coin"]["symbol"] == "POOR"
    assert data["dev_wallet"] == DEV


def test_report_text_contains_all_sections():
    checker = _checker(
        indexer=_full_indexer(),
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    text = checker.check(MINT).to_text()
    for section in ("Coin", "Dev", "Side wallets", "Clusters", "Wash", "Verdict"):
        assert section in text
    assert "HIGH-RISK PATTERN" in text
    assert "never flags" in text


# --- CLI ------------------------------------------------------------------

def _registry_file(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"entries": [], "sample_data": True}))
    return str(path)


def test_cli_flagged_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("src.http_client.make_client",
                        lambda timeout=30.0: httpx.Client(transport=_dex_transport()))
    stub = _full_indexer()
    monkeypatch.setattr("src.deep_check.HeliusIndexer", lambda *a, **k: stub)
    monkeypatch.setattr(
        "src.deep_check.DevHistoryBuilder",
        lambda indexer, data: StubBuilder(_history(DEV, ["rugged", "rugged"])),
    )
    rc = main(["--mint", MINT, "--registry", _registry_file(tmp_path)])
    assert rc == 1
    assert "FLAGGED" in capsys.readouterr().out


def test_cli_clean_exits_0(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("src.http_client.make_client",
                        lambda timeout=30.0: httpx.Client(transport=_dex_transport()))
    stub = _full_indexer()
    monkeypatch.setattr("src.deep_check.HeliusIndexer", lambda *a, **k: stub)
    monkeypatch.setattr(
        "src.deep_check.DevHistoryBuilder",
        lambda indexer, data: StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    rc = main(["--mint", MINT, "--registry", _registry_file(tmp_path)])
    assert rc == 0
    assert "HIGH-RISK PATTERN" in capsys.readouterr().out


def test_cli_no_key_degrades_exits_0(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.setattr("src.http_client.make_client",
                        lambda timeout=30.0: httpx.Client(transport=_dex_transport()))
    rc = main(["--mint", MINT, "--registry", _registry_file(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dev unknown" in out
    assert "no Helius key" in out
    assert "POOR" in out


def test_cli_invalid_mint_exits_2(monkeypatch, tmp_path):
    rc = main(["--mint", "nope", "--registry", _registry_file(tmp_path)])
    assert rc == 2


def test_cli_wash_inputs_scored(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("src.http_client.make_client",
                        lambda timeout=30.0: httpx.Client(transport=_dex_transport()))
    stub = StubIndexer(enabled=False)
    monkeypatch.setattr("src.deep_check.HeliusIndexer", lambda *a, **k: stub)
    rc = main([
        "--mint", MINT, "--registry", _registry_file(tmp_path),
        "--holders", "40", "--top10", "0.4,0.2,0.1",
    ])
    assert rc == 0
    assert "Wash score:" in capsys.readouterr().out
