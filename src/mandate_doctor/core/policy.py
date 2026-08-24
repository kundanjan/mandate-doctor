"""Policy engine — the bounded/gated decision layer.

Enforces NPCI's retry cap per mandate per cycle.

Regulatory basis: NPCI Circular OC-215A/2025-26, dated 21 May 2025,
effective 1 Aug 2025. Verbatim rule: "Maximum of 1 attempt and 3 retries
per mandate (per sequence number) shall be permitted."

The original attempt is implied by the failure event itself — the system
only decides what happens AFTER that original attempt failed. Therefore
the budget tracks retries only: exactly 3 retries are allowed after the
original attempt. A fourth retry must be rejected.

This is the fail-safe: even if the classifier is wrong, the policy
engine prevents retry-budget violations.
"""

from __future__ import annotations

import structlog

from mandate_doctor.core.models import (
    Action,
    DebitAttempt,
    Decision,
    FailureBucket,
)

logger = structlog.get_logger(__name__)

# NPCI OC-215A: 1 original attempt + 3 retries per mandate per cycle.
# The original attempt already happened (that is why we are deciding),
# so this constant is the number of RETRIES permitted after it.
MAX_RETRIES_AFTER_ORIGINAL = 3


class RetryBudget:
    """Tracks retries consumed per (mandate, cycle).

    Keyed by (mandate_id, cycle_id) because the NPCI cap resets each
    billing cycle. The original attempt is not counted here — it is
    the event that brought the system to a decision in the first place.
    """

    def __init__(self, max_retries: int = MAX_RETRIES_AFTER_ORIGINAL):
        self._max_retries = max_retries
        self._used: dict[tuple[str, str], int] = {}

    def remaining_retries(self, mandate_id: str, cycle_id: str) -> int:
        """Retries still permitted for this mandate in this cycle."""
        used = self._used.get((mandate_id, cycle_id), 0)
        return max(0, self._max_retries - used)

    def consume_retry(self, mandate_id: str, cycle_id: str) -> bool:
        """Consume one retry slot. Returns False if exhausted."""
        if self.remaining_retries(mandate_id, cycle_id) <= 0:
            return False
        self._used[(mandate_id, cycle_id)] = self._used.get((mandate_id, cycle_id), 0) + 1
        return True

    def reset_cycle(self, mandate_id: str, cycle_id: str) -> None:
        """Clear the budget for a fresh billing cycle."""
        self._used.pop((mandate_id, cycle_id), None)


# Global retry budget instance
retry_budget = RetryBudget()


def reset_budget() -> None:
    """Reset the global retry budget. Used in tests and new cycles."""
    global retry_budget
    retry_budget = RetryBudget()


def decide(
    attempt: DebitAttempt,
    bucket: FailureBucket,
    confidence: float,
    signals: list[str],
    reasoning: str,
) -> Decision:
    """Make a decision based on the classification and retry budget.

    Returns a Decision with action, reasoning, and remaining retries.
    """
    mandate_id = attempt.mandate_id
    cycle_id = attempt.cycle_id
    remaining = retry_budget.remaining_retries(mandate_id, cycle_id)

    # Fail-safe: if no retries remain, hold regardless of bucket.
    if remaining <= 0:
        logger.warn(
            "budget_exhausted",
            mandate_id=mandate_id,
            cycle_id=cycle_id,
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
            reasoning=(
                f"{reasoning} | Retry budget exhausted "
                f"({MAX_RETRIES_AFTER_ORIGINAL}/{MAX_RETRIES_AFTER_ORIGINAL} "
                "retries used after original attempt)"
            ),
            retry_budget_remaining=0,
        )

    # Map bucket to action.
    action = _bucket_to_action(bucket)

    # Consume a retry slot only for retry actions.
    if action in (Action.SCHEDULE_RETRY, Action.RETRY_IMMEDIATELY):
        retry_budget.consume_retry(mandate_id, cycle_id)
        remaining = retry_budget.remaining_retries(mandate_id, cycle_id)

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
        cycle_id=cycle_id,
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
