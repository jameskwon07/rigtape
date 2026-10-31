"""The control API: route table, JSON errors, limits, and the serve/replay flags."""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import struct
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from helpers import HOST, adu, child_env, raw, read_response

from rigtape import control
from rigtape.control import ControlServer
from rigtape.device import Device, load_device
from rigtape.faults.engine import FaultEngine
from rigtape.faults.routes import routes
from rigtape.faults.spec import load_scenario
from rigtape.server import ModbusServer

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "pid"


async def http(port: int, request: bytes) -> tuple[int, dict[str, Any] | None, bytes]:
    """Send raw HTTP/1.1 and return (status, JSON body or None, whole response)."""
    reader, writer = await asyncio.open_connection(HOST, port)
    writer.write(request)
    response = await asyncio.wait_for(reader.read(), timeout=10)  # the server closes
    writer.close()
    status = int(response.split(b" ", 2)[1])
    head, _, body = response.partition(b"\r\n\r\n")
    assert body.endswith(b"\n")  # curl output ends its line
    assert b"connection: close" in head.lower()
    return status, json.loads(body) if body else None, response


async def call(port: int, method: str, path: str) -> tuple[int, dict[str, Any] | None]:
    status, body, _ = await http(port, f"{method} {path} HTTP/1.1\r\nhost: x\r\n\r\n".encode())
    return status, body


@pytest.fixture
async def api() -> AsyncIterator[tuple[FaultEngine, int, int]]:
    """The PID device with the example scenario and its control API, both on free ports."""
    scenario = load_scenario(EXAMPLES / "faults.yaml")
    engine = FaultEngine(scenario, "ci-42")
    device = Device(load_device(EXAMPLES / "device.yaml"))
    server = ModbusServer(device, host=HOST, port=0, interceptor=engine)
    api = ControlServer(routes(engine), host=HOST, port=0)
    _, modbus_port = await server.start()
    _, api_port = await api.start()
    try:
        yield engine, modbus_port, api_port
    finally:
        await api.close()
        await server.close()


async def test_get_faults_reports_seed_and_counters(api: tuple[FaultEngine, int, int]) -> None:
    _, modbus_port, api_port = api
    reader, writer = await raw(modbus_port)
    for tid in range(1, 4):
        writer.write(adu(tid, "0600000258"))
        await read_response(reader)
    writer.close()
    status, body = await call(api_port, "GET", "/faults")
    assert status == 200
    assert body is not None
    assert body["seed"] == "ci-42"
    busy = next(r for r in body["rules"] if r["name"] == "busy-on-third-sp-write")
    assert busy == {
        "name": "busy-on-third-sp-write", "type": "exception", "enabled": True,
        "matched": 3, "fired": 1, "remaining": 0,
    }  # fmt: skip
    assert [r["enabled"] for r in body["rules"]] == [True, True, False, False]


async def test_enable_applies_from_the_next_request_and_disable_restores(
    api: tuple[FaultEngine, int, int],
) -> None:
    _, modbus_port, api_port = api
    reader, writer = await raw(modbus_port)
    writer.write(adu(1, "0300000001"))
    assert (await read_response(reader))[2] == "030200c8"
    assert await call(api_port, "POST", "/faults/hang/enable") == (
        200,
        {"name": "hang", "enabled": True},
    )
    writer.write(adu(2, "0300000001"))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(reader.read(1), timeout=0.3)  # swallowed, connection still open
    assert await call(api_port, "POST", "/faults/hang/disable") == (
        200,
        {"name": "hang", "enabled": False},
    )
    writer.write(adu(3, "0300000001"))
    assert (await read_response(reader))[2] == "030200c8"
    writer.close()


async def test_unknown_rule_is_404_with_the_names(api: tuple[FaultEngine, int, int]) -> None:
    status, body = await call(api[2], "POST", "/faults/nope/enable")
    assert status == 404
    assert body == {
        "error": "no rule named 'nope'",
        "hint": "rules: slow-pv, busy-on-third-sp-write, hang, cable-pull",
    }


async def test_wrong_method_is_405_and_unknown_path_404(api: tuple[FaultEngine, int, int]) -> None:
    status, body = await call(api[2], "GET", "/faults/hang/enable")
    assert (status, body) == (
        405,
        {"error": "GET is not allowed on /faults/hang/enable", "hint": "use POST"},
    )
    status, body = await call(api[2], "DELETE", "/faults")
    assert (status, body) == (405, {"error": "DELETE is not allowed on /faults", "hint": "use GET"})
    status, body = await call(api[2], "GET", "/status")
    assert status == 404
    assert body == {
        "error": "no route for /status",
        "hint": "paths: /faults, /faults/{name}/disable, /faults/{name}/enable",
    }
    assert (await call(api[2], "GET", "/faults?verbose=1"))[0] == 200  # a query string is ignored


async def test_bodies_are_discarded_up_to_64_kib(api: tuple[FaultEngine, int, int]) -> None:
    small = b"x" * 1000
    head = b"POST /faults/hang/enable HTTP/1.1\r\nhost: x\r\ncontent-length: 1000\r\n\r\n"
    assert (await http(api[2], head + small))[0] == 200
    big = b"x" * (64 * 1024 + 1)
    head = b"POST /faults/hang/enable HTTP/1.1\r\nhost: x\r\ncontent-length: 65537\r\n\r\n"
    status, body, _ = await http(api[2], head + big)
    assert (status, body) == (413, {"error": "request body over 65536 bytes"})


async def test_garbage_is_400_and_silence_times_out(
    api: tuple[FaultEngine, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body, _ = await http(api[2], b"\x16\x03\x01 not http\r\n\r\n")
    assert status == 400
    assert body is not None
    assert body["error"].startswith("bad request")
    monkeypatch.setattr(control, "READ_TIMEOUT_S", 0.2)
    status, body, _ = await http(api[2], b"GET /faults HTTP/1.1\r\n")  # never finishes the headers
    assert (status, body) == (408, {"error": "no complete request within 0.2 s"})


async def test_non_loopback_bind_warns(caplog: pytest.LogCaptureFixture) -> None:
    api = ControlServer([], host="0.0.0.0", port=0)
    with caplog.at_level("WARNING"):
        _, port = await api.start()
    await api.close()
    assert f"publish it to loopback only, e.g. docker run -p 127.0.0.1:{port}:{port}" in caplog.text


async def run_serve(*args: str) -> tuple[asyncio.subprocess.Process, dict[str, Any]]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rigtape", "serve", str(EXAMPLES / "device.yaml"), "--listen", "0",
        "--faults", str(EXAMPLES / "faults.yaml"), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
    )  # fmt: skip
    assert proc.stdout is not None
    return proc, json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=10))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_api_port_0_is_reported_and_no_api_disables_it() -> None:
    proc, ready = await run_serve("--api-port", "0")
    try:
        assert ready["api"]["host"] == HOST
        assert (await call(ready["api"]["port"], "GET", "/faults"))[0] == 200
    finally:
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), timeout=5) == 0
    proc, ready = await run_serve("--no-api")
    try:
        assert "api" not in ready
        assert ready["seed"] == "ci-42"
    finally:
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), timeout=5) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_api_port_in_use_is_a_usage_error() -> None:
    blocker = await asyncio.start_server(lambda r, w: None, HOST, 0)
    port = blocker.sockets[0].getsockname()[1]
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rigtape", "serve", str(EXAMPLES / "device.yaml"), "--listen", "0",
        "--faults", str(EXAMPLES / "faults.yaml"), "--api-port", str(port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
    )  # fmt: skip
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    finally:
        blocker.close()
        await blocker.wait_closed()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode == 2
    lines = err.decode().splitlines()
    assert lines[-2].startswith(f"error: cannot listen on {HOST}:{port}:")
    hint = (
        "hint: another process uses this port; pick another with --api-port (0 = any) or --no-api"
    )
    assert lines[-1] == hint


async def test_close_does_not_wait_for_an_idle_connection() -> None:
    api = ControlServer([], host=HOST, port=0)
    _, port = await api.start()
    reader, writer = await asyncio.open_connection(HOST, port)  # connected, sends nothing
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(api.close(), timeout=5)
    assert asyncio.get_running_loop().time() - started < 1.0  # not the 5 s read timeout
    assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    writer.close()


async def test_reset_mid_request_logs_no_traceback(
    api: tuple[FaultEngine, int, int], caplog: pytest.LogCaptureFixture
) -> None:
    sock = socket.socket()
    sock.connect((HOST, api[2]))
    sock.sendall(b"GET /faults HTTP/1.1\r\nhost: x")  # headers never finish
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # RST on close
    sock.close()
    await asyncio.sleep(0.1)
    assert (await call(api[2], "GET", "/faults"))[0] == 200  # still serving
    assert "Unhandled exception" not in caplog.text
