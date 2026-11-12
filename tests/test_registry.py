from datetime import date

from src.registry import (
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


def test_check_unknown_wallet():
    data = load_registry(REGISTRY)
    result = check_wallet(data, "Unknown111111111111111111111111111111", today=TODAY)
    assert result["flagged"] is False
    assert result["risk_level"] == "unknown"
