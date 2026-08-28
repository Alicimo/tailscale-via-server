import asyncio
import contextlib
import functools
import io
import unittest
from unittest import mock

import tailscale_connect_proxy as proxy

REAL_OPEN_CONNECTION = asyncio.open_connection


@contextlib.asynccontextmanager
async def running_proxy(*, verbose=False):
    server = await asyncio.start_server(
        functools.partial(proxy.handle_client, verbose=verbose),
        proxy.LISTEN_ADDRESS,
        0,
        limit=proxy.MAX_HEADER_BYTES,
    )
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


class DestinationTests(unittest.TestCase):
    def test_allows_only_https_official_suffixes(self):
        self.assertTrue(proxy.destination_allowed("controlplane.tailscale.com", 443))
        self.assertTrue(proxy.destination_allowed("derp12.tailscale.io.", 443))

    def test_rejects_suffix_lookalikes_and_other_ports(self):
        self.assertFalse(proxy.destination_allowed("tailscale.com.example", 443))
        self.assertFalse(proxy.destination_allowed("exampletailscale.com", 443))
        self.assertFalse(proxy.destination_allowed("controlplane.tailscale.com", 80))
        self.assertFalse(proxy.destination_allowed("127.0.0.1", 443))


class RequestTests(unittest.TestCase):
    def test_parses_valid_connect_request(self):
        request = (
            b"CONNECT controlplane.tailscale.com:443 HTTP/1.1\r\n"
            b"Host: controlplane.tailscale.com:443\r\n\r\n"
        )
        self.assertEqual(
            proxy.parse_connect_request(request), ("controlplane.tailscale.com", 443)
        )

    def test_rejects_non_connect_requests(self):
        with self.assertRaises(ValueError):
            proxy.parse_connect_request(b"GET https://tailscale.com/ HTTP/1.1\r\n\r\n")

    def test_rejects_unapproved_destination_and_malformed_port(self):
        for target in (
            "example.com:443",
            "controlplane.tailscale.com:80",
            "tailscale.com:x",
            f"controlplane.tailscale.com:{'9' * 5000}",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                proxy.parse_connect_request(
                    f"CONNECT {target} HTTP/1.1\r\n\r\n".encode()
                )


class ProxySocketTests(unittest.IsolatedAsyncioTestCase):
    async def open_client(self, port):
        return await REAL_OPEN_CONNECTION(proxy.LISTEN_ADDRESS, port)

    async def test_denied_request_returns_403_without_connecting_upstream(self):
        upstream = mock.AsyncMock()
        output = io.StringIO()
        with (
            mock.patch.object(proxy.asyncio, "open_connection", upstream),
            contextlib.redirect_stdout(output),
        ):
            async with running_proxy() as port:
                reader, writer = await self.open_client(port)
                writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), timeout=1)
                await proxy.close_writer(writer)

        self.assertTrue(response.startswith(b"HTTP/1.1 403 Forbidden\r\n"))
        self.assertIn("DENY", output.getvalue())
        upstream.assert_not_awaited()

    async def test_upstream_failure_returns_502(self):
        upstream = mock.AsyncMock(side_effect=OSError("connection failed"))
        output = io.StringIO()
        with (
            mock.patch.object(proxy.asyncio, "open_connection", upstream),
            contextlib.redirect_stdout(output),
        ):
            async with running_proxy() as port:
                reader, writer = await self.open_client(port)
                writer.write(b"CONNECT tailscale.com:443 HTTP/1.1\r\n\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), timeout=1)
                await proxy.close_writer(writer)

        self.assertTrue(response.startswith(b"HTTP/1.1 502 Bad Gateway\r\n"))
        self.assertIn("ERROR", output.getvalue())
        upstream.assert_awaited_once_with("tailscale.com", 443)

    async def test_permitted_request_relays_bytes_and_logs_only_when_verbose(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(65_536):
                    writer.write(data)
                    await writer.drain()
            finally:
                await proxy.close_writer(writer)

        upstream_server = await asyncio.start_server(echo, proxy.LISTEN_ADDRESS, 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]

        async def connect_upstream(host, port):
            self.assertEqual((host, port), ("tailscale.com", 443))
            return await REAL_OPEN_CONNECTION(proxy.LISTEN_ADDRESS, upstream_port)

        try:
            for verbose in (False, True):
                with self.subTest(verbose=verbose):
                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            proxy.asyncio,
                            "open_connection",
                            side_effect=connect_upstream,
                        ),
                        contextlib.redirect_stdout(output),
                    ):
                        async with running_proxy(verbose=verbose) as port:
                            reader, writer = await self.open_client(port)
                            writer.write(b"CONNECT tailscale.com:443 HTTP/1.1\r\n\r\n")
                            await writer.drain()
                            response = await asyncio.wait_for(
                                reader.readuntil(b"\r\n\r\n"), timeout=1
                            )
                            self.assertEqual(
                                response,
                                b"HTTP/1.1 200 Connection Established\r\n\r\n",
                            )
                            writer.write(b"relayed")
                            await writer.drain()
                            self.assertEqual(
                                await asyncio.wait_for(
                                    reader.readexactly(len(b"relayed")), timeout=1
                                ),
                                b"relayed",
                            )
                            await proxy.close_writer(writer)
                            await asyncio.sleep(0)

                    self.assertEqual("ALLOW" in output.getvalue(), verbose)
        finally:
            upstream_server.close()
            await upstream_server.wait_closed()

    async def test_oversized_request_is_closed_without_upstream_connection(self):
        upstream = mock.AsyncMock()
        request = (
            b"CONNECT tailscale.com:443 HTTP/1.1\r\nX-Test: "
            + b"x" * proxy.MAX_HEADER_BYTES
            + b"\r\n\r\n"
        )
        output = io.StringIO()
        with (
            mock.patch.object(proxy.asyncio, "open_connection", upstream),
            contextlib.redirect_stdout(output),
        ):
            async with running_proxy() as port:
                reader, writer = await self.open_client(port)
                writer.write(request)
                await writer.drain()
                self.assertEqual(
                    await asyncio.wait_for(reader.read(), timeout=1),
                    b"",
                )
                await proxy.close_writer(writer)

        self.assertIn("ERROR", output.getvalue())
        upstream.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
