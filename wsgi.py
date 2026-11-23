"""WSGI 入口，供 gunicorn 使用。"""

from natlab import app

__all__ = ["app"]
