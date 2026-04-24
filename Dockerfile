# syntax=docker/dockerfile:1.6

# ---------- Builder stage ----------
# Build every wheel here so the final image carries no compilers. Torch is
# pinned to the pure-CPU index; otherwise pip pulls the ~800 MB CUDA wheel
# from PyPI (which is what was bloating the previous image).
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

# Strip torch out of requirements so the subsequent wheel pass can't resolve
# it against PyPI's CUDA build — we want the CPU wheel only.
RUN grep -v -i '^torch' requirements.txt > requirements-no-torch.txt && \
    pip install --upgrade pip && \
    pip wheel --wheel-dir /wheels \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=1.9.0" && \
    pip wheel --wheel-dir /wheels -r requirements-no-torch.txt && \
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

# Runtime-only system deps — no compilers, no dev headers, no git.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libopenblas0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN grep -v -i '^torch' requirements.txt > requirements-no-torch.txt && \
    pip install --no-index --find-links=/wheels "torch>=1.9.0" && \
    pip install --no-index --find-links=/wheels \
        -r requirements-no-torch.txt gunicorn pytesseract && \
    rm -rf /wheels /root/.cache requirements-no-torch.txt && \
    find /usr/local/lib/python3.11 -type d -name '__pycache__' -prune -exec rm -rf {} + && \
    find /usr/local/lib/python3.11 -type d -name 'tests' -prune -exec rm -rf {} +

# Copy application code last so source edits don't bust the deps layer.
COPY . .

RUN chmod +x /app/entrypoint.sh && \
    mkdir -p /app/slides /app/static/slide_images /app/static/Sumora_images /app/templates

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
