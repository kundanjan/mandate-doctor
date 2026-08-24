"""Tests for the control/treatment evaluation harness.

Invariants pinned here:
1. Control follows the documented fixed retry policy (T+1/T+2/T+3).
2. Treatment never violates hard stops and never exceeds the NPCI budget.
3. All arms share the same eligible-cycle denominator.
4. Bootstrap CI is reproducible and brackets the point estimate.
"""

import pytest

from eval.generate_batch import generate_batch
from eval.harness import (
    aggregate,
    bootstrap_ci,
    run_control,
    run_evaluation,
    run_natural,
    run_treatment,
)
from eval.outcome_environment import (
    HIDDEN_TERMINAL,
    OutcomeTable,
    build_scenarios_from_attempts,
)


def _build_table(scenario: str = "balanced", size: int = 100) -> OutcomeTable:
    attempts = generate_batch(size=size, seed=42, scenario=scenario)
    scenarios = build_scenarios_from_attempts(attempts, profile=scenario)
    return OutcomeTable(scenarios)


class TestControlPolicy:
    """The control must reproduce Razorpay's documented T+1/T+2/T+3."""

    def test_control_retries_at_most_three_times(self):
        table = _build_table(size=50)
        results = run_control(table)
        for result in results:
            assert result.retry_attempts <= 3

    def test_control_recovery_days_within_one_to_three(self):
        table = _build_table(size=50)
        results = run_control(table)
        for result in results:
            if result.recovered:
                assert 1 <= result.recovered_day <= 3

    def test_control_halted_when_all_retries_fail(self):
        table = _build_table(size=50)
        results = run_control(table)
        halted = [r for r in results if r.final_state == "halted"]
        for result in halted:
            assert result.recovered is False
            assert result.retry_attempts == 3

    def test_control_violates_hard_stops_on_terminal_mandates(self):
        """The control blindly retries terminal mandates — that is its
        documented weakness and the reason for the comparison."""
        table = _build_table(scenario="stop_heavy", size=100)
        results = run_control(table)
        violations = [r for r in results if r.hard_stop_violations > 0]
        assert violations
        terminal_count = sum(
            1
            for scenario_id in table.scenario_ids()
            if table.scenario(scenario_id).hidden_failure_category == HIDDEN_TERMINAL
        )
        assert terminal_count > 0


class TestTreatmentPolicy:
    """The treatment must respect every constraint the control ignores."""

    def test_treatment_never_violates_hard_stops(self):
        for profile in ("balanced", "stop_heavy", "low_balance_heavy", "adversarial_generic"):
            table = _build_table(scenario=profile, size=100)
            results = run_treatment(table)
            assert all(r.hard_stop_violations == 0 for r in results), profile

    def test_treatment_never_exceeds_npci_budget(self):
        for profile in ("balanced", "stop_heavy", "low_balance_heavy"):
            table = _build_table(scenario=profile, size=100)
            results = run_treatment(table)
            assert all(r.retry_attempts <= 3 for r in results), profile

    def test_treatment_abstains_on_generic_declines(self):
        """The adversarial profile returns bare payment_declined. The
        treatment must not retry blindly — it holds or sends a link."""
        table = _build_table(scenario="adversarial_generic", size=50)
        results = run_treatment(table)
        assert all(r.retry_attempts == 0 for r in results)


class TestSharedDenominator:
    """All arms must run over exactly the same scenarios."""

    def test_same_eligible_cycles_across_arms(self):
        table = _build_table(size=100)
        natural = run_natural(table)
        control = run_control(table)
        treatment = run_treatment(table)
        assert len(natural) == len(control) == len(treatment)
        natural_ids = {r.scenario_id for r in natural}
        assert natural_ids == {r.scenario_id for r in control}
        assert natural_ids == {r.scenario_id for r in treatment}


class TestBootstrap:
    """The CI machinery must be reproducible and bracket the estimate."""

    def test_bootstrap_reproducible(self):
        table = _build_table(size=60)
        treatment = run_treatment(table)
        natural = run_natural(table)
        first = bootstrap_ci(treatment, natural, n_bootstrap=200, seed=7)
        second = bootstrap_ci(treatment, natural, n_bootstrap=200, seed=7)
        assert first == second

    def test_bootstrap_contains_point_estimate(self):
        table = _build_table(size=60)
        treatment = run_treatment(table)
        natural = run_natural(table)
        full_rate, lower, upper = bootstrap_ci(treatment, natural, n_bootstrap=300, seed=7)
        assert lower <= full_rate <= upper

    def test_full_rate_is_incremental_not_raw(self):
        """Incremental rate must exclude cases the natural arm also
        recovers — counting those would credit the agent for self-healing."""
        table = _build_table(size=60)
        treatment = run_treatment(table)
        natural = run_natural(table)
        natural_recovered = {r.scenario_id for r in natural if r.recovered}
        treatment_only = sum(
            1 for r in treatment if r.recovered and r.scenario_id not in natural_recovered
        )
        full_rate, _, _ = bootstrap_ci(treatment, natural, n_bootstrap=10, seed=7)
        assert full_rate == pytest.approx(treatment_only / len(treatment))


class TestReportStructure:
    """The report must carry everything the dashboard and pitch need."""

    def test_report_has_all_arms_and_lifts(self):
        table = _build_table(size=60)
        report = run_evaluation(table, n_bootstrap=50)
        for arm in ("natural", "control", "treatment"):
            assert arm in report
            assert report[arm].eligible_cycles == 60
        assert "treatment_incremental_lift" in report
        assert "control_incremental_lift" in report
        assert "lift_treatment_over_control" in report

    def test_aggregate_counts_are_consistent(self):
        table = _build_table(size=60)
        results = run_treatment(table)
        report = aggregate(results, "treatment")
        assert report.eligible_cycles == 60
        assert report.recovered_cycles == sum(1 for r in results if r.recovered)
        assert report.total_retry_attempts == sum(r.retry_attempts for r in results)
