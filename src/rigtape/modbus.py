"""Modbus TCP framing (MBAP) and the PDU codec for function codes 3, 4, 6 and 16.

References: MODBUS Application Protocol Specification V1.1b3 and
MODBUS Messaging on TCP/IP Implementation Guide V1.0b.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04
WRITE_SINGLE_REGISTER = 0x06
WRITE_MULTIPLE_REGISTERS = 0x10

MAX_READ_COUNT = 125  # 0x7D
MAX_WRITE_COUNT = 123  # 0x7B
ADDRESS_SPACE = 0x10000

_MBAP = struct.Struct(">HHHB")  # transaction id, protocol id, length, unit id
_ADDRESS_COUNT = struct.Struct(">HH")


class ExceptionCode(IntEnum):
    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03
    SERVER_DEVICE_FAILURE = 0x04
    SERVER_DEVICE_BUSY = 0x06
    GATEWAY_TARGET_FAILED = 0x0B


class ModbusError(Exception):
    """A request that must be answered with a Modbus exception response."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Modbus exception {code:#04x}")
        self.code = code


class FramingError(Exception):
    """The byte stream cannot be split into ADUs any more; close the connection."""


@dataclass(frozen=True, slots=True)
class Request:
    conn_id: int
    tid: int
    unit: int
    pdu: bytes  # function code + data, exactly as received

    @property
    def fc(self) -> int:
        return self.pdu[0]


@dataclass(frozen=True, slots=True)
class ReadRequest:
    fc: int
    address: int
    count: int

    def response(self, values: Sequence[int]) -> bytes:
        if len(values) != self.count:
            raise ValueError(f"expected {self.count} values, got {len(values)}")
        return struct.pack(f">BB{self.count}H", self.fc, 2 * self.count, *values)


@dataclass(frozen=True, slots=True)
class WriteRequest:
    fc: int
    address: int
    values: tuple[int, ...]

    def response(self) -> bytes:
        if self.fc == WRITE_SINGLE_REGISTER:
            return struct.pack(">BHH", self.fc, self.address, self.values[0])  # echo
        return struct.pack(">BHH", self.fc, self.address, len(self.values))


def is_exception(pdu: bytes) -> bool:
    return bool(pdu) and pdu[0] & 0x80 != 0


def exception_pdu(fc: int, code: int) -> bytes:
    return bytes([(fc | 0x80) & 0xFF, code])


def encode_adu(tid: int, unit: int, pdu: bytes) -> bytes:
    if not 1 <= len(pdu) <= 253:
        raise ValueError(f"PDU length {len(pdu)} is outside 1-253")
    return _MBAP.pack(tid, 0, len(pdu) + 1, unit) + pdu


async def read_adu(reader: asyncio.StreamReader) -> tuple[int, int, bytes] | None:
    """Read the next ADU as (tid, unit, pdu); None on a clean EOF between frames.

    Frames with a non-zero protocol id are skipped. A length outside 2-254 raises
    FramingError, because the stream cannot be resynchronised after it.
    """
    while True:
        try:
            header = await reader.readexactly(_MBAP.size)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return None
            raise FramingError("connection closed inside an MBAP header") from exc
        tid, protocol, length, unit = _MBAP.unpack(header)
        if not 2 <= length <= 254:
            raise FramingError(f"MBAP length {length} is outside 2-254")
        try:
            pdu = await reader.readexactly(length - 1)
        except asyncio.IncompleteReadError as exc:
            raise FramingError("connection closed inside a PDU") from exc
        if protocol == 0:
            return tid, unit, pdu


def parse_request(pdu: bytes) -> ReadRequest | WriteRequest:
    """Parse a request PDU, checking in the spec order 01 -> 03 -> 02.

    Raises ModbusError with the exception code to send back.
    """
    fc = pdu[0] if pdu else 0
    data = pdu[1:]
    if fc in (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS):
        address, count = _two_words(data, exact=True)
        _check(1 <= count <= MAX_READ_COUNT, ExceptionCode.ILLEGAL_DATA_VALUE)
        _check(address + count <= ADDRESS_SPACE, ExceptionCode.ILLEGAL_DATA_ADDRESS)
        return ReadRequest(fc, address, count)
    if fc == WRITE_SINGLE_REGISTER:
        address, value = _two_words(data, exact=True)
        return WriteRequest(fc, address, (value,))
    if fc == WRITE_MULTIPLE_REGISTERS:
        address, count = _two_words(data, exact=False)
        values = data[5:]
        _check(1 <= count <= MAX_WRITE_COUNT, ExceptionCode.ILLEGAL_DATA_VALUE)
        _check(data[4] == 2 * count == len(values), ExceptionCode.ILLEGAL_DATA_VALUE)
        _check(address + count <= ADDRESS_SPACE, ExceptionCode.ILLEGAL_DATA_ADDRESS)
        return WriteRequest(fc, address, struct.unpack(f">{count}H", values))
    raise ModbusError(ExceptionCode.ILLEGAL_FUNCTION)


def _two_words(data: bytes, *, exact: bool) -> tuple[int, int]:
    # FC16 has a byte count after address and quantity, so it needs at least 5 bytes.
    _check(len(data) == 4 if exact else len(data) >= 5, ExceptionCode.ILLEGAL_DATA_VALUE)
    address, count = _ADDRESS_COUNT.unpack_from(data)
    return address, count


def _check(condition: bool, code: ExceptionCode) -> None:
    if not condition:
        raise ModbusError(code)
