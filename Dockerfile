# Curatarr in a container (NAS / server installs; the Windows tray stays the
# desktop route). Everything the app writes - the database, ChromaDB, logs,
# caches, backups and the .env the setup wizard writes - lives in /app/data:
# mount ONE volume there and the container itself stays disposable.
#
#   docker build -t curatarr .
#   docker run -d -p 8000:8000 -v /srv/curatarr:/app/data curatarr
#
# deploy/docker-compose.yml is the TrueNAS / compose version.

# python:3.12.15-slim-trixie, pinned to the multi-arch index digest: a tag can
# be re-pointed, a digest cannot (bump both together).
FROM python:3.12.15-slim-trixie@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CURATARR_ENV_FILE=/app/data/.env

WORKDIR /app

# The hash-pinned universal lock (src/deps_lock.py): the same install CI
# tests, every file checked against a sha256 written in the repo. Its own
# layer, so a code-only change does not reinstall the dependencies.
COPY requirements.txt ./
COPY lock/ lock/
RUN pip install --require-hashes -r lock/requirements.txt

COPY src/ src/
COPY frontend/ frontend/
COPY assets/ assets/
COPY build_models.py ./

# 568:568 is TrueNAS SCALE's "apps" user; any uid works as long as it owns
# the mounted data directory.
RUN mkdir -p /app/data && chown -R 568:568 /app/data
USER 568:568

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"]

# No --reload: an update is a new image, not a file change.
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "8", "--no-server-header"]
