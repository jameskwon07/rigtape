"""Repository rules that keep the module boundaries and the runtime license-clean.

- core modules never import the feature packages (rigtape.recording, rigtape.faults);
  those packages never import each other; only cli/__main__ wire everything together.
- src never imports pymodbus (a dev-only dependency, missing from the --no-dev image).
- every runtime dependency, transitively, has a license on the allow-list.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from importlib.metadata import metadata, requires
from pathlib import Path

import pytest
from packaging.requirements import Requirement  # ships with pytest
from packaging.utils import canonicalize_name

from rigtape.config import load_yaml
from rigtape.device import load_device
from rigtape.recording.format import read_recording

SRC = Path(__file__).resolve().parents[1] / "src"
FEATURE_PACKAGES = ("rigtape.recording", "rigtape.faults")
ASSEMBLY = {"rigtape.cli", "rigtape.__main__"}
ALLOWED_LICENSES = {
    # SPDX ids (License-Expression) and trove classifier names (License :: OSI Approved :: ...)
    *("MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0"),
    *("MIT License", "BSD License", "Apache Software License"),
    "Python Software Foundation License",
}


def within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def imported_modules(source: str, module: str, *, is_package: bool = False) -> set[str]:
    """Every module an import statement could load, with relative imports resolved."""
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.rsplit(".", node.level - 1)[0]
                base = f"{anchor}.{base}" if base else anchor
            found.add(base)
            # `from pkg import name` may load the submodule pkg.name
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def src_imports() -> Iterator[tuple[str, set[str]]]:
    for path in sorted(SRC.rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if is_package else parts)
        source = path.read_text(encoding="utf-8")
        yield module, imported_modules(source, module, is_package=is_package)


def forbidden_for(module: str) -> tuple[str, ...]:
    if module in ASSEMBLY:
        return ()
    return tuple(p for p in FEATURE_PACKAGES if not within(module, p))


@pytest.mark.parametrize(
    ("module", "source", "expected"),
    [
        ("rigtape.device", "from .faults import engine", "rigtape.faults.engine"),
        ("rigtape.device", "from . import recording", "rigtape.recording"),
        ("rigtape.recording.replay", "from ..faults.spec import Rule", "rigtape.faults.spec"),
        ("rigtape.recording.replay", "from .format import read", "rigtape.recording.format"),
        ("rigtape.server", "import rigtape.faults", "rigtape.faults"),
        ("rigtape.server", "if TYPE_CHECKING:\n    from rigtape import faults", "rigtape.faults"),
    ],
)
def test_resolves_relative_imports(module: str, source: str, expected: str) -> None:
    assert expected in imported_modules(source, module)


def test_import_boundaries() -> None:
    modules = dict(src_imports())
    assert {"rigtape", "rigtape.cli", "rigtape.modbus", "rigtape.config"} <= modules.keys()
    violations = [
        f"{module} imports {name}"
        for module, names in modules.items()
        for name in sorted(names)
        if any(within(name, banned) for banned in forbidden_for(module))
    ]
    assert violations == []


def test_src_does_not_import_pymodbus() -> None:
    offenders = [m for m, names in src_imports() if any(within(n, "pymodbus") for n in names)]
    assert offenders == []


def runtime_distributions() -> set[str]:
    """Installed runtime closure; markers are evaluated for this platform and Python only."""
    seen: set[str] = set()
    todo = ["rigtape"]
    while todo:
        name = canonicalize_name(todo.pop())
        if name in seen:
            continue
        seen.add(name)
        for spec in requires(name) or []:
            req = Requirement(spec)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return seen - {"rigtape"}


def licenses(dist: str) -> list[str]:
    meta = metadata(dist)
    if expression := meta.get("License-Expression"):
        return [t for t in re.findall(r"[\w.+-]+", expression) if t not in {"AND", "OR"}]
    classifiers = [
        c.rpartition(" :: ")[2] for c in meta.get_all("Classifier") or [] if c.startswith("License")
    ]
    return classifiers or [meta.get("License") or "UNKNOWN"]


def test_runtime_dependency_licenses() -> None:
    dists = runtime_distributions()
    assert {"pydantic", "pyyaml"} <= dists
    rejected = {d: licenses(d) for d in sorted(dists) if not set(licenses(d)) <= ALLOWED_LICENSES}
    assert rejected == {}, "runtime licenses outside the allow-list; discuss in an issue first"


EXAMPLES = SRC.parent / "examples"


def example_devices() -> list[Path]:
    """Every YAML file under examples/ that declares the device format, whatever its name."""
    found = []
    for path in sorted(EXAMPLES.rglob("*.y*ml")):
        data = load_yaml(path)
        if isinstance(data, dict) and data.get("format") == "rigtape.device/v1":
            found.append(path)
    return found


def test_examples_are_found() -> None:
    assert example_devices(), "no example device files found; did the glob or the format change?"


@pytest.mark.parametrize("path", example_devices(), ids=str)
def test_example_devices_state_their_source(path: Path) -> None:
    assert (load_device(path).source or "").strip(), f"{path}: add a source line"


def example_recordings() -> list[Path]:
    return [
        p
        for p in sorted(EXAMPLES.rglob("*.jsonl"))
        if '"format":"rigtape.recording"' in p.read_text()[:40]
    ]


def test_example_recordings_exist_and_state_their_source() -> None:
    assert example_recordings(), "no example recording found under examples/"
    for path in example_recordings():
        header = read_recording(path).header
        assert (header.get("source") or "").strip(), f"{path}: record it with --source"
        assert path.read_text(encoding="utf-8").endswith("\n")
