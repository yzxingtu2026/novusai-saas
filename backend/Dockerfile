FROM node:22.22.0-bookworm-slim AS plugin-toolchain

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        [ -e "$f" ] || continue; \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; \
    done; \
    apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    build-essential libcairo2-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /usr/local/bin/

COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
COPY VERSION /
COPY VERSION ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
# 中文: 生产依赖必须由 uv.lock 决定，避免 pip 按宽松范围解析到未验证的新版本。
# EN: Production dependencies must come from uv.lock to avoid unverified range resolution.
RUN uv sync --frozen --no-dev

FROM builder AS plugin-builder

COPY --from=plugin-toolchain /usr/local/bin/node /usr/local/bin/node
COPY --from=plugin-toolchain /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global --no-audit --no-fund pnpm@10.28.2

COPY backend/scripts ./scripts
COPY --exclude=frontend/dist backend/plugins/novusdoc/ ./plugins/novusdoc/
COPY --exclude=frontend/dist backend/plugins/slider-captcha/ ./plugins/slider-captcha/
COPY --exclude=frontend/dist backend/plugins/storage-billing/ ./plugins/storage-billing/
COPY --exclude=frontend/dist backend/plugins/storage-migration/ ./plugins/storage-migration/

RUN set -eux; \
    for plugin in novusdoc slider-captcha storage-billing storage-migration; do \
        frontend="plugins/$plugin/frontend"; \
        if [ -f "$frontend/pnpm-lock.yaml" ]; then \
            pnpm --dir "$frontend" install --frozen-lockfile; \
        else \
            npm ci --prefix "$frontend" --no-audit --no-fund; \
        fi; \
        .venv/bin/novusai plugin build "plugins/$plugin"; \
        .venv/bin/novusai plugin validate "plugins/$plugin"; \
    done

FROM scratch AS plugin-assets

COPY --from=plugin-builder /app/plugins/novusdoc/frontend/dist/ /plugins/novusdoc/frontend/dist/
COPY --from=plugin-builder /app/plugins/slider-captcha/frontend/dist/ /plugins/slider-captcha/frontend/dist/
COPY --from=plugin-builder /app/plugins/storage-billing/frontend/dist/ /plugins/storage-billing/frontend/dist/
COPY --from=plugin-builder /app/plugins/storage-migration/frontend/dist/ /plugins/storage-migration/frontend/dist/

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/app/.venv/bin:$PATH" \
    TZ=Asia/Shanghai

RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        [ -e "$f" ] || continue; \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; \
    done; \
    apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 novusai \
    && useradd --system --uid 10001 --gid novusai --home-dir /app --shell /usr/sbin/nologin novusai

WORKDIR /app

COPY --from=builder --chown=novusai:novusai /app/.venv /app/.venv
COPY --from=builder /VERSION /VERSION
COPY --from=builder --chown=novusai:novusai /app/VERSION /app/VERSION
COPY --chown=novusai:novusai \
    --exclude=plugins/novusdoc/frontend/dist \
    --exclude=plugins/slider-captcha/frontend/dist \
    --exclude=plugins/storage-billing/frontend/dist \
    --exclude=plugins/storage-migration/frontend/dist \
    backend/ .
COPY --from=plugin-assets --chown=novusai:novusai /plugins/ ./plugins/

RUN mkdir -p /app/logs /app/storage/uploads /app/storage/cache/images /app/var/celerybeat \
    && chown -R novusai:novusai /app

USER novusai

FROM runtime AS api

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5).read()" || exit 1
CMD ["sh", "-ec", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-}\" --workers 2"]

FROM runtime AS worker

CMD ["celery", "-A", "app.celery_app:celery_app", "worker", "--loglevel=info", "--pool=solo"]

FROM runtime AS beat

CMD ["celery", "-A", "app.celery_app:celery_app", "beat", "--loglevel=info", "--schedule=/app/var/celerybeat/celerybeat-schedule"]
