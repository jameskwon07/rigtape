## What changes

<!-- One change per PR, reviewable in about 30 minutes (product code up to ~350 lines). -->

## Files to read closely (product code)

-

## Files to skim (table-style tests, config)

-

## Checks

<!-- Summarise the results of:
uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
docker build -t rigtape:dev . && RIGTAPE_DOCKER_IMAGE=rigtape:dev uv run pytest tests/docker -->

## Checklist

- [ ] One change only; tests that run without real hardware are included
- [ ] Design decisions added to `docs/decisions.md` (date, decision, reason)
- [ ] CLAUDE.md "Commands" section updated if a build, test or run command appeared or changed
- [ ] Example device definitions and recordings state their source
- [ ] New dependencies: license checked (MIT, BSD, Apache-2.0, PSF; open an issue before GPL/AGPL)
- [ ] No secrets (tokens, keys, credentials) and no proprietary documents, device definitions or captured traffic
