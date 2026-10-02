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

# alerts.py reads these at import time too, and most execute_trade/execute_signal_*
# tests only mock trading_client (not send_alert), so without this override every
# such test would fire a REAL Discord message / email using the developer's own
# .env webhook and SMTP settings. setdefault() is enough here for the same reason
# as above: conftest.py runs before any test module imports trading/alerts, so
# this value is already in os.environ by the time alerts.py's own load_dotenv()
# runs (which never overwrites an already-set variable).
os.environ.setdefault("DISCORD_WEBHOOK_URL", "")
os.environ.setdefault("ALERT_EMAIL_TO", "")
os.environ.setdefault("ALERT_EMAIL_SMTP_URL", "")
os.environ.setdefault("ALERT_EMAIL_FROM", "")

# Same reasoning for the heartbeat dead-man's-switch ping (alerts.send_heartbeat):
# without this, any test that runs main.ta_job() to completion would ping a real
# external uptime-monitoring URL from the developer's own .env.
os.environ.setdefault("HEARTBEAT_URL", "")
