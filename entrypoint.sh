#!/bin/bash
set -e

echo "===== STARTING APPLICATION ====="
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"
echo "Python path: $(which python)"

# Google OAuth config sanity checks
for var in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET SECRET_KEY; do
    if [ -z "${!var}" ]; then
        echo "⚠️ WARNING: $var is not set."
    fi
done

if [ -z "$APP_BASE_URL" ]; then
    if [ -n "$K_SERVICE" ]; then
        echo "⚠️ WARNING: APP_BASE_URL is not set in Cloud Run environment. OAuth redirects may not work correctly."
    else
        echo "ℹ️ APP_BASE_URL is not set, will use default based on environment detection."
    fi
fi

# NVIDIA NIM API key sanity check
if [ -z "$NVIDIA_API_KEY" ]; then
    echo "⚠️ WARNING: NVIDIA_API_KEY is not set. AI inference will fail."
fi

# Create required working directories
mkdir -p /app/slides /app/static/slide_images /app/static/images \
         /app/static/Sumora_images /app/templates /tmp

# Verify LibreOffice is installed (required for PPTX -> PDF conversion)
if command -v soffice &> /dev/null; then
    echo "✅ LibreOffice found: $(soffice --version 2>/dev/null | head -n 1)"
else
    echo "⚠️ LibreOffice not found — PPTX uploads will be rejected."
fi

PORT=${PORT:-8080}
echo "Starting gunicorn on port $PORT"
exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
