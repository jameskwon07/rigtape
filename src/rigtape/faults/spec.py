"""faults.yaml: a scenario of rules, checked top to bottom; the first rule that fires wins."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from rigtape.config import ConfigModel, load_model
from rigtape.errors import ConfigError

Unit = Annotated[int, Field(ge=0, le=255, strict=True)]
FunctionCode = Annotated[int, Field(ge=1, le=127, strict=True)]
Address = Annotated[int, Field(ge=0, le=0xFFFF, strict=True)]
FaultType = Literal["delay", "no_response", "exception", "disconnect"]


class Match(ConfigModel):
    """Which requests a rule applies to; an omitted field matches everything."""

    unit_ids: tuple[Unit, ...] | None = Field(default=None, min_length=1)
    function_codes: tuple[FunctionCode, ...] | None = Field(default=None, min_length=1)
    address: tuple[Address, Address] | None = None  # [from, to]; overlaps the request's range


class Rule(ConfigModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")  # also the API path segment
    type: FaultType
    match: Match = Match()
    probability: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    after: int = Field(default=0, ge=0, strict=True)  # let this many matches through first
    count: int | None = Field(default=None, ge=1, strict=True)  # then fire at most this often
    enabled: bool = Field(default=True, strict=True)
    delay_ms: int | None = Field(default=None, ge=0, strict=True)  # delay only
    code: int | None = Field(default=None, ge=1, le=255, strict=True)  # exception only


class Scenario(ConfigModel):
    format: Literal["rigtape.faults/v1"]
    seed: Annotated[int, Field(strict=True)] | str | None = None  # strict: no `true` -> 1
    rules: tuple[Rule, ...] = ()


def load_scenario(path: Path) -> Scenario:
    scenario = load_model(path, Scenario)
    if problem := _first_problem(scenario):
        raise ConfigError(f"{path}: {problem}")
    return scenario


def _first_problem(scenario: Scenario) -> str | None:
    """Checks across fields, as 'field path: message'."""
    seen: dict[str, int] = {}
    for i, rule in enumerate(scenario.rules):
        if rule.name in seen:
            return (
                f"rules[{i}].name: name {rule.name!r} is already used by rules[{seen[rule.name]}]"
            )
        seen[rule.name] = i
        for field, needed_by in (("delay_ms", "delay"), ("code", "exception")):
            present = getattr(rule, field) is not None
            if rule.type == needed_by and not present:
                return f"rules[{i}].{field}: required for type {needed_by}"
            if rule.type != needed_by and present:
                return f"rules[{i}].{field}: only used by type {needed_by}"
        if rule.match.address is not None and rule.match.address[0] > rule.match.address[1]:
            return f"rules[{i}].match.address: from {rule.match.address[0]} is above to"
    return None
