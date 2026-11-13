"""Tests for src.ml_train — the proper ML training job (v2.14).

Synthetic data only in the sense that fixtures are hand-built
labelled records in the real dataset's shape — never marked
synthetic, never taken from the synthetic demo generator, and
never read from the real dataset file (v2.13 is rewriting it).
"""
import json

import pytest

from src import db as db_mod
from src.ml_model import InsufficientDataError
from src.ml_train import (
    load_real_dataset,
    main,
    stratified_split,
    train_job,
)


def _record(mint, outcome, collapse, dev_sold, lp_removed, wash=20.0):
    return {
        "mint": mint,
        "outcome": outcome,
        "collapse_pct": collapse,
        "dev_sold_pct": dev_sold,
        "lp_removed": lp_removed,
        "time_to_collapse_minutes": 60 if outcome == "rugged" else 5000,
        "wash_score": wash,
        "top1_share": 0.4 if outcome == "rugged" else 0.05,
        "top10_share": 0.8 if outcome == "rugged" else 0.3,
        "holder_count": 100 if outcome == "rugged" else 5000,
        "cluster_size": 3 if outcome == "rugged" else 0,
        "liquidity_usd": 5000.0 if outcome == "rugged" else 200000.0,
    }


def _fixture_records(n_rugged=6, n_survived=8):
    records = [
        _record(f"RUGMINT{i:02d}", "rugged", 95.0, 70.0, True, wash=80.0)
        for i in range(n_rugged)
    ]
    records += [
        _record(f"OKMINT{i:02d}", "survived", 10.0, 5.0, False, wash=10.0)
        for i in range(n_survived)
    ]
    return records


def test_stratified_split_counts_and_determinism():
    records = _fixture_records(6, 8)
    first = stratified_split(records, seed=42)
    second = stratified_split(records, seed=42)
    assert first == second  # deterministic
    total = sum(len(v) for v in first.values())
    assert total == 14
    for split in ("train", "val", "test"):
        outcomes = {r["outcome"] for r in first[split]}
        assert outcomes <= {"rugged", "survived"}
    # Both classes appear in train; test and val each hold some.
    assert len(first["test"]) >= 2
    assert len(first["val"]) >= 2


def test_stratified_split_tiny_class_all_to_train():
    records = _fixture_records(2, 8)
    splits = stratified_split(records, seed=42)
    rugged_train = [r for r in splits["train"] if r["outcome"] == "rugged"]
    assert len(rugged_train) == 2  # cannot split a 2-example class
    assert not splits["test"] or all(
        r["outcome"] == "survived" for r in splits["test"]
    )


def test_train_job_reports_metrics_per_split(tmp_path):
    conn = db_mod.init_db(tmp_path / "test.db")
    try:
        report = train_job(
            _fixture_records(6, 8),
            "real-v2",
            db_conn=conn,
            out_dir=tmp_path / "models",
            seed=42,
        )
    finally:
        conn.close()
    assert report["n_labelled"] == 14
    assert report["n_rugged"] == 6
    assert report["n_survived"] == 8
    assert report["rule_version"] == "real-v2"
    for name in ("train", "val", "test"):
        metrics = report["metrics"][name]
        assert metrics["n"] == report["splits"][name]["n"]
        assert metrics["accuracy"] is not None
    model_path = tmp_path / "models" / f"{report['run_id']}.json"
    assert model_path.is_file()
    model = json.loads(model_path.read_text())
    assert model["format"] == "rugfarm-ml-model"
    assert model["source"] == "real"
    runs = db_mod.list_model_runs(db_mod.init_db(tmp_path / "test.db"))
    assert len(runs) == 1
    assert runs[0]["run_id"] == report["run_id"]


def test_train_job_is_deterministic(tmp_path):
    records = _fixture_records(6, 8)
    first = train_job(records, "real-v2", out_dir=tmp_path / "m1", seed=42)
    second = train_job(records, "real-v2", out_dir=tmp_path / "m2", seed=42)
    assert first["run_id"] == second["run_id"]
    weights1 = json.loads((tmp_path / "m1" / f"{first['run_id']}.json").read_text())[
        "weights"
    ]
    weights2 = json.loads((tmp_path / "m2" / f"{second['run_id']}.json").read_text())[
        "weights"
    ]
    assert weights1 == weights2


def test_train_job_refuses_too_few_rugged(tmp_path):
    with pytest.raises(InsufficientDataError, match="rugged"):
        train_job(
            _fixture_records(1, 10), "real-v2", out_dir=tmp_path / "m", seed=42
        )


def test_train_job_refuses_too_few_labelled(tmp_path):
    with pytest.raises(InsufficientDataError, match="6 labelled"):
        train_job(
            _fixture_records(2, 4), "real-v2", out_dir=tmp_path / "m", seed=42
        )


def test_load_real_dataset_rejects_synthetic(tmp_path):
    path = tmp_path / "dataset.json"
    records = _fixture_records(2, 2)
    records[0] = dict(records[0], synthetic=True)
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError, match="synthetic"):
        load_real_dataset(path)


def test_load_real_dataset_reads_rule_version_from_sidecar(tmp_path):
    path = tmp_path / "ml_real_dataset.json"
    path.write_text(json.dumps(_fixture_records(2, 2)))
    sidecar = tmp_path / "ml_real_dataset.summary.json"
    sidecar.write_text(json.dumps({"labelling_rule_version": "real-v2"}))
    _, rule_version = load_real_dataset(path)
    assert rule_version == "real-v2"


def test_load_real_dataset_rejects_non_list(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError, match="JSON list"):
        load_real_dataset(path)


def test_cli_refuses_real_dataset_with_zero_rugged(tmp_path):
    dataset = tmp_path / "ml_real_dataset.json"
    dataset.write_text(json.dumps(_fixture_records(0, 12)))
    assert (
        main(
            [
                "--dataset",
                str(dataset),
                "--db",
                str(tmp_path / "test.db"),
                "--out",
                str(tmp_path / "models"),
            ]
        )
        == 2
    )


def test_cli_trains_and_persists(tmp_path):
    dataset = tmp_path / "ml_real_dataset.json"
    dataset.write_text(json.dumps(_fixture_records(6, 8)))
    result = main(
        [
            "--dataset",
            str(dataset),
            "--db",
            str(tmp_path / "test.db"),
            "--out",
            str(tmp_path / "models"),
            "--json",
        ]
    )
    assert result == 0
    assert list((tmp_path / "models").glob("ml-*.json"))
    conn = db_mod.init_db(tmp_path / "test.db")
    try:
        assert len(db_mod.list_model_runs(conn)) == 1
    finally:
        conn.close()


def test_cli_bad_dataset_exits_2(tmp_path):
    assert (
        main(
            [
                "--dataset",
                str(tmp_path / "missing.json"),
                "--db",
                str(tmp_path / "test.db"),
            ]
        )
        == 2
    )
