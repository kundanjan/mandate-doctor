"""Outcome data collection pipeline.

Generates scenarios calibrated by the frozen NPCI AutoPay snapshot
(data/npci-autopay-execution-2026-07.csv), executes each through real
Razorpay test-mode APIs (order -> payment link -> hosted checkout via
Playwright), and records the measured outcome to SQLite.

Data provenance per record:
  bank          - sampled with weights = NPCI declined volume (measured)
  error_class   - bd/td sampled from that bank's NPCI BD/TD split (measured)
  amount        - from a fixed price-point grid (experiment design)
  regime        - explicit experimental factor: how often the simulated
                  customer completes the recovery payment (labeled column,
                  not a hidden constant)
  recovered     - measured: link.status == "paid" after the checkout run
  webhook events- captured by the running API server for each scenario

No outcome probability is invented anywhere in this module.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import random
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import httpx
import structlog
from playwright.async_api import BrowserContext, async_playwright

from eval.checkout_bot import new_checkout_context, npcibank_to_rzp_bank, pay_payment_link
from mandate_doctor.api.events import EventSink
from mandate_doctor.config import settings
from mandate_doctor.services.razorpay import (
    RazorpayError,
    create_payment_link,
)

logger = structlog.get_logger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NPCI_CSV = DATA_DIR / "npci-autopay-execution-2026-07.csv"
DEFAULT_DB = DATA_DIR / "training_data.db"

BASE_URL = "https://api.razorpay.com/v1"
LOCAL_API = "http://localhost:8000"

# Fixed subscription price points (paise). Part of experiment design.
AMOUNTS_PAISE = [19_900, 49_900, 99_900, 149_900, 299_900]

# Explicit behavioral regimes for the simulated payer. These are labeled
# experimental factors recorded per-row — swept arms, not hidden priors.
REGIMES: dict[str, float] = {
    "pessimistic": 0.30,
    "base": 0.50,
    "optimistic": 0.75,
}


class BankWeights(TypedDict):
    bank: str
    declined_volume: float
    bd_share: float
    approved_pct: float


class Scenario(TypedDict):
    scenario_key: str
    npci_bank: str
    rzp_bank: str
    error_class: str
    amount_paise: int
    regime: str
    retry_prior: float


@dataclass(slots=True)
class ScenarioRow:
    scenario_key: str
    batch_id: str
    npci_bank: str
    rzp_bank: str
    error_class: str
    amount_paise: int
    regime: str
    order_id: str | None
    plink_id: str | None
    short_url: str | None
    assigned_click: str | None
    recovered: int
    poll_status: str | None
    error: str | None
    created_at: str
    failed_payment_id: str | None = None
    failure_error_code: str | None = None
    retry_prior: float | None = None


def load_bank_weights(csv_path: Path | None = None) -> list[BankWeights]:
    """Banks sorted by declining volume, straight from the frozen CSV."""
    path = csv_path or NPCI_CSV
    rows_by_bank: dict[str, dict[str, float]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            entry = rows_by_bank.setdefault(row["remitter_bank"], {})
            try:
                entry[row["category"]] = float(row["value"])
            except ValueError:
                continue

    banks: list[BankWeights] = []
    for name, vals in rows_by_bank.items():
        volume = vals.get("Total Volume", 0.0)
        bd = vals.get("BD", 0.0)
        td = vals.get("TD", 0.0)
        banks.append(
            {
                "bank": name,
                "declined_volume": volume * (bd + td) / 100.0,
                "bd_share": bd / (bd + td) if (bd + td) > 0 else 0.5,
                "approved_pct": max(0.0, 100.0 - bd - td),
            }
        )
    banks.sort(key=lambda b: b["declined_volume"], reverse=True)
    return banks


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            scenario_key TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            npci_bank TEXT NOT NULL,
            rzp_bank TEXT NOT NULL,
            error_class TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            regime TEXT NOT NULL,
            order_id TEXT,
            plink_id TEXT,
            short_url TEXT,
            assigned_click TEXT,
            recovered INTEGER NOT NULL DEFAULT 0,
            poll_status TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_outcomes_batch ON outcomes(batch_id);
        """
    )
    # v2 columns (two-phase: real bounce + retry prior); tolerate re-runs
    for stmt in (
        "ALTER TABLE outcomes ADD COLUMN failed_payment_id TEXT",
        "ALTER TABLE outcomes ADD COLUMN failure_error_code TEXT",
        "ALTER TABLE outcomes ADD COLUMN retry_prior REAL",
    ):
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(stmt)
    conn.commit()
    return conn


def _draw(scenario_key: str, salt: str) -> float:
    digest = hashlib.blake2b(f"{salt}|{scenario_key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def design_scenario(
    rng: random.Random, banks: list[BankWeights], batch_id: str, i: int
) -> Scenario:
    """Sample one scenario: bank weighted by measured declined volume,
    error class from that bank's measured BD/TD split."""
    weights = [b["declined_volume"] for b in banks]
    bank = rng.choices(banks, weights=weights, k=1)[0]
    key = f"{batch_id}_{i}"
    error_class = "td" if _draw(key, "errclass") > bank["bd_share"] else "bd"
    rzp_bank = npcibank_to_rzp_bank(bank["bank"])
    return {
        "scenario_key": key,
        "npci_bank": bank["bank"],
        "rzp_bank": rzp_bank,
        "retry_prior": bank["approved_pct"] / 100.0,
        "error_class": error_class,
        "amount_paise": rng.choice(AMOUNTS_PAISE),
        "regime": rng.choice(list(REGIMES)),
    }


async def collect_one(
    scn: Scenario,
    batch_id: str,
    context: BrowserContext,
    auth: tuple[str, str],
    sink: EventSink | None = None,
) -> ScenarioRow:
    async def emit(event: dict[str, Any]) -> None:
        if sink is not None:
            await sink.publish({"scenario": scn["scenario_key"], **event})

    now = datetime.now(UTC).isoformat()
    row = ScenarioRow(
        scenario_key=scn["scenario_key"],
        batch_id=batch_id,
        npci_bank=scn["npci_bank"],
        rzp_bank=scn["rzp_bank"],
        error_class=scn["error_class"],
        amount_paise=scn["amount_paise"],
        regime=scn["regime"],
        order_id=None,
        plink_id=None,
        short_url=None,
        assigned_click=None,
        recovered=0,
        poll_status=None,
        error=None,
        retry_prior=scn["retry_prior"],
        created_at=now,
    )

    try:

        async def _on_step(step: str, status: str) -> None:
            await emit({"type": "step", "node": f"bot_{step}", "status": status})

        # ---- PHASE A: the mandate debit bounce (real failed payment) ----
        # Test mode cannot execute a server-side mandate debit, so the
        # debit attempt uses the closest executable vehicle: a checkout
        # session that FAILS. The failure is a real Razorpay payment
        # with a real error code, captured below.
        await emit({"type": "step", "node": "order", "status": "running"})
        debit_link = await create_payment_link(
            amount_paise=scn["amount_paise"],
            reference_id=f"{scn['scenario_key']}_debit",
            description=f"Debit attempt {scn['error_class'].upper()} {scn['npci_bank']}",
            send_sms=False,
            send_email=False,
        )
        row.order_id = debit_link.get("order_id")
        await emit(
            {
                "type": "step",
                "node": "order",
                "status": "ok",
                "detail": debit_link["id"],
            }
        )

        await pay_payment_link(
            context=context,
            short_url=debit_link["short_url"],
            mobile="9820123456",
            bank_label=scn["rzp_bank"],
            succeed=False,  # the bounce
            on_step=_on_step,
        )

        # Capture the REAL failure evidence: the payment.failed webhook
        # (HMAC-verified by the API server) is indexed by reference_id.
        evidence = await _wait_for_bounce_evidence(f"{scn['scenario_key']}_debit", timeout_s=20.0)
        if evidence is not None:
            row.order_id = evidence.get("order_id")
            row.failed_payment_id = evidence.get("payment_id")
            row.failure_error_code = evidence.get("error_code") or evidence.get("error_description")
            await emit(
                {
                    "type": "step",
                    "node": "bounce",
                    "status": "ok",
                    "detail": f"{row.failed_payment_id} · {row.failure_error_code}",
                }
            )
        else:
            await emit(
                {
                    "type": "step",
                    "node": "bounce",
                    "status": "error",
                    "detail": "no webhook evidence received",
                }
            )

        # ---- PHASE B: recovery intervention (payment link) ----
        await emit({"type": "step", "node": "link", "status": "running"})
        link = await create_payment_link(
            amount_paise=scn["amount_paise"],
            reference_id=scn["scenario_key"],
            description=f"Recovery {scn['error_class'].upper()} {scn['npci_bank']}",
            send_sms=False,
            send_email=False,
        )
        row.plink_id = link["id"]
        row.short_url = link["short_url"]
        await emit({"type": "step", "node": "link", "status": "ok", "detail": link["id"]})

        # Treatment assignment: does the simulated payer complete payment?
        # Regime is an explicitly labeled factor; the draw is deterministic
        # per scenario key for reproducibility.
        pays = _draw(scn["scenario_key"], f"pays|{scn['regime']}") < REGIMES[scn["regime"]]
        row.assigned_click = "success" if pays else "failure"

        bot_outcome = await pay_payment_link(
            context=context,
            short_url=row.short_url or "",
            mobile="9820123456",
            bank_label=scn["rzp_bank"],
            succeed=pays,
            on_step=_on_step,
        )
        if bot_outcome == "timeout":
            row.error = "checkout_timeout"
            await emit({"type": "step", "node": "bot", "status": "timeout"})
            return row

        # Measure truth from the API, not from what we clicked. Failure
        # clicks keep the link in "created" — one confirmation poll
        # suffices; success clicks need up to 25s for the capture chain.
        await emit({"type": "step", "node": "poll", "status": "running"})
        status = await _poll_link(
            row.plink_id or "",
            auth,
            timeout_s=25.0 if pays else 6.0,
        )
        row.poll_status = status
        row.recovered = 1 if status == "paid" else 0

        # Final chance: webhook delivery may have lagged the whole run.
        if row.failed_payment_id is None:
            late = await _wait_for_bounce_evidence(f"{scn['scenario_key']}_debit", timeout_s=8.0)
            if late is not None:
                row.order_id = late.get("order_id")
                row.failed_payment_id = late.get("payment_id")
                row.failure_error_code = late.get("error_code")
        await emit(
            {"type": "step", "node": "poll", "status": "ok", "detail": status or "unconfirmed"}
        )

    except RazorpayError as exc:
        row.error = f"razorpay:{exc.error_code}:{exc.description[:120]}"
        await emit(
            {
                "type": "step",
                "node": "order",
                "status": "error",
                "detail": f"{exc.error_code}: {exc.description[:80]}",
            }
        )
    except Exception as exc:  # noqa: BLE001 - record and continue
        row.error = f"unexpected:{type(exc).__name__}"
        await emit(
            {"type": "step", "node": "poll", "status": "error", "detail": type(exc).__name__}
        )

    return row


async def _wait_for_bounce_evidence(
    reference_id: str, timeout_s: float = 20.0
) -> dict[str, Any] | None:
    """Poll our own API server for the verified payment.failed evidence."""
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{LOCAL_API}/api/bounce/{reference_id}")
            except httpx.HTTPError:
                await asyncio.sleep(1.5)
                continue
            if resp.status_code == 200:
                evidence: dict[str, Any] = resp.json()
                return evidence
            await asyncio.sleep(1.5)
    return None


async def _poll_link(link_id: str, auth: tuple[str, str], timeout_s: float = 25.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=10.0, auth=auth) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{BASE_URL}/payment_links/{link_id}")
            except httpx.HTTPError:
                await asyncio.sleep(2.0)
                continue
            if resp.status_code == 200:
                status: str = resp.json().get("status", "")
                if status in ("paid", "expired", "cancelled"):
                    return status
                if status == "partially paid":
                    return status
            await asyncio.sleep(3.0)
    return None


async def run_batch(
    n: int,
    workers: int,
    batch_id: str,
    db_path: Path,
    sink: EventSink | None = None,
) -> dict[str, int]:
    async def emit(event: dict[str, Any]) -> None:
        if sink is not None:
            await sink.publish(event)

    banks = load_bank_weights()
    rng = random.Random(42)
    conn = init_db(db_path)
    auth = (settings.razorpay_key_id, settings.razorpay_key_secret)

    scenarios = [design_scenario(rng, banks, batch_id, i) for i in range(n)]
    await emit(
        {"type": "calibration", "banks": len(banks), "top_bank": banks[0]["bank"] if banks else ""}
    )
    queue: asyncio.Queue[Scenario] = asyncio.Queue()
    for scn in scenarios:
        queue.put_nowait(scn)

    counts = {"done": 0, "recovered": 0, "errors": 0}

    async def worker(ctx_factory: Callable[[], Awaitable[BrowserContext]]) -> None:
        context = await ctx_factory()
        await asyncio.sleep(random.uniform(1.0, 4.0))  # stagger starts
        try:
            while True:
                try:
                    scn = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                assert scn is not None
                await asyncio.sleep(random.uniform(2.0, 5.0))  # smooth API pressure
                await emit(
                    {
                        "type": "scenario_start",
                        **{
                            k: scn[k]
                            for k in (
                                "scenario_key",
                                "npci_bank",
                                "rzp_bank",
                                "error_class",
                                "amount_paise",
                                "regime",
                            )
                        },
                    }
                )
                row = await collect_one(scn, batch_id, context, auth, sink=sink)
                conn.execute(
                    """INSERT OR REPLACE INTO outcomes
                       (scenario_key, batch_id, npci_bank, rzp_bank, error_class,
                        amount_paise, regime, order_id, plink_id, short_url,
                        assigned_click, recovered, poll_status, error, created_at,
                        failed_payment_id, failure_error_code, retry_prior)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row.scenario_key,
                        row.batch_id,
                        row.npci_bank,
                        row.rzp_bank,
                        row.error_class,
                        row.amount_paise,
                        row.regime,
                        row.order_id,
                        row.plink_id,
                        row.short_url,
                        row.assigned_click,
                        row.recovered,
                        row.poll_status,
                        row.error,
                        row.created_at,
                        row.failed_payment_id,
                        row.failure_error_code,
                        row.retry_prior,
                    ),
                )
                conn.commit()
                counts["done"] += 1
                counts["recovered"] += row.recovered
                counts["errors"] += 1 if row.error else 0
                await emit(
                    {
                        "type": "scenario_end",
                        "scenario": row.scenario_key,
                        "recovered_this": row.recovered,
                        "status": row.poll_status,
                        "error": row.error,
                        **counts,
                    }
                )
                if counts["done"] % 10 == 0:
                    logger.info("progress", **counts)
        finally:
            await context.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        async def ctx_factory() -> BrowserContext:
            return await new_checkout_context(browser)

        await asyncio.gather(*(worker(ctx_factory) for _ in range(workers)))
        await browser.close()

    conn.close()
    await emit({"type": "batch_end", **counts})
    logger.info("batch_complete", **counts)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=120)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--batch-id", type=str, default=None)
    args = parser.parse_args()

    batch_id = args.batch_id or datetime.now(UTC).strftime("b%Y%m%d%H%M")
    stats = asyncio.run(run_batch(args.n, args.workers, batch_id, DEFAULT_DB))
    print(json.dumps({"batch_id": batch_id, **stats}))


if __name__ == "__main__":
    main()
