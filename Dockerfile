# Base image pinned by its multi-arch index digest so that a rebuild cannot silently pick up
# a different Python patch level. Bump deliberately (and re-run the CI image build) when needed.
FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies (ffmpeg for video splitting and compression)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user. The bridge needs no root: it reads its own code and writes only
# to /app/session, which is a mounted volume.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --shell /usr/sbin/nologin --no-create-home app

WORKDIR /app

# Install Python dependencies first for optimal layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY --chown=app:app app/ /app/

# Ensure session directory exists and belongs to the runtime user
RUN mkdir -p /app/session && chown app:app /app/session

USER app

CMD ["python", "bridge.py"]
