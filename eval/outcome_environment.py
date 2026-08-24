"""Independent outcome environment for policy evaluation.

The outcome environment materializes potential outcomes for every
scenario-action pair BEFORE any policy runs. Both the control (fixed
retry) and treatment (context-aware) policies read from the same
immutable table. This prevents the treatment from receiving favorable
outcomes by construction.

Design rules:

1. Hidden state (true failure category, natural recovery timing) lives
   ONLY in this module. Policy code receives only observed fields.
2. RNG streams are keyed by ``(scenario_id, purpose)`` — common random
   numbers — so every policy arm faces identical luck for identical
   decisions.
3. The outcome table is built once and frozen; there is no mutation
   path after construction.
4. Recovery probabilities are EVALUATION ASSUMPTIONS. The NPCI data
   calibrates the failure mix, not post-action recovery, which no
   public source publishes. See data/README.md.
"""

from __future__ import annotations

import hashlib

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# Failure categories for the hidden ground truth. These are evaluator-only
# labels; the policy must never see them.
HIDDEN_TECHNICAL = "technical"
HIDDEN_LOW_BALANCE = "low_balance"
HIDDEN_AMBIGUOUS = "ambiguous_customer_action"
HIDDEN_TERMINAL = "terminal"

HIDDEN_CATEGORIES = frozenset(
    {HIDDEN_TECHNICAL, HIDDEN_LOW_BALANCE, HIDDEN_AMBIGUOUS, HIDDEN_TERMINAL}
)

# Actions a policy can take.
ACTION_RETRY = "retry"
ACTION_PAYMENT_LINK = "payment_link"
ACTION_RECONSENT = "reconsent"
ACTION_HOLD = "hold"

ACTIONS = frozenset({ACTION_RETRY, ACTION_PAYMENT_LINK, ACTION_RECONSENT, ACTION_HOLD})

# Horizon in days. Any case unresolved after this is counted as lost.
HORIZON_DAYS = 10


def _uniform_from_key(key: str) -> float:
    """Deterministic uniform [0,1) from a string key (common random numbers)."""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


class Scenario(BaseModel):
    """One failed mandate cycle: observed fields plus hidden ground truth.

    ``observed_*`` fields are what the policy may read. ``hidden_*``
    fields exist only for the evaluator. A policy that reads a hidden
    field is a correctness bug, enforced by test, not by convention.
    """

    scenario_id: str
    mandate_id: str
    cycle_id: str
    amount_paise: int
    bank_name: str

    observed_error_code: str
    observed_error_description: str

    hidden_failure_category: str
    hidden_natural_recovery_day: int | None = Field(
        default=None, description="Day the case would recover with zero intervention"
    )
    hidden_retry_success_base: float = Field(
        default=0.0, description="Base P(retry succeeds) at optimal timing"
    )
    hidden_optimal_retry_day: int = Field(
        default=1, description="Best day to retry for this scenario"
    )
    hidden_payment_link_success: float = Field(
        default=0.0, description="P(payment link recovers the case)"
    )
    hidden_reconsent_success: float = Field(
        default=0.0, description="P(reconsent recovers the case)"
    )


class PotentialOutcome(BaseModel):
    """A pre-drawn outcome for one (scenario, action, day) combination."""

    scenario_id: str
    action: str
    day: int
    succeeds: bool
    recovered_amount_paise: int


class OutcomeTable:
    """Frozen lookup of potential outcomes.

    Built once from scenarios. Read-only afterwards.
    """

    def __init__(self, scenarios: list[Scenario]):
        self._scenarios = {s.scenario_id: s for s in scenarios}
        self._outcomes: dict[tuple[str, str, int], PotentialOutcome] = {}
        self._build()

    def _build(self) -> None:
        for scenario in self._scenarios.values():
            for action in ACTIONS:
                for day in range(0, HORIZON_DAYS + 1):
                    succeeds = self._draw(scenario, action, day)
                    recovered = scenario.amount_paise if succeeds else 0
                    self._outcomes[(scenario.scenario_id, action, day)] = PotentialOutcome(
                        scenario_id=scenario.scenario_id,
                        action=action,
                        day=day,
                        succeeds=succeeds,
                        recovered_amount_paise=recovered,
                    )

    def _draw(self, scenario: Scenario, action: str, day: int) -> bool:
        """Draw an outcome for (scenario, action, day).

        Keyed by (scenario_id, action, day) so every arm sees the same
        draw for the same decision. Probabilities are evaluation
        assumptions derived from the hidden category.
        """
        key = f"{scenario.scenario_id}|{action}|{day}"
        draw = _uniform_from_key(key)

        if action == ACTION_HOLD:
            return False
        if action == ACTION_RECONSENT:
            return draw < scenario.hidden_reconsent_success
        if action == ACTION_PAYMENT_LINK:
            return draw < scenario.hidden_payment_link_success
        if action == ACTION_RETRY:
            return draw < self._retry_probability(scenario, day)
        raise ValueError(f"Unknown action: {action}")

    def _retry_probability(self, scenario: Scenario, day: int) -> float:
        """P(retry succeeds) for this scenario on this day.

        Terminal cases never recover on retry. Others peak near the
        optimal retry day and decay with distance from it.
        """
        if scenario.hidden_failure_category == HIDDEN_TERMINAL:
            return 0.0
        distance = abs(day - scenario.hidden_optimal_retry_day)
        decay = 0.85**distance
        return min(1.0, scenario.hidden_retry_success_base * decay)

    def outcome(self, scenario_id: str, action: str, day: int) -> PotentialOutcome:
        """Look up a pre-drawn outcome. Raises KeyError on unknown keys."""
        return self._outcomes[(scenario_id, action, day)]

    def natural_recovery_day(self, scenario_id: str) -> int | None:
        """Day this scenario would recover with zero intervention, if any."""
        return self._scenarios[scenario_id].hidden_natural_recovery_day

    def scenario(self, scenario_id: str) -> Scenario:
        return self._scenarios[scenario_id]

    def scenario_ids(self) -> list[str]:
        return list(self._scenarios.keys())

    def is_frozen(self) -> bool:
        """Always true: no mutation API exists."""
        return True


def build_scenarios_from_attempts(
    attempts: list,
    profile: str = "balanced",
) -> list[Scenario]:
    """Build scenarios from generated DebitAttempts using hidden labels.

    The hidden failure category is derived from the error code, matching
    the generator's own hidden labels. Natural recovery and success
    probabilities are profile-based evaluation assumptions.
    """
    from mandate_doctor.core.codes import lookup_bucket

    scenarios: list[Scenario] = []
    for index, attempt in enumerate(attempts):
        code = attempt.error.code if attempt.error else "unknown"
        bucket, _ = lookup_bucket(code)
        hidden_category = _bucket_to_hidden_category(bucket)
        scenarios.append(
            Scenario(
                scenario_id=f"sc_{profile}_{index:04d}",
                mandate_id=attempt.mandate_id,
                # Each scenario is one billing cycle for this mandate. The
                # generator reuses mandate IDs, so derive a unique cycle_id
                # here instead of trusting the attempt's default value.
                cycle_id=f"cycle_{index:04d}",
                amount_paise=attempt.amount,
                bank_name="synthetic",
                observed_error_code=code,
                observed_error_description=attempt.error.description if attempt.error else "",
                hidden_failure_category=hidden_category,
                hidden_natural_recovery_day=_natural_recovery_day(
                    hidden_category, f"{attempt.mandate_id}|{attempt.cycle_id}"
                ),
                hidden_retry_success_base=_retry_base(hidden_category),
                hidden_optimal_retry_day=_optimal_retry_day(
                    hidden_category, f"{attempt.mandate_id}|{attempt.cycle_id}"
                ),
                hidden_payment_link_success=_payment_link_probability(hidden_category),
                hidden_reconsent_success=_reconsent_probability(hidden_category),
            )
        )
    return scenarios


def _bucket_to_hidden_category(bucket) -> str:
    """Map classifier-visible bucket to hidden category.

    For evaluation, buckets map to hidden categories so the policy's
    deterministic view and the evaluator's truth stay consistent where
    the code is visible, while remaining hidden to the policy itself.
    """
    if bucket is None:
        return HIDDEN_AMBIGUOUS
    from mandate_doctor.core.models import FailureBucket

    mapping = {
        FailureBucket.TECHNICAL: HIDDEN_TECHNICAL,
        FailureBucket.LOW_BALANCE: HIDDEN_LOW_BALANCE,
        FailureBucket.STOP: HIDDEN_TERMINAL,
        FailureBucket.AMBIGUOUS: HIDDEN_AMBIGUOUS,
    }
    return mapping.get(bucket, HIDDEN_AMBIGUOUS)


def _natural_recovery_day(hidden_category: str, seed_key: str) -> int | None:
    """Day of self-recovery without intervention, or None if never.

    Evaluation assumptions derived from Triage's per-cause natural
    recovery table (their holdout arm) and industry understanding:
    - technical failures frequently self-resolve (outages end)
    - low balance partially resolves near salary days
    - terminal cases never self-resolve
    """
    draw = _uniform_from_key(f"natural|{seed_key}")
    if hidden_category == HIDDEN_TECHNICAL:
        return None if draw > 0.65 else 1 + int(draw * 3)  # ~65% recover in days 1-3
    if hidden_category == HIDDEN_LOW_BALANCE:
        return None if draw > 0.22 else 3 + int(draw * 4)  # ~22% recover days 3-6
    return None


def _retry_base(hidden_category: str) -> float:
    """Base retry success at optimal timing, per hidden category.

    Evaluation assumptions, documented as such. Terminal is 0.0 by
    construction — retrying a revoked mandate can never succeed.
    """
    values = {
        HIDDEN_TECHNICAL: 0.80,
        HIDDEN_LOW_BALANCE: 0.65,
        HIDDEN_AMBIGUOUS: 0.35,
        HIDDEN_TERMINAL: 0.0,
    }
    return values[hidden_category]


def _optimal_retry_day(hidden_category: str, seed_key: str) -> int:
    """Best day to retry for this scenario."""
    if hidden_category == HIDDEN_LOW_BALANCE:
        # Salary-window hypothesis: funds land on a specific day 2-6.
        draw = _uniform_from_key(f"salary|{seed_key}")
        return 2 + int(draw * 5)
    if hidden_category == HIDDEN_TECHNICAL:
        return 0  # retry immediately once the outage is over
    return 1


def _payment_link_probability(hidden_category: str) -> float:
    values = {
        HIDDEN_TECHNICAL: 0.50,
        HIDDEN_LOW_BALANCE: 0.45,
        HIDDEN_AMBIGUOUS: 0.30,
        HIDDEN_TERMINAL: 0.10,
    }
    return values[hidden_category]


def _reconsent_probability(hidden_category: str) -> float:
    values = {
        HIDDEN_TECHNICAL: 0.60,
        HIDDEN_LOW_BALANCE: 0.40,
        HIDDEN_AMBIGUOUS: 0.25,
        HIDDEN_TERMINAL: 0.15,
    }
    return values[hidden_category]
