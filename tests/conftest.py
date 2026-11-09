import pytest


@pytest.fixture(autouse=True)
def _no_remote_analytics(monkeypatch):
    """Tests never send usage events to PostHog, even after a release key is set."""
    monkeypatch.setenv("DO_NOT_TRACK", "1")
