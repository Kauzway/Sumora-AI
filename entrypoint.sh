#!/bin/bash
set -e

echo "===== STARTING APPLICATION ====="
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"
echo "Python path: $(which python)"

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
