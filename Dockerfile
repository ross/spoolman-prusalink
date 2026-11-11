FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/ross/spoolman-prusalink"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# The sqlite3 CLI, for reading the bridge's database (see README's Ledger section).
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# The pinned dependencies first, so a source change doesn't reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 1000 --no-create-home bridge \
    && mkdir -p /data /config && chown bridge /data

USER bridge
VOLUME ["/data"]
ENTRYPOINT ["spoolman-prusalink"]
CMD ["run"]
