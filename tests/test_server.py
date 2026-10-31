"""ModbusServer serving examples/pid/device.yaml: pymodbus interop and raw framing edge cases."""

from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pymodbus.client import AsyncModbusTcpClient

from rigtape.device import Device, DeviceSpec, load_device
from rigtape.server import ModbusServer

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "pid" / "device.yaml"
HOST = "127.0.0.1"


@pytest.fixture
async def server() -> AsyncIterator[tuple[ModbusServer, int]]:
    srv = ModbusServer(Device(load_device(EXAMPLE)), host=HOST, port=0)
    _, port = await srv.start()
    try:
        yield srv, port
    finally:
        await srv.close()


@pytest.fixture
async def client(server: tuple[ModbusServer, int]) -> AsyncIterator[AsyncModbusTcpClient]:
    c = AsyncModbusTcpClient(HOST, port=server[1], timeout=2, retries=0, reconnect_delay=0)
    assert await c.connect()
    try:
        yield c
    finally:
        c.close()
        await asyncio.sleep(0)


def adu(tid: int, pdu: str, *, unit: int = 1, protocol: int = 0) -> bytes:
    body = bytes.fromhex(pdu)
    return struct.pack(">HHHB", tid, protocol, len(body) + 1, unit) + body


async def raw(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(HOST, port)


async def read_response(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    tid, _, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
    return tid, unit, (await reader.readexactly(length - 1)).hex()


async def test_reads_initial_values(client: AsyncModbusTcpClient) -> None:
    assert (await client.read_input_registers(0, count=1)).registers == [200]
    assert (await client.read_holding_registers(0, count=1)).registers == [200]


async def test_writes_read_back(client: AsyncModbusTcpClient) -> None:
    assert not (await client.write_register(0, 450)).isError()
    assert (await client.read_holding_registers(0, count=1)).registers == [450]
    assert not (await client.write_registers(0, [500])).isError()
    assert (await client.read_holding_registers(0, count=1)).registers == [500]


@pytest.mark.parametrize(
    ("call", "code"),
    [
        pytest.param(lambda c: c.write_register(10, 7), 2, id="readonly"),
        pytest.param(lambda c: c.read_holding_registers(5, count=1), 2, id="undefined"),
        pytest.param(lambda c: c.write_registers(9, [1, 2]), 2, id="fc16-undefined-and-readonly"),
        pytest.param(lambda c: c.read_coils(0, count=1), 1, id="fc1"),
    ],
)
async def test_exceptions(
    client: AsyncModbusTcpClient, call: Callable[[AsyncModbusTcpClient], Awaitable[Any]], code: int
) -> None:
    response = await call(client)
    assert response.isError()
    assert response.exception_code == code
    assert (await client.read_holding_registers(10, count=1)).registers == [1001]


async def test_fc16_with_a_bad_address_writes_nothing(client: AsyncModbusTcpClient) -> None:
    response = await client.write_registers(0, [450, 1])  # HR0 writable, HR1 undefined
    assert response.exception_code == 2
    assert (await client.read_holding_registers(0, count=1)).registers == [200]


async def test_pipelined_requests_answer_in_order(server: tuple[ModbusServer, int]) -> None:
    reader, writer = await raw(server[1])
    writer.write(adu(7, "0400000001") + adu(8, "03000a0001"))
    assert await read_response(reader) == (7, 1, "040200c8")
    assert await read_response(reader) == (8, 1, "030203e9")
    writer.close()


async def test_skips_non_modbus_protocol_frames(server: tuple[ModbusServer, int]) -> None:
    reader, writer = await raw(server[1])
    writer.write(adu(1, "0400000001", protocol=1) + adu(2, "0400000001"))
    assert await read_response(reader) == (2, 1, "040200c8")
    writer.close()


@pytest.mark.parametrize(("tid", "unit"), [(0, 0), (65535, 255), (0x1234, 17)])
async def test_echoes_tid_and_unit(server: tuple[ModbusServer, int], tid: int, unit: int) -> None:
    reader, writer = await raw(server[1])
    writer.write(adu(tid, "0300000001", unit=unit))
    assert await read_response(reader) == (tid, unit, "030200c8")
    writer.close()


async def test_bad_length_closes_the_connection(server: tuple[ModbusServer, int]) -> None:
    reader, writer = await raw(server[1])
    writer.write(struct.pack(">HHHB", 1, 0, 0, 1))  # length 0
    assert await reader.read() == b""
    writer.close()


async def test_close_sends_eof_to_open_clients() -> None:
    srv = ModbusServer(Device(load_device(EXAMPLE)), host=HOST, port=0)
    _, port = await srv.start()
    reader, writer = await raw(port)
    writer.write(adu(1, "0300000001"))
    await read_response(reader)
    await asyncio.wait_for(srv.close(), timeout=5)
    assert await reader.read() == b""
    writer.close()
    with pytest.raises(ConnectionRefusedError):
        await raw(port)


async def test_close_does_not_wait_for_a_client_that_stopped_reading() -> None:
    registers = [{"address": a} for a in range(125)]
    spec = DeviceSpec.model_validate({"format": "rigtape.device/v1", "input_registers": registers})
    srv = ModbusServer(Device(spec), host=HOST, port=0)
    _, port = await srv.start()
    loop = asyncio.get_running_loop()
    with socket.socket() as sock:  # a raw socket that never reads, unlike an asyncio stream
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)
        await loop.sock_connect(sock, (HOST, port))
        send = asyncio.create_task(loop.sock_sendall(sock, adu(1, "040000007d") * 30000))

        def buffered() -> int:  # answer bytes the server could not send yet
            return sum(w.transport.get_write_buffer_size() for w, _ in srv._connections.values())

        for _ in range(100):
            if buffered() >= 65536:  # above the high-water mark: the server waits in drain()
                break
            await asyncio.sleep(0.05)
        assert buffered() >= 65536
        await asyncio.wait_for(srv.close(), timeout=5)
        send.cancel()


async def test_a_connection_whose_task_starts_after_close_ends_at_once() -> None:
    """close() can miss a connection accepted just before it, whose task has not started yet.

    Driven directly: an end-to-end version depends on loop-turn timing, and with the fewest
    turns it hits a race inside asyncio itself (accept before attach).
    """
    srv = ModbusServer(Device(load_device(EXAMPLE)), host=HOST, port=0)
    await srv.start()
    await srv.close()
    ours, theirs = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=ours)
    peer_reader, peer_writer = await asyncio.open_connection(sock=theirs)
    peer_writer.write(adu(1, "0300000001"))
    await asyncio.wait_for(srv._serve(reader, writer), timeout=5)  # must not answer or wait
    try:
        answer = await asyncio.wait_for(peer_reader.read(), timeout=5)
    except ConnectionResetError:  # Linux resets a socket closed with unread data
        answer = b""
    assert answer == b""
    peer_writer.close()
