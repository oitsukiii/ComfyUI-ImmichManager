#!/usr/bin/env python3
"""P1 修复自测：config 半脱敏 / api_key 防覆盖 / panel_token 鉴权逻辑。

不依赖真实 Immich；独立于 tests/test_client.py（连真实 Immich 的集成测试）。
用法: python3 tests/test_config.py
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from config import ConfigManager  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


# --- 1. 半脱敏 ---
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "config.json")
    cm = ConfigManager(path)
    cm.update(api_key="secret-immich-key-123", base_url="http://127.0.0.1:2283/api")
    pub = cm.public_config()
    check("public_config 含 api_key_configured", "api_key_configured" in pub)
    check("public_config 为布尔", isinstance(pub.get("api_key_configured"), bool) and pub["api_key_configured"] is True)
    check("public_config 不含真实 key", "secret-immich-key-123" not in json.dumps(pub))
    check("public_config 不含 api_key 字段", "api_key" not in pub)
    check("public_config 不含 panel_token 字段", "panel_token" not in pub)
    check("public_config 回显 base_url", pub.get("base_url") == "http://127.0.0.1:2283/api")

    # --- 2. api_key 防覆盖（前端误回传） ---
    cm.update(api_key="True")   # 模拟前端把布尔 true 转字符串回传
    check("sanitize 忽略 'True'", cm.get("api_key") == "secret-immich-key-123")
    cm.update(api_key=True)     # 布尔直接回传
    check("sanitize 忽略布尔", cm.get("api_key") == "secret-immich-key-123")
    cm.update(api_key="")       # 空串
    check("sanitize 忽略空串", cm.get("api_key") == "secret-immich-key-123")
    cm.update(api_key="  ")     # 纯空白
    check("sanitize 忽略空白", cm.get("api_key") == "secret-immich-key-123")
    cm.update(api_key="new-real-key-456")  # 真实 key 可更新
    check("sanitize 接受真实 key", cm.get("api_key") == "new-real-key-456")

    # --- 3. 落盘权限 0o600 ---
    mode = os.stat(path).st_mode & 0o777
    check("config.json 权限 0o600", mode == 0o600, f"-> {oct(mode)}")
    check("config.json 内容含新 key", "new-real-key-456" in open(path, encoding="utf-8").read())

    # --- 4. panel_token 半脱敏 ---
    cm.update(panel_token="panel-secret-xyz")
    pub2 = cm.public_config()
    check("panel_token_configured 为布尔", isinstance(pub2.get("panel_token_configured"), bool) and pub2["panel_token_configured"] is True)
    check("public_config 不含 panel_token 真实值", "panel-secret-xyz" not in json.dumps(pub2))

    # --- 5. 持久化重载 ---
    cm2 = ConfigManager(path)
    check("重载保持配置", cm2.get("api_key") == "new-real-key-456" and cm2.get("panel_token") == "panel-secret-xyz")

# --- 5a. base_url 校验（SSRF 防线） ---
from config import normalize_base_url  # noqa: E402

check("base_url http 通过", normalize_base_url("http://127.0.0.1:2283/api") == "http://127.0.0.1:2283/api")
check("base_url https 通过", normalize_base_url("https://immich.example.com/api/") == "https://immich.example.com/api")
check("base_url 私网通过（局域网场景）", normalize_base_url("http://192.168.1.50:2283/api") == "http://192.168.1.50:2283/api")
check("base_url 域名通过", normalize_base_url("http://localhost:2283/api") == "http://localhost:2283/api")
for bad in ["file:///etc/passwd", "ftp://x", "javascript://x", "//nohost", "127.0.0.1:2283", ""]:
    try:
        normalize_base_url(bad)
        check(f"base_url 拒绝 {bad!r}", False)
    except ValueError:
        check(f"base_url 拒绝 {bad!r}", True)
# IP 混淆/危险地址拒绝（SSRF 加固，审小爪 P2-1 第二轮）
for bad in [
    "http://2130706433:2283/api",          # 十进制 = 127.0.0.1
    "http://0x7f000001:2283/api",          # 十六进制 = 127.0.0.1
    "http://017700000001:2283/api",        # 八进制 = 127.0.0.1
    "http://evil.com@127.0.0.1:2283/api",  # userinfo 混淆
    "http://[::1]:2283/api",               # IPv6 loopback
    "http://169.254.169.254/api",          # 云 metadata（link-local）
    "http://0.0.0.0:2283/api",             # 未指定地址
    "http://224.0.0.1:2283/api",           # 组播
    "http://0251.0376.0251.0376/api",      # 点分八进制 = 169.254.169.254（metadata！）
    "http://0177.0.0.1:2283/api",          # 点分八进制 = 127.0.0.1
    "http://0x7f.0.0.1:2283/api",          # 点分十六进制 = 127.0.0.1
    "http://127.1:2283/api",               # 少段 = 127.0.0.1
    "http://127.000.000.001:2283/api",     # 前导零 = 127.0.0.1
]:
    try:
        normalize_base_url(bad)
        check(f"base_url 拒绝混淆 {bad!r}", False)
    except ValueError:
        check(f"base_url 拒绝混淆 {bad!r}", True)
# 合法 hex 主机名/域名不被误伤（审小爪 P2-1 第二轮）
for good in [
    "http://cafe:2283/api",
    "http://deadbeef:2283/api",
    "http://cafe.local:2283/api",
    "http://nas-beef:2283/api",
]:
    try:
        normalize_base_url(good)
        check(f"base_url 允许主机名 {good!r}", True)
    except ValueError:
        check(f"base_url 允许主机名 {good!r}", False)

# update() 非法 base_url 抛错且不落盘
with tempfile.TemporaryDirectory() as tmp:
    path2 = os.path.join(tmp, "c.json")
    cm3 = ConfigManager(path2)
    try:
        cm3.update(base_url="file:///etc/passwd")
        check("update 拒绝非法 base_url", False)
    except ValueError:
        check("update 拒绝非法 base_url", True)
    check("非法 base_url 未落盘", cm3.get("base_url") == "http://127.0.0.1:2283/api")

# config.json 被手工编辑出非法 base_url → 回退默认
with tempfile.TemporaryDirectory() as tmp:
    path3 = os.path.join(tmp, "c.json")
    with open(path3, "w", encoding="utf-8") as fh:
        json.dump({"base_url": "ftp://bad"}, fh)
    cm4 = ConfigManager(path3)
    check("坏 base_url 回退默认", cm4.get("base_url") == "http://127.0.0.1:2283/api")

# --- 5c. 代理配置（proxy_mode / proxy_url，P6） ---
from config import (  # noqa: E402
    ALLOWED_PROXY_MODES,
    ConfigManager as _CM,
    detect_env_proxy,
    mask_credentials,
    normalize_proxy_url,
    resolve_proxy_settings,
)

# 默认值：不使用代理（修复"启动脚本设了 HTTP_PROXY、代理又没运行"导致局域网请求被劫持）
with tempfile.TemporaryDirectory() as tmp:
    cm5 = _CM(os.path.join(tmp, "c.json"))
    check("proxy_mode 默认 off", cm5.get("proxy_mode") == "off")
    check("proxy_url 默认空", cm5.get("proxy_url") == "")
    pub = cm5.public_config()
    check("public_config 带 proxy_mode/proxy_url", pub.get("proxy_mode") == "off" and pub.get("proxy_url") == "")
    check("public_config 带 proxy_env 字段", "proxy_env" in pub)
    check("public_config 不回传 api_key 明文", "api_key" not in pub and pub.get("api_key_configured") is False)

# 合法代理地址
for good, want in (
    ("http://127.0.0.1:7897", "http://127.0.0.1:7897"),
    ("https://proxy.lan:8080/", "https://proxy.lan:8080"),
    ("  http://192.168.2.50:7890  ", "http://192.168.2.50:7890"),
    ("http://proxy.internal", "http://proxy.internal"),
):
    try:
        check(f"proxy_url 接受 {good!r}", normalize_proxy_url(good) == want)
    except ValueError as exc:
        check(f"proxy_url 接受 {good!r}", False, f"抛错 {exc}")

# 非法代理地址（协议/凭据/路径/危险 IP/混淆字面量/类型）
for bad, label in (
    ("", "空串"),
    ("   ", "空白串"),
    (None, "None"),
    ("socks5://127.0.0.1:1080", "socks5 协议（无 PySocks 依赖）"),
    ("ftp://127.0.0.1:21", "ftp 协议"),
    ("127.0.0.1:7897", "缺协议"),
    ("http://", "缺主机"),
    ("http://user:pass@127.0.0.1:7897", "带用户名密码"),
    ("http://127.0.0.1:7897/proxy", "带路径"),
    ("http://127.0.0.1:7897?a=b", "带参数"),
    ("http://169.254.169.254:80", "link-local（云 metadata）"),
    ("http://0.0.0.0:7897", "未指定地址"),
    ("http://0x7f000001:7897", "IP 混淆字面量"),
):
    try:
        normalize_proxy_url(bad)
        check(f"proxy_url 拒绝 {label}", False)
    except ValueError:
        check(f"proxy_url 拒绝 {label}", True)

# update()：非法 mode / 非法地址抛错；空地址=不改动
with tempfile.TemporaryDirectory() as tmp:
    cm6 = _CM(os.path.join(tmp, "c.json"))
    try:
        cm6.update(proxy_mode="bogus")
        check("update 拒绝非法 proxy_mode", False)
    except ValueError:
        check("update 拒绝非法 proxy_mode", True)
    check("非法 proxy_mode 未落盘", cm6.get("proxy_mode") == "off")
    cm6.update(proxy_mode="custom", proxy_url="http://127.0.0.1:7897")
    check("update 保存合法代理", cm6.get("proxy_mode") == "custom" and cm6.get("proxy_url") == "http://127.0.0.1:7897")
    cm6.update(proxy_url="")
    check("空 proxy_url 不改动已存地址", cm6.get("proxy_url") == "http://127.0.0.1:7897")
    cm6.update(proxy_mode="off")
    check("切回 off 保留已填地址（便于切回）", cm6.get("proxy_url") == "http://127.0.0.1:7897")
    try:
        cm6.update(proxy_url="socks5://127.0.0.1:1080")
        check("update 拒绝非法 proxy_url", False)
    except ValueError:
        check("update 拒绝非法 proxy_url", True)
    check("非法 proxy_url 未覆盖原值", cm6.get("proxy_url") == "http://127.0.0.1:7897")

# resolve_proxy_settings：脏组合一律降级 off
check("resolve: custom+空 → off", resolve_proxy_settings("custom", "") == ("off", ""))
check("resolve: custom+非法 → off（地址保留）", resolve_proxy_settings("custom", "socks5://x:1") == ("off", "socks5://x:1"))
check("resolve: custom+合法 → custom", resolve_proxy_settings("custom", "http://127.0.0.1:7897") == ("custom", "http://127.0.0.1:7897"))
check("resolve: env 保持 env", resolve_proxy_settings("env", "")[0] == "env")
check("resolve: 非法 mode → off", resolve_proxy_settings("bogus", "http://a:1")[0] == "off")
check("resolve: 非字符串地址不炸", resolve_proxy_settings("custom", None) == ("off", ""))
check("resolve: 覆盖全部白名单 mode", ALLOWED_PROXY_MODES == {"off", "custom", "env"})

# config.json 被手工编辑成 custom 但没地址 → 载入时降级 off
with tempfile.TemporaryDirectory() as tmp:
    path5 = os.path.join(tmp, "c.json")
    with open(path5, "w", encoding="utf-8") as fh:
        json.dump({"proxy_mode": "custom", "proxy_url": ""}, fh)
    cm7 = _CM(path5)
    check("脏代理组合载入降级 off", cm7.get("proxy_mode") == "off")
    with open(path5, "w", encoding="utf-8") as fh:
        json.dump({"proxy_mode": "custom", "proxy_url": "http://127.0.0.1:7897"}, fh)
    cm8 = _CM(path5)
    check("合法代理组合载入保留", cm8.get("proxy_mode") == "custom")

# 凭据遮罩 + 环境变量检测
check("mask_credentials 遮罩 userinfo", mask_credentials("http://u:p@h:1") == "http://***@h:1")
check("mask_credentials 无凭据原样", mask_credentials("http://127.0.0.1:7897") == "http://127.0.0.1:7897")
_saved_proxy = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")}
try:
    for k in _saved_proxy:
        os.environ.pop(k, None)
    check("detect_env_proxy 无变量返回空", detect_env_proxy() == "")
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
    check("detect_env_proxy 读到变量", detect_env_proxy() == "http://127.0.0.1:7897")
    os.environ["HTTP_PROXY"] = "http://u:secret@127.0.0.1:7897"
    check("detect_env_proxy 遮罩凭据", "secret" not in detect_env_proxy() and detect_env_proxy() == "http://***@127.0.0.1:7897")
finally:
    for k, v in _saved_proxy.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# --- 5b. 共享单例（节点与路由同实例） ---
# ⚠️ 先把单例指到临时路径，避免测试 update 污染真实 config.json
import config as config_mod  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    config_mod._default_config = ConfigManager(os.path.join(tmp, "c.json"))
    from config import get_config  # noqa: E402

    c1 = get_config()
    c2 = get_config()
    check("get_config 单例", c1 is c2)
    import upload_node  # noqa: E402

    check("upload_node 用共享单例", upload_node._CONFIG is get_config())
    # 面板更新配置 → 节点读同一实例的值（等效面板 PUT /config 后节点即时生效）
    get_config().update(api_key="shared-key-999")
    check("共享单例更新节点可见", upload_node._CONFIG.get("api_key") == "shared-key-999")
    # 真实 config.json 不应被创建
    check("测试未污染真实 config.json", not os.path.exists(os.path.join(REPO_ROOT, "config.json")))

# --- 6. server_routes 鉴权逻辑 ---
# 纯函数（config.py，无 aiohttp 依赖）独立测；_check_auth 依赖 aiohttp 单独 try
from config import parse_bool, sanitize_order  # noqa: E402

check("sanitize_order 白名单", sanitize_order("asc") == "asc" and sanitize_order("evil") == "desc")
check("parse_bool 字符串", parse_bool("true") is True and parse_bool("false") is False and parse_bool(True) is True)

try:
    from server_routes import _check_auth  # noqa: E402
    HAS_AIOHTTP = True
except ImportError as exc:
    print(f"[SKIP] _check_auth 依赖 aiohttp 不可用: {exc}")
    HAS_AIOHTTP = False

if HAS_AIOHTTP:

    def _fake_request(headers, query=None):
        class _H:
            def __init__(self, h):
                self._h = h

            def get(self, key, default=None):
                return self._h.get(key, default)

        class _Q:
            def __init__(self, q):
                self._q = q or {}

            def get(self, key, default=None):
                return self._q.get(key, default)

        class _R:
            def __init__(self, h, q, remote="192.168.1.50"):
                self.headers = _H(h)
                self.query = _Q(q)
                # 默认模拟非本机来源：本机信任逻辑在 test_routes.py 第 11 段覆盖
                self.remote = remote

        return _R(headers, query)

    with tempfile.TemporaryDirectory() as tmp:
        cm = ConfigManager(os.path.join(tmp, "c.json"))
        # 未配置 token = 放行
        req = _fake_request({})
        check("未配置 token 放行", _check_auth(req, cm) is True)
        # 配置 token 后，无 header / 错误 header 拒绝
        cm.update(panel_token="tok123")
        req2 = _fake_request({})
        check("无 header 拒绝", _check_auth(req2, cm) is False)
        req3 = _fake_request({"Authorization": "Bearer wrong"})
        check("错误 token 拒绝", _check_auth(req3, cm) is False)
        req4 = _fake_request({"Authorization": "Bearer tok123"})
        check("正确 token 放行", _check_auth(req4, cm) is True)
        # query token 一律不接受（token 只能走 header，防日志泄漏）
        req5 = _fake_request({}, {"token": "tok123"})
        check("query token 拒绝", _check_auth(req5, cm) is False)
        # header 是唯一通道
        req8 = _fake_request({"Authorization": "Bearer tok123"}, {"token": "wrong"})
        check("header 放行且 query 无关", _check_auth(req8, cm) is True)


fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
