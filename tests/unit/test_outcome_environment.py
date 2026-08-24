"""Tests for the independent outcome environment.

The critical invariants:
1. Potential outcomes are materialized for every action and day before
   any policy runs.
2. The table is shared and immutable — both arms read identical draws.
3. Hidden state exists only in the evaluator layer; policies receive
   only observed fields.
4. Terminal cases can never succeed on retry.
"""

from eval.generate_batch import generate_batch
from eval.outcome_environment import (
    ACTION_HOLD,
    ACTION_PAYMENT_LINK,
    ACTION_RECONSENT,
    ACTION_RETRY,
    HIDDEN_TERMINAL,
    HORIZON_DAYS,
    OutcomeTable,
    build_scenarios_from_attempts,
)


def _build_table(scenario: str = "balanced", size: int = 100) -> OutcomeTable:
    attempts = generate_batch(size=size, seed=42, scenario=scenario)
    scenarios = build_scenarios_from_attempts(attempts, profile=scenario)
    return OutcomeTable(scenarios)


class TestOutcomeMaterialization:
    """Every (scenario, action, day) must resolve to a pre-drawn outcome."""

    def test_all_combinations_materialized(self):
        table = _build_table(size=20)
        for scenario_id in table.scenario_ids():
            for action in (ACTION_RETRY, ACTION_PAYMENT_LINK, ACTION_RECONSENT, ACTION_HOLD):
                for day in range(0, HORIZON_DAYS + 1):
                    outcome = table.outcome(scenario_id, action, day)
                    assert outcome.scenario_id == scenario_id
                    assert outcome.action == action
                    assert outcome.day == day
                    assert isinstance(outcome.succeeds, bool)
                    if outcome.succeeds:
                        assert outcome.recovered_amount_paise > 0
                    else:
                        assert outcome.recovered_amount_paise == 0

    def test_hold_never_recovers(self):
        table = _build_table(size=20)
        for scenario_id in table.scenario_ids():
            for day in range(0, HORIZON_DAYS + 1):
                assert table.outcome(scenario_id, ACTION_HOLD, day).succeeds is False


class TestDeterminism:
    """Same inputs produce the same table, guaranteeing paired comparison."""

    def test_same_seed_same_outcomes(self):
        table_a = _build_table(size=50)
        table_b = _build_table(size=50)
        assert table_a.scenario_ids() == table_b.scenario_ids()
        for scenario_id in table_a.scenario_ids():
            for action in (ACTION_RETRY, ACTION_PAYMENT_LINK, ACTION_RECONSENT, ACTION_HOLD):
                for day in range(0, 6):
                    outcome_a = table_a.outcome(scenario_id, action, day)
                    outcome_b = table_b.outcome(scenario_id, action, day)
                    assert outcome_a == outcome_b

    def test_shared_table_gives_identical_draws(self):
        """Two reads from the SAME table instance are identical — this is
        what makes the control/treatment comparison paired."""
        table = _build_table(size=50)
        scenario_id = table.scenario_ids()[0]
        first = table.outcome(scenario_id, ACTION_RETRY, 2)
        second = table.outcome(scenario_id, ACTION_RETRY, 2)
        assert first == second


class TestHiddenStateIsolation:
    """The policy must only ever read observed fields. The harness passes
    observed_error_code / observed_error_description to the classifier —
    this test pins that contract at the model level."""

    def test_scenario_has_observed_and_hidden_fields(self):
        table = _build_table(size=10)
        scenario = table.scenario(table.scenario_ids()[0])
        assert scenario.observed_error_code != ""
        assert scenario.hidden_failure_category in {
            "technical",
            "low_balance",
            "ambiguous_customer_action",
            "terminal",
        }

    def test_natural_recovery_only_for_recoverable_categories(self):
        table = _build_table(size=100)
        for scenario_id in table.scenario_ids():
            scenario = table.scenario(scenario_id)
            recovery_day = table.natural_recovery_day(scenario_id)
            if scenario.hidden_failure_category == HIDDEN_TERMINAL:
                assert recovery_day is None


class TestTerminalNeverRetries:
    """Retrying a terminal mandate must structurally fail — this is what
    makes the control's blind retries violations rather than wins."""

    def test_terminal_retry_always_fails(self):
        table = _build_table(size=100)
        for scenario_id in table.scenario_ids():
            scenario = table.scenario(scenario_id)
            if scenario.hidden_failure_category != HIDDEN_TERMINAL:
                continue
            for day in range(0, HORIZON_DAYS + 1):
                assert table.outcome(scenario_id, ACTION_RETRY, day).succeeds is False
