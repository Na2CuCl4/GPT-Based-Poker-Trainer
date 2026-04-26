"""Production entry point for Gunicorn + gthread.

Usage:
    gunicorn --worker-class gthread -w 1 --threads 50 --bind 0.0.0.0:5000 --timeout 120 wsgi:app
"""
import yaml
from web.server import create_app


def _load_config(path: str = "config/game_config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


_config = _load_config()
app, socketio = create_app(_config)
