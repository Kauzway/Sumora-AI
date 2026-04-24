# syntax=docker/dockerfile:1.6

# ---------- Builder stage ----------
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

# Keep torch out of the requirements pass so pip cannot pull the ~800 MB
# CUDA wheel from PyPI — we want the CPU wheel only.
RUN grep -v -i '^torch' requirements.txt > requirements-no-torch.txt && \
    pip install --upgrade pip && \
    pip wheel --wheel-dir /wheels \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=1.9.0" && \
    pip wheel --wheel-dir /wheels -r requirements-no-torch.txt && \
    pip wheel --wheel-dir /wheels gunicorn

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PRODUCTION=true

# Runtime deps:
# - libreoffice-impress (pulls libreoffice-core) converts PPTX/PPT -> PDF so the
#   vision pipeline can render slides to images. Heavier than Tesseract, but it
#   replaces both OCR and every non-PDF parser.
# - poppler-utils: kept in case PDF utilities are needed downstream.
# - libopenblas0: runtime for numpy/faiss/torch.
# - fonts-liberation + fonts-dejavu: reasonable default typography for the
#   LibreOffice render so PPTX output doesn't fall back to ugly substitutes.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-impress \
    libreoffice-core \
    fonts-liberation \
    fonts-dejavu \
    poppler-utils \
    libopenblas0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN grep -v -i '^torch' requirements.txt > requirements-no-torch.txt && \
    pip install --no-index --find-links=/wheels "torch>=1.9.0" && \
    pip install --no-index --find-links=/wheels \
        -r requirements-no-torch.txt gunicorn && \
    rm -rf /wheels /root/.cache requirements-no-torch.txt && \
    find /usr/local/lib/python3.11 -type d -name '__pycache__' -prune -exec rm -rf {} + && \
    find /usr/local/lib/python3.11 -type d -name 'tests' -prune -exec rm -rf {} +

COPY . .

RUN chmod +x /app/entrypoint.sh && \
    mkdir -p /app/slides /app/static/slide_images /app/static/Sumora_images /app/templates

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
