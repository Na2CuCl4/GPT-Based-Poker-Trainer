"""Production entry point for Gunicorn + Eventlet.

Usage:
    gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:5000 --timeout 120 wsgi:app
"""
import eventlet
eventlet.monkey_patch()  # must be before all other imports

import yaml
from web.server import create_app


def _load_config(path: str = "config/game_config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


_config = _load_config()
app, socketio = create_app(_config)
