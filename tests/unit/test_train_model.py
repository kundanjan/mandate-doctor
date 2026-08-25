"""Tests for the recovery model trainer (pure numpy, no network)."""

from __future__ import annotations

import numpy as np

from eval.train_model import build_design, fit_logistic, kfold_metrics, predict_proba, sigmoid


def _synthetic_rows(n: int = 60) -> list[dict]:
    rows = []
    rng = np.random.default_rng(0)
    for i in range(n):
        bank = "BankA" if i % 2 == 0 else "BankB"
        # BankA recovers 80% of the time, BankB 20% — learnable signal
        recovered = 1 if rng.random() < (0.8 if bank == "BankA" else 0.2) else 0
        rows.append(
            {
                "scenario_key": f"s{i}",
                "npci_bank": bank,
                "rzp_bank": bank,
                "error_class": "bd" if i % 3 else "td",
                "amount_paise": 19_900 if i % 2 else 49_900,
                "regime": "base",
                "failure_error_code": "BAD_REQUEST_ERROR",
                "retry_prior": 0.3 if bank == "BankA" else 0.1,
                "recovered": recovered,
            }
        )
    return rows


def test_sigmoid_bounds() -> None:
    z = np.array([-100.0, 0.0, 100.0])
    p = sigmoid(z)
    assert p[0] < 1e-10 and abs(p[1] - 0.5) < 1e-9 and p[2] > 1 - 1e-10


def test_model_learns_bank_signal() -> None:
    rows = _synthetic_rows()
    X, y, names, _ = build_design(rows)
    w = fit_logistic(X, y, epochs=800)
    p = predict_proba(w, X)
    acc = float(((p >= 0.5).astype(float) == y).mean())
    assert acc > 0.70  # the bank signal is strong and learnable


def test_cv_metrics_on_separable_data() -> None:
    rows = _synthetic_rows(80)
    X, y, _, _ = build_design(rows)
    metrics = kfold_metrics(X, y, k=4)
    assert metrics["cv_accuracy"] > 0.7
    assert 0.0 <= metrics["cv_precision"] <= 1.0
    assert 0.0 <= metrics["cv_recall"] <= 1.0


def test_design_has_intercept_and_alignment() -> None:
    rows = _synthetic_rows(20)
    X, y, names, _ = build_design(rows)
    assert X.shape[0] == len(rows) == len(y)
    assert names[-1] == "intercept"
    assert (X[:, -1] == 1.0).all()
