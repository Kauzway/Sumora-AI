FROM python:3.9-slim

# Install system dependencies including SWIG
RUN apt-get update && apt-get install -y \
    swig \
    build-essential \
    python3-dev \
    libopenblas-dev \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install gunicorn and Python dependencies
RUN pip install --no-cache-dir gunicorn
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the port the app runs on
EXPOSE 8080

# Use gunicorn to run the app in production mode
CMD exec gunicorn --bind :8080 --workers 1 --threads 8 --timeout 0 app:app 
