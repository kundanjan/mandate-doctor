"""Domain models for Mandate Doctor.

Three core objects:
- Mandate: the recurring payment authorization
- DebitAttempt: a single debit attempt with error details
- Decision: the agent's classification + action + reasoning
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MandateStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"
    HALTED = "halted"


class FailureBucket(StrEnum):
    LOW_BALANCE = "low_balance"
    TECHNICAL = "technical"
    STOP = "stop"
    AMBIGUOUS = "ambiguous"


class Action(StrEnum):
    SCHEDULE_RETRY = "schedule_retry"
    RETRY_IMMEDIATELY = "retry_immediately"
    SEND_PAYMENT_LINK = "send_payment_link"
    HOLD_FOR_REVIEW = "hold_for_review"
    TRIGGER_RECONSENT = "trigger_reconsent"
    NO_ACTION = "no_action"


class Mandate(BaseModel):
    """A recurring payment mandate."""

    mandate_id: str
    customer_id: str
    merchant_id: str
    amount: int  # in paise
    frequency: str  # monthly, weekly, daily
    status: MandateStatus = MandateStatus.ACTIVE
    registered_at: datetime = Field(default_factory=datetime.now)
    bank: str | None = None
    payment_method: str = "upi"  # upi, enach, card


class ErrorDetail(BaseModel):
    """Error details from a failed debit attempt, mirroring Razorpay's error schema."""

    code: str
    description: str
    source: str = "bank"  # bank, gateway, customer
    step: str = "payment"  # payment, authentication, authorization
    reason: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)


class DebitAttempt(BaseModel):
    """A single debit attempt against a mandate."""

    attempt_id: str
    mandate_id: str
    timestamp: datetime = Field(default_factory=datetime.now)
    amount: int  # in paise
    result: str = "failed"  # success, failed
    error: ErrorDetail | None = None
    is_synthetic: bool = False  # True for enriched UPI test-mode events


class Decision(BaseModel):
    """The agent's classification and action for a failed debit attempt."""

    attempt_id: str
    mandate_id: str
    bucket: FailureBucket
    confidence: float = 0.0  # 0.0 to 1.0
    signals_used: list[str] = Field(default_factory=list)
    action_taken: Action
    reasoning: str = ""  # human-readable explanation
    retry_budget_remaining: int = 0
    outcome: str | None = None  # resolved, escalated, expired
    timestamp: datetime = Field(default_factory=datetime.now)


class AuditEntry(BaseModel):
    """A single entry in the audit trail."""

    entry_id: str
    mandate_id: str
    attempt_id: str
    decision: Decision
    created_at: datetime = Field(default_factory=datetime.now)
