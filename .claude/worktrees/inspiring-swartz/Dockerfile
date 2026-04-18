# Kim Relay Server — Docker image
# Deployable to Railway, Render, Fly.io free tier.
#
# Build:  docker build -t kim-relay .
# Run:    docker run -p 3001:3001 \
#           -e RELAY_PHONE_API_KEY=your-phone-key \
#           -e RELAY_PC_API_KEY=your-pc-key \
#           kim-relay

FROM python:3.12-slim

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# ── App user (don't run as root) ──────────────────────────────────────────────
RUN useradd -m -u 1000 kim
WORKDIR /app
RUN chown kim:kim /app
USER kim

# ── Python deps ───────────────────────────────────────────────────────────────
COPY --chown=kim:kim requirements-relay.txt ./
RUN pip install --no-cache-dir --user -r requirements-relay.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY --chown=kim:kim relay_server/ ./relay_server/

# ── Data directory for SQLite ─────────────────────────────────────────────────
RUN mkdir -p /app/data
ENV RELAY_DB_PATH=/app/data/relay.db

# ── Runtime ───────────────────────────────────────────────────────────────────
ENV PORT=3001
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 3001

CMD ["python", "-m", "uvicorn", "relay_server.main:app", \
     "--host", "0.0.0.0", "--port", "3001", \
     "--workers", "1", "--log-level", "info"]
