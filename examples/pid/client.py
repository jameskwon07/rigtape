#!/usr/bin/env python3
"""Demo client for examples/pid/device.yaml: write a setpoint, then watch PV follow it.

Standard library only, Python 3.9+. Output has no timestamps, so a run against a replayed
recording prints exactly what the recorded run printed.

Registers (0.1 degC per count): holding register 0 = SP (setpoint), input register 0 = PV.

    python3 examples/pid/client.py --port 5020 --sp 60 --reads 10
"""

import argparse
import socket
import struct
import sys
import time

TIMEOUT_S = 3.0
RETRY_S = 10.0  # docker-proxy accepts connections before the app inside is listening
EXCEPTIONS = {
    1: "ILLEGAL FUNCTION",
    2: "ILLEGAL DATA ADDRESS",
    3: "ILLEGAL DATA VALUE",
    4: "SERVER DEVICE FAILURE",
    6: "SERVER DEVICE BUSY",
    11: "GATEWAY TARGET DEVICE FAILED TO RESPOND",
}


class ModbusException(Exception):
    def __init__(self, code):
        super().__init__(f"exception {code} ({EXCEPTIONS.get(code, 'UNKNOWN')})")


class Client:
    def __init__(self, host, port, unit):
        self.address = (host, port)
        self.unit = unit
        self.tid = 0
        self.sock = None

    def request(self, pdu):
        """Send one request PDU and return the response PDU."""
        if self.sock is None:
            self.sock = socket.create_connection(self.address, timeout=TIMEOUT_S)
        self.tid = (self.tid + 1) & 0xFFFF
        try:
            self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu)
            while True:
                tid, _, length, _ = struct.unpack(">HHHB", self._recv(7))
                body = self._recv(length - 1)
                if tid == self.tid:  # skip late answers to requests that timed out
                    break
        except OSError:
            self.close()  # timeout or lost connection: start clean on the next request
            raise
        if body[0] & 0x80:
            raise ModbusException(body[1])
        return body

    def _recv(self, size):
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionResetError("connection closed by the server")
            data += chunk
        return data

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def read_register(client, function_code, address):
    body = client.request(struct.pack(">BHH", function_code, address, 1))
    return struct.unpack(">H", body[2:4])[0]


def degrees(counts):
    return f"{counts / 10:.1f} C"


def first_request(client, send):
    """Retry the first request while the server is still starting."""
    deadline = time.monotonic() + RETRY_S
    while True:
        try:
            return send()
        except ConnectionError:  # refused, reset or closed; a timeout is not retried
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def describe(error):
    if isinstance(error, ModbusException):
        return str(error)
    if isinstance(error, socket.timeout):
        return "timeout"
    return "connection lost"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument("--sp", type=float, help="setpoint to write first, in degC")
    parser.add_argument("--reads", type=int, default=10, help="how many SP/PV reads to print")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between reads")
    args = parser.parse_args()
    if args.sp is not None and not 0 <= round(args.sp * 10) <= 0xFFFF:
        parser.error("--sp must be between 0 and 6553.5")

    client = Client(args.host, args.port, args.unit)
    failed = False
    started = False
    if args.sp is not None:
        sp = round(args.sp * 10)
        try:
            first_request(client, lambda: client.request(struct.pack(">BHH", 6, 0, sp)))
            print(f"wrote SP {degrees(sp)}", flush=True)
        except (ModbusException, OSError) as error:
            print(f"write SP: {describe(error)}", flush=True)
            failed = True
        started = True
    start = time.monotonic()
    for n in range(args.reads):
        time.sleep(max(0.0, start + n * args.interval - time.monotonic()))  # no drift
        try:
            if started:
                sp = read_register(client, 3, 0)
            else:
                sp = first_request(client, lambda: read_register(client, 3, 0))
                started = True
            pv = read_register(client, 4, 0)
            print(f"SP {degrees(sp)}  PV {degrees(pv)}", flush=True)
        except (ModbusException, OSError) as error:
            print(describe(error), flush=True)
            failed = True
    client.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
