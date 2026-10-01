"""Allowlisting HTTPS CONNECT proxy: the replay sandbox's only way out.

The sandbox profile only permits connections to this proxy's local port. The
proxy reads the CONNECT target and either refuses it (403) or splices bytes
both ways. No TLS interception: it never sees request contents, only which
host the agent wants to reach.

The allowlist changes per phase:

    Phase A (prepare)   pypi.org, files.pythonhosted.org / registry.npmjs.org
    Phase B (agent)     api.anthropic.com
    Phase C (judge)     nothing
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

PYPI = frozenset({"pypi.org", "files.pythonhosted.org"})
NPM = frozenset({"registry.npmjs.org"})
ANTHROPIC = frozenset({"api.anthropic.com"})
NOTHING: frozenset[str] = frozenset()


@dataclass
class AllowlistProxy:
    """Runs on 127.0.0.1 in a background thread. Use as a context manager."""

    allow: frozenset[str] = NOTHING
    port: int = 0
    upstream_port: int = 443  # tests point this at a local echo server
    log: list[tuple[str, str]] = field(default_factory=list)  # ("ALLOW" | "BLOCK", "host:port")
    _loop: asyncio.AbstractEventLoop | None = None
    _thread: threading.Thread | None = None
    _ready: threading.Event = field(default_factory=threading.Event)

    def __enter__(self) -> "AllowlistProxy":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("allowlist proxy failed to start")
        return self

    def __exit__(self, *exc) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5)

    def set_allow(self, hosts: frozenset[str]) -> None:
        self.allow = frozenset(hosts)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def blocked(self) -> list[str]:
        return [target for verdict, target in self.log if verdict == "BLOCK"]

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", self.port))
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            server.close()
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(server.wait_closed())
            self._loop.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            method, target, _ = request.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
            writer.close()
            return
        host, _, port = target.rpartition(":")
        if method != "CONNECT" or host not in self.allow or port != "443":
            self.log.append(("BLOCK", target))
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        self.log.append(("ALLOW", target))
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, self.upstream_port), 30)
        except (OSError, asyncio.TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
