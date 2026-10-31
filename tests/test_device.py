"""device.yaml validation and register semantics, driven through Device.handle with raw PDUs."""

from __future__ import annotations

from pathlib import Path

import pytest

from rigtape.device import Device, DeviceSpec, load_device
from rigtape.errors import ConfigError
from rigtape.modbus import Request

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "pid" / "device.yaml"
VALID = """\
format: rigtape.device/v1
holding_registers:
  - {name: sp, address: 0, value: 200}
  - {name: model_id, address: 10, value: 1001, readonly: true}
  - {address: 11, value: 7}
input_registers:
  - {name: pv, address: 0, value: 200}
  - {name: status, address: 20, value: 1}
behaviors:
  - {type: ramp, register: pv, toward: sp, rate_per_s: 10}
"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def device(tmp_path: Path, clock: FakeClock) -> Device:
    return Device(load(tmp_path, VALID), clock)


def load(tmp_path: Path, text: str) -> DeviceSpec:
    path = tmp_path / "device.yaml"
    path.write_text(text, encoding="utf-8")
    return load_device(path)


def call(device: Device, pdu: str) -> str:
    return device.handle(Request(conn_id=1, tid=1, unit=1, pdu=bytes.fromhex(pdu))).hex()


def read_pv(device: Device) -> int:
    return int(call(device, "0400000001")[4:], 16)


def test_example_loads() -> None:
    spec = load_device(EXAMPLE)
    assert spec.name == "Virtual PID temperature controller"
    assert spec.source


@pytest.mark.parametrize(
    ("pdu", "response"),
    [
        pytest.param("0400000001", "040200c8", id="read-pv"),
        pytest.param("0300000001", "030200c8", id="read-sp"),
        pytest.param("030000000b", "8302", id="gap-in-block"),
        pytest.param("0300050001", "8302", id="undefined"),
        pytest.param("04000a0001", "8402", id="holding-only-address"),
        pytest.param("0300000000", "8303", id="count-0-before-address"),
        pytest.param("0100000001", "8101", id="fc1"),
        pytest.param("03000a0002", "030403e90007", id="block"),
    ],
)
def test_reads(device: Device, pdu: str, response: str) -> None:
    assert call(device, pdu) == response


def test_fc6_write_echoes_and_reads_back(device: Device) -> None:
    assert call(device, "0600000258") == "0600000258"
    assert call(device, "0300000001") == "03020258"


def test_fc16_write_reads_back(device: Device) -> None:
    assert call(device, "10000b000102abcd") == "10000b0001"
    assert call(device, "03000b0001") == "0302abcd"


@pytest.mark.parametrize(
    "pdu",
    [
        pytest.param("06000a0007", id="fc6-readonly"),
        pytest.param("0600050001", id="fc6-undefined"),
        pytest.param("0600140001", id="fc6-input-only-address"),
        pytest.param("10000a0001020005", id="fc16-readonly"),
        pytest.param("10000a00020400010002", id="fc16-partly-readonly"),
        pytest.param("10000000020400010002", id="fc16-partly-undefined"),
    ],
)
def test_rejected_writes_change_nothing(device: Device, pdu: str) -> None:
    before = call(device, "0300000001"), call(device, "03000a0002")
    assert call(device, pdu)[2:] == "02"
    assert (call(device, "0300000001"), call(device, "03000a0002")) == before


def test_ramp_follows_setpoint(device: Device, clock: FakeClock) -> None:
    assert read_pv(device) == 200  # equal to sp, nothing moves
    clock.now += 100
    assert read_pv(device) == 200
    call(device, "0600000258")  # sp = 600
    clock.now += 10
    assert read_pv(device) == 300
    assert read_pv(device) == 300  # same instant, same value
    clock.now += 30
    assert read_pv(device) == 600
    clock.now += 100
    assert read_pv(device) == 600  # never overshoots


def test_ramp_restarts_from_current_value_when_setpoint_changes(
    device: Device, clock: FakeClock
) -> None:
    call(device, "0600000258")  # sp = 600
    clock.now += 10  # pv = 300
    call(device, "06000000fa")  # sp = 250: turn around from 300
    clock.now += 2
    assert read_pv(device) == 280
    clock.now += 100
    assert read_pv(device) == 250


def test_ramp_reports_the_nearest_count(device: Device, clock: FakeClock) -> None:
    call(device, "0600000258")  # up from 200
    clock.now += 0.125
    assert read_pv(device) == 201  # 201.25
    clock.now += 0.25
    assert read_pv(device) == 204  # 203.75
    clock.now = 2000.0
    call(device, "06000000c8")  # down from 600 to 200
    clock.now += 0.125
    assert read_pv(device) == 599  # 598.75


def test_turning_around_never_steps_backwards(device: Device, clock: FakeClock) -> None:
    call(device, "0600000258")  # sp = 600
    clock.now += 5.0625  # pv at 250.625
    before = read_pv(device)
    call(device, "06000000c8")  # sp = 200: now moving down
    seen = [read_pv(device)]
    for _ in range(4):
        clock.now += 0.03125
        seen.append(read_pv(device))
    assert before == 251
    assert seen == sorted(seen, reverse=True)
    assert seen[0] <= before


def test_rate_accepts_yaml_exponent_forms(tmp_path: Path) -> None:
    spec = load(tmp_path, VALID.replace("rate_per_s: 10", "rate_per_s: 1e1"))  # YAML 1.1: text
    assert spec.behaviors[0].rate_per_s == 10.0


@pytest.mark.parametrize("period", [1 / 16, 1 / 4, 1.0])
def test_rewriting_the_same_setpoint_does_not_slow_the_ramp(
    device: Device, clock: FakeClock, period: float
) -> None:
    """PLCs and HMIs often rewrite the setpoint every cycle, alone or in an FC16 block."""
    for step in range(round(10 / period)):
        call(device, "0600000258" if step % 2 else "1000000001020258")  # sp = 600
        clock.now += period
    assert read_pv(device) == 300


def test_client_side_setpoint_ramp_keeps_partial_progress(device: Device, clock: FakeClock) -> None:
    sp = 200
    for _ in range(80):  # sp +5 every 0.25 s, always ahead of pv moving at 10/s
        sp += 5
        call(device, f"060000{sp:04x}")
        clock.now += 0.25
    assert read_pv(device) == 400


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(VALID + "colour: red\n", "colour: Extra inputs are not permitted", id="key"),
        pytest.param(
            VALID.replace("format: rigtape.device/v1", "format: rigtape.recording"),
            "format: Input should be 'rigtape.device/v1'",
            id="format",
        ),
        pytest.param(
            VALID.replace("address: 10,", "address: 0,"),
            "holding_registers[1].address: address 0 is already used by holding_registers[0]",
            id="duplicate-address",
        ),
        pytest.param(
            VALID.replace("address: 11", "address: 70000"),
            "holding_registers[2].address: Input should be less than or equal to 65535",
            id="address-70000",
        ),
        pytest.param(
            VALID.replace("value: 7", "value: -1"),
            "holding_registers[2].value: Input should be greater than or equal to 0",
            id="negative-value",
        ),
        pytest.param(
            VALID.replace("name: pv", "name: sp"),
            "input_registers[0].name: name 'sp' is already used by holding_registers[0]",
            id="duplicate-name",
        ),
        pytest.param(
            VALID.replace("toward: sp", "toward: spp"),
            "behaviors[0].toward: no register is named 'spp'",
            id="unknown-toward",
        ),
        pytest.param(
            VALID.replace("register: pv, toward: sp", "register: sp, toward: model_id"),
            "behaviors[0].register: holding_registers[0] is writable; add readonly: true",
            id="writable-driven",
        ),
        pytest.param(
            VALID + "  - {type: ramp, register: pv, toward: model_id, rate_per_s: 1}\n",
            "behaviors[0].register: 'pv' has more than one behavior",
            id="two-behaviors",
        ),
        pytest.param(
            VALID.replace("toward: sp", "toward: pv"),
            "behaviors[0].toward: 'pv' is itself driven by a behavior",
            id="toward-self",
        ),
        pytest.param(
            VALID.replace("rate_per_s: 10", "rate_per_s: .inf"),
            "behaviors[0].rate_per_s: Input should be a finite number",
            id="rate-inf",
        ),
        pytest.param(
            VALID.replace("address: 11", "address: true"),
            "holding_registers[2].address: Input should be a valid integer",
            id="bool-address",
        ),
        pytest.param(
            VALID.replace("value: 7", "value: '7'"),
            "holding_registers[2].value: Input should be a valid integer",
            id="string-value",
        ),
        pytest.param(
            VALID.replace("readonly: true", "readonly: 'yes'"),
            "holding_registers[1].readonly: Input should be a valid boolean",
            id="string-readonly",
        ),
        pytest.param(
            VALID.replace("rate_per_s: 10", "rate_per_s: 0"),
            "behaviors[0].rate_per_s: Input should be greater than 0",
            id="rate-0",
        ),
        pytest.param(
            VALID.replace("type: ramp", "type: lag"),
            "behaviors[0].type: Input should be 'ramp'",
            id="unknown-behavior",
        ),
    ],
)
def test_invalid_device_is_one_line_with_field_path(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ConfigError) as info:
        load(tmp_path, text)
    error = str(info.value)
    assert "\n" not in error
    assert error.startswith(f"{tmp_path / 'device.yaml'}: ")
    assert message in error
