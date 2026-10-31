"""Exit codes and the user-facing error format: one 'error:' line and an optional 'hint:' line."""

from __future__ import annotations

import sys
from enum import IntEnum
from typing import TextIO


class ExitCode(IntEnum):
    OK = 0
    RUNTIME_ERROR = 1
    USAGE_ERROR = 2  # bad arguments or configuration; matches argparse
    INTERRUPTED = 130  # Ctrl+C


class RigtapeError(Exception):
    exit_code: ExitCode = ExitCode.RUNTIME_ERROR

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class UsageError(RigtapeError):
    exit_code = ExitCode.USAGE_ERROR


class ConfigError(RigtapeError):
    exit_code = ExitCode.USAGE_ERROR


def report(error: RigtapeError, stream: TextIO | None = None) -> ExitCode:
    """Print the error to stderr (or `stream`) and return the exit code to use."""
    out = stream or sys.stderr
    print(f"error: {error}", file=out)
    if error.hint:
        print(f"hint: {error.hint}", file=out)
    return error.exit_code
