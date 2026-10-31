"""Shared test setup: never really wait between emails during tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import send  # noqa: E402


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(send.time, "sleep", lambda seconds: None)
