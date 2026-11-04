FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 1000 --no-create-home bridge \
    && mkdir -p /data /config && chown bridge /data

USER bridge
VOLUME ["/data"]
ENTRYPOINT ["spoolman-prusalink"]
CMD ["run"]
