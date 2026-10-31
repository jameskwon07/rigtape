"""asyncio Modbus TCP listener with one sequential request loop per connection."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from rigtape.modbus import FramingError, Request, encode_adu, exception_pdu, read_adu

log = logging.getLogger(__name__)


class Handler(Protocol):
    """Answers one request. Synchronous, so each request is atomic on the event loop."""

    def handle(self, req: Request) -> bytes | None:
        """Return the response PDU, or None to send nothing."""


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do with one request before the handler sees it; the default answers normally."""

    action: Literal["respond", "no_response", "exception", "disconnect"] = "respond"
    delay_s: float = 0.0  # respond, but this much later
    code: int = 0  # the exception code when action is "exception"
    rule: str | None = None  # the fault rule that fired, for the log


RESPOND = Decision()


class Interceptor(Protocol):
    """Fault injection seam: called once per request, in arrival order."""

    def decide(self, req: Request) -> Decision: ...


class ModbusServer:
    def __init__(
        self, handler: Handler, *, host: str, port: int, interceptor: Interceptor | None = None
    ) -> None:
        self._handler = handler
        self._interceptor = interceptor
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._conn_ids = itertools.count(1)
        # Open connections, closed by close() itself: wait_closed() waits for them on 3.12+,
        # and Server.close_clients() only exists from 3.13.
        self._connections: dict[int, tuple[asyncio.StreamWriter, asyncio.Task[None]]] = {}

    async def start(self) -> tuple[str, int]:
        """Start listening; returns the bound address, so port 0 picks a free port."""
        self._server = await asyncio.start_server(self._serve, self._host, self._port)
        host, port = self._server.sockets[0].getsockname()[:2]
        return host, port

    async def close(self) -> None:
        """Stop accepting, close every client connection, then wait for them to finish."""
        server, self._server = self._server, None  # connections that start later end at once
        if server is None:
            return
        server.close()
        connections = list(self._connections.values())
        for writer, task in connections:  # abort if unsent data would hold a graceful close open
            (writer.transport.abort if writer.transport.get_write_buffer_size() else writer.close)()
            task.cancel()  # a sleeping delay fault must not hold the shutdown either
        await asyncio.gather(*(task for _, task in connections), return_exceptions=True)
        await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn_id = next(self._conn_ids)
        task = asyncio.current_task()
        assert task is not None
        self._connections[conn_id] = (writer, task)
        try:
            while self._server is not None and (adu := await read_adu(reader)) is not None:
                tid, unit, pdu = adu
                req = Request(conn_id, tid, unit, pdu)
                decision = self._interceptor.decide(req) if self._interceptor else RESPOND
                if decision.rule:
                    what = f"delay {decision.delay_s:g} s" if decision.delay_s else decision.action
                    log.info("connection %d: fault %s: %s", conn_id, decision.rule, what)
                if decision.action == "no_response":
                    continue  # not executed; the connection stays open
                if decision.action == "disconnect":
                    break  # not executed; closed with FIN below
                if decision.action == "exception":
                    response: bytes | None = exception_pdu(req.fc, decision.code)
                else:
                    response = self._handler.handle(req)  # executed now, answered after the delay
                    if decision.delay_s:
                        await asyncio.sleep(decision.delay_s)
                if response is not None:
                    writer.write(encode_adu(tid, unit, response))
                    await writer.drain()
        except FramingError as exc:
            log.warning("connection %d closed: %s", conn_id, exc)
        except OSError:
            pass  # reset, timed out or unreachable: nobody is left to answer
        finally:
            del self._connections[conn_id]
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
