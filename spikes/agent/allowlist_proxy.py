"""Spike: HTTPS CONNECT proxy that only tunnels to allowlisted hosts.

No TLS interception: it reads the CONNECT target, refuses anything not on
the list with 403, and otherwise splices bytes both ways.
"""
import asyncio
import sys

ALLOW = set()


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(reader, writer):
    request = await reader.readuntil(b"\r\n\r\n")
    method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ", 2)
    host, _, port = target.rpartition(":")
    if method != "CONNECT" or host not in ALLOW or port != "443":
        print(f"BLOCK {method} {target}", flush=True)
        writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    print(f"ALLOW {target}", flush=True)
    up_reader, up_writer = await asyncio.open_connection(host, int(port))
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


async def main(port):
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    async with server:
        await server.serve_forever()


ALLOW.update(sys.argv[2].split(","))
asyncio.run(main(int(sys.argv[1])))
