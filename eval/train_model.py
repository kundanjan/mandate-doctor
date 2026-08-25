"""Train the recovery-probability model on collected outcomes.

Reads labeled rows from data/training_data.db (link-arm outcomes),
fits an L2-regularized logistic regression with pure numpy (no sklearn
dependency), reports k-fold cross-validated metrics, and saves an
interpretable model artifact (JSON) consumed by the policy engine.

Data provenance per row (all measured, none invented):
  npci_bank, error_class, amount_paise, regime -> features
  failure_error_code                           -> real Razorpay code
  retry_prior                                  -> NPCI bank approval rate
  recovered                                    -> real API-verified outcome
Rows with errors (checkout_timeout, API failures) are excluded.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "training_data.db"
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

RANDOM_SEED = 42
L2_LAMBDA = 1.0
LR = 0.1
EPOCHS = 3000


def load_rows(db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT scenario_key, npci_bank, rzp_bank, error_class, amount_paise,
               regime, failure_error_code, retry_prior, recovered
        FROM outcomes
        WHERE error IS NULL AND assigned_click IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_design(
    rows: list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray, list[str], dict[str, list[Any]]]:
    """One-hot categoricals + scaled numeric features.

    Returns X, y, feature_names, and the encoding maps (for the artifact).
    """
    banks = sorted({r["npci_bank"] for r in rows})
    classes = sorted({r["error_class"] for r in rows})
    regimes = sorted({r["regime"] for r in rows})
    err_codes = sorted({r["failure_error_code"] or "NONE" for r in rows})
    amounts = sorted({r["amount_paise"] for r in rows})

    feature_names: list[str] = []
    columns: list[np.ndarray] = []

    def add_onehot(
        name_prefix: str,
        values: list[Any],
        getter: Callable[[dict[str, Any]], Any],
    ) -> None:
        for v in values:
            feature_names.append(f"{name_prefix}={v}")
            columns.append(np.array([1.0 if getter(r) == v else 0.0 for r in rows]))

    add_onehot("bank", banks, lambda r: r["npci_bank"])
    add_onehot("err_class", classes, lambda r: r["error_class"])
    add_onehot("regime", regimes, lambda r: r["regime"])
    add_onehot("fail_code", err_codes, lambda r: r["failure_error_code"] or "NONE")
    add_onehot("amount", amounts, lambda r: r["amount_paise"])
    # numeric: NPCI retry prior (real per-bank approval rate)
    feature_names.append("retry_prior")
    columns.append(np.array([float(r["retry_prior"] or 0.0) for r in rows]))
    # intercept
    feature_names.append("intercept")
    columns.append(np.ones(len(rows)))

    X: np.ndarray = np.stack(columns, axis=1)
    y = np.array([float(r["recovered"]) for r in rows])
    encoding = {
        "banks": banks,
        "classes": classes,
        "regimes": regimes,
        "err_codes": err_codes,
        "amounts": amounts,
    }
    return X, y, feature_names, encoding


def sigmoid(z: np.ndarray) -> np.ndarray:
    out: np.ndarray = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
    return out


def fit_logistic(
    X: np.ndarray, y: np.ndarray, l2: float = L2_LAMBDA, lr: float = LR, epochs: int = EPOCHS
) -> np.ndarray:
    """Gradient descent on L2-regularized negative log-likelihood."""
    n, d = X.shape
    w = np.zeros(d)
    rng = np.random.default_rng(RANDOM_SEED)
    for _ in range(epochs):
        idx = rng.permutation(n)
        for i in idx:  # stochastic, stable for small data
            xi, yi = X[i], y[i]
            p = sigmoid(xi @ w)
            grad = (p - yi) * xi + l2 * w / n
            w -= lr * grad
    return w


def predict_proba(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    z: np.ndarray = X @ w
    return sigmoid(z)


def kfold_metrics(
    X: np.ndarray, y: np.ndarray, k: int = 5, seed: int = RANDOM_SEED
) -> dict[str, Any]:
    """Stratified-ish k-fold via label-shuffled rounds; reports accuracy,
    precision, recall, and log-loss on held-out folds."""
    rng = np.random.default_rng(seed)
    n = len(y)
    idx = rng.permutation(n)
    folds = np.array_split(idx, k)
    accs, precs, recs, losses = [], [], [], []
    for f in range(k):
        test_idx = folds[f]
        train_idx = np.concatenate([folds[j] for j in range(k) if j != f])
        if len(np.unique(y[train_idx])) < 2:
            continue
        w = fit_logistic(X[train_idx], y[train_idx])
        p = predict_proba(w, X[test_idx])
        yhat = (p >= 0.5).astype(float)
        yt = y[test_idx]
        accs.append(float((yhat == yt).mean()))
        tp = float(((yhat == 1) & (yt == 1)).sum())
        fp = float(((yhat == 1) & (yt == 0)).sum())
        fn = float(((yhat == 0) & (yt == 1)).sum())
        precs.append(tp / (tp + fp) if tp + fp > 0 else 0.0)
        recs.append(tp / (tp + fn) if tp + fn > 0 else 0.0)
        eps = 1e-9
        losses.append(float(-np.mean(yt * np.log(p + eps) + (1 - yt) * np.log(1 - p + eps))))
    if not accs:
        return {
            "cv_accuracy": 0.0,
            "cv_precision": 0.0,
            "cv_recall": 0.0,
            "cv_logloss": 0.0,
            "folds": k,
        }
    return {
        "cv_accuracy": float(np.mean(accs)),
        "cv_precision": float(np.mean(precs)),
        "cv_recall": float(np.mean(recs)),
        "cv_logloss": float(np.mean(losses)),
        "folds": k,
    }


def train(db_path: Path | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    rows = load_rows(db_path)
    if len(rows) < 20:
        logger.warning("insufficient_data", rows=len(rows), minimum=20)
        return {"status": "insufficient_data", "rows": len(rows)}

    X, y, feature_names, encoding = build_design(rows)
    w = fit_logistic(X, y)
    p = predict_proba(w, X)
    insample_acc = float(((p >= 0.5).astype(float) == y).mean())
    metrics = kfold_metrics(X, y)
    metrics.update(
        {
            "status": "ok",
            "rows": len(rows),
            "recovered": int(y.sum()),
            "in_sample_accuracy": insample_acc,
            "base_rate": float(y.mean()),
        }
    )

    artifact = {
        "model_type": "logistic_regression_l2",
        "trained_at": __import__("datetime").datetime.now().isoformat(),
        "feature_names": feature_names,
        "weights": [float(x) for x in w],
        "encoding": encoding,
        "hyperparams": {"l2": L2_LAMBDA, "lr": LR, "epochs": EPOCHS, "seed": RANDOM_SEED},
        "metrics": metrics,
        "data_provenance": (
            "labels = API-verified payment-link outcomes from Razorpay "
            "test mode; retry_prior = NPCI Jul-2026 bank approval rates; "
            "no invented probabilities"
        ),
    }

    out = out_dir or MODELS_DIR
    out.mkdir(exist_ok=True)
    path = out / "recovery_model.json"
    path.write_text(json.dumps(artifact, indent=2))
    logger.info("model_saved", path=str(path), **metrics)
    return artifact


def load_model(path: Path | None = None) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((path or MODELS_DIR / "recovery_model.json").read_text())
    return loaded


def train_incremental(min_new_rows: int = 5) -> dict[str, Any]:
    """Retrain only if enough NEW labeled rows arrived since the last run.

    Stores every accepted model version under models/history/ and appends
    a metrics line to models/training_log.jsonl, so training progress is
    auditable and the policy engine always has the latest artifact.
    """
    log_path = MODELS_DIR / "training_log.jsonl"
    rows = load_rows()
    last_rows = 0
    if log_path.exists():
        lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        if lines:
            last_rows = int(json.loads(lines[-1]).get("rows", 0))
    if log_path.exists() and len(rows) - last_rows < min_new_rows:
        return {
            "status": "skipped",
            "reason": f"only {len(rows) - last_rows} new rows (< {min_new_rows})",
            "rows": len(rows),
            "last_trained_rows": last_rows,
        }

    artifact = train()
    if artifact.get("status") != "ok":
        return artifact

    MODELS_DIR.mkdir(exist_ok=True)
    hist_dir = MODELS_DIR / "history"
    hist_dir.mkdir(exist_ok=True)
    stamp = artifact["trained_at"].replace(":", "").replace("-", "").replace("+", "Z")
    (hist_dir / f"recovery_model_{stamp}.json").write_text(json.dumps(artifact, indent=2))
    m = artifact["metrics"]
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "trained_at": artifact["trained_at"],
                    "rows": m["rows"],
                    "recovered": m["recovered"],
                    "cv_accuracy": m["cv_accuracy"],
                    "cv_precision": m["cv_precision"],
                    "cv_recall": m["cv_recall"],
                    "cv_logloss": m["cv_logloss"],
                }
            )
            + "\n"
        )
    return artifact


def score_row(model: dict[str, Any], row: dict[str, Any]) -> float:
    """P(recovered) for one feature row using the saved artifact."""
    enc = model["encoding"]
    names = model["feature_names"]
    weights = model["weights"]
    wmap = dict(zip(names, weights))

    def onehot(prefix: str, value: str, allowed: list[str]) -> float:
        key = f"{prefix}={value}"
        return wmap.get(key, 0.0) if value in allowed else 0.0

    z = 0.0
    z += onehot("bank", row.get("npci_bank") or "", enc["banks"])
    z += onehot("err_class", row.get("error_class") or "", enc["classes"])
    z += onehot("regime", row.get("regime") or "", enc["regimes"])
    code = row.get("failure_error_code") or "NONE"
    z += onehot("fail_code", code, enc["err_codes"])
    amount = row.get("amount_paise")
    z += onehot("amount", amount if amount is not None else "", enc["amounts"])
    z += wmap.get("retry_prior", 0.0) * float(row.get("retry_prior") or 0.0)
    z += wmap.get("intercept", 0.0)
    return float(sigmoid(np.array(z)))


if __name__ == "__main__":
    result = train()
    print(json.dumps({k: v for k, v in result.items() if k != "feature_names"}, indent=2))
