"""Tests for the idempotency layer (at-most-once recovery execution)."""

from __future__ import annotations

import asyncio

import pytest

from mandate_doctor.core.idempotency import IdempotencyRepository


@pytest.fixture
def repo(tmp_path) -> IdempotencyRepository:
    return IdempotencyRepository(tmp_path / "idem_test.db")


def test_claim_won_once_then_deduplicated(repo: IdempotencyRepository) -> None:
    first = repo.claim("pay_1:RECOVER")
    assert first.won is True

    second = repo.claim("pay_1:RECOVER")
    assert second.won is False
    assert second.cached_state == "PENDING"


def test_decision_cache_roundtrip(repo: IdempotencyRepository) -> None:
    repo.claim("pay_2:RECOVER")
    repo.record_decision("pay_2:RECOVER", "RECOVERY_LINK", "technical failure")

    dup = repo.claim("pay_2:RECOVER")
    assert dup.won is False
    assert dup.cached_decision == "RECOVERY_LINK"
    assert dup.cached_reason == "technical failure"


def test_execution_pk_rejects_double_execution(repo: IdempotencyRepository) -> None:
    repo.claim("pay_3:RECOVER")
    assert repo.record_execution("pay_3:RECOVER", "plink_A") is True
    assert repo.record_execution("pay_3:RECOVER", "plink_B") is False
    # original execution ref is preserved
    assert repo.get_execution("pay_3:RECOVER") is not None
    assert repo.get_execution("pay_3:RECOVER")[0] == "plink_A"


def test_stale_sweep_escalates_abandoned_claims(repo: IdempotencyRepository) -> None:
    repo.claim("pay_4:RECOVER")
    # backdate the claim so it looks abandoned
    conn = repo._get_conn()
    with conn:
        conn.execute(
            "UPDATE recovery_claims SET claimed_at = ? WHERE idempotency_key = ?",
            ("2020-01-01T00:00:00+00:00", "pay_4:RECOVER"),
        )
    swept = repo.sweep_stale(max_pending_minutes=10)
    assert swept == 1
    dup = repo.claim("pay_4:RECOVER")
    assert dup.cached_decision == "ESCALATE_STALE"


def test_stats_counts(repo: IdempotencyRepository) -> None:
    repo.claim("a:RECOVER")
    repo.claim("a:RECOVER")  # dedup
    repo.record_execution("a:RECOVER", "ref_a")
    repo.claim("b:RECOVER")  # pending, not executed
    s = repo.stats()
    assert s["claims"] == 2
    assert s["executed"] == 1
    assert s["deduplicated"] == 1


async def _race(repo: IdempotencyRepository, n: int) -> list:
    return await asyncio.gather(
        *(asyncio.to_thread(repo.claim, "pay_race:RECOVER") for _ in range(n))
    )


def test_concurrent_claims_exactly_one_wins(tmp_path) -> None:
    """10 coroutines race the same key across threads — exactly 1 wins."""
    repo = IdempotencyRepository(tmp_path / "race.db")
    results = asyncio.run(_race(repo, 10))
    wins = sum(1 for r in results if r.won)
    assert wins == 1
    assert sum(1 for r in results if not r.won) == 9
