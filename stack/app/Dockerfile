# The cc-observatory app (ADR 0030): python -m ccobs.server, built on the user's machine
# by compose from the repository root (build context ".", filtered by .dockerignore).
#
# Two build stages (plus a uv stage that is only copied from): a builder that installs the project and its server extra from uv.lock into
# a virtual environment, and a runtime with only that environment. No RUN --mount (cache
# or bind mounts): compose falls back to the classic builder without Buildx, which does
# not support it (ADR 0020). The layers are ordered so that a change to the source
# reuses the dependency layer.
#
# Every base image is pinned to an exact tag and its multi-arch index digest; Dependabot
# updates them (.github/dependabot.yml). Python follows .python-version (3.12), and uv
# the [tool.uv] required-version range of pyproject.toml.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

COPY --from=uv /uv /usr/local/bin/uv

# The image's own Python, never one uv downloads; the environment in /opt/ccobs/venv,
# copied as is into the runtime stage, which has the same Python at the same path.
# Bytecode is compiled here, since the runtime does not write it.
ENV UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/ccobs/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /build

# The dependencies only, from the lockfile: this layer is rebuilt only when
# pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --extra server --no-install-project

# Then the project itself, installed as a package (not linked to /build).
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra server --no-editable

FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

LABEL org.opencontainers.image.title="cc-observatory-app" \
      org.opencontainers.image.description="Local observability for Claude Code: the hook ingestion app" \
      org.opencontainers.image.source="https://github.com/Luanderson-Dev/cc-observatory" \
      org.opencontainers.image.licenses="MIT"

# A fixed, unprivileged user and group (10001), and the data directory it owns: a new
# named volume mounted there copies that ownership, so the app can write its SQLite
# database without a chown on the host (ADR 0020).
RUN groupadd --system --gid 10001 ccobs \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin ccobs \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/ccobs

# Owned by root, so the app cannot change its own code.
COPY --from=builder /opt/ccobs/venv /opt/ccobs/venv

# CCOBS_LISTEN_HOST: all interfaces of the container, or neither Docker's port publishing
# nor Prometheus (over the compose network) could reach the app; the code's default is
# 127.0.0.1. compose publishes the app on 127.0.0.1 only and the metrics port not at all
# (ADRs 0018 and 0028). Python writes logs at once and no bytecode at run time.
ENV PATH=/opt/ccobs/venv/bin:$PATH \
    CCOBS_LISTEN_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /var/lib/ccobs

# Documentation only: the app (CCOBS_PORT) and its metrics listener (CCOBS_METRICS_PORT).
EXPOSE 4300 9464

# Exec form: python is PID 1 and gets Docker's SIGTERM, which uvicorn handles by shutting
# down gracefully (ADR 0030).
CMD ["python", "-m", "ccobs.server"]
