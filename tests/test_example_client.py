"""examples/pid/client.py, the README demo client, run as a subprocess like a user would."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rigtape.device import Device, load_device
from rigtape.recording.format import read_recording
from rigtape.recording.replay import Replayer
from rigtape.server import ModbusServer

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "pid"
# The README promises stock python3 3.9+; CI points this at a 3.9 interpreter.
PYTHON = os.environ.get("RIGTAPE_CLIENT_PYTHON", sys.executable)  # set but empty -> fail loudly


async def run_client(port: int, *args: str) -> tuple[int, list[str]]:
    proc = await asyncio.create_subprocess_exec(
        PYTHON, str(EXAMPLES / "client.py"), "--port", str(port), "--interval", "0.05",
        *args, stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    assert proc.returncode is not None
    return proc.returncode, out.decode().splitlines()


def serve(port: int = 0) -> ModbusServer:
    return ModbusServer(Device(load_device(EXAMPLES / "device.yaml")), host="127.0.0.1", port=port)


@pytest.fixture
async def port() -> AsyncIterator[int]:
    server = serve()
    _, bound = await server.start()
    try:
        yield bound
    finally:
        await server.close()


async def test_prints_exactly_the_requested_reads(port: int) -> None:
    assert await run_client(port, "--reads", "3") == (0, ["SP 20.0 C  PV 20.0 C"] * 3)


async def test_writes_setpoint_then_reads(port: int) -> None:
    code, lines = await run_client(port, "--sp", "60", "--reads", "2")
    assert code == 0
    assert lines[0] == "wrote SP 60.0 C"
    assert [line[:13] for line in lines[1:]] == ["SP 60.0 C  PV"] * 2


@pytest.mark.parametrize("early", ["refuse", "accept-then-close"])
async def test_retries_until_the_server_starts(early: str) -> None:
    """Before the app listens, a port is refused, or docker-proxy accepts and closes at once."""
    with socket.socket() as probe:  # reserve a free port number, then release it
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    knocked = asyncio.Event()

    def accept_then_close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        knocked.set()
        writer.close()

    if early == "accept-then-close":
        proxy = await asyncio.start_server(accept_then_close, "127.0.0.1", free)
    client = asyncio.create_task(run_client(free, "--reads", "3"))
    if early == "accept-then-close":
        await asyncio.wait_for(knocked.wait(), timeout=10)  # the client got EOF and will retry
        proxy.close()
        await proxy.wait_closed()
    else:
        await asyncio.sleep(0.5)  # nothing to wait on: a refused connect leaves no trace here
    server = serve(free)
    await server.start()
    try:
        assert await client == (0, ["SP 20.0 C  PV 20.0 C"] * 3)
    finally:
        await server.close()


async def test_reports_lost_connection_and_fails() -> None:
    server = serve()
    _, bound = await server.start()
    proc = await asyncio.create_subprocess_exec(
        PYTHON, str(EXAMPLES / "client.py"), "--port", str(bound), "--interval", "0.3",
        "--reads", "3", stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip
    assert proc.stdout is not None
    first = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    await server.close()  # the device goes away between reads
    rest, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    assert proc.returncode == 1
    assert [first.decode().strip(), *rest.decode().splitlines()] == [
        "SP 20.0 C  PV 20.0 C",
        "connection lost",
        "connection lost",
    ]


async def test_shipped_recording_replays_the_documented_run() -> None:
    """README: examples/pid/session.jsonl is a recorded run of client.py --sp 60 --reads 10."""
    replayer = Replayer(read_recording(EXAMPLES / "session.jsonl"))
    server = ModbusServer(replayer, host="127.0.0.1", port=0)
    _, bound = await server.start()
    try:
        code, lines = await run_client(bound, "--sp", "60", "--reads", "10")
    finally:
        await server.close()
    assert (code, lines[0]) == (0, "wrote SP 60.0 C")
    assert lines[1:] == [f"SP 60.0 C  PV {pv}.0 C" for pv in range(20, 30)]
    assert (replayer.misses, replayer.unrecorded_writes) == (0, 0)
