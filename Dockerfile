FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    DATA_DIR=/data \
    PORT=8787

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

LABEL org.opencontainers.image.title="Trimarr" \
      org.opencontainers.image.description="Keeps Sonarr series at season 1 unless Plex shows a recent play, and adds watchlist titles to Sonarr and Radarr." \
      org.opencontainers.image.source="https://github.com/keberling/season-keeper"

EXPOSE 8787
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; p=os.environ.get('PORT','8787'); urllib.request.urlopen(f'http://127.0.0.1:{p}/health')"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
