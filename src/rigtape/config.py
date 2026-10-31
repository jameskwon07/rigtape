"""YAML configuration loading: strict YAML, pydantic validation, one-line errors."""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from rigtape.errors import ConfigError

_MERGE_TAG = "tag:yaml.org,2002:merge"


class ConfigModel(BaseModel):
    """Base for user-written files: unknown keys are errors, not silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


ModelT = TypeVar("ModelT", bound=BaseModel)


class _DuplicateKeyError(yaml.constructor.ConstructorError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML keeps the last one silently)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, Hashable):
                continue  # SafeLoader reports unhashable keys itself
            if key in seen:
                raise _DuplicateKeyError(
                    problem=f"duplicate key {key!r}", problem_mark=key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not UTF-8 text") from exc
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f"{path}:{mark.line + 1}:{mark.column + 1}" if mark else str(path)
        hint = "remove or rename one of the keys" if isinstance(exc, _DuplicateKeyError) else None
        what = ", ".join(part for part in (exc.context, exc.problem) if part)
        raise ConfigError(f"{where}: {_one_line(what)}", hint=hint) from exc
    except yaml.YAMLError as exc:  # e.g. ReaderError for control characters
        raise ConfigError(f"{path}: invalid YAML: {_one_line(str(exc))}") from exc


def load_model(path: Path, model: type[ModelT]) -> ModelT:
    data = load_yaml(path)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_first_error(exc)}") from exc


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    first = errors[0]
    field = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in first["loc"])
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{field.lstrip('.') or 'top level'}: {_one_line(first['msg'])}{more}"


def _one_line(text: str) -> str:
    return " ".join(text.split())
