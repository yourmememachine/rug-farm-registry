"""Tests for the v2.16 keyless deep check — mint check without a
DexScreener pair and without a Helius key.

Synthetic / mocked data only, no network. The public-RPC layer
is mocked with an httpx MockTransport dispatching on the
JSON-RPC method (and, where one method serves two calls, on
the params). The motivating cases are BagsBack (no DexScreener
pair) and POOR, used as shapes with synthetic addresses.
"""
import base64
import json

import httpx
import pytest

from src.deep_check import DeepChecker, main
from src.dev_history import DevHistory, DevHistoryBuilder, MintRecord
from src.indexer import MintOutcome, PUMP_FUN_PROGRAM
from src.public_rpc import (
    TOKEN_PROGRAM,
    PublicRpcClient,
    PublicRpcIndexer,
    best_metadata_creator,
    b58encode,
    fetch_metadata,
    fetch_rpc_coin,
    get_holder_accounts,
    get_largest_accounts,
    get_mint_account,
    get_token_supply,
    parse_metadata,
)

MINT = "7fAJ48SPtycJNLERz7G2ZDtfgwij6DhW3ArJ8BSnpump"  # BagsBack
DEV = "CGaqXYFcmjY5okNWkBPWjxQrdJ8oUcUCuSnnhVn8aTAR"  # BagsBack dev
OTHER = "H9TMtTxBL84rqpX8uqC3TpTgM9BmE3bJDPzStFk8U1Th"
FUNDER = "G4JXShDM6NqP1XpMo3gpY24wxPj8NVRqqKMTKv5gLNfY"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(text: str) -> bytes:
    """Base58-decode a 32-byte Solana address (test helper)."""
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    full = n.to_bytes(32, "big")
    leading = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading + full[leading:]


def _metadata_bytes(name="BagsBack", symbol="BAGS", creators=None,
                    uri="https://example.com/m.json"):
    """Synthetic Metaplex metadata account bytes.

    creators: [(address_bytes, verified, share)] or None for no
    creators section.
    """
    def _s(text):
        raw = text.encode()
        return len(raw).to_bytes(4, "little") + raw

    out = bytearray()
    out.append(4)  # MetadataV1
    out += bytes(32)  # update authority
    out += bytes(32)  # mint
    out += _s(name)
    out += _s(symbol)
    out += _s(uri)
    out += (500).to_bytes(2, "little")  # seller fee basis points
    if not creators:
        out.append(0)
    else:
        out.append(1)
        out += len(creators).to_bytes(4, "little")
        for addr, verified, share in creators:
            out += addr
            out.append(1 if verified else 0)
            out.append(share)
    return bytes(out)


def _holder_entry(owner_bytes, amount_ui, decimals=6):
    """One getProgramAccounts dataSlice entry: owner + amount."""
    raw = owner_bytes + int(amount_ui * 10**decimals).to_bytes(8, "little")
    return {"account": {"data": [base64.b64encode(raw).decode(), "base64"]}}


class RpcMock:
    """Mock JSON-RPC transport dispatching on method (and params).

    results: {method: entry} where entry is a plain result value
    (returned as {"result": value}), a callable taking params ->
    result, or a list whose first item is a (status, json) tuple:
    a queue of raw responses served in order (for 429 tests).
    Every call is recorded in .calls as (method, params).
    """

    def __init__(self, results):
        self.results = dict(results)
        self.calls: list[tuple] = []

    def handler(self, request):
        body = json.loads(request.content)
        method = body.get("method")
        params = body.get("params", [])
        self.calls.append((method, params))
        entry = self.results.get(method)
        if callable(entry):
            result = entry(params)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": result}
            )
        if isinstance(entry, list) and entry and isinstance(entry[0], tuple):
            status, payload = entry.pop(0)
            return httpx.Response(status, json=payload)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": entry}
        )

    def client(self):
        return PublicRpcClient(
            client=httpx.Client(transport=httpx.MockTransport(self.handler))
        )


def _empty_rpc_client():
    """Every RPC method returns no result."""
    return RpcMock({}).client()


def _dex_transport(pairs=None, status=200):
    payload = pairs if pairs is not None else []

    def handler(request):
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _pair(mint=MINT, mc=2322, liq=38, vol=0, chg=-99.0,
          price="0.000002322", created_ms=1794400000000):
    return {
        "chainId": "solana",
        "dexId": "pumpswap",
        "pairAddress": "E" * 44,
        "baseToken": {"address": mint, "name": "BagsBack", "symbol": "BAGS"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
        "priceUsd": price,
        "txns": {"h24": {"buys": 0, "sells": 0}},
        "volume": {"h24": vol, "h6": vol, "h1": vol},
        "priceChange": {"h24": chg, "h6": chg, "h1": chg, "m5": 0.0},
        "liquidity": {"usd": liq},
        "fdv": mc,
        "marketCap": mc,
        "pairCreatedAt": created_ms,
    }


def _create_tx(mint, dev, sig, block_time=1794400000):
    """Minimal pump.fun create transaction."""
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


def _transfer_tx(sig, src, dst, lamports, block_time=1794300000):
    """A parsed system-transfer transaction."""
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": src}, {"pubkey": dst}],
                "instructions": [{
                    "parsed": {
                        "type": "transfer",
                        "info": {
                            "source": src,
                            "destination": dst,
                            "lamports": lamports,
                        },
                    },
                }],
            },
        },
        "meta": {},
        "blockTime": block_time,
    }


class DisabledIndexer:
    """Stub indexer with no key: the keyless path runs."""

    enabled = False


class StubIndexer:
    """Stub keyed indexer: canned DAS, signatures, txs, transfers."""

    def __init__(self, enabled=True, das=None, signatures=None, txs=None,
                 transfers=None):
        self.enabled = enabled
        self._das = das
        self._signatures = signatures or []
        self._txs = txs or {}
        self._transfers = transfers or []

    def _rpc(self, method, params):
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


def _record(mint, dev, outcome="failed_but_honest"):
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


def _mint_account(decimals=6, mint_auth=None, freeze_auth=None):
    return {
        "value": {
            "owner": TOKEN_PROGRAM,
            "data": {
                "parsed": {
                    "info": {
                        "decimals": decimals,
                        "mintAuthority": mint_auth,
                        "freezeAuthority": freeze_auth,
                    }
                },
                "program": "spl-token",
                "space": 82,
            },
            "executable": False,
            "lamports": 1000000,
            "rentEpoch": 1,
        }
    }


def _program_accounts_dispatch(metadata_bytes, holder_entries):
    """getProgramAccounts callable: metadata call vs holder scan.

    The metadata lookup has no dataSlice; the holder scan does.
    """
    def dispatch(params):
        opts = params[1] if len(params) > 1 else {}
        if opts.get("dataSlice"):
            return list(holder_entries)
        if metadata_bytes is None:
            return []
        return [{"account": {"data": [
            base64.b64encode(metadata_bytes).decode(), "base64"
        ]}}]

    return dispatch


def _fifty_holders():
    """50 holder accounts: top 200k/150k/100k, rest 11k each."""
    entries = []
    amounts = [200000, 150000, 100000] + [11000] * 47
    for i, amount in enumerate(amounts):
        entries.append(_holder_entry(bytes([i + 1]) * 32, amount))
    return entries


# --- Metadata parsing -----------------------------------------------------

def test_parse_metadata_full():
    raw = _metadata_bytes(
        creators=[(_b58decode(DEV), True, 100)]
    )
    meta = parse_metadata(raw)
    assert meta["name"] == "BagsBack"
    assert meta["symbol"] == "BAGS"
    assert meta["uri"] == "https://example.com/m.json"
    assert meta["creators"] == [
        {"address": DEV, "verified": True, "share": 100}
    ]
    assert meta["update_authority"] == "1" * 32


def test_parse_metadata_no_creators():
    meta = parse_metadata(_metadata_bytes(creators=None))
    assert meta["name"] == "BagsBack"
    assert meta["creators"] == []


def test_parse_metadata_malformed_is_none():
    assert parse_metadata(b"") is None
    assert parse_metadata(b"\x05" + bytes(100)) is None  # wrong key
    assert parse_metadata(_metadata_bytes()[:40]) is None  # truncated


def test_best_metadata_creator_verified_wins_tie():
    meta = {"creators": [
        {"address": OTHER, "verified": False, "share": 90},
        {"address": DEV, "verified": True, "share": 90},
    ]}
    assert best_metadata_creator(meta) == DEV
    meta["creators"][1]["verified"] = False
    assert best_metadata_creator(meta) == OTHER  # higher share wins
    assert best_metadata_creator({"creators": []}) is None
    assert best_metadata_creator(None) is None


def test_b58encode_roundtrip():
    assert b58encode(bytes(32)) == "1" * 32
    assert _b58decode(b58encode(bytes([7]) * 32)) == bytes([7]) * 32
    assert b58encode(_b58decode(DEV)) == DEV


# --- Public RPC client ------------------------------------------------------

def test_rpc_returns_result():
    mock = RpcMock({"getSlot": 12345})
    assert mock.client().rpc("getSlot", []) == 12345


def test_rpc_retries_429_then_succeeds():
    mock = RpcMock({"getSlot": [
        (429, {"error": {"code": 429, "message": "slow down"}}),
        (200, {"jsonrpc": "2.0", "id": 1, "result": 99}),
    ]})
    assert mock.client().rpc("getSlot", []) == 99
    assert len(mock.calls) == 2


def test_rpc_endpoint_fallback():
    def handler(request):
        if "api.mainnet-beta" in str(request.url):
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": 7}
        )

    client = PublicRpcClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.rpc("getSlot", []) == 7


def test_rpc_dead_method_after_exhaustion():
    mock = RpcMock({"getSlot": [(429, {"error": "slow"})] * 20})
    client = mock.client()
    with pytest.raises(Exception):
        client.rpc("getSlot", [])
    assert "getSlot" in client.dead_methods
    calls = len(mock.calls)
    with pytest.raises(Exception):
        client.rpc("getSlot", [])
    assert len(mock.calls) == calls  # dead: no further transport calls


def test_rpc_error_response_raises_immediately():
    # A 200 with an RPC-level "error" is deterministic: raise at once.
    mock = RpcMock({"getSlot": [
        (200, {"error": {"code": -32602, "message": "bad"}}),
    ]})
    with pytest.raises(Exception):
        mock.client().rpc("getSlot", [])
    assert len(mock.calls) == 1


# --- Raw fetch helpers ------------------------------------------------------

def test_get_mint_account_revoked_authorities():
    mock = RpcMock({"getAccountInfo": _mint_account()})
    acct = get_mint_account(mock.client(), MINT)
    assert acct["decimals"] == 6
    assert acct["owner"] == TOKEN_PROGRAM
    assert acct["mint_authority"] == "revoked"  # None in parsed info
    assert acct["freeze_authority"] == "revoked"


def test_get_mint_account_kept_authorities():
    mock = RpcMock({"getAccountInfo": _mint_account(mint_auth=DEV, freeze_auth=DEV)})
    acct = get_mint_account(mock.client(), MINT)
    assert acct["mint_authority"] == DEV
    assert acct["freeze_authority"] == DEV


def test_get_token_supply():
    mock = RpcMock({"getTokenSupply": {
        "value": {"amount": "1000000000000", "decimals": 6, "uiAmount": 1000000.0}
    }})
    assert get_token_supply(mock.client(), MINT) == 1000000.0


def test_get_largest_accounts():
    mock = RpcMock({"getTokenLargestAccounts": {"value": [
        {"address": "T" * 44, "uiAmount": 400000.0},
        {"address": "U" * 44, "uiAmount": 300000.0},
    ]}})
    largest = get_largest_accounts(mock.client(), MINT)
    assert [a["amount"] for a in largest] == [400000.0, 300000.0]


def test_get_holder_accounts_sorted_and_counts():
    entries = [
        _holder_entry(bytes([1]) * 32, 100),
        _holder_entry(bytes([2]) * 32, 500),
        _holder_entry(bytes([3]) * 32, 0),  # zero balance: listed, not a holder
    ]
    mock = RpcMock({"getProgramAccounts": entries})
    accounts = get_holder_accounts(mock.client(), TOKEN_PROGRAM, MINT, 6)
    assert [a["amount"] for a in accounts] == [500.0, 100.0, 0.0]
    assert accounts[0]["owner"] == b58encode(bytes([2]) * 32)
    assert len([a for a in accounts if a["amount"] > 0]) == 2


def test_fetch_metadata_via_program_accounts():
    raw = _metadata_bytes(creators=[(_b58decode(DEV), True, 100)])
    mock = RpcMock({"getProgramAccounts": _program_accounts_dispatch(raw, [])})
    meta = fetch_metadata(mock.client(), MINT)
    assert meta["symbol"] == "BAGS"
    assert meta["creators"][0]["address"] == DEV


def test_fetch_metadata_none_when_no_account():
    mock = RpcMock({"getProgramAccounts": []})
    assert fetch_metadata(mock.client(), MINT) is None


# --- Keyless coin card ------------------------------------------------------

def _rpc_coin_mock(largest=None):
    raw = _metadata_bytes(creators=[(_b58decode(DEV), True, 100)])
    return RpcMock({
        "getAccountInfo": _mint_account(),
        "getTokenSupply": {
            "value": {"amount": "1000000000000", "decimals": 6, "uiAmount": 1000000.0}
        },
        "getTokenLargestAccounts": largest,
        "getProgramAccounts": _program_accounts_dispatch(raw, _fifty_holders()),
    })


def test_fetch_rpc_coin_full_card():
    coin, note, metadata = fetch_rpc_coin(_rpc_coin_mock().client(), MINT)
    assert coin is not None
    assert coin["source"] == "public_rpc"
    assert coin["name"] == "BagsBack"
    assert coin["symbol"] == "BAGS"
    assert coin["supply"] == 1000000.0
    assert coin["mint_authority"] == "revoked"
    assert coin["freeze_authority"] == "revoked"
    assert coin["holder_count"] == 50
    assert coin["top1_share"] == pytest.approx(0.2)
    assert coin["top10_share"] == pytest.approx(0.527)
    # No price on-chain — explicit Nones, never guessed.
    assert coin["price_usd"] is None
    assert coin["market_cap_usd"] is None
    assert coin["liquidity_usd"] is None
    assert "No price, market cap or volume" in note
    assert metadata["creators"][0]["address"] == DEV


def test_fetch_rpc_coin_all_missing():
    coin, note, metadata = fetch_rpc_coin(_empty_rpc_client(), MINT)
    assert coin is None
    assert metadata is None
    assert "no on-chain mint data" in note


def test_fetch_rpc_coin_largest_429_falls_back_to_scan():
    raw = _metadata_bytes(creators=[(_b58decode(DEV), True, 100)])
    mock = RpcMock({
        "getAccountInfo": _mint_account(),
        "getTokenSupply": {
            "value": {"amount": "1000000000000", "decimals": 6, "uiAmount": 1000000.0}
        },
        # Largest-accounts stays throttled on both endpoints.
        "getTokenLargestAccounts": [(429, {"error": "slow"})] * 10,
        "getProgramAccounts": _program_accounts_dispatch(raw, _fifty_holders()),
    })
    client = mock.client()
    coin, note, _ = fetch_rpc_coin(client, MINT)
    assert coin is not None
    assert coin["holder_count"] == 50
    assert coin["top1_share"] == pytest.approx(0.2)
    assert "largest-accounts lookup failed" in note
    assert "getTokenLargestAccounts" in client.dead_methods


def test_fetch_rpc_coin_no_mint_account_no_scan():
    # Mint account lookup fails: no program/decimals, so no holder
    # scan — but supply + metadata still build a thin card.
    raw = _metadata_bytes(creators=[(_b58decode(DEV), True, 100)])
    mock = RpcMock({
        "getAccountInfo": [
            (200, {"error": {"code": -32602, "message": "bad"}}),
        ],
        "getTokenSupply": {
            "value": {"amount": "1000000000000", "decimals": 6, "uiAmount": 1000000.0}
        },
        "getTokenLargestAccounts": None,
        "getProgramAccounts": _program_accounts_dispatch(raw, []),
    })
    coin, note, _ = fetch_rpc_coin(mock.client(), MINT)
    assert coin is not None
    assert coin["holder_count"] is None
    assert coin["symbol"] == "BAGS"
    assert "mint account lookup failed" in note


# --- Keyless indexer adapter ------------------------------------------------

def test_public_rpc_indexer_get_asset_from_metadata():
    raw = _metadata_bytes(creators=[(_b58decode(DEV), True, 100)])
    mock = RpcMock({"getProgramAccounts": _program_accounts_dispatch(raw, [])})
    indexer = PublicRpcIndexer(mock.client())
    asset = indexer._rpc("getAsset", {"id": MINT})
    assert asset["creators"][0]["address"] == DEV
    assert indexer._rpc("getAsset", [MINT])["creators"][0]["address"] == DEV


def test_public_rpc_indexer_other_das_method_raises():
    indexer = PublicRpcIndexer(_empty_rpc_client())
    with pytest.raises(RuntimeError):
        indexer._rpc("getAssetsByOwner", {"owner": DEV})


def test_public_rpc_indexer_funding_transfers():
    txs = {
        "sigF": _transfer_tx("sigF", FUNDER, DEV, 3_000_000_000),
        "sigD": _transfer_tx("sigD", DEV, OTHER, 1_000_000_000),
    }

    def sigs(params):
        return [{"signature": "sigF"}, {"signature": "sigD"}]

    def tx(params):
        return txs.get(params[0])

    mock = RpcMock({
        "getSignaturesForAddress": sigs,
        "getTransaction": tx,
    })
    indexer = PublicRpcIndexer(mock.client())
    transfers = indexer.fetch_funding_transfers([DEV])
    by_sig = {t["signature"]: t for t in transfers}
    assert by_sig["sigF"]["from"] == FUNDER
    assert by_sig["sigF"]["to"] == DEV
    assert by_sig["sigF"]["amount_sol"] == 3.0
    assert by_sig["sigD"]["to"] == OTHER


def test_public_rpc_indexer_fetch_mint_outcome_delegates(monkeypatch):
    seen = {}

    class StubOutcomeIndexer:
        def __init__(self, api_key=""):
            seen["api_key"] = api_key

        def fetch_mint_outcome(self, mint, dev_wallet=None):
            seen["mint"] = mint
            return MintOutcome(mint=mint, dev_wallet=dev_wallet)

    monkeypatch.setattr("src.indexer.HeliusIndexer", StubOutcomeIndexer)
    indexer = PublicRpcIndexer(_empty_rpc_client())
    outcome = indexer.fetch_mint_outcome(MINT, dev_wallet=DEV)
    assert outcome.mint == MINT
    assert seen["mint"] == MINT
    assert seen["api_key"] == ""  # keyless delegation, no key used


# --- DeepChecker, keyless end to end ----------------------------------------

def _keyless_checker(mock, dex_pairs=None, builder=None):
    return DeepChecker(
        http_client=httpx.Client(transport=_dex_transport(dex_pairs)),
        indexer=DisabledIndexer(),
        registry_data={},
        history_builder=builder,
        rpc_client=mock.client(),
    )


def test_keyless_dex_miss_returns_rpc_coin_and_metadata_creator():
    checker = _keyless_checker(
        _rpc_coin_mock(),
        dex_pairs=[],
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    assert report.coin is not None
    assert report.coin["source"] == "public_rpc"
    assert report.coin["price_usd"] is None
    assert report.coin["holder_count"] == 50
    assert report.coin_note is not None
    assert "public Solana RPC" in report.coin_note
    assert report.dev_wallet == DEV
    assert report.dev_method == "metadata"
    assert report.dev_history is not None
    assert report.wash is not None
    assert report.wash["level"] == "low"
    assert report.verdict == "clean"
    assert report.flagged is False
    assert any("keyless" in n for n in report.notes)
    assert any("last 100 signatures" in n for n in report.notes)
    text = report.to_text()
    assert "public Solana RPC" in text
    assert "mint authority: revoked" in text
    assert "holders: 50" in text


def test_keyless_dex_pair_uses_dex_coin_but_metadata_creator():
    raw = _metadata_bytes(creators=[(_b58decode(DEV), True, 100)])
    mock = RpcMock({
        "getProgramAccounts": _program_accounts_dispatch(raw, []),
        "getSignaturesForAddress": [],
    })
    checker = _keyless_checker(
        mock,
        dex_pairs=[_pair()],
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    assert report.coin["price_usd"] is not None  # DexScreener market data
    assert report.coin.get("source") != "public_rpc"
    assert report.coin_note is None
    assert report.dev_wallet == DEV
    assert report.dev_method == "metadata"
    assert report.verdict == "clean"


def test_keyless_metadata_without_creators_falls_back_to_creation_tx():
    raw = _metadata_bytes(creators=None)
    txs = {"sig-create": _create_tx(MINT, DEV, "sig-create")}

    def sigs(params):
        return [{"signature": "sig-create"}] if params[0] == MINT else []

    def tx(params):
        return txs.get(params[0])

    mock = RpcMock({
        "getProgramAccounts": _program_accounts_dispatch(raw, []),
        "getSignaturesForAddress": sigs,
        "getTransaction": tx,
    })
    checker = _keyless_checker(
        mock,
        dex_pairs=[_pair()],
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    assert report.dev_wallet == DEV
    assert report.dev_method == "creation_tx"
    assert report.dev_note is not None


def test_keyed_path_ignores_metadata_when_key_set():
    raw = _metadata_bytes(creators=[(_b58decode(OTHER), True, 100)])
    mock = RpcMock({
        "getProgramAccounts": _program_accounts_dispatch(raw, []),
    })
    checker = DeepChecker(
        http_client=httpx.Client(transport=_dex_transport([_pair()])),
        indexer=StubIndexer(
            das={"creators": [{"address": DEV, "share": 100, "verified": True}]},
            signatures=[],
            txs={},
            transfers=[],
        ),
        registry_data={},
        history_builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
        rpc_client=mock.client(),
    )
    report = checker.check(MINT)
    assert report.dev_wallet == DEV
    assert report.dev_method == "das"  # Helius DAS wins; keyless never runs
    assert "getProgramAccounts" not in [m for m, _ in mock.calls]


def test_keyless_wash_high_from_rpc_holders_is_pattern_not_flag():
    raw = _metadata_bytes(creators=[(_b58decode(DEV), True, 100)])
    concentrated = [
        _holder_entry(bytes([1]) * 32, 900000),
        _holder_entry(bytes([2]) * 32, 50000),
    ]
    mock = RpcMock({
        "getAccountInfo": _mint_account(),
        "getTokenSupply": {
            "value": {"amount": "1000000000000", "decimals": 6, "uiAmount": 1000000.0}
        },
        "getTokenLargestAccounts": None,
        "getProgramAccounts": _program_accounts_dispatch(raw, concentrated),
    })
    checker = _keyless_checker(
        mock,
        dex_pairs=[],
        builder=StubBuilder(_history(DEV, ["failed_but_honest"])),
    )
    report = checker.check(MINT)
    assert report.wash is not None
    assert report.wash["level"] == "high"
    assert report.verdict == "high-risk pattern"
    assert report.flagged is False
    assert "wash_high" in report.pattern_signals


def test_keyless_flagged_dev_still_flags():
    checker = _keyless_checker(
        _rpc_coin_mock(),
        dex_pairs=[],
        builder=StubBuilder(_history(DEV, ["rugged", "rugged"])),
    )
    report = checker.check(MINT)
    assert report.verdict == "flagged"
    assert report.flagged is True


def test_both_sources_empty_cli_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.setattr(
        "src.http_client.make_client",
        lambda timeout=30.0: httpx.Client(transport=_dex_transport([])),
    )
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"entries": [], "sample_data": True}))
    rc = main(["--mint", MINT, "--registry", str(path)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "no data for this mint either" in out


def test_keyless_dev_history_via_real_builder(monkeypatch):
    mint1, mint2 = "A" * 44, "B" * 44
    txs = {
        "sig-1": _create_tx(mint1, DEV, "sig-1"),
        "sig-2": _create_tx(mint2, DEV, "sig-2"),
    }

    def sigs(params):
        if params[0] == DEV:
            return [{"signature": "sig-1"}, {"signature": "sig-2"}]
        return []

    def tx(params):
        return txs.get(params[0])

    class StubOutcomeIndexer:
        def __init__(self, api_key=""):
            pass

        def fetch_mint_outcome(self, mint, dev_wallet=None):
            if mint == mint1:
                return MintOutcome(
                    mint=mint, dev_wallet=dev_wallet,
                    peak_market_cap_usd=100000, current_market_cap_usd=1000,
                    lp_removed=True, dev_sold_pct=0.0,
                    time_to_collapse_minutes=60,
                )
            return MintOutcome(
                mint=mint, dev_wallet=dev_wallet,
                peak_market_cap_usd=100000, current_market_cap_usd=100000,
                lp_removed=False, dev_sold_pct=0.0,
                time_to_collapse_minutes=60,
            )

    monkeypatch.setattr("src.indexer.HeliusIndexer", StubOutcomeIndexer)
    mock = RpcMock({
        "getSignaturesForAddress": sigs,
        "getTransaction": tx,
    })
    builder = DevHistoryBuilder(PublicRpcIndexer(mock.client()), {})
    history = builder.build(DEV, limit=5)
    assert len(history.mints) == 2
    assert history.rugged_mint_count == 1
    assert history.verdict == "under_review"


# --- Smoke-test regressions (BagsBack live run, Nov 13) -------------------

def test_parse_funding_transfers_skips_memo_string_parsed():
    """Memo instructions parse to a string, not a dict — the
    BagsBack live run crashed the whole funding scan on one.
    """
    from src.chain_index import parse_funding_transfers

    tx = {
        "transaction": {
            "signatures": ["sigM"],
            "message": {
                "accountKeys": [{"pubkey": FUNDER}, {"pubkey": DEV}],
                "instructions": [
                    {
                        "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
                        "parsed": "arbHz7BCK9QEzHIps1ML9mnIwjJ1iuT0cP1XVuGkF1AG5A",
                    },
                    {
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": FUNDER,
                                "destination": DEV,
                                "lamports": 3_000_000_000,
                            },
                        },
                    },
                ],
            },
        },
    }
    edges = parse_funding_transfers(tx)
    assert len(edges) == 1
    assert edges[0]["from"] == FUNDER
    assert edges[0]["to"] == DEV
    assert edges[0]["amount_lamports"] == 3_000_000_000


def test_keyless_funding_scan_survives_memo_instruction():
    """Same shape through the keyless indexer: memo tx + transfer tx."""
    memo_tx = _transfer_tx("sigM", FUNDER, DEV, 0)
    memo_tx["transaction"]["message"]["instructions"] = [{
        "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        "parsed": "aGVsbG8gbWVtbw==",
    }]
    txs = {
        "sigM": memo_tx,
        "sigF": _transfer_tx("sigF", FUNDER, DEV, 3_000_000_000),
    }

    def sigs(params):
        return [{"signature": "sigM"}, {"signature": "sigF"}]

    def tx(params):
        return txs.get(params[0])

    mock = RpcMock({
        "getSignaturesForAddress": sigs,
        "getTransaction": tx,
    })
    transfers = PublicRpcIndexer(mock.client()).fetch_funding_transfers([DEV])
    assert len(transfers) == 1
    assert transfers[0]["signature"] == "sigF"
    assert transfers[0]["amount_sol"] == 3.0


def test_resolve_creator_pages_back_to_creation():
    """BagsBack's mint has 200+ signatures: the creation is past
    the first 100-signature window, so creator resolution must
    page backwards to find it.
    """
    from src.deep_check import resolve_creator

    total = 250
    sigs = [{"signature": f"sig-{i:04d}"} for i in range(total - 1, -1, -1)]
    txs = {f"sig-{0:04d}": _create_tx(MINT, DEV, "sig-0000")}

    class PagingStubIndexer(StubIndexer):
        def get_signatures(self, address, limit=50, before=None):
            all_sigs = self._signatures
            if before:
                idx = next(
                    (i for i, s in enumerate(all_sigs)
                     if s.get("signature") == before),
                    None,
                )
                if idx is None:
                    return []
                all_sigs = all_sigs[idx + 1:]
            return all_sigs[:limit]

    indexer = PagingStubIndexer(
        das={}, signatures=sigs, txs=txs, transfers=[]
    )
    creator, method, note = resolve_creator(indexer, MINT, signature_limit=100)
    assert creator == DEV
    assert method == "creation_tx"
    assert note is not None


def test_resolve_creator_window_exhausted_reports_it():
    """When even 10 pages do not reach the creation, the note says
    the window was exhausted — it never invents a creator.
    """
    from src.deep_check import resolve_creator

    total = 1100  # more than 10 pages x 100
    sigs = [{"signature": f"sig-{i:04d}"} for i in range(total - 1, -1, -1)]

    class PagingStubIndexer(StubIndexer):
        def get_signatures(self, address, limit=50, before=None):
            all_sigs = self._signatures
            if before:
                idx = next(
                    (i for i, s in enumerate(all_sigs)
                     if s.get("signature") == before),
                    None,
                )
                if idx is None:
                    return []
                all_sigs = all_sigs[idx + 1:]
            return all_sigs[:limit]

    indexer = PagingStubIndexer(das={}, signatures=sigs, txs={}, transfers=[])
    creator, method, note = resolve_creator(indexer, MINT, signature_limit=100)
    assert creator is None
    assert method == "none"
    assert "no creation transaction found" in note


def _raw_create_tx(mint, dev, sig, block_time=1794400000, discriminator=None):
    """A pump.fun create transaction as the public RPC returns it:
    no parsed instruction — the create is raw base58 data starting
    with the create discriminator. ``discriminator`` defaults to
    v1; pass the v2 discriminator for a post-migration launch.
    """
    from src.dev_check import CREATE_DISCRIMINATOR

    disc = discriminator if discriminator is not None else CREATE_DISCRIMINATOR
    data = b58encode(disc + bytes(64))
    return {
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": dev}],
                "instructions": [{
                    "programId": PUMP_FUN_PROGRAM,
                    "data": data,
                    "accounts": [mint],
                }],
            },
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": []},
        "blockTime": block_time,
    }


def test_is_creation_tx_raw_discriminator():
    """The public RPC does not parse pump.fun instructions, so the
    creation check must match the raw create discriminator.
    """
    from src.deep_check import _is_creation_tx

    assert _is_creation_tx(_raw_create_tx(MINT, DEV, "sig-raw"), MINT) is True
    # Same shape, wrong discriminator, first account not the mint:
    # a buy, not a creation. (A real pump.fun buy's first account
    # is the global account, never the mint.)
    buy = _raw_create_tx(MINT, DEV, "sig-buy")
    buy["transaction"]["message"]["instructions"][0]["data"] = b58encode(
        bytes(8) + bytes(64)
    )
    buy["transaction"]["message"]["instructions"][0]["accounts"] = [OTHER, MINT]
    assert _is_creation_tx(buy, MINT) is False
    # Parsed shape still counts (Helius data).
    assert _is_creation_tx(_create_tx(MINT, DEV, "sig-parsed"), MINT) is True


def test_is_creation_tx_raw_create_v2():
    """Pump.fun migrated to create_v2 (BagsBack, Nov 12 2026):
    its discriminator must count as a creation, and buy_v2 must not.
    """
    from src.dev_check import CREATE_V2_DISCRIMINATOR
    from src.deep_check import _is_creation_tx

    v2 = _raw_create_tx(
        MINT, DEV, "sig-v2", discriminator=CREATE_V2_DISCRIMINATOR
    )
    assert _is_creation_tx(v2, MINT) is True
    buy_v2 = _raw_create_tx(MINT, DEV, "sig-buyv2")
    buy_v2["transaction"]["message"]["instructions"][0]["data"] = b58encode(
        bytes.fromhex("b817ee6167c5d33d") + bytes(16)
    )
    buy_v2["transaction"]["message"]["instructions"][0]["accounts"] = [OTHER, MINT]
    assert _is_creation_tx(buy_v2, MINT) is False


def test_resolve_creator_raw_creation_tx():
    """Creator resolution from a raw (unparsed) creation tx — the
    shape the public RPC actually returns for BagsBack.
    """
    from src.deep_check import resolve_creator

    txs = {"sig-create": _raw_create_tx(MINT, DEV, "sig-create")}
    indexer = StubIndexer(
        das={},
        signatures=[{"signature": "sig-later"}, {"signature": "sig-create"}],
        txs=txs,
    )
    creator, method, note = resolve_creator(indexer, MINT)
    assert creator == DEV
    assert method == "creation_tx"
    assert note is not None
