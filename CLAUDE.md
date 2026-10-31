# rigtape

A headless Modbus TCP device simulator for testing control software without the hardware.
It serves a device from a register map, records the traffic between a client and a real device,
replays that recording with no device attached, and injects faults on purpose: delays, no
response, Modbus exception responses, disconnects. One Docker image runs every command, so it
works the same on a laptop and in CI.

Milestone 1 (Modbus TCP serve, record, replay, basic faults, Docker, README quick start) is done.

## Next milestones (do not build yet; add ideas to this list instead of widening a change)

- M2: SCPI over TCP (raw socket, port 5025): command/response tables, `*IDN?`, an error queue
  (`SYST:ERR?`). Reference devices: DC power supply, DMM, electronic load.
- M3: Python client and pytest fixtures, another Modbus reference device (energy meter), CI test
  reports.
- Later: Modbus RTU (serial), VXI-11 and HiSLIP, an MCP server for AI agents.
- serve, deferred from M1: coils and discrete inputs (FC 1/2/5/15), FC 22/23/43, 32-bit, float,
  word order, scaling and value-range checks (03), unit id filtering and gateway behaviour (0x0B),
  first-order lag and noise behaviors, connection limits and idle timeouts, request logging
  (`-v`), block notation for register ranges, rejecting YAML 1.1 octal (`0100`) and sexagesimal
  (`1:30`) numbers, one port for every address when `--host` resolves to several with `--listen 0`.
- record, deferred: faults while recording (chaos proxy in front of a real device), late events,
  decoded fields, pcap export, masking sensitive values, live echo of exchanges (`-v`), a flag for
  the 30 s pending window, idle timeouts.
- replay, deferred: accepting unrecorded writes (`--accept-unrecorded-writes`), a per-address
  state model, `rigtape profile` (a device.yaml from a recording), out-of-order write matching and an ignore list for values that
  change every time, time-based replay and replaying recorded delays and silences, `--strict`, a
  cursor per connection.
- faults, deferred: RST disconnects, jitter, dropping only the response, late answers with old
  transaction ids, split frames and garbage bytes, targeting one connection, fault combinations
  and timelines, device restarts, scenario packs.
- API, deferred: `GET /status`, `POST /reset` (with the M3 pytest fixture), creating rules from
  JSON (PUT), token authentication, reading and writing registers, OpenAPI and JSON Schema.
- Operations, deferred: Python 3.15, macOS and Windows CI, multi-architecture images, automatic
  digest updates, publishing the image to a registry, JSON logs, HEALTHCHECK, a SIGTERM lost
  during the first ~0.4 s as PID 1 (tini or `docker run --init`).

## Module boundaries

- `recording/` and `faults/` import only the core modules (`modbus`, `server`, `control`,
  `config`, `errors`); the core never imports them, and they never import each other. `cli.py` is
  the only place that wires modules together. `tests/test_rules.py` checks this with the AST.
- The seams are `Handler` (serve and replay), `Interceptor` (faults) and the control API's route
  table. New handlers, fault types and routes plug in through them; keep them small and stable.
- No module-level global state, so several instances can run in one process later.

## Technical choices

Change this section when a choice changes.

- Python 3.11 or newer, asyncio. uv for packaging (the version is pinned identically in
  `ci.yml` and the `Dockerfile`), `pyproject.toml`, `src/` layout, `uv.lock`.
- Modbus TCP framing (MBAP and PDU) is hand-written on asyncio: fault injection needs byte- and
  timing-level control.
- pymodbus (BSD-3-Clause) is a dev-only dependency (`>=3.15,<3.16`, no extras: `[development]`
  pulls in AGPL zuban). It plays the "real device" and the reference client in tests and is never
  imported from `src/`.
- Runtime dependencies: pydantic (configuration validation), PyYAML (configuration files), h11
  (control API). Keep the list short and allow only permissive licenses (MIT, BSD,
  Apache-2.0, PSF); ask before adding anything GPL or AGPL. `tests/test_rules.py` checks the
  runtime closure.
- Dev dependencies: pytest, pytest-asyncio, pytest-timeout, ruff (lint and format), mypy (strict
  on `src`, `python_version` 3.11), types-PyYAML, pymodbus.
- Docker base image `python:3.14-slim-trixie` pinned by digest; the process runs as uid 999.
- The example port is 5020 (502 needs root on Linux); in Docker map it with `-p 502:5020`.

## Commands

Update this section whenever a build, test or run command appears or changes.

- Install: `uv sync --locked`
- Full check (required before a PR): `uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
- Auto-fix: `uv run ruff check --fix . && uv run ruff format .`
- Python 3.11 tests (the project venv stays on 3.14): `uv run --isolated --locked --python 3.11 pytest`
- Demo client on Python 3.9: `RIGTAPE_CLIENT_PYTHON=/usr/bin/python3 uv run pytest tests/test_example_client.py`
- Run: `uv run rigtape serve examples/pid/device.yaml` (default port 5020; `--listen 0` picks a free one)
- Record: `uv run rigtape record --upstream 127.0.0.1:5020 --listen 5021 -o session.jsonl --source "where it came from"` (never overwrites)
- Replay: `uv run rigtape replay examples/pid/session.jsonl` (unrecorded writes and requests get exception 04; check `misses` and `unrecorded_writes` in the stop summary)
- Faults: `uv run rigtape serve examples/pid/device.yaml --faults examples/pid/faults.yaml` (also for replay; `--seed`, `--api-port`, `--no-api`)
- Control API: `curl localhost:18502/faults`, `curl -X POST localhost:18502/faults/hang/enable` (and `disable`)
- Demo client (standard library, Python 3.9+): `python3 examples/pid/client.py --port 5020 --sp 60 --reads 10`
- Docker: `docker build -t rigtape:dev . && RIGTAPE_DOCKER_IMAGE=rigtape:dev uv run pytest tests/docker`
- Run in Docker (the image's default command serves the PID example): `docker run --rm -p 127.0.0.1:5020:5020 rigtape:dev`
- Add a dependency: `uv add <pkg>` (runtime) or `uv add --dev <pkg>`.

## How to work

- Code, comments, commit messages, documentation and CLI output are in English.
- One change per pull request, small enough to review in about 30 minutes (product code up to
  roughly 350 lines). Give the pros and cons when proposing or changing something.
- Every change ships with tests that run without real hardware. Run the full check before
  finishing and summarise the results.
- Record design decisions in `docs/decisions.md`: one line each with date, decision and reason.
- Example device definitions and recordings state their source. Use only public manuals and
  devices you own; never add proprietary documents, device definitions or captured traffic.
- Never commit secrets (tokens, keys). Releases (PyPI packages, container images) are made by the
  maintainer; a change never publishes anything on its own.
