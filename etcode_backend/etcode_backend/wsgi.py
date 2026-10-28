"""Gunicorn entry point: ``etcode_backend.wsgi:app``."""
from .api import create_app

app = create_app()
