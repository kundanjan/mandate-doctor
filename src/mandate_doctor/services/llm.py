"""LLM client for ambiguous failure classification.

Only called when:
1. Error code is not in the deterministic lookup table
2. Pattern matching on description found no keywords

This is the "AI at the edge" — rules handle known cases, LLM handles unknowns.
"""

from __future__ import annotations

import json

import httpx
import structlog

from mandate_doctor.config import settings
from mandate_doctor.core.models import FailureBucket

logger = structlog.get_logger(__name__)

CLASSIFICATION_PROMPT = """You are a payment failure classifier for Indian recurring payments (UPI AutoPay / e-NACH).

Given a failed debit attempt's error details, classify it into ONE of these buckets:

- LOW_BALANCE: Customer's account has insufficient funds. Retryable — schedule retry near salary date.
- TECHNICAL: Bank/server/network error, not the customer's fault. Retryable immediately.
- STOP: Fraud suspected, mandate revoked/cancelled, account closed/frozen. NEVER retry.
- AMBIGUOUS: Cannot determine from available information. Hold for human review.

Error code: {error_code}
Error description: {error_description}
Error source: {error_source}
Error step: {error_step}

Respond with ONLY a JSON object:
{{
    "bucket": "LOW_BALANCE|TECHNICAL|STOP|AMBIGUOUS",
    "confidence": 0.0-1.0,
    "reasoning": "one sentence explaining your classification"
}}

Rules:
- If unsure, choose AMBIGUOUS (never guess on money decisions)
- "do not honor" or "declined by bank" without clear reason → AMBIGUOUS
- Specific fraud/revoked/closed signals → STOP
- Timeout/server/network → TECHNICAL
- Balance/funds/insufficient → LOW_BALANCE
"""


async def llm_classify(
    error_code: str,
    error_description: str,
    error_source: str,
    error_step: str,
) -> tuple[FailureBucket, float, str]:
    """Use LLM to classify an ambiguous error.

    Returns (bucket, confidence, reasoning).
    Falls back to AMBIGUOUS on any failure.
    """
    prompt = CLASSIFICATION_PROMPT.format(
        error_code=error_code,
        error_description=error_description,
        error_source=error_source,
        error_step=error_step,
    )

    try:
        response = await _call_llm(prompt)
        result = json.loads(response)

        bucket_str = result.get("bucket", "AMBIGUOUS").upper()
        try:
            bucket = FailureBucket(bucket_str.lower())
        except ValueError:
            bucket = FailureBucket.AMBIGUOUS

        confidence = float(result.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))  # clamp

        reasoning = result.get("reasoning", "LLM classification")

        logger.info(
            "llm_classification",
            error_code=error_code,
            bucket=bucket.value,
            confidence=confidence,
            reasoning=reasoning,
        )

        return bucket, confidence, reasoning

    except Exception as e:
        logger.error(
            "llm_classification_failed",
            error_code=error_code,
            error=str(e),
        )
        return (
            FailureBucket.AMBIGUOUS,
            0.3,
            f"LLM classification failed ({type(e).__name__}) — defaulting to AMBIGUOUS",
        )


async def _call_llm(prompt: str) -> str:
    """Call the LLM API. Supports OpenAI-compatible endpoints.

    Providers (set via env vars):
    - OpenCode Zen: OPENCODE_ZEN_API_KEY + base_url=https://opencode.ai/zen/v1
    - OpenAI: OPENAI_API_KEY + base_url=https://api.openai.com/v1
    - Any OpenAI-compatible: OPENAI_API_KEY + OPENAI_BASE_URL
    """
    import os

    # OpenCode Zen takes priority if key is set
    opencode_key = os.getenv("OPENCODE_ZEN_API_KEY", "")
    if opencode_key:
        api_key = opencode_key
        base_url = "https://opencode.ai/zen/v1"
        model = os.getenv("LLM_MODEL", "deepseek-v4-flash-free")
    else:
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    if not api_key:
        raise RuntimeError(
            "No LLM API key set. Set OPENCODE_ZEN_API_KEY (free at opencode.ai/zen) "
            "or OPENAI_API_KEY. Without LLM, unknown errors default to AMBIGUOUS."
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 200,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
