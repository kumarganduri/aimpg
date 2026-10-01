import socket
import socketserver
import threading

import pytest

from aimpg.replay.proxy import ANTHROPIC, NOTHING, AllowlistProxy


def connect(proxy: AllowlistProxy, target: str) -> tuple[str, socket.socket]:
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    status = s.recv(1024).decode().split("\r\n")[0]
    return status, s


@pytest.fixture
def echo_server():
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(self.request.recv(1024).upper())

    server = socketserver.TCPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def test_blocks_hosts_not_on_the_list():
    with AllowlistProxy(ANTHROPIC) as proxy:
        status, s = connect(proxy, "github.com:443")
        s.close()
    assert "403" in status
    assert proxy.log == [("BLOCK", "github.com:443")]


def test_blocks_wrong_port_and_plain_http():
    with AllowlistProxy(ANTHROPIC) as proxy:
        assert "403" in connect(proxy, "api.anthropic.com:80")[0]
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        s.sendall(b"GET http://api.anthropic.com/ HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"403" in s.recv(1024)
    assert len(proxy.blocked()) == 2


def test_allowed_host_is_tunnelled(echo_server):
    with AllowlistProxy(frozenset({"localhost"}), upstream_port=echo_server) as proxy:
        status, s = connect(proxy, "localhost:443")
        assert "200" in status
        s.sendall(b"ping")
        assert s.recv(1024) == b"PING"
        s.close()
    assert proxy.log == [("ALLOW", "localhost:443")]


def test_allowlist_switches_between_phases(echo_server):
    with AllowlistProxy(frozenset({"localhost"}), upstream_port=echo_server) as proxy:
        assert "200" in connect(proxy, "localhost:443")[0]
        proxy.set_allow(NOTHING)
        assert "403" in connect(proxy, "localhost:443")[0]


def test_garbage_request_is_dropped():
    with AllowlistProxy(ANTHROPIC) as proxy:
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        s.sendall(b"\x00\x01nonsense\r\n\r\n")
        assert s.recv(1024) == b""
