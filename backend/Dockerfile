# Multi-stage Dockerfile for ALOFT Backend
# Stage 1: Builder - compiles dependencies and creates a virtual environment
FROM python:3.12-slim AS builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies required for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Stage 2: Production image - minimal runtime with non-root user
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Install only runtime dependencies (no libpq5 — app uses MongoDB, not PostgreSQL).
# ffmpeg is required by app/services/audio_repository.py to compress narration
# MP3s to 64kbps before storage -- without it, compression silently no-ops
# and every audio file is stored at roughly double the size.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -r aloft && useradd -r -g aloft aloft

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Create application directory with proper permissions
WORKDIR /app
RUN chown -R aloft:aloft /app

# Copy application code
COPY --chown=aloft:aloft app ./app

# Copy gunicorn config (required by CMD)
COPY --chown=aloft:aloft gunicorn.conf.py ./gunicorn.conf.py

# Music assets are no longer bundled in the image -- they're fetched from R2
# at runtime via app/services/music_asset_repository.py (run
# scripts/upload_music_assets_to_r2.py once to upload them).

# Create audio storage directory with proper permissions
RUN mkdir -p /app/audio_storage && \
    chown -R aloft:aloft /app/audio_storage

# Switch to non-root user
USER aloft

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run with gunicorn
CMD ["gunicorn", "app.main:app", "--config", "gunicorn.conf.py"]
