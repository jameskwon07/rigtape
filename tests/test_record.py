"""`rigtape record`: a transparent proxy that logs exchanges to a JSONL file."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from conftest import RealDevice, SilentUpstream
from helpers import HOST, adu, child_env, eventually, exchange, lines, raw, read_response
from pymodbus.client import AsyncModbusTcpClient

from rigtape.cli import _endpoint, main
from rigtape.device import Device, DeviceSpec, load_device
from rigtape.recording import proxy as proxy_module
from rigtape.recording.format import RecordingWriter
from rigtape.recording.proxy import RecordingProxy
from rigtape.server import ModbusServer

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "pid" / "device.yaml"
Recorder = Callable[..., Awaitable[tuple[RecordingProxy, int, Path]]]


@pytest.fixture
async def recorder(tmp_path: Path) -> AsyncIterator[Recorder]:
    """Starts a proxy in front of an upstream port; closes everything afterwards."""
    started: list[tuple[RecordingProxy, RecordingWriter]] = []

    async def start(upstream: int, *, window: float = 30.0) -> tuple[RecordingProxy, int, Path]:
        path = tmp_path / f"rec{len(started)}.jsonl"
        writer = RecordingWriter(path, upstream=f"{HOST}:{upstream}", source="test")
        proxy = RecordingProxy((HOST, upstream), writer, host=HOST, port=0, pending_window_s=window)
        started.append((proxy, writer))
        return proxy, (await proxy.start())[1], path

    yield start
    for proxy, writer in started:
        await proxy.close()
        writer.close()


@pytest.fixture
async def device() -> AsyncIterator[int]:
    """rigtape serve as the upstream: unlike pymodbus's server it answers pipelined frames."""
    server = ModbusServer(Device(load_device(EXAMPLE)), host=HOST, port=0)
    _, port = await server.start()
    try:
        yield port
    finally:
        await server.close()


REQUESTS = ["0300000002", "0400000001", "0600000190", "0300000001", "0600200001", "0100000001"]
# read HR0-1, read IR0, write HR0=400, read HR0, write undefined HR32 (-> 02), FC1 (-> 01)


async def test_client_gets_the_same_bytes_as_from_the_device(
    real_device: RealDevice, recorder: Recorder
) -> None:
    _, port, _ = await recorder(real_device.port)
    through_proxy = await exchange(port, *REQUESTS)
    await exchange(real_device.port, "06000000fa")  # put HR0 back to 250 for the direct run
    assert through_proxy == await exchange(real_device.port, *REQUESTS)
    assert through_proxy[2] == "0600000190"
    assert through_proxy[4] == "8602"


async def test_recording_file_structure(real_device: RealDevice, recorder: Recorder) -> None:
    _, port, path = await recorder(real_device.port)
    client = AsyncModbusTcpClient(HOST, port=port, timeout=2, retries=0)
    assert await client.connect()
    assert (await client.read_input_registers(0, count=1)).registers == [200]
    assert (await client.write_register(0, 300)).isError() is False
    assert (await client.write_register(32, 1)).exception_code == 2
    client.close()
    await eventually(lambda: lines(path)[-1]["ev"] == "close")
    events = lines(path)
    assert events[0]["format"] == "rigtape.recording"
    assert events[0]["version"] == 1
    assert events[0]["source"] == "test"
    assert events[0]["listen"] == f"{HOST}:{port}"  # the bound address, not the requested 0
    assert [e["ev"] for e in events[1:]] == ["open", "x", "x", "x", "close"]
    assert events[1]["peer"].startswith("127.0.0.1:")
    assert [e["rsp"] for e in events[2:5]] == ["040200c8", "060000012c", "8602"]
    assert int(events[4]["rsp"][:2], 16) & 0x80
    assert all(e["lat"] > 0 for e in events[2:5])
    assert events[5] == {"t": events[5]["t"], "ev": "close", "conn": 1, "by": "client"}


async def nothing_arrives(reader: asyncio.StreamReader) -> None:
    """The connection is open and the proxy invented no answer."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(reader.read(1), timeout=0.2)


async def test_silent_upstream_pending_request_is_closed_not_answered(
    silent_upstream: SilentUpstream, recorder: Recorder
) -> None:
    proxy, port, path = await recorder(silent_upstream.port)
    reader, writer = await raw(port)
    writer.write(adu(9, "0400000001"))
    await nothing_arrives(reader)
    assert proxy.exchanges == 0
    writer.close()
    await writer.wait_closed()
    await eventually(lambda: proxy.exchanges == 1)
    x = lines(path)[2]
    assert (x["tid"], x["rsp"], x["err"], x["lat"]) == (9, None, "closed", None)


async def test_silent_upstream_pending_requests_time_out_with_their_arrival_times(
    silent_upstream: SilentUpstream, recorder: Recorder
) -> None:
    proxy, port, path = await recorder(silent_upstream.port, window=0.3)
    reader, writer = await raw(port)
    writer.write(adu(1, "0400000001"))
    await asyncio.sleep(0.1)
    writer.write(adu(2, "0400000001"))
    await eventually(lambda: proxy.unanswered == 2)
    first, second = lines(path)[2:4]
    assert (first["rsp"], first["err"], second["err"]) == (None, "timeout", "timeout")
    assert 0.05 < second["t"] - first["t"] < 0.25  # `t` is when each request arrived
    await nothing_arrives(reader)
    writer.close()


async def test_pipelined_and_repeated_tids_are_paired_fifo(device: int, recorder: Recorder) -> None:
    proxy, port, path = await recorder(device)
    reader, writer = await raw(port)
    writer.write(adu(7, "0400000001") + adu(8, "0300000001"))
    assert (await read_response(reader))[0] == 7
    assert (await read_response(reader))[0] == 8
    writer.write(adu(3, "0400000001") + adu(3, "0300000001"))  # pymodbus retries reuse a tid
    first, second = await read_response(reader), await read_response(reader)
    assert (first[0], second[0]) == (3, 3)
    assert (first[2], second[2]) == ("040200c8", "030200c8")
    writer.close()
    await eventually(lambda: proxy.exchanges == 4)
    recorded = [(e["tid"], e["req"], e["rsp"]) for e in lines(path) if e.get("ev") == "x"]
    assert recorded == [
        (7, "0400000001", "040200c8"),
        (8, "0300000001", "030200c8"),
        (3, "0400000001", "040200c8"),
        (3, "0300000001", "030200c8"),
    ]


async def test_refused_upstream_closes_only_that_connection(
    refused_port: int, recorder: Recorder
) -> None:
    _, port, path = await recorder(refused_port)
    for _ in range(2):  # the proxy keeps accepting after a refusal
        reader, writer = await raw(port)
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        writer.close()
    await eventually(lambda: sum(e.get("ev") == "close" for e in lines(path)) == 2)
    closes = [e for e in lines(path) if e.get("ev") == "close"]
    assert [(c["conn"], c["by"]) for c in closes] == [(1, "upstream"), (2, "upstream")]
    assert all(c["err"].startswith("connect:") for c in closes)


async def test_bad_upstream_host_name_is_a_closed_connection_not_a_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "rec.jsonl"
    writer = RecordingWriter(path, upstream="127.0.0..1:1", source="test")
    proxy = RecordingProxy(("127.0.0..1", 1), writer, host=HOST, port=0)
    _, port = await proxy.start()
    try:
        reader, client = await raw(port)
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        client.close()
        await eventually(lambda: lines(path)[-1].get("ev") == "close")
    finally:
        await proxy.close()
        writer.close()
    assert lines(path)[-1]["by"] == "upstream"
    assert lines(path)[-1]["err"].startswith("connect:")
    assert "Unhandled exception" not in caplog.text


async def test_close_does_not_wait_for_a_client_that_stopped_reading(recorder: Recorder) -> None:
    registers = [{"address": a} for a in range(125)]  # big answers fill the buffers quickly
    spec = DeviceSpec.model_validate({"format": "rigtape.device/v1", "input_registers": registers})
    device = ModbusServer(Device(spec), host=HOST, port=0)
    _, device_port = await device.start()
    proxy, port, _ = await recorder(device_port)
    loop = asyncio.get_running_loop()
    try:
        with socket.socket() as sock:  # a raw socket that never reads, unlike an asyncio stream
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.setblocking(False)
            await loop.sock_connect(sock, (HOST, port))
            send = asyncio.create_task(loop.sock_sendall(sock, adu(1, "040000007d") * 30000))

            def buffered() -> int:  # answer bytes the proxy could not send to the client yet
                return sum(w.transport.get_write_buffer_size() for w in proxy._tasks.values())

            await eventually(lambda: buffered() >= 65536, within=20)
            await asyncio.wait_for(proxy.close(), timeout=5)
            send.cancel()
    finally:
        await device.close()


async def test_a_connection_whose_task_starts_after_close_is_dropped(tmp_path: Path) -> None:
    writer = RecordingWriter(tmp_path / "rec.jsonl", upstream="x:1", source="test")
    proxy = RecordingProxy((HOST, 1), writer, host=HOST, port=0)
    await proxy.start()
    await proxy.close()
    ours, theirs = socket.socketpair()
    reader, client = await asyncio.open_connection(sock=ours)
    peer_reader, peer_writer = await asyncio.open_connection(sock=theirs)
    await asyncio.wait_for(proxy._serve(reader, client), timeout=5)
    try:
        answer = await asyncio.wait_for(peer_reader.read(), timeout=5)
    except ConnectionResetError:  # Linux resets a socket closed with unread data
        answer = b""
    assert answer == b""
    assert lines(tmp_path / "rec.jsonl")[1:] == []  # no open event for a dropped connection
    peer_writer.close()
    writer.close()


async def test_stopping_during_the_upstream_connect_still_closes_the_connection(
    refused_port: int, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open_connection, hanging = asyncio.open_connection, asyncio.Event()

    async def slow_upstream(host: str, port: int) -> object:  # only the device never answers SYN
        if port == refused_port:
            await hanging.wait()
        return await real_open_connection(host, port)

    monkeypatch.setattr(proxy_module.asyncio, "open_connection", slow_upstream)
    proxy, port, path = await recorder(refused_port)
    _, client = await raw(port)
    await eventually(lambda: lines(path)[-1].get("ev") == "open")
    await asyncio.wait_for(proxy.close(), timeout=5)
    last = lines(path)[-1]
    assert (last["ev"], last["by"], last["err"]) == ("close", "proxy", "connect: cancelled")
    client.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_sigterm_writes_stop_last_and_exits_0(
    silent_upstream: SilentUpstream, tmp_path: Path
) -> None:
    out = tmp_path / "rec.jsonl"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rigtape", "record", "--upstream", f"{HOST}:{silent_upstream.port}",
        "--listen", "0", "-o", str(out), "--source", "signal test",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        ready = json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=10))
        assert (ready["mode"], ready["file"]) == ("record", str(out))
        assert ready["upstream"] == f"{HOST}:{silent_upstream.port}"
        reader, writer = await raw(ready["modbus"]["port"])
        writer.write(adu(1, "0400000001"))
        await asyncio.wait_for(silent_upstream.received.wait(), timeout=5)  # forwarded
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), timeout=5) == 0
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        writer.close()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    events = lines(out)
    assert events[0]["source"] == "signal test"
    assert events[0]["listen"] == f"{HOST}:{ready['modbus']['port']}"
    assert [e["ev"] for e in events[1:]] == ["open", "x", "close", "stop"]
    assert (events[2]["rsp"], events[2]["err"]) == (None, "closed")
    assert events[3]["by"] == "proxy"
    assert events[4]["reason"] == "signal"
    assert proc.stderr is not None
    assert b"recorded 1 exchanges (1 unanswered)" in await proc.stderr.read()


def test_existing_output_is_never_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "rec.jsonl"
    out.write_text("precious\n")
    assert main(["record", "--upstream", "127.0.0.1:5020", "-o", str(out), "--listen", "0"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert err[-2] == f"error: {out} already exists"
    assert err[-1].startswith("hint: recordings are never overwritten")
    assert out.read_text() == "precious\n"


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root posix user")
def test_unwritable_output_hints_at_docker_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    locked = tmp_path / "data"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        argv = ["record", "--upstream", "127.0.0.1:5020", "-o", str(locked / "x.jsonl")]
        assert main(argv) == 2
    finally:
        locked.chmod(0o700)
    err = capsys.readouterr().err.splitlines()
    assert err[-2].startswith(f"error: cannot write {locked / 'x.jsonl'}: Permission denied")
    assert err[-1].startswith('hint: in Docker, add --user "$(id -u):$(id -g)"')


def test_listen_failure_leaves_no_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "rec.jsonl"
    argv = ["record", "--upstream", "127.0.0.1:5020", "-o", str(out), "--host", "127.0.0..1"]
    assert main(argv) == 2
    assert "cannot listen" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    "text", ["5020", "host", "host:", ":5020", "host:0", "host:70000", "::1", "fe80::1", "::1:5020"]
)
def test_bad_upstream_is_usage_error(text: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["record", "--upstream", text, "-o", "x.jsonl"]) == 2
    assert "invalid address" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "parsed"),
    [("[::1]:5020", ("::1", 5020)), ("[fe80::1%en0]:502", ("fe80::1%en0", 502)), ("h:1", ("h", 1))],
)
def test_upstream_endpoints_parse(text: str, parsed: tuple[str, int]) -> None:
    assert _endpoint(text) == parsed
