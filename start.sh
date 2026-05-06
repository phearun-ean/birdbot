
pkill -f python#!/bin/bash
echo "Starting Bird Nest House Bot..."
source .venv/bin/activate
gunicorn app:flask_app --worker-class sync --workers 1 --timeout 120 --bind 0.0.0.0:${PORT:-5000}
