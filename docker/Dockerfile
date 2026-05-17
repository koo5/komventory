FROM python:3.12-slim

# python:3.12-slim ships /tmp as 0775 (no sticky/world-writable). apt-get drops
# privileges to `_apt` during fetch and chokes on that — fix before apt commands.
RUN chmod 1777 /tmp \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy

WORKDIR /app

# Install deps separately from project source so source bind-mounts during dev
# don't invalidate the dep layer. --frozen uses uv.lock verbatim — fails the
# build if pyproject.toml and uv.lock disagree, so we never accidentally pull
# unpinned versions in CI/Docker.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Install the project itself in editable mode so bind-mounting src/ over /app/src
# at runtime picks up changes without rebuild.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -e .

ENV KOMVENTORY_DATA=/data
ENV PATH=/app/.venv/bin:$PATH
ENV PYTHONUNBUFFERED=1

CMD ["komventory", "watch"]
