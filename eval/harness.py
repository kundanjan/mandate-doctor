"""Control/treatment evaluation harness.

Runs three arms on the same frozen outcome table:

1. NATURAL (do nothing): counts scenarios that self-recover. The true
   baseline — an agent that counts self-healing cases as its own wins
   is lying to the merchant.
2. CONTROL: Razorpay's documented fixed retry policy. The original
   charge fails on T=0, then the subscription is retried blindly on
   T+1, T+2, T+3, and halted if still failing. No classification, no
   stopping rules, no timing intelligence.
3. TREATMENT: Mandate Doctor's context-aware policy (classify, decide
   under the NPCI 1+3 budget, execute, observe).

All three arms read from the same immutable outcome table, so any
difference in recovery is attributable to the policy, not to luck.

Metrics include bootstrap confidence intervals on the incremental lift
so an inconclusive result is reported as inconclusive rather than
silently presented as a win.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from eval.outcome_environment import (
    ACTION_PAYMENT_LINK,
    ACTION_RECONSENT,
    ACTION_RETRY,
    HORIZON_DAYS,
    OutcomeTable,
)


@dataclass
class CaseResult:
    """Outcome of one arm on one scenario."""

    scenario_id: str
    arm: str
    recovered: bool = False
    recovered_day: int | None = None
    recovered_amount_paise: int = 0
    retry_attempts: int = 0
    hard_stop_violations: int = 0
    budget_violations: int = 0
    final_state: str = "lost"


# v0 scheduling policy: bucket -> candidate retry days (0-based).
# The estimator slice will replace this with data-driven windows.
RETRY_SCHEDULE: dict = {
    "technical": [0],
    "low_balance": [2, 4, 6],
}


@dataclass
class ArmReport:
    """Aggregate metrics for one arm."""

    arm: str
    eligible_cycles: int = 0
    recovered_cycles: int = 0
    recovered_amount_paise: int = 0
    total_retry_attempts: int = 0
    hard_stop_violations: int = 0
    budget_violations: int = 0
    recovered_days: list[int] = field(default_factory=list)

    @property
    def recovery_rate(self) -> float:
        if self.eligible_cycles == 0:
            return 0.0
        return self.recovered_cycles / self.eligible_cycles

    @property
    def mean_time_to_recovery(self) -> float | None:
        if not self.recovered_days:
            return None
        return sum(self.recovered_days) / len(self.recovered_days)


def run_natural(table: OutcomeTable) -> list[CaseResult]:
    """Do-nothing arm: count scenarios that self-recover by the horizon."""
    results = []
    for scenario_id in table.scenario_ids():
        scenario = table.scenario(scenario_id)
        recovery_day = table.natural_recovery_day(scenario_id)
        recovered = recovery_day is not None and recovery_day <= HORIZON_DAYS
        results.append(
            CaseResult(
                scenario_id=scenario_id,
                arm="natural",
                recovered=recovered,
                recovered_day=recovery_day if recovered else None,
                recovered_amount_paise=scenario.amount_paise if recovered else 0,
                final_state="recovered" if recovered else "lost",
            )
        )
    return results


def run_control(table: OutcomeTable) -> list[CaseResult]:
    """Razorpay documented default: blind T+1, T+2, T+3 retries, then halt.

    The control retries every failure regardless of cause. Retrying a
    terminal mandate is counted as a hard-stop violation (the control
    does not know better — that is the point of the comparison).
    """
    from eval.outcome_environment import HIDDEN_TERMINAL

    results = []
    for scenario_id in table.scenario_ids():
        scenario = table.scenario(scenario_id)
        violations = 0
        for retries, day in enumerate((1, 2, 3), start=1):
            if scenario.hidden_failure_category == HIDDEN_TERMINAL:
                violations += 1
            outcome = table.outcome(scenario_id, ACTION_RETRY, day)
            if outcome.succeeds:
                results.append(
                    CaseResult(
                        scenario_id=scenario_id,
                        arm="control",
                        recovered=True,
                        recovered_day=day,
                        recovered_amount_paise=outcome.recovered_amount_paise,
                        retry_attempts=retries,
                        hard_stop_violations=violations,
                        final_state="recovered",
                    )
                )
                break
        else:
            results.append(
                CaseResult(
                    scenario_id=scenario_id,
                    arm="control",
                    retry_attempts=3,
                    hard_stop_violations=violations,
                    final_state="halted",
                )
            )
    return results


def run_treatment(table: OutcomeTable, budget: object | None = None) -> list[CaseResult]:
    """Mandate Doctor policy: classify, decide under budget, execute.

    Uses the real classifier and policy engine from the codebase so the
    treatment arm is the actual system, not a hand-written copy.

    Scheduling (v0, deterministic — the estimator slice will replace
    this with data-driven windows):
    - TECHNICAL failures retry immediately (day 0)
    - LOW_BALANCE retries spread across days 2/4/6 (salary-window
      hypothesis; blind T+1/T+2/T+3 is the control's weakness)
    - STOP triggers reconsent once, never a retry
    - AMBIGUOUS sends one payment link, never a retry
    """
    from mandate_doctor.core.classifier import classify
    from mandate_doctor.core.models import (
        DebitAttempt,
        ErrorDetail,
        FailureBucket,
    )
    from mandate_doctor.core.policy import RetryBudget

    retry_budget = budget if budget is not None else RetryBudget()

    results = []

    for scenario_id in table.scenario_ids():
        scenario = table.scenario(scenario_id)
        attempt = DebitAttempt(
            attempt_id=f"att_{scenario_id}",
            mandate_id=scenario.mandate_id,
            cycle_id=scenario.cycle_id,
            amount=scenario.amount_paise,
            result="failed",
            error=ErrorDetail(
                code=scenario.observed_error_code,
                description=scenario.observed_error_description,
            ),
            is_synthetic=True,
        )

        bucket, confidence, signals, reasoning = classify(attempt)
        recovered = False
        recovered_day: int | None = None
        retries = 0
        final_state = "lost"

        if bucket == FailureBucket.STOP:
            outcome = table.outcome(scenario_id, ACTION_RECONSENT, 0)
            if outcome.succeeds:
                recovered = True
                recovered_day = 0
                final_state = "recovered"
            else:
                final_state = "reconsent_failed"
        elif bucket == FailureBucket.AMBIGUOUS:
            outcome = table.outcome(scenario_id, ACTION_PAYMENT_LINK, 0)
            if outcome.succeeds:
                recovered = True
                recovered_day = 0
                final_state = "recovered"
            else:
                final_state = "held"
        else:
            schedule = RETRY_SCHEDULE.get(bucket.value, [])
            for day in schedule:
                if retries >= 3:
                    final_state = "budget_exhausted"
                    break
                if not retry_budget.consume_retry(scenario.mandate_id, scenario.cycle_id):
                    final_state = "budget_exhausted"
                    break
                retries += 1
                outcome = table.outcome(scenario_id, ACTION_RETRY, day)
                if outcome.succeeds:
                    recovered = True
                    recovered_day = day
                    final_state = "recovered"
                    break
            else:
                final_state = "retries_failed"

        results.append(
            CaseResult(
                scenario_id=scenario_id,
                arm="treatment",
                recovered=recovered,
                recovered_day=recovered_day,
                recovered_amount_paise=scenario.amount_paise if recovered else 0,
                retry_attempts=retries,
                hard_stop_violations=0,
                final_state=final_state,
            )
        )
    return results


def aggregate(results: list[CaseResult], arm: str) -> ArmReport:
    report = ArmReport(arm=arm, eligible_cycles=len(results))
    for result in results:
        if result.recovered:
            report.recovered_cycles += 1
            report.recovered_amount_paise += result.recovered_amount_paise
            if result.recovered_day is not None:
                report.recovered_days.append(result.recovered_day)
        report.total_retry_attempts += result.retry_attempts
        report.hard_stop_violations += result.hard_stop_violations
        report.budget_violations += result.budget_violations
    return report


def bootstrap_ci(
    treatment: list[CaseResult],
    natural: list[CaseResult],
    n_bootstrap: int = 1000,
    seed: int = 7,
) -> tuple[float, float, float]:
    """Bootstrap 95% CI on the incremental lift of treatment vs natural.

    Incremental lift is measured per-case: a case counts as incremental
    if treatment recovered it AND natural would not have. Resampling
    cases with replacement yields a distribution; report mean and the
    2.5/97.5 percentiles.
    """
    rng = random.Random(seed)
    treatment_by_id = {r.scenario_id: r for r in treatment}
    natural_by_id = {r.scenario_id: r for r in natural}
    case_ids = list(treatment_by_id.keys())

    def incremental_rate(case_sample: list[str]) -> float:
        incremental = 0
        for scenario_id in case_sample:
            t = treatment_by_id[scenario_id]
            n = natural_by_id[scenario_id]
            if t.recovered and not n.recovered:
                incremental += 1
        return incremental / len(case_sample)

    full_rate = incremental_rate(case_ids)
    draws = [incremental_rate([rng.choice(case_ids) for _ in case_ids]) for _ in range(n_bootstrap)]
    draws.sort()
    lower = draws[int(0.025 * n_bootstrap)]
    upper = draws[int(0.975 * n_bootstrap)]
    return full_rate, lower, upper


def run_evaluation(
    table: OutcomeTable,
    n_bootstrap: int = 1000,
    seed: int = 7,
) -> dict:
    """Run all three arms and return the comparison report."""
    natural = run_natural(table)
    control = run_control(table)
    treatment = run_treatment(table)

    natural_report = aggregate(natural, "natural")
    control_report = aggregate(control, "control")
    treatment_report = aggregate(treatment, "treatment")

    incremental_rate, ci_lower, ci_upper = bootstrap_ci(
        treatment, natural, n_bootstrap=n_bootstrap, seed=seed
    )

    control_incremental, control_ci_lower, control_ci_upper = bootstrap_ci(
        control, natural, n_bootstrap=n_bootstrap, seed=seed
    )

    return {
        "eligible_cycles": natural_report.eligible_cycles,
        "natural": natural_report,
        "control": control_report,
        "treatment": treatment_report,
        "treatment_incremental_lift": {
            "rate": incremental_rate,
            "ci_95": (ci_lower, ci_upper),
            "inconclusive": ci_lower <= 0 <= ci_upper,
        },
        "control_incremental_lift": {
            "rate": control_incremental,
            "ci_95": (control_ci_lower, control_ci_upper),
            "inconclusive": control_ci_lower <= 0 <= control_ci_upper,
        },
        "lift_treatment_over_control": (
            treatment_report.recovery_rate - control_report.recovery_rate
        ),
    }


def print_report(report: dict) -> None:
    """Human-readable report for the dashboard and CLI."""
    eligible = report["eligible_cycles"]

    def pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    print(f"\nEligible failed cycles: {eligible}")
    print(f"Horizon: {HORIZON_DAYS} days")
    print()
    for arm in ("natural", "control", "treatment"):
        arm_report = report[arm]
        attempts_per_recovery = (
            arm_report.total_retry_attempts / arm_report.recovered_cycles
            if arm_report.recovered_cycles > 0
            else float("inf")
        )
        violations_per_1000 = (
            arm_report.hard_stop_violations / eligible * 1000 if eligible > 0 else 0.0
        )
        print(f"{arm.upper()} arm:")
        print(f"  Recovery rate: {pct(arm_report.recovery_rate)}")
        print(f"  Recovered amount: ₹{arm_report.recovered_amount_paise / 100:,.2f}")
        print(f"  Retry attempts: {arm_report.total_retry_attempts}")
        if arm != "natural":
            print(f"  Attempts per recovery: {attempts_per_recovery:.2f}")
        print(f"  Hard-stop violations: {arm_report.hard_stop_violations}")
        if arm != "natural":
            print(f"  Violations per 1000 cases: {violations_per_1000:.1f}")
        print(f"  Budget violations: {arm_report.budget_violations}")
        if arm_report.mean_time_to_recovery is not None:
            print(f"  Mean days to recovery: {arm_report.mean_time_to_recovery:.1f}")
        print()

    treatment_lift = report["treatment_incremental_lift"]
    control_lift = report["control_incremental_lift"]
    print("Incremental lift over natural recovery:")
    print(
        f"  Control:   {pct(control_lift['rate'])} "
        f"95% CI [{pct(control_lift['ci_95'][0])}, {pct(control_lift['ci_95'][1])}]"
        f"{' (INCONCLUSIVE)' if control_lift['inconclusive'] else ''}"
    )
    print(
        f"  Treatment: {pct(treatment_lift['rate'])} "
        f"95% CI [{pct(treatment_lift['ci_95'][0])}, {pct(treatment_lift['ci_95'][1])}]"
        f"{' (INCONCLUSIVE)' if treatment_lift['inconclusive'] else ''}"
    )
    print(f"  Treatment over control: {report['lift_treatment_over_control']:+.1%}")


if __name__ == "__main__":
    import logging

    import structlog

    # Evaluation runs emit thousands of classifier decisions; keep only
    # warnings and errors visible.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

    from eval.generate_batch import SCENARIO_PROFILES, generate_batch
    from eval.outcome_environment import build_scenarios_from_attempts

    for profile_name in SCENARIO_PROFILES:
        attempts = generate_batch(size=500, seed=42, scenario=profile_name)
        scenarios = build_scenarios_from_attempts(attempts, profile=profile_name)
        table = OutcomeTable(scenarios)
        report = run_evaluation(table)
        print(f"\n{'=' * 60}")
        print(f"PROFILE: {profile_name}")
        print_report(report)
