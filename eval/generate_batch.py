"""Synthetic batch generator for evaluation.

Generates 500 DebitAttempts with realistic NPCI return-code distributions.

Distribution sources:
- productgrowth.in (updated Jun 2026): typical merchant-side UPI failure breakdown
  from fintech audits, sourced from NPCI Circular OC-149 and NPCI BD/TD statistics
  https://productgrowth.in/insights/fintech/upi-payment-success-rates/
- Razorpay official error codes: github.com/razorpay/markdown-docs/blob/master/errors/payments/list.md
- NPCI per-bank BD/TD data: npci.org.in/statistics/bd-td-and-uptime

Distribution (based on industry data):
- Bank server timeout: 35-45% → TECHNICAL
- Wrong UPI PIN / exceeded attempts: 20-30% → AMBIGUOUS
- Insufficient balance: 15-25% → LOW_BALANCE
- Network/connectivity issues: 10-15% → TECHNICAL
- Account blocked/deactivated: 5-10% → STOP

We use the midpoint of each range, normalized to 100%.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from mandate_doctor.core.models import DebitAttempt, ErrorDetail, FailureBucket

# Distribution based on productgrowth.in industry benchmarks
# Each entry: (error_code, description, source_bucket, weight)
DISTRIBUTION: list[tuple[str, str, FailureBucket, float]] = [
    # TECHNICAL — bank server timeout (35-45%, midpoint 40%)
    ("bank_technical_error", "Bank server timeout during payment processing", FailureBucket.TECHNICAL, 0.25),
    ("timeout", "Payment gateway request timed out", FailureBucket.TECHNICAL, 0.10),
    ("gateway_technical_error", "Gateway technical error", FailureBucket.TECHNICAL, 0.05),

    # AMBIGUOUS — wrong UPI PIN / exceeded attempts (20-30%, midpoint 25%)
    ("invalid_upi_pin", "Incorrect UPI PIN entered", FailureBucket.AMBIGUOUS, 0.10),
    ("incorrect_pin", "Wrong PIN provided for authentication", FailureBucket.AMBIGUOUS, 0.05),
    ("pin_attempts_exceeded", "Maximum PIN attempts exceeded", FailureBucket.AMBIGUOUS, 0.05),
    ("authentication_failed", "3D secure or OTP authentication failed", FailureBucket.AMBIGUOUS, 0.05),

    # LOW_BALANCE — insufficient balance (15-25%, midpoint 20%)
    ("insufficient_funds", "Insufficient funds in account", FailureBucket.LOW_BALANCE, 0.15),
    ("insufficient_balance", "Account balance lower than transaction amount", FailureBucket.LOW_BALANCE, 0.05),

    # TECHNICAL — network/connectivity (10-15%, midpoint 12.5%)
    ("connection_error", "Network connection lost during transaction", FailureBucket.TECHNICAL, 0.07),
    ("upi_timeout", "UPI network timeout", FailureBucket.TECHNICAL, 0.055),

    # STOP — account blocked/deactivated (5-10%, midpoint 7.5%)
    ("account_closed", "Bank account has been closed", FailureBucket.STOP, 0.02),
    ("account_frozen", "Account frozen by bank", FailureBucket.STOP, 0.015),
    ("mandate_revoked", "Mandate revoked by customer", FailureBucket.STOP, 0.015),
    ("fraud_suspected", "Transaction flagged as potentially fraudulent", FailureBucket.STOP, 0.01),
    ("mandate_expired", "Mandate validity period has expired", FailureBucket.STOP, 0.01),
    ("do_not_honor", "Bank declined the transaction", FailureBucket.STOP, 0.005),
]

# Razorpay error source/step mapping (from official docs)
ERROR_SOURCES = {
    FailureBucket.TECHNICAL: ("bank", "payment"),
    FailureBucket.AMBIGUOUS: ("customer", "authentication"),
    FailureBucket.LOW_BALANCE: ("bank", "payment"),
    FailureBucket.STOP: ("bank", "authorization"),
}


def generate_batch(
    size: int = 500,
    seed: int = 42,
) -> list[DebitAttempt]:
    """Generate a synthetic batch of failed debit attempts.

    Args:
        size: Number of attempts to generate
        seed: Random seed for reproducibility

    Returns:
        List of DebitAttempt objects with realistic error distributions
    """
    rng = random.Random(seed)

    # Normalize weights
    total_weight = sum(w for _, _, _, w in DISTRIBUTION)
    normalized = [(code, desc, bucket, w / total_weight) for code, desc, bucket, w in DISTRIBUTION]

    attempts: list[DebitAttempt] = []
    for i in range(size):
        # Pick error type based on weighted distribution
        r = rng.random()
        cumulative = 0.0
        chosen_code, chosen_desc, chosen_bucket, _ = normalized[0]
        for code, desc, bucket, weight in normalized:
            cumulative += weight
            if r <= cumulative:
                chosen_code, chosen_desc, chosen_bucket = code, desc, bucket
                break

        source, step = ERROR_SOURCES[chosen_bucket]

        attempt = DebitAttempt(
            attempt_id=f"att_synthetic_{i:04d}",
            mandate_id=f"md_synthetic_{i % 100:03d}",  # 100 unique mandates, ~5 attempts each
            amount=rng.choice([19900, 49900, 99900, 149900, 299900, 499900]),  # realistic amounts in paise
            result="failed",
            error=ErrorDetail(
                code=chosen_code,
                description=chosen_desc,
                source=source,
                step=step,
            ),
            is_synthetic=True,
        )
        attempts.append(attempt)

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
    """Print the actual distribution of the generated batch."""
    from collections import Counter

    bucket_counts: Counter[str] = Counter()
    code_counts: Counter[str] = Counter()

    for a in attempts:
        if a.error:
            # Map code to bucket for counting
            from mandate_doctor.core.codes import lookup_bucket
            bucket, _ = lookup_bucket(a.error.code)
            if bucket:
                bucket_counts[bucket.value] += 1
            code_counts[a.error.code] += 1

    total = len(attempts)
    print(f"\nBatch size: {total}")
    print(f"\nBy bucket:")
    for bucket, count in bucket_counts.most_common():
        print(f"  {bucket}: {count} ({count/total*100:.1f}%)")
    print(f"\nBy error code:")
    for code, count in code_counts.most_common():
        print(f"  {code}: {count} ({count/total*100:.1f}%)")


if __name__ == "__main__":
    batch = generate_batch(size=500, seed=42)
    save_batch(batch, Path("eval/synthetic_batch.json"))
    print_distribution(batch)
    print(f"\nSaved to eval/synthetic_batch.json")
