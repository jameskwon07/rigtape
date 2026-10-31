from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from rigtape.modbus import (
    ExceptionCode,
    FramingError,
    ModbusError,
    ReadRequest,
    Request,
    WriteRequest,
    encode_adu,
    exception_pdu,
    is_exception,
    parse_request,
    read_adu,
)

if TYPE_CHECKING:
    from conftest import RealDevice

ILLEGAL_FUNCTION = ExceptionCode.ILLEGAL_FUNCTION
ILLEGAL_ADDRESS = ExceptionCode.ILLEGAL_DATA_ADDRESS
ILLEGAL_VALUE = ExceptionCode.ILLEGAL_DATA_VALUE


def h(text: str) -> bytes:
    return bytes.fromhex(text)


def stream(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


# --- spec examples (Application Protocol V1.1b3, 6.3 / 6.4 / 6.6 / 6.12) ---


def test_fc3_spec_example() -> None:
    req = parse_request(h("03 006B 0003"))
    assert req == ReadRequest(fc=3, address=0x6B, count=3)
    assert req.response([0x022B, 0x0000, 0x0064]) == h("03 06 022B 0000 0064")


def test_fc4_spec_example() -> None:
    req = parse_request(h("04 0008 0001"))
    assert req == ReadRequest(fc=4, address=8, count=1)
    assert req.response([0x000A]) == h("04 02 000A")


def test_fc6_response_is_echo() -> None:
    pdu = h("06 0001 0003")
    req = parse_request(pdu)
    assert req == WriteRequest(fc=6, address=1, values=(3,))
    assert req.response() == pdu


def test_fc16_response_is_header() -> None:
    req = parse_request(h("10 0001 0002 04 000A 0102"))
    assert req == WriteRequest(fc=16, address=1, values=(0x000A, 0x0102))
    assert req.response() == h("10 0001 0002")


def test_read_response_needs_exact_value_count() -> None:
    with pytest.raises(ValueError, match="expected 2 values"):
        ReadRequest(fc=3, address=0, count=2).response([1])


# --- check order: 01 -> 03 -> 02 ---


@pytest.mark.parametrize(
    ("pdu", "code"),
    [
        pytest.param("01 0000 0001", ILLEGAL_FUNCTION, id="fc1-unsupported"),
        pytest.param("2B 0E 01 00", ILLEGAL_FUNCTION, id="fc43-unsupported"),
        pytest.param("00 0000 0001", ILLEGAL_FUNCTION, id="fc0-invalid"),
        pytest.param("", ILLEGAL_FUNCTION, id="empty"),
        pytest.param("03 0000 0000", ILLEGAL_VALUE, id="fc3-count-0"),
        pytest.param("03 0000 007E", ILLEGAL_VALUE, id="fc3-count-126"),
        pytest.param("04 0000 0000", ILLEGAL_VALUE, id="fc4-count-0"),
        pytest.param("04 0000 007E", ILLEGAL_VALUE, id="fc4-count-126"),
        pytest.param("03 FFFF 007E", ILLEGAL_VALUE, id="fc3-value-before-address"),
        pytest.param("03 0000", ILLEGAL_VALUE, id="fc3-short"),
        pytest.param("03 0000 0001 00", ILLEGAL_VALUE, id="fc3-long"),
        pytest.param("06 0000", ILLEGAL_VALUE, id="fc6-short"),
        pytest.param("10 0000 0000 00", ILLEGAL_VALUE, id="fc16-count-0"),
        pytest.param("10 0000 007C F8" + "0000" * 124, ILLEGAL_VALUE, id="fc16-count-124"),
        pytest.param("10 0000 0002 02 0001", ILLEGAL_VALUE, id="fc16-byte-count"),
        pytest.param("10 0000 0002 04 0001", ILLEGAL_VALUE, id="fc16-missing-data"),
        pytest.param("10 FFFF 0000 00", ILLEGAL_VALUE, id="fc16-value-before-address"),
        pytest.param("10 0000 0001", ILLEGAL_VALUE, id="fc16-no-byte-count"),
        pytest.param("03 FFFF 0002", ILLEGAL_ADDRESS, id="fc3-past-end"),
        pytest.param("04 FF84 007D", ILLEGAL_ADDRESS, id="fc4-past-end"),
        pytest.param("10 FFFF 0002 04 0001 0002", ILLEGAL_ADDRESS, id="fc16-past-end"),
    ],
)
def test_parse_request_exception_codes(pdu: str, code: int) -> None:
    with pytest.raises(ModbusError) as info:
        parse_request(h(pdu))
    assert info.value.code == code


@pytest.mark.parametrize(
    "pdu",
    [
        "03 0000 007D",
        "03 FFFF 0001",
        "04 FF83 007D",
        "06 FFFF FFFF",
        "10 FF85 007B F6" + "00" * 246,
    ],
)
def test_parse_request_accepts_limits(pdu: str) -> None:
    parse_request(h(pdu))


@pytest.mark.parametrize(("fc", "pdu"), [(0x01, "8101"), (0x03, "8302"), (0x10, "9004")])
def test_exception_pdu(fc: int, pdu: str) -> None:
    code = int(pdu[2:], 16)
    assert exception_pdu(fc, code) == h(pdu)
    assert is_exception(h(pdu))


def test_is_exception_false_for_normal_responses() -> None:
    assert not is_exception(h("03 02 0000"))
    assert not is_exception(b"")


def test_request_fc() -> None:
    assert Request(conn_id=1, tid=7, unit=1, pdu=h("10 0000 0001 02 0000")).fc == 0x10


# --- MBAP framing ---

ADU_A = h("0001 0000 0006 11 03 006B 0003")
ADU_B = h("FFFF 0000 0006 00 06 0001 0003")


def test_encode_adu() -> None:
    assert encode_adu(1, 0x11, h("03 006B 0003")) == ADU_A
    with pytest.raises(ValueError, match="PDU length"):
        encode_adu(1, 1, b"")


async def test_read_two_adus_in_one_chunk() -> None:
    reader = stream(ADU_A + ADU_B)
    assert await read_adu(reader) == (1, 0x11, h("03 006B 0003"))
    assert await read_adu(reader) == (0xFFFF, 0, h("06 0001 0003"))
    assert await read_adu(reader) is None


async def test_read_adu_split_into_single_bytes() -> None:
    reader = stream(*(bytes([b]) for b in ADU_A))
    assert await read_adu(reader) == (1, 0x11, h("03 006B 0003"))
    assert await read_adu(reader) is None


@pytest.mark.parametrize("length", [0, 1, 255, 0xFFFF])
async def test_read_adu_rejects_bad_length(length: int) -> None:
    header = h("0001 0000") + length.to_bytes(2, "big") + b"\x01"
    with pytest.raises(FramingError, match="MBAP length"):
        await read_adu(stream(header + b"\x03" * 300))


@pytest.mark.parametrize("length", [2, 254])
async def test_read_adu_accepts_length_limits(length: int) -> None:
    pdu = b"\x03" * (length - 1)
    header = h("0001 0000") + length.to_bytes(2, "big") + b"\x07"
    assert await read_adu(stream(header + pdu)) == (1, 7, pdu)


async def test_read_adu_skips_nonzero_protocol_id() -> None:
    other = h("0005 0001 0006 01 03 0000 0001")
    assert await read_adu(stream(other + ADU_A)) == (1, 0x11, h("03 006B 0003"))


@pytest.mark.parametrize("data", [ADU_A[:3], ADU_A[:-1]], ids=["in-header", "in-pdu"])
async def test_read_adu_eof_inside_frame(data: bytes) -> None:
    with pytest.raises(FramingError, match="connection closed"):
        await read_adu(stream(data))


# --- interop smoke test against the pymodbus "real device" ---


async def test_codec_reads_from_pymodbus_device(real_device: RealDevice) -> None:
    reader, writer = await asyncio.open_connection(real_device.host, real_device.port)
    try:
        writer.write(encode_adu(0x1234, real_device.unit, h("04 0000 0001")))
        await writer.drain()
        tid, unit, pdu = await asyncio.wait_for(read_adu(reader), 5) or (0, 0, b"")
    finally:
        writer.close()
        await writer.wait_closed()
    assert (tid, unit) == (0x1234, real_device.unit)
    assert pdu == ReadRequest(fc=4, address=0, count=1).response(real_device.input_registers[:1])
