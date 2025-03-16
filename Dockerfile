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

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt
# Explicitly install gunicorn and verify it's installed
RUN pip install --no-cache-dir gunicorn && \
    gunicorn --version

# Copy the rest of the application
COPY . .

# Ensure entrypoint script is executable
RUN chmod +x /app/entrypoint.sh

# Expose the port the app runs on
EXPOSE 8080

# Use the entrypoint script
CMD ["/app/entrypoint.sh"] 
