"""Gunicorn settings for the etcode-backend HTTP API (not the generation worker)."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

bind = f"{os.environ.get('ETCODE_HOST', '0.0.0.0')}:{os.environ.get('ETCODE_PORT', '8765')}"
worker_class = "gthread"            # each open SSE stream occupies one thread
workers = int(os.environ.get("ETCODE_GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("ETCODE_GUNICORN_THREADS", "32"))
timeout = 0                          # SSE streams may legitimately stay open for a long time
graceful_timeout = 30
keepalive = 5
wsgi_app = "etcode_backend.wsgi:app"
