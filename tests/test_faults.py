"""Fault injection: the seeded engine, faults on the server, and the serve/replay commands."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from helpers import HOST, adu, child_env, raw, read_response
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusIOException

from rigtape.cli import main
from rigtape.device import Device, load_device
from rigtape.errors import ConfigError
from rigtape.faults.engine import FaultEngine, fires
from rigtape.faults.spec import Scenario, load_scenario
from rigtape.modbus import Request
from rigtape.server import ModbusServer

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "pid"
PV, SP_600 = "0400000001", "0600000258"
# fires("ci-42", "r", n, 0.3) for n in 1..20, fixed here so a change in the draw is noticed
GOLDEN = [1, 2, 3, 7, 8, 12, 16, 18, 20]


def scenario(*rules: dict[str, object], seed: str | int | None = "ci-42") -> Scenario:
    return Scenario.model_validate(
        {"format": "rigtape.faults/v1", "seed": seed, "rules": list(rules)}
    )


def req(pdu: str, *, unit: int = 1) -> Request:
    return Request(conn_id=1, tid=1, unit=unit, pdu=bytes.fromhex(pdu))


def test_draw_is_a_fixed_function_of_seed_rule_and_index() -> None:
    assert [n for n in range(1, 21) if fires("ci-42", "r", n, 0.3)] == GOLDEN
    assert all(fires("ci-42", "r", n, 1.0) for n in range(1, 21))
    assert not any(fires("ci-42", "r", n, 0.0) for n in range(1, 21))
    assert [n for n in range(1, 21) if fires("ci-43", "r", n, 0.3)] != GOLDEN


def test_rules_before_a_rule_do_not_shift_its_pattern() -> None:
    probabilistic = {"name": "r", "type": "no_response", "probability": 0.3}
    alone = FaultEngine(scenario(probabilistic), "ci-42")
    shadowed = FaultEngine(
        scenario({"name": "first", "type": "exception", "code": 6}, probabilistic), "ci-42"
    )
    fired_alone = [n for n in range(1, 21) if alone.decide(req(PV)).action == "no_response"]
    assert fired_alone == GOLDEN
    for _ in range(20):
        assert shadowed.decide(req(PV)).action == "exception"  # the first rule always wins...
    assert shadowed.status()["rules"][1]["fired"] == len(GOLDEN)  # ...but the second still draws


def test_after_and_count_fire_exactly_once_on_the_third_match() -> None:
    engine = FaultEngine(
        scenario({"name": "busy", "type": "exception", "after": 2, "count": 1, "code": 6}), 1
    )
    actions = [engine.decide(req(SP_600)).action for _ in range(5)]
    assert actions == ["respond", "respond", "exception", "respond", "respond"]
    assert engine.decide(req(SP_600)).code == 0
    assert engine.status()["rules"][0] == {
        "name": "busy", "type": "exception", "enabled": True,
        "matched": 6, "fired": 1, "remaining": 0,
    }  # fmt: skip


def test_after_skips_matches_but_keeps_counting_them() -> None:
    engine = FaultEngine(
        scenario({"name": "r", "type": "no_response", "probability": 0.3, "after": 2}), "ci-42"
    )
    fired = [n for n in range(1, 21) if engine.decide(req(PV)).action == "no_response"]
    assert fired == [
        n for n in GOLDEN if n > 2
    ]  # the draw index is the match number, not n - after


def test_a_disabled_rule_does_not_count_matches() -> None:
    engine = FaultEngine(
        scenario({"name": "r", "type": "no_response", "probability": 0.3}), "ci-42"
    )
    engine.set_enabled("r", False)
    for _ in range(5):
        assert engine.decide(req(PV)).action == "respond"
    engine.set_enabled("r", True)
    fired = [n for n in range(1, 21) if engine.decide(req(PV)).action == "no_response"]
    assert fired == GOLDEN  # the five requests while disabled did not advance n
    assert engine.status()["rules"][0]["matched"] == 20


def test_a_zero_delay_that_fires_still_wins() -> None:
    engine = FaultEngine(
        scenario(
            {"name": "zero", "type": "delay", "delay_ms": 0},
            {"name": "busy", "type": "exception", "code": 6},
        ),
        1,
    )
    decision = engine.decide(req(PV))
    assert (decision.rule, decision.action, decision.delay_s) == ("zero", "respond", 0.0)


def test_a_shadowed_firing_still_spends_the_count() -> None:
    engine = FaultEngine(
        scenario(
            {"name": "first", "type": "exception", "code": 6, "count": 1},
            {"name": "second", "type": "disconnect", "count": 1},
        ),
        1,
    )
    assert (
        engine.decide(req(PV)).rule == "first"
    )  # both fired; the first won and both counts are spent
    assert engine.decide(req(PV)).action == "respond"


@pytest.mark.parametrize(
    ("match", "pdu", "unit", "matches"),
    [
        ({"function_codes": [4]}, PV, 1, True),
        ({"function_codes": [3]}, PV, 1, False),
        ({"unit_ids": [2, 3]}, PV, 2, True),
        ({"unit_ids": [2, 3]}, PV, 1, False),
        ({"address": [0, 0]}, "0400000001", 1, True),
        ({"address": [1, 5]}, "0400000001", 1, False),
        ({"address": [3, 3]}, "0400000005", 1, True),  # reads 0-4, overlaps
        ({"address": [5, 9]}, "0400000005", 1, False),  # reads 0-4, no overlap
        ({"address": [0, 0]}, "0600000258", 1, True),  # FC6 writes one register
        ({"address": [0, 0]}, "0100000001", 1, True),  # coils carry an address too
        ({"address": [0, 0]}, "2b0e0100", 1, False),  # FC43 has no address
        ({}, "2b0e0100", 1, True),
    ],
)
def test_match_fields(match: dict[str, object], pdu: str, unit: int, matches: bool) -> None:
    engine = FaultEngine(scenario({"name": "r", "type": "disconnect", "match": match}), 1)
    assert (engine.decide(req(pdu, unit=unit)).action == "disconnect") is matches


def test_delay_decision_carries_seconds_and_toggling_works() -> None:
    engine = FaultEngine(scenario({"name": "slow", "type": "delay", "delay_ms": 1500}), "s")
    assert engine.decide(req(PV)).delay_s == 1.5
    assert engine.set_enabled("slow", False) is True
    assert engine.decide(req(PV)).delay_s == 0.0
    assert engine.set_enabled("nope", True) is False
    assert engine.names == ["slow"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("seed: 1\nrules: []\n", "format: Field required", id="no-format"),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n  - {name: a, type: delay}\n",
            "rules[0].delay_ms: required for type delay",
            id="delay-needs-ms",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n  - {name: a, type: exception}\n",
            "rules[0].code: required for type exception",
            id="exception-needs-code",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n  - {name: a, type: disconnect, code: 6}\n",
            "rules[0].code: only used by type exception",
            id="code-on-disconnect",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n"
            "  - {name: a, type: disconnect}\n  - {name: a, type: disconnect}\n",
            "rules[1].name: name 'a' is already used by rules[0]",
            id="duplicate-name",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n  - {name: a/b, type: disconnect}\n",
            "rules[0].name: String should match pattern",
            id="name-not-a-path-segment",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n"
            "  - {name: a, type: disconnect, match: {address: [5, 1]}}\n",
            "rules[0].match.address: from 5 is above to",
            id="address-backwards",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n"
            "  - {name: a, type: disconnect, probability: 1.5}\n",
            "rules[0].probability: Input should be less than or equal to 1",
            id="probability",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nseed: true\n",
            "seed.int: Input should be a valid integer",
            id="bool-seed",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n  - {name: a, type: jitter}\n",
            "rules[0].type: Input should be 'delay', 'no_response', 'exception' or 'disconnect'",
            id="unknown-type",
        ),
        pytest.param(
            "format: rigtape.faults/v1\nrules:\n"
            "  - {name: a, type: disconnect, match: {function_codes: []}}\n",
            "rules[0].match.function_codes: Tuple should have at least 1 item",
            id="empty-match-list",
        ),
    ],
)
def test_invalid_scenarios_are_one_line_with_field_path(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "faults.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_scenario(path)
    assert "\n" not in str(info.value)
    assert str(info.value).startswith(f"{path}: ")
    assert message in str(info.value)


def test_example_scenario_loads() -> None:
    assert [r.name for r in load_scenario(EXAMPLES / "faults.yaml").rules] == [
        "slow-pv", "busy-on-third-sp-write", "hang", "cable-pull"
    ]  # fmt: skip


@pytest.fixture
async def faulty() -> AsyncIterator[tuple[FaultEngine, int, ModbusServer]]:
    """The PID device behind every basic fault type, one rule each, toggled from the tests."""
    rules = [
        {
            "name": "slow",
            "type": "delay",
            "delay_ms": 200,
            "enabled": False,
            "match": {"function_codes": [6]},
        },
        {"name": "stuck", "type": "delay", "delay_ms": 3000, "enabled": False},
        {"name": "hang", "type": "no_response", "enabled": False},
        {"name": "busy", "type": "exception", "code": 6, "enabled": False},
        {"name": "weird", "type": "exception", "code": 0x99, "enabled": False},
        {"name": "pull", "type": "disconnect", "enabled": False},
    ]
    engine = FaultEngine(scenario(*rules), 1)
    server = ModbusServer(
        Device(load_device(EXAMPLES / "device.yaml")), host=HOST, port=0, interceptor=engine
    )
    _, port = await server.start()
    try:
        yield engine, port, server
    finally:
        await server.close()


@pytest.fixture
async def client(
    faulty: tuple[FaultEngine, int, ModbusServer],
) -> AsyncIterator[AsyncModbusTcpClient]:
    c = AsyncModbusTcpClient(HOST, port=faulty[1], timeout=0.3, retries=0, reconnect_delay=0)
    assert await c.connect()
    try:
        yield c
    finally:
        c.close()
        await asyncio.sleep(0)


async def test_delay_executes_now_and_answers_late(
    faulty: tuple[FaultEngine, int, ModbusServer], client: AsyncModbusTcpClient
) -> None:
    engine, port, _ = faulty
    client.comm_params.timeout_connect = 2  # pymodbus's request timeout
    engine.set_enabled("slow", True)
    started = time.monotonic()
    write = asyncio.create_task(client.write_register(0, 450))
    reader, writer = await raw(port)  # a second connection sees the write before its reply
    await asyncio.sleep(0.05)
    writer.write(adu(1, "0300000001"))
    assert (await read_response(reader))[2] == "030201c2"
    assert not write.done()
    assert (await write).isError() is False
    assert time.monotonic() - started >= 0.19
    writer.close()


async def test_requests_behind_a_delayed_one_wait_their_turn(
    faulty: tuple[FaultEngine, int, ModbusServer],
) -> None:
    engine, port, _ = faulty
    engine.set_enabled("slow", True)
    reader, writer = await raw(port)
    started = time.monotonic()
    writer.write(adu(1, "0600000190") + adu(2, PV) + adu(3, PV))  # one delayed write, two reads
    answers = [await read_response(reader) for _ in range(3)]
    assert [a[0] for a in answers] == [1, 2, 3]
    assert time.monotonic() - started >= 0.19  # the reads waited behind the write
    writer.close()


async def test_close_does_not_wait_for_a_sleeping_delay(
    faulty: tuple[FaultEngine, int, ModbusServer],
) -> None:
    engine, port, _ = faulty
    engine.set_enabled("stuck", True)
    reader, writer = await raw(port)
    writer.write(adu(1, PV))  # held for 3 s by the fault
    await asyncio.sleep(0.1)
    started = time.monotonic()
    await asyncio.wait_for(faulty[2].close(), timeout=5)
    assert time.monotonic() - started < 1.0
    assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    writer.close()


async def test_no_response_times_out_and_the_connection_survives(
    faulty: tuple[FaultEngine, int, ModbusServer], client: AsyncModbusTcpClient
) -> None:
    engine, _, _ = faulty
    engine.set_enabled("hang", True)
    with pytest.raises(ModbusIOException):
        await client.write_register(0, 450)
    engine.set_enabled("hang", False)
    assert (await client.read_holding_registers(0, count=1)).registers == [200]  # not written


async def test_exception_is_returned_without_executing_the_write(
    faulty: tuple[FaultEngine, int, ModbusServer], client: AsyncModbusTcpClient
) -> None:
    engine, port, _ = faulty
    engine.set_enabled("busy", True)
    response = await client.write_register(0, 450)
    assert response.isError()
    assert response.exception_code == 6
    engine.set_enabled("busy", False)
    assert (await client.read_holding_registers(0, count=1)).registers == [200]
    engine.set_enabled("weird", True)  # a code pymodbus does not know: raw client
    reader, writer = await raw(port)
    writer.write(adu(9, PV))
    assert await read_response(reader) == (9, 1, "8499")
    writer.close()


async def test_disconnect_closes_the_connection_with_eof(
    faulty: tuple[FaultEngine, int, ModbusServer],
) -> None:
    engine, port, _ = faulty
    engine.set_enabled("pull", True)
    reader, writer = await raw(port)
    writer.write(adu(1, PV))
    assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    writer.close()


async def run_command(*args: str) -> tuple[asyncio.subprocess.Process, dict[str, object]]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rigtape", *args, "--listen", "0", "--api-port", "0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
    )  # fmt: skip
    assert proc.stdout is not None
    return proc, json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=10))


async def stop(proc: asyncio.subprocess.Process) -> int:
    if proc.returncode is None:
        proc.send_signal(signal.SIGTERM)
    return await asyncio.wait_for(proc.wait(), timeout=5)


async def third_write_is_busy(port: int) -> list[str]:
    reader, writer = await raw(port)
    answers = []
    for tid in range(1, 5):
        writer.write(adu(tid, SP_600))
        answers.append((await read_response(reader))[2])
    writer.close()
    return answers


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("command", ["serve", "replay"])
async def test_example_scenario_applies_to_serve_and_replay(command: str) -> None:
    target = EXAMPLES / ("device.yaml" if command == "serve" else "session.jsonl")
    faults = ["--faults", str(EXAMPLES / "faults.yaml")]
    proc, ready = await run_command(command, str(target), *faults, "--seed", "7")
    try:
        assert ready["seed"] == "7"  # --seed beats the file's ci-42; the busy rule is certain
        assert isinstance(ready["api"], dict)
        answers = await third_write_is_busy(int(ready["modbus"]["port"]))  # type: ignore[index]
        assert answers == [SP_600, SP_600, "8606", SP_600]
    finally:
        assert await stop(proc) == 0


def test_invalid_scenario_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "faults.yaml"
    bad.write_text("format: rigtape.faults/v1\nrules:\n  - {name: a, type: delay}\n")
    argv = ["serve", str(EXAMPLES / "device.yaml"), "--faults", str(bad), "--listen", "0"]
    assert main(argv) == 2
    assert "rules[0].delay_ms: required for type delay" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_generated_seed_is_printed_and_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "faults.yaml"
    path.write_text(
        "format: rigtape.faults/v1\nrules:\n"
        "  - {name: flaky, type: exception, code: 4, probability: 0.5,"
        " match: {function_codes: [4]}}\n"
    )

    async def pattern(*extra: str) -> tuple[str, list[str]]:
        proc, ready = await run_command(
            "serve", str(EXAMPLES / "device.yaml"), "--faults", str(path), *extra
        )
        try:
            reader, writer = await raw(int(ready["modbus"]["port"]))  # type: ignore[index]
            answers = []
            for tid in range(1, 25):  # 24 draws at p=0.5: all-equal odds about 2^-23
                writer.write(adu(tid, PV))
                answers.append((await read_response(reader))[2])
            writer.close()
        finally:
            await stop(proc)
        return str(ready["seed"]), answers

    seed, first = await pattern()
    assert len(seed) == 8  # generated
    expected = ["8404" if fires(seed, "flaky", n, 0.5) else "040200c8" for n in range(1, 25)]
    assert first == expected  # exactly what the printed seed predicts
    assert (await pattern("--seed", seed))[1] == first
    assert (await pattern("--seed", seed + "x"))[1] != first
