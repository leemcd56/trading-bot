# ai_review.py
"""
Second-opinion AI gate for TA-driven BUY/SELL decisions.

The rules engine (analysis.py + trading.py) still forms its own buy/sell
assumption first, exactly as before — nothing here changes the indicator
gates. Only once the rules engine already wants to act does `confirm_trade`
hand that specific proposed trade, plus the indicator snapshot behind it, to
an LLM and ask it to CONFIRM or VETO. The AI never originates a trade, never
picks the symbol or side, and never overrides a HOLD.

Enable with AI_CONFIRMATION_ENABLED=true and ANTHROPIC_API_KEY set in .env.
Disabled by default — existing behavior is unchanged unless opted in.

Fails closed: if the gate is enabled but the API key is missing, the request
errors out, times out, or the response can't be parsed, the trade is VETOed.
A broken integration should never silently fall back to "trade anyway."
"""
import json
import os
import re

import requests
from dotenv import load_dotenv

from utils import logger

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_CONFIRMATION_ENABLED = os.getenv("AI_CONFIRMATION_ENABLED", "false").strip().lower() == "true"
AI_CONFIRMATION_MODEL = os.getenv("AI_CONFIRMATION_MODEL", "claude-sonnet-5")
AI_CONFIRMATION_TIMEOUT_S = 20

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> str:
    """
    Pull the JSON object out of the model's reply, tolerating a markdown code
    fence (```json ... ```) or stray prose around it - models occasionally
    wrap or annotate JSON despite being told to return only the bare object.
    """
    match = _JSON_OBJECT_RE.search(text)
    return match.group(0) if match else text

# Fields pulled from the analysis dict and shown to the reviewer. Keep this in
# sync with the keys analyze_trends() actually returns (analysis.py).
_SNAPSHOT_FIELDS = [
    "current_price", "atr_14",
    "adx", "plus_di", "minus_di", "rsi_14", "macd", "macd_signal", "sma_50",
    "strong_trend", "uptrend",
    "sar_below_price", "sar_above_price", "sar_flipped_to_bull", "sar_flipped_to_bear",
    "near_upper_band", "near_lower_band", "bb_squeeze",
    "bullish_crossover", "bullish_crossover_recent", "bearish_crossover",
    "trending_up_a_lot", "similar_to_yesterday",
    "dive_bombing", "dead_cat_bounce", "extended_decline", "volatility_spike", "avoid_long",
    "adx_rising", "volume_confirmed", "above_long_term_ma",
]

_SYSTEM_PROMPT = (
    "You are a risk-review gate bolted onto a conservative, trend-following stock "
    "trading bot. The bot's rules engine has already decided, purely from its own "
    "technical-indicator gates, that it wants to {action} {symbol}. Your only job is "
    "to sanity-check that one proposed trade against the indicator snapshot you are "
    "given: look for internal contradictions, stale-looking or missing data, or a "
    "setup that is technically-legal-but-unwise despite passing the mechanical gates "
    "(e.g. indicators barely scraping past thresholds, conflicting signals, obvious "
    "chop). You are not asked for a new idea — you cannot pick a different symbol or "
    "a different side, and you cannot turn a BUY into a SELL or vice versa. If the "
    "snapshot reasonably supports the proposed trade, confirm it. "
    'Respond with ONLY a JSON object and nothing else: '
    '{{"decision": "CONFIRM" or "VETO", "reasoning": "one or two sentences"}}'
)


def confirm_trade(symbol: str, action: str, analysis: dict, mode: str) -> dict:
    """
    Ask an LLM to confirm or veto a BUY/SELL the rules engine already decided on.

    Returns {"decision": "CONFIRM" | "VETO", "reasoning": str}.
    When AI_CONFIRMATION_ENABLED is false (the default), always returns CONFIRM
    immediately with no network call, so existing behavior is unaffected unless
    this is explicitly turned on.
    """
    if not AI_CONFIRMATION_ENABLED:
        return {"decision": "CONFIRM", "reasoning": "AI confirmation disabled"}

    if not ANTHROPIC_API_KEY:
        logger.warning(
            "AI_CONFIRMATION_ENABLED=true but ANTHROPIC_API_KEY is not set; vetoing %s %s",
            action, symbol,
        )
        return {"decision": "VETO", "reasoning": "ANTHROPIC_API_KEY not configured"}

    snapshot = {field: analysis.get(field) for field in _SNAPSHOT_FIELDS}
    user_content = (
        f"Trading mode: {mode}\n"
        f"Proposed action: {action} {symbol}\n"
        f"Indicator snapshot:\n{json.dumps(snapshot, default=str, indent=2)}"
    )
    payload = {
        "model": AI_CONFIRMATION_MODEL,
        "max_tokens": 200,
        "system": _SYSTEM_PROMPT.format(action=action, symbol=symbol),
        "messages": [{"role": "user", "content": user_content}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    try:
        resp = requests.post(_ANTHROPIC_URL, headers=headers, json=payload, timeout=AI_CONFIRMATION_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        parsed = json.loads(_extract_json_object(text))
        decision = str(parsed.get("decision", "")).strip().upper()
        reasoning = str(parsed.get("reasoning", "")).strip()
        if decision not in ("CONFIRM", "VETO"):
            raise ValueError(f"unexpected decision field: {decision!r}")
        logger.info(f"AI review for {action} {symbol}: {decision} - {reasoning}")
        return {"decision": decision, "reasoning": reasoning}
    except Exception as e:
        logger.warning(f"AI confirmation failed for {action} {symbol}: {e}")
        return {"decision": "VETO", "reasoning": f"AI confirmation error: {e}"}
