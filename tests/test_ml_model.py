"""Tests for the ML rug-risk model (v2.8).

Synthetic data only, no network. The model is trained on the
clearly-labelled synthetic dataset (or on hand-built weights) —
never on real outcomes, which are too few to trust yet.
"""
import json
import math

import pytest

from src.dev_history import DevHistoryBuilder
from src.indexer import MintOutcome, TokenCreation
from src.ml_model import (
    FEATURE_NAMES,
    InsufficientDataError,
    RugRiskModel,
    evaluate_model,
    extract_features,
    features_from_mint_event,
    load_model,
    save_model,
    score_dev_history,
    synthetic_dataset,
    train_model,
    train_on_synthetic,
)
from src.ml_model import main as ml_main
from src.labeller import MintEvent


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _flat_model(bias: float, weights: list[float] | None = None) -> RugRiskModel:
    """A model with known weights, for controlled predictions."""
    return RugRiskModel(
        weights=list(weights) if weights is not None else [0.0] * len(FEATURE_NAMES),
        bias=bias,
        feature_means=[0.5] * len(FEATURE_NAMES),
        feature_stds=[1.0] * len(FEATURE_NAMES),
        n_train=0,
        source="synthetic",
    )


FULL_RECORD = {
    "collapse_pct": 95.0,
    "dev_sold_pct": 80.0,
    "lp_removed": True,
    "time_to_collapse_minutes": 30,
    "wash_score": 70.0,
    "top1_share": 0.4,
    "top10_share": 0.85,
    "holder_count": 120,
    "cluster_size": 4,
    "liquidity_usd": 5000.0,
}


# --- Feature extraction -------------------------------------------------

def test_extract_features_full_record():
    vec = extract_features(FULL_RECORD)
    assert vec.missing == []
    assert vec.present_count == len(FEATURE_NAMES)
    assert len(vec.values) == len(FEATURE_NAMES)
    assert vec.values[0] == pytest.approx(0.95)  # collapse
    assert vec.values[1] == pytest.approx(0.80)  # dev sold
    assert vec.values[2] == 1.0  # LP removed
    assert vec.values[4] == pytest.approx(0.70)  # wash
    assert vec.values[5] == pytest.approx(0.40)  # top1
    assert vec.values[8] == pytest.approx(0.40)  # cluster 4/10
    assert all(v is not None and 0.0 <= v <= 1.0 for v in vec.values)


def test_extract_features_missing_flagged_not_zero_filled():
    vec = extract_features({"collapse_pct": 90.0})
    assert vec.values[0] == pytest.approx(0.90)
    assert vec.values[1] is None
    assert vec.values[2] is None
    assert "dev_sold_pct" in vec.missing
    assert "lp_removed" in vec.missing
    assert "wash_score" in vec.missing
    assert len(vec.missing) == len(FEATURE_NAMES) - 1


def test_extract_features_shares_as_percent():
    vec = extract_features({"top1_share": 45, "top10_share": 85})
    assert vec.values[5] == pytest.approx(0.45)
    assert vec.values[6] == pytest.approx(0.85)


def test_extract_features_aliases():
    vec = extract_features({"collapse": 50.0, "dev_sell_pct": 10.0})
    assert vec.values[0] == pytest.approx(0.50)
    assert vec.values[1] == pytest.approx(0.10)


def test_features_from_mint_event():
    event = MintEvent(100000, 5000, lp_removed=True, dev_sold_pct=60,
                      time_to_collapse_minutes=120)
    vec = features_from_mint_event(
        event, {"wash_score": 55.0, "holder_count": 300}
    )
    assert vec.values[0] == pytest.approx(0.95)
    assert vec.values[2] == 1.0
    assert vec.values[4] == pytest.approx(0.55)
    assert "top1_share" in vec.missing


# --- Training -----------------------------------------------------------

def test_train_converges_and_beats_coin_flip():
    model, report = train_on_synthetic()
    assert report["loss_after"] < report["loss_before"]
    assert report["n_train"] > 0 and report["n_test"] > 0
    ev = report["evaluation"]
    assert ev["accuracy"] is not None and ev["accuracy"] >= 0.9
    assert ev["precision"] is not None and ev["precision"] >= 0.8
    assert ev["recall"] is not None and ev["recall"] >= 0.8
    assert report["source"] == "synthetic"
    assert model.source == "synthetic"


def test_train_on_outcome_records():
    model, report = train_model(synthetic_dataset(), source="outcomes")
    assert report["source"] == "outcomes"
    assert model.n_train == report["n_train"]
    # A clearly-rugged mint scores high, a clearly-honest one low.
    assert model.predict_dict(FULL_RECORD).probability > 0.7
    honest = dict(FULL_RECORD, collapse_pct=10.0, dev_sold_pct=2.0,
                  lp_removed=False, time_to_collapse_minutes=5000,
                  wash_score=5.0, top1_share=0.05, top10_share=0.2,
                  holder_count=5000, cluster_size=0, liquidity_usd=200000.0)
    assert model.predict_dict(honest).probability < 0.3


def test_insufficient_data_too_few():
    records = synthetic_dataset(n_rugged=2, n_survived=2)
    with pytest.raises(InsufficientDataError, match="Insufficient training data"):
        train_model(records)


def test_insufficient_data_single_class():
    records = [
        dict(r, outcome="rugged") for r in synthetic_dataset(n_rugged=12, n_survived=0)
    ]
    with pytest.raises(InsufficientDataError):
        train_model(records)


def test_unknown_outcomes_skipped():
    records = synthetic_dataset(n_rugged=10, n_survived=10)
    for r in records:
        r["outcome"] = "unknown"
    with pytest.raises(InsufficientDataError):
        train_model(records)


# --- Prediction ---------------------------------------------------------

def test_predict_bands():
    vec = extract_features(FULL_RECORD)
    assert _flat_model(_logit(0.20)).predict(vec).band == "low"
    assert _flat_model(_logit(0.50)).predict(vec).band == "medium"
    assert _flat_model(_logit(0.90)).predict(vec).band == "high"
    assert _flat_model(_logit(0.20)).predict(vec).probability == pytest.approx(0.2, abs=0.01)


def test_top_features_explain_the_score():
    weights = [0.0] * len(FEATURE_NAMES)
    weights[0] = 2.0  # collapse drives the score
    model = _flat_model(0.0, weights)
    pred = model.predict(extract_features(FULL_RECORD))
    assert pred.top_features[0]["feature"] == "collapse_pct"
    assert pred.top_features[0]["direction"] == "raises risk"
    assert len(pred.top_features) == 3
    assert all("feature" in t and "contribution" in t for t in pred.top_features)


def test_missing_features_contribute_zero_and_are_reported():
    weights = [0.0] * len(FEATURE_NAMES)
    weights[4] = 3.0  # wash score has a big weight...
    model = _flat_model(0.0, weights)
    vec = extract_features({"collapse_pct": 90.0})  # ...but wash is missing
    pred = model.predict(vec)
    assert "wash_score" in pred.missing_features
    # Missing features are imputed with the training mean, so their
    # standardised value is 0 and they cannot move the score.
    assert pred.probability == pytest.approx(0.5, abs=0.01)


def test_predict_dict_from_record():
    model, _ = train_on_synthetic()
    pred = model.predict_dict(FULL_RECORD)
    assert 0.0 <= pred.probability <= 1.0
    assert pred.band in ("low", "medium", "high")
    assert pred.missing_features == []


# --- Evaluation ---------------------------------------------------------

def test_evaluate_model_on_unseen_synthetic():
    model, _ = train_on_synthetic(seed=7)
    result = evaluate_model(model, synthetic_dataset(seed=99))
    assert result["n_labelled"] == 80
    assert result["accuracy"] is not None and result["accuracy"] >= 0.85


def test_evaluate_no_labelled_records():
    model, _ = train_on_synthetic()
    with pytest.raises(InsufficientDataError):
        evaluate_model(model, [{"mint": "x", "outcome": "unknown"}])


# --- Persistence --------------------------------------------------------

def test_persistence_round_trip(tmp_path):
    model, _ = train_on_synthetic()
    path = tmp_path / "model.json"
    save_model(path, model)
    loaded = load_model(path)
    assert loaded.weights == model.weights
    assert loaded.bias == model.bias
    assert loaded.source == "synthetic"
    vec = extract_features(FULL_RECORD)
    assert loaded.predict(vec).probability == pytest.approx(
        model.predict(vec).probability
    )


def test_save_refuses_registry(tmp_path):
    model, _ = train_on_synthetic()
    with pytest.raises(ValueError, match="registry.json"):
        save_model(tmp_path / "registry.json", model)


def test_load_bad_json(tmp_path):
    path = tmp_path / "model.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_model(path)


def test_load_wrong_format(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"format": "something-else", "version": 1}))
    with pytest.raises(ValueError, match="format"):
        load_model(path)


# --- Synthetic data -----------------------------------------------------

def test_synthetic_dataset_is_labelled_synthetic():
    records = synthetic_dataset()
    assert len(records) == 80
    assert all(r.get("synthetic") is True for r in records)
    assert sum(1 for r in records if r["outcome"] == "rugged") == 40
    assert sum(1 for r in records if r["outcome"] == "survived") == 40


# --- CLI ----------------------------------------------------------------

def test_cli_demo_trains_and_saves(tmp_path):
    path = tmp_path / "model.json"
    assert ml_main(["--demo", "--model", str(path)]) == 0
    assert path.is_file()
    assert load_model(path).source == "synthetic"


def test_cli_predict(tmp_path, capsys):
    model_path = tmp_path / "model.json"
    assert ml_main(["--demo", "--model", str(model_path)]) == 0
    mint_path = tmp_path / "mint.json"
    mint_path.write_text(json.dumps(FULL_RECORD))
    assert ml_main(["--predict", str(mint_path), "--model", str(model_path)]) == 0
    out = capsys.readouterr().out
    assert "Rug risk:" in out
    assert "Top contributors:" in out


def test_cli_predict_needs_model(tmp_path):
    mint_path = tmp_path / "mint.json"
    mint_path.write_text(json.dumps(FULL_RECORD))
    assert ml_main(["--predict", str(mint_path)]) == 2


def test_cli_train_insufficient_data(tmp_path):
    outcomes = tmp_path / "outcomes.json"
    outcomes.write_text(json.dumps(synthetic_dataset(n_rugged=2, n_survived=2)))
    assert ml_main(["--train", str(outcomes), "--model", str(tmp_path / "m.json")]) == 2


def test_cli_train_missing_file(tmp_path):
    assert ml_main(["--train", str(tmp_path / "nope.json"), "--model", str(tmp_path / "m.json")]) == 2


def test_cli_evaluate(tmp_path, capsys):
    model_path = tmp_path / "model.json"
    assert ml_main(["--demo", "--model", str(model_path)]) == 0
    data_path = tmp_path / "outcomes.json"
    data_path.write_text(json.dumps(synthetic_dataset(seed=123)))
    assert ml_main(["--evaluate", "--model", str(model_path), "--data", str(data_path)]) == 0
    assert "accuracy" in capsys.readouterr().out


def test_cli_no_mode():
    assert ml_main([]) == 2


# --- Dev-history integration (advisory only) ----------------------------

def _history_with_model():
    """A flagged history (2 rugged mints) + a trained model."""
    dev = "DevWa11et11111111111111111111111111111"
    creations = [
        TokenCreation(mint=f"Mint{i}1111111111111111111111111111111",
                      dev_wallet=dev, program="pump.fun")
        for i in (1, 2)
    ]
    outcomes = {
        c.mint: MintOutcome(
            mint=c.mint, dev_wallet=dev,
            peak_market_cap_usd=100000, current_market_cap_usd=2000,
            lp_removed=True, dev_sold_pct=80, time_to_collapse_minutes=60,
            evidence={"solscan_mint": f"https://solscan.io/token/{c.mint}"},
        )
        for c in creations
    }
    model, _ = train_on_synthetic()
    builder = DevHistoryBuilder(client=None)
    history = builder.build(dev, creations=creations, outcomes=outcomes, ml_model=model)
    return history


def test_dev_history_ml_score_attached_but_verdict_unchanged():
    history = _history_with_model()
    assert history.verdict == "flagged"  # rule verdict, untouched by ML
    assert history.ml_score is not None
    assert history.ml_score["advisory_only"] is True
    assert history.ml_score["band"] == "high"
    assert history.ml_score["max_probability"] > 0.7
    assert len(history.ml_score["per_mint"]) == 2
    assert history.ml_score["per_mint"][0]["missing_features"]  # wash/holders missing
    assert "ml_score" in history.to_dict()
    assert "ML risk" in history.to_text()


def test_dev_history_without_model_has_no_ml_score():
    dev = "DevWa11et11111111111111111111111111111"
    creations = [
        TokenCreation(mint="Mint111111111111111111111111111111111",
                      dev_wallet=dev, program="pump.fun")
    ]
    outcomes = {
        creations[0].mint: MintOutcome(
            mint=creations[0].mint, dev_wallet=dev,
            peak_market_cap_usd=100000, current_market_cap_usd=90000,
            evidence={"solscan_mint": "https://solscan.io/token/x"},
        )
    }
    history = DevHistoryBuilder(client=None).build(
        dev, creations=creations, outcomes=outcomes
    )
    assert history.verdict == "clean"
    assert history.ml_score is None
    assert "ML risk" not in history.to_text()


def test_score_dev_history_flags_missing_features():
    model, _ = train_on_synthetic()
    history = _history_with_model()
    # Re-score directly: history mints have no wash/holder data.
    scored = score_dev_history(history, model)
    assert scored["per_mint"][0]["missing_features"]
    assert "wash_score" in scored["per_mint"][0]["missing_features"]
