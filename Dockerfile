# Multi-stage build following uv's Docker guide. Pinned by tag and digest; bump both together.
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first, so code changes reuse this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-editable --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Same base as the builder: the venv points at /usr/local/bin/python.
FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 999 rigtape \
    && useradd --system --gid 999 --uid 999 --no-create-home rigtape
# The venv stays root-owned and read-only for the runtime user.
COPY --from=builder /app/.venv /app/.venv
COPY --chmod=755 examples /app/examples
ENV PATH="/app/.venv/bin:$PATH" RIGTAPE_HOST=0.0.0.0 PYTHONUNBUFFERED=1
WORKDIR /app
USER 999:999
EXPOSE 5020 18502
ENTRYPOINT ["rigtape"]
CMD ["serve", "examples/pid/device.yaml"]
