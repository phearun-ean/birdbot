web: gunicorn app:flask_app --worker-class sync --workers 1 --timeout 120 --bind 0.0.0.0:${PORT:-5000}
