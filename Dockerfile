FROM python:3.9-slim

# Install system dependencies including SWIG and Tesseract OCR
RUN apt-get update && apt-get install -y \
    swig \
    build-essential \
    python3-dev \
    libopenblas-dev \
    git \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-osd \
    tesseract-ocr-script-latn \
    poppler-utils \
    libtesseract-dev \
    libleptonica-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create tessdata directories and ensure proper TESSDATA_PREFIX setup
RUN mkdir -p /usr/share/tesseract-ocr/5.0/tessdata \
    && mkdir -p /usr/share/tesseract-ocr/5/tessdata \
    && mkdir -p /usr/share/tessdata \
    && FOUND_DATA=$(find /usr -name eng.traineddata -type f | head -n 1) \
    && if [ -n "$FOUND_DATA" ]; then \
        cp "$FOUND_DATA" /usr/share/tesseract-ocr/5.0/tessdata/eng.traineddata; \
        cp "$FOUND_DATA" /usr/share/tesseract-ocr/5/tessdata/eng.traineddata; \
        cp "$FOUND_DATA" /usr/share/tessdata/eng.traineddata; \
        echo "Copied eng.traineddata to multiple locations"; \
    fi

# Verify Tesseract installation and make sure it's in the PATH
RUN tesseract --version && \
    tesseract --list-langs || true && \
    which tesseract && \
    echo "export PATH=$PATH:/usr/bin" >> /etc/profile && \
    echo "export TESSDATA_PREFIX=/usr/share/tesseract-ocr/5.0/tessdata" >> /etc/profile

# Set environment variables for Tesseract
ENV PATH="/usr/bin:${PATH}"
ENV TESSDATA_PREFIX="/usr/share/tesseract-ocr/5.0/tessdata"
ENV PRODUCTION="true"

# Google OAuth environment variables will need to be set at runtime:
# - GOOGLE_CLIENT_ID
# - GOOGLE_CLIENT_SECRET
# - SECRET_KEY
# - APP_BASE_URL

# Explicitly set Tesseract environment in the container
RUN echo "Verifying Tesseract configuration:" && \
    echo "TESSDATA_PREFIX=$TESSDATA_PREFIX" && \
    find /usr -name eng.traineddata | xargs -I{} echo "Found eng.traineddata at: {}" && \
    ls -la $TESSDATA_PREFIX || true

# Set working directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt
# Explicitly install gunicorn and verify it's installed
RUN pip install --no-cache-dir gunicorn && \
    pip install --no-cache-dir pytesseract && \
    gunicorn --version

# Copy the rest of the application
COPY . .

# Ensure entrypoint script is executable
RUN chmod +x /app/entrypoint.sh

# Create directories for uploads and cache
RUN mkdir -p /app/slides /app/static/slide_images /app/static/Sumora_images /app/templates

# Test if tesseract is properly installed and accessible
RUN python -c "import pytesseract; from PIL import Image; print('Tesseract version:', pytesseract.get_tesseract_version()); img = Image.new('RGB', (50, 10), color=(255, 255, 255)); result = pytesseract.image_to_string(img); print('OCR test result length:', len(result))"

# Expose the port the app runs on
EXPOSE 8080

# Use the entrypoint script
CMD ["/app/entrypoint.sh"] 
