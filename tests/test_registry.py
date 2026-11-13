import copy
from datetime import date

from src.registry import (
    add_or_update_entry,
    check_wallet,
    is_expired,
    is_flagged,
    load_registry,
    validate_entry,
    validate_registry,
)

REGISTRY = "data/registry.json"
TODAY = date(2026, 11, 12)

FLAGGED = "SAMPLEDEV11111111111111111111111111111111"
UNDER_REVIEW = "SAMPLEDEV33333333333333333333333333333333"
EXPIRED = "SAMPLEDEV44444444444444444444444444444444"


def _sample_entry(**overrides):
    entry = {
        "wallet": FLAGGED,
        "cluster_id": "cluster-sample-a1",
        "status": "flagged",
        "first_seen": "2026-10-01",
        "last_seen": "2026-11-10",
        "ttl_days": 180,
        "rugged_mints": [
            {"mint": "SAMPLEMNT1111111111111111111111111111111",
             "outcome": "rugged", "peak_market_cap_usd": 100000,
             "current_market_cap_usd": 1000,
             "evidence": {"solscan_mint": "https://solscan.io/token/x"}},
            {"mint": "SAMPLEMNT2222222222222222222222222222222",
             "outcome": "rugged", "peak_market_cap_usd": 100000,
             "current_market_cap_usd": 1000,
             "evidence": {"solscan_mint": "https://solscan.io/token/y"}},
        ],
    }
    entry.update(overrides)
    return entry


def test_sample_registry_loads_and_validates():
    data = load_registry(REGISTRY)
    assert validate_registry(data) == []
    assert data["sample_data"] is True
    assert len(data["entries"]) == 4


def test_flagged_rule_requires_two_rugged_mints():
    one_rug = _sample_entry(rugged_mints=_sample_entry()["rugged_mints"][:1])
    assert validate_entry(one_rug)  # errors: <2 rugged mints for flagged
    assert not is_flagged(one_rug, TODAY)


def test_missing_evidence_rejected():
    no_evidence = _sample_entry()
    no_evidence["rugged_mints"][0] = {**no_evidence["rugged_mints"][0], "evidence": {}}
    assert validate_entry(no_evidence)
    assert not is_flagged(no_evidence, TODAY)


def test_ttl_expiry():
    data = load_registry(REGISTRY)
    expired = next(e for e in data["entries"] if e["wallet"] == EXPIRED)
    assert is_expired(expired, TODAY)
    assert not is_flagged(expired, TODAY)


def test_under_review_not_flagged():
    data = load_registry(REGISTRY)
    entry = next(e for e in data["entries"] if e["wallet"] == UNDER_REVIEW)
    assert not is_flagged(entry, TODAY)
    result = check_wallet(data, UNDER_REVIEW, today=TODAY)
    assert result["flagged"] is False
    assert result["risk_level"] == "watch"
    assert result["rugged_mint_count"] == 1


def test_check_flagged_wallet():
    data = load_registry(REGISTRY)
    result = check_wallet(data, FLAGGED, today=TODAY)
    assert result["flagged"] is True
    assert result["risk_level"] == "high"
    assert result["rugged_mint_count"] == 3
    assert result["evidence"]  # evidence is returned, not just a boolean


def test_check_flagged_wallet_with_known_mint():
    # v1.2: a mint that IS one of the dev's rugged mints is confirmed.
    data = load_registry(REGISTRY)
    result = check_wallet(
        data, FLAGGED,
        mint="SAMPLEMNT1111111111111111111111111111111", today=TODAY,
    )
    assert result["flagged"] is True
    assert result["mint_in_registry"] is True
    assert not any("not one of" in r for r in result["reasons"])


def test_check_flagged_wallet_with_unrelated_mint():
    # v1.2: a valid mint that is NOT one of the dev's rugged mints must
    # not be mislabelled as rugged — the verdict stays about the dev.
    data = load_registry(REGISTRY)
    result = check_wallet(
        data, FLAGGED, mint="UnknownMnt" + "1" * 22, today=TODAY,
    )
    assert result["flagged"] is True  # dev is still flagged
    assert result["risk_level"] == "high"
    assert result["mint_in_registry"] is False
    assert any("not one of" in r for r in result["reasons"])


def test_check_unknown_wallet():
    data = load_registry(REGISTRY)
    result = check_wallet(data, "Unknown111111111111111111111111111111", today=TODAY)
    assert result["flagged"] is False
    assert result["risk_level"] == "unknown"


def test_failed_merge_does_not_mutate_existing_entry():
    # v2.6: merging into an entry that stays invalid must leave the
    # existing entry untouched, even though the merge is rejected.
    # The existing entry is flagged with only 1 rugged mint (invalid);
    # the incoming entry adds a failed-but-honest mint, so the merged
    # entry is still flagged with 1 rugged mint and fails validation.
    existing = _sample_entry(
        status="flagged",
        rugged_mints=[_sample_entry()["rugged_mints"][0]],
    )
    incoming = _sample_entry(
        status="under_review",
        rugged_mints=[
            {"mint": "SAMPLEMNT3333333333333333333333333333333",
             "outcome": "failed_but_honest", "peak_market_cap_usd": 100000,
             "current_market_cap_usd": 20000,
             "evidence": {"solscan_mint": "https://solscan.io/token/z"}},
        ],
    )
    data = {"entries": [existing]}
    before = copy.deepcopy(existing)
    ok, message = add_or_update_entry(data, incoming)
    assert ok is False
    assert "merged entry invalid" in message
    assert existing == before
