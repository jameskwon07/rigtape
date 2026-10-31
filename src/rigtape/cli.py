"""Command line entry point. The only place that wires modules together."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import os
import secrets
import signal
import socket
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any, NoReturn, Protocol

from rigtape.control import ControlServer
from rigtape.device import Device, load_device
from rigtape.errors import ConfigError, ExitCode, RigtapeError, UsageError, report
from rigtape.faults.engine import FaultEngine
from rigtape.faults.routes import routes as fault_routes
from rigtape.faults.spec import load_scenario
from rigtape.recording.format import FORMAT, RecordingWriter, read_recording
from rigtape.recording.proxy import RecordingProxy
from rigtape.recording.replay import Replayer
from rigtape.server import ModbusServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5020  # 502 needs root on Linux; map it with docker -p 502:5020
DEFAULT_API_PORT = 18502  # unassigned by IANA, outside ephemeral ranges, easy to remember

log = logging.getLogger("rigtape")
_LISTEN_HINTS = {
    errno.EADDRINUSE: "another process uses this port; pick another with {flag} (0 = any)",
    errno.EACCES: "ports below 1024 need root; use {flag} 5020 and map it with -p 502:5020",
}
_NOT_LOCAL_HINT = "use an address of this machine: 127.0.0.1, or 0.0.0.0 for every interface"
_DOCKER_USER_HINT = (
    'in Docker, add --user "$(id -u):$(id -g)" so the container can write to the mounted directory'
    " (rootless Docker and Podman: run without --user instead)"
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # Raise instead of exiting so main() owns the output format and the exit code.
        raise UsageError(message, hint=f"run '{self.prog} --help' for usage")


class _Listener(Protocol):
    async def start(self) -> tuple[str, int]: ...
    async def close(self) -> None: ...


def _port(text: str) -> int:
    if not text.isdigit() or int(text) > 0xFFFF:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}; use 0-65535 (0 = any free port)")
    return int(text)


def _endpoint(text: str) -> tuple[str, int]:
    host, colon, port = text.rpartition(":")
    bare_v6 = ":" in host and not (host.startswith("[") and host.endswith("]"))  # ::1 needs []
    if not colon or not host or bare_v6 or not port.isdigit() or not 1 <= int(port) <= 0xFFFF:
        raise argparse.ArgumentTypeError(f"invalid address {text!r}; use HOST:PORT or [IPv6]:PORT")
    return host.strip("[]"), int(port)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="rigtape",
        description="Simulate Modbus TCP devices for testing control software.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"rigtape {version('rigtape')}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    serve = _command(
        commands, "serve", "Simulate a Modbus TCP device from a device.yaml map.", True
    )
    serve.add_argument("device", type=Path, metavar="DEVICE_YAML")

    record = _command(commands, "record", "Proxy a real device and record every exchange.")
    record.add_argument("--upstream", required=True, type=_endpoint, metavar="HOST:PORT")
    record.add_argument("-o", "--output", required=True, type=Path, metavar="FILE")
    record.add_argument("--source", help="where the device comes from; kept in the file header")

    replay = _command(
        commands, "replay", "Answer requests from a recording, no device attached.", True
    )
    replay.add_argument("recording", type=Path, metavar="RECORDING_JSONL")
    return parser


def _command(
    commands: Any, name: str, description: str, faults: bool = False
) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = commands.add_parser(
        name,
        help=description[0].lower() + description[1:-1],
        description=description,
        allow_abbrev=False,
    )
    if faults:
        parser.add_argument("--faults", type=Path, metavar="FAULTS_YAML", help="fault scenario")
        parser.add_argument("--seed", help="seed for probabilistic faults (overrides the file)")
        parser.add_argument(
            "--api-port",
            type=_port,
            default=DEFAULT_API_PORT,
            metavar="PORT",
            help=f"control API port (default {DEFAULT_API_PORT}; 0 picks a free port)",
        )
        parser.add_argument("--no-api", action="store_true", help="do not start the control API")
    parser.add_argument(
        "--listen",
        type=_port,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"Modbus TCP port (default {DEFAULT_PORT}; 0 picks a free port)",
    )
    binds = "address to bind, Modbus and control API alike" if faults else "address to bind"
    parser.add_argument(
        "--host",
        help=f"{binds} (default: $RIGTAPE_HOST, else {DEFAULT_HOST}; use an IP, not a name, "
        "with --listen 0)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            parser.print_help()
            return ExitCode.OK
        logging.basicConfig(level=logging.INFO, format="rigtape: %(message)s", stream=sys.stderr)
        args.host = args.host or os.environ.get("RIGTAPE_HOST") or DEFAULT_HOST
        return {"serve": _serve, "record": _record, "replay": _replay}[args.command](args)
    except SystemExit as exc:  # --help and --version exit through argparse
        return exc.code if isinstance(exc.code, int) else ExitCode.OK
    except RigtapeError as exc:
        return report(exc)
    except KeyboardInterrupt:  # Ctrl+C where signal handlers are unavailable (Windows)
        return ExitCode.INTERRUPTED


def _serve(args: argparse.Namespace) -> int:
    try:
        spec = load_device(args.device)
    except ConfigError as exc:
        if _is_recording(args.device):
            message = f"{args.device} is a recording, not a device map"
            raise ConfigError(message, hint="use 'rigtape replay'") from exc
        raise
    engine, api = _faults(args)
    server = ModbusServer(Device(spec), host=args.host, port=args.listen, interceptor=engine)
    log.info("serving %s from %s", spec.name or "device", args.device)
    return asyncio.run(_run(server, args, api=api, mode="serve"))


def _record(args: argparse.Namespace) -> int:
    host, port = args.upstream
    upstream = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    try:
        writer = RecordingWriter(args.output, upstream=upstream, source=args.source)
    except FileExistsError:
        hint = "recordings are never overwritten; pick another -o path"
        raise UsageError(f"{args.output} already exists", hint=hint) from None
    except OSError as exc:
        denied = exc.errno in (errno.EACCES, errno.EPERM)
        message = f"cannot write {args.output}: {exc.strerror or exc}"
        raise UsageError(message, hint=_DOCKER_USER_HINT if denied else None) from exc
    proxy = RecordingProxy((host, port), writer, host=args.host, port=args.listen)
    log.info("recording exchanges with %s to %s", upstream, args.output)
    try:
        code = asyncio.run(
            _run(proxy, args, mode="record", upstream=upstream, file=str(args.output))
        )
    except UsageError:
        writer.close()
        args.output.unlink()  # nothing was recorded; leave no header-only file behind
        raise
    writer.event("stop", reason="signal")
    writer.close()
    log.info(
        "recorded %d exchanges (%d unanswered) to %s",
        proxy.exchanges,
        proxy.unanswered,
        args.output,
    )
    return code


def _replay(args: argparse.Namespace) -> int:
    replayer = Replayer(read_recording(args.recording))
    engine, api = _faults(args)
    server = ModbusServer(replayer, host=args.host, port=args.listen, interceptor=engine)
    log.info("replaying %s", args.recording)
    ready = {"mode": "replay", "recording": str(args.recording)}
    code = asyncio.run(_run(server, args, api=api, **ready))
    log.info(
        "replayed %d requests: misses=%d unrecorded_writes=%d",
        replayer.requests, replayer.misses, replayer.unrecorded_writes,
    )  # fmt: skip
    return code


def _faults(args: argparse.Namespace) -> tuple[FaultEngine | None, ControlServer | None]:
    """The fault engine and its control API when --faults is given; the API needs rules."""
    if args.faults is None:
        return None, None
    scenario = load_scenario(args.faults)
    seed = args.seed if args.seed is not None else scenario.seed
    if seed is None:
        seed = secrets.token_hex(4)  # printed on the READY line, so a run can be repeated
    engine = FaultEngine(scenario, seed)
    args.seed = engine.seed
    log.info("faults: %d rules from %s, seed %s", len(scenario.rules), args.faults, engine.seed)
    api = (
        None
        if args.no_api
        else ControlServer(fault_routes(engine), host=args.host, port=args.api_port)
    )
    return engine, api


def _is_recording(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as file:
            return f'"format":"{FORMAT}"' in file.readline()
    except (OSError, UnicodeDecodeError):
        return False


async def _run(
    listener: _Listener,
    args: argparse.Namespace,
    *,
    api: ControlServer | None = None,
    **ready: object,
) -> int:
    """Run until SIGTERM (exit 0) or SIGINT (exit 130), then close every connection."""
    loop = asyncio.get_running_loop()
    stopped: asyncio.Future[int] = loop.create_future()
    handled: list[signal.Signals] = []
    for sig, exit_code in ((signal.SIGTERM, ExitCode.OK), (signal.SIGINT, ExitCode.INTERRUPTED)):
        try:
            loop.add_signal_handler(sig, _stop, stopped, exit_code)
            handled.append(sig)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    try:
        try:
            bound_host, bound_port = await listener.start()
        except OSError as exc:
            raise _listen_error(exc, args.host, args.listen) from exc
        except UnicodeError as exc:  # IDNA rejects malformed names such as "127.0.0..1"
            bad = socket.gaierror(f"invalid host name ({exc})")
            raise _listen_error(bad, args.host, args.listen) from exc
        if api is not None:
            try:
                api_host, api_port = await api.start()
            except OSError as exc:
                raise _listen_error(exc, args.host, args.api_port, "--api-port") from exc
            ready["api"] = {"host": api_host, "port": api_port}
            log.info("control API on %s:%d", api_host, api_port)
        if getattr(args, "faults", None) is not None:
            ready["seed"] = args.seed
        _ready(modbus={"host": bound_host, "port": bound_port}, **ready)
        log.info("listening on %s:%d; stop with Ctrl+C", bound_host, bound_port)
        return await stopped
    finally:
        for sig in handled:
            loop.remove_signal_handler(sig)
        if api is not None:
            await api.close()
        await listener.close()


def _stop(stopped: asyncio.Future[int], code: int) -> None:
    if not stopped.done():
        stopped.set_result(code)


def _ready(**fields: object) -> None:
    """One JSON line on stdout once listening, for scripts and tests to wait on."""
    print(json.dumps({"event": "ready", **fields}), flush=True)


def _listen_error(exc: OSError, host: str, port: int, flag: str = "--listen") -> UsageError:
    # asyncio rewords strerror, so rebuild it from errno; gaierror numbers are EAI_* codes
    reason, hint = os.strerror(exc.errno or 0), _LISTEN_HINTS.get(exc.errno or 0)
    if hint:
        hint = hint.format(flag=flag) + (" or --no-api" if flag == "--api-port" else "")
    if isinstance(exc, socket.gaierror) or not exc.errno:  # unknown host, or not a local address
        reason, hint = exc.strerror or str(exc), _NOT_LOCAL_HINT
    return UsageError(f"cannot listen on {host}:{port}: {reason}", hint=hint)
