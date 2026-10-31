"""`rigtape record`: a transparent proxy between a client and a real device that logs exchanges.

Every client connection gets its own upstream connection and two pumps. Responses are matched
to requests by transaction id (FIFO per id). The proxy never answers on the device's behalf
and never times a request out; it only stops waiting to *record* an answer after a window.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections import deque
from dataclasses import dataclass, field

from rigtape.modbus import FramingError, encode_adu, read_adu
from rigtape.recording.format import RecordingWriter

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Pending:
    unit: int
    pdu: bytes
    t: float


@dataclass
class _Connection:
    id: int
    client: asyncio.StreamWriter
    upstream: asyncio.StreamWriter
    pending: dict[int, deque[_Pending]] = field(default_factory=dict)
    by: str = "client"  # which side closed first
    err: str | None = None


class RecordingProxy:
    def __init__(
        self,
        upstream: tuple[str, int],
        writer: RecordingWriter,
        *,
        host: str,
        port: int,
        pending_window_s: float = 30.0,
    ) -> None:
        self._upstream = upstream
        self._writer = writer
        self._host = host
        self._port = port
        self._window = pending_window_s
        self._server: asyncio.Server | None = None
        self._conn_ids = itertools.count(1)
        self._tasks: dict[asyncio.Task[None], asyncio.StreamWriter] = {}  # live client connections
        self.exchanges = 0
        self.unanswered = 0

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._serve, self._host, self._port)
        host, port = self._server.sockets[0].getsockname()[:2]
        self._writer.begin(f"{host}:{port}")
        return host, port

    async def close(self) -> None:
        server, self._server = self._server, None  # connections that start later end at once
        if server is None:
            return
        server.close()
        for task, writer in list(self._tasks.items()):  # abort if unsent data would block a close
            (writer.transport.abort if writer.transport.get_write_buffer_size() else writer.close)()
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        if self._server is None:  # accepted while close() was cancelling the others
            writer.close()
            return
        self._tasks[task] = writer
        conn_id = next(self._conn_ids)
        peer = writer.get_extra_info("peername")
        self._writer.event("open", conn=conn_id, peer=f"{peer[0]}:{peer[1]}")
        try:
            try:
                up_reader, up_writer = await asyncio.open_connection(*self._upstream)
            except (OSError, UnicodeError) as exc:  # refused, unreachable, or a bad host name
                self._writer.event("close", conn=conn_id, by="upstream", err=f"connect: {exc}")
                log.warning("connection %d: cannot reach upstream: %s", conn_id, exc)
                return
            except asyncio.CancelledError:  # close() ran while the connect was pending
                self._writer.event("close", conn=conn_id, by="proxy", err="connect: cancelled")
                raise
            conn = _Connection(conn_id, writer, up_writer)
            pumps = [
                asyncio.create_task(self._pump_requests(conn, reader)),
                asyncio.create_task(self._pump_responses(conn, up_reader)),
                asyncio.create_task(self._expire(conn)),
            ]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:  # the proxy itself is stopping
                conn.by = "proxy"
                raise
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
                self._finish(conn)
                up_writer.close()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self._tasks.pop(task, None)

    async def _pump_requests(self, conn: _Connection, reader: asyncio.StreamReader) -> None:
        try:
            while (adu := await read_adu(reader)) is not None:
                tid, unit, pdu = adu
                pending = _Pending(unit, pdu, self._writer.now())
                conn.pending.setdefault(tid, deque()).append(pending)
                conn.upstream.write(encode_adu(tid, unit, pdu))
                await conn.upstream.drain()
        except FramingError as exc:
            conn.err = f"framing: {exc}"
        except OSError as exc:
            conn.err = str(exc)
        conn.by = "client"

    async def _pump_responses(self, conn: _Connection, reader: asyncio.StreamReader) -> None:
        try:
            while (adu := await read_adu(reader)) is not None:
                tid, unit, pdu = adu
                now = self._writer.now()
                conn.client.write(encode_adu(tid, unit, pdu))
                await conn.client.drain()
                queue = conn.pending.get(tid)
                if queue:
                    self._record(conn, queue.popleft(), tid, pdu, now)
                else:
                    log.warning("connection %d: response with tid %d has no request", conn.id, tid)
        except FramingError as exc:
            conn.err = f"framing: {exc}"
        except OSError as exc:
            conn.err = str(exc)
        conn.by = "upstream"

    async def _expire(self, conn: _Connection) -> None:
        """Give up recording an answer after the window; the client may still get one later."""
        while True:
            await asyncio.sleep(max(0.01, self._window / 10))
            limit = self._writer.now() - self._window
            for tid, queue in list(conn.pending.items()):
                while queue and queue[0].t <= limit:
                    self._record(conn, queue.popleft(), tid, None, None, err="timeout")

    def _finish(self, conn: _Connection) -> None:
        for tid, queue in conn.pending.items():
            for pending in queue:
                self._record(conn, pending, tid, None, None, err="closed")
        err = {"err": conn.err} if conn.err else {}
        self._writer.event("close", conn=conn.id, by=conn.by, **err)

    def _record(
        self,
        conn: _Connection,
        pending: _Pending,
        tid: int,
        rsp: bytes | None,
        now: float | None,
        err: str | None = None,
    ) -> None:
        self.exchanges += 1
        self.unanswered += rsp is None
        self._writer.event(
            "x", pending.t, conn=conn.id, tid=tid, uid=pending.unit, req=pending.pdu.hex(),
            rsp=None if rsp is None else rsp.hex(),
            lat=None if now is None else round(now - pending.t, 6),
            **({"err": err} if err else {}),
        )  # fmt: skip
