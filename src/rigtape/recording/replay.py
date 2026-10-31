"""`rigtape replay`: answer requests from a recording, no device attached.

The recording is cut into segments at value-changing writes; inside a segment each distinct
request has a FIFO of recorded responses. Requests drive the cursor, so replay is deterministic
and extra polling before a write can never return post-write values. The user-facing rules are
in README.md, "Replay a recording".
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from rigtape.modbus import ExceptionCode, Request, exception_pdu, is_exception
from rigtape.recording.format import Exchange, Recording

WRITE_FCS = frozenset({5, 6, 15, 16, 22, 23})
Key = tuple[int | None, bytes]  # (unit id when the recording has several, request PDU)

log = logging.getLogger(__name__)


@dataclass
class _Segment:
    checkpoint: Key | None  # the value-changing write that opens this segment
    responses: dict[Key, list[bytes]] = field(default_factory=dict)
    cursor: Counter[Key] = field(default_factory=Counter)

    def pending(self, key: Key) -> bool:
        """Recorded responses for this key that have not been replayed yet."""
        return self.cursor[key] < len(self.responses.get(key, ()))

    def take(self, key: Key) -> bytes:
        """The next recorded response in order; once used up, stay on the settled one."""
        responses = self.responses[key]
        index = self.cursor[key]
        self.cursor[key] += 1
        return responses[index] if index < len(responses) else self.settled(key)

    def settled(self, key: Key) -> bytes:
        # the last normal response, so a recorded exception (06 busy) does not repeat forever
        responses = self.responses[key]
        return next((r for r in reversed(responses) if not is_exception(r)), responses[-1])

    def first(self, key: Key) -> bytes:
        responses = self.responses[key]  # first normal response, for reads borrowed before a write
        return next((r for r in responses if not is_exception(r)), responses[0])


class Replayer:
    """The Handler behind `rigtape replay`. One cursor per process, shared by all connections."""

    def __init__(self, recording: Recording) -> None:
        answered = [x for x in recording.exchanges if x.rsp is not None]
        if dropped := len(recording.exchanges) - len(answered):
            log.warning("%d unanswered requests not replayed; use a fault scenario", dropped)
        self._units = {x.unit for x in answered}
        self._multi_unit = len(self._units) > 1
        self._segments = self._build(answered)
        self._current = 0
        self._borrowed: set[Key] = set()
        self.requests = self.misses = self.unrecorded_writes = 0

    def _key(self, unit: int, pdu: bytes) -> Key:
        return (unit if self._multi_unit else None, pdu)

    def _build(self, answered: list[Exchange]) -> list[_Segment]:
        segments = [_Segment(None)]
        last_write: dict[tuple[int | None, bytes], bytes] = {}  # (unit, fc + address) -> PDU
        for x in answered:
            key = self._key(x.unit, x.req)
            assert x.rsp is not None
            if x.req[0] in WRITE_FCS and not is_exception(x.rsp):
                where = (key[0], x.req[:3])
                if last_write.get(where) != x.req:  # first or value-changing write: a checkpoint
                    last_write[where] = x.req
                    segments.append(_Segment(key))
            segments[-1].responses.setdefault(key, []).append(x.rsp)
        return segments

    def handle(self, req: Request) -> bytes:
        self.requests += 1
        if self._multi_unit and req.unit not in self._units:
            return exception_pdu(req.fc, ExceptionCode.GATEWAY_TARGET_FAILED)
        key = self._key(req.unit, req.pdu)
        segment = self._segments[self._current]
        following = self._segments[self._current + 1 : self._current + 2]
        if req.fc in WRITE_FCS:
            if segment.pending(key):  # a recorded repeat or failure of this write comes first
                return segment.take(key)
            if following and following[0].checkpoint == key:
                self._current += 1
                return following[0].take(key)
            if key in segment.responses:
                return segment.take(key)
            return self._unrecorded_write(req, following)
        if key in segment.responses:
            return segment.take(key)
        for earlier in reversed(self._segments[: self._current]):
            if key in earlier.responses:
                return earlier.settled(key)
        for later in self._segments[self._current + 1 :]:
            if key in later.responses:
                if key not in self._borrowed:
                    self._borrowed.add(key)
                    log.warning("%s answered from a later segment (after a write)", req.pdu.hex())
                return later.first(key)
        return self._miss(req)

    def _unrecorded_write(self, req: Request, following: list[_Segment]) -> bytes:
        self.unrecorded_writes += 1
        expected = following[0].checkpoint if following else None
        hint = f"expected next recorded write {expected[1].hex()}" if expected else "none left"
        log.warning("unrecorded write %s; %s", req.pdu.hex(), hint)
        return exception_pdu(req.fc, ExceptionCode.SERVER_DEVICE_FAILURE)

    def _miss(self, req: Request) -> bytes:
        self.misses += 1
        keys = [k for s in self._segments for k in s.responses if k[0] in (None, req.unit)]
        same_fc = [k for k in keys if k[1][:1] == req.pdu[:1]] or keys
        nearest = min(same_fc, key=lambda k: _address_gap(k[1], req.pdu), default=None)
        log.warning(
            "no recorded response for %s; nearest recorded request is %s",
            req.pdu.hex(), nearest[1].hex() if nearest else "none",
        )  # fmt: skip
        return exception_pdu(req.fc, ExceptionCode.SERVER_DEVICE_FAILURE)


def _address_gap(a: bytes, b: bytes) -> int:
    if len(a) >= 3 and len(b) >= 3:
        return abs(int.from_bytes(a[1:3], "big") - int.from_bytes(b[1:3], "big"))
    return 0xFFFF
