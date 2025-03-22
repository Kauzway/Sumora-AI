#!/bin/bash
set -e

echo "===== STARTING APPLICATION ====="
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"
echo "Python path: $(which python)"

# Create required directories if they don't exist
mkdir -p /app/slides /app/static/slide_images /app/static/images /app/static/Sumora_images /app/templates /tmp

# Check for Tesseract installation
if command -v tesseract &> /dev/null; then
    TESSERACT_VERSION=$(tesseract --version | head -n 1)
    echo "✅ Tesseract found: $TESSERACT_VERSION"
    # Verify Tesseract functionality with a simple test
    if python -c "from PIL import Image; import pytesseract; img = Image.new('RGB', (50, 10), color = (255, 255, 255)); pytesseract.image_to_string(img)" &> /dev/null; then
        echo "✅ Tesseract OCR is functioning correctly"
    else
        echo "⚠️ Tesseract is installed but not functioning correctly. Check configuration."
    fi
else
    echo "⚠️ Tesseract not found. Image text extraction will be limited."
    # Install tesseract if this is a Cloud Run environment
    if [ -n "$K_SERVICE" ] || [ -n "$K_REVISION" ]; then
        echo "Cloud Run environment detected, attempting to install Tesseract..."
        apt-get update && apt-get install -y tesseract-ocr tesseract-ocr-eng && apt-get clean
        if command -v tesseract &> /dev/null; then
            TESSERACT_VERSION=$(tesseract --version | head -n 1)
            echo "✅ Successfully installed Tesseract: $TESSERACT_VERSION"
        else
            echo "❌ Failed to install Tesseract. Proceeding without OCR capabilities."
        fi
    fi
fi

# Get the PORT environment variable or default to 8080
PORT=${PORT:-8080}
echo "Using port: $PORT"

# Try to use gunicorn if available
if command -v gunicorn &> /dev/null; then
    echo "Gunicorn found, using it to start the application"
    echo "Gunicorn version: $(gunicorn --version)"
    exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
else
    echo "Gunicorn not found, falling back to Flask development server"
    # Fall back to Flask's development server with port from environment
    export FLASK_APP=app.py
    exec python -m flask run --host=0.0.0.0 --port=$PORT
fi 
