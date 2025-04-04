#!/bin/bash
set -e

echo "===== STARTING APPLICATION ====="
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"
echo "Python path: $(which python)"

# Check for Google OAuth environment variables
echo "===== CHECKING GOOGLE OAUTH CONFIGURATION ====="
if [ -z "$GOOGLE_CLIENT_ID" ]; then
    echo "⚠️ WARNING: GOOGLE_CLIENT_ID is not set. Google OAuth login will not work."
fi

if [ -z "$GOOGLE_CLIENT_SECRET" ]; then
    echo "⚠️ WARNING: GOOGLE_CLIENT_SECRET is not set. Google OAuth login will not work."
fi

if [ -z "$SECRET_KEY" ]; then
    echo "⚠️ WARNING: SECRET_KEY is not set. A random key will be generated, but sessions will not persist across restarts."
fi

if [ -z "$APP_BASE_URL" ]; then
    if [ -n "$K_SERVICE" ]; then
        echo "⚠️ WARNING: APP_BASE_URL is not set in Cloud Run environment. OAuth redirects may not work correctly."
    else
        echo "ℹ️ APP_BASE_URL is not set, will use default based on environment detection."
    fi
fi

echo "===== END OAUTH CONFIGURATION CHECK ====="

# Create required directories if they don't exist
mkdir -p /app/slides /app/static/slide_images /app/static/images /app/static/Sumora_images /app/templates /tmp

# Detect Tesseract version and set appropriate data path
TESSERACT_VERSION=$(tesseract --version | head -n 1 | awk '{print $2}' | cut -d. -f1)

# First try to find any existing eng.traineddata file
echo "Searching for eng.traineddata files..."
FOUND_TRAINEDDATA=$(find /usr -name eng.traineddata -type f 2>/dev/null | head -n 1)

if [ -n "$FOUND_TRAINEDDATA" ]; then
    DIR_NAME=$(dirname "$FOUND_TRAINEDDATA")
    echo "Found eng.traineddata at: $FOUND_TRAINEDDATA"
    echo "Setting TESSDATA_PREFIX=$DIR_NAME"
    export TESSDATA_PREFIX="$DIR_NAME"
else
    # If not found, try common locations based on version
    echo "No eng.traineddata found, trying common locations based on version $TESSERACT_VERSION"
    if [ "$TESSERACT_VERSION" == "5" ]; then
        # For Tesseract 5.x, try these locations
        for tessdata_path in \
            "/usr/share/tesseract-ocr/5.0/tessdata" \
            "/usr/share/tesseract-ocr/5/tessdata" \
            "/usr/share/tesseract/tessdata" \
            "/usr/share/tessdata"; do
            
            if [ -d "$tessdata_path" ]; then
                export TESSDATA_PREFIX="$tessdata_path"
                echo "Using $TESSDATA_PREFIX for Tesseract 5.x"
                break
            fi
        done
    else
        # For Tesseract 4.x or other versions
        for tessdata_path in \
            "/usr/share/tesseract-ocr/4.00/tessdata" \
            "/usr/share/tesseract-ocr/4.0/tessdata" \
            "/usr/share/tesseract-ocr/4/tessdata" \
            "/usr/share/tesseract-ocr/tessdata"; do
            
            if [ -d "$tessdata_path" ]; then
                export TESSDATA_PREFIX="$tessdata_path"
                echo "Using $TESSDATA_PREFIX for Tesseract 4.x or other"
                break
            fi
        done
    fi
fi

echo "Set PATH=$PATH"
echo "Set TESSDATA_PREFIX=$TESSDATA_PREFIX"

# Install language data if needed
if [ -z "$TESSDATA_PREFIX" ] || [ ! -f "${TESSDATA_PREFIX}/eng.traineddata" ]; then
    echo "No eng.traineddata found, attempting to install language data..."
    apt-get update && apt-get install -y tesseract-ocr-eng
    
    # Search again for eng.traineddata
    FOUND_TRAINEDDATA=$(find /usr -name eng.traineddata -type f 2>/dev/null | head -n 1)
    if [ -n "$FOUND_TRAINEDDATA" ]; then
        DIR_NAME=$(dirname "$FOUND_TRAINEDDATA")
        echo "After installation, found eng.traineddata at: $FOUND_TRAINEDDATA"
        echo "Setting TESSDATA_PREFIX=$DIR_NAME"
        export TESSDATA_PREFIX="$DIR_NAME"
    else
        echo "Failed to find eng.traineddata even after installation"
    fi
fi

# Check for Tesseract installation
if command -v tesseract &> /dev/null; then
    TESSERACT_VERSION=$(tesseract --version | head -n 1)
    echo "✅ Tesseract found: $TESSERACT_VERSION"
    echo "✅ Tesseract binary location: $(which tesseract)"
    
    # Try to list available languages
    tesseract --list-langs || echo "Warning: Could not list available languages"
    
    # Verify Tesseract functionality with a simple test
    if python -c "from PIL import Image; import pytesseract; img = Image.new('RGB', (50, 10), color = (255, 255, 255)); print(pytesseract.get_tesseract_version()); result = pytesseract.image_to_string(img); print('OCR Result length:', len(result))" &> /dev/null; then
        echo "✅ Tesseract OCR is functioning correctly"
    else
        echo "⚠️ Tesseract is installed but not functioning correctly. Attempting to fix..."
        
        # Show the actual error
        python -c "from PIL import Image; import pytesseract; img = Image.new('RGB', (50, 10), color = (255, 255, 255)); print(pytesseract.get_tesseract_version()); result = pytesseract.image_to_string(img); print('OCR Result length:', len(result))" || true
        
        # If TESSDATA_PREFIX is set but the directory doesn't exist, create it
        if [ -n "$TESSDATA_PREFIX" ] && [ ! -d "$TESSDATA_PREFIX" ]; then
            echo "Creating TESSDATA_PREFIX directory: $TESSDATA_PREFIX"
            mkdir -p "$TESSDATA_PREFIX"
        fi
        
        # Try to find eng.traineddata anywhere in the system and copy it
        FOUND_TRAINEDDATA=$(find /usr -name eng.traineddata -type f 2>/dev/null | head -n 1)
        if [ -n "$FOUND_TRAINEDDATA" ] && [ -n "$TESSDATA_PREFIX" ]; then
            echo "Copying $FOUND_TRAINEDDATA to $TESSDATA_PREFIX/"
            cp "$FOUND_TRAINEDDATA" "$TESSDATA_PREFIX/"
            
            # Try the test again
            if python -c "from PIL import Image; import pytesseract; img = Image.new('RGB', (50, 10), color = (255, 255, 255)); print(pytesseract.get_tesseract_version()); result = pytesseract.image_to_string(img); print('OCR Result length:', len(result))" &> /dev/null; then
                echo "✅ Tesseract OCR is now functioning correctly after copying eng.traineddata"
            else
                echo "⚠️ Tesseract OCR still not functioning correctly. Will proceed without OCR capabilities."
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
            FOUND_TRAINEDDATA=$(find /usr -name eng.traineddata -type f 2>/dev/null | head -n 1)
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
