from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from helpers import child_env

from rigtape import cli
from rigtape.cli import main
from rigtape.device import Device, load_device
from rigtape.server import ModbusServer


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"rigtape {version('rigtape')}\n"


@pytest.mark.parametrize("argv", [["--bogus"], ["--vers"]], ids=["unknown", "abbreviation"])
def test_usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == 2
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("error: unrecognized arguments")
    assert err[1] == "hint: run 'rigtape --help' for usage"


def test_python_dash_m() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "rigtape", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.startswith("rigtape ")


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "pid" / "device.yaml"


async def spawn_serve(*args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rigtape", "serve", str(EXAMPLE), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
    )  # fmt: skip


@asynccontextmanager
async def serving(*args: str) -> AsyncIterator[tuple[asyncio.subprocess.Process, dict[str, Any]]]:
    """`rigtape serve --listen 0` in a subprocess, killed if a test fails half way."""
    proc = await spawn_serve("--listen", "0", *args)
    try:
        assert proc.stdout is not None
        yield proc, json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=10))
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def stop(proc: asyncio.subprocess.Process, sig: signal.Signals) -> int:
    proc.send_signal(sig)
    return await asyncio.wait_for(proc.wait(), timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_serve_ready_line_and_sigterm() -> None:
    async with serving() as (proc, ready):
        port = ready["modbus"]["port"]
        assert ready == {
            "event": "ready",
            "mode": "serve",
            "modbus": {"host": "127.0.0.1", "port": port},
        }
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(bytes.fromhex("000100000006010400000001"))
        assert await reader.readexactly(11) == bytes.fromhex("00010000000501040200c8")
        assert await stop(proc, signal.SIGTERM) == 0
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""  # open clients get EOF
        writer.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_serve_sigint_exits_130() -> None:
    async with serving() as (proc, _):
        assert await stop(proc, signal.SIGINT) == 130


async def test_serve_port_in_use_is_usage_error() -> None:
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = blocker.sockets[0].getsockname()[1]
    proc = await spawn_serve("--listen", str(port))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    finally:
        blocker.close()
        await blocker.wait_closed()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode == 2
    assert out == b""
    lines = err.decode().splitlines()
    assert lines[-2] == f"error: cannot listen on 127.0.0.1:{port}: {os.strerror(errno.EADDRINUSE)}"
    assert lines[-1].startswith("hint: another process uses this port")


def test_serve_malformed_host_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", str(EXAMPLE), "--host", "127.0.0..1", "--listen", "0"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert err[-2].startswith("error: cannot listen on 127.0.0..1:0: invalid host name")
    assert err[-1].startswith("hint: use an address of this machine")


def test_serve_invalid_device_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "device.yaml"
    path.write_text("format: rigtape.device/v1\nholding_registers:\n  - {address: 70000}\n")
    assert main(["serve", str(path)]) == 2
    err = capsys.readouterr().err.splitlines()
    assert err == [
        f"error: {path}: holding_registers[0].address: Input should be less than or equal to 65535"
    ]


@pytest.mark.parametrize("port", ["-1", "65536", "x"])
def test_serve_rejects_bad_port(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", str(EXAMPLE), "--listen", port]) == 2
    assert "invalid port" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "env", "host"),
    [
        pytest.param([], None, "127.0.0.1", id="default"),
        pytest.param([], "0.0.0.0", "0.0.0.0", id="env"),
        pytest.param(["--host", "::1"], "0.0.0.0", "::1", id="flag-beats-env"),
    ],
)
def test_serve_host_precedence(
    argv: list[str], env: str | None, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def fake_run(server: object, args: argparse.Namespace, **ready: object) -> int:
        seen.append(args.host)
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)
    if env is None:
        monkeypatch.delenv("RIGTAPE_HOST", raising=False)
    else:
        monkeypatch.setenv("RIGTAPE_HOST", env)
    assert main(["serve", str(EXAMPLE), *argv]) == 0
    assert seen == [host]


async def test_serves_without_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no loop.add_signal_handler; serve must still run (Ctrl+C ends it)."""

    def unsupported(*args: object) -> None:
        raise NotImplementedError

    ready = asyncio.Event()
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", unsupported)
    monkeypatch.setattr(cli, "_ready", lambda **fields: ready.set())
    server = ModbusServer(Device(load_device(EXAMPLE)), host="127.0.0.1", port=0)
    task = asyncio.create_task(cli._run(server, argparse.Namespace(host="127.0.0.1", listen=0)))
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_keyboard_interrupt_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_serve", interrupted)
    assert main(["serve", str(EXAMPLE)]) == 130


@pytest.mark.parametrize(
    ("exc", "reason", "hint"),
    [
        pytest.param(
            OSError(errno.EADDRINUSE, "error while attempting to bind ..."),
            os.strerror(errno.EADDRINUSE),
            "another process uses this port",
            id="in-use",
        ),
        pytest.param(
            OSError(errno.EACCES, "error while attempting to bind ..."),
            os.strerror(errno.EACCES),
            "ports below 1024 need root",
            id="privileged",
        ),
        pytest.param(  # errno 13 is EACCES, but here it is an EAI_* lookup code
            socket.gaierror(13, "nodename nor servname provided, or not known"),
            "nodename nor servname provided, or not known",
            "use an address of this machine",
            id="unknown-host",
        ),
        pytest.param(
            OSError("could not bind on any address out of [('10.255.255.1', 5020)]"),
            "could not bind on any address",
            "use an address of this machine",
            id="not-local",
        ),
    ],
)
def test_listen_errors_explain_the_fix(exc: OSError, reason: str, hint: str) -> None:
    error = cli._listen_error(exc, "h", 1)
    assert str(error).startswith(f"cannot listen on h:1: {reason}")
    assert error.hint is not None
    assert error.hint.startswith(hint)
    assert error.exit_code == 2
