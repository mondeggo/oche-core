FROM ghcr.io/astral-sh/uv:0.12.12 AS uv
FROM python:3.13-slim AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN groupadd --gid 10001 ochecore && useradd --uid 10001 --gid ochecore --create-home ochecore
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config ./config
RUN mkdir /data && chown ochecore:ochecore /data
ENV PATH="/app/.venv/bin:$PATH" OCHECORE_DATA_DIR=/data OCHECORE_HOST=0.0.0.0
USER ochecore
EXPOSE 9180
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9180/healthz', timeout=3)"]
CMD ["ochecore"]
