# Asset index: one image, two roles (web UI and periodic scanner), see compose.yaml.
FROM python:3.13-slim-bookworm

# bsdtar (libarchive) extracts single members of zip/rar/7z archives for previews.
# aria2 downloads queued links in the fetcher service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libarchive-tools aria2 \
    && rm -rf /var/lib/apt/lists/*

# 7-Zip (the official 7zz) tests archives whole for the "verify" move job, RAR included:
# libarchive cannot decode some RAR methods. From 7-Zip's own GitHub releases (the server
# reaches GitHub for updates anyway); bsdtar unpacks the download (no xz needed);
# 7zzs is the static build (the slim image may lack libstdc++).
ARG SEVENZIP_TAG=24.09
ARG SEVENZIP_VERSION=2409
RUN arch="$(dpkg --print-architecture | sed 's/^amd64$/x64/')" \
    && python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/7z.tar.xz')" \
        "https://github.com/ip7z/7zip/releases/download/${SEVENZIP_TAG}/7z${SEVENZIP_VERSION}-linux-${arch}.tar.xz" \
    && bsdtar -xf /tmp/7z.tar.xz -C /tmp 7zzs \
    && mv /tmp/7zzs /usr/local/bin/7zz \
    && rm /tmp/7z.tar.xz \
    && 7zz i > /dev/null

COPY --from=ghcr.io/astral-sh/uv:0.7.20 /uv /usr/local/bin/uv

# C.UTF-8: without it libarchive can't match non-ASCII member names (Chinese, Japanese).
ENV LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    ASSET_DATA_DIR=/data \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY assetindex ./assetindex
RUN uv sync --frozen --no-dev

# Non-root. /data is created here so a named volume inherits this ownership.
ARG APP_UID=1000
RUN useradd --uid "${APP_UID}" --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
USER app

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/stats', timeout=8)"]
# One worker on purpose: render/extract slot limits and the failure cache are per process.
CMD ["python", "-m", "assetindex", "serve", "--host", "0.0.0.0", "--port", "8000"]
