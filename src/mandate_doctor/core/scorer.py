"""ML Scorer — loads the trained sklearn pipeline and predicts P(recovery).

Used by the recovery pipeline to replace hardcoded rules with
data-driven decisions. Falls back gracefully when no model exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import structlog

logger = structlog.get_logger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "models"


class MLScorer:
    """Loads the fitted sklearn pipeline and scores recovery probability.

    Usage:
        scorer = MLScorer()
        score = scorer.score({
            "npci_bank": "Canara Bank",
            "error_class": "bd",
            "amount_paise": 19900,
            "regime": "optimistic",
            "retry_prior": 0.271,
        })
        # score = 0.82 (P(recovery))
    """

    def __init__(self, model_dir: Path | None = None) -> None:
        self._dir = model_dir or MODEL_DIR
        self._pipeline = None
        self._metadata: dict[str, Any] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        pipeline_path = self._dir / "recovery_pipeline.joblib"
        json_path = self._dir / "recovery_model.json"
        if not pipeline_path.exists():
            logger.warning("no_pipeline", path=str(pipeline_path))
            self._loaded = True
            return
        try:
            self._pipeline = joblib.load(pipeline_path)
            if json_path.exists():
                self._metadata = json.loads(json_path.read_text())
            self._loaded = True
            logger.info("pipeline_loaded", path=str(pipeline_path))
        except (OSError, ValueError, KeyError) as exc:
            logger.error("pipeline_load_failed", path=str(pipeline_path), error=str(exc))
            self._loaded = True

    def score(self, features: dict[str, Any]) -> float | None:
        """Return P(recovery) for a single failed payment.

        Args:
            features: dict with keys matching the training columns:
                npci_bank, error_class, amount_paise, regime, retry_prior

        Returns:
            Probability in [0, 1], or None if no model available.
        """
        self._ensure_loaded()
        if self._pipeline is None:
            return None

        import pandas as pd  # type: ignore[import-untyped]
        row = pd.DataFrame([{
            "amount_paise": features.get("amount_paise", 0),
            "retry_prior": features.get("retry_prior", 0.0),
            "npci_bank": features.get("npci_bank", "unknown"),
            "error_class": features.get("error_class", "unknown"),
            "regime": features.get("regime", "base"),
        }])

        try:
            proba = self._pipeline.predict_proba(row)
            return float(proba[0, 1])
        except (ValueError, KeyError, IndexError) as exc:
            logger.error("score_failed", error=str(exc))
            return None

    def score_batch(self, rows: list[dict[str, Any]]) -> list[float | None]:
        """Score multiple rows."""
        return [self.score(r) for r in rows]

    @property
    def metrics(self) -> dict[str, Any] | None:
        self._ensure_loaded()
        return self._metadata.get("metrics")

    @property
    def is_available(self) -> bool:
        self._ensure_loaded()
        return self._pipeline is not None
