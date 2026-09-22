FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/bridge

RUN useradd --create-home --uid 10001 bridge
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install .
COPY --chown=bridge:bridge config ./config
COPY --chown=bridge:bridge scripts ./scripts
RUN mkdir -p /data/state /data/telegram-web-profile /app/var/tmp \
    && chown -R bridge:bridge /data /app/var \
    && chmod 0700 /data/state /data/telegram-web-profile /app/var/tmp \
    && chmod 0755 /app/scripts/docker-entrypoint.sh /app/scripts/bootstrap_telegram_web.py

USER bridge
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["music-bridge"]
