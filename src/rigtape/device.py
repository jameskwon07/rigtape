"""device.yaml: a register map with simple behaviors, served as a Modbus TCP device."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from rigtape.config import ConfigModel, load_model
from rigtape.errors import ConfigError
from rigtape.modbus import (
    READ_HOLDING_REGISTERS,
    ExceptionCode,
    ModbusError,
    ReadRequest,
    Request,
    WriteRequest,
    exception_pdu,
    parse_request,
)

Table = Literal["holding_registers", "input_registers"]
Loc = tuple[Table, int]
TABLES: tuple[Table, ...] = ("holding_registers", "input_registers")


class InputRegister(ConfigModel):
    address: int = Field(ge=0, le=0xFFFF, strict=True)  # 0-based PDU address
    value: int = Field(default=0, ge=0, le=0xFFFF, strict=True)  # raw 16-bit value
    name: str | None = None


class HoldingRegister(InputRegister):
    readonly: bool = Field(default=False, strict=True)


class Ramp(ConfigModel):
    """`register` moves toward the current value of `toward` and never overshoots."""

    type: Literal["ramp"]
    driven: str = Field(alias="register")  # `register` would shadow ABCMeta.register
    toward: str
    rate_per_s: float = Field(gt=0, allow_inf_nan=False)  # lax: YAML 1.1 reads 1e3 as text


class DeviceSpec(ConfigModel):
    format: Literal["rigtape.device/v1"]
    name: str | None = None
    source: str | None = None  # where the register map comes from; required in examples/
    holding_registers: tuple[HoldingRegister, ...] = ()
    input_registers: tuple[InputRegister, ...] = ()
    behaviors: tuple[Ramp, ...] = ()


def load_device(path: Path) -> DeviceSpec:
    spec = load_model(path, DeviceSpec)
    if problem := _first_problem(spec):
        raise ConfigError(f"{path}: {problem}")
    return spec


def _first_problem(spec: DeviceSpec) -> str | None:
    """Checks across entries that pydantic cannot express per field, as 'field path: message'."""
    addresses: dict[Loc, str] = {}
    names: dict[str, tuple[str, HoldingRegister | InputRegister]] = {}
    for table, i, reg in _registers(spec):
        where = f"{table}[{i}]"
        if (first := addresses.get((table, reg.address))) is not None:
            return f"{where}.address: address {reg.address} is already used by {first}"
        if reg.name in names:
            return f"{where}.name: name {reg.name!r} is already used by {names[reg.name][0]}"
        addresses[table, reg.address] = where
        if reg.name is not None:
            names[reg.name] = (where, reg)
    driven = [ramp.driven for ramp in spec.behaviors]
    for i, ramp in enumerate(spec.behaviors):
        for key, name in (("register", ramp.driven), ("toward", ramp.toward)):
            if name not in names:
                return f"behaviors[{i}].{key}: no register is named {name!r}"
        where, reg = names[ramp.driven]
        if isinstance(reg, HoldingRegister) and not reg.readonly:
            return f"behaviors[{i}].register: {where} is writable; add readonly: true"
        if driven.count(ramp.driven) > 1:
            return f"behaviors[{i}].register: {ramp.driven!r} has more than one behavior"
        if ramp.toward in driven:
            return f"behaviors[{i}].toward: {ramp.toward!r} is itself driven by a behavior"
    return None


def _registers(spec: DeviceSpec) -> list[tuple[Table, int, HoldingRegister | InputRegister]]:
    return [(t, i, reg) for t in TABLES for i, reg in enumerate(getattr(spec, t))]


@dataclass
class _Ramp:
    target: Loc
    rate_per_s: float
    start: float  # unrounded, so restarting on every write loses no partial progress
    start_time: float

    def position(self, target: int, now: float) -> float:
        step = self.rate_per_s * (now - self.start_time)
        if self.start <= target:
            return min(self.start + step, target)
        return max(self.start - step, target)


class Device:
    """The Handler behind `rigtape serve`. Behaviors have no timers: reads use the clock."""

    def __init__(self, spec: DeviceSpec, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._values: dict[Loc, int] = {}
        self._readonly: set[Loc] = {
            ("holding_registers", h.address) for h in spec.holding_registers if h.readonly
        }
        loc: dict[str, Loc] = {}
        for table, _, reg in _registers(spec):
            self._values[table, reg.address] = reg.value
            if reg.name is not None:
                loc[reg.name] = (table, reg.address)
        now = clock()
        self._ramps = {
            loc[r.driven]: _Ramp(loc[r.toward], r.rate_per_s, self._values[loc[r.driven]], now)
            for r in spec.behaviors
        }

    def handle(self, req: Request) -> bytes:
        try:
            parsed = parse_request(req.pdu)
            if isinstance(parsed, ReadRequest):
                return parsed.response(self._read(parsed))
            self._write(parsed)
            return parsed.response()
        except ModbusError as exc:
            return exception_pdu(req.fc, exc.code)

    def _read(self, req: ReadRequest) -> list[int]:
        holding = req.fc == READ_HOLDING_REGISTERS
        table: Table = "holding_registers" if holding else "input_registers"
        locs = [(table, a) for a in range(req.address, req.address + req.count)]
        if any(loc not in self._values for loc in locs):
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        now = self._clock()  # one instant per request, so a block read is consistent
        return [self._value(loc, now) for loc in locs]

    def _write(self, req: WriteRequest) -> None:
        locs: list[Loc] = [("holding_registers", req.address + i) for i in range(len(req.values))]
        if any(loc not in self._values or loc in self._readonly for loc in locs):
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_ADDRESS)  # nothing is written
        now = self._clock()
        for ramp in self._ramps.values():
            if ramp.target in locs:  # restart from the current position toward the new target
                ramp.start, ramp.start_time = ramp.position(self._values[ramp.target], now), now
        self._values.update(zip(locs, req.values, strict=True))

    def _value(self, loc: Loc, now: float) -> int:
        ramp = self._ramps.get(loc)
        if ramp is None:
            return self._values[loc]
        return round(ramp.position(self._values[ramp.target], now))  # nearest, either direction
