"""Minimal HTTP/1.1 control API: h11 parses requests, a route table answers with JSON.

One request per connection (`Connection: close`), no request bodies (anything sent is read and
discarded, over 64 KiB is refused), no authentication: bind it to loopback unless published
deliberately.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
from collections.abc import Callable, Sequence
from http import HTTPStatus

import h11

Route = tuple[str, str, Callable[[dict[str, str]], tuple[int, object]]]
"""(method, path pattern such as "/faults/{name}/enable", handler(params) -> (status, json))"""

MAX_BODY = 64 * 1024
READ_TIMEOUT_S = 5.0

log = logging.getLogger(__name__)


class ControlServer:
    def __init__(self, routes: Sequence[Route], *, host: str, port: int) -> None:
        self._routes = [
            (method, pattern.strip("/").split("/"), fn) for method, pattern, fn in routes
        ]
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()  # open connections, cancelled by close()

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._serve, self._host, self._port)
        host, port = self._server.sockets[0].getsockname()[:2]
        if not ipaddress.ip_address(host).is_loopback:
            log.warning(
                "control API on %s:%d has no authentication; publish it to loopback only, "
                "e.g. docker run -p 127.0.0.1:%d:%d", host, port, port, port,
            )  # fmt: skip
        return host, port

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for task in list(self._tasks):  # wait_closed() would wait for idle connections on 3.12+
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            try:
                status, body = await self._handle(reader)
            except h11.RemoteProtocolError as exc:
                status, body = 400, {"error": f"bad request: {exc}"}
            except TimeoutError:  # before OSError: it is a subclass
                status, body = 408, {"error": f"no complete request within {READ_TIMEOUT_S:g} s"}
            payload = json.dumps(body).encode()
            head = (
                f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
                f"content-type: application/json\r\ncontent-length: {len(payload)}\r\n"
                "connection: close\r\n\r\n"
            )
            writer.write(head.encode() + payload)
            await writer.drain()
        except OSError:
            pass  # the client reset the connection; nothing to answer
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self._tasks.discard(task)

    async def _handle(self, reader: asyncio.StreamReader) -> tuple[int, object]:
        conn = h11.Connection(h11.SERVER)
        request: h11.Request | None = None
        body = 0
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                conn.receive_data(await asyncio.wait_for(reader.read(65536), READ_TIMEOUT_S))
            elif isinstance(event, h11.Request):
                request = event
            elif isinstance(event, h11.Data):
                body += len(event.data)  # read and discarded: no route takes a body
                if body > MAX_BODY:
                    return 413, {"error": f"request body over {MAX_BODY} bytes"}
            elif isinstance(event, h11.EndOfMessage):
                break
            else:  # ConnectionClosed or PAUSED before a full request
                return 400, {"error": "connection closed before the request ended"}
        assert request is not None
        return self._dispatch(request.method.decode(), request.target.decode())

    def _dispatch(self, method: str, target: str) -> tuple[int, object]:
        path = target.split("?", 1)[0].strip("/").split("/")
        allowed: list[str] = []
        for route_method, pattern, handler in self._routes:
            params = _bind(pattern, path)
            if params is None:
                continue
            if route_method != method:
                allowed.append(route_method)
                continue
            return handler(params)
        shown = "/" + "/".join(path)
        if allowed:
            hint = f"use {', '.join(allowed)}"
            return 405, {"error": f"{method} is not allowed on {shown}", "hint": hint}
        known = sorted({"/" + "/".join(p) for _, p, _ in self._routes})
        return 404, {"error": f"no route for {shown}", "hint": "paths: " + ", ".join(known)}


def _bind(pattern: list[str], path: list[str]) -> dict[str, str] | None:
    if len(pattern) != len(path):
        return None
    params: dict[str, str] = {}
    for part, actual in zip(pattern, path, strict=True):
        if part.startswith("{") and part.endswith("}"):
            if not actual:
                return None
            params[part[1:-1]] = actual
        elif part != actual:
            return None
    return params
