"""The fault engine: matches rules to requests, draws with a seeded hash, and decides.

The draw for the n-th request that matches a rule depends only on (seed, rule name, n), so
adding or removing other rules never shifts a rule's pattern, and the same seed gives the same
faults on every Python version and machine.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from rigtape.faults.spec import Rule, Scenario
from rigtape.modbus import Request
from rigtape.server import Decision

_ADDRESSED = {1: 5, 2: 5, 3: 5, 4: 5, 15: 5, 16: 5, 5: 3, 6: 3}  # fc -> PDU bytes with an address


def fires(seed: str, rule: str, n: int, probability: float) -> bool:
    digest = hashlib.sha256(f"{seed}:{rule}:{n}".encode()).digest()[:8]
    return (int.from_bytes(digest, "big") >> 11) / 2**53 < probability  # exact in float64


def _span(pdu: bytes) -> tuple[int, int] | None:
    """(first address, count) of a request, or None for function codes without an address."""
    fc = pdu[0] if pdu else 0
    if fc not in _ADDRESSED or len(pdu) < _ADDRESSED[fc]:
        return None
    first = int.from_bytes(pdu[1:3], "big")
    return first, int.from_bytes(pdu[3:5], "big") if fc not in (5, 6) else 1


@dataclass
class _State:
    rule: Rule
    enabled: bool
    remaining: int | None  # fires left, None = unlimited
    matched: int = 0
    fired: int = 0

    def matches(self, req: Request) -> bool:
        match = self.rule.match
        if match.unit_ids is not None and req.unit not in match.unit_ids:
            return False
        if match.function_codes is not None and req.fc not in match.function_codes:
            return False
        if match.address is not None:
            span = _span(req.pdu)
            if span is None:
                return False
            first, count = span
            low, high = match.address
            return first <= high and first + max(count, 1) - 1 >= low
        return True

    def decision(self) -> Decision:
        rule = self.rule
        if rule.type == "delay":
            return Decision(delay_s=(rule.delay_ms or 0) / 1000, rule=rule.name)
        if rule.type == "exception":
            return Decision("exception", code=rule.code or 0, rule=rule.name)
        return Decision(rule.type, rule=rule.name)


class FaultEngine:
    """The Interceptor behind --faults. Rule counters are shared by every connection."""

    def __init__(self, scenario: Scenario, seed: int | str) -> None:
        self.seed = str(seed)
        self._states = {r.name: _State(r, r.enabled, r.count) for r in scenario.rules}  # in order

    def decide(self, req: Request) -> Decision:
        decision = Decision()
        for state in self._states.values():
            if not state.enabled or not state.matches(req):
                continue
            state.matched += 1  # every matching active rule counts, even when another one fired
            if state.matched <= state.rule.after or state.remaining == 0:
                continue
            if not fires(self.seed, state.rule.name, state.matched, state.rule.probability):
                continue
            state.fired += 1
            if state.remaining is not None:
                state.remaining -= 1  # spent even when an earlier rule already won this request
            if decision.rule is None:
                decision = state.decision()  # the first rule that fires wins
        return decision

    @property
    def names(self) -> list[str]:
        return list(self._states)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Switch a rule on or off; False when there is no such rule."""
        if name not in self._states:
            return False
        self._states[name].enabled = enabled
        return True

    def status(self) -> dict[str, Any]:
        rules = [
            {
                "name": s.rule.name,
                "type": s.rule.type,
                "enabled": s.enabled,
                "matched": s.matched,
                "fired": s.fired,
                "remaining": s.remaining,
            }
            for s in self._states.values()
        ]
        return {"seed": self.seed, "rules": rules}
