"""核心状态机与 HTTP API 的自动化验收测试。

用标准库 unittest 编写（环境里不依赖 pytest）：
    PYTHONPATH=. python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest

from natlab import create_app
from natlab.core import NatError, NatTable


def open_conn(table: NatTable, i: str = "10.0.0.21", ip: str = "198.51.100.7", port: int = 443):
    return table.open_connection(i, 51515, ip, port)


def make_table(size: int = 10, cooldown: int = 60) -> NatTable:
    table = NatTable(cooldown_seconds=cooldown)
    table.configure("203.0.113.10", 40000, 40000 + size - 1)
    return table


class PortReuseTests(unittest.TestCase):
    def test_different_upstreams_share_public_port(self):
        table = make_table()
        first = open_conn(table, ip="198.51.100.7")
        second = open_conn(table, ip="198.51.100.8")

        self.assertEqual(first["public"]["port"], 40000)
        self.assertEqual(second["public"]["port"], 40000)
        self.assertNotEqual(first["upstream"], second["upstream"])
        self.assertEqual(table.snapshot()["summary"]["active_connections"], 2)

    def test_same_upstream_never_collides(self):
        table = make_table(size=10)
        ports = {open_conn(table, i=f"10.0.0.{i}")["public"]["port"] for i in range(1, 11)}
        self.assertEqual(ports, set(range(40000, 40010)))

        with self.assertRaises(NatError) as ctx:
            open_conn(table, i="10.0.0.99")
        err = ctx.exception
        self.assertEqual(err.code, "NO_PORT_AVAILABLE")
        self.assertEqual(err.details["active_count"], 10)
        self.assertEqual(err.details["cooling_count"], 0)
        self.assertEqual(err.details["capacity"], 10)

    def test_concurrent_opens_on_same_upstream_are_unique(self):
        table = make_table(size=40)
        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(40)

        def worker(index: int) -> None:
            barrier.wait()
            try:
                results.append(open_conn(table, i=f"10.1.0.{index}"))
            except Exception as exc:  # pragma: no cover - 失败时用于断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        ports = [item["public"]["port"] for item in results]
        self.assertEqual(len(ports), 40)
        self.assertEqual(len(set(ports)), 40)
        self.assertEqual(set(ports), set(range(40000, 40040)))

    def test_other_upstream_can_reuse_port_under_pressure(self):
        table = make_table(size=2)
        open_conn(table, ip="198.51.100.7")
        open_conn(table, ip="198.51.100.7")
        with self.assertRaises(NatError):
            open_conn(table, ip="198.51.100.7")
        # 另一个上游仍有余量，且从 40000 开始分配
        other = open_conn(table, ip="198.51.100.8")
        self.assertEqual(other["public"]["port"], 40000)


class CooldownTests(unittest.TestCase):
    def test_closed_port_is_cooled_and_other_upstream_unaffected(self):
        table = make_table()
        conn = open_conn(table, ip="198.51.100.7")
        port = conn["public"]["port"]

        closed = table.close_connection(conn["id"])
        self.assertEqual(closed["cooldown"]["public_port"], port)
        self.assertEqual(closed["cooldown"]["cooldown_until"], 60)
        self.assertEqual(closed["cooldown"]["remaining_seconds"], 60)

        # 原上游拿不到这个端口
        replacement = open_conn(table, ip="198.51.100.7")
        self.assertNotEqual(replacement["public"]["port"], port)

        # 其他上游立刻可以用这个端口
        other = open_conn(table, ip="198.51.100.8")
        self.assertEqual(other["public"]["port"], port)

    def test_cooldown_expiry_is_exact(self):
        table = make_table(size=1)
        conn = open_conn(table)
        table.close_connection(conn["id"])

        table.advance(59)
        with self.assertRaises(NatError) as ctx:
            open_conn(table)
        self.assertEqual(ctx.exception.details["cooling_count"], 1)

        result = table.advance(1)
        self.assertEqual(result["expired_cooldowns"], 1)
        self.assertEqual(table.snapshot()["cooldowns"], [])
        again = open_conn(table)
        self.assertEqual(again["public"]["port"], 40000)

    def test_close_twice_rejected(self):
        table = make_table()
        conn = open_conn(table)
        table.close_connection(conn["id"])
        with self.assertRaises(NatError) as ctx:
            table.close_connection(conn["id"])
        self.assertEqual(ctx.exception.code, "CONNECTION_NOT_FOUND")

    def test_close_rejects_non_integer_id(self):
        table = make_table()
        conn = open_conn(table)
        for bad in ("abc", "1.5", 1.5, True, None, "１２"):
            with self.subTest(value=bad):
                with self.assertRaises(NatError) as ctx:
                    table.close_connection(bad)
                self.assertEqual(ctx.exception.code, "INVALID_INPUT")
        # 字符串数字可用
        self.assertEqual(table.close_connection(str(conn["id"]))["closed"]["id"], conn["id"])

    def test_exhaustion_reports_active_and_cooling_counts(self):
        table = make_table(size=3)
        a = open_conn(table, ip="198.51.100.7")
        b = open_conn(table, ip="198.51.100.7")
        open_conn(table, ip="198.51.100.7")
        table.close_connection(a["id"])

        with self.assertRaises(NatError) as ctx:
            open_conn(table, ip="198.51.100.7")
        details = ctx.exception.details
        self.assertEqual(details["active_count"], 2)
        self.assertEqual(details["cooling_count"], 1)
        self.assertEqual(details["blocked_count"], 3)
        self.assertEqual(details["capacity"], 3)
        self.assertEqual(details["active_ports"], [40001, 40002])
        self.assertEqual(details["cooling_ports"], [40000])

        # 快照里该上游被标记为耗尽
        summary = table.snapshot()["upstreams"][0]
        self.assertTrue(summary["exhausted"])
        self.assertEqual(summary["active_count"], 2)
        self.assertEqual(summary["cooling_count"], 1)
        self.assertEqual(summary["available_count"], 0)
        self.assertEqual(b["public"]["port"], 40001)


class ReplyTests(unittest.TestCase):
    def test_reply_delivered_to_internal_endpoint(self):
        table = make_table()
        conn = open_conn(table, i="10.0.0.21", ip="198.51.100.7")
        result = table.simulate_reply("198.51.100.7", 443, conn["public"]["port"])
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(result["internal"]["ip"], "10.0.0.21")
        self.assertEqual(result["internal"]["port"], 51515)
        self.assertEqual(result["connection_id"], conn["id"])

    def test_reply_routed_per_upstream_on_shared_port(self):
        table = make_table()
        a = open_conn(table, i="10.0.0.21", ip="198.51.100.7")
        b = open_conn(table, i="10.0.0.22", ip="198.51.100.8")
        self.assertEqual(a["public"]["port"], b["public"]["port"])

        first = table.simulate_reply("198.51.100.7", 443, a["public"]["port"])
        second = table.simulate_reply("198.51.100.8", 443, b["public"]["port"])
        self.assertEqual(first["internal"]["ip"], "10.0.0.21")
        self.assertEqual(second["internal"]["ip"], "10.0.0.22")

    def test_late_packet_during_cooldown_is_dropped(self):
        table = make_table()
        conn = open_conn(table)
        port = conn["public"]["port"]
        table.close_connection(conn["id"])

        table.advance(30)
        result = table.simulate_reply("198.51.100.7", 443, port)
        self.assertEqual(result["outcome"], "dropped")
        self.assertEqual(result["reason_code"], "COOLDOWN_LATE")
        self.assertEqual(result["remaining_seconds"], 30)
        self.assertEqual(result["original_internal"]["ip"], "10.0.0.21")
        self.assertIn("冷却", result["reason"])

    def test_packet_after_cooldown_has_no_mapping(self):
        table = make_table()
        conn = open_conn(table)
        port = conn["public"]["port"]
        table.close_connection(conn["id"])
        table.advance(60)
        result = table.simulate_reply("198.51.100.7", 443, port)
        self.assertEqual(result["outcome"], "dropped")
        self.assertEqual(result["reason_code"], "NO_MAPPING")

    def test_packet_for_other_upstream_port_in_cooldown_reports_cooling(self):
        table = make_table()
        a = open_conn(table, ip="198.51.100.7")
        table.close_connection(a["id"])
        result = table.simulate_reply("198.51.100.8", 443, a["public"]["port"])
        self.assertEqual(result["outcome"], "dropped")
        self.assertEqual(result["reason_code"], "NO_MAPPING")
        self.assertEqual(result["cooling_upstreams"][0]["ip"], "198.51.100.7")

    def test_unknown_upstream_on_occupied_port_is_dropped(self):
        table = make_table()
        conn = open_conn(table, ip="198.51.100.7")
        result = table.simulate_reply("198.51.100.9", 443, conn["public"]["port"])
        self.assertEqual(result["outcome"], "dropped")
        self.assertEqual(result["reason_code"], "UPSTREAM_MISMATCH")
        self.assertEqual(result["current_upstreams"][0]["ip"], "198.51.100.7")

    def test_reply_outside_configured_range_is_dropped(self):
        table = make_table(size=2)
        result = table.simulate_reply("198.51.100.7", 443, 50000)
        self.assertEqual(result["outcome"], "dropped")
        self.assertEqual(result["reason_code"], "PORT_OUT_OF_RANGE")

    def test_reply_with_wrong_upstream_port_is_dropped(self):
        table = make_table()
        conn = open_conn(table, port=443)
        result = table.simulate_reply("198.51.100.7", 8443, conn["public"]["port"])
        self.assertEqual(result["outcome"], "dropped")
        self.assertEqual(result["reason_code"], "UPSTREAM_MISMATCH")


class ConfigAndValidationTests(unittest.TestCase):
    def test_invalid_inputs(self):
        table = NatTable()
        cases = [
            (("not-an-ip", 40000, 40009), "INVALID_INPUT"),
            (("203.0.113.10", 0, 40009), "INVALID_INPUT"),
            (("203.0.113.10", 40009, 40000), "INVALID_RANGE"),
            (("203.0.113.10", "abc", 40009), "INVALID_INPUT"),
        ]
        for args, code in cases:
            with self.subTest(args=args):
                with self.assertRaises(NatError) as ctx:
                    table.configure(*args)
                self.assertEqual(ctx.exception.code, code)

    def test_open_requires_config(self):
        table = NatTable()
        with self.assertRaises(NatError) as ctx:
            open_conn(table)
        self.assertEqual(ctx.exception.code, "CONFIG_MISSING")

    def test_config_locked_while_state_exists_and_replace_override(self):
        table = make_table()
        open_conn(table)
        with self.assertRaises(NatError) as ctx:
            table.configure("203.0.113.11", 41000, 41009)
        self.assertEqual(ctx.exception.code, "CONFIG_LOCKED")

        table.configure("203.0.113.11", 41000, 41009, replace=True)
        snapshot = table.snapshot()
        self.assertEqual(snapshot["config"]["public_ip"], "203.0.113.11")
        self.assertEqual(snapshot["connections"], [])
        conn = open_conn(table)
        self.assertEqual(conn["public"]["ip"], "203.0.113.11")
        self.assertEqual(conn["public"]["port"], 41000)

    def test_reset_clears_state_but_keeps_config_and_clock(self):
        table = make_table()
        conn = open_conn(table)
        table.close_connection(conn["id"])
        table.advance(10)
        table.reset()
        snapshot = table.snapshot()
        self.assertEqual(snapshot["connections"], [])
        self.assertEqual(snapshot["cooldowns"], [])
        self.assertEqual(snapshot["clock"]["now"], 10)
        self.assertIsNotNone(snapshot["config"])
        # 编号单调递增：清空后不会复用旧编号
        self.assertGreater(open_conn(table)["id"], conn["id"])

    def test_invalid_advance(self):
        table = NatTable()
        for bad in (-1, "x", None, True):
            with self.subTest(value=bad):
                with self.assertRaises(NatError):
                    table.advance(bad)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(cooldown_seconds=60)
        self.client = self.app.test_client()
        response = self.client.post(
            "/api/config",
            json={"public_ip": "203.0.113.10", "port_min": 40000, "port_max": 40001},
        )
        self.assertEqual(response.status_code, 200)

    def post(self, path, payload):
        return self.client.post(path, json=payload)

    def test_page_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for marker in ("模拟建连", "模拟上游回包", "推进", "冷却"):
            self.assertIn(marker, html)

    def test_full_http_flow(self):
        created = self.post(
            "/api/connections",
            {
                "internal_ip": "10.0.0.21",
                "internal_port": 51515,
                "upstream_ip": "198.51.100.7",
                "upstream_port": 443,
            },
        )
        self.assertEqual(created.status_code, 200)
        body = created.get_json()
        conn = body["result"]
        self.assertEqual(conn["public"]["port"], 40000)
        self.assertEqual(body["snapshot"]["summary"]["active_connections"], 1)

        replied = self.post(
            "/api/replies",
            {"upstream_ip": "198.51.100.7", "upstream_port": 443, "public_port": 40000},
        )
        self.assertEqual(replied.get_json()["result"]["outcome"], "delivered")

        closed = self.post(f"/api/connections/{conn['id']}/close", {})
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.get_json()["result"]["cooldown"]["public_port"], 40000)

        late = self.post(
            "/api/replies",
            {"upstream_ip": "198.51.100.7", "upstream_port": 443, "public_port": 40000},
        )
        self.assertEqual(late.get_json()["result"]["reason_code"], "COOLDOWN_LATE")

        advanced = self.post("/api/clock/advance", {"seconds": 60})
        self.assertEqual(advanced.get_json()["snapshot"]["cooldowns"], [])

        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["clock"]["now"], 60)
        self.assertEqual(state["summary"]["active_connections"], 0)

    def test_exhaustion_returns_409_with_counts(self):
        payload = {
            "internal_ip": "10.0.0.21",
            "internal_port": 51515,
            "upstream_ip": "198.51.100.7",
            "upstream_port": 443,
        }
        self.assertEqual(self.post("/api/connections", payload).status_code, 200)
        self.assertEqual(self.post("/api/connections", payload).status_code, 200)
        response = self.post("/api/connections", payload)
        self.assertEqual(response.status_code, 409)
        error = response.get_json()["error"]
        self.assertEqual(error["code"], "NO_PORT_AVAILABLE")
        self.assertEqual(error["details"]["active_count"], 2)
        self.assertEqual(error["details"]["capacity"], 2)

    def test_bad_payloads_are_400(self):
        response = self.post("/api/connections", {"internal_ip": "nope"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_INPUT")

        response = self.client.post(
            "/api/config",
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_JSON")

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").get_json(), {"status": "ok"})

    def test_env_cooldown_validation(self):
        import os

        from natlab import _cooldown_from_env

        old = os.environ.get("NATLAB_COOLDOWN_SECONDS")
        try:
            os.environ["NATLAB_COOLDOWN_SECONDS"] = "15"
            self.assertEqual(_cooldown_from_env(60), 15)
            os.environ["NATLAB_COOLDOWN_SECONDS"] = ""
            self.assertEqual(_cooldown_from_env(60), 60)
            for bad in ("abc", "-5"):
                os.environ["NATLAB_COOLDOWN_SECONDS"] = bad
                with self.subTest(value=bad):
                    with self.assertRaises(RuntimeError):
                        _cooldown_from_env(60)
        finally:
            if old is None:
                os.environ.pop("NATLAB_COOLDOWN_SECONDS", None)
            else:
                os.environ["NATLAB_COOLDOWN_SECONDS"] = old

    def test_state_reports_cooldown_seconds(self):
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["clock"]["cooldown_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
