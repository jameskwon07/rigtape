# Security policy

## Supported versions

rigtape is in early development. Fixes go to the latest commit on `main` and, once releases
exist, to the latest release only.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/jameskwon07/rigtape/security/advisories/new).
Please do not open a public issue for a security problem.

Include the rigtape version (`rigtape --version`) or commit, the command line, and the smallest
input that shows the problem (device YAML, faults YAML, recording lines or the bytes sent).
Reports are handled on a best-effort basis; there is no bug bounty.

## Scope and documented limits

rigtape is a test tool for developer machines and CI, not for production networks.

- The Modbus TCP port has no authentication, like Modbus TCP itself.
- The fault control API (started with `--faults`, port 18502 by default) has no authentication.
  It binds the same address as the Modbus port: `127.0.0.1` unless `--host` or `RIGTAPE_HOST`
  says otherwise, and rigtape logs a warning when it is bound off loopback.
- The Docker image sets `RIGTAPE_HOST=0.0.0.0` (the same as `--host 0.0.0.0`), so both ports
  listen on every interface inside the container. Publish them as `-p 127.0.0.1:...` only.
- `rigtape record` forwards traffic to the device given with `--upstream`. Anyone who can reach
  the recorder's port can talk to that device through it.

Reports about these documented limits are still welcome when they show a way around them, for
example a crash or a hang caused by crafted input.
