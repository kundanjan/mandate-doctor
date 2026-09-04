#!/usr/bin/env python3
"""Comprehensive ML model comparison for Mandate Doctor recovery prediction.

Reads v2 training data, preprocesses features, and evaluates 25+ models
with proper cross-validation and leakage prevention.
"""

import json
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.decomposition import PCA
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis, QuadraticDiscriminantAnalysis
from sklearn.ensemble import (
    AdaBoostClassifier,
    ExtraTreesClassifier,
    GradientBoostingClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    f1_score,
    make_scorer,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_validate
from sklearn.naive_bayes import BernoulliNB, GaussianNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier

warnings.filterwarnings("ignore")

DB_PATH = Path("data/training_data.db")
RANDOM_SEED = 42
CV_FOLDS = 5

# ---------- Load data ----------

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


# ---------- Feature engineering ----------

def build_features(df):
    """Build X, y, feature names, and preprocessing info."""
    y = df["recovered"].values.astype(int)

    # Drop identifiers and outcome-leaking columns (including recovered = target)
    drop_cols = [
        "scenario_key", "order_id", "plink_id", "short_url",
        "assigned_click", "poll_status", "error", "failed_payment_id",
        "failure_error_code", "design_version", "recovered",
    ]
    X = df.drop(columns=[c for c in drop_cols if c in df.columns], errors="ignore")

    # Parse created_at into temporal features
    if "created_at" in X.columns:
        dt = pd.to_datetime(X["created_at"], utc=True)
        X["hour"] = dt.dt.hour
        X["day_of_week"] = dt.dt.dayofweek
        X["day_of_month"] = dt.dt.day
        X["minute"] = dt.dt.minute
        X = X.drop(columns=["created_at"])

    # Identify column types
    cat_cols = X.select_dtypes(include=["object"]).columns.tolist()
    num_cols = X.select_dtypes(include=["number"]).columns.tolist()

    # Fill NaN in retry_prior
    if "retry_prior" in X.columns:
        X["retry_prior"] = X["retry_prior"].fillna(0.0)

    return X, y, cat_cols, num_cols


# ---------- Build pipelines ----------

def make_pipelines(cat_cols, num_cols):
    """Create all model pipelines with proper preprocessing."""

    # Preprocessor for models that need scaling
    scaler_ct = ColumnTransformer([
        ("num", Pipeline([("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler())]), num_cols),
        ("cat", Pipeline([("imputer", SimpleImputer(strategy="constant", fill_value="missing")),
                          ("ohe", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), cat_cols),
    ], remainder="drop")

    # Preprocessor for tree models (no scaling needed)
    tree_ct = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), num_cols),
        ("cat", Pipeline([("imputer", SimpleImputer(strategy="constant", fill_value="missing")),
                          ("ohe", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), cat_cols),
    ], remainder="drop")

    models = {}

    # === Linear / Logistic ===
    models["LogReg baseline"] = Pipeline([("prep", scaler_ct), ("clf", LogisticRegression(max_iter=2000, random_state=RANDOM_SEED))])
    models["LogReg L1"] = Pipeline([("prep", scaler_ct), ("clf", LogisticRegression(penalty="l1", solver="liblinear", max_iter=2000, random_state=RANDOM_SEED))])
    models["LogReg L2"] = Pipeline([("prep", scaler_ct), ("clf", LogisticRegression(penalty="l2", C=0.5, max_iter=2000, random_state=RANDOM_SEED))])
    models["LogReg elastic"] = Pipeline([("prep", scaler_ct), ("clf", LogisticRegression(penalty="elasticnet", solver="saga", l1_ratio=0.5, max_iter=2000, random_state=RANDOM_SEED))])
    models["SGD logloss"] = Pipeline([("prep", scaler_ct), ("clf", SGDClassifier(loss="log_loss", max_iter=2000, random_state=RANDOM_SEED))])

    # === LDA / QDA ===
    models["LDA"] = Pipeline([("prep", scaler_ct), ("clf", LinearDiscriminantAnalysis())])
    models["QDA"] = Pipeline([("prep", scaler_ct), ("clf", QuadraticDiscriminantAnalysis())])

    # === KNN ===
    for k in [3, 5, 7, 11]:
        models[f"KNN k={k}"] = Pipeline([("prep", scaler_ct), ("clf", KNeighborsClassifier(n_neighbors=k))])

    # === Naive Bayes ===
    models["Gaussian NB"] = Pipeline([("prep", scaler_ct), ("clf", GaussianNB())])
    models["Bernoulli NB"] = Pipeline([("prep", scaler_ct), ("clf", BernoulliNB())])

    # === Tree-based ===
    models["Decision Tree"] = Pipeline([("prep", tree_ct), ("clf", DecisionTreeClassifier(max_depth=6, random_state=RANDOM_SEED))])
    models["Random Forest 100"] = Pipeline([("prep", tree_ct), ("clf", RandomForestClassifier(n_estimators=100, max_depth=6, random_state=RANDOM_SEED))])
    models["Random Forest 300"] = Pipeline([("prep", tree_ct), ("clf", RandomForestClassifier(n_estimators=300, max_depth=5, random_state=RANDOM_SEED))])
    models["Extra Trees 100"] = Pipeline([("prep", tree_ct), ("clf", ExtraTreesClassifier(n_estimators=100, max_depth=6, random_state=RANDOM_SEED))])
    models["GBoost 100"] = Pipeline([("prep", tree_ct), ("clf", GradientBoostingClassifier(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=RANDOM_SEED))])
    models["GBoost 200"] = Pipeline([("prep", tree_ct), ("clf", GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=RANDOM_SEED))])
    models["HistGBoost 100"] = Pipeline([("prep", tree_ct), ("clf", HistGradientBoostingClassifier(max_iter=100, max_depth=3, random_state=RANDOM_SEED))])
    models["HistGBoost 200"] = Pipeline([("prep", tree_ct), ("clf", HistGradientBoostingClassifier(max_iter=200, max_depth=3, learning_rate=0.05, random_state=RANDOM_SEED))])
    models["AdaBoost 50"] = Pipeline([("prep", tree_ct), ("clf", AdaBoostClassifier(n_estimators=50, random_state=RANDOM_SEED))])
    models["AdaBoost 100"] = Pipeline([("prep", tree_ct), ("clf", AdaBoostClassifier(n_estimators=100, random_state=RANDOM_SEED))])

    # === SVM ===
    models["Linear SVM"] = Pipeline([("prep", scaler_ct), ("clf", SVC(kernel="linear", probability=True, random_state=RANDOM_SEED))])
    models["RBF SVM"] = Pipeline([("prep", scaler_ct), ("clf", SVC(kernel="rbf", probability=True, random_state=RANDOM_SEED))])
    models["Poly SVM"] = Pipeline([("prep", scaler_ct), ("clf", SVC(kernel="poly", degree=3, probability=True, random_state=RANDOM_SEED))])
    models["Sigmoid SVM"] = Pipeline([("prep", scaler_ct), ("clf", SVC(kernel="sigmoid", probability=True, random_state=RANDOM_SEED))])

    # === PCA pipelines ===
    for n in [5, 8, 10]:
        pca = Pipeline([("prep", scaler_ct), ("pca", PCA(n_components=n))])
        models[f"PCA({n}) → LogReg"] = Pipeline([("pca_step", pca), ("clf", LogisticRegression(max_iter=2000, random_state=RANDOM_SEED))])
        models[f"PCA({n}) → LDA"] = Pipeline([("pca_step", pca), ("clf", LinearDiscriminantAnalysis())])
        models[f"PCA({n}) → KNN5"] = Pipeline([("pca_step", pca), ("clf", KNeighborsClassifier(n_neighbors=5))])
        models[f"PCA({n}) → RF"] = Pipeline([("pca_step", pca), ("clf", RandomForestClassifier(n_estimators=100, max_depth=5, random_state=RANDOM_SEED))])
        models[f"PCA({n}) → SVM"] = Pipeline([("pca_step", pca), ("clf", SVC(kernel="rbf", probability=True, random_state=RANDOM_SEED))])

    # === LDA pipeline (explicit) ===
    models["LDA → LogReg"] = Pipeline([("prep", scaler_ct), ("lda", LinearDiscriminantAnalysis()), ("clf", LogisticRegression(max_iter=2000, random_state=RANDOM_SEED))])

    return models


# ---------- Evaluation ----------

def evaluate_all(X, y, models):
    cv = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=RANDOM_SEED)
    base_rate = y.mean()

    scoring = {
        "accuracy": "accuracy",
        "precision": make_scorer(precision_score, zero_division=0),
        "recall": make_scorer(recall_score, zero_division=0),
        "f1": make_scorer(f1_score, zero_division=0),
        "roc_auc": "roc_auc",
        "pr_auc": make_scorer(average_precision_score),
    }

    results = []
    for name, pipeline in models.items():
        try:
            scores = cross_validate(pipeline, X, y, cv=cv, scoring=scoring, error_score=np.nan)
            row = {
                "model": name,
                "accuracy": np.nanmean(scores["test_accuracy"]),
                "accuracy_std": np.nanstd(scores["test_accuracy"]),
                "precision": np.nanmean(scores["test_precision"]),
                "recall": np.nanmean(scores["test_recall"]),
                "f1": np.nanmean(scores["test_f1"]),
                "roc_auc": np.nanmean(scores["test_roc_auc"]),
                "pr_auc": np.nanmean(scores["test_pr_auc"]),
            }
            results.append(row)
            print(f"  ✓ {name:35} acc={row['accuracy']:.1%} f1={row['f1']:.3f} auc={row['roc_auc']:.3f} pr={row['pr_auc']:.3f}")
        except Exception as e:
            print(f"  ✗ {name:35} FAILED: {e}")
            results.append({"model": name, "accuracy": 0, "f1": 0, "roc_auc": 0, "pr_auc": 0, "error": str(e)})

    results.sort(key=lambda r: r.get("roc_auc", 0), reverse=True)
    return results, base_rate


# ---------- Main ----------

def main():
    print("=" * 80)
    print("MANDATE DOCTOR — COMPREHENSIVE ML MODEL COMPARISON")
    print("=" * 80)

    df = load_data()
    X, y, cat_cols, num_cols = build_features(df)
    print(f"\nDataset: {len(df)} rows, {X.shape[1]} features")
    print(f"Categorical: {cat_cols}")
    print(f"Numerical: {num_cols}")
    print(f"Class balance: {y.mean():.1%} recovered ({y.sum()}/{len(y)})")

    models = make_pipelines(cat_cols, num_cols)
    print(f"\nEvaluating {len(models)} models with {CV_FOLDS}-fold stratified CV...\n")

    results, base_rate = evaluate_all(X, y, models)

    # Save results
    print("\n" + "=" * 80)
    print(f"{'RANK':<5} {'MODEL':<35} {'ACC':>6} {'F1':>6} {'AUC':>6} {'PR-AUC':>6} {'LIFT':>6}")
    print("=" * 80)
    for i, r in enumerate(results, 1):
        lift = r["accuracy"] - base_rate
        print(f"{i:<5} {r['model']:<35} {r['accuracy']:.1%}  {r['f1']:.3f}  {r['roc_auc']:.3f}  {r['pr_auc']:.3f}  {lift:+.1%}")

    print(f"\nBase rate: {base_rate:.1%}")
    print(f"Best model: {results[0]['model']} (AUC={results[0]['roc_auc']:.3f})")

    # Save to JSON
    out_path = Path("models/model_comparison.json")
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
