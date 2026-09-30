FROM python:3.13-alpine

LABEL org.opencontainers.image.title="hydrus-danbooru-bridge" \
      org.opencontainers.image.description="Danbooru-compatible API in front of the Hydrus Network Client API"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8000

RUN apk add --no-cache tzdata && adduser -D -H -u 10001 bridge
WORKDIR /app
COPY bridge ./bridge
USER bridge

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('BRIDGE_PORT','8000'), timeout=4)" || exit 1

CMD ["python", "-m", "bridge"]
