"""`rigtape replay`: segment and queue rules, driven with hand-written recordings."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import pytest
from helpers import HOST, adu, child_env, exchange, raw, read_response

from rigtape.cli import main
from rigtape.device import Device, load_device
from rigtape.errors import ConfigError
from rigtape.modbus import Request
from rigtape.recording.format import RecordingWriter, read_recording
from rigtape.recording.proxy import RecordingProxy
from rigtape.recording.replay import Replayer
from rigtape.server import ModbusServer

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "pid" / "device.yaml"
HEADER = (
    '{"format":"rigtape.recording","version":1,"protocol":"modbus-tcp",'
    '"started_at":"2026-10-13T00:00:00.000Z","tool":"test","upstream":"u","listen":"l",'
    '"source":"hand-written test data"}'
)
PV, SP = "0400000001", "0300000001"
SP_600 = "0600000258"
FC16_600 = "1000000001020258"


def x(t: float, req: str, rsp: str | None, *, uid: int = 1, conn: int = 1) -> str:
    line = {"t": t, "ev": "x", "conn": conn, "tid": 1, "uid": uid, "req": req, "rsp": rsp}
    return json.dumps(line | ({"err": "closed", "lat": None} if rsp is None else {"lat": 0.001}))


def write_recording(tmp_path: Path, *events: str, header: str = HEADER) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join([header, *events]) + "\n", encoding="utf-8")
    return path


def replayer(tmp_path: Path, *pairs: tuple[str, str | None]) -> Replayer:
    events = [x(i, req, rsp) for i, (req, rsp) in enumerate(pairs)]
    return Replayer(read_recording(write_recording(tmp_path, *events)))


def call(rep: Replayer, pdu: str, *, unit: int = 1) -> str:
    return rep.handle(Request(conn_id=1, tid=1, unit=unit, pdu=bytes.fromhex(pdu))).hex()


def test_same_read_replays_in_order_then_settles_on_the_last_normal(tmp_path: Path) -> None:
    rep = replayer(tmp_path, (PV, "040200c8"), (PV, "040200c9"), (PV, "8406"))
    assert [call(rep, PV) for _ in range(5)] == [
        "040200c8",
        "040200c9",
        "8406",
        "040200c9",
        "040200c9",
    ]


def test_extra_polls_before_a_write_never_see_post_write_values(tmp_path: Path) -> None:
    rep = replayer(tmp_path, (PV, "040200c8"), (SP_600, SP_600), (PV, "040200d2"))
    assert [call(rep, PV) for _ in range(3)] == ["040200c8"] * 3
    assert call(rep, SP_600) == SP_600
    assert [call(rep, PV) for _ in range(2)] == ["040200d2"] * 2


def test_repeated_and_failed_writes_keep_the_segment_queue(tmp_path: Path) -> None:
    """A PLC rewriting the setpoint, and a failed block write, stay in the segment's FIFOs."""
    rep = replayer(
        tmp_path,
        (PV, "040200c8"), (SP_600, SP_600), (PV, "040200d2"), (SP_600, SP_600),
        (FC16_600, "9002"), (PV, "040200dc"),
    )  # fmt: skip
    assert [call(rep, p) for p in (PV, SP_600, SP_600, FC16_600)] == [
        "040200c8",
        SP_600,
        SP_600,
        "9002",
    ]
    assert [call(rep, PV) for _ in range(3)] == [
        "040200d2",
        "040200dc",
        "040200dc",
    ]  # still segment 1
    assert call(rep, "0600000190") == "8604"  # a write that was never recorded
    assert (rep.requests, rep.misses, rep.unrecorded_writes) == (8, 0, 1)


def test_a_recorded_failure_of_the_next_write_is_replayed_before_advancing(tmp_path: Path) -> None:
    rep = replayer(tmp_path, (PV, "040200c8"), (SP_600, "8606"), (SP_600, SP_600), (PV, "040200d2"))
    assert call(rep, PV) == "040200c8"
    assert call(rep, SP_600) == "8606"  # the device was busy the first time
    assert call(rep, PV) == "040200c8"  # and the setpoint had not changed yet
    assert call(rep, SP_600) == SP_600
    assert call(rep, PV) == "040200d2"


@pytest.mark.parametrize(
    ("pdu", "answer"),
    [
        ("0600000064", "8604"),
        ("050001ff00", "8504"),
        ("0f000000010101", "8f04"),
        ("060000", "8604"),
    ],
    ids=["fc6", "fc5", "fc15", "short"],
)
def test_unrecorded_writes_get_04_and_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, pdu: str, answer: str
) -> None:
    rep = replayer(tmp_path, (SP, "03020258"), (SP_600, SP_600))
    with caplog.at_level(logging.WARNING):
        assert call(rep, pdu) == answer
    assert f"unrecorded write {pdu}; expected next recorded write 0600000258" in caplog.text
    assert call(rep, SP) == "03020258"  # nothing changed
    assert rep.unrecorded_writes == 1


def test_unknown_request_warns_with_the_nearest_recorded_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rep = replayer(
        tmp_path, (SP, "03020258"), ("03000a0001", "030203e9"), ("0400090001", "040200c8")
    )
    with caplog.at_level(logging.WARNING):
        assert call(rep, "0300090001") == "8304"
    assert (
        "no recorded response for 0300090001; nearest recorded request is 03000a0001" in caplog.text
    )
    assert rep.misses == 1  # the FC4 read of the same address is not "nearest": another table


def test_unanswered_exchanges_are_skipped_with_a_notice(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        rep = replayer(tmp_path, (PV, None), (PV, "040200c8"))
    assert "1 unanswered requests not replayed; use a fault scenario" in caplog.text
    assert call(rep, PV) == "040200c8"


async def test_cursor_survives_reconnects_and_answers_with_the_current_tid_and_unit(
    tmp_path: Path,
) -> None:
    rep = replayer(tmp_path, (PV, "040200c8"), (PV, "040200c9"))
    server = ModbusServer(rep, host=HOST, port=0)
    _, port = await server.start()
    try:
        reader, writer = await raw(port)
        writer.write(adu(5, PV, unit=1))
        assert await read_response(reader) == (5, 1, "040200c8")
        writer.close()
        reader, writer = await raw(port)
        writer.write(adu(77, PV, unit=9))  # one recorded unit: any unit id is answered
        assert await read_response(reader) == (77, 9, "040200c9")
        writer.close()
    finally:
        await server.close()


def test_key_missing_from_the_current_segment_takes_the_earlier_settled_value(
    tmp_path: Path,
) -> None:
    rep = replayer(tmp_path, (PV, "040200c8"), (PV, "040200c9"), (PV, "8406"), (SP_600, SP_600))
    assert call(rep, SP_600) == SP_600
    assert call(rep, PV) == "040200c9"  # last normal response of segment 0, not its first


def test_reads_borrowed_from_a_later_segment_warn_once_and_skip_transient_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rep = replayer(
        tmp_path,
        (PV, "040200c8"),
        (SP_600, SP_600),
        ("03000a0001", "8306"),
        ("03000a0001", "030203e9"),
    )
    with caplog.at_level(logging.WARNING):
        assert call(rep, "03000a0001") == "030203e9"  # the first normal answer, not the 8306
        assert call(rep, "03000a0001") == "030203e9"
    assert caplog.text.count("answered from a later segment") == 1


def test_two_recorded_units_keep_their_own_answers(tmp_path: Path) -> None:
    events = [x(0, PV, "040200c8", uid=1), x(1, PV, "040200c9", uid=2)]
    rep = Replayer(read_recording(write_recording(tmp_path, *events)))
    assert call(rep, PV, unit=1) == "040200c8"
    assert call(rep, PV, unit=2) == "040200c9"
    assert call(rep, PV, unit=3) == "840b"


SEQUENCE = [
    (PV, 0.5),
    (SP_600, 0.5),
    (PV, 1.0),
    (PV, 1.0),
    ("06000001f4", 1.0),
    (PV, 0.0),
    (SP, 0.0),
]


async def test_round_trip_record_then_replay_gives_the_same_bytes(tmp_path: Path) -> None:
    clock = [1000.0]
    device = ModbusServer(Device(load_device(EXAMPLE), lambda: clock[0]), host=HOST, port=0)
    _, device_port = await device.start()
    path = tmp_path / "trip.jsonl"
    writer = RecordingWriter(path, upstream="device", source="round trip test")
    proxy = RecordingProxy((HOST, device_port), writer, host=HOST, port=0)
    _, proxy_port = await proxy.start()

    async def drive(port: int) -> list[str]:
        reader, writer = await raw(port)
        answers = []
        for tid, (pdu, step) in enumerate(SEQUENCE, start=1):
            writer.write(adu(tid, pdu))
            answers.append((await read_response(reader))[2])
            clock[0] += step  # the device moves between requests, like a real one
        writer.close()
        return answers

    recorded = await drive(proxy_port)
    await proxy.close()
    writer.close()
    await device.close()
    assert len(set(recorded)) > 3  # the values really changed
    replay = ModbusServer(Replayer(read_recording(path)), host=HOST, port=0)
    _, replay_port = await replay.start()
    try:
        assert await drive(replay_port) == recorded
    finally:
        await replay.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_replay_command_ready_line_and_summary(tmp_path: Path) -> None:
    path = write_recording(tmp_path, x(0, PV, "040200c8"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rigtape", "replay", str(path), "--listen", "0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        ready = json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=10))
        assert (ready["mode"], ready["recording"]) == ("replay", str(path))
        assert await exchange(ready["modbus"]["port"], PV, SP) == ["040200c8", "8304"]
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), timeout=5) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.stderr is not None
    assert b"replayed 2 requests: misses=1 unrecorded_writes=0" in await proc.stderr.read()


def test_serve_and_replay_point_at_each_other_for_the_wrong_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recording = write_recording(tmp_path, x(0, PV, "040200c8"))
    assert main(["serve", str(recording), "--listen", "0"]) == 2
    err = capsys.readouterr().err
    assert f"error: {recording} is a recording, not a device map\nhint: use 'rigtape replay'" in err
    assert main(["replay", str(EXAMPLE), "--listen", "0"]) == 2
    err = capsys.readouterr().err
    assert "not a rigtape.recording file" in err
    assert "hint: record one with 'rigtape record'" in err


def test_truncated_last_line_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_recording(tmp_path, x(0, PV, "040200c8"), '{"t":1,"ev":"x","conn":1,"ti')
    with caplog.at_level(logging.WARNING):
        recording = read_recording(path)
    assert len(recording.exchanges) == 1
    assert "ignoring truncated last line" in caplog.text


@pytest.mark.parametrize(
    ("events", "message"),
    [
        pytest.param(['{"t":0', x(1, PV, "040200c8")], "session.jsonl:2: not valid JSON", id="mid"),
        pytest.param([x(0, PV, "zz")], "session.jsonl:2: bad exchange line", id="bad-hex"),
        pytest.param(
            ['{"t":0,"ev":"x","conn":1}'], "session.jsonl:2: bad exchange line", id="missing"
        ),
        pytest.param([x(0, "", "040200c8")], "bad exchange line: empty PDU", id="empty-req"),
        pytest.param([x(0, PV, "")], "bad exchange line: empty PDU", id="empty-rsp"),
    ],
)
def test_bad_lines_are_errors_with_line_numbers(
    tmp_path: Path, events: list[str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        read_recording(write_recording(tmp_path, *events))


def test_newer_major_version_is_refused(tmp_path: Path) -> None:
    path = write_recording(tmp_path, header=HEADER.replace('"version":1', '"version":2'))
    with pytest.raises(ConfigError, match="version 2 is newer"):
        read_recording(path)


def test_unknown_events_and_fields_are_ignored_and_exchanges_sorted(tmp_path: Path) -> None:
    events = [
        '{"t":0,"ev":"open","conn":1,"peer":"x"}',
        x(2.0, PV, "040200c9"),
        '{"t":1,"ev":"x","conn":1,"tid":1,"uid":1,"req":"0400000001","rsp":"040200c8","note":"new"}',
        '{"t":3,"ev":"someday","conn":1}',
    ]
    recording = read_recording(write_recording(tmp_path, *events))
    assert [e.rsp for e in recording.exchanges] == [b"\x04\x02\x00\xc8", b"\x04\x02\x00\xc9"]
