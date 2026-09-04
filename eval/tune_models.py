#!/usr/bin/env python3
"""Hyperparameter tuning for top 5 models from model comparison.

Uses StratifiedKFold + GridSearchCV/RandomizedSearchCV with proper
preprocessing pipelines. Outputs best params, scores, and comparison.
"""

import json
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import (
    GridSearchCV,
    RandomizedSearchCV,
    StratifiedKFold,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.svm import SVC

warnings.filterwarnings("ignore")

DB_PATH = Path("data/training_data.db")
RANDOM_SEED = 42
CV_FOLDS = 5


def load_data():
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        """
        SELECT scenario_key, npci_bank, rzp_bank, error_class, amount_paise,
               regime, order_id, plink_id, short_url, assigned_click, recovered,
               poll_status, error, created_at, failed_payment_id,
               failure_error_code, retry_prior, design_version
        FROM outcomes
        WHERE COALESCE(design_version, 1) >= 2
          AND error IS NULL
          AND assigned_click IS NOT NULL
        """,
        conn,
    )
    conn.close()
    return df


def build_features(df):
    y = df["recovered"].values.astype(int)
    drop_cols = [
        "scenario_key", "order_id", "plink_id", "short_url",
        "assigned_click", "poll_status", "error", "failed_payment_id",
        "failure_error_code", "design_version", "recovered",
    ]
    X = df.drop(columns=[c for c in drop_cols if c in df.columns], errors="ignore")

    if "created_at" in X.columns:
        dt = pd.to_datetime(X["created_at"], utc=True)
        X["hour"] = dt.dt.hour
        X["day_of_week"] = dt.dt.dayofweek
        X["day_of_month"] = dt.dt.day
        X["minute"] = dt.dt.minute
        X = X.drop(columns=["created_at"])

    cat_cols = X.select_dtypes(include=["object"]).columns.tolist()
    num_cols = X.select_dtypes(include=["number"]).columns.tolist()
    if "retry_prior" in X.columns:
        X["retry_prior"] = X["retry_prior"].fillna(0.0)

    return X, y, cat_cols, num_cols


def make_preprocessors(cat_cols, num_cols):
    scaler_ct = ColumnTransformer([
        ("num", Pipeline([("imputer", SimpleImputer(strategy="median")),
                          ("scaler", StandardScaler())]), num_cols),
        ("cat", Pipeline([("imputer", SimpleImputer(strategy="constant", fill_value="missing")),
                          ("ohe", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), cat_cols),
    ], remainder="drop")

    tree_ct = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), num_cols),
        ("cat", Pipeline([("imputer", SimpleImputer(strategy="constant", fill_value="missing")),
                          ("ohe", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), cat_cols),
    ], remainder="drop")

    return scaler_ct, tree_ct


# ---------- Model grids ----------

def get_grids(scaler_ct, tree_ct):
    grids = {}

    # 1. Extra Trees
    grids["Extra Trees"] = {
        "pipeline": Pipeline([("prep", tree_ct), ("clf", ExtraTreesClassifier(random_state=RANDOM_SEED))]),
        "param_grid": {
            "clf__n_estimators": [100, 200, 300, 500],
            "clf__max_depth": [4, 6, 8, 10, None],
            "clf__min_samples_split": [2, 5, 10],
            "clf__min_samples_leaf": [1, 2, 4],
            "clf__max_features": ["sqrt", "log2", None],
            "clf__class_weight": ["balanced", None],
        },
        "search": "random",
        "n_iter": 200,
    }

    # 2. Random Forest
    grids["Random Forest"] = {
        "pipeline": Pipeline([("prep", tree_ct), ("clf", RandomForestClassifier(random_state=RANDOM_SEED))]),
        "param_grid": {
            "clf__n_estimators": [100, 200, 300, 500],
            "clf__max_depth": [4, 6, 8, 10, None],
            "clf__min_samples_split": [2, 5, 10],
            "clf__min_samples_leaf": [1, 2, 4],
            "clf__max_features": ["sqrt", "log2", None],
            "clf__class_weight": ["balanced", None],
        },
        "search": "random",
        "n_iter": 200,
    }

    # 3. HistGradientBoosting
    grids["HistGBoost"] = {
        "pipeline": Pipeline([("prep", tree_ct), ("clf", HistGradientBoostingClassifier(random_state=RANDOM_SEED))]),
        "param_grid": {
            "clf__max_iter": [100, 200, 300],
            "clf__learning_rate": [0.01, 0.05, 0.1, 0.2],
            "clf__max_depth": [3, 5, 7, None],
            "clf__min_samples_leaf": [5, 10, 20],
            "clf__max_leaf_nodes": [15, 31, 63],
            "clf__l2_regularization": [0.0, 0.1, 1.0],
            "clf__class_weight": ["balanced", None],
        },
        "search": "random",
        "n_iter": 200,
    }

    # 4. Linear SVM (best accuracy/F1 from comparison)
    grids["Linear SVM"] = {
        "pipeline": Pipeline([("prep", scaler_ct), ("clf", SVC(probability=True, random_state=RANDOM_SEED))]),
        "param_grid": {
            "clf__C": [0.01, 0.1, 0.5, 1.0, 5.0, 10.0],
            "clf__class_weight": ["balanced", None],
        },
        "search": "grid",
    }

    # 5. RBF SVM
    grids["RBF SVM"] = {
        "pipeline": Pipeline([("prep", scaler_ct), ("clf", SVC(probability=True, random_state=RANDOM_SEED))]),
        "param_grid": {
            "clf__C": [0.1, 0.5, 1.0, 5.0, 10.0, 50.0],
            "clf__gamma": ["scale", "auto", 0.01, 0.05, 0.1, 0.5],
            "clf__class_weight": ["balanced", None],
        },
        "search": "random",
        "n_iter": 100,
    }

    return grids


# ---------- Run tuning ----------

def tune_model(name, config, X, y, cv):
    print(f"\n{'='*70}")
    print(f"TUNING: {name}")
    print(f"{'='*70}")

    if config["search"] == "grid":
        search = GridSearchCV(
            config["pipeline"],
            config["param_grid"],
            cv=cv,
            scoring="roc_auc",
            n_jobs=-1,
            refit=True,
            verbose=0,
        )
    else:
        search = RandomizedSearchCV(
            config["pipeline"],
            config["param_grid"],
            n_iter=config.get("n_iter", 100),
            cv=cv,
            scoring="roc_auc",
            n_jobs=-1,
            refit=True,
            random_state=RANDOM_SEED,
            verbose=0,
        )

    search.fit(X, y)

    best = search.best_params_
    best_score = search.best_score_
    cv_results = search.cv_results_

    # Extract mean/std for top 5
    rank_idx = cv_results["rank_test_score"].argsort()[:5]
    top5 = []
    for i in rank_idx:
        top5.append({
            "rank": int(cv_results["rank_test_score"][i]),
            "params": {k.replace("clf__", ""): v for k, v in cv_results["params"][i].items()},
            "auc_mean": round(float(cv_results["mean_test_score"][i]), 4),
            "auc_std": round(float(cv_results["std_test_score"][i]), 4),
        })

    print(f"\nBest AUC: {best_score:.4f} ± {search.cv_results_['std_test_score'][search.best_index_]:.4f}")
    print(f"Best params:")
    for k, v in best.items():
        print(f"  {k.replace('clf__', '')}: {v}")
    print(f"\nTop 5 configurations:")
    for t in top5:
        print(f"  #{t['rank']}  AUC={t['auc_mean']:.4f}±{t['auc_std']:.4f}  {t['params']}")

    return {
        "model": name,
        "best_params": {k.replace("clf__", ""): v for k, v in best.items()},
        "best_auc_mean": round(best_score, 4),
        "best_auc_std": round(float(cv_results["std_test_score"][search.best_index_]), 4),
        "top5": top5,
        "total_configs": len(cv_results["rank_test_score"]),
    }


def main():
    print("=" * 70)
    print("MANDATE DOCTOR — HYPERPARAMETER TUNING (TOP 5 MODELS)")
    print("=" * 70)

    df = load_data()
    X, y, cat_cols, num_cols = build_features(df)
    print(f"Dataset: {len(df)} rows, {X.shape[1]} features")
    print(f"Class balance: {y.mean():.1%} recovered ({y.sum()}/{len(y)})")

    scaler_ct, tree_ct = make_preprocessors(cat_cols, num_cols)
    grids = get_grids(scaler_ct, tree_ct)
    cv = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=RANDOM_SEED)

    results = []
    for name, config in grids.items():
        result = tune_model(name, config, X, y, cv)
        results.append(result)

    # Sort by best AUC
    results.sort(key=lambda r: r["best_auc_mean"], reverse=True)

    print("\n" + "=" * 70)
    print("FINAL RANKING (TUNED)")
    print("=" * 70)
    print(f"{'RANK':<5} {'MODEL':<20} {'AUC':>10} {'STD':>8} {'CONFIGS':>8}")
    print("-" * 70)
    for i, r in enumerate(results, 1):
        print(f"{i:<5} {r['model']:<20} {r['best_auc_mean']:.4f}    {r['best_auc_std']:.4f}  {r['total_configs']:>6}")

    # Save
    out_path = Path("models/tuning_results.json")
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
