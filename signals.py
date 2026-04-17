# signals.py
"""
External signal feed via Financial Modeling Prep analyst upgrades/downgrades.

Returns today's buy/sell recommendations:
  fetch_signals() → {"buy": ["MSFT", ...], "sell": ["TSLA", ...]}

Set FMP_API_KEY in .env to enable. If the key is absent or the request
fails the function returns empty lists so the TA loop runs unaffected.

Free tier: 250 calls/day — hourly polling uses ~16 calls on a trading day.
"""
import os
from datetime import date

import requests

from utils import logger

FMP_API_KEY = os.getenv("FMP_API_KEY", "")
_FMP_URL = "https://financialmodelingprep.com/api/v3/upgrades-downgrades"

# Grade strings that count as a buy or sell signal (case-insensitive substring match).
_BUY_GRADES = {"buy", "strong buy", "outperform", "overweight", "accumulate", "conviction buy", "market outperform"}
_SELL_GRADES = {"sell", "strong sell", "underperform", "underweight", "reduce", "market underperform"}

# Only trust entries with these action types.
_BUY_ACTIONS = {"upgrade", "initiated", "reiterated", "maintains"}
_SELL_ACTIONS = {"downgrade", "initiated"}


def _grade_is(grade: str, grade_set: set) -> bool:
    g = grade.lower().strip()
    return any(target in g for target in grade_set)


def fetch_signals() -> dict:
    """
    Query FMP for today's analyst actions and return buy/sell symbol lists.
    Returns {"buy": [], "sell": []} on error or when FMP_API_KEY is not set.
    """
    if not FMP_API_KEY:
        return {"buy": [], "sell": []}

    today = date.today().isoformat()
    try:
        resp = requests.get(_FMP_URL, params={"apikey": FMP_API_KEY}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"FMP signals fetch failed: {e}")
        return {"buy": [], "sell": []}

    if not isinstance(data, list):
        logger.warning(f"FMP signals: unexpected response type {type(data)}")
        return {"buy": [], "sell": []}

    buy: list[str] = []
    sell: list[str] = []
    seen: set[str] = set()

    for entry in data:
        published = (entry.get("publishedDate") or "")[:10]
        if published != today:
            continue

        symbol = (entry.get("symbol") or "").upper().strip()
        if not symbol or symbol in seen:
            continue

        action = (entry.get("action") or "").lower().strip()
        new_grade = entry.get("newGrade") or ""

        if action in _BUY_ACTIONS and _grade_is(new_grade, _BUY_GRADES):
            buy.append(symbol)
            seen.add(symbol)
        elif action in _SELL_ACTIONS and _grade_is(new_grade, _SELL_GRADES):
            sell.append(symbol)
            seen.add(symbol)

    logger.info(f"FMP signals for {today}: buy={buy}, sell={sell}")
    return {"buy": buy, "sell": sell}
