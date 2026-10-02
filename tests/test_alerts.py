"""
Tests for alerts.send_heartbeat: the dead-man's-switch ping to an external
uptime-monitoring service. Deliberately not another bot-side alert - see the
function's docstring for why.
"""
from unittest.mock import patch

import alerts


def test_send_heartbeat_noop_without_url():
    """No HEARTBEAT_URL configured -> no HTTP call at all."""
    with patch.object(alerts, "HEARTBEAT_URL", ""), \
         patch("requests.get") as mock_get:
        alerts.send_heartbeat()
    mock_get.assert_not_called()


def test_send_heartbeat_pings_configured_url():
    with patch.object(alerts, "HEARTBEAT_URL", "https://hc-ping.com/test-uuid"), \
         patch("requests.get") as mock_get:
        alerts.send_heartbeat()
    mock_get.assert_called_once_with("https://hc-ping.com/test-uuid", timeout=5)


def test_send_heartbeat_never_raises_on_network_failure():
    """A failed ping must never break the caller - this runs in a trading-loop finally block."""
    with patch.object(alerts, "HEARTBEAT_URL", "https://hc-ping.com/test-uuid"), \
         patch("requests.get", side_effect=Exception("network down")):
        alerts.send_heartbeat()  # must not raise
