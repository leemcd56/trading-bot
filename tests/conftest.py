"""
pytest conftest: set required environment variables before any test module
is collected, so config.py does not raise at import time.
"""
import os

# config.py raises if MOTHERDUCK_TOKEN is absent; use a dummy for tests.
os.environ.setdefault("MOTHERDUCK_TOKEN", "test-motherduck-token")

# Default to moderate so config.py picks a valid mode during collection.
os.environ.setdefault("TRADING_MODE", "moderate")

# trading.py constructs an Alpaca client at import time; dummy credentials keep
# module imports stable in tests that patch the client before use.
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

# ai_review.py loads .env itself and reads AI_CONFIRMATION_ENABLED at import
# time. Force it off for the suite regardless of the developer's real .env,
# so execute_trade tests never make a real network call to Anthropic; tests
# that need to exercise the gate mock confirm_trade directly (see
# tests/test_ai_review.py). setdefault() means this only applies when the
# variable isn't already set in the environment running pytest.
os.environ.setdefault("AI_CONFIRMATION_ENABLED", "false")
