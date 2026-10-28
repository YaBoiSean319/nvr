"""A reverse proxy for tests, written the way nginx, Cloudflare Tunnel and ``tailscale serve`` behave towards an upstream (#132).

It forwards raw TCP, so WebSocket upgrades pass through, and rewrites each request's head: it appends the address it claims to have seen to
``X-Forwarded-For`` (keeping whatever the caller sent, as proxies do), sets ``X-Forwarded-Proto`` and ``X-Forwarded-Host``, and points ``Host`` at the
upstream (nginx's default), so a service that trusts ``Host`` alone sees the wrong site. Its settings can be changed between requests.
"""
from __future__ import annotations

import asyncio
import threading


class ReverseProxy:
    def __init__(self, upstream_port: int, *, client: str = "203.0.113.9", proto: str = "https", host: str = "nvr.example.test", rewrite_host: bool = True, forward: bool = True) -> None:
        self.upstream_port, self.client, self.proto, self.host, self.rewrite_host, self.forward = upstream_port, client, proto, host, rewrite_host, forward
        self.requests: list[str] = []
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.port = 0
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(10):
            raise RuntimeError("The proxy did not start")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._serve, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        self._server = server
        self._ready.set()
        self._loop.run_forever()

    def close(self) -> None:
        if self._loop:
            async def stop() -> None:
                self._server.close()
                for task in asyncio.all_tasks():
                    if task is not asyncio.current_task():
                        task.cancel()
                await asyncio.sleep(0)
                self._loop.stop()
            asyncio.run_coroutine_threadsafe(stop(), self._loop)

    def _rewrite(self, head: bytes) -> bytes:
        lines = head.decode("latin-1").split("\r\n")
        request_line, headers = lines[0], [line for line in lines[1:] if line]
        self.requests.append(request_line)
        names = {line.split(":", 1)[0].strip().lower() for line in headers}
        kept = [line for line in headers if line.split(":", 1)[0].strip().lower() not in {"x-forwarded-proto", "x-forwarded-host", "x-forwarded-for", "connection"} | ({"host"} if self.rewrite_host else set())]
        sent = [line.split(":", 1)[1].strip() for line in headers if line.split(":", 1)[0].strip().lower() == "x-forwarded-for"]
        if self.forward:
            kept += [f"X-Forwarded-For: {', '.join(sent + [self.client])}", f"X-Forwarded-Proto: {self.proto}", f"X-Forwarded-Host: {self.host}"]
        if self.rewrite_host:
            kept.append(f"Host: 127.0.0.1:{self.upstream_port}")
        kept.append("Connection: Upgrade" if "upgrade" in names else "Connection: close")
        return ("\r\n".join([request_line, *kept]) + "\r\n\r\n").encode("latin-1")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        except (asyncio.IncompleteReadError, OSError):
            writer.close()
            return
        upstream_writer.write(self._rewrite(head[:-4]))
        await upstream_writer.drain()

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            try:
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    sink.close()
                except OSError:
                    pass
        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
