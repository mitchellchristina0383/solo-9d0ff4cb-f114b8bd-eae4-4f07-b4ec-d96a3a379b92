"""TCP NAT 演练台的核心状态机。

设计约束（与页面上的演练规则一一对应）：

* 公网端口按 (公网端口, 上游端点) 这一对键复用：不同上游端点可以落在同一个
  公网端口上；同一上游端点的多条连接必须拿到互不相同的公网端口。
* 连接关闭后，(公网端口, 原上游端点) 进入固定长度的冷却期，冷却期内不能再
  分配给该上游；其他上游不受影响。
* 所有时间都使用"演练时钟"（虚拟秒），不依赖真实时间，方便值班员推进和复现。
* 所有公开方法都在同一把可重入锁内完成校验和状态变更，Flask 多线程下也不会
  出现检查/写入竞态。
"""

from __future__ import annotations

import ipaddress
import threading
from dataclasses import dataclass
from typing import Any

COOLDOWN_SECONDS = 60
MIN_PORT = 1
MAX_PORT = 65535


def format_elapsed(seconds: int) -> str:
    """把演练秒数格式化为 T+[Nd ]HH:MM:SS。"""

    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"T+{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"T+{hours:02d}:{minutes:02d}:{secs:02d}"


def format_host_port(ip: str, port: int) -> str:
    """IPv6 地址加方括号，避免 ip:port 产生歧义。"""

    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


class NatError(Exception):
    """可安全返回给页面的业务错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _is_ascii_digits(text: str) -> bool:
    """只接受 ASCII 数字，避免全角数字等 Unicode 数字被 int() 悄悄接受。"""

    return bool(text) and all("0" <= char <= "9" for char in text)


def _parse_ip(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NatError("INVALID_INPUT", f"{field}必须是有效的 IP 地址", details={"field": field})
    try:
        # str() 会做压缩/规范化，后续匹配使用同一规范形式。
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise NatError(
            "INVALID_INPUT",
            f"{field}“{value.strip()}”不是有效的 IP 地址",
            details={"field": field},
        ) from exc


def _parse_port(value: Any, field: str) -> int:
    port: int | None = None
    if isinstance(value, bool):
        port = None
    elif isinstance(value, int):
        port = value
    elif isinstance(value, float) and value.is_integer():
        port = int(value)
    elif isinstance(value, str) and _is_ascii_digits(value.strip()):
        port = int(value.strip())
    if port is None or not MIN_PORT <= port <= MAX_PORT:
        raise NatError(
            "INVALID_INPUT",
            f"{field}必须是 {MIN_PORT}-{MAX_PORT} 之间的整数",
            details={"field": field},
        )
    return port


def _parse_seconds(value: Any, field: str = "推进秒数") -> int:
    amount: int | None = None
    if isinstance(value, bool):
        amount = None
    elif isinstance(value, int):
        amount = value
    elif isinstance(value, float) and value.is_integer():
        amount = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if _is_ascii_digits(text):
            amount = int(text)
    if amount is None or amount < 0:
        raise NatError(
            "INVALID_INPUT",
            f"{field}必须是非负整数秒",
            details={"field": field},
        )
    return amount


@dataclass(frozen=True)
class Endpoint:
    ip: str
    port: int

    @property
    def label(self) -> str:
        return format_host_port(self.ip, self.port)

    def to_dict(self) -> dict[str, Any]:
        return {"ip": self.ip, "port": self.port, "label": self.label}


@dataclass(frozen=True)
class PublicConfig:
    public_ip: str
    port_min: int
    port_max: int

    @property
    def size(self) -> int:
        return self.port_max - self.port_min + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_ip": self.public_ip,
            "port_min": self.port_min,
            "port_max": self.port_max,
            "size": self.size,
            "label": f"{self.public_ip}:{self.port_min}-{self.port_max}",
        }


@dataclass(frozen=True)
class Connection:
    id: int
    internal: Endpoint
    upstream: Endpoint
    public_port: int
    created_at: int


@dataclass(frozen=True)
class Cooldown:
    public_port: int
    upstream: Endpoint
    internal: Endpoint
    connection_id: int
    closed_at: int
    until: int


class NatTable:
    """单个演练会话的 NAT 映射表。"""

    def __init__(self, *, cooldown_seconds: int = COOLDOWN_SECONDS, start_at: int = 0) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能为负数")
        if isinstance(start_at, bool) or not isinstance(start_at, int) or start_at < 0:
            raise ValueError("start_at 必须是非负整数")
        self._lock = threading.RLock()
        self.cooldown_seconds = cooldown_seconds
        self._config: PublicConfig | None = None
        self._now = start_at
        self._seq = 0
        self._connections: dict[int, Connection] = {}
        self._active_index: dict[tuple[int, Endpoint], int] = {}
        self._cooldowns: dict[tuple[int, Endpoint], Cooldown] = {}

    # ------------------------------------------------------------------ 配置与时钟

    def configure(
        self,
        public_ip: Any,
        port_min: Any,
        port_max: Any,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        """设置公网 IP 与端口范围。

        已有演练数据（活动连接或未到期冷却）时默认拒绝修改，防止已有映射
        失去公网地址语义；replace=True 会先清空演练数据。
        """

        with self._lock:
            config = PublicConfig(
                _parse_ip(public_ip, "公网 IP"),
                _parse_port(port_min, "起始端口"),
                _parse_port(port_max, "结束端口"),
            )
            if config.port_min > config.port_max:
                raise NatError(
                    "INVALID_RANGE",
                    f"起始端口 {config.port_min} 不能大于结束端口 {config.port_max}",
                    details={"field": "port_min"},
                )

            self._prune_expired()
            changed = config != self._config
            if changed and self._has_state() and not replace:
                raise NatError(
                    "CONFIG_LOCKED",
                    "当前存在演练数据，修改公网配置前请先清空演练数据",
                    status=409,
                    details={
                        "active_connections": len(self._connections),
                        "cooldowns": len(self._cooldowns),
                    },
                )
            if replace or (changed and not self._has_state()):
                # 没有存活映射时顺带清掉历史序号与已过期冷却，得到干净会话。
                self._clear_state()
            self._config = config
            return {"config": config.to_dict(), "changed": changed}

    def reset(self) -> dict[str, Any]:
        """清空连接与冷却，保留公网配置和演练时钟。"""

        with self._lock:
            removed = len(self._connections) + len(self._cooldowns)
            self._clear_state()
            return {"removed": removed}

    def advance(self, seconds: Any) -> dict[str, Any]:
        """推进演练时钟并清理已到期冷却。"""

        amount = _parse_seconds(seconds)
        with self._lock:
            self._now += amount
            before = len(self._cooldowns)
            self._prune_expired()
            return {
                "advanced_seconds": amount,
                "now": self._now,
                "now_label": format_elapsed(self._now),
                "expired_cooldowns": before - len(self._cooldowns),
            }

    # ------------------------------------------------------------------ 建连与关闭

    def open_connection(
        self,
        internal_ip: Any,
        internal_port: Any,
        upstream_ip: Any,
        upstream_port: Any,
    ) -> dict[str, Any]:
        """模拟内网端点向指定上游发起连接，返回分到的公网端口。"""

        with self._lock:
            if self._config is None:
                raise NatError(
                    "CONFIG_MISSING",
                    "请先设置公网 IP 与端口范围",
                    status=409,
                )
            internal = Endpoint(
                _parse_ip(internal_ip, "内网 IP"),
                _parse_port(internal_port, "内网端口"),
            )
            upstream = Endpoint(
                _parse_ip(upstream_ip, "上游 IP"),
                _parse_port(upstream_port, "上游端口"),
            )
            self._prune_expired()
            config = self._config

            for port in range(config.port_min, config.port_max + 1):
                key = (port, upstream)
                if key not in self._active_index and key not in self._cooldowns:
                    self._seq += 1
                    connection = Connection(
                        id=self._seq,
                        internal=internal,
                        upstream=upstream,
                        public_port=port,
                        created_at=self._now,
                    )
                    self._connections[connection.id] = connection
                    self._active_index[key] = connection.id
                    return self._connection_dict(connection)

            raise self._exhausted_error(upstream)

    def close_connection(self, connection_id: Any) -> dict[str, Any]:
        """关闭连接，并让 (公网端口, 原上游) 进入冷却。"""

        with self._lock:
            if isinstance(connection_id, bool):
                raise NatError("INVALID_INPUT", "连接编号必须是整数")
            if isinstance(connection_id, int):
                conn_id = connection_id
            elif isinstance(connection_id, str) and _is_ascii_digits(connection_id.strip()):
                conn_id = int(connection_id.strip())
            elif isinstance(connection_id, float) and connection_id.is_integer():
                conn_id = int(connection_id)
            else:
                raise NatError("INVALID_INPUT", "连接编号必须是整数")

            self._prune_expired()
            connection = self._connections.get(conn_id)
            if connection is None:
                raise NatError("CONNECTION_NOT_FOUND", "连接不存在或已经关闭", status=404)

            closed = self._connection_dict(connection)
            del self._connections[conn_id]
            self._active_index.pop((connection.public_port, connection.upstream), None)
            cooldown = Cooldown(
                public_port=connection.public_port,
                upstream=connection.upstream,
                internal=connection.internal,
                connection_id=connection.id,
                closed_at=self._now,
                until=self._now + self.cooldown_seconds,
            )
            self._cooldowns[(cooldown.public_port, cooldown.upstream)] = cooldown
            return {
                "closed": closed,
                "cooldown": self._cooldown_dict(cooldown),
            }

    # ------------------------------------------------------------------ 回包模拟

    def simulate_reply(
        self,
        upstream_ip: Any,
        upstream_port: Any,
        public_port: Any,
    ) -> dict[str, Any]:
        """模拟上游向公网端口回包，返回送达的内网端点或丢弃原因。"""

        with self._lock:
            if self._config is None:
                raise NatError(
                    "CONFIG_MISSING",
                    "请先设置公网 IP 与端口范围",
                    status=409,
                )
            upstream = Endpoint(
                _parse_ip(upstream_ip, "上游 IP"),
                _parse_port(upstream_port, "上游端口"),
            )
            port = _parse_port(public_port, "公网端口")
            self._prune_expired()
            config = self._config
            base: dict[str, Any] = {
                "public": {
                    "ip": config.public_ip,
                    "port": port,
                    "label": format_host_port(config.public_ip, port),
                },
                "upstream": upstream.to_dict(),
                "at": self._now,
                "at_label": format_elapsed(self._now),
            }
            key = (port, upstream)

            conn_id = self._active_index.get(key)
            if conn_id is not None:
                connection = self._connections[conn_id]
                return {
                    "outcome": "delivered",
                    "connection_id": connection.id,
                    "internal": connection.internal.to_dict(),
                    **base,
                }

            cooldown = self._cooldowns.get(key)
            if cooldown is not None:
                remaining = max(0, cooldown.until - self._now)
                return {
                    "outcome": "dropped",
                    "reason_code": "COOLDOWN_LATE",
                    "reason": (
                        f"公网端口 {port} 与原上游 {upstream.label} 的映射仍在冷却"
                        f"（剩余 {remaining} 秒），迟包已丢弃"
                    ),
                    "original_internal": cooldown.internal.to_dict(),
                    "connection_id": cooldown.connection_id,
                    "closed_at": cooldown.closed_at,
                    "closed_at_label": format_elapsed(cooldown.closed_at),
                    "cooldown_until": cooldown.until,
                    "cooldown_until_label": format_elapsed(cooldown.until),
                    "remaining_seconds": remaining,
                    **base,
                }

            if not config.port_min <= port <= config.port_max:
                return {
                    "outcome": "dropped",
                    "reason_code": "PORT_OUT_OF_RANGE",
                    "reason": (
                        f"公网端口 {port} 不在配置范围 "
                        f"{config.port_min}-{config.port_max} 内，回包已丢弃"
                    ),
                    "port_min": config.port_min,
                    "port_max": config.port_max,
                    **base,
                }

            other_upstreams = sorted(
                {
                    connection.upstream
                    for connection in self._connections.values()
                    if connection.public_port == port
                },
                key=lambda endpoint: (endpoint.ip, endpoint.port),
            )
            cooling_upstreams = sorted(
                {
                    cooldown.upstream
                    for cooldown in self._cooldowns.values()
                    if cooldown.public_port == port
                },
                key=lambda endpoint: (endpoint.ip, endpoint.port),
            )
            if other_upstreams:
                labels = "、".join(endpoint.label for endpoint in other_upstreams)
                return {
                    "outcome": "dropped",
                    "reason_code": "UPSTREAM_MISMATCH",
                    "reason": (
                        f"公网端口 {port} 当前映射到上游 {labels}，"
                        f"与来源 {upstream.label} 不匹配，回包已丢弃"
                    ),
                    "current_upstreams": [endpoint.to_dict() for endpoint in other_upstreams],
                    "cooling_upstreams": [endpoint.to_dict() for endpoint in cooling_upstreams],
                    **base,
                }

            if cooling_upstreams:
                labels = "、".join(endpoint.label for endpoint in cooling_upstreams)
                return {
                    "outcome": "dropped",
                    "reason_code": "NO_MAPPING",
                    "reason": (
                        f"公网端口 {port} 没有与上游 {upstream.label} 的映射；"
                        f"该端口正为上游 {labels} 冷却，与本回包来源无关"
                    ),
                    "cooling_upstreams": [endpoint.to_dict() for endpoint in cooling_upstreams],
                    **base,
                }

            return {
                "outcome": "dropped",
                "reason_code": "NO_MAPPING",
                "reason": (
                    f"公网端口 {port} 上不存在与上游 {upstream.label} 的映射"
                    "（从未分配，或冷却已经结束）"
                ),
                **base,
            }

    # ------------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._prune_expired()
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        config = self._config
        connections = sorted(
            self._connections.values(),
            key=lambda item: (item.public_port, item.upstream.ip, item.upstream.port, item.id),
        )
        cooldowns = sorted(
            self._cooldowns.values(),
            key=lambda item: (item.until, item.public_port, item.upstream.ip, item.upstream.port),
        )

        upstreams = sorted(
            {connection.upstream for connection in connections}
            | {cooldown.upstream for cooldown in cooldowns},
            key=lambda endpoint: (endpoint.ip, endpoint.port),
        )
        upstream_summaries = []
        for upstream in upstreams:
            active_ports = sorted(
                connection.public_port
                for connection in connections
                if connection.upstream == upstream
            )
            cooling_ports = sorted(
                cooldown.public_port
                for cooldown in cooldowns
                if cooldown.upstream == upstream
            )
            blocked = len(active_ports) + len(cooling_ports)
            capacity = config.size if config is not None else 0
            upstream_summaries.append(
                {
                    "upstream": upstream.to_dict(),
                    "active_count": len(active_ports),
                    "cooling_count": len(cooling_ports),
                    "blocked_count": blocked,
                    "available_count": max(0, capacity - blocked),
                    "capacity": capacity,
                    "active_ports": active_ports,
                    "cooling_ports": cooling_ports,
                    "exhausted": config is not None and blocked >= capacity,
                }
            )

        return {
            "config": config.to_dict() if config is not None else None,
            "clock": {
                "now": self._now,
                "label": format_elapsed(self._now),
                "cooldown_seconds": self.cooldown_seconds,
            },
            "connections": [self._connection_dict(connection) for connection in connections],
            "cooldowns": [self._cooldown_dict(cooldown) for cooldown in cooldowns],
            "upstreams": upstream_summaries,
            "summary": {
                "active_connections": len(connections),
                "cooldowns": len(cooldowns),
                "upstreams": len(upstream_summaries),
            },
        }

    # ------------------------------------------------------------------ 内部工具

    def _connection_dict(self, connection: Connection) -> dict[str, Any]:
        config = self._config
        public = None
        if config is not None:
            public = {
                "ip": config.public_ip,
                "port": connection.public_port,
                "label": format_host_port(config.public_ip, connection.public_port),
            }
        return {
            "id": connection.id,
            "internal": connection.internal.to_dict(),
            "upstream": connection.upstream.to_dict(),
            "public": public,
            "created_at": connection.created_at,
            "created_label": format_elapsed(connection.created_at),
            "age_seconds": self._now - connection.created_at,
        }

    def _cooldown_dict(self, cooldown: Cooldown) -> dict[str, Any]:
        config = self._config
        public = None
        if config is not None:
            public = {
                "ip": config.public_ip,
                "port": cooldown.public_port,
                "label": format_host_port(config.public_ip, cooldown.public_port),
            }
        return {
            "public": public,
            "public_port": cooldown.public_port,
            "upstream": cooldown.upstream.to_dict(),
            "internal": cooldown.internal.to_dict(),
            "connection_id": cooldown.connection_id,
            "closed_at": cooldown.closed_at,
            "closed_at_label": format_elapsed(cooldown.closed_at),
            "cooldown_until": cooldown.until,
            "cooldown_until_label": format_elapsed(cooldown.until),
            "remaining_seconds": max(0, cooldown.until - self._now),
        }

    def _exhausted_error(self, upstream: Endpoint) -> NatError:
        config = self._config
        assert config is not None
        active_ports = sorted(
            connection.public_port
            for connection in self._connections.values()
            if connection.upstream == upstream
        )
        cooling_ports = sorted(
            cooldown.public_port
            for cooldown in self._cooldowns.values()
            if cooldown.upstream == upstream
        )
        blocked = len(active_ports) + len(cooling_ports)
        return NatError(
            "NO_PORT_AVAILABLE",
            (
                f"上游 {upstream.label} 的公网端口已耗尽：在用 {len(active_ports)} 个，"
                f"冷却 {len(cooling_ports)} 个（范围 {config.port_min}-{config.port_max}，"
                f"共 {config.size} 个）"
            ),
            status=409,
            details={
                "upstream": upstream.to_dict(),
                "port_min": config.port_min,
                "port_max": config.port_max,
                "capacity": config.size,
                "active_count": len(active_ports),
                "cooling_count": len(cooling_ports),
                "blocked_count": blocked,
                "active_ports": active_ports,
                "cooling_ports": cooling_ports,
                "hint": "推进演练时钟待冷却结束后重试，或扩大公网端口范围",
            },
        )

    def _prune_expired(self) -> None:
        expired = [
            key for key, cooldown in self._cooldowns.items() if cooldown.until <= self._now
        ]
        for key in expired:
            del self._cooldowns[key]

    def _has_state(self) -> bool:
        return bool(self._connections or self._cooldowns)

    def _clear_state(self) -> None:
        self._connections.clear()
        self._active_index.clear()
        self._cooldowns.clear()
        # 故意不重置 _seq：连接编号保持单调递增，避免清空后用旧编号误关新连接。
