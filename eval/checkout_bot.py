"""Playwright automation for Razorpay test-mode payment-link checkout.

Drives the real hosted checkout (rzp.io short_url) through:
  mobile number -> Continue -> Netbanking -> bank -> mock bank page
  -> explicit Success / Failure button.

Every outcome is produced by Razorpay's own test gateway; this module
only performs the clicks. The success/failure decision is passed in by
the caller (experimental treatment assignment, recorded in the dataset).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from playwright.async_api import Browser, BrowserContext
from playwright.async_api import TimeoutError as PWTimeout

logger = structlog.get_logger(__name__)

CONTACT_TIMEOUT = 25_000
BANK_PAGE_TIMEOUT = 30_000

# Razorpay's hosted checkout serves a different flow to Playwright's
# default headless UA; a normal Chrome UA + phone viewport gets the
# standard mobile checkout.
CHECKOUT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


async def new_checkout_context(browser: Browser) -> BrowserContext:
    context: BrowserContext = await browser.new_context(
        user_agent=CHECKOUT_UA,
        viewport={"width": 414, "height": 896},
    )
    return context


async def pay_payment_link(
    context: BrowserContext,
    short_url: str,
    mobile: str,
    bank_label: str,
    succeed: bool,
    on_step: Callable[[str, str], Awaitable[None]] | None = None,
) -> str:
    """Complete a test-mode netbanking checkout.

    Returns one of: "paid", "failed", "timeout".
    `on_step(step_name, status)` fires at each pipeline stage so the
    live dashboard can light nodes up in real time.
    """

    async def _emit(step: str, status: str) -> None:
        if on_step is not None:
            await on_step(step, status)

    page = await context.new_page()
    step = "goto"
    try:
        await _emit("goto", "running")
        await page.goto(short_url, wait_until="domcontentloaded", timeout=60_000)
        await _emit("goto", "ok")

        # Mobile layout: link summary first, checkout opens after tapping
        # "Proceed to Pay". Desktop layout goes straight to the iframe.
        step = "proceed"
        proceed = page.get_by_role("button", name="Proceed to Pay")
        try:
            await proceed.click(timeout=6_000)
            await _emit("proceed", "ok")
        except PWTimeout:
            pass  # desktop layout: no summary gate

        # The hosted checkout lives in a (possibly hidden until ready)
        # iframe with class razorpay-checkout-frame.
        step = "checkout_frame"
        frame_el = await page.wait_for_selector(
            "iframe.razorpay-checkout-frame",
            state="attached",
            timeout=CONTACT_TIMEOUT,
        )
        if frame_el is None:
            await _emit("checkout_frame", "timeout")
            return "timeout"
        frame = await frame_el.content_frame()
        if frame is None:
            await _emit("checkout_frame", "timeout")
            return "timeout"
        step = "contact_visible"
        await frame.get_by_test_id("contactNumber").wait_for(
            state="visible", timeout=CONTACT_TIMEOUT
        )

        step = "contact_submit"
        await _emit("contact_submit", "running")
        mobile_box = frame.get_by_test_id("contactNumber")
        await mobile_box.click()
        await mobile_box.press_sequentially(mobile, delay=40)
        await frame.get_by_role("button", name="Continue").click()
        await _emit("contact_submit", "ok")

        # Step 2: pick the target bank. Click handlers live on the label
        # containers, not the radio inputs. The recommended bank appears
        # as a top-level label; others sit behind the Netbanking section
        # (which expands into a full bank list page state).
        step = "bank_select"
        await _emit("bank_select", "running")
        bank_label_xpath = f"xpath=//label[contains(normalize-space(.), '{bank_label}')]"
        section_xpath = "xpath=//label[starts-with(normalize-space(.), 'Netbanking')]"
        options_ready = frame.locator(bank_label_xpath).or_(frame.locator(section_xpath))
        await options_ready.first.wait_for(state="visible", timeout=CONTACT_TIMEOUT)

        bank_entry = frame.locator(bank_label_xpath)
        async with page.context.expect_page(timeout=BANK_PAGE_TIMEOUT) as new_page_info:
            if await bank_entry.count() > 0:
                await bank_entry.first.click()
            else:
                await frame.locator(section_xpath).first.click()
                expanded = frame.locator(bank_label_xpath)
                await expanded.first.click()
        bank_page = await new_page_info.value
        await _emit("bank_select", "ok")

        # Step 4: mock bank page — the measured moment of truth
        step = "bank_choice"
        await _emit("bank_choice", "running")
        await bank_page.wait_for_load_state("domcontentloaded")
        choice = "Success" if succeed else "Failure"
        btn = bank_page.get_by_role("button", name=choice)
        await btn.click(timeout=BANK_PAGE_TIMEOUT)
        await _emit("bank_choice", choice.lower())

        logger.info("checkout_completed", outcome=choice.lower(), bank=bank_label)
        return "paid" if succeed else "failed"

    except PWTimeout:
        logger.warning("checkout_timeout", url=short_url, step=step)
        await _emit(step, "timeout")
        return "timeout"
    finally:
        await page.close()


def npcibank_to_rzp_bank(npci_bank: str) -> str:
    """Map an NPCI remitter-bank name to the display fragment Razorpay
    uses for its netbanking entry (verified against the live test-mode
    bank list). Large banks missing from that list (SBI, HDFC, ICICI,
    Axis, Kotak) fall back to Bank of Baroda; the substitution is
    recorded per-row in the dataset via the rzp_bank column.
    """
    mapping = {
        "state bank of india": "Bank of Baroda",
        "bank of baroda": "Bank of Baroda",
        "union bank of india": "Union Bank of India",
        "canara bank": "Canara Bank",
        "punjab national bank": "Punjab National Bank",
        "hdfc bank": "Bank of Baroda",
        "icici bank": "Bank of Baroda",
        "axis bank": "Bank of Baroda",
        "kotak mahindra bank": "Bank of Baroda",
        "idbi bank": "IDBI",
        "indusind bank": "Indusind",
        "yes bank": "Yes Bank",
        "federal bank": "Bank of Baroda",
        "idfc first bank": "IDFC",
        "au small finance bank": "AU Small Finance Bank",
        "central bank of india": "Central Bank of India",
        "bank of india": "Bank of India",
        "indian bank": "Indian Bank",
        "airtel payments bank": "Bank of Baroda",
        "ujjivan small finance bank": "Ujjivan",
        "equitas small finance bank": "Equitas",
    }
    key = npci_bank.strip().lower()
    if key in mapping:
        return mapping[key]
    for candidate, label in mapping.items():
        if candidate.split()[0] in key:
            return label
    return "Bank of Baroda"


async def smoke_test() -> None:
    """Manual sanity run: `python -m eval.checkout_bot <short_url>`."""
    import sys

    from playwright.async_api import async_playwright

    url = sys.argv[1]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await new_checkout_context(browser)
        outcome = await pay_payment_link(
            ctx, url, mobile="9820123456", bank_label="Bank of Baroda", succeed=False
        )
        print(f"outcome: {outcome}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(smoke_test())
