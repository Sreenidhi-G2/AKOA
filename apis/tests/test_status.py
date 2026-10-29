import pytest

from app.config import load_settings
from app.status import compute_status

CFG = load_settings()
BASE = dict(desired=2, ready=2, cpu_pct=50, req_rate=20, error_rate=0.0, restarts_1h=0, cfg=CFG)


def status(**over):
    return compute_status(**{**BASE, **over})[0]


@pytest.mark.parametrize(
    "over, expected",
    [
        ({}, "Healthy"),
        ({"desired": None}, "Unknown"),
        ({"ready": 0}, "Critical"),
        ({"ready": 1}, "Degraded"),
        ({"error_rate": 0.06}, "Degraded"),
        ({"error_rate": 0.02}, "Warning"),
        ({"cpu_pct": 120}, "Warning"),
        ({"restarts_1h": 3}, "Warning"),
        ({"cpu_pct": 4, "req_rate": 0.3}, "Underutilized"),
        # low CPU but real traffic is not idle
        ({"cpu_pct": 4, "req_rate": 20}, "Healthy"),
        # unknown traffic: cannot call it idle
        ({"cpu_pct": 4, "req_rate": None}, "Healthy"),
        # precedence: Critical beats a CPU warning
        ({"ready": 0, "cpu_pct": 300}, "Critical"),
    ],
)
def test_status_rules(over, expected):
    assert status(**over) == expected


def test_reasons_are_explanatory():
    _, reasons = compute_status(**{**BASE, "cpu_pct": 150})
    assert "150%" in reasons[0] and "request" in reasons[0]
