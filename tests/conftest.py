"""Test-wide guard: no test may open a real network connection.

Every external call in the test-suite goes through mocked transports (httpx.MockTransport, fake requests sessions).
If any code path tried to reach a real host - a model API, Crustdata, a government site - socket.connect raises and
the test fails. This is what backs the claim "no model API request can occur" in the offline suite.
"""
import socket

import pytest


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def refuse(self, address, *a, **k):
        raise NetworkBlocked(f"test tried to open a real network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: refuse(None, a[0] if a else None))
    yield
