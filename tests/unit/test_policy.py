"""Tests for the policy engine and retry budget."""

from mandate_doctor.core.models import (
    Action,
    DebitAttempt,
    ErrorDetail,
    FailureBucket,
)
from mandate_doctor.core.policy import RetryBudget, decide


def _make_attempt(mandate_id: str = "md_test") -> DebitAttempt:
    """Helper to create a test DebitAttempt."""
    return DebitAttempt(
        attempt_id="att_test",
        mandate_id=mandate_id,
        amount=50000,
        error=ErrorDetail(code="insufficient_funds", description="low balance"),
    )


class TestRetryBudget:
    """Tests for the RetryBudget class."""

    def test_new_budget_has_full_capacity(self):
        budget = RetryBudget(max_attempts=4)
        assert budget.remaining("md_test") == 4

    def test_consume_reduces_remaining(self):
        budget = RetryBudget(max_attempts=4)
        assert budget.consume("md_test") is True
        assert budget.remaining("md_test") == 3

    def test_consume_exhausted_returns_false(self):
        budget = RetryBudget(max_attempts=2)
        budget.consume("md_test")
        budget.consume("md_test")
        assert budget.consume("md_test") is False
        assert budget.remaining("md_test") == 0

    def test_reset_restores_budget(self):
        budget = RetryBudget(max_attempts=4)
        budget.consume("md_test")
        budget.consume("md_test")
        budget.reset("md_test")
        assert budget.remaining("md_test") == 4

    def test_different_mandates_have_independent_budgets(self):
        budget = RetryBudget(max_attempts=4)
        budget.consume("md_a")
        assert budget.remaining("md_a") == 3
        assert budget.remaining("md_b") == 4


class TestDecisions:
    """Tests for the decide function."""

    def test_low_balance_schedules_retry(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test")
        assert decision.action_taken == Action.SCHEDULE_RETRY
        assert decision.retry_budget_remaining == 3  # consumed 1

    def test_technical_retries_immediately(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.TECHNICAL, 0.90, ["test"], "test")
        assert decision.action_taken == Action.RETRY_IMMEDIATELY

    def test_stop_triggers_reconsent(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.STOP, 0.98, ["test"], "test")
        assert decision.action_taken == Action.TRIGGER_RECONSENT
        assert decision.retry_budget_remaining == 4  # not consumed

    def test_ambiguous_holds_for_review(self):
        attempt = _make_attempt()
        decision = decide(attempt, FailureBucket.AMBIGUOUS, 0.4, ["test"], "test")
        assert decision.action_taken == Action.HOLD_FOR_REVIEW
        assert decision.retry_budget_remaining == 4  # not consumed

    def test_budget_exhausted_holds_for_review(self):
        budget = RetryBudget(max_attempts=1)
        # Monkey-patch the global budget for this test
        import mandate_doctor.core.policy as policy_module

        original = policy_module.retry_budget
        policy_module.retry_budget = budget

        try:
            attempt = _make_attempt()
            # First attempt consumes budget
            decide(attempt, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test")
            # Second attempt should be held despite being LOW_BALANCE
            decision = decide(attempt, FailureBucket.LOW_BALANCE, 0.95, ["test"], "test")
            assert decision.action_taken == Action.HOLD_FOR_REVIEW
            assert "budget exhausted" in decision.reasoning.lower()
        finally:
            policy_module.retry_budget = original
