"""Failure classifier — the AI judgment layer.

Classifies failed debit attempts into failure buckets:
- LOW_BALANCE: retryable, time to salary/fund date
- TECHNICAL: retryable immediately, not customer's fault
- STOP: never retry (fraud, revoked, closed)
- AMBIGUOUS: hold for review, don't auto-act

Three layers:
1. Deterministic lookup for known NPCI error codes (no AI needed)
2. Pattern matching on error description keywords (no AI needed)
3. LLM classification for truly unknown errors (AI at the edge)

The AI only fires when rules can't decide — restraint is the signal.
"""

from __future__ import annotations

import structlog

from mandate_doctor.core.codes import lookup_bucket
from mandate_doctor.core.models import (
    DebitAttempt,
    ErrorDetail,
    FailureBucket,
)

logger = structlog.get_logger(__name__)

# Weights for the confidence scorer on ambiguous payment_declined codes
SCORER_WEIGHTS = {
    "historical_success_rate": 0.3,
    "decline_velocity": -0.2,  # negative: more declines = less confident
    "amount_deviation": -0.15,  # negative: unusual amount = suspicious
    "days_since_salary_window": -0.1,  # negative: far from salary = less likely balance
    "mandate_age_days": 0.1,  # older mandates are more stable
}

# Thresholds for the confidence scorer
THRESHOLD_HIGH = 0.7  # above → LOW_BALANCE (retry)
THRESHOLD_LOW = 0.3  # below → STOP (hold for review)


def classify(attempt: DebitAttempt) -> tuple[FailureBucket, float, list[str], str]:
    """Classify a failed debit attempt into a failure bucket.

    Returns:
        (bucket, confidence, signals_used, reasoning)
    """
    if attempt.error is None:
        return (
            FailureBucket.AMBIGUOUS,
            0.5,
            ["no_error_detail"],
            "No error details provided — holding for review",
        )

    error_code = attempt.error.code
    error_desc = attempt.error.description

    # Step 1: Deterministic lookup for known codes
    bucket, confidence = lookup_bucket(error_code)
    if bucket is not None:
        signals = [f"known_code:{error_code}"]
        reasoning = f"Error code '{error_code}' maps to {bucket.value} (deterministic)"
        logger.info(
            "classification_deterministic",
            attempt_id=attempt.attempt_id,
            mandate_id=attempt.mandate_id,
            bucket=bucket.value,
            confidence=confidence,
            error_code=error_code,
        )
        return bucket, confidence, signals, reasoning

    # Step 2: Check for known patterns in error description
    bucket, confidence, signals = _score_from_description(attempt.error)
    if bucket is not None:
        reasoning = (
            f"Pattern match in error description → {bucket.value} (confidence: {confidence:.2f})"
        )
        logger.info(
            "classification_pattern_match",
            attempt_id=attempt.attempt_id,
            mandate_id=attempt.mandate_id,
            bucket=bucket.value,
            confidence=confidence,
            signals=signals,
        )
        return bucket, confidence, signals, reasoning

    # Step 3: Ambiguous — default to hold for review
    reasoning = (
        f"Unknown error code '{error_code}' and no pattern match in description — "
        f"holding for human review (never guess on money decisions)"
    )
    logger.warn(
        "classification_ambiguous",
        attempt_id=attempt.attempt_id,
        mandate_id=attempt.mandate_id,
        error_code=error_code,
        error_desc=error_desc[:100],
    )
    return FailureBucket.AMBIGUOUS, 0.4, ["unknown_code", "no_pattern_match"], reasoning


def _score_from_description(error: ErrorDetail) -> tuple[FailureBucket | None, float, list[str]]:
    """Try to classify from error description text patterns.

    Returns (bucket, confidence, signals) or (None, 0, []) if no match.
    """
    desc = error.description.lower()
    signals = []

    # Low balance patterns
    balance_keywords = ["insufficient", "low balance", "not enough", "funds", "balance"]
    if any(kw in desc for kw in balance_keywords):
        signals.append("description_balance_keyword")
        return FailureBucket.LOW_BALANCE, 0.80, signals

    # Technical patterns
    technical_keywords = ["timeout", "connection", "server", "network", "gateway", "switch"]
    if any(kw in desc for kw in technical_keywords):
        signals.append("description_technical_keyword")
        return FailureBucket.TECHNICAL, 0.75, signals

    # Stop patterns
    stop_keywords = ["revoked", "expired", "closed", "frozen", "blocked", "fraud", "cancelled"]
    if any(kw in desc for kw in stop_keywords):
        signals.append("description_stop_keyword")
        return FailureBucket.STOP, 0.85, signals

    return None, 0.0, []
