"""Tests for the policy engine and retry budget."""

from mandate_doctor.core.models import (
    Action,
    DebitAttempt,
    ErrorDetail,
    FailureBucket,
)
from mandate_doctor.core.policy import RetryBudget, decide


def _make_attempt(mandate_id: str = "md_test", cycle_id: str = "cycle_01") -> DebitAttempt:
    """Helper to create a test DebitAttempt."""
    return DebitAttempt(
        attempt_id="att_test",
        mandate_id=mandate_id,
        cycle_id=cycle_id,
        amount=50000,
        error=ErrorDetail(code="insufficient_funds", description="low balance"),
    )


class TestRetryBudget:
    """Tests for the RetryBudget class."""

    def test_new_budget_has_three_retries(self):
        budget = RetryBudget()
        assert budget.remaining_retries("md_test", "cycle_01") == 3

    def test_consume_reduces_remaining(self):
        budget = RetryBudget()
        assert budget.consume_retry("md_test", "cycle_01") is True
        assert budget.remaining_retries("md_test", "cycle_01") == 2

    def test_consume_exhausted_returns_false(self):
        budget = RetryBudget(max_retries=1)
        assert budget.consume_retry("md_test", "cycle_01") is True
        assert budget.consume_retry("md_test", "cycle_01") is False
        assert budget.remaining_retries("md_test", "cycle_01") == 0

    def test_reset_cycle_restores_budget(self):
        budget = RetryBudget()
        budget.consume_retry("md_test", "cycle_01")
        budget.consume_retry("md_test", "cycle_01")
        budget.reset_cycle("md_test", "cycle_01")
        assert budget.remaining_retries("md_test", "cycle_01") == 3

    def test_budget_scoped_per_cycle(self):
        budget = RetryBudget()
        budget.consume_retry("md_test", "cycle_01")
        budget.consume_retry("md_test", "cycle_01")
        assert budget.remaining_retries("md_test", "cycle_01") == 1
        assert budget.remaining_retries("md_test", "cycle_02") == 3

    def test_different_mandates_have_independent_budgets(self):
        budget = RetryBudget()
        budget.consume_retry("md_a", "cycle_01")
        assert budget.remaining_retries("md_a", "cycle_01") == 2
        assert budget.remaining_retries("md_b", "cycle_01") == 3


class TestDecisions:
    """Tests for the decide function."""

    def test_low_balance_schedules_retry(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test")
        assert decision.action_taken == Action.SCHEDULE_RETRY
        assert decision.retry_budget_remaining == 2  # consumed 1 of 3

    def test_technical_retries_immediately(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.TECHNICAL, 0.90, ["test"], "test")
        assert decision.action_taken == Action.RETRY_IMMEDIATELY

    def test_stop_triggers_reconsent(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.STOP, 0.98, ["test"], "test")
        assert decision.action_taken == Action.TRIGGER_RECONSENT
        assert decision.retry_budget_remaining == 3  # not consumed

    def test_ambiguous_holds_for_review(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.AMBIGUOUS, 0.4, ["test"], "test")
        assert decision.action_taken == Action.HOLD_FOR_REVIEW
        assert decision.retry_budget_remaining == 3  # not consumed

    def test_exactly_three_retries_allowed_after_original(self):
        """NPCI OC-215A: 1 original attempt + 3 retries. The fourth retry
        request must be held, not executed."""
        attempt = _make_attempt()
        decisions = [
            decide(attempt, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test") for _ in range(4)
        ]
        actions = [d.action_taken for d in decisions]
        assert actions == [
            Action.SCHEDULE_RETRY,
            Action.SCHEDULE_RETRY,
            Action.SCHEDULE_RETRY,
            Action.HOLD_FOR_REVIEW,
        ]
        assert decisions[2].retry_budget_remaining == 0
        assert "budget exhausted" in decisions[3].reasoning.lower()

    def test_budget_scoped_by_cycle(self):
        """Retries consumed in one cycle must not leak into the next."""
        attempt_1 = _make_attempt(cycle_id="cycle_01")
        attempt_2 = _make_attempt(cycle_id="cycle_02")
        decide(attempt_1, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test")
        decide(attempt_1, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test")
        decision = decide(attempt_2, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test")
        assert decision.action_taken == Action.SCHEDULE_RETRY
        assert decision.retry_budget_remaining == 2
