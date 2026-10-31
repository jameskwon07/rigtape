# Contributing to rigtape

The most useful help right now is to run the README quick start against your own Modbus software
and open an issue about what broke or what was missing.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the scope. Planned
  and deferred work is listed under "Next milestones" in [CLAUDE.md](CLAUDE.md), which is also the
  project guide for coding agents.
- Example device definitions and recordings must state their `source`. Use only public manuals
  and devices you own. Never contribute proprietary documents, device definitions or captured
  traffic.

## Development setup

You need [uv](https://docs.astral.sh/uv/) (CI pins 0.13.0), and Docker for the image tests.

    uv sync --locked

## Pull requests

- One change per pull request, small enough to review in about 30 minutes (roughly 350 lines of
  product code).
- Every change ships with tests that run without real hardware. pymodbus plays the "real device"
  in tests and is never imported from `src/`.
- Run the full check before you open the pull request:

      uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest

  and, if you touched the image or the CLI, the Docker smoke tests:

      docker build -t rigtape:dev . && RIGTAPE_DOCKER_IMAGE=rigtape:dev uv run pytest tests/docker

- Record design decisions in [docs/decisions.md](docs/decisions.md): one line with the date, the
  decision and the reason.
- New runtime dependencies must have a permissive license (MIT, BSD, Apache-2.0, PSF);
  `tests/test_rules.py` checks this. Open an issue before proposing anything GPL or AGPL.
- Code, comments, commit messages, documentation and CLI output are in English.

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), the license of this project.
