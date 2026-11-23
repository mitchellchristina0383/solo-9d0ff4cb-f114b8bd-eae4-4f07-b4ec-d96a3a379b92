"""Flask 应用工厂：页面 + JSON API。

同一份核心状态只在一个进程内有效，因此 Docker Compose 只启动一个
gunicorn worker（见 Dockerfile），避免多 worker 时各进程各有一张 NAT 表。

API 约定：
* GET  /api/state            → 快照
* POST /api/*（变更类）       → {"snapshot": 快照, "result": 本次操作结果}
* 业务错误                    → HTTP 4xx + {"error": {code, message, details}}
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable

from flask import Flask, jsonify, render_template, request

from .core import COOLDOWN_SECONDS, NatError, NatTable


def _json_body() -> dict[str, Any]:
    """读取 JSON 对象请求体；空 body 视为 {}。"""

    if not request.data:
        return {}
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise NatError("INVALID_JSON", "请求体必须是 JSON 对象")
    return payload


def _api(view: Callable[..., dict[str, Any]]):
    """把 NatError 统一转成 4xx JSON 响应。"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return jsonify(view(*args, **kwargs))
        except NatError as exc:
            return jsonify({"error": exc.to_dict()}), exc.status

    return wrapper


def _cooldown_from_env(default: int) -> int:
    raw = os.environ.get("NATLAB_COOLDOWN_SECONDS")
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"NATLAB_COOLDOWN_SECONDS 必须是整数秒，当前为 {raw!r}"
        ) from exc
    if value < 0:
        raise RuntimeError(f"NATLAB_COOLDOWN_SECONDS 不能为负数，当前为 {value}")
    return value


def create_app(*, cooldown_seconds: int | None = None) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = False

    if cooldown_seconds is None:
        cooldown_seconds = _cooldown_from_env(COOLDOWN_SECONDS)
    table = NatTable(cooldown_seconds=cooldown_seconds)
    app.extensions["nat_table"] = table

    def mutate(result: dict[str, Any]) -> dict[str, Any]:
        """变更类接口统一返回“最新快照 + 本次操作结果”。"""

        return {"snapshot": table.snapshot(), "result": result}

    @app.get("/")
    def index():
        return render_template("index.html", cooldown_seconds=table.cooldown_seconds)

    @app.get("/api/state")
    @_api
    def get_state():
        return table.snapshot()

    @app.post("/api/config")
    @_api
    def post_config():
        body = _json_body()
        return mutate(
            table.configure(
                body.get("public_ip"),
                body.get("port_min"),
                body.get("port_max"),
                replace=bool(body.get("replace", False)),
            )
        )

    @app.post("/api/reset")
    @_api
    def post_reset():
        return mutate(table.reset())

    @app.post("/api/clock/advance")
    @_api
    def post_advance():
        return mutate(table.advance(_json_body().get("seconds")))

    @app.post("/api/connections")
    @_api
    def post_connection():
        body = _json_body()
        return mutate(
            table.open_connection(
                body.get("internal_ip"),
                body.get("internal_port"),
                body.get("upstream_ip"),
                body.get("upstream_port"),
            )
        )

    @app.post("/api/connections/<connection_id>/close")
    @_api
    def post_close(connection_id: str):
        return mutate(table.close_connection(connection_id))

    @app.post("/api/replies")
    @_api
    def post_reply():
        body = _json_body()
        return mutate(
            table.simulate_reply(
                body.get("upstream_ip"),
                body.get("upstream_port"),
                body.get("public_port"),
            )
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    return app


app = create_app()
