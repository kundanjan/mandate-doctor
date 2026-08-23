"""NPCI return code lookup table.

Maps Razorpay/NPCI error codes to failure buckets.
Source: NPCI UPI Error Codes, Razorpay Payments Error Codes.

This is the deterministic classifier — no LLM needed for known codes.
"""

from mandate_doctor.core.models import FailureBucket


# NPCI UPI AutoPay / e-NACH return codes → failure bucket
# Codes are lowercase, matched against error.code field
CODE_TO_BUCKET: dict[str, FailureBucket] = {
    # === LOW BALANCE (retryable, time to salary date) ===
    "insufficient_funds": FailureBucket.LOW_BALANCE,
    "insufficient_balance": FailureBucket.LOW_BALANCE,
    "low_balance": FailureBucket.LOW_BALANCE,
    "account_balance_low": FailureBucket.LOW_BALANCE,
    "funds_insufficient": FailureBucket.LOW_BALANCE,

    # === TECHNICAL (retryable immediately, not customer's fault) ===
    "bank_technical_error": FailureBucket.TECHNICAL,
    "gateway_technical_error": FailureBucket.TECHNICAL,
    "timeout": FailureBucket.TECHNICAL,
    "connection_error": FailureBucket.TECHNICAL,
    "server_error": FailureBucket.TECHNICAL,
    "upi_timeout": FailureBucket.TECHNICAL,
    "npci_timeout": FailureBucket.TECHNICAL,
    "switch_error": FailureBucket.TECHNICAL,
    "system_error": FailureBucket.TECHNICAL,
    "temporary_failure": FailureBucket.TECHNICAL,
    "request_timeout": FailureBucket.TECHNICAL,

    # === STOP (never retry — fraud, revoked, closed) ===
    "mandate_revoked": FailureBucket.STOP,
    "mandate_expired": FailureBucket.STOP,
    "mandate_cancelled": FailureBucket.STOP,
    "account_closed": FailureBucket.STOP,
    "account_frozen": FailureBucket.STOP,
    "account_blocked": FailureBucket.STOP,
    "fraud_suspected": FailureBucket.STOP,
    "risk_flagged": FailureBucket.STOP,
    "mandate_not_found": FailureBucket.STOP,
    "invalid_mandate": FailureBucket.STOP,
    "customer_revoked": FailureBucket.STOP,
    "do_not_honor": FailureBucket.STOP,
    "restricted_card": FailureBucket.STOP,
    "lost_card": FailureBucket.STOP,
    "stolen_card": FailureBucket.STOP,
    "pickup_card": FailureBucket.STOP,
    "closed_account": FailureBucket.STOP,

    # === CUSTOMER ACTION NEEDED (not retryable, needs intervention) ===
    "invalid_vpa": FailureBucket.AMBIGUOUS,
    "invalid_upi_pin": FailureBucket.AMBIGUOUS,
    "incorrect_pin": FailureBucket.AMBIGUOUS,
    "pin_attempts_exceeded": FailureBucket.AMBIGUOUS,
    "transaction_limit_exceeded": FailureBucket.AMBIGUOUS,
    "daily_limit_exceeded": FailureBucket.AMBIGUOUS,
    "authentication_failed": FailureBucket.AMBIGUOUS,
    "otp_expired": FailureBucket.AMBIGUOUS,
    "invalid_otp": FailureBucket.AMBIGUOUS,
    "payment_declined": FailureBucket.AMBIGUOUS,
    "user_declined": FailureBucket.AMBIGUOUS,
}

# Confidence scores for deterministic matches
DETERMINISTIC_CONFIDENCE: dict[FailureBucket, float] = {
    FailureBucket.LOW_BALANCE: 0.95,
    FailureBucket.TECHNICAL: 0.90,
    FailureBucket.STOP: 0.98,
    FailureBucket.AMBIGUOUS: 0.85,
}


def lookup_bucket(error_code: str) -> tuple[FailureBucket | None, float]:
    """Look up the failure bucket for a known error code.

    Returns (bucket, confidence) or (None, 0.0) if code is unknown.
    """
    code = error_code.lower().strip()
    bucket = CODE_TO_BUCKET.get(code)
    if bucket is not None:
        return bucket, DETERMINISTIC_CONFIDENCE[bucket]
    return None, 0.0
