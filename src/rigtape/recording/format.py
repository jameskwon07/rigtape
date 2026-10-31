"""Recording files: JSON Lines v1. One header line, then one event per line, PDUs as hex.

Lines are appended and flushed one at a time, so a crash keeps everything before it. Readers
ignore unknown events and fields, so events can be added without a new major version.
"""

from __future__ import annotations

import errno
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from rigtape.errors import ConfigError

FORMAT = "rigtape.recording"
VERSION = 1

log = logging.getLogger(__name__)


class RecordingWriter:
    def __init__(self, path: Path, *, upstream: str, source: str | None) -> None:
        self._file = path.open("x", encoding="utf-8")  # never overwrite: real recordings are rare
        self._start = time.monotonic()
        self._upstream = upstream
        self._source = source

    def begin(self, listen: str) -> None:
        """Write the header once the proxy is listening, so `listen` is the bound address."""
        started_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self._write(
            format=FORMAT, version=VERSION, protocol="modbus-tcp", started_at=started_at,
            tool=f"rigtape {version('rigtape')}", upstream=self._upstream, listen=listen,
            source=self._source,
        )  # fmt: skip

    def now(self) -> float:
        """Seconds since the recording started (monotonic)."""
        return time.monotonic() - self._start

    def event(self, ev: str, t: float | None = None, /, **fields: object) -> None:
        """One event line; `t` defaults to now (the proxy passes the request's arrival time)."""
        self._write(t=round(self.now() if t is None else t, 6), ev=ev, **fields)

    def close(self) -> None:
        self._file.close()

    def _write(self, **fields: object) -> None:
        self._file.write(json.dumps(fields, separators=(",", ":")) + "\n")
        self._file.flush()


@dataclass(frozen=True, slots=True)
class Exchange:
    t: float  # when the request arrived, seconds since the recording started
    unit: int
    req: bytes  # request PDU
    rsp: bytes | None  # response PDU; None when the device never answered


@dataclass(frozen=True)
class Recording:
    header: dict[str, Any]
    exchanges: list[Exchange]  # sorted by t (stable), including unanswered ones


def read_recording(path: Path) -> Recording:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        denied = exc.errno == errno.EACCES
        hint = "in Docker, the file must be readable by uid 999" if denied else None
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}", hint=hint) from exc
    try:
        header = _parse(path, 1, lines[0]) if lines else None
    except ConfigError:
        header = None
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise ConfigError(
            f"{path}: not a {FORMAT} file",
            hint="record one with 'rigtape record'; device.yaml files are for 'rigtape serve'",
        )
    major = header.get("version")
    if not isinstance(major, int) or major > VERSION:
        raise ConfigError(f"{path}: recording version {major!r} is newer than this rigtape")
    exchanges = []
    for number, line in enumerate(lines[1:], start=2):
        try:
            event = _parse(path, number, line)
        except ConfigError:
            if number != len(lines):
                raise
            log.warning("%s: ignoring truncated last line", path)
            break
        if isinstance(event, dict) and event.get("ev") == "x":
            exchanges.append(_exchange(path, number, event))
    exchanges.sort(key=lambda x: x.t)
    return Recording(header, exchanges)


def _parse(path: Path, number: int, line: str) -> object:
    try:
        return json.loads(line)
    except ValueError as exc:
        raise ConfigError(f"{path}:{number}: not valid JSON: {exc}") from exc


def _exchange(path: Path, number: int, event: dict[str, Any]) -> Exchange:
    try:
        req, rsp = bytes.fromhex(event["req"]), event["rsp"]
        rsp = None if rsp is None else bytes.fromhex(rsp)
        if not req or rsp == b"":
            raise ValueError("empty PDU")
        return Exchange(t=float(event["t"]), unit=int(event["uid"]), req=req, rsp=rsp)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path}:{number}: bad exchange line: {exc}") from exc
