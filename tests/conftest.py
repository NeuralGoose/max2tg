"""Shared pytest configuration for the bridge test suite."""
import pytest
import requests


@pytest.fixture(autouse=True)
def _no_real_http(request, monkeypatch):
    """Unit tests must never reach Telegram or a CDN: any request that is not
    mocked fails immediately instead of hanging on a network timeout."""
    if request.node.get_closest_marker("integration"):
        return

    def _blocked(self, prepared, **kwargs):
        raise requests.ConnectionError(
            f"real HTTP disabled in unit tests: {prepared.method} {prepared.url}"
        )

    monkeypatch.setattr(requests.Session, "send", _blocked)
