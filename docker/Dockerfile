FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy

WORKDIR /app

# Install deps separately from project source so source bind-mounts during dev
# don't invalidate the dep layer.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project

# Install the project itself in editable mode so bind-mounting src/ over /app/src
# at runtime picks up changes without rebuild.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -e .

ENV KOMVENTORY_DATA=/data
ENV PATH=/app/.venv/bin:$PATH
ENV PYTHONUNBUFFERED=1

CMD ["komventory", "watch"]
