"""Policy engine — the bounded/gated decision layer.

Enforces NPCI's 1+3 retry cap per mandate per cycle.
Maps failure buckets to actions with timing and escalation rules.

This is the fail-safe: even if the classifier is wrong,
the policy engine prevents retry-budget violations.
"""

from __future__ import annotations

import structlog

from mandate_doctor.core.models import (
    Action,
    Decision,
    DebitAttempt,
    FailureBucket,
)

logger = structlog.get_logger(__name__)

# NPCI cap: 1 original attempt + max 3 retries per cycle
MAX_ATTEMPTS_PER_CYCLE = 4


class RetryBudget:
    """Tracks retry attempts per mandate per cycle.

    Enforces NPCI's hard cap regardless of classifier confidence.
    """

    def __init__(self, max_attempts: int = MAX_ATTEMPTS_PER_CYCLE):
        self._max = max_attempts
        self._used: dict[str, int] = {}  # mandate_id → attempts used

    def remaining(self, mandate_id: str) -> int:
        """How many attempts remain for this mandate in the current cycle."""
        used = self._used.get(mandate_id, 0)
        return max(0, self._max - used)

    def consume(self, mandate_id: str) -> bool:
        """Consume one attempt. Returns False if budget exhausted."""
        if self.remaining(mandate_id) <= 0:
            return False
        self._used[mandate_id] = self._used.get(mandate_id, 0) + 1
        return True

    def reset(self, mandate_id: str) -> None:
        """Reset budget for a new cycle."""
        self._used.pop(mandate_id, None)


# Global retry budget instance
retry_budget = RetryBudget()


def decide(
    attempt: DebitAttempt,
    bucket: FailureBucket,
    confidence: float,
    signals: list[str],
    reasoning: str,
) -> Decision:
    """Make a decision based on the classification and retry budget.

    Returns a Decision with action, reasoning, and remaining budget.
    """
    mandate_id = attempt.mandate_id
    remaining = retry_budget.remaining(mandate_id)

    # Fail-safe: if budget is exhausted, no action regardless of bucket
    if remaining <= 0:
        logger.warn(
            "budget_exhausted",
            mandate_id=mandate_id,
            attempt_id=attempt.attempt_id,
            bucket=bucket.value,
        )
        return Decision(
            attempt_id=attempt.attempt_id,
            mandate_id=mandate_id,
            bucket=bucket,
            confidence=confidence,
            signals_used=signals,
            action_taken=Action.HOLD_FOR_REVIEW,
            reasoning=f"{reasoning} | Budget exhausted ({MAX_ATTEMPTS_PER_CYCLE}/{MAX_ATTEMPTS_PER_CYCLE} used)",
            retry_budget_remaining=0,
        )

    # Map bucket → action
    action = _bucket_to_action(bucket)

    # Consume budget for retry actions
    if action in (Action.SCHEDULE_RETRY, Action.RETRY_IMMEDIATELY):
        retry_budget.consume(mandate_id)
        remaining = retry_budget.remaining(mandate_id)

    decision = Decision(
        attempt_id=attempt.attempt_id,
        mandate_id=mandate_id,
        bucket=bucket,
        confidence=confidence,
        signals_used=signals,
        action_taken=action,
        reasoning=reasoning,
        retry_budget_remaining=remaining,
    )

    logger.info(
        "decision_made",
        mandate_id=mandate_id,
        attempt_id=attempt.attempt_id,
        bucket=bucket.value,
        action=action.value,
        confidence=confidence,
        budget_remaining=remaining,
    )

    return decision


def _bucket_to_action(bucket: FailureBucket) -> Action:
    """Map a failure bucket to the appropriate action."""
    match bucket:
        case FailureBucket.LOW_BALANCE:
            return Action.SCHEDULE_RETRY
        case FailureBucket.TECHNICAL:
            return Action.RETRY_IMMEDIATELY
        case FailureBucket.STOP:
            return Action.TRIGGER_RECONSENT
        case FailureBucket.AMBIGUOUS:
            return Action.HOLD_FOR_REVIEW
