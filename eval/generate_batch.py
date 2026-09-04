"""NPCI-calibrated synthetic batch generator for evaluation.

Generates DebitAttempt objects whose failure distribution is calibrated
against frozen NPCI AutoPay Mandate Execution statistics.

Calibration source:
    data/npci-autopay-execution-2026-07.csv
    NPCI UPI AutoPay Ecosystem Statistics, July 2026, Top 50 remitter banks.
    Retrieved 2026-08-23. See data/README.md for full provenance.

What is calibrated from real data:
    - Per-bank Approved%, BD%, TD% (volume-weighted across 50 banks)
    - Bank selection probability proportional to execution volume

What is NOT calibrated (evaluation assumptions only):
    - Sub-category composition within BD. NPCI publishes BD as a single
      aggregate that includes insufficient funds, invalid PIN, limits
      exceeded, account blocked/closed/frozen, and other business reasons.
      The split of BD into LOW_BALANCE vs AMBIGUOUS vs STOP sub-categories
      is an evaluation assumption, not a published statistic.
    - Recovery probabilities after a retry action.

The hidden ground truth label exists only inside the evaluation environment.
It is never exposed to the recovery system.
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mandate_doctor.core.models import DebitAttempt, ErrorDetail

# Fixed base timestamp for reproducible fixtures.
# All generated attempts use BASE_TIMESTAMP + i * 1 hour so that the same
# seed always produces identical timestamps across runs.
BASE_TIMESTAMP = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Frozen calibration file paths
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REMITTER_CSV = DATA_DIR / "npci-autopay-execution-2026-07.csv"

# ---------------------------------------------------------------------------
# Evaluation-mode scenario profiles
#
# These control how the aggregate BD bucket is decomposed into sub-categories
# for evaluation purposes. They are NOT calibrated from public data because
# NPCI does not publish BD sub-composition. Each profile represents a plausible
# merchant population; running the harness across all profiles tests policy
# robustness rather than relying on one favorable distribution.
#
# Keys are FailureBucket values. Values are conditional probabilities within BD.
# TD is handled separately using the real calibrated TD percentage.
# ---------------------------------------------------------------------------

SCENARIO_PROFILES: dict[str, dict[str, float]] = {
    "balanced": {
        "low_balance": 0.55,
        "ambiguous_customer_action": 0.30,
        "stop_terminal": 0.15,
    },
    "low_balance_heavy": {
        "low_balance": 0.75,
        "ambiguous_customer_action": 0.15,
        "stop_terminal": 0.10,
    },
    "stop_heavy": {
        "low_balance": 0.25,
        "ambiguous_customer_action": 0.20,
        "stop_terminal": 0.55,
    },
    "technical_heavy": {
        # High proportion of transient bank/gateway failures — these are
        # retryable and where the timing-aware policy outperforms blind T+1.
        # BD sub-composition is secondary; the TD share is driven by the real
        # calibrated NPCI per-bank TD% (see generate_batch logic below).
        # This profile biases the BD split to simulate a tech-failure-heavy
        # merchant (e.g. neobank or infrastructure-heavy biller).
        "low_balance": 0.20,
        "ambiguous_customer_action": 0.65,
        "stop_terminal": 0.15,
    },
    "adversarial_generic": {
        # All failures return generic `payment_declined` with no description.
        # The classifier must abstain on most of these.
        "low_balance": 0.40,
        "ambiguous_customer_action": 0.35,
        "stop_terminal": 0.25,
    },
}

# ---------------------------------------------------------------------------
# Error-code pools per hidden ground-truth category
# ---------------------------------------------------------------------------

TECHNICAL_CODES = [
    ("bank_technical_error", "Bank server timeout during payment processing"),
    ("timeout", "Payment gateway request timed out"),
]

BD_LOW_BALANCE_CODES = [
    ("insufficient_funds", "Insufficient funds in account"),
    ("insufficient_balance", "Account balance lower than transaction amount"),
]

BD_AMBIGUOUS_CODES = [
    ("invalid_upi_pin", "Incorrect UPI PIN entered"),
    ("pin_attempts_exceeded", "Maximum PIN attempts exceeded"),
    ("authentication_failed", "Authentication failed"),
]

BD_STOP_CODES = [
    ("account_closed", "Bank account has been closed"),
    ("mandate_revoked", "Mandate revoked by customer"),
    ("fraud_suspected", "Transaction flagged as potentially fraudulent"),
]

# Generic decline used in the adversarial profile
GENERIC_DECLINE = ("payment_declined", "")

AMOUNTS_PAISE = [19_900, 49_900, 99_900, 149_900, 299_900, 499_900]


# ---------------------------------------------------------------------------
# Calibration loading
# ---------------------------------------------------------------------------


def load_bank_calibration(
    csv_path: Path | None = None,
) -> list[dict[str, str | float]]:
    """Load bank-level Approved/BD/TD calibration from the frozen CSV.

    Returns a list of dicts with keys:
        bank: str
        volume_mn: float
        approved_pct: float
        bd_pct: float
        td_pct: float
    """
    path = csv_path or REMITTER_CSV
    if not path.exists():
        raise FileNotFoundError(
            f"Calibration CSV not found: {path}. "
            "Run the snapshot download script or check data/README.md."
        )

    rows_by_bank: dict[str, dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bank = row["remitter_bank"]
            if bank not in rows_by_bank:
                rows_by_bank[bank] = {"bank": bank}
            rows_by_bank[bank][row["category"]] = row["value"]

    result = []
    for entry in rows_by_bank.values():
        result.append(
            {
                "bank": entry["bank"],
                "volume_mn": float(entry["Total Volume"]),
                "approved_pct": float(entry["Approved"]),
                "bd_pct": float(entry["BD"]),
                "td_pct": float(entry["TD"]),
            }
        )
    return result


def compute_weighted_aggregates(
    banks: list[dict[str, str | float]],
) -> dict[str, float]:
    """Compute volume-weighted Approved/BD/TD percentages across all banks."""
    total_volume = sum(float(b["volume_mn"]) for b in banks)
    if total_volume == 0:
        raise ValueError("Total volume is zero; cannot compute weighted aggregates.")

    def weighted(key: str) -> float:
        return sum(float(b["volume_mn"]) * float(b[key]) for b in banks) / total_volume

    return {
        "approved_pct": weighted("approved_pct"),
        "bd_pct": weighted("bd_pct"),
        "td_pct": weighted("td_pct"),
    }


# ---------------------------------------------------------------------------
# Batch generation
# ---------------------------------------------------------------------------


def generate_batch(
    size: int = 500,
    seed: int = 42,
    scenario: str = "balanced",
    csv_path: Path | None = None,
) -> list[DebitAttempt]:
    """Generate a synthetic batch calibrated against NPCI July 2026 data.

    Args:
        size: Number of failed debit attempts to generate.
        seed: Random seed for reproducibility.
        scenario: Key into SCENARIO_PROFILES controlling BD sub-composition.
        csv_path: Optional override path to the calibration CSV.

    Returns:
        List of DebitAttempt objects with realistic error distributions.
    """
    if scenario not in SCENARIO_PROFILES:
        raise ValueError(
            f"Unknown scenario '{scenario}'. Available: {list(SCENARIO_PROFILES.keys())}"
        )

    rng = random.Random(seed)
    banks = load_bank_calibration(csv_path)
    profile = SCENARIO_PROFILES[scenario]

    # Build bank-selection weights proportional to volume
    total_volume = sum(float(b["volume_mn"]) for b in banks)
    bank_weights = [float(b["volume_mn"]) / total_volume for b in banks]

    attempts: list[DebitAttempt] = []

    for i in range(size):
        # Select a bank weighted by its execution volume
        bank = rng.choices(banks, weights=bank_weights, k=1)[0]

        # Decide whether this attempt fails as TD or BD based on the bank's
        # actual published rates among declined transactions
        td_share = float(bank["td_pct"])
        bd_share = float(bank["bd_pct"])
        declined_total = td_share + bd_share
        if declined_total <= 0:
            # Skip banks with zero declines (should not happen in practice)
            continue

        td_probability = td_share / declined_total

        if rng.random() < td_probability:
            # Technical decline — deterministic TECHNICAL bucket
            code, description = rng.choice(TECHNICAL_CODES)
            hidden_label = "technical"
        else:
            # Business decline — sub-category depends on the evaluation profile
            r = rng.random()
            cumulative = 0.0
            hidden_label = "unknown"
            for label_key, probability in profile.items():
                cumulative += probability
                if r <= cumulative:
                    hidden_label = label_key
                    break

            if hidden_label == "low_balance":
                code, description = rng.choice(BD_LOW_BALANCE_CODES)
            elif hidden_label == "ambiguous_customer_action":
                code, description = rng.choice(BD_AMBIGUOUS_CODES)
            elif hidden_label == "stop_terminal":
                code, description = rng.choice(BD_STOP_CODES)
            else:
                code, description = GENERIC_DECLINE

        if scenario == "adversarial_generic":
            # Override all codes with generic decline, no description
            code, description = GENERIC_DECLINE

        attempts.append(
            DebitAttempt(
                attempt_id=f"att_synthetic_{scenario[:4]}_{seed}_{i:04d}",
                mandate_id=f"md_synthetic_{scenario[:4]}_{seed}_{i % 100:03d}",
                cycle_id=f"cyc_{seed}_{i // 100:02d}",
                timestamp=BASE_TIMESTAMP + timedelta(hours=i),
                amount=rng.choice(AMOUNTS_PAISE),
                result="failed",
                error=ErrorDetail(
                    code=code,
                    description=description,
                    source="bank",
                    step="payment",
                ),
                is_synthetic=True,
            )
        )

    return attempts


def save_batch(attempts: list[DebitAttempt], path: Path) -> None:
    """Save the synthetic batch to a JSON file."""
    data = [a.model_dump(mode="json") for a in attempts]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def load_batch(path: Path) -> list[DebitAttempt]:
    """Load a synthetic batch from a JSON file."""
    data = json.loads(path.read_text())
    return [DebitAttempt.model_validate(a) for a in data]


def print_distribution(attempts: list[DebitAttempt]) -> None:
    """Print the observed distribution of the generated batch."""
    bucket_counts: Counter[str] = Counter()
    code_counts: Counter[str] = Counter()

    for a in attempts:
        if a.error:
            from mandate_doctor.core.codes import lookup_bucket

            bucket, _ = lookup_bucket(a.error.code)
            if bucket:
                bucket_counts[bucket.value] += 1
            else:
                bucket_counts["unmapped"] += 1
            code_counts[a.error.code] += 1

    total = len(attempts)
    print(f"\nBatch size: {total}")
    print("\nBy classifier-visible bucket:")
    for bucket, count in bucket_counts.most_common():
        print(f"  {bucket}: {count} ({count / total * 100:.1f}%)")
    print("\nBy error code:")
    for code, count in code_counts.most_common():
        print(f"  {code}: {count} ({count / total * 100:.1f}%)")


if __name__ == "__main__":
    banks = load_bank_calibration()
    agg = compute_weighted_aggregates(banks)
    print("NPCI calibration loaded:")
    print(f"  Banks: {len(banks)}")
    print(f"  Weighted Approved: {agg['approved_pct']:.2f}%")
    print(f"  Weighted BD:       {agg['bd_pct']:.2f}%")
    print(f"  Weighted TD:       {agg['td_pct']:.2f}%")

    for scenario_name in SCENARIO_PROFILES:
        batch = generate_batch(size=500, seed=42, scenario=scenario_name)
        print(f"\n{'=' * 50}")
        print(f"Scenario: {scenario_name}")
        print_distribution(batch)

    output = generate_batch(size=500, seed=42, scenario="balanced")
    save_batch(output, Path("eval/synthetic_batch.json"))
    print("\nSaved balanced batch to eval/synthetic_batch.json")
