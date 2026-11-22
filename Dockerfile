# Production All-in-One Container for Superset Job Pilot:
#   - Python 3.11 + Flask + Gunicorn (24/7 Superset Monitor, Auto-Applier, Shortlist PDF Scanner)
#   - Node.js 22 LTS (Built-in Loopback WhatsApp Linked-Device QR Bridge via Baileys)
#   - Headless Chromium + Liberation Fonts (1-Page A4 Arial Company Resume Tailor PDF Export)
FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    DATA_DIR=/app/data \
    WA_AUTH_DIR=/app/data/wa_auth \
    WA_BRIDGE_PORT=8091 \
    WHATSAPP_BRIDGE_URL=http://127.0.0.1:8091 \
    CHROME_BIN=/usr/bin/chromium \
    AI_PROVIDER=heuristic \
    ENABLE_OLLAMA=false

# 1. System packages: Chromium (for 1-page A4 resume PDF), fonts, and Node.js 22 LTS (for WhatsApp bridge)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    chromium \
    fonts-liberation \
    fonts-dejavu-core \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python dependencies (cached layer)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install Node.js WhatsApp bridge dependencies (cached layer)
COPY whatsapp_bridge/package.json whatsapp_bridge/package-lock.json ./whatsapp_bridge/
RUN cd /app/whatsapp_bridge && npm ci --omit=dev --no-audit --no-fund

# 4. Copy application source (respecting .dockerignore — zero secrets/vault baked in)
COPY . .
RUN chmod +x /app/docker-entrypoint.sh && mkdir -p /app/data /app/data/wa_auth

# Persistent storage for encrypted SQLite vault + WhatsApp Linked-Device auth session
VOLUME ["/app/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/api/health" >/dev/null || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
