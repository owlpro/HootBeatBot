FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium chromium-sandbox ffmpeg fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 bridge
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install .
RUN mkdir -p /app/config
COPY --chown=bridge:bridge config/topics.example.yml ./config/topics.example.yml
COPY --chown=bridge:bridge scripts ./scripts
RUN mkdir -p /data/state /app/var/tmp \
    && chown -R bridge:bridge /data /app/var \
    && chmod 0700 /data/state /app/var/tmp \
    && chmod 0755 /app/scripts/docker-entrypoint.sh

USER bridge
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["music-bridge"]
