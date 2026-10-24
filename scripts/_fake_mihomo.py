"""Loopback-only SOCKS5 fixture for CLI process tests, never a real proxy."""
import argparse
import asyncio
from pathlib import Path
import signal
import struct

import yaml


async def socks_connection(reader, writer, seen=None):
    remote = None
    try:
        version, count = await reader.readexactly(2)
        assert version == 5
        await reader.readexactly(count)
        writer.write(b"\x05\x00")
        await writer.drain()
        version, command, _reserved, kind = await reader.readexactly(4)
        assert version == 5 and command == 1
        if kind == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        elif kind == 1:
            host = ".".join(str(v) for v in await reader.readexactly(4))
        else:
            raise ValueError("fixture only supports IPv4/domain")
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        # Test names are resolved locally to the fixture, not by real DNS.
        if host not in {"127.0.0.1", "localhost", "game.invalid"}:
            raise ValueError("fixture refuses non-loopback destination")
        if seen is not None:
            seen.append((host, port))
        upstream, remote = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        await writer.drain()
        async def copy(source, target):
            try:
                while chunk := await source.read(65536):
                    target.write(chunk)
                    await target.drain()
            finally:
                target.close()
        await asyncio.gather(copy(reader, remote), copy(upstream, writer))
    except (OSError, ValueError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        if remote:
            remote.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", action="store_true")
    parser.add_argument("-d")
    parser.add_argument("-f", required=True)
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.f).read_text(encoding="utf-8"))
    if args.t:
        return
    done = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, done.set)
    servers = [await asyncio.start_server(socks_connection, "127.0.0.1", item["port"])
               for item in config["listeners"]]
    try:
        await done.wait()
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
