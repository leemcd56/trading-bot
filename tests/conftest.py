"""
pytest conftest: set required environment variables before any test module
is collected, so config.py does not raise at import time.
"""
import os

# config.py raises if MOTHERDUCK_TOKEN is absent; use a dummy for tests.
os.environ.setdefault("MOTHERDUCK_TOKEN", "test-motherduck-token")

# Default to moderate so config.py picks a valid mode during collection.
os.environ.setdefault("TRADING_MODE", "moderate")
