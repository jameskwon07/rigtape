"""Shared fixtures. pymodbus stays here: it plays the "real device" and is never imported by src."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

HOST = "127.0.0.1"
UNIT = 1
HOLDING = (250, 0)  # HR0 = setpoint, 0.1 degC per count
INPUT = (200, 0)  # IR0 = process value


@dataclass(frozen=True)
class RealDevice:
    server: ModbusTcpServer
    port: int
    host: str = HOST
    unit: int = UNIT
    holding_registers: tuple[int, ...] = HOLDING
    input_registers: tuple[int, ...] = INPUT


@pytest.fixture
async def real_device() -> AsyncIterator[RealDevice]:
    """A pymodbus 3.15 TCP server on 127.0.0.1 with an ephemeral port."""
    device = SimDevice(
        UNIT,
        simdata=(  # coils, discrete inputs, holding registers, input registers
            [SimData(0, count=16, values=False, datatype=DataType.BITS)],
            [SimData(0, count=16, values=False, datatype=DataType.BITS)],
            [SimData(0, values=list(HOLDING), datatype=DataType.REGISTERS)],
            [SimData(0, values=list(INPUT), datatype=DataType.REGISTERS)],
        ),
    )
    server = ModbusTcpServer(device, address=(HOST, 0))  # needs a running loop
    await server.serve_forever(background=True)
    try:
        # The only line that reaches into pymodbus internals.
        port = server.transport.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        yield RealDevice(server, port)
    finally:
        await server.shutdown()


@dataclass(frozen=True)
class SilentUpstream:
    port: int
    received: asyncio.Event  # set once a request reached the device


@pytest.fixture
async def silent_upstream() -> AsyncIterator[SilentUpstream]:
    """A device that accepts connections and swallows requests but never answers."""
    received = asyncio.Event()

    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(1024):
                received.set()
        finally:
            writer.close()

    server = await asyncio.start_server(swallow, HOST, 0)
    try:
        yield SilentUpstream(server.sockets[0].getsockname()[1], received)
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def refused_port() -> int:
    """A port nothing listens on: reserved, then released."""
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])
