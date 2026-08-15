#!/usr/bin/env python3
"""P3 路由冒烟测试：真实 aiohttp 挂载 server_routes，mock ImmichClient。

覆盖前端面板用到的端点与鉴权/校验：
- GET /config（信任模式 / token 模式 401 / Bearer 通过）
- GET /buckets、/bucket（时间轴）
- GET thumbnail（?token= query 放行 / 无 token 拒绝）
- PUT /config（SSRF 校验 400 / 合法保存 / page_size 下界）
用法: python3 tests/test_routes.py
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server_routes  # noqa: E402
import config as config_mod  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


class FakeClient:
    """mock ImmichClient：不触网，字段形状对齐 v2 实测。"""

    def __init__(self, ext_map=None, ext_error=None, version_error=None, buckets_error=None):
        self.calls = []
        self._ext_map = ext_map
        self._ext_error = ext_error
        self._version_error = version_error
        self._buckets_error = buckets_error

    def version(self):
        self.calls.append(("version",))
        if self._version_error:
            raise self._version_error
        return {"major": 2, "minor": 7, "patch": 5}

    def get_buckets(self, order="desc"):
        self.calls.append(("buckets", order))
        if self._buckets_error:
            raise self._buckets_error
        return [{"timeBucket": "2026-08-01", "count": 558}, {"timeBucket": "2026-07-01", "count": 120}]

    def get_bucket(self, time_bucket, order="desc"):
        self.calls.append(("bucket", time_bucket, order))
        return [{"id": "asset-1", "fileCreatedAt": "2026-08-01T10:00:00.000Z", "type": "IMAGE", "mimeType": "image/jpeg"}]

    def asset_ext_map(self):
        self.calls.append(("ext_map",))
        if self._ext_error:
            raise self._ext_error
        return dict(self._ext_map or {})

    def get_thumbnail(self, asset_id, size, cond_headers=None):
        self.calls.append(("thumbnail", asset_id, size))
        # raw=None 时 _iter_upstream 会按断流结束；close 需存在（finally 调用）
        return types.SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "image/jpeg", "ETag": '"abc"'},
            raw=None,
            close=lambda: None,
        )

    def get_original(self, asset_id, cond_headers=None):
        self.calls.append(("original", asset_id))
        return types.SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "video/mp4", "Content-Length": "123"},
            raw=None,
            close=lambda: None,
        )

    def update_assets_bulk(self, asset_ids, is_favorite):
        self.calls.append(("bulk_update", asset_ids, is_favorite))
        if getattr(self, "fail_bulk", False):
            return {"updated": 0, "failed": list(asset_ids)}
        return {"updated": len(asset_ids), "failed": []}

    def close(self):
        pass


def build_app(tmpdir, fake_client=None):
    """构造挂载好路由的 aiohttp app（config 单例指向临时路径）。"""
    config_mod._default_config = config_mod.ConfigManager(os.path.join(tmpdir, "config.json"))
    # 测试客户端从 loopback 连入，remote 恒为 127.0.0.1；关闭本机信任，
    # 保持现有测试的"纯 token 校验"语义（本机信任逻辑单独在 11 段验证）
    server_routes._TRUST_LOCALHOST = False
    routes = web.RouteTableDef()
    fake_server = types.SimpleNamespace(routes=routes)
    server_routes.setup_routes(fake_server)
    app = web.Application()
    app.add_routes(routes)
    if fake_client is None:
        fake_client = FakeClient()
    server_routes._get_client = lambda cfg: fake_client
    # 重置模块级 ext 缓存：不同测试段用不同 FakeClient，缓存不能跨段共享
    server_routes._ext_cache.update({"ts": 0.0, "map": {}})
    return app, fake_client


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        app, fake = build_app(tmp)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # ---- 1. 信任模式（未配置 panel_token） ----
            r = await client.get("/api/immich_plus/config")
            check("信任模式 GET /config 200", r.status == 200)
            data = await r.json()
            check("config 半脱敏", data.get("api_key_configured") is False and "api_key" not in data)

            r = await client.get("/api/immich_plus/health")
            hd = await r.json()
            check("health 未配置 key stage=config", r.status == 200 and hd.get("stage") == "config" and hd.get("ok") is False)

            r = await client.get("/api/immich_plus/buckets")
            check("GET /buckets 200", r.status == 200 and len(await r.json()) == 2)

            r = await client.get("/api/immich_plus/bucket?timeBucket=2026-08-01")
            check("GET /bucket 200", r.status == 200 and (await r.json())[0]["id"] == "asset-1")

            # ---- 2. thumbnail ?token= 与信任模式 ----
            r = await client.get("/api/immich_plus/assets/asset-1/thumbnail?size=preview")
            check("thumbnail 信任模式 200", r.status == 200 and r.headers.get("ETag") == '"abc"')

            # ---- 3. 开启 panel_token 后 ----
            r = await client.put("/api/immich_plus/config", json={"panel_token": "tok123"})
            check("PUT panel_token 200", r.status == 200)

            r = await client.get("/api/immich_plus/config")
            check("token 模式无 header 401", r.status == 401)

            r = await client.get("/api/immich_plus/config", headers={"Authorization": "Bearer tok123"})
            check("token 模式 Bearer 200", r.status == 200)

            r = await client.get("/api/immich_plus/assets/asset-1/thumbnail", headers={"Authorization": "Bearer tok123"})
            check("thumbnail header 鉴权 200", r.status == 200)

            # query token 一律拒绝（token 不进 URL，防日志泄漏）
            r = await client.get("/api/immich_plus/assets/asset-1/thumbnail?size=preview&token=tok123")
            check("thumbnail query token 拒绝", r.status == 401)

            r = await client.get("/api/immich_plus/buckets?token=tok123")
            check("buckets query token 拒绝", r.status == 401)

            # ---- 4. PUT /config 校验 ----
            r = await client.put(
                "/api/immich_plus/config",
                json={"base_url": "file:///etc/passwd", "api_key": "x"},
                headers={"Authorization": "Bearer tok123"},
            )
            check("非法 base_url 400", r.status == 400)

            r = await client.put(
                "/api/immich_plus/config",
                json={"base_url": "http://169.254.169.254/api", "api_key": "x"},
                headers={"Authorization": "Bearer tok123"},
            )
            check("metadata base_url 400", r.status == 400)

            r = await client.put(
                "/api/immich_plus/config",
                json={"base_url": "http://2130706433:2283/api", "api_key": "x"},
                headers={"Authorization": "Bearer tok123"},
            )
            check("IP 混淆 base_url 400", r.status == 400)

            r = await client.put(
                "/api/immich_plus/config",
                json={"unknown_field": "x", "page_size": 50},
                headers={"Authorization": "Bearer tok123"},
            )
            check("未知字段被忽略 200", r.status == 200)
            unk = await r.json()
            check("未知字段不入库", "unknown_field" not in unk, f"-> {sorted(unk.keys())}")
            check("合法字段仍生效", unk.get("page_size") == 50, f"-> {unk.get('page_size')}")

            r = await client.put(
                "/api/immich_plus/config",
                json={"timeline_range": "30d"},
                headers={"Authorization": "Bearer tok123"},
            )
            check("非法 timeline_range 400", r.status == 400)

            r = await client.put(
                "/api/immich_plus/config",
                json={"timeline_interval": "5m"},
                headers={"Authorization": "Bearer tok123"},
            )
            check("非法 timeline_interval 400", r.status == 400)

            r = await client.put(
                "/api/immich_plus/config",
                json={"timeline_range": "3d", "timeline_interval": "30m"},
                headers={"Authorization": "Bearer tok123"},
            )
            check("合法 timeline 配置 200", r.status == 200)
            tl = await r.json()
            check("timeline_range 保存", tl.get("timeline_range") == "3d", f"-> {tl.get('timeline_range')}")
            check("timeline_interval 保存", tl.get("timeline_interval") == "30m", f"-> {tl.get('timeline_interval')}")

            r = await client.put(
                "/api/immich_plus/config",
                json={"page_size": 0},
                headers={"Authorization": "Bearer tok123"},
            )
            check("page_size clamp 200", r.status == 200)
            clamped = await r.json()
            check("page_size 下界=1", clamped.get("page_size") == 1, f"-> {clamped.get('page_size')}")

            r = await client.put(
                "/api/immich_plus/config",
                json={"base_url": "http://192.168.1.50:2283/api", "api_key": "real-key-1", "page_size": 0},
                headers={"Authorization": "Bearer tok123"},
            )
            check("合法 base_url 200", r.status == 200)
            saved = await r.json()
            check("page_size 下界=1", saved.get("page_size") == 1, f"-> {saved.get('page_size')}")
            check("回传不含 key", "real-key-1" not in json.dumps(saved))

            # api_key 落盘到临时 config.json
            r = await client.get("/api/immich_plus/config", headers={"Authorization": "Bearer tok123"})
            d = await r.json()
            check("api_key_configured=True", d.get("api_key_configured") is True)
            check("base_url 更新生效", d.get("base_url") == "http://192.168.1.50:2283/api")

            # health 全通：version + buckets（FakeClient 正常路径）→ ok + 资产统计
            r = await client.get("/api/immich_plus/health", headers={"Authorization": "Bearer tok123"})
            hd = await r.json()
            check("health 全通 ok=true", r.status == 200 and hd.get("ok") is True)
            check("health 返回版本", hd.get("version", {}).get("major") == 2, f"-> {hd.get('version')}")
            check("health 资产统计", hd.get("buckets_count") == 2 and hd.get("assets_count") == 678, f"-> buckets={hd.get('buckets_count')} assets={hd.get('assets_count')}")

            # ---- 清空配置（reset 回出厂状态） ----
            r = await client.post("/api/immich_plus/config/reset", headers={"Authorization": "Bearer tok123"})
            check("reset 200", r.status == 200)
            rj = await r.json()
            check("reset 后 api_key 清空", rj.get("api_key_configured") is False)
            check("reset 后 panel_token 清空", rj.get("panel_token_configured") is False)
            check("reset 后 base_url 恢复默认", rj.get("base_url") == "http://127.0.0.1:2283/api", f"-> {rj.get('base_url')}")
            check("reset 后 default_album 清空", rj.get("default_album") == "")

            # reset 后回到信任模式（token 已清，无 header 也放行）
            r2 = await client.get("/api/immich_plus/config")
            check("reset 后无 token 放行", r2.status == 200)
        finally:
            await client.close()

    # ---- 5. bucket 补 ext（格式角标） ----
    with tempfile.TemporaryDirectory() as tmp:
        fc = FakeClient(ext_map={"asset-1": "png"})
        app, fake = build_app(tmp, fc)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/immich_plus/bucket?timeBucket=2026-08-01")
            check("bucket 带 ext 200", r.status == 200)
            data = await r.json()
            check("bucket ext 注入", data[0].get("ext") == "png", f"-> {data[0]!r}")
            # TTL 缓存：第二次请求不重复拉 ext_map
            r2 = await client.get("/api/immich_plus/bucket?timeBucket=2026-08-01")
            ext_calls = [c for c in fc.calls if c[0] == "ext_map"]
            check("ext 缓存生效（只拉一次）", len(ext_calls) == 1, f"-> {ext_calls!r}")

            # original 路由（视频代理）
            r3 = await client.get("/api/immich_plus/assets/asset-1/original")
            check("original 路由 200", r3.status == 200)
            check("original Content-Type 透传", r3.headers.get("Content-Type") == "video/mp4")
        finally:
            await client.close()

    # ---- 6. ext_map 异常兜底：不阻断时间轴 ----
    with tempfile.TemporaryDirectory() as tmp:
        # 模拟 asset_ext_map 抛非 ImmichError（如 JSONDecodeError）
        fc = FakeClient(ext_error=ValueError("bad json"))
        app, fake = build_app(tmp, fc)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/immich_plus/bucket?timeBucket=2026-08-01")
            check("ext 异常仍 200", r.status == 200, f"-> {r.status}")
            data = await r.json()
            check("ext 异常无 ext 字段", "ext" not in data[0], f"-> {data[0]!r}")
        finally:
            await client.close()

    # ---- 7. client 无 asset_ext_map 方法：getattr 容错 ----
    with tempfile.TemporaryDirectory() as tmp:
        class BareClient:
            """只有桶方法，没有 asset_ext_map（模拟旧/第三方 client）。"""

            def get_bucket(self, time_bucket, order="desc"):
                return [{"id": "asset-1"}]

        fc = BareClient()
        app, fake = build_app(tmp, fc)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/immich_plus/bucket?timeBucket=2026-08-01")
            check("无 ext_map 方法仍 200", r.status == 200)
            data = await r.json()
            check("无 ext_map 方法无 ext", "ext" not in data[0])
        finally:
            await client.close()

    # ---- 8. 批量收藏端点（v0.6）：PUT /assets {ids, isFavorite} ----
    with tempfile.TemporaryDirectory() as tmp:
        app, fake = build_app(tmp)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await client.put("/api/immich_plus/config", json={"panel_token": "tok123"})

            r = await client.put("/api/immich_plus/assets", json={"ids": ["a1", "a2"], "isFavorite": True})
            check("批量收藏无 token 401", r.status == 401)

            r = await client.put("/api/immich_plus/assets",
                                 json={"ids": ["a1", "a2"], "isFavorite": True},
                                 headers={"Authorization": "Bearer tok123"})
            check("批量收藏 200", r.status == 200)
            data = await r.json()
            check("批量收藏 updated=2 failed=[]", data.get("updated") == 2 and data.get("failed") == [], f"-> {data}")
            calls = [c for c in fake.calls if c[0] == "bulk_update"]
            check("bulk_update 收到 ids+fav", len(calls) == 1 and calls[0][1] == ["a1", "a2"] and calls[0][2] is True,
                  f"-> {calls}")

            r = await client.put("/api/immich_plus/assets",
                                 json={"ids": ["a1"], "isFavorite": "true"},
                                 headers={"Authorization": "Bearer tok123"})
            check("isFavorite 非布尔 400", r.status == 400)

            r = await client.put("/api/immich_plus/assets",
                                 json={"ids": [], "isFavorite": True},
                                 headers={"Authorization": "Bearer tok123"})
            check("空 ids 400", r.status == 400)

            r = await client.put("/api/immich_plus/assets",
                                 json={"ids": [str(i) for i in range(1001)], "isFavorite": True},
                                 headers={"Authorization": "Bearer tok123"})
            check("超 1000 ids 400", r.status == 400)

            # 全部失败 → 502（部分成功仍 200 带 failed 明细）
            fake.fail_bulk = True
            r = await client.put("/api/immich_plus/assets",
                                 json={"ids": ["x"], "isFavorite": True},
                                 headers={"Authorization": "Bearer tok123"})
            check("全部失败 502", r.status == 502)
        finally:
            await client.close()

    # ---- 9. 面板令牌端点（panel-token）：生成/显示/重新生成/清除 ----
    with tempfile.TemporaryDirectory() as tmp:
        app, fake = build_app(tmp)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # 9.1 信任模式：POST 生成（无需鉴权）
            r = await client.post("/api/immich_plus/panel-token")
            check("无 token 时生成 200", r.status == 200)
            d = await r.json()
            tok1 = d.get("token", "")
            check("生成返回 64 位 hex 令牌", isinstance(tok1, str) and len(tok1) == 64, f"-> {tok1!r}")

            # 生成后立即上锁：后续请求需 Bearer
            r = await client.get("/api/immich_plus/config")
            check("生成后无 header 401", r.status == 401)
            r = await client.get("/api/immich_plus/config", headers={"Authorization": "Bearer " + tok1})
            check("生成后 Bearer 200", r.status == 200)
            d = await r.json()
            check("config 显示 panel_token_configured", d.get("panel_token_configured") is True)

            # 9.2 GET 显示（需鉴权）
            r = await client.get("/api/immich_plus/panel-token")
            check("显示无 token 401", r.status == 401)
            r = await client.get("/api/immich_plus/panel-token", headers={"Authorization": "Bearer " + tok1})
            check("显示 200", r.status == 200)
            d = await r.json()
            check("显示返回同一令牌", d.get("token") == tok1, f"-> {d}")

            # 9.3 重新生成：旧 token 作废
            r = await client.post("/api/immich_plus/panel-token")
            check("重新生成无 header 401", r.status == 401)
            r = await client.post("/api/immich_plus/panel-token", headers={"Authorization": "Bearer " + tok1})
            check("重新生成带旧 token 200", r.status == 200)
            d = await r.json()
            tok2 = d.get("token", "")
            check("新令牌不同于旧令牌", bool(tok2) and tok2 != tok1, f"-> {tok2!r}")
            r = await client.get("/api/immich_plus/config", headers={"Authorization": "Bearer " + tok1})
            check("旧令牌失效 401", r.status == 401)
            r = await client.get("/api/immich_plus/config", headers={"Authorization": "Bearer " + tok2})
            check("新令牌可用 200", r.status == 200)

            # 9.4 清除：回到信任模式（需鉴权）
            r = await client.delete("/api/immich_plus/panel-token")
            check("清除无 header 401", r.status == 401)
            r = await client.delete("/api/immich_plus/panel-token", headers={"Authorization": "Bearer " + tok2})
            check("清除 200", r.status == 200)
            r = await client.get("/api/immich_plus/config")
            check("清除后回到信任模式 200", r.status == 200)
            d = await r.json()
            check("清除后 panel_token_configured False", d.get("panel_token_configured") is False)

            # 9.5 清除后 GET 显示 → 400（未生成）
            r = await client.get("/api/immich_plus/panel-token")
            check("清除后显示 400", r.status == 400)
        finally:
            await client.close()

    # ---- 10. PUT /config 自动生成令牌（连接配置好后仍无 token） ----
    with tempfile.TemporaryDirectory() as tmp:
        app, fake = build_app(tmp)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.put(
                "/api/immich_plus/config",
                json={"base_url": "http://192.168.1.50:2283/api", "api_key": "auto-key"},
            )
            check("保存连接配置 200", r.status == 200)
            d = await r.json()
            tok = d.get("panel_token_plain", "")
            check("自动生成令牌（明文一次性下发）", isinstance(tok, str) and len(tok) == 64, f"-> {tok!r}")
            check("响应含 panel_token_configured True", d.get("panel_token_configured") is True)

            # 自动生成后上锁
            r = await client.get("/api/immich_plus/config")
            check("自动生成后无 header 401", r.status == 401)
            r = await client.get("/api/immich_plus/config", headers={"Authorization": "Bearer " + tok})
            check("自动生成后 Bearer 200", r.status == 200)

            # 再次保存（已有 token）不重复生成、不下发明文
            r = await client.put(
                "/api/immich_plus/config",
                json={"timeline_range": "7d"},
                headers={"Authorization": "Bearer " + tok},
            )
            check("再次保存 200", r.status == 200)
            d2 = await r.json()
            check("已有 token 不下发明文", "panel_token_plain" not in d2 and d2.get("panel_token_configured") is True)

            # 未配置 api_key 时不自动生成
            with tempfile.TemporaryDirectory() as tmp2:
                app2, fake2 = build_app(tmp2)
                client2 = TestClient(TestServer(app2))
                await client2.start_server()
                try:
                    r2 = await client2.put(
                        "/api/immich_plus/config",
                        json={"base_url": "http://192.168.1.50:2283/api"},
                    )
                    check("无 api_key 不自动生成", r2.status == 200)
                    d3 = await r2.json()
                    check("无 api_key 无明文", "panel_token_plain" not in d3 and d3.get("panel_token_configured") is False)
                finally:
                    await client2.close()
        finally:
            await client.close()

    # ---- 11. 本机 localhost 免令牌（_is_localhost / _check_auth） ----
    with tempfile.TemporaryDirectory() as tmp:
        cfg = config_mod.ConfigManager(os.path.join(tmp, "config.json"))
        cfg.update(panel_token="secret-tok")

        # _is_localhost 纯函数判定
        check("_is_localhost 127.0.0.1", server_routes._is_localhost("127.0.0.1") is True)
        check("_is_localhost ::1", server_routes._is_localhost("::1") is True)
        check("_is_localhost ::ffff:127.0.0.1", server_routes._is_localhost("::ffff:127.0.0.1") is True)
        check("_is_localhost 局域网 IP False", server_routes._is_localhost("192.168.1.50") is False)
        check("_is_localhost ::ffff:局域网 False", server_routes._is_localhost("::ffff:192.168.1.99") is False)
        check("_is_localhost None False", server_routes._is_localhost(None) is False)
        check("_is_localhost 空串 False", server_routes._is_localhost("") is False)

        def fake_req(remote, headers=None):
            return types.SimpleNamespace(remote=remote, headers=headers or {})

        # 生产默认 _TRUST_LOCALHOST=True
        server_routes._TRUST_LOCALHOST = True
        check("本机无 header 放行（即使已配 token）",
              server_routes._check_auth(fake_req("127.0.0.1"), cfg) is True)
        check("本机 IPv6 放行",
              server_routes._check_auth(fake_req("::1"), cfg) is True)
        check("局域网无 header 拒绝（已配 token）",
              server_routes._check_auth(fake_req("192.168.1.50"), cfg) is False)
        check("局域网正确 Bearer 放行",
              server_routes._check_auth(fake_req("192.168.1.50", {"Authorization": "Bearer secret-tok"}), cfg) is True)
        check("局域网错误 Bearer 拒绝",
              server_routes._check_auth(fake_req("192.168.1.50", {"Authorization": "Bearer wrong"}), cfg) is False)

        # 开关关闭（如测试）后本机也要令牌
        server_routes._TRUST_LOCALHOST = False
        check("关闭本机信任后本机无 header 拒绝",
              server_routes._check_auth(fake_req("127.0.0.1"), cfg) is False)
        check("关闭本机信任后本机 Bearer 放行",
              server_routes._check_auth(fake_req("127.0.0.1", {"Authorization": "Bearer secret-tok"}), cfg) is True)
        server_routes._TRUST_LOCALHOST = True  # 恢复生产默认

    # ---- 12. health 分阶段校验：connect（地址不可达）/ auth（API Key 无效） ----
    with tempfile.TemporaryDirectory() as tmp:
        conn_err = server_routes.ImmichError("Immich 连接失败: boom", None)
        auth_err = server_routes.ImmichError("Immich GET /timeline/buckets -> HTTP 401: Invalid API key", 401)
        fc = FakeClient(version_error=conn_err)
        app, fake = build_app(tmp, fc)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await client.put(
                "/api/immich_plus/config",
                json={"base_url": "http://127.0.0.1:9999/api", "api_key": "k", "panel_token": "tok123"},
            )
            auth_hdr = {"Authorization": "Bearer tok123"}
            # version 抛连接错误（status_code=None）→ stage=connect
            r = await client.get("/api/immich_plus/health", headers=auth_hdr)
            hd = await r.json()
            check("health 连接失败 stage=connect",
                  r.status == 200 and hd.get("ok") is False and hd.get("stage") == "connect" and "地址" in hd.get("error", ""),
                  f"-> {hd}")
            # version 通了，buckets 401 → stage=auth
            fake._version_error = None
            fake._buckets_error = auth_err
            r = await client.get("/api/immich_plus/health", headers=auth_hdr)
            hd = await r.json()
            check("health key 无效 stage=auth",
                  r.status == 200 and hd.get("ok") is False and hd.get("stage") == "auth" and "API Key" in hd.get("error", ""),
                  f"-> {hd}")
        finally:
            await client.close()

    # 未污染真实 config.json
    check("测试未污染真实 config.json", not os.path.exists(os.path.join(REPO_ROOT, "config.json")))


import asyncio  # noqa: E402

asyncio.run(main())

fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
