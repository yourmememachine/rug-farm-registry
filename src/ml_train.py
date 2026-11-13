"""Proper ML training job (v2.14 "Pro").

The model in ``src.ml_model`` could already train, but there was
no *job* around it: no train/validation/test split, no persisted
model, no record of what was trained on what. This module is that
job:

    python -m src.ml_train [--dataset data/ml_real_dataset.json]
                           [--db data/rugfarm.db] [--out data/models]
                           [--seed 42] [--json]

1. Load the REAL dataset (``data/ml_real_dataset.json``). Any
   record marked ``synthetic`` is rejected outright — this job
   never trains on synthetic data.
2. Stratified train / validation / test split (deterministic for
   a given seed), documented per split. A class with fewer than
   3 examples cannot be split three ways, so it all goes to
   train — and the report says so.
3. Train the existing from-scratch logistic regression
   (``src.ml_model`` primitives — no model code is duplicated
   here) on the train split only, then report REAL metrics
   (accuracy / precision / recall) per split.
4. Persist: the model as a JSON artifact under ``data/models/``
   and one ``model_runs`` row in the SQLite database, keyed by a
   deterministic run id (same data + seed = same run id, so a
   re-run is an update, not a duplicate).

Refusal is a feature: fewer than 10 labelled records, or fewer
than 2 rugged (or 2 survived) examples, exits 2 with the exact
counts and the labelling-rule version that produced them — the
job never pretends a model trained. Today the real dataset has
0 rugged records, so the job refuses; that is the honest
outcome until the dataset grows.

Exit codes: 0 = trained and persisted, 2 = insufficient data /
bad file / invalid args. Stdlib + httpx only, no wallet, no funds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

from . import db as db_mod
from .ml_model import (
    EPOCHS,
    L2_LAMBDA,
    LEARNING_RATE,
    MIN_PER_CLASS,
    MIN_TRAINING_SAMPLES,
    InsufficientDataError,
    RugRiskModel,
    _column_stats,
    _loss_and_gradients,
    _standardize_all,
    evaluate_predictions,
    extract_features,
    save_model,
)

EXIT_OK = 0
EXIT_INVALID = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "ml_real_dataset.json"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "rugfarm.db"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "models"
DEFAULT_SEED = 42
TEST_FRACTION = 0.2
VAL_FRACTION = 0.2


def load_real_dataset(path: str | Path) -> tuple[list[dict], str | None]:
    """Load the real labelled dataset; reject any synthetic record.

    Returns (records, rule_version). The rule version comes from
    the ``ml_real_dataset.summary.json`` sidecar when it exists,
    else from the first record that carries one.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Cannot read dataset file {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Dataset {path} must hold a JSON list of records.")
    synthetic = [r for r in data if isinstance(r, dict) and r.get("synthetic")]
    if synthetic:
        raise ValueError(
            f"Dataset {path} contains {len(synthetic)} synthetic record(s) "
            "— this job trains on real data only."
        )
    rule_version = None
    summary_path = path.with_name("ml_real_dataset.summary.json")
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            rule_version = summary.get("labelling_rule_version")
        except (ValueError, OSError):
            pass
    if rule_version is None:
        for record in data:
            if isinstance(record, dict) and record.get("labelling_rule_version"):
                rule_version = record["labelling_rule_version"]
                break
    return data, rule_version


def stratified_split(
    records: list[dict],
    test_fraction: float = TEST_FRACTION,
    val_fraction: float = VAL_FRACTION,
    seed: int = DEFAULT_SEED,
) -> dict[str, list[dict]]:
    """Deterministic stratified train / validation / test split.

    Each class is shuffled (seeded) and split separately, so every
    split holds both classes where the class is big enough. A
    class with fewer than 3 examples cannot be split three ways —
    it all goes to train, and the caller must read the per-split
    counts in the report instead of assuming a balanced split.
    """
    by_class: dict[str, list[dict]] = {}
    for record in records:
        by_class.setdefault(record.get("outcome", "unknown"), []).append(record)
    splits: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    for outcome in sorted(by_class):
        items = list(by_class[outcome])
        random.Random(f"{seed}:{outcome}").shuffle(items)
        n = len(items)
        if n < 3:
            splits["train"].extend(items)
            continue
        n_test = max(1, int(round(n * test_fraction)))
        remaining = n - n_test
        n_val = max(1, int(round(n * val_fraction))) if remaining >= 2 else 0
        splits["test"].extend(items[:n_test])
        splits["val"].extend(items[n_test : n_test + n_val])
        splits["train"].extend(items[n_test + n_val :])
    return splits


def _train_on_split(
    train_records: list[dict],
    epochs: int = EPOCHS,
    learning_rate: float = LEARNING_RATE,
    l2: float = L2_LAMBDA,
) -> RugRiskModel:
    """Gradient descent on the train split (ml_model primitives)."""
    vectors = [extract_features(r) for r in train_records]
    labels = [1 if r.get("outcome") == "rugged" else 0 for r in train_records]
    means, stds = _column_stats(vectors)
    x_train = _standardize_all(vectors, means, stds)
    weights = [0.0] * len(vectors[0].values) if vectors else []
    bias = 0.0
    for _ in range(epochs):
        _, grad_w, grad_b = _loss_and_gradients(weights, bias, x_train, labels, l2)
        weights = [w - learning_rate * g for w, g in zip(weights, grad_w)]
        bias -= learning_rate * grad_b
    return RugRiskModel(
        weights=weights,
        bias=bias,
        feature_means=means,
        feature_stds=stds,
        n_train=len(train_records),
        source="real",
    )


def _evaluate(model: RugRiskModel, records: list[dict]) -> dict:
    """Metrics for one split; rates are None when unscorable."""
    if not records:
        return evaluate_predictions([], [])
    probs = [model.predict_dict(r).probability for r in records]
    labels = [1 if r.get("outcome") == "rugged" else 0 for r in records]
    return evaluate_predictions(probs, labels)


def _run_id(rule_version: str | None, seed: int, labelled: list[dict]) -> str:
    """Deterministic run id: same data + seed = same run id."""
    mints = sorted(str(r.get("mint", "")) for r in labelled)
    digest = hashlib.sha256(
        f"{rule_version}|{seed}|{','.join(mints)}".encode("utf-8")
    ).hexdigest()
    return f"ml-{digest[:12]}"


def train_job(
    records: list[dict],
    rule_version: str | None,
    db_conn=None,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Run the full training job. Returns the report dict.

    Raises InsufficientDataError (with exact counts and the rule
    version) when the data cannot honestly train a model.
    """
    labelled = [
        r
        for r in records
        if isinstance(r, dict) and r.get("outcome") in ("rugged", "survived")
    ]
    n_rugged = sum(1 for r in labelled if r["outcome"] == "rugged")
    n_survived = len(labelled) - n_rugged
    if len(labelled) < MIN_TRAINING_SAMPLES:
        raise InsufficientDataError(
            f"Insufficient training data: {len(labelled)} labelled "
            f"records (rugged: {n_rugged}, survived: {n_survived}), "
            f"need at least {MIN_TRAINING_SAMPLES} "
            f"(labelling rule {rule_version})."
        )
    if n_rugged < MIN_PER_CLASS:
        raise InsufficientDataError(
            f"Insufficient training data: {n_rugged} rugged example(s), "
            f"need at least {MIN_PER_CLASS} "
            f"(labelling rule {rule_version})."
        )
    if n_survived < MIN_PER_CLASS:
        raise InsufficientDataError(
            f"Insufficient training data: {n_survived} survived example(s), "
            f"need at least {MIN_PER_CLASS} "
            f"(labelling rule {rule_version})."
        )

    splits = stratified_split(labelled, seed=seed)
    model = _train_on_split(splits["train"])
    metrics = {
        name: _evaluate(model, split_records)
        for name, split_records in splits.items()
    }
    # ml_model convention: evaluation = the held-out test metrics.
    model.evaluation = metrics["test"]

    run_id = _run_id(rule_version, seed, labelled)
    out_path = Path(out_dir) / f"{run_id}.json"
    save_model(out_path, model)
    if db_conn is not None:
        db_mod.record_model_run(
            db_conn,
            run_id,
            rule_version,
            train_n=len(splits["train"]),
            metrics_json=json.dumps(metrics),
        )

    def _split_counts(split_records: list[dict]) -> dict:
        return {
            "n": len(split_records),
            "rugged": sum(
                1 for r in split_records if r.get("outcome") == "rugged"
            ),
            "survived": sum(
                1 for r in split_records if r.get("outcome") == "survived"
            ),
        }

    return {
        "run_id": run_id,
        "rule_version": rule_version,
        "seed": seed,
        "n_labelled": len(labelled),
        "n_rugged": n_rugged,
        "n_survived": n_survived,
        "splits": {name: _split_counts(s) for name, s in splits.items()},
        "metrics": metrics,
        "model_path": str(out_path),
    }


def _fmt_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _print_report(report: dict) -> None:
    print(f"Run {report['run_id']} (labelling rule {report['rule_version']}).")
    print(
        f"Labelled: {report['n_labelled']} "
        f"(rugged {report['n_rugged']}, survived {report['n_survived']})."
    )
    for name in ("train", "val", "test"):
        counts = report["splits"][name]
        metrics = report["metrics"][name]
        print(
            f"{name}: {counts['n']} records "
            f"(rugged {counts['rugged']}, survived {counts['survived']}) — "
            f"accuracy {_fmt_metric(metrics['accuracy'])}, "
            f"precision {_fmt_metric(metrics['precision'])}, "
            f"recall {_fmt_metric(metrics['recall'])} "
            f"({metrics['true_positives']} TP, {metrics['false_positives']} FP, "
            f"{metrics['false_negatives']} FN, {metrics['true_negatives']} TN)."
        )
    print(f"Model saved to {report['model_path']}")


def main(argv: list[str] | None = None) -> int:
    """ML training job CLI.

    ``python -m src.ml_train [--dataset <path>] [--db <path>]
    [--out <dir>] [--seed N] [--json]``
    Exit 0 = trained and persisted, 2 = insufficient data / bad
    file / invalid args.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm ML training job: real data, train/val/test split, persisted model + run record."
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=str(DEFAULT_DATASET_PATH),
        help="Real labelled dataset JSON (default: data/ml_real_dataset.json)",
    )
    parser.add_argument(
        "--db", type=str, default=str(DEFAULT_DB_PATH), help="SQLite database path"
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(DEFAULT_OUT_DIR),
        help="Directory for model artifacts (default: data/models)",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="Split seed (default: 42)"
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)

    try:
        records, rule_version = load_real_dataset(args.dataset)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID

    conn = db_mod.init_db(args.db)
    try:
        report = train_job(
            records, rule_version, db_conn=conn, out_dir=args.out, seed=args.seed
        )
    except InsufficientDataError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
