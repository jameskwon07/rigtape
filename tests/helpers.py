"""Raw Modbus TCP frames and subprocess helpers shared by the test modules."""

from __future__ import annotations

import asyncio
import json
import os
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"


def adu(tid: int, pdu: str, *, unit: int = 1, protocol: int = 0) -> bytes:
    body = bytes.fromhex(pdu)
    return struct.pack(">HHHB", tid, protocol, len(body) + 1, unit) + body


async def raw(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(HOST, port)


async def read_response(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    tid, _, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
    return tid, unit, (await reader.readexactly(length - 1)).hex()


async def exchange(port: int, *pdus: str, unit: int = 1) -> list[str]:
    """Send request PDUs one by one on a fresh connection and return the response PDUs."""
    reader, writer = await raw(port)
    answers = []
    for tid, pdu in enumerate(pdus, start=1):
        writer.write(adu(tid, pdu, unit=unit))
        answers.append((await read_response(reader))[2])
    writer.close()
    return answers


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def eventually(check: Callable[[], bool], within: float = 5.0) -> None:
    """Poll a condition the test cannot await directly (file contents, counters)."""
    for _ in range(int(within / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within the timeout")


def child_env() -> dict[str, str]:
    """Environment for a `rigtape` subprocess: the defaults, whatever the developer exported."""
    return {k: v for k, v in os.environ.items() if k != "RIGTAPE_HOST"}
