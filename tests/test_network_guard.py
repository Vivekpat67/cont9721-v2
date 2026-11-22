"""The test-wide network guard still refuses every non-loopback address."""
import socket

import pytest
from conftest import NetworkBlocked


@pytest.mark.parametrize("addr", [("8.8.8.8", 53), ("api.crustdata.com", 443), ("2001:4860:4860::8888", 53)])
def test_external_connections_are_refused(addr):
    s = socket.socket(socket.AF_INET6 if ":" in addr[0] else socket.AF_INET)
    try:
        with pytest.raises(NetworkBlocked):
            s.connect(addr)
        with pytest.raises(NetworkBlocked):
            s.connect_ex(addr)
    finally:
        s.close()
    with pytest.raises(NetworkBlocked):
        socket.create_connection(addr, timeout=1)
