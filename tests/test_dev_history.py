"""Dev-history tests — all data is synthetic and caller-supplied.
No network is ever made here, and no real API key is used."""

import json

import pytest

from src import dev_history
from src.dev_history import (
    DevHistory,
    DevHistoryBuilder,
    MintRecord,
    evidence_for,
    main,
    record_from_outcome,
)
from src.indexer import PUMP_FUN_PROGRAM, MintOutcome, TokenCreation
from src.registry import validate_entry

DEV = "RUGDEV111111111111111111111111111111111"
OTHER_DEV = "RUGDEV222222222222222222222222222222222"
MINT1 = "RUGMNT111111111111111111111111111111111"
MINT2 = "RUGMNT222222222222222222222222222222222"
MINT3 = "RUGMNT333333333333333333333333333333333"
MINT4 = "RUGMNT444444444444444444444444444444444"


def _creation(mint, dev=DEV, sig=None, timestamp="2026-11-10T12:00:00+00:00"):
    return TokenCreation(
        mint=mint, dev_wallet=dev, program="pump.fun",
        timestamp=timestamp, signature=sig,
    )


def _rugged_outcome(mint, dev=DEV, peak=1_000_000.0, current=50_000.0):
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


def _honest_outcome(mint, dev=DEV):
    """75% collapse, no LP removal -> 'failed_but_honest'."""
    return MintOutcome(
        mint=mint,
        dev_wallet=dev,
        peak_market_cap_usd=1_000_000.0,
        current_market_cap_usd=250_000.0,
        lp_removed=False,
        dev_sold_pct=0.0,
        time_to_collapse_minutes=60,
    )


def _no_pair_outcome(mint, dev=DEV):
    """No DexScreener pair: all market-cap numbers missing."""
    return MintOutcome(mint=mint, dev_wallet=dev)


class StubClient:
    """Stub HeliusClient: enabled flag + canned signatures/txs/outcomes."""

    def __init__(self, enabled=True, signatures=None, txs=None, outcomes=None):
        self.enabled = enabled
        self._signatures = signatures or []
        self._txs = txs or {}
        self._outcomes = outcomes or {}
        self.calls = []

    def get_signatures(self, address, limit=50):
        self.calls.append(("get_signatures", address, limit))
        return self._signatures[:limit]

    def get_transaction(self, signature):
        self.calls.append(("get_transaction", signature))
        return self._txs.get(signature)

    def fetch_mint_outcome(self, mint, dev_wallet=None):
        self.calls.append(("fetch_mint_outcome", mint))
        if mint not in self._outcomes:
            raise RuntimeError(f"no stub outcome for {mint}")
        return self._outcomes[mint]


def _tx(mint, dev, sig, block_time=1763000000):
    """Minimal Helius jsonParsed transaction with a pump.fun create."""
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


def _builder(registry_data=None):
    # Disabled client: supplied-data builds must never need a key.
    return DevHistoryBuilder(StubClient(enabled=False), registry_data or {})


def _history(creations, outcomes, registry_data=None):
    return _builder(registry_data).build(
        DEV, creations=creations, outcomes=outcomes
    )


# --- History building from supplied data --------------------------------

def test_two_rugged_mints_is_flagged():
    history = _history(
        [_creation(MINT1), _creation(MINT2)],
        {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)},
    )
    assert history.verdict == "flagged"
    assert history.rugged_mint_count == 2
    assert history.checked == 2


def test_one_rugged_mint_is_under_review():
    history = _history(
        [_creation(MINT1), _creation(MINT2)],
        {MINT1: _rugged_outcome(MINT1), MINT2: _honest_outcome(MINT2)},
    )
    assert history.verdict == "under_review"
    assert history.rugged_mint_count == 1
    assert history.draft_entry() is None


def test_zero_rugged_with_real_data_is_clean():
    history = _history(
        [_creation(MINT1), _creation(MINT2)],
        {MINT1: _honest_outcome(MINT1), MINT2: _honest_outcome(MINT2)},
    )
    assert history.verdict == "clean"
    assert history.rugged_mint_count == 0
    assert history.draft_entry() is None


def test_no_mints_is_insufficient_data_not_clean():
    history = _history([], {})
    assert history.verdict == "insufficient_data"
    assert history.checked == 0


def test_all_mints_without_data_is_insufficient_data():
    history = _history(
        [_creation(MINT1), _creation(MINT2)],
        {MINT1: _no_pair_outcome(MINT1), MINT2: _no_pair_outcome(MINT2)},
    )
    assert history.verdict == "insufficient_data"
    assert history.mints[0].outcome == "insufficient_data"
    assert history.mints[0].data_status == "insufficient_data"


def test_mixed_rugged_and_missing_data():
    history = _history(
        [_creation(MINT1), _creation(MINT2)],
        {MINT1: _rugged_outcome(MINT1), MINT2: _no_pair_outcome(MINT2)},
    )
    assert history.verdict == "under_review"
    assert history.counts_by_outcome() == {"rugged": 1, "insufficient_data": 1}


def test_failed_but_honest_never_counts_as_rugged():
    history = _history(
        [_creation(MINT1), _creation(MINT2), _creation(MINT3)],
        {
            MINT1: _honest_outcome(MINT1),
            MINT2: _honest_outcome(MINT2),
            MINT3: _honest_outcome(MINT3),
        },
    )
    assert history.verdict == "clean"
    assert history.rugged_mint_count == 0


def test_duplicate_mint_counted_once():
    history = _history(
        [_creation(MINT1), _creation(MINT1), _creation(MINT2)],
        {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)},
    )
    assert history.checked == 2
    assert history.rugged_mint_count == 2


def test_creations_from_other_devs_are_filtered_out():
    history = _history(
        [_creation(MINT1), _creation(MINT2, dev=OTHER_DEV)],
        {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2, dev=OTHER_DEV)},
    )
    assert history.checked == 1
    assert history.verdict == "under_review"


def test_outcome_fetch_failure_is_insufficient_data():
    client = StubClient(enabled=True, outcomes={})
    builder = DevHistoryBuilder(client)
    history = builder.build(
        DEV, creations=[_creation(MINT1)], outcomes=None
    )
    assert history.mints[0].outcome == "insufficient_data"
    assert history.verdict == "insufficient_data"


def test_missing_outcome_in_supplied_map_is_insufficient_data():
    history = _history([_creation(MINT1)], {})
    assert history.mints[0].outcome == "insufficient_data"


def test_invalid_wallet_raises():
    with pytest.raises(ValueError, match="not a valid Solana address"):
        _builder().build("not-an-address", creations=[])


def test_limit_below_one_raises():
    with pytest.raises(ValueError, match="limit must be >= 1"):
        _builder().build(DEV, limit=0, creations=[])


def test_build_without_key_and_without_supplied_data_raises():
    builder = DevHistoryBuilder(StubClient(enabled=False))
    with pytest.raises(RuntimeError, match="HELIUS_API_KEY is not set"):
        builder.build(DEV)


# --- Live fetch path (stubbed signatures/transactions) -------------------

def test_fetch_path_parses_signatures_into_creations():
    client = StubClient(
        enabled=True,
        signatures=[{"signature": "sig1"}, {"signature": "sig2"}],
        txs={"sig1": _tx(MINT1, DEV, "sig1"),
             "sig2": _tx(MINT2, DEV, "sig2")},
        outcomes={MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)},
    )
    history = DevHistoryBuilder(client).build(DEV, limit=10)
    assert history.verdict == "flagged"
    assert history.checked == 2
    assert ("get_signatures", DEV, 10) in client.calls


def test_fetch_path_skips_failed_transaction_fetch():
    client = StubClient(
        enabled=True,
        signatures=[{"signature": "sig1"}, {"signature": "sig2"}],
        txs={"sig1": _tx(MINT1, DEV, "sig1")},  # sig2 missing -> None
        outcomes={MINT1: _rugged_outcome(MINT1)},
    )
    history = DevHistoryBuilder(client).build(DEV, limit=10)
    assert history.checked == 1
    assert history.verdict == "under_review"


# --- Registry merge ------------------------------------------------------

def _registry_entry(wallet, mints):
    return {
        "wallet": wallet,
        "cluster_id": "cluster-test",
        "status": "flagged" if len(mints) >= 2 else "under_review",
        "first_seen": "2026-10-01",
        "last_seen": "2026-11-10",
        "ttl_days": 180,
        "rugged_mints": mints,
    }


def _registry_mint(mint):
    return {
        "mint": mint,
        "outcome": "rugged",
        "peak_market_cap_usd": 500000,
        "current_market_cap_usd": 10000,
        "evidence": {"solscan_mint": f"https://solscan.io/token/{mint}"},
    }


def test_registry_rugged_mint_counts_toward_flag():
    registry = {"entries": [_registry_entry(DEV, [_registry_mint(MINT3)])]}
    history = _history(
        [_creation(MINT1)],
        {MINT1: _rugged_outcome(MINT1)},
        registry_data=registry,
    )
    assert history.verdict == "flagged"
    assert history.rugged_mint_count == 2
    assert history.mints[-1].source == "registry"


def test_registry_merge_dedupes_same_mint():
    registry = {"entries": [_registry_entry(DEV, [_registry_mint(MINT1)])]}
    history = _history(
        [_creation(MINT1)],
        {MINT1: _rugged_outcome(MINT1)},
        registry_data=registry,
    )
    assert history.checked == 1
    assert history.rugged_mint_count == 1


# --- Draft entry ---------------------------------------------------------

def test_draft_entry_valid_only_when_flagged():
    history = _history(
        [_creation(MINT1, timestamp="2026-11-01T10:00:00+00:00"),
         _creation(MINT2, timestamp="2026-11-10T10:00:00+00:00")],
        {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)},
    )
    draft = history.draft_entry()
    assert draft is not None
    assert validate_entry(draft) == []
    assert draft["wallet"] == DEV
    assert draft["status"] == "flagged"
    assert draft["first_seen"] == "2026-11-01"
    assert draft["last_seen"] == "2026-11-10"
    assert len(draft["rugged_mints"]) == 2
    assert all(m["evidence"] for m in draft["rugged_mints"])


def test_draft_entry_excludes_non_rugged_mints():
    history = _history(
        [_creation(MINT1), _creation(MINT2), _creation(MINT3)],
        {
            MINT1: _rugged_outcome(MINT1),
            MINT2: _rugged_outcome(MINT2),
            MINT3: _honest_outcome(MINT3),
        },
    )
    draft = history.draft_entry()
    assert draft is not None
    assert [m["mint"] for m in draft["rugged_mints"]] == [MINT1, MINT2]


# --- Rendering -----------------------------------------------------------

def test_to_dict_round_trips_through_json():
    history = _history(
        [_creation(MINT1), _creation(MINT2)],
        {MINT1: _rugged_outcome(MINT1), MINT2: _honest_outcome(MINT2)},
    )
    data = json.loads(json.dumps(history.to_dict()))
    assert data["dev_wallet"] == DEV
    assert data["verdict"] == "under_review"
    assert data["draft_entry"] is None
    assert data["mints"][0]["evidence"]["dexscreener"].endswith(MINT1)


def test_to_text_contains_verdict_mints_and_evidence():
    history = _history(
        [_creation(MINT1)],
        {MINT1: _rugged_outcome(MINT1)},
    )
    text = history.to_text()
    assert "UNDER_REVIEW" in text
    assert MINT1 in text
    assert "dexscreener.com" in text
    assert "no market data" not in text


def test_record_from_outcome_without_pair_is_insufficient():
    record = record_from_outcome(_creation(MINT1), _no_pair_outcome(MINT1))
    assert record.outcome == "insufficient_data"
    assert record.data_status == "insufficient_data"
    assert record.has_evidence  # links are still attached, numbers are not


def test_evidence_for_builds_all_links():
    evidence = evidence_for(MINT1, DEV, signature="sig9")
    assert evidence["solscan_mint"] == f"https://solscan.io/token/{MINT1}"
    assert evidence["dexscreener"] == f"https://dexscreener.com/solana/{MINT1}"
    assert evidence["solscan_wallet"] == f"https://solscan.io/account/{DEV}"
    assert evidence["helius_tx"] == "https://explorer.helius.xyz/tx/sig9"


# --- CLI exit codes ------------------------------------------------------

def test_cli_missing_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert main(["--wallet", DEV]) == 2
    assert "HELIUS_API_KEY is not set" in capsys.readouterr().err


def test_cli_invalid_wallet_exits_2(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "TESTKEY123")
    assert main(["--wallet", "nope"]) == 2


def test_cli_limit_zero_exits_2(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "TESTKEY123")
    assert main(["--wallet", DEV, "--limit", "0"]) == 2


def _stub_helius_client(monkeypatch, outcomes):
    """Point the module's HeliusClient at a stub with canned data."""

    class _StubHeliusClient:
        def __init__(self):
            self.enabled = True

        def get_signatures(self, address, limit=50):
            return [{"signature": "sig1"}, {"signature": "sig2"}][:limit]

        def get_transaction(self, signature):
            mint = MINT1 if signature == "sig1" else MINT2
            return _tx(mint, DEV, signature)

        def fetch_mint_outcome(self, mint, dev_wallet=None):
            return outcomes[mint]

    monkeypatch.setattr(dev_history, "HeliusClient", _StubHeliusClient)
    monkeypatch.setattr(
        dev_history, "DEFAULT_REGISTRY_PATH", "/nonexistent/registry.json"
    )


def test_cli_flagged_exits_1(monkeypatch, capsys):
    _stub_helius_client(
        monkeypatch,
        {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)},
    )
    assert main(["--wallet", DEV]) == 1
    assert "FLAGGED" in capsys.readouterr().out


def test_cli_clean_exits_0(monkeypatch, capsys):
    _stub_helius_client(
        monkeypatch,
        {MINT1: _honest_outcome(MINT1), MINT2: _honest_outcome(MINT2)},
    )
    assert main(["--wallet", DEV]) == 0
    assert "CLEAN" in capsys.readouterr().out


def test_cli_json_prints_valid_json(monkeypatch, capsys):
    _stub_helius_client(
        monkeypatch,
        {MINT1: _rugged_outcome(MINT1), MINT2: _rugged_outcome(MINT2)},
    )
    assert main(["--wallet", DEV, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "flagged"
    assert data["draft_entry"] is not None
    assert validate_entry(data["draft_entry"]) == []
