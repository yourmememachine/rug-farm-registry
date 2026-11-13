"""Real-v2 labelling rule tests — v2.13.

The v2.11 real-world validation proved the real-v1 rule too
strict: all 7 verified documented rugs were labelled "excluded".
v2 loosens ONLY the thresholds the verified numbers demand
(branch B liquidity $100 -> $2,500, volume $100 -> $500, age
7 -> 5 days) and TIGHTENS the branch A collapse proxy (-70% ->
-90%, aligning it with the registry's own >= 90% collapse
standard). The famous three (LIBRA, HAWK, CAR) are still
excluded — they hold real liquidity and market cap, and the
rule is not forced to catch them. The >= 2 rugged mints +
evidence flagging rule is unchanged and is pinned here too.

All data is synthetic / hardcoded from the verified Nov 13
numbers. No network.
"""
import json
from datetime import date

from src.labeller import COLLAPSE_RUG_PCT, MintEvent
from src.real_data import (
    LABELLING_RULE_VERSION,
    format_relabel_summary,
    label_token,
    labelling_rule,
    main,
    relabel_records,
    summarize,
)
from src.registry import (
    MIN_RUGGED_MINTS_TO_FLAG,
    is_flagged,
    validate_entry,
)

# -- The 7 verified documented rugs, exact Nov 13 observations ------
# (market cap, liquidity, 24h volume, 24h change, pair age days)
# from data/ml_real_dataset.json — the evidence base for v2.

LIBRA = "Bo9jh3wsmcC2AjakLWzNmKJ3SgtZmXEcSaW7L2FAvUsU"
HAWK = "HAWKThXRcNL9ZGZKqgUXLm4W8tnRZ7U6MVdEepSutj34"
CAR = "7oBYdEhV4GkXC19ZfgAvXpJWp2Rn9pm1Bx2cVNxFpump"
ABDUL = "G1evzzdxVXa7q3ypytdrEWstKdaS2rvDG8q76jDTpump"
FOMOGIVING = "C1kd6g65hLXLF2xzM6Wu3XueXEN88dqCRBCHsEKhpump"
LIQUID = "F3cCiTtB5hh7sf2BudYeXm45jPpF4FNbADRjje7MDfrF"
SER = "7uyptQEZRL5pz318S3oRjHW4iszo9thfd8nkU8ATpump"

DOCUMENTED_RUGS = {
    # Famous three: still excluded under v2 (real liquidity + MC).
    LIBRA: (2_881_254.0, 4_416.55, 20.01, None, 636.87, "excluded"),
    HAWK: (870_444.0, 149_238.90, 19.41, None, 708.86, "excluded"),
    CAR: (1_733_147.0, 201_204.44, 11.79, -1.17, 641.85, "excluded"),
    # Four small rugs: rugged under v2 branch B, excluded under v1.
    ABDUL: (1_797.0, 1_906.66, 0.09, -2.47, 5.67, "rugged"),
    FOMOGIVING: (1_696.0, 1_763.27, 60.24, -0.05, 5.79, "rugged"),
    LIQUID: (1_683.0, 1_775.44, 0.55, -2.29, 5.90, "rugged"),
    SER: (1_520.0, 1_606.14, 413.56, -8.21, 5.94, "rugged"),
}


def _obs(change=None, liquidity=None, volume=None, market_cap=None, age=None):
    return {
        "change_24h_pct": change,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "market_cap_usd": market_cap,
        "pair_age_days": age,
    }


# -- Branch A: collapse bar tightened -70% -> -90% --------------------

def test_branch_a_needs_a_90_percent_fall():
    base = dict(change=-92.0, liquidity=400.0, volume=200.0, age=3.0)
    assert label_token(_obs(**base)) == "rugged"
    # Exactly -90% still counts.
    assert label_token(_obs(**{**base, "change": -90.0})) == "rugged"
    # -89.9% does not: the collapse bar is 90%, not 70%.
    assert label_token(_obs(**{**base, "change": -89.9})) == "excluded"


def test_branch_a_v1_70_percent_collapse_is_now_excluded():
    # v1 labelled a -70% day with thin liquidity rugged. v2 does
    # not: a -70% day is ordinary meme volatility, and the rule
    # keeps collapse >= 90%. This tightening is deliberate.
    assert label_token(_obs(
        change=-70.0, liquidity=999.99, volume=999.99, age=1.0,
    )) == "excluded"
    assert label_token(_obs(
        change=-85.0, liquidity=400.0, volume=200.0, age=3.0,
    )) == "excluded"


# -- Branch B: the v2 loosening, threshold by threshold --------------

def test_branch_b_long_dead_new_thresholds():
    # The 4 small rugs sit just inside every new threshold.
    assert label_token(_obs(
        change=-2.47, liquidity=1_906.66, volume=0.09,
        market_cap=1_797.0, age=5.67,
    )) == "rugged"
    # Liquidity: $2,500 is the cap; $2,500.00 itself does not pass.
    assert label_token(_obs(
        change=-2.0, liquidity=2_499.99, volume=10.0,
        market_cap=2_000.0, age=30.0,
    )) == "rugged"
    assert label_token(_obs(
        change=-2.0, liquidity=2_500.0, volume=10.0,
        market_cap=2_000.0, age=30.0,
    )) == "excluded"
    # Volume: $500 is the cap; $500.00 itself does not pass.
    assert label_token(_obs(
        change=-8.21, liquidity=1_606.14, volume=499.99,
        market_cap=1_520.0, age=5.94,
    )) == "rugged"
    assert label_token(_obs(
        change=-8.21, liquidity=1_606.14, volume=500.0,
        market_cap=1_520.0, age=5.94,
    )) == "excluded"
    # Market cap: $5,000 is unchanged from v1.
    assert label_token(_obs(
        change=-2.0, liquidity=1_000.0, volume=10.0,
        market_cap=4_999.99, age=30.0,
    )) == "rugged"
    assert label_token(_obs(
        change=-2.0, liquidity=1_000.0, volume=10.0,
        market_cap=5_000.0, age=30.0,
    )) == "excluded"
    # Age: 5 days is the floor; 4.99 days does not pass.
    assert label_token(_obs(
        change=-2.0, liquidity=1_000.0, volume=10.0,
        market_cap=2_000.0, age=5.0,
    )) == "rugged"
    assert label_token(_obs(
        change=-2.0, liquidity=1_000.0, volume=10.0,
        market_cap=2_000.0, age=4.99,
    )) == "excluded"


def test_branch_b_needs_all_four_conditions():
    # Liquidity alone never labels a mint rugged: a live token
    # with a small pool but real trading is excluded.
    assert label_token(_obs(
        change=-2.0, liquidity=1_000.0, volume=50_000.0,
        market_cap=2_000.0, age=30.0,
    )) == "excluded"
    assert label_token(_obs(
        change=-2.0, liquidity=1_000.0, volume=10.0,
        market_cap=100_000.0, age=30.0,
    )) == "excluded"


# -- The 7 documented rugs, expected outcome per rug ------------------

def test_documented_rugs_expected_outcomes_under_v2():
    for mint, (mc, liq, vol, change, age, expected) in DOCUMENTED_RUGS.items():
        got = label_token(_obs(
            change=change, liquidity=liq, volume=vol,
            market_cap=mc, age=age,
        ))
        assert got == expected, f"{mint}: expected {expected}, got {got}"


def test_famous_three_still_excluded_because_of_real_liquidity_and_mc():
    # LIBRA holds $4.4k liquidity and a $2.9M market cap; HAWK
    # $149k / $870k; CAR $201k / $1.7M. No liquidity/market-cap
    # branch catches them, and v2 does not force the rule to.
    for mint in (LIBRA, HAWK, CAR):
        mc, liq, vol, change, age, expected = DOCUMENTED_RUGS[mint]
        assert expected == "excluded"
        assert liq >= 4_000.0
        assert mc >= 800_000.0


# -- Survived rule unchanged; missing data still never qualifies -----

def test_survived_rule_unchanged():
    assert label_token(_obs(
        change=12.0, liquidity=250_000.0, volume=800_000.0,
        market_cap=5_000_000.0, age=60.0,
    )) == "survived"
    rule = labelling_rule()
    assert rule["version"] == "real-v2"
    assert rule["survived"]["liquidity_usd_min"] == 10_000.0
    assert rule["survived"]["pair_age_days_min"] == 7.0
    assert rule["rugged_branch_b_long_dead"]["liquidity_usd_max"] == 2_500.0
    assert rule["rugged_branch_b_long_dead"]["volume_24h_usd_max"] == 500.0
    assert rule["rugged_branch_b_long_dead"]["market_cap_usd_max"] == 5_000.0
    assert rule["rugged_branch_b_long_dead"]["pair_age_days_min"] == 5.0


def test_missing_data_never_satisfies_v2_thresholds():
    # Branch B numbers all present except the age.
    assert label_token(_obs(
        change=-2.0, liquidity=1_000.0, volume=10.0,
        market_cap=2_000.0, age=None,
    )) == "excluded"
    # LIBRA's shape: no 24h change at all.
    assert label_token(_obs(
        change=None, liquidity=4_416.55, volume=20.01,
        market_cap=2_881_254.0, age=636.87,
    )) == "excluded"


# -- Re-labelling -------------------------------------------------------

def _v1_record(mint, symbol, outcome, mc, liq, vol, change, age):
    return {
        "mint": mint,
        "symbol": symbol,
        "outcome": outcome,
        "source": "dexscreener",
        "market_cap_usd": mc,
        "liquidity_usd": liq,
        "volume_24h_usd": vol,
        "change_24h_pct": change,
        "pair_age_days": age,
        "fetched_at": "2026-11-13T18:00:00+00:00",
        "labelling_rule_version": "real-v1",
        "synthetic": False,
    }


def test_relabel_records_updates_outcomes_and_version():
    before = [
        # Rugged under v1 branch A (-70%), excluded under v2.
        _v1_record("V1RUG11111111111111111111111111111111", "V1R",
                   "rugged", 8_000.0, 400.0, 200.0, -70.0, 3.0),
        # Excluded under v1 (liq $1,906 > $100), rugged under v2.
        _v1_record(ABDUL, "ABDUL", "excluded", 1_797.0, 1_906.66,
                   0.09, -2.47, 5.67),
        # Survived under both.
        _v1_record("V1OK111111111111111111111111111111111", "V1O",
                   "survived", 5_000_000.0, 250_000.0, 800_000.0,
                   12.0, 60.0),
    ]
    snapshot = json.loads(json.dumps(before))
    after = relabel_records(before)
    assert before == snapshot  # input not mutated
    by_mint = {r["mint"]: r for r in after}
    assert by_mint["V1RUG11111111111111111111111111111111"]["outcome"] == "excluded"
    assert by_mint[ABDUL]["outcome"] == "rugged"
    assert by_mint["V1OK111111111111111111111111111111111"]["outcome"] == "survived"
    for record in after:
        assert record["labelling_rule_version"] == "real-v2"
        assert record["labelling_rule"]["version"] == "real-v2"
        # Nothing else changes: same facts, same fetch time.
        assert record["fetched_at"] == "2026-11-13T18:00:00+00:00"
        assert record["source"] == "dexscreener"
    assert summarize(after)["n_rugged"] == 1
    assert summarize(after)["n_survived"] == 1
    assert summarize(after)["n_excluded"] == 1


def test_format_relabel_summary_lists_changed_mints():
    before = [
        _v1_record(ABDUL, "ABDUL", "excluded", 1_797.0, 1_906.66,
                   0.09, -2.47, 5.67),
        _v1_record("V1OK111111111111111111111111111111111", "V1O",
                   "survived", 5_000_000.0, 250_000.0, 800_000.0,
                   12.0, 60.0),
    ]
    after = relabel_records(before)
    text = format_relabel_summary(before, after)
    assert "0 rugged-like -> 1" in text
    assert "1 record(s) changed outcome" in text
    assert f"{ABDUL}: excluded -> rugged" in text


# -- Flagging rule unchanged (>= 2 rugged mints + evidence) -----------

DEV_WALLET = "7kGAXsa7n1qN2FuNoJAGmzebmN9KqqLAHcwj7gvoekKk"


def _entry(n_rugged):
    mints = [
        {
            "mint": mint,
            "outcome": "rugged",
            "peak_market_cap_usd": 100_000.0,
            "current_market_cap_usd": 1_000.0,
            "evidence": {"dexscreener": f"https://dexscreener.com/solana/{mint}"},
        }
        for mint in (LIBRA, HAWK)[:n_rugged]
    ]
    return {
        "wallet": DEV_WALLET,
        "cluster_id": "test-cluster",
        "status": "flagged",
        "first_seen": "2026-11-01",
        "last_seen": "2026-11-13",
        "ttl_days": 180,
        "rugged_mints": mints,
    }


def test_flagging_rule_still_needs_two_rugged_mints_with_evidence():
    assert MIN_RUGGED_MINTS_TO_FLAG == 2
    one = _entry(1)
    assert validate_entry(one) == [
        "flagged entries need >= 2 rugged mints, got 1"
    ]
    assert is_flagged(one, today=date(2026, 11, 13)) is False
    two = _entry(2)
    assert validate_entry(two) == []
    assert is_flagged(two, today=date(2026, 11, 13)) is True


def test_labeller_has_no_liquidity_threshold_to_align():
    # v2.13 checked src/labeller.py: it labels on collapse +
    # dev sell % + LP removal + time, with no liquidity input at
    # all, so the v1 liquidity flaw does not apply there and it
    # is unchanged. Its collapse bar stays at 90%.
    assert COLLAPSE_RUG_PCT == 90.0
    assert "liquidity_usd" not in MintEvent.__dataclass_fields__
    assert not any("liquid" in f for f in MintEvent.__dataclass_fields__)


# -- --relabel CLI ------------------------------------------------------

def test_cli_relabel_rewrites_dataset_with_v2(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    before = [
        _v1_record(ABDUL, "ABDUL", "excluded", 1_797.0, 1_906.66,
                   0.09, -2.47, 5.67),
        _v1_record("V1OK111111111111111111111111111111111", "V1O",
                   "survived", 5_000_000.0, 250_000.0, 800_000.0,
                   12.0, 60.0),
    ]
    # 12 labelled records are needed for exit 0; pad with survived.
    for i in range(10):
        before.append(_v1_record(
            f"V1PAD{i:02d}" + "1" * 30, f"P{i}", "survived",
            5_000_000.0, 250_000.0, 800_000.0, 12.0, 60.0,
        ))
    out.write_text(json.dumps(before), encoding="utf-8")
    code = main(["--relabel", "--out", str(out)])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 12
    by_mint = {r["mint"]: r for r in data}
    assert by_mint[ABDUL]["outcome"] == "rugged"
    assert all(r["labelling_rule_version"] == "real-v2" for r in data)
    summary = json.loads(
        (tmp_path / "dataset.summary.json").read_text(encoding="utf-8")
    )
    assert summary["labelling_rule_version"] == "real-v2"
    assert summary["summary"]["n_rugged"] == 1
    assert summary["summary"]["n_survived"] == 11
    captured = capsys.readouterr()
    assert "Re-labelled with rule real-v2" in captured.out
    assert f"{ABDUL}: excluded -> rugged" in captured.out


def test_cli_relabel_with_another_mode_exits_2(tmp_path, capsys):
    out = tmp_path / "dataset.json"
    out.write_text("[]", encoding="utf-8")
    assert main(["--relabel", "--fetch", "--out", str(out)]) == 2
    assert "exactly one" in capsys.readouterr().err


def test_cli_relabel_missing_file_exits_2(tmp_path, capsys):
    # An absent file is an empty dataset: relabel succeeds on
    # zero records but the < 10 labelled guard exits 2.
    assert main(["--relabel", "--out", str(tmp_path / "nope.json")]) == 2
    assert "too few usable tokens" in capsys.readouterr().err
