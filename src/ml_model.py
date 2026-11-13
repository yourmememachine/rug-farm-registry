"""ML rug-risk model — v2.8.

The registry's verdicts are hand-written rules, and they stay that
way: the >= 2 rugged mints + evidence rule is the ONLY flagging
path, and this model never flags a wallet on its own. What this
module adds is a *learned* advisory score next to the rule verdict:
a logistic-regression rug-risk model, implemented from scratch in
stdlib only (no sklearn, no numpy — the project is stdlib + httpx).

    Feature extraction — one fixed 10-feature vector per mint,
        built from a mint event + the advanced-detection outputs
        (wash score, holder concentration, stealth-cluster size).
        A missing feature is flagged as missing, never silently
        zero-filled as if it were real: the model imputes it with
        the training mean (so it contributes exactly 0 to the
        log-odds) and reports it in the prediction.
    Logistic regression — gradient descent with L2 regularisation,
        trained on OutcomeTracker JSON records (verdict + later
        outcome) or on a small synthetic labelled dataset that
        ships ONLY for tests/demo and is clearly labelled
        synthetic.
    predict_risk — probability + band (low / medium / high) +
        the top contributing features, so a human can see WHY
        the score is what it is. No black box.
    Evaluation — accuracy / precision / recall on a held-out
        split. Too little training data -> "insufficient training
        data", never a pretence that the model is good.
    Persistence — weights + feature stats to a caller-given JSON
        file; never writes to the registry.

Run:
    python -m src.ml_model --train outcomes.json --model model.json
    python -m src.ml_model --demo --model model.json
    python -m src.ml_model --predict mint.json --model model.json [--json]
    python -m src.ml_model --evaluate --model model.json --data outcomes.json
    # Exit 0 = ok, 2 = invalid args / insufficient data / bad file.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .advanced import load_outcomes
from .labeller import MintEvent, collapse_pct

EXIT_OK = 0
EXIT_INVALID = 2

MODEL_FORMAT = "rugfarm-ml-model"
MODEL_VERSION = 1

# Bands on the predicted rug probability.
LOW_MAX = 0.33
HIGH_MIN = 0.67

# Training guardrails: below this, the model is not worth trusting.
MIN_TRAINING_SAMPLES = 10
MIN_PER_CLASS = 2

# Gradient-descent defaults (deterministic — no random init).
LEARNING_RATE = 0.1
EPOCHS = 600
L2_LAMBDA = 0.01
HOLDOUT_FRACTION = 0.2
SPLIT_SEED = 42

FEATURE_NAMES = (
    "collapse_pct",
    "dev_sold_pct",
    "lp_removed",
    "time_to_collapse_minutes",
    "wash_score",
    "top1_share",
    "top10_share",
    "holder_count",
    "cluster_size",
    "liquidity_usd",
)


class InsufficientDataError(ValueError):
    """Raised when there is not enough labelled data to train honestly."""


# ======================================================================
# Feature extraction
# ======================================================================
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _log_scale(x: float, cap: float) -> float:
    """log10(x + 1) scaled so x == cap maps to 1.0. x < 0 -> 0.0."""
    if x is None or (isinstance(x, float) and math.isnan(x)) or x < 0:
        return 0.0
    return _clamp01(math.log10(x + 1.0) / math.log10(cap + 1.0))


def _share(x: float) -> float:
    """A holder share: 0-1 as a fraction, > 1 treated as a percent."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return _clamp01(x / 100.0 if x > 1.0 else float(x))


def _number(value: object) -> float | None:
    """A finite number, or None when the value is missing / not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if math.isnan(value) else float(value)
    return None


@dataclass
class FeatureVector:
    """One mint's features, normalised to ~0-1.

    ``values`` holds a normalised float per feature, or None when
    that feature was missing — missing is explicit, never silently
    zero-filled as if it were real. ``missing`` names the missing
    features.
    """

    values: list[float | None]
    missing: list[str] = field(default_factory=list)

    @property
    def present_count(self) -> int:
        return sum(1 for v in self.values if v is not None)

    def to_dict(self) -> dict:
        return {
            "features": {
                name: value
                for name, value in zip(FEATURE_NAMES, self.values)
            },
            "missing": list(self.missing),
        }


def extract_features(data: dict) -> FeatureVector:
    """Build the fixed 10-feature vector from a mint record dict.

    Accepted keys (all optional — absence is recorded as missing):
    collapse_pct (or "collapse"), dev_sold_pct (or "dev_sell_pct"),
    lp_removed, time_to_collapse_minutes (or "time_to_collapse"),
    wash_score, top1_share, top10_share, holder_count,
    cluster_size, liquidity_usd. Shares may be 0-1 or percent.
    """
    raw: dict[str, float | None] = {}

    collapse = _number(data.get("collapse_pct", data.get("collapse")))
    raw["collapse_pct"] = (
        _clamp01(collapse / 100.0) if collapse is not None else None
    )

    dev_sold = _number(data.get("dev_sold_pct", data.get("dev_sell_pct")))
    raw["dev_sold_pct"] = (
        _clamp01(dev_sold / 100.0) if dev_sold is not None else None
    )

    lp = data.get("lp_removed")
    raw["lp_removed"] = (
        None if lp is None else (1.0 if bool(lp) else 0.0)
    )

    minutes = _number(
        data.get("time_to_collapse_minutes", data.get("time_to_collapse"))
    )
    raw["time_to_collapse_minutes"] = (
        _log_scale(minutes, 10080.0) if minutes is not None else None
    )

    wash = _number(data.get("wash_score"))
    raw["wash_score"] = _clamp01(wash / 100.0) if wash is not None else None

    top1 = _number(data.get("top1_share"))
    raw["top1_share"] = _share(top1) if top1 is not None else None

    top10 = _number(data.get("top10_share"))
    raw["top10_share"] = _share(top10) if top10 is not None else None

    holders = _number(data.get("holder_count"))
    raw["holder_count"] = (
        _log_scale(holders, 100000.0) if holders is not None else None
    )

    cluster = _number(data.get("cluster_size"))
    raw["cluster_size"] = (
        _clamp01(cluster / 10.0) if cluster is not None else None
    )

    liquidity = _number(data.get("liquidity_usd"))
    raw["liquidity_usd"] = (
        _log_scale(liquidity, 10_000_000.0) if liquidity is not None else None
    )

    values = [raw[name] for name in FEATURE_NAMES]
    missing = [name for name, v in zip(FEATURE_NAMES, values) if v is None]
    return FeatureVector(values=values, missing=missing)


def features_from_mint_event(
    event: MintEvent, extras: dict | None = None
) -> FeatureVector:
    """Feature vector from a labeller MintEvent + optional extras.

    ``extras`` carries the advanced-detection outputs (wash_score,
    top1_share, top10_share, holder_count, cluster_size,
    liquidity_usd) when they are known.
    """
    data: dict = dict(extras or {})
    data["collapse_pct"] = collapse_pct(event)
    data["dev_sold_pct"] = event.dev_sold_pct
    data["lp_removed"] = event.lp_removed
    data["time_to_collapse_minutes"] = event.time_to_collapse_minutes
    return extract_features(data)


# ======================================================================
# Logistic regression (from scratch)
# ======================================================================
def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _band(probability: float) -> str:
    if probability < LOW_MAX:
        return "low"
    if probability < HIGH_MIN:
        return "medium"
    return "high"


@dataclass
class RiskPrediction:
    probability: float
    band: str  # low | medium | high
    top_features: list[dict] = field(default_factory=list)
    missing_features: list[str] = field(default_factory=list)
    log_odds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "probability": round(self.probability, 4),
            "band": self.band,
            "log_odds": round(self.log_odds, 4),
            "top_features": self.top_features,
            "missing_features": list(self.missing_features),
        }


@dataclass
class RugRiskModel:
    """A trained logistic-regression rug-risk model.

    ``feature_means`` / ``feature_stds`` are the training-set stats
    of the normalised features; a missing feature is imputed with
    its mean, so after standardisation it contributes exactly 0 to
    the log-odds — and is still reported as missing.
    """

    weights: list[float]
    bias: float
    feature_means: list[float]
    feature_stds: list[float]
    feature_names: list[str] = field(
        default_factory=lambda: list(FEATURE_NAMES)
    )
    n_train: int = 0
    source: str = "outcomes"  # "outcomes" | "synthetic"
    evaluation: dict | None = None

    def _standardize(self, vector: FeatureVector) -> list[float]:
        xs: list[float] = []
        for i, value in enumerate(vector.values):
            raw = self.feature_means[i] if value is None else value
            std = self.feature_stds[i] if self.feature_stds[i] else 1.0
            xs.append((raw - self.feature_means[i]) / std)
        return xs

    def predict(self, vector: FeatureVector) -> RiskPrediction:
        xs = self._standardize(vector)
        log_odds = self.bias + sum(
            w * x for w, x in zip(self.weights, xs)
        )
        probability = _sigmoid(log_odds)
        contributions = [
            {
                "feature": name,
                "value": vector.values[i],
                "contribution": round(w * xs[i], 4),
                "direction": (
                    "raises risk"
                    if w * xs[i] > 0
                    else ("lowers risk" if w * xs[i] < 0 else "no effect")
                ),
                "missing": vector.values[i] is None,
            }
            for i, (name, w) in enumerate(zip(self.feature_names, self.weights))
        ]
        top = sorted(
            contributions, key=lambda c: abs(c["contribution"]), reverse=True
        )[:3]
        return RiskPrediction(
            probability=probability,
            band=_band(probability),
            top_features=top,
            missing_features=list(vector.missing),
            log_odds=log_odds,
        )

    def predict_dict(self, data: dict) -> RiskPrediction:
        """Predict straight from a mint record dict."""
        return self.predict(extract_features(data))

    def to_dict(self) -> dict:
        return {
            "format": MODEL_FORMAT,
            "version": MODEL_VERSION,
            "source": self.source,
            "feature_names": list(self.feature_names),
            "weights": list(self.weights),
            "bias": self.bias,
            "feature_means": list(self.feature_means),
            "feature_stds": list(self.feature_stds),
            "n_train": self.n_train,
            "evaluation": self.evaluation,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RugRiskModel":
        if not isinstance(data, dict):
            raise ValueError("Model file must hold a JSON object.")
        if data.get("format") != MODEL_FORMAT:
            raise ValueError(
                f"Model file format must be {MODEL_FORMAT!r}, "
                f"got {data.get('format')!r}."
            )
        if data.get("version") != MODEL_VERSION:
            raise ValueError(
                f"Model version must be {MODEL_VERSION}, "
                f"got {data.get('version')!r}."
            )
        names = data.get("feature_names") or list(FEATURE_NAMES)
        weights = data.get("weights")
        means = data.get("feature_means")
        stds = data.get("feature_stds")
        if (
            not isinstance(weights, list)
            or len(weights) != len(names)
            or not isinstance(means, list)
            or len(means) != len(names)
            or not isinstance(stds, list)
            or len(stds) != len(names)
        ):
            raise ValueError(
                "Model file is missing weights / feature_means / "
                "feature_stds, or their lengths do not match."
            )
        return cls(
            weights=[float(w) for w in weights],
            bias=float(data.get("bias", 0.0)),
            feature_means=[float(m) for m in means],
            feature_stds=[float(s) for s in stds],
            feature_names=list(names),
            n_train=int(data.get("n_train", 0)),
            source=str(data.get("source", "outcomes")),
            evaluation=data.get("evaluation"),
        )


def _loss_and_gradients(
    weights: list[float],
    bias: float,
    xs_batch: list[list[float]],
    ys: list[int],
    l2: float,
) -> tuple[float, list[float], float]:
    """Mean binary cross-entropy + L2, and its gradients."""
    n = len(ys)
    grad_w = [0.0] * len(weights)
    grad_b = 0.0
    loss = 0.0
    for xs, y in zip(xs_batch, ys):
        z = bias + sum(w * x for w, x in zip(weights, xs))
        p = _sigmoid(z)
        p = min(max(p, 1e-12), 1.0 - 1e-12)
        loss += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        err = p - y
        for j in range(len(weights)):
            grad_w[j] += err * xs[j] / n
        grad_b += err / n
    loss = loss / n + l2 * sum(w * w for w in weights)
    for j in range(len(weights)):
        grad_w[j] += 2.0 * l2 * weights[j]
    return loss, grad_w, grad_b


def _column_stats(
    vectors: list[FeatureVector],
) -> tuple[list[float], list[float]]:
    """Per-feature mean / std over observed values only."""
    means: list[float] = []
    stds: list[float] = []
    for i in range(len(FEATURE_NAMES)):
        observed = [v.values[i] for v in vectors if v.values[i] is not None]
        if not observed:
            means.append(0.5)
            stds.append(1.0)
            continue
        mean = sum(observed) / len(observed)
        var = sum((v - mean) ** 2 for v in observed) / len(observed)
        means.append(mean)
        stds.append(math.sqrt(var) if var > 1e-12 else 1.0)
    return means, stds


def _standardize_all(
    vectors: list[FeatureVector],
    means: list[float],
    stds: list[float],
) -> list[list[float]]:
    out: list[list[float]] = []
    for v in vectors:
        row = []
        for i, value in enumerate(v.values):
            raw = means[i] if value is None else value
            std = stds[i] if stds[i] else 1.0
            row.append((raw - means[i]) / std)
        out.append(row)
    return out


def evaluate_predictions(
    predictions: list[float], labels: list[int]
) -> dict:
    """Accuracy / precision / recall at a 0.5 threshold.

    Every rate is None when its denominator is 0 — never a fake 0.
    """
    if not labels:
        return {
            "n": 0,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": 0,
            "true_negatives": 0,
        }
    tp = fp = fn = tn = 0
    for p, y in zip(predictions, labels):
        pred = 1 if p >= 0.5 else 0
        if pred == 1 and y == 1:
            tp += 1
        elif pred == 1 and y == 0:
            fp += 1
        elif pred == 0 and y == 1:
            fn += 1
        else:
            tn += 1
    return {
        "n": len(labels),
        "accuracy": (tp + tn) / len(labels),
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "recall": tp / (tp + fn) if (tp + fn) else None,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
    }


def train_model(
    records: list[dict],
    *,
    source: str = "outcomes",
    seed: int = SPLIT_SEED,
    epochs: int = EPOCHS,
    learning_rate: float = LEARNING_RATE,
    l2: float = L2_LAMBDA,
) -> tuple[RugRiskModel, dict]:
    """Train on labelled records; returns (model, report).

    Label: outcome "rugged" = 1, "survived" = 0. Records with an
    "unknown" (or missing) outcome are skipped — they are not
    training data. Raises InsufficientDataError when there are
    fewer than MIN_TRAINING_SAMPLES labelled records, or fewer
    than MIN_PER_CLASS of either class.
    """
    labelled = [
        (r, 1 if r.get("outcome") == "rugged" else 0)
        for r in records
        if r.get("outcome") in ("rugged", "survived")
    ]
    if len(labelled) < MIN_TRAINING_SAMPLES:
        raise InsufficientDataError(
            f"Insufficient training data: {len(labelled)} labelled "
            f"records, need at least {MIN_TRAINING_SAMPLES} "
            f"(rugged/survived outcomes only)."
        )
    if sum(y for _, y in labelled) < MIN_PER_CLASS:
        raise InsufficientDataError(
            "Insufficient training data: need at least "
            f"{MIN_PER_CLASS} rugged examples."
        )
    if sum(1 - y for _, y in labelled) < MIN_PER_CLASS:
        raise InsufficientDataError(
            "Insufficient training data: need at least "
            f"{MIN_PER_CLASS} survived examples."
        )

    vectors = [extract_features(r) for r, _ in labelled]
    labels = [y for _, y in labelled]

    # Deterministic 80/20 held-out split.
    rng = random.Random(seed)
    order = list(range(len(labelled)))
    rng.shuffle(order)
    n_test = max(1, int(round(HOLDOUT_FRACTION * len(labelled))))
    test_idx = order[:n_test]
    train_idx = order[n_test:]

    train_vectors = [vectors[i] for i in train_idx]
    train_labels = [labels[i] for i in train_idx]
    test_vectors = [vectors[i] for i in test_idx]
    test_labels = [labels[i] for i in test_idx]

    means, stds = _column_stats(train_vectors)
    x_train = _standardize_all(train_vectors, means, stds)
    x_test = _standardize_all(test_vectors, means, stds)

    weights = [0.0] * len(FEATURE_NAMES)
    bias = 0.0
    loss_before, _, _ = _loss_and_gradients(
        weights, bias, x_train, train_labels, l2
    )
    for _ in range(epochs):
        _, grad_w, grad_b = _loss_and_gradients(
            weights, bias, x_train, train_labels, l2
        )
        weights = [
            w - learning_rate * g for w, g in zip(weights, grad_w)
        ]
        bias -= learning_rate * grad_b
    loss_after, _, _ = _loss_and_gradients(
        weights, bias, x_train, train_labels, l2
    )

    model = RugRiskModel(
        weights=weights,
        bias=bias,
        feature_means=means,
        feature_stds=stds,
        n_train=len(train_idx),
        source=source,
    )
    test_probs = [
        model.predict(v).probability for v in test_vectors
    ]
    evaluation = evaluate_predictions(test_probs, test_labels)
    model.evaluation = evaluation
    report = {
        "source": source,
        "n_labelled": len(labelled),
        "n_train": len(train_idx),
        "n_test": len(test_idx),
        "loss_before": round(loss_before, 4),
        "loss_after": round(loss_after, 4),
        "evaluation": evaluation,
    }
    return model, report


def evaluate_model(model: RugRiskModel, records: list[dict]) -> dict:
    """Evaluate a saved model on labelled records (no retraining)."""
    labelled = [
        (r, 1 if r.get("outcome") == "rugged" else 0)
        for r in records
        if r.get("outcome") in ("rugged", "survived")
    ]
    if not labelled:
        raise InsufficientDataError(
            "No labelled records (rugged/survived outcomes) to evaluate on."
        )
    probs = [model.predict_dict(r).probability for r, _ in labelled]
    result = evaluate_predictions(probs, [y for _, y in labelled])
    result["n_labelled"] = len(labelled)
    return result


# ======================================================================
# Synthetic labelled data (tests / demo ONLY — never real data)
# ======================================================================
def synthetic_dataset(
    n_rugged: int = 40,
    n_survived: int = 40,
    seed: int = 7,
) -> list[dict]:
    """Clearly-labelled synthetic training data for tests/demo.

    Rugged examples look like rugs (deep fast collapse, big dev
    sell, LP removed, high wash, concentrated holders, stealth
    cluster); survived examples look like honest coins. Every
    record carries ``"synthetic": True`` — this data must never be
    mistaken for real outcomes. A few values are randomly dropped
    so missing-feature handling is exercised too.
    """
    rng = random.Random(seed)
    records: list[dict] = []

    def maybe_drop(record: dict, key: str, chance: float = 0.1) -> None:
        if rng.random() < chance:
            record.pop(key, None)

    for i in range(n_rugged):
        record = {
            "mint": f"SYNTH-RUG-{i:03d}",
            "verdict": "flagged",
            "outcome": "rugged",
            "collapse_pct": round(rng.uniform(85.0, 99.0), 1),
            "dev_sold_pct": round(rng.uniform(40.0, 95.0), 1),
            "lp_removed": rng.random() < 0.8,
            "time_to_collapse_minutes": int(rng.uniform(5, 600)),
            "wash_score": round(rng.uniform(50.0, 95.0), 1),
            "top1_share": round(rng.uniform(0.25, 0.60), 3),
            "top10_share": round(rng.uniform(0.60, 0.95), 3),
            "holder_count": int(rng.uniform(20, 400)),
            "cluster_size": int(rng.uniform(2, 6)),
            "liquidity_usd": round(rng.uniform(1_000, 20_000), 2),
            "synthetic": True,
        }
        maybe_drop(record, "wash_score")
        maybe_drop(record, "holder_count")
        records.append(record)
    for i in range(n_survived):
        record = {
            "mint": f"SYNTH-OK-{i:03d}",
            "verdict": "not_flagged",
            "outcome": "survived",
            "collapse_pct": round(rng.uniform(0.0, 60.0), 1),
            "dev_sold_pct": round(rng.uniform(0.0, 15.0), 1),
            "lp_removed": rng.random() < 0.1,
            "time_to_collapse_minutes": int(rng.uniform(2_000, 10_000)),
            "wash_score": round(rng.uniform(0.0, 35.0), 1),
            "top1_share": round(rng.uniform(0.01, 0.15), 3),
            "top10_share": round(rng.uniform(0.10, 0.45), 3),
            "holder_count": int(rng.uniform(500, 20_000)),
            "cluster_size": int(rng.uniform(0, 1)),
            "liquidity_usd": round(rng.uniform(30_000, 500_000), 2),
            "synthetic": True,
        }
        maybe_drop(record, "wash_score")
        maybe_drop(record, "top1_share")
        records.append(record)
    rng.shuffle(records)
    return records


def train_on_synthetic(
    n_rugged: int = 40,
    n_survived: int = 40,
    seed: int = 7,
    **kwargs,
) -> tuple[RugRiskModel, dict]:
    """Train on the synthetic demo dataset (labelled synthetic)."""
    return train_model(
        synthetic_dataset(n_rugged, n_survived, seed),
        source="synthetic",
        **kwargs,
    )


# ======================================================================
# Persistence
# ======================================================================
def _refuse_registry_path(path: Path) -> None:
    if path.name == "registry.json":
        raise ValueError("Refusing to write to registry.json — use a model file.")


def save_model(path: str | Path, model: RugRiskModel) -> None:
    """Write a trained model to a caller-given JSON file."""
    p = Path(path)
    _refuse_registry_path(p)
    payload = dict(model.to_dict())
    payload["created"] = date.today().isoformat()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_model(path: str | Path) -> RugRiskModel:
    """Load a model from a JSON file."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Model file {p} is not valid JSON: {exc}") from exc
    return RugRiskModel.from_dict(data)


# ======================================================================
# Dev-history integration (advisory only)
# ======================================================================
def score_dev_history(history, model: RugRiskModel) -> dict:
    """Advisory ML scores for a DevHistory — never a verdict.

    Each mint is scored from the fields the history actually has
    (collapse, dev sell, LP removed, time-to-collapse); wash,
    holder, cluster and liquidity features are missing for history
    mints and are flagged as missing, never guessed. The dev's
    rule-based verdict is NOT touched by this function.
    """
    per_mint: list[dict] = []
    for mint in history.mints:
        collapse = collapse_pct(
            MintEvent(
                peak_market_cap_usd=mint.peak_market_cap_usd,
                current_market_cap_usd=mint.current_market_cap_usd,
            )
        )
        prediction = model.predict_dict(
            {
                "collapse_pct": collapse,
                "dev_sold_pct": mint.dev_sold_pct,
                "lp_removed": mint.lp_removed,
                "time_to_collapse_minutes": mint.time_to_collapse_minutes,
            }
        )
        per_mint.append(
            {
                "mint": mint.mint,
                "outcome": mint.outcome,
                **prediction.to_dict(),
            }
        )
    scored = [p for p in per_mint if p["probability"] is not None]
    max_prob = max((p["probability"] for p in scored), default=None)
    return {
        "advisory_only": True,
        "max_probability": max_prob,
        "band": _band(max_prob) if max_prob is not None else None,
        "per_mint": per_mint,
    }


# ======================================================================
# CLI
# ======================================================================
def _load_json_file(path: str, what: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Cannot read {what} file {path}: {exc}") from exc


def _print_report(report: dict) -> None:
    ev = report["evaluation"]
    print(
        f"Trained on {report['n_train']} records "
        f"(held out {report['n_test']} for evaluation, source: {report['source']})."
    )
    print(f"Loss: {report['loss_before']} -> {report['loss_after']}")
    if ev["accuracy"] is None:
        print("Held-out evaluation: not enough test data to score.")
        return
    print(
        f"Held-out: accuracy {ev['accuracy']:.2f}, "
        f"precision {ev['precision'] if ev['precision'] is None else round(ev['precision'], 2)}, "
        f"recall {ev['recall'] if ev['recall'] is None else round(ev['recall'], 2)} "
        f"({ev['true_positives']} TP, {ev['false_positives']} FP, "
        f"{ev['false_negatives']} FN, {ev['true_negatives']} TN)."
    )
    if ev["accuracy"] is not None and ev["accuracy"] < 0.7:
        print(
            "WARNING: held-out accuracy is below 0.70 — this model is "
            "weak. Treat its scores as advisory only, and train on "
            "more real outcomes before trusting it."
        )


def _print_prediction(prediction: RiskPrediction) -> None:
    print(
        f"Rug risk: {prediction.probability:.2f} ({prediction.band.upper()})"
    )
    print(f"Log-odds: {prediction.log_odds:.2f}")
    if prediction.missing_features:
        print(
            "Missing features (imputed with training mean, "
            "contribute 0 to the score): "
            + ", ".join(prediction.missing_features)
        )
    print("Top contributors:")
    for item in prediction.top_features:
        value = (
            f"{item['value']:.2f}"
            if isinstance(item["value"], float)
            else str(item["value"])
        )
        print(
            f"  - {item['feature']} = {value} "
            f"({item['contribution']:+.2f}, {item['direction']})"
        )


def main(argv: list[str] | None = None) -> int:
    """ML risk model CLI.

    ``python -m src.ml_model --train outcomes.json --model model.json``
    ``python -m src.ml_model --demo --model model.json``
    ``python -m src.ml_model --predict mint.json --model model.json [--json]``
    ``python -m src.ml_model --evaluate --model model.json --data outcomes.json``
    Exit 0 = ok, 2 = invalid args / insufficient data / bad file.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm ML risk model: logistic regression, stdlib only, advisory scores next to the rule verdict."
    )
    parser.add_argument("--train", type=str, default=None, help="OutcomeTracker JSON file to train on")
    parser.add_argument("--demo", action="store_true", help="Train on the synthetic demo dataset (labelled synthetic)")
    parser.add_argument("--predict", type=str, default=None, help="Mint JSON file (feature dict) to score")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate a saved model on --data")
    parser.add_argument("--model", type=str, default=None, help="Model JSON file (write on train/demo, read on predict/evaluate)")
    parser.add_argument("--data", type=str, default=None, help="OutcomeTracker JSON file to evaluate on")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args(argv)

    modes = [bool(args.train), args.demo, bool(args.predict), args.evaluate]
    if sum(modes) != 1:
        print(
            "ERROR: pick exactly one mode: --train, --demo, --predict or --evaluate.",
            file=sys.stderr,
        )
        return EXIT_INVALID

    try:
        if args.train or args.demo:
            if not args.model:
                print("ERROR: --train / --demo needs --model <out.json>.", file=sys.stderr)
                return EXIT_INVALID
            if args.demo:
                model, report = train_on_synthetic()
            else:
                records = load_outcomes(args.train)
                if not isinstance(records, list):
                    print(f"ERROR: {args.train} must hold a JSON list.", file=sys.stderr)
                    return EXIT_INVALID
                model, report = train_model(records)
            save_model(args.model, model)
            if args.json:
                print(json.dumps({"model": model.to_dict(), "report": report}, indent=2))
            else:
                print(f"Model saved to {args.model}")
                _print_report(report)
            return EXIT_OK

        if args.predict:
            if not args.model:
                print("ERROR: --predict needs --model <model.json>.", file=sys.stderr)
                return EXIT_INVALID
            data = _load_json_file(args.predict, "mint")
            if not isinstance(data, dict):
                print(f"ERROR: {args.predict} must hold a JSON object.", file=sys.stderr)
                return EXIT_INVALID
            model = load_model(args.model)
            prediction = model.predict_dict(data)
            if args.json:
                print(json.dumps(prediction.to_dict(), indent=2))
            else:
                _print_prediction(prediction)
            return EXIT_OK

        # --evaluate
        if not args.model or not args.data:
            print("ERROR: --evaluate needs --model and --data.", file=sys.stderr)
            return EXIT_INVALID
        model = load_model(args.model)
        records = load_outcomes(args.data)
        if not isinstance(records, list):
            print(f"ERROR: {args.data} must hold a JSON list.", file=sys.stderr)
            return EXIT_INVALID
        result = evaluate_model(model, records)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                f"Evaluated on {result['n_labelled']} labelled records: "
                f"accuracy {result['accuracy'] if result['accuracy'] is None else round(result['accuracy'], 2)}, "
                f"precision {result['precision'] if result['precision'] is None else round(result['precision'], 2)}, "
                f"recall {result['recall'] if result['recall'] is None else round(result['recall'], 2)} "
                f"({result['true_positives']} TP, {result['false_positives']} FP, "
                f"{result['false_negatives']} FN, {result['true_negatives']} TN)."
            )
        return EXIT_OK
    except (ValueError, InsufficientDataError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
