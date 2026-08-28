#!/usr/bin/env python3
"""A small, restricted HTTP CONNECT proxy for Tailscale control traffic."""

import argparse
import asyncio
import contextlib
import functools

ALLOWED_SUFFIXES = ("tailscale.com", "tailscale.io")
LISTEN_ADDRESS = "127.0.0.1"
MAX_HEADER_BYTES = 16_384
REQUEST_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 10


def destination_allowed(host: str, port: int) -> bool:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return False
    if not host or any(character.isspace() for character in host):
        return False
    try:
        host = host.rstrip(".").lower()
        host.encode("ascii")
    except UnicodeError:
        return False
    return port == 443 and any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_SUFFIXES
    )


class RequestError(ValueError):
    """The client sent a request that is not a supported CONNECT request."""


def parse_connect_request(request: bytes) -> tuple[str, int]:
    """Parse and validate a CONNECT request, returning its host and port."""
    if len(request) > MAX_HEADER_BYTES or not request.endswith(b"\r\n\r\n"):
        raise RequestError("invalid or oversized request headers")

    lines = request[:-4].split(b"\r\n")
    request_line = lines[0].split(b" ") if lines else []
    if len(request_line) != 3:
        raise RequestError("invalid request line")

    method, target, version = request_line
    if method != b"CONNECT" or version not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise RequestError("only HTTP CONNECT is supported")
    if any(not header or b":" not in header for header in lines[1:]):
        raise RequestError("invalid header")

    try:
        target_text = target.decode("ascii")
    except UnicodeDecodeError as error:
        raise RequestError("CONNECT destination is not ASCII") from error

    host, separator, port_text = target_text.rpartition(":")
    if (
        not separator
        or not host
        or not port_text
        or not port_text.isascii()
        or not port_text.isdigit()
    ):
        raise RequestError("invalid CONNECT destination")
    try:
        port = int(port_text)
    except ValueError as error:
        raise RequestError("invalid CONNECT port") from error
    if not destination_allowed(host, port):
        raise RequestError("destination is not allowed")
    return host, port


async def close_writer(writer: asyncio.StreamWriter) -> None:
    if writer.is_closing():
        return
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65_536):
            writer.write(data)
            await writer.drain()
    finally:
        await close_writer(writer)


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    verbose: bool = False,
) -> None:
    peer = writer.get_extra_info("peername")
    tunnel_established = False

    try:
        request = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        host, port = parse_connect_request(request)
    except RequestError as error:
        print(f"DENY {peer} {error}", flush=True)
        if not writer.is_closing():
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            with contextlib.suppress(Exception):
                await writer.drain()
        await close_writer(writer)
        return
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        ConnectionError,
        OSError,
        TimeoutError,
    ) as error:
        print(f"ERROR {peer} {error}", flush=True)
        await close_writer(writer)
        return

    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        tunnel_established = True
        if verbose:
            print(f"ALLOW {peer} {host}:{port}", flush=True)

        await asyncio.gather(
            relay(reader, upstream_writer),
            relay(upstream_reader, writer),
        )
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        ConnectionError,
        OSError,
        TimeoutError,
        UnicodeError,
    ) as error:
        print(f"ERROR {peer} {error}", flush=True)
        if not tunnel_established and not writer.is_closing():
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            with contextlib.suppress(Exception):
                await writer.drain()
    finally:
        await close_writer(writer)


async def main(port: int, verbose: bool = False) -> None:
    server = await asyncio.start_server(
        functools.partial(handle_client, verbose=verbose),
        LISTEN_ADDRESS,
        port,
        limit=MAX_HEADER_BYTES,
    )
    if verbose:
        print(f"LISTEN {LISTEN_ADDRESS}:{port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, choices=range(1, 65536), required=True)
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.port, arguments.verbose))
