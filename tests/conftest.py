"""Test-wide guard: no test may open a real network connection.

Every external call in the test-suite goes through mocked transports (httpx.MockTransport, fake requests sessions).
If any code path tried to reach a real host - a model API, Crustdata, a government site - socket.connect raises and
the test fails. This is what backs the claim "no model API request can occur" in the offline suite.
"""
import socket

import pytest


class NetworkBlocked(RuntimeError):
    pass


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refuse(self, address, *a, **k):
        raise NetworkBlocked(f"test tried to open a real network connection to {address!r}")

    # Loopback only (this machine): Windows builds asyncio's internal socket pair, used by FastAPI's TestClient,
    # by connecting to 127.0.0.1. Loopback cannot reach any external host, so the guarantee above still holds.
    def guarded(real):
        def connect(self, address, *a, **k):
            if isinstance(address, tuple) and address and address[0] in LOOPBACK:
                return real(self, address, *a, **k)
            return refuse(self, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(real_connect_ex))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: refuse(None, a[0] if a else None))
    yield
