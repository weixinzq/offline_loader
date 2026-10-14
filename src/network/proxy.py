"""Explicit SOCKS5 routing shared by login and game transports."""
from __future__ import annotations

import os
from urllib.parse import urlsplit


def process_proxy() -> str | None:
    return os.environ.get("AOLA_PROXY_URL") or None


def proxy_connector(url: str):
    from aiohttp_socks import ProxyConnector, ProxyType

    parsed = urlsplit(url)
    if parsed.scheme != "socks5" or not parsed.hostname or not parsed.port:
        raise ValueError("代理必须是 socks5://host:port")
    return ProxyConnector(
        proxy_type=ProxyType.SOCKS5, host=parsed.hostname, port=parsed.port,
        username=parsed.username, password=parsed.password, rdns=True,
    )


async def proxy_socket(url: str, host: str, port: int, timeout: float):
    from python_socks.async_.asyncio import Proxy

    return await Proxy.from_url(url, rdns=True).connect(
        dest_host=host, dest_port=port, timeout=timeout,
    )
