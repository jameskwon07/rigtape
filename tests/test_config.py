from __future__ import annotations

import io
from pathlib import Path

import pytest
from pydantic import field_validator

from rigtape.config import ConfigModel, load_model, load_yaml
from rigtape.errors import ConfigError, ExitCode, report


class Register(ConfigModel):
    name: str
    address: int


class Device(ConfigModel):
    name: str
    holding_registers: tuple[Register, ...] = ()


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "device.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def config_error(path: Path) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        load_model(path, Device)
    return info.value


def test_loads_valid_file(tmp_path: Path) -> None:
    path = write(tmp_path, "name: pid\nholding_registers:\n  - {name: sp, address: 0}\n")
    assert load_model(path, Device) == Device(
        name="pid", holding_registers=(Register(name="sp", address=0),)
    )


@pytest.mark.parametrize(
    ("text", "line"),
    [
        pytest.param("name: a\nname: b\n", 2, id="top-level"),
        pytest.param("name: a\nholding_registers:\n  - {name: sp, name: pv}\n", 3, id="nested"),
        pytest.param("x:\n  y:\n    z: 1\n    z: 2\n", 4, id="deep"),
    ],
)
def test_rejects_duplicate_keys(tmp_path: Path, text: str, line: int) -> None:
    error = config_error(write(tmp_path, text))
    assert f"device.yaml:{line}:" in str(error)
    assert "duplicate key" in str(error)
    assert error.hint


def test_allows_merge_keys(tmp_path: Path) -> None:
    text = "base: &b {name: sp, address: 1}\nother:\n  <<: *b\n  address: 2\n"
    assert load_yaml(write(tmp_path, text)) == {
        "base": {"name": "sp", "address": 1},
        "other": {"name": "sp", "address": 2},
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(
            "name: pid\ncolour: red\n", "colour: Extra inputs are not permitted", id="extra"
        ),
        pytest.param(
            "name: pid\nholding_registers:\n  - {name: sp, address: x}\n",
            "holding_registers[0].address: Input should be a valid integer",
            id="nested-path",
        ),
        pytest.param("holding_registers: []\n", "name: Field required", id="missing"),
        pytest.param("- a\n", "top level: Input should be a valid dictionary", id="not-mapping"),
        pytest.param("", "top level: Input should be a valid dictionary", id="empty"),
    ],
)
def test_validation_error_is_one_line_with_path(tmp_path: Path, text: str, message: str) -> None:
    error = str(config_error(write(tmp_path, text)))
    assert "\n" not in error
    assert error.startswith(f"{tmp_path / 'device.yaml'}: ")
    assert message in error


def test_counts_further_errors(tmp_path: Path) -> None:
    error = str(config_error(write(tmp_path, "a: 1\nb: 2\n")))
    assert error.endswith("(and 2 more)")  # a, b extra; name missing


def test_yaml_syntax_error_has_position(tmp_path: Path) -> None:
    error = config_error(write(tmp_path, "name: [unclosed\n"))
    assert "device.yaml:2:1:" in str(error)
    assert error.hint is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("name: a\x07b\n", "invalid YAML: unacceptable character", id="control-char"),
        pytest.param(
            "a: 1\n---\nb: 2\n", "2:1: expected a single document in the stream", id="multi-doc"
        ),
    ],
)
def test_yaml_errors_are_one_line(tmp_path: Path, text: str, message: str) -> None:
    error = str(config_error(write(tmp_path, text)))
    assert "\n" not in error
    assert message in error


class Strict(ConfigModel):
    name: str

    @field_validator("name")
    @classmethod
    def reject(cls, value: str) -> str:
        raise ValueError("line one\nline two")


def test_multiline_validator_message_becomes_one_line(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_model(write(tmp_path, "name: x\n"), Strict)
    assert str(info.value).endswith("name: Value error, line one line two")


def test_missing_file(tmp_path: Path) -> None:
    error = config_error(tmp_path / "nope.yaml")
    assert "cannot read" in str(error)
    assert error.exit_code == ExitCode.USAGE_ERROR


def test_report_prints_error_and_hint() -> None:
    out = io.StringIO()
    code = report(ConfigError("bad file", hint="fix it"), out)
    assert code == 2
    assert out.getvalue() == "error: bad file\nhint: fix it\n"
