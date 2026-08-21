# =============================================================================
# Tony-EDWARD — Docker image
# =============================================================================
# Base: Python 3.12 slim. Adds system deps for Playwright + pty fork.
# Final image size ~1.2GB (Playwright Chromium is the largest chunk).
# =============================================================================

FROM python:3.12-slim AS base

# --- System deps ---
# playwright install-deps pulls most of these, but we pre-install the
# essentials so first-boot is fast.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
        git \
        bash \
        build-essential \
        libffi-dev \
        libssl-dev \
        libsqlite3-dev \
        # Playwright Chromium runtime deps (subset)
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        # For pty.fork() used by terminal_exec.py
        util-linux \
        # For terminal_exec.py PTY support
        libutempter0 \
    && rm -rf /var/lib/apt/lists/*

# --- Working dir ---
WORKDIR /app

# --- Python deps ---
# Install requirements first (better Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# --- Playwright browsers + OS deps ---
RUN python -m playwright install --with-deps chromium

# --- App source ---
COPY . /app/

# Ensure storage dir exists (Render mounts a persistent disk here)
RUN mkdir -p /app/storage

# --- Python path ---
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# --- Expose port ---
EXPOSE 8000

# --- Health check ---
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# --- Run ---
# Use --workers=1 to keep terminal manager + pattern DB consistent.
# Scale horizontally by deploying multiple services with separate storage
# (or wire up Redis + Postgres for shared state in a future upgrade).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
