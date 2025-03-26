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

# Create tessdata directories and ensure eng.traineddata is available
RUN mkdir -p /usr/share/tesseract-ocr/5.0/tessdata \
    && mkdir -p /usr/share/tesseract-ocr/5/tessdata \
    && mkdir -p /usr/share/tessdata \
    && find /usr -name eng.traineddata | xargs -I{} ln -sf {} /usr/share/tesseract-ocr/5.0/tessdata/eng.traineddata \
    && find /usr -name eng.traineddata | xargs -I{} ln -sf {} /usr/share/tesseract-ocr/5/tessdata/eng.traineddata \
    && find /usr -name eng.traineddata | xargs -I{} ln -sf {} /usr/share/tessdata/eng.traineddata

# Verify Tesseract installation and make sure it's in the PATH
RUN tesseract --version && \
    tesseract --list-langs && \
    which tesseract && \
    echo "export PATH=$PATH:/usr/bin" >> /etc/profile && \
    echo "export TESSDATA_PREFIX=/usr/share/tesseract-ocr/5.0/tessdata" >> /etc/profile

# Set environment variables for Tesseract
ENV PATH="/usr/bin:${PATH}"
ENV TESSDATA_PREFIX="/usr/share/tesseract-ocr/5.0/tessdata"
ENV PRODUCTION="true"

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
RUN python -c "import pytesseract; print('Tesseract version:', pytesseract.get_tesseract_version())"

# Expose the port the app runs on
EXPOSE 8080

# Use the entrypoint script
CMD ["/app/entrypoint.sh"] 
