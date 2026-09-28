# RUNBOOK: one container, one embedded SQLite file, no runtime services.
# Build the image once while online; afterwards the image starts offline.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RUNBOOK_DATA_DIR=/data \
    RUNBOOK_SEED_ON_BOOT=1 \
    RUNBOOK_DEMO_SESSIONS=on

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY fixtures.json ./fixtures.json
COPY fixtures ./fixtures
COPY server.py ./server.py
COPY .dogfood.toml ./.dogfood.toml

RUN mkdir -p /data && chmod 770 /data
EXPOSE 8080

# The service binds to 0.0.0.0 so Compose and the sandbox preview can reach it.
CMD ["python", "server.py"]
