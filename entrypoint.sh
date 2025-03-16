#!/bin/bash
set -e

echo "===== STARTING APPLICATION ====="
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"
echo "Python path: $(which python)"

# Try to use gunicorn if available
if command -v gunicorn &> /dev/null; then
    echo "Gunicorn found, using it to start the application"
    echo "Gunicorn version: $(gunicorn --version)"
    exec gunicorn --bind :8080 --workers 1 --threads 8 --timeout 0 app:app
else
    echo "Gunicorn not found, falling back to Flask development server"
    # Fall back to Flask's development server
    exec python app.py
fi 
