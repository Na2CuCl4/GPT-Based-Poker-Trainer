"""Development entry point for the Texas Hold'em poker trainer.

For production (10-50 users), use Gunicorn instead:
    gunicorn --worker-class gthread -w 1 --threads 50 --bind 0.0.0.0:5000 --timeout 120 wsgi:app
"""
import os
import sys
import yaml

# Ensure project root is on sys.path when run as script
sys.path.insert(0, os.path.dirname(__file__))

from web.server import create_app


def load_config(path: str = "config/game_config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    config = load_config()
    app, socketio = create_app(config)
    print("=" * 50)
    print("  德州扑克训练器已启动")
    print("  浏览器访问: http://localhost:5000")
    print("  按 Ctrl+C 退出")
    print("=" * 50)
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
