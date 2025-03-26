#!/bin/bash
set -e

echo "===== STARTING APPLICATION ====="
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"
echo "Python path: $(which python)"

# Create required directories if they don't exist
mkdir -p /app/slides /app/static/slide_images /app/static/images /app/static/Sumora_images /app/templates /tmp

# Detect Tesseract version and set appropriate data path
TESSERACT_VERSION=$(tesseract --version | head -n 1 | awk '{print $2}' | cut -d. -f1)
if [ "$TESSERACT_VERSION" == "5" ]; then
    export TESSDATA_PREFIX="/usr/share/tesseract-ocr/5.0/tessdata"
    # Check if the directory exists, if not try alternatives
    if [ ! -d "$TESSDATA_PREFIX" ]; then
        if [ -d "/usr/share/tesseract-ocr/5/tessdata" ]; then
            export TESSDATA_PREFIX="/usr/share/tesseract-ocr/5/tessdata"
        elif [ -d "/usr/share/tesseract/tessdata" ]; then
            export TESSDATA_PREFIX="/usr/share/tesseract/tessdata"
        fi
    fi
else
    export TESSDATA_PREFIX="/usr/share/tesseract-ocr/4.00/tessdata"
fi

echo "Set PATH=$PATH"
echo "Set TESSDATA_PREFIX=$TESSDATA_PREFIX"

# Create eng.traineddata symlink if needed
if [ ! -f "${TESSDATA_PREFIX}/eng.traineddata" ] && [ -f "/usr/share/tesseract-ocr/tessdata/eng.traineddata" ]; then
    echo "Creating symlink for eng.traineddata from /usr/share/tesseract-ocr/tessdata/"
    ln -sf /usr/share/tesseract-ocr/tessdata/eng.traineddata ${TESSDATA_PREFIX}/eng.traineddata
fi

# Check for Tesseract installation
if command -v tesseract &> /dev/null; then
    TESSERACT_VERSION=$(tesseract --version | head -n 1)
    echo "✅ Tesseract found: $TESSERACT_VERSION"
    echo "✅ Tesseract binary location: $(which tesseract)"
    
    # List all possible tessdata locations
    echo "Searching for tessdata directories..."
    find /usr -name tessdata -type d | while read dir; do
        echo "Found tessdata directory: $dir"
        if [ -f "$dir/eng.traineddata" ]; then
            echo "  ✅ Found eng.traineddata in this directory"
            # Update TESSDATA_PREFIX if we found a valid directory with eng.traineddata
            export TESSDATA_PREFIX="$dir"
            echo "  ✅ Updated TESSDATA_PREFIX=$TESSDATA_PREFIX"
        else
            echo "  ❌ No eng.traineddata in this directory"
        fi
    done
    
    echo "Current TESSDATA_PREFIX=$TESSDATA_PREFIX"
    
    # Verify Tesseract functionality with a simple test
    if python -c "from PIL import Image; import pytesseract; img = Image.new('RGB', (50, 10), color = (255, 255, 255)); print(pytesseract.get_tesseract_version()); result = pytesseract.image_to_string(img); print('OCR Result length:', len(result))" &> /dev/null; then
        echo "✅ Tesseract OCR is functioning correctly"
    else
        echo "⚠️ Tesseract is installed but not functioning correctly. Attempting to fix..."
        # Show the actual error
        python -c "from PIL import Image; import pytesseract; img = Image.new('RGB', (50, 10), color = (255, 255, 255)); print(pytesseract.get_tesseract_version()); result = pytesseract.image_to_string(img); print('OCR Result length:', len(result))" || true
        
        # Try to install language data if missing
        if [ ! -f "${TESSDATA_PREFIX}/eng.traineddata" ]; then
            echo "Installing missing tessdata files..."
            apt-get update && apt-get install -y tesseract-ocr-eng
            
            # Find the installed eng.traineddata file
            FOUND_TRAINEDDATA=$(find /usr -name eng.traineddata -type f | head -n 1)
            if [ -n "$FOUND_TRAINEDDATA" ]; then
                DIR_NAME=$(dirname "$FOUND_TRAINEDDATA")
                echo "Found eng.traineddata at: $FOUND_TRAINEDDATA"
                echo "Setting TESSDATA_PREFIX=$DIR_NAME"
                export TESSDATA_PREFIX="$DIR_NAME"
            fi
        fi
    fi
else
    echo "⚠️ Tesseract not found. Image text extraction will be limited."
    # Install tesseract if this is a Cloud Run environment
    if [ -n "$K_SERVICE" ] || [ -n "$K_REVISION" ]; then
        echo "Cloud Run environment detected, attempting to install Tesseract..."
        apt-get update && apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd tesseract-ocr-script-latn && apt-get clean
        
        if command -v tesseract &> /dev/null; then
            TESSERACT_VERSION=$(tesseract --version | head -n 1)
            echo "✅ Successfully installed Tesseract: $TESSERACT_VERSION"
            echo "✅ Tesseract binary location: $(which tesseract)"
            
            # Find the installed eng.traineddata file
            FOUND_TRAINEDDATA=$(find /usr -name eng.traineddata -type f | head -n 1)
            if [ -n "$FOUND_TRAINEDDATA" ]; then
                DIR_NAME=$(dirname "$FOUND_TRAINEDDATA")
                echo "Found eng.traineddata at: $FOUND_TRAINEDDATA"
                echo "Setting TESSDATA_PREFIX=$DIR_NAME"
                export TESSDATA_PREFIX="$DIR_NAME"
            fi
            
            # Verify installation worked
            tesseract --list-langs
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
