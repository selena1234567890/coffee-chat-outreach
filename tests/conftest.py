"""Shared test setup: never really wait between emails during tests, and leave the
safety checks (guard.py) off except in tests marked @pytest.mark.guard, so other
tests don't need approved templates and real-looking names."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guard  # noqa: E402
import send  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "guard: run with the real safety checks")


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(send.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def guard_off(request, monkeypatch):
    if not request.node.get_closest_marker("guard"):
        monkeypatch.setattr(guard, "check", lambda *a, **k: [])
