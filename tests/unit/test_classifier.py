"""Tests for the failure classifier."""

import pytest

from mandate_doctor.core.classifier import classify
from mandate_doctor.core.models import DebitAttempt, ErrorDetail, FailureBucket


def _make_attempt(error_code: str, description: str = "") -> DebitAttempt:
    """Helper to create a test DebitAttempt."""
    return DebitAttempt(
        attempt_id="att_test",
        mandate_id="md_test",
        amount=50000,
        error=ErrorDetail(
            code=error_code,
            description=description or error_code,
            source="bank",
            step="payment",
        ),
    )


class TestDeterministicLookup:
    """Tests for known NPCI error code classification."""

    def test_insufficient_funds_classified_as_low_balance(self):
        attempt = _make_attempt("insufficient_funds")
        bucket, confidence, signals, reasoning = classify(attempt)
        assert bucket == FailureBucket.LOW_BALANCE
        assert confidence >= 0.9
        assert "known_code:insufficient_funds" in signals

    def test_bank_technical_error_classified_as_technical(self):
        attempt = _make_attempt("bank_technical_error")
        bucket, confidence, _, _ = classify(attempt)
        assert bucket == FailureBucket.TECHNICAL
        assert confidence >= 0.85

    def test_mandate_revoked_classified_as_stop(self):
        attempt = _make_attempt("mandate_revoked")
        bucket, confidence, _, _ = classify(attempt)
        assert bucket == FailureBucket.STOP
        assert confidence >= 0.95

    def test_fraud_suspected_classified_as_stop(self):
        attempt = _make_attempt("fraud_suspected")
        bucket, _, _, _ = classify(attempt)
        assert bucket == FailureBucket.STOP

    def test_invalid_vpa_classified_as_ambiguous(self):
        attempt = _make_attempt("invalid_vpa")
        bucket, _, _, _ = classify(attempt)
        assert bucket == FailureBucket.AMBIGUOUS

    def test_case_insensitive_lookup(self):
        attempt = _make_attempt("INSUFFICIENT_FUNDS")
        bucket, _, _, _ = classify(attempt)
        assert bucket == FailureBucket.LOW_BALANCE


class TestPatternMatching:
    """Tests for description-based pattern matching."""

    def test_balance_keyword_in_description(self):
        attempt = _make_attempt("unknown_code", "Transaction failed: insufficient balance in account")
        bucket, confidence, signals, _ = classify(attempt)
        assert bucket == FailureBucket.LOW_BALANCE
        assert "description_balance_keyword" in signals

    def test_technical_keyword_in_description(self):
        attempt = _make_attempt("unknown_code", "Gateway timeout while processing")
        bucket, _, signals, _ = classify(attempt)
        assert bucket == FailureBucket.TECHNICAL
        assert "description_technical_keyword" in signals

    def test_stop_keyword_in_description(self):
        attempt = _make_attempt("unknown_code", "Mandate has been revoked by customer")
        bucket, _, signals, _ = classify(attempt)
        assert bucket == FailureBucket.STOP
        assert "description_stop_keyword" in signals


class TestAmbiguousFallback:
    """Tests for unknown codes falling back to AMBIGUOUS."""

    def test_unknown_code_classified_as_ambiguous(self):
        attempt = _make_attempt("xyzzy_unknown_error", "Something weird happened")
        bucket, confidence, signals, reasoning = classify(attempt)
        assert bucket == FailureBucket.AMBIGUOUS
        assert "unknown_code" in signals
        assert "never guess" in reasoning.lower() or "human review" in reasoning.lower()

    def test_no_error_detail_classified_as_ambiguous(self):
        attempt = DebitAttempt(
            attempt_id="att_test",
            mandate_id="md_test",
            amount=50000,
            error=None,
        )
        bucket, _, _, _ = classify(attempt)
        assert bucket == FailureBucket.AMBIGUOUS
