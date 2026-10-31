# rigtape

[![CI](https://github.com/jameskwon07/rigtape/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jameskwon07/rigtape/actions/workflows/ci.yml)

**Reproduce Modbus field bugs at your desk,
so your customer gets a tested fix, not a debug build.**

> **Status: early development.**
> Works today: `rigtape serve` simulates a Modbus TCP device from a register map,
> `rigtape record` records the traffic between your software and a real device,
> `rigtape replay` answers from that recording with no device attached, and `--faults` makes
> either one misbehave on purpose: delays, no response, exception responses, disconnects, with a
> seed so CI sees the same faults every run, and a small HTTP API to switch them on and off.
> Not yet: device restarts, split or garbage frames, RST, Modbus RTU, SCPI.

rigtape is a headless Modbus TCP device simulator for testing control software without the
hardware. It ships as one Docker image, so it runs the same on your laptop and in CI.

## Quick start: a virtual PID temperature controller

You need Docker, git, python3 (3.9 or later) and curl. The demo client uses only the standard library.

```bash
git clone https://github.com/jameskwon07/rigtape.git && cd rigtape
docker build -t rigtape .
docker network create rigtape
docker run --rm -d --name pid --network rigtape -p 127.0.0.1:5020:5020 rigtape   # serves examples/pid/device.yaml
python3 examples/pid/client.py --port 5020 --sp 60 --reads 10                     # writes SP = 60.0 C, PV climbs
```

```text
wrote SP 60.0 C
SP 60.0 C  PV 20.0 C
SP 60.0 C  PV 21.0 C
SP 60.0 C  PV 22.0 C
...
```

The device is [`examples/pid/device.yaml`](examples/pid/device.yaml): the setpoint SP is holding
register 0, the process value PV is input register 0, both in 0.1 C per count. PV moves toward SP
at 1.0 C per second and stops there.

Now record a session. The recorder sits between the client and the device (here the simulated
one; with your own hardware, `--upstream` is its address):

```bash
rm -rf rec && mkdir rec
docker run --rm -d --name rec --network rigtape -p 127.0.0.1:5021:5021 \
  --user "$(id -u):$(id -g)" -v "$PWD/rec:/data" rigtape \
  record --upstream pid:5020 --listen 5021 -o /data/pid.jsonl --source "README walkthrough"
python3 examples/pid/client.py --port 5021 --sp 45 --reads 10
docker stop rec pid
```

Replay it with no device running. The client prints exactly what it printed during the recording:

```bash
docker run --rm -d --name rep -p 127.0.0.1:5020:5020 \
  --user "$(id -u):$(id -g)" -v "$PWD/rec:/data" rigtape replay /data/pid.jsonl
python3 examples/pid/client.py --port 5020 --sp 45 --reads 10                     # same output, no device
docker stop rep && docker network rm rigtape
```

No time to record? [`examples/pid/session.jsonl`](examples/pid/session.jsonl) is a recorded run
of `client.py --sp 60 --reads 10`:
`docker run --rm -p 127.0.0.1:5020:5020 rigtape replay examples/pid/session.jsonl`.

Now break it on purpose. [`examples/pid/faults.yaml`](examples/pid/faults.yaml) slows a PV read
by 1.5 s with probability 0.2 (seed `ci-42` makes that reads 1 and 3, every time), answers the
third setpoint write with exception 06 (device busy), and defines two more faults that start
switched off:

```bash
docker run --rm -d --name pid -p 127.0.0.1:5020:5020 -p 127.0.0.1:18502:18502 \
  rigtape serve examples/pid/device.yaml --faults examples/pid/faults.yaml
python3 examples/pid/client.py --port 5020 --sp 60 --reads 3     # reads 1 and 3 are 1.5 s slow (seed ci-42)
python3 examples/pid/client.py --port 5020 --sp 61 --reads 0     # fine
python3 examples/pid/client.py --port 5020 --sp 62 --reads 0     # write SP: exception 6 (SERVER DEVICE BUSY)
curl -X POST localhost:18502/faults/hang/enable
python3 examples/pid/client.py --port 5020 --reads 1             # timeout
curl -X POST localhost:18502/faults/hang/disable
python3 examples/pid/client.py --port 5020 --reads 1             # back to normal
docker stop pid
```

The same `--faults` file works with `replay`, so a CI job can replay a real device *and* make it
misbehave at a chosen request.

## Define a device

```yaml
format: rigtape.device/v1
name: Virtual PID temperature controller
source: Where this register map comes from (a public manual, your own device, ...).
holding_registers:              # FC3 read, FC6/FC16 write
  - {name: sp, address: 0, value: 200}
  - {name: model_id, address: 10, value: 1001, readonly: true}
input_registers:                # FC4 read
  - {name: pv, address: 0, value: 200}
behaviors:
  - {type: ramp, register: pv, toward: sp, rate_per_s: 10}
```

- Addresses are 0-based PDU addresses (0-65535). Values are raw 16-bit integers (0-65535);
  scaling and units belong in comments. Write numbers without leading zeros or colons: YAML reads
  `0100` as octal 64 and `1:30` as 90. Use `0x` for hex.
- Holding registers are writable unless `readonly: true`. Input registers are read-only.
- `ramp` moves `register` toward the current value of `toward` by `rate_per_s` counts per second
  (reads give the nearest whole count) and never overshoots. Writing a new value to `toward`
  turns the ramp around from where it is; rewriting the same value changes nothing. The ramped
  register must be read-only.
- Function codes 3, 4, 6 and 16 are supported. Others get exception 01 (illegal function).
  Reading an undefined address or writing a read-only one gets exception 02 (illegal data
  address), and a rejected FC16 write changes nothing.
- Any unit id is accepted and echoed back.
- Unknown keys, wrong types and duplicate addresses are errors, reported on one line with the field
  path, for example
  `error: device.yaml: holding_registers[1].address: address 0 is already used by holding_registers[0]`.
  YAML problems such as duplicate keys are reported with `line:column` instead.

## Record a real device

```text
rigtape record --upstream HOST:PORT -o FILE [--source TEXT] [--listen PORT] [--host HOST]
```

Point your software at the recorder instead of the device. Every request is forwarded as is
and every answer comes back as is; the recorder adds no timeouts and never answers for the device.
Any function code and any unit id pass through, because the recorder only looks at the framing.

The file is JSON Lines: a header, then one line per event, flushed as it happens:

```jsonl
{"format":"rigtape.recording","version":1,"protocol":"modbus-tcp","started_at":"2026-10-13T13:12:37.309Z","tool":"rigtape 0.1.0","upstream":"pid:5020","listen":"0.0.0.0:5021","source":"README walkthrough"}
{"t":1.422971,"ev":"open","conn":1,"peer":"172.18.0.1:52944"}
{"t":1.423375,"ev":"x","conn":1,"tid":1,"uid":1,"req":"0600000258","rsp":"0600000258","lat":0.000257}
{"t":2.431474,"ev":"x","conn":1,"tid":5,"uid":1,"req":"0400000001","rsp":"040200d2","lat":0.000152}
{"t":10.4,"ev":"x","conn":1,"tid":21,"uid":1,"req":"0400000001","rsp":null,"err":"closed","lat":null}
{"t":10.428682,"ev":"close","conn":1,"by":"client"}
{"t":10.459406,"ev":"stop","reason":"signal"}
```

- `req` and `rsp` are the Modbus PDUs (function code and data) in hex, exactly as they went over
  the wire. An exception response starts with the function code plus 0x80, for example `8602`.
- `t` is seconds since the start, taken when the request arrived; `lat` is how long the device
  took to answer.
- `rsp: null` means the device never answered: `err` is `timeout` after 30 s, or `closed` when
  the connection went away first. These lines are skipped by replay.
- `-o` is never overwritten: an existing file makes the recorder exit at once with code 2 (under
  `docker run --rm -d` that also removes the container, so run it without `-d` once if the client
  reports `connection lost`). Stop the recorder with Ctrl+C or `docker stop`; the last line is
  `stop`, and a summary of exchanges and unanswered requests goes to stderr.
- `--source` says where the device came from; it is stored in the header next to `upstream` and
  `listen`, the address the recorder was bound to.

## Replay a recording

```text
rigtape replay FILE [--listen PORT] [--host HOST]
```

Replay answers each request with the recorded response bytes; only the transaction and unit ids
are taken from the live request. Reads of the same register are answered in recorded order, so a
trend (PV climbing) plays back as it was recorded, and the last response repeats once the recording
runs out. Writes drive the playback:

1. Writing the same values in the same order as the recording reproduces it exactly, including a
   recorded exception (a busy device) before the retry that succeeded. Polling more often than the
   recording did before a write still returns pre-write values, never later ones.
2. A write that is not in the recording (other value, other order, or a function code the device
   never saw) gets exception 04, a warning that names the next recorded write, and a count.
   Nothing changes.
3. A request that never appears in the recording gets exception 04 and a warning that names the
   closest recorded request.

At stop, replay prints `replayed N requests: misses=M unrecorded_writes=W` to stderr. In CI,
check that both counts are 0. Physics such as PV following a new SP is `serve`'s job, not replay's.
If the recording has one unit id, any unit id is answered; with several, unknown ones get
exception 0B.

## Inject faults

```text
rigtape serve  DEVICE_YAML     --faults FAULTS_YAML [--seed SEED] [--api-port PORT] [--no-api]
rigtape replay RECORDING_JSONL --faults FAULTS_YAML [--seed SEED] [--api-port PORT] [--no-api]
```

```yaml
format: rigtape.faults/v1
seed: ci-42                     # --seed overrides; if neither is set, one is generated and printed
rules:                          # checked top to bottom; the first rule that fires wins
  - name: slow-pv
    type: delay
    match: {function_codes: [4], address: [0, 0]}
    probability: 0.2            # decided per matching request
    delay_ms: 1500
  - name: busy-on-third-sp-write
    type: exception
    match: {function_codes: [6, 16], address: [0, 0]}
    after: 2                    # let the first 2 matching writes through
    count: 1                    # then fire exactly once
    code: 6                     # SERVER DEVICE BUSY
  - name: hang
    type: no_response           # not executed, no reply, connection stays open
    enabled: false              # off at start: POST /faults/hang/enable
  - name: cable-pull
    type: disconnect            # not executed, connection closed with FIN
    enabled: false
    count: 1
```

| type | What the client sees | Is the request executed? |
|---|---|---|
| `delay` | The normal answer, `delay_ms` later. Later requests on that connection wait too, as on a real device. A delay longer than the client's timeout looks like a late answer with an old transaction id. | Yes, before the delay |
| `no_response` | Nothing; the connection stays open until the client gives up | No |
| `exception` | A Modbus exception response with `code` (1-255) | No |
| `disconnect` | The connection closed (FIN) | No |

- `match` narrows a rule to `unit_ids`, `function_codes` and an `address` range `[from, to]`
  that overlaps the request's registers. Address rules understand function codes 1-6, 15 and 16
  (coils and registers alike); other function codes never match an address rule. An omitted
  field matches everything.
- Every enabled rule that matches counts the request. The first `after` matches pass; each later match is a
  draw with `probability`; `count` caps the number of fires. A rule that fires while an earlier
  rule already won still spends its count, so adding or removing rules never shifts the others.
- The draw depends only on the seed, the rule name and the match number. Same seed, same faults,
  on every machine and Python version. `--seed` beats the file's `seed`; with neither, a seed is
  generated and printed on the READY line and in the log so a run can be repeated.
- Counters are per process and shared by all connections, so with several clients the order of
  arrival decides which one gets the fault. Give each client its own rule via `unit_ids`.
- Rules switched on and off while running keep their counters; `GET /faults` shows them.

The control API runs beside the Modbus port whenever `--faults` is given (default port 18502,
`--api-port 0` for a free one, `--no-api` to skip it). It has no authentication: it binds the
same address as the Modbus port (`--host`, so `--host 0.0.0.0` exposes it and a warning is
logged), and in Docker publish it as `-p 127.0.0.1:18502:18502` only.

| Request | Answer |
|---|---|
| `GET /faults` | `{"seed": "ci-42", "rules": [{"name": "slow-pv", "type": "delay", "enabled": true, "matched": 0, "fired": 0, "remaining": null}, ...]}` |
| `POST /faults/{name}/enable` | `{"name": "hang", "enabled": true}`; applies from the next Modbus request |
| `POST /faults/{name}/disable` | `{"name": "hang", "enabled": false}` |

Errors are JSON too: `{"error": "no rule named 'x'", "hint": "rules: slow-pv, ..."}` with 404,
405 for a wrong method, 413 for a body over 64 KiB (no route takes one), 400 for non-HTTP input,
408 when no complete request arrives within 5 s.

## Command line

- `--listen` defaults to 5020; `0` picks a free port. Port 502 needs root on Linux, so map it
  instead: `docker run -p 502:5020 ...`. With `--listen 0`, give `--host` an IP address: a name
  such as `localhost` can resolve to IPv4 and IPv6, and each would get its own port.
- `--host` defaults to `$RIGTAPE_HOST`, else `127.0.0.1`. The image sets `RIGTAPE_HOST=0.0.0.0`.
- Once listening, every command prints one JSON line on stdout, for scripts to wait on:
  `{"event": "ready", "modbus": {"host": "127.0.0.1", "port": 5020}, "mode": "serve"}`
  (`record` adds `upstream` and `file`, `replay` adds `recording`, `--faults` adds `seed`, and
  `api` unless `--no-api`). Logs and errors go to stderr; every fault that fires is logged with
  its rule name.
- Exit codes: 0 after SIGTERM, 1 runtime error, 2 bad arguments or input file (or the port is
  taken), 130 after Ctrl+C.

Without Docker, from a clone with [uv](https://docs.astral.sh/uv/):
`uv run rigtape serve examples/pid/device.yaml`.

## Connect your own software

Point your Modbus TCP client at the published port. To use the standard port, publish it as
`docker run --rm -p 502:5020 -v "$PWD/my-device.yaml:/app/device.yaml:ro" rigtape serve device.yaml`.

- `-p 502:5020` listens on every interface of your machine, not just localhost.
- The container runs as uid 999, so a mounted file must be readable by others
  (`chmod a+r my-device.yaml`), and a directory for recordings must be writable by the container
  user: pass `--user "$(id -u):$(id -g)"` as in the quick start.
- Rootless Docker and Podman may refuse ports below 1024. They also map your own user to root
  inside the container, so there run `record` and `replay` without `--user` (Podman:
  `--userns=keep-id`); with `--user` the recorder gets `Permission denied`.

## Contributing

Bug reports and device requests are welcome in
[issues](https://github.com/jameskwon07/rigtape/issues). Read [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a pull request, and [SECURITY.md](SECURITY.md) to report a vulnerability privately.

## License

[Apache-2.0](LICENSE)
