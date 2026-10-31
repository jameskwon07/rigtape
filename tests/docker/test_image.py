"""Smoke tests for a built image.

Run: RIGTAPE_DOCKER_IMAGE=rigtape:dev uv run pytest tests/docker
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

IMAGE = os.environ.get("RIGTAPE_DOCKER_IMAGE", "")
CLIENT = Path(__file__).resolve().parents[2] / "examples" / "pid" / "client.py"

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not IMAGE, reason="set RIGTAPE_DOCKER_IMAGE to an image tag"),
]


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30, check=False
    )


def docker_run(*args: str) -> subprocess.CompletedProcess[str]:
    return docker("run", "--rm", *args)


def wait_ready(container: str) -> dict[str, object]:
    """The READY line, not the published port: docker-proxy accepts before the app listens."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for line in docker("logs", container).stdout.splitlines():
            if line.startswith('{"event": "ready"'):
                ready: dict[str, object] = json.loads(line)
                return ready
        time.sleep(0.2)
    pytest.fail(f"no READY line; logs: {docker('logs', container).stderr}")


def published_port(container: str, inside: str) -> str:
    return docker("port", container, inside).stdout.split()[0].rpartition(":")[2]


def run_client(port: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLIENT), "--port", port, *args],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip


def stopped_cleanly(container: str) -> bool:
    """`docker stop` sends SIGTERM to PID 1; a clean shutdown exits 0."""
    assert docker("stop", container).returncode == 0
    return docker("inspect", "-f", "{{.State.ExitCode}}", container).stdout.strip() == "0"


def test_version() -> None:
    result = docker_run(IMAGE, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rigtape ")


def test_runs_as_non_root_uid_999() -> None:
    result = docker_run("--entrypoint", "id", IMAGE, "-u")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "999"


def test_serves_the_pid_example_by_default() -> None:
    container = docker("run", "-d", "-p", "127.0.0.1::5020", IMAGE).stdout.strip()
    try:
        assert wait_ready(container)["modbus"] == {"host": "0.0.0.0", "port": 5020}
        client = run_client(published_port(container, "5020/tcp"), "--reads", "1")
        assert (client.returncode, client.stdout) == (0, "SP 20.0 C  PV 20.0 C\n")
        assert stopped_cleanly(container)
    finally:
        docker("rm", "-f", container)


def test_record_then_replay_in_containers(tmp_path: Path) -> None:
    """The README walkthrough: serve <- record <- client, then replay the file to the client."""
    tag = f"rigtape-smoke-{os.getpid()}"
    pid, rec, rep, network = f"{tag}-pid", f"{tag}-rec", f"{tag}-rep", f"{tag}-net"
    user = f"{os.getuid()}:{os.getgid()}"  # so the recording lands in our directory, not root's
    data = tmp_path / "data"
    data.mkdir()
    client_args = ("--sp", "45", "--reads", "3", "--interval", "0.2")
    try:
        assert docker("network", "create", network).returncode == 0
        docker("run", "-d", "--name", pid, "--network", network, IMAGE)
        wait_ready(pid)
        docker(
            "run", "-d", "--name", rec, "--network", network, "-p", "127.0.0.1::5021",
            "--user", user, "-v", f"{data}:/data", IMAGE,
            "record", "--upstream", f"{pid}:5020", "--listen", "5021",
            "-o", "/data/pid.jsonl", "--source", "docker smoke test",
        )  # fmt: skip
        assert wait_ready(rec)["mode"] == "record"
        recorded = run_client(published_port(rec, "5021/tcp"), *client_args)
        assert recorded.returncode == 0, recorded.stdout
        assert recorded.stdout.startswith("wrote SP 45.0 C\nSP 45.0 C  PV ")
        assert stopped_cleanly(rec)
        assert stopped_cleanly(pid)
        events = [json.loads(line) for line in (data / "pid.jsonl").read_text().splitlines()]
        assert sum(e.get("ev") == "x" for e in events) == 7  # one write, three SP+PV reads
        assert events[-1]["ev"] == "stop"

        docker(
            "run", "-d", "--name", rep, "-p", "127.0.0.1::5020", "--user", user,
            "-v", f"{data}:/data", IMAGE, "replay", "/data/pid.jsonl",
        )  # fmt: skip
        assert wait_ready(rep)["mode"] == "replay"
        replayed = run_client(published_port(rep, "5020/tcp"), *client_args)
        assert replayed.stdout == recorded.stdout  # byte for byte, with no device attached
        assert stopped_cleanly(rep)
    finally:
        for container in (pid, rec, rep):
            docker("rm", "-f", container)
        docker("network", "rm", network)


def test_faults_toggle_in_a_container() -> None:
    """The README fault step: the example scenario, and the control API on a loopback port."""
    container = docker(
        "run", "-d", "-p", "127.0.0.1::5020", "-p", "127.0.0.1::18502", IMAGE,
        "serve", "examples/pid/device.yaml", "--faults", "examples/pid/faults.yaml",
    ).stdout.strip()  # fmt: skip
    try:
        ready = wait_ready(container)
        assert ready["seed"] == "ci-42"
        assert ready["api"] == {"host": "0.0.0.0", "port": 18502}
        modbus, api = published_port(container, "5020/tcp"), published_port(container, "18502/tcp")
        writes = [run_client(modbus, "--sp", str(c), "--reads", "0").stdout for c in (60, 61, 62)]
        assert writes == [
            "wrote SP 60.0 C\n",
            "wrote SP 61.0 C\n",
            "write SP: exception 6 (SERVER DEVICE BUSY)\n",
        ]

        def post(path: str) -> str:
            with urllib.request.urlopen(f"http://127.0.0.1:{api}{path}", data=b"", timeout=10) as r:
                return str(r.read().decode())

        assert '"enabled": true' in post("/faults/hang/enable")
        assert run_client(modbus, "--reads", "1").stdout == "timeout\n"
        assert '"enabled": false' in post("/faults/hang/disable")
        assert run_client(modbus, "--reads", "1").stdout.startswith("SP 61.0 C  PV ")
        assert stopped_cleanly(container)
    finally:
        docker("rm", "-f", container)
