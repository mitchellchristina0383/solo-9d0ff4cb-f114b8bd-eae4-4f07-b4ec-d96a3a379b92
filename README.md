# TCP NAT 演练台

值班员排查“采集终端连不上游”用的 TCP NAT 演练页。在网页上设一个公网出口
（IP + 端口范围），模拟内网端点向不同上游端点建连，观察分到的公网端口；
再推进演练时钟、从指定上游向公网端口打回包，看送达的内网端点或丢弃原因。

## 快速启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

冷却时长默认 60 秒（演练时间），可用环境变量调整：

```bash
NATLAB_COOLDOWN_SECONDS=10 NATLAB_PORT=8080 docker compose up --build
```

没有 Docker 时本地直跑：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 8 wsgi:app
```

> 必须单 worker。演练状态保存在进程内存中，多 worker 会各有一张表。
> Compose 里已固定 `--workers 1 --threads 8`，并发安全由核心状态机的锁保证。

## 演练规则（页面实现口径）

| 规则 | 实现 |
| --- | --- |
| 不同上游端点可共用公网端口 | 映射键是 `(公网端口, 上游端点)` |
| 同一上游端点的连接绝不撞号 | 分配时排除该上游所有在用端口 |
| 关闭后 `(公网端口, 原上游)` 冷却 60 秒 | 冷却表按键隔离，到期自动释放 |
| 其他上游不受冷却影响 | 冷却只锁原上游该端口 |
| 耗尽时列出该上游在用/冷却端口数 | 409 + `active_count` / `cooling_count` / 端口明细 |
| 冷却中的迟包丢弃 | 回包先查在用映射，再查冷却表，命中冷却即 `COOLDOWN_LATE` |
| 可推进演练时钟 | 虚拟秒数，不依赖真实时间，便于复现 |

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前快照（配置、时钟、连接、冷却、按上游汇总） |
| POST | `/api/config` | 设置 `public_ip` / `port_min` / `port_max`；`replace=true` 清空后应用 |
| POST | `/api/reset` | 清空连接与冷却，保留配置和时钟 |
| POST | `/api/clock/advance` | `{"seconds": N}` 推进演练时钟 |
| POST | `/api/connections` | `internal_ip/internal_port/upstream_ip/upstream_port` 建连 |
| POST | `/api/connections/<id>/close` | 关闭连接并进入冷却 |
| POST | `/api/replies` | `upstream_ip/upstream_port/public_port` 模拟回包 |

变更类接口返回 `{"snapshot": ..., "result": ...}`；业务错误返回 4xx +
`{"error": {"code", "message", "details"}}`。

回包丢弃原因码：`COOLDOWN_LATE`、`UPSTREAM_MISMATCH`、`PORT_OUT_OF_RANGE`、
`NO_MAPPING`。

## 自测

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v   # 后端 29 项
node tests/frontend_smoke.mjs                          # 前端交互 17 项断言
```

`tests/frontend_smoke.mjs` 用最小 DOM 桩加载页面里的真实脚本，按后端返回形状
驱动交互，防止 JS 与 API 字段漂移；没有 Node 时可跳过。

## 结构

```
natlab/core.py           状态机：分配、复用、冷却、回包、快照
natlab/__init__.py       Flask 应用工厂 + JSON API
natlab/templates/index.html  单页演练台
wsgi.py                  gunicorn 入口
tests/                   后端单测 + 前端桩冒烟
Dockerfile / docker-compose.yml
```
