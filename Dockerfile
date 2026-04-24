# syntax=docker/dockerfile:1.6

# ---------- Builder stage ----------
# Strategy: install torch from the CPU-only index FIRST, then install the rest
# of the requirements. Once torch is already present in the interpreter, pip's
# resolver accepts it as satisfied and does not re-download the CUDA build
# that PyPI serves as "torch". The previous wheel-based approach kept letting
# the CUDA wheel sneak in as a transitive dep of sentence-transformers.
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

WORKDIR /build
COPY requirements.txt .

# 1. CPU-only torch. The dedicated index hosts torch+cpu wheels exclusively.
# 2. Everything else. Pip sees torch is installed and accepts it as satisfying
#    sentence-transformers' torch>=1.11 constraint — no CUDA torch pulled.
RUN pip install --upgrade pip && \
    pip install --index-url https://download.pytorch.org/whl/cpu "torch>=1.9.0" && \
    pip install -r requirements.txt gunicorn

# Pre-download the sentence-transformers embedding model at build time. This
# bakes it into the image so the running container never hits HuggingFace at
# first-use (which was throwing 429 in production). ~90 MB on disk.
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('all-MiniLM-L6-v2', cache_folder='/opt/hf-cache')"

# Trim the fat before copying site-packages forward: no __pycache__, no test
# suites, no .pyc. Saves tens of MB.
RUN find /usr/local/lib/python3.11 -type d \( -name '__pycache__' -o -name 'tests' -o -name 'test' \) -prune -exec rm -rf {} + && \
    find /usr/local/lib/python3.11 -name '*.pyc' -delete

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRODUCTION=true \
    HF_HOME=/opt/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf-cache
# Note: NOT setting HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE so that the occasional
# metadata check can still fall back to the network — the actual model weights
# are already on disk from the builder stage, so no full download ever runs.

# Runtime deps only — no compilers, no dev headers.
# libreoffice-impress + core handles PPTX/PPT -> PDF at upload time.
# libopenblas0 is the shared lib numpy/faiss/torch link against at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-impress \
    libreoffice-core \
    fonts-liberation \
    fonts-dejavu \
    poppler-utils \
    libopenblas0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Bring Python packages and the gunicorn launcher from the builder. No pip in
# the runtime stage — everything is already installed to site-packages.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn
COPY --from=builder /opt/hf-cache /opt/hf-cache

WORKDIR /app
COPY . .

RUN chmod +x /app/entrypoint.sh && \
    mkdir -p /app/slides /app/static/slide_images /app/static/Sumora_images /app/templates

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
