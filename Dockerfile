FROM python:3.12-slim

# gosu: privilege dropping (Debian equivalent of su-exec)
# util-linux: provides the mountpoint binary for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    util-linux \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appgroup && useradd -r -g appgroup appuser

WORKDIR /app
RUN mkdir -p /app/data && touch /app/data/config.yml

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY app.py entrypoint.sh ./
COPY src ./src
COPY static ./static
COPY templates ./templates

RUN chmod +x /app/entrypoint.sh && \
    chown -R appuser:appgroup /app

EXPOSE 8222

# entrypoint.sh drops privileges to PUID/PGID via gosu
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8222", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
