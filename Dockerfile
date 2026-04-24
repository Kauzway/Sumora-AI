# syntax=docker/dockerfile:1.6

# ---------- Builder stage ----------
# Compile wheels for all Python deps here so the final image doesn't need
# build-essential / swig / python3-dev. torch is pinned to the CPU wheel index
# to avoid pulling the ~750 MB CUDA build that we can't use on Cloud Run anyway.
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    swig \
    python3-dev \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .

# CPU-only torch from the dedicated index is ~200 MB instead of ~800 MB.
RUN pip install --upgrade pip && \
    pip wheel --wheel-dir /wheels \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        torch && \
    pip wheel --wheel-dir /wheels -r requirements.txt && \
    pip wheel --wheel-dir /wheels gunicorn pytesseract

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PRODUCTION=true \
    PATH="/usr/bin:${PATH}" \
    TESSDATA_PREFIX="/usr/share/tesseract-ocr/5/tessdata"

# Runtime system deps only — no compilers, no dev headers, no git.
# Tesseract script-latn / osd packages were dropped; the app only uses English.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libopenblas0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

WORKDIR /app

# Install the pre-built wheels from the builder stage.
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels \
        -r requirements.txt gunicorn pytesseract torch && \
    rm -rf /wheels /root/.cache

# Copy application code last so source changes don't bust the deps layer.
COPY . .

RUN chmod +x /app/entrypoint.sh && \
    mkdir -p /app/slides /app/static/slide_images /app/static/Sumora_images /app/templates

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
