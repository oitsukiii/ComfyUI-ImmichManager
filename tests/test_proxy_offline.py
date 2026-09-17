#!/usr/bin/env python3
"""P6 自测：代理配置对 ImmichClient 的实际行为（离线，不触网）。

用本机临时 HTTP server 冒充 Immich / 冒充代理，并把 HTTP_PROXY 指向"死代理"，
复现用户 Windows 上那次真实故障（Clash 没开、启动脚本却设了 HTTP_PROXY=127.0.0.1:7897）：

1. off（默认）      → 忽略环境变量，局域网直连成功（本次修复点）
2. env              → 被环境变量劫持，请求失败（旧行为，向后兼容）
3. custom           → 真走指定代理（用"假代理 + 死 base_url"证明请求确实到了代理）
4. custom 但代理不可用 → 失败且报错里带代理地址（便于区分"代理挂了/Immich 挂了"）
5. custom 但地址为空/非法 → 降级为 off，仍然直连成功（脏配置兜底）

用法: python3 tests/test_proxy_offline.py
"""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from immich_client import ImmichClient, ImmichError  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


# ---------- 测试替身 ----------

class _StubHandler(BaseHTTPRequestHandler):
    """最小 HTTP 桩：任何 GET 都回 200 + JSON（既可当 Immich，也可当"假代理"）。"""

    payload = {"major": 1, "minor": 2, "patch": 3}

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 约定
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


class _ImmichStub(_StubHandler):
    payload = {"major": 1, "minor": 2, "patch": 3}


class _ProxyStub(_StubHandler):
    """冒充代理：收到绝对 URI 请求也直接回 200，用版本号 9.9.9 区分来源。"""

    payload = {"major": 9, "minor": 9, "patch": 9}


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _port(srv):
    return srv.server_address[1]


def _dead_port():
    """拿一个"一定没人监听"的本地端口。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)
_SAVED_ENV = {k: os.environ.get(k) for k in _ENV_KEYS}


def _set_env(**kv):
    """清空所有代理相关环境变量后，只设置给定的键（值 None = 不设）。"""
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kv.items():
        if v is not None:
            os.environ[k] = v


def _restore_env():
    for k, v in _SAVED_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------- 用例 ----------

immich_srv = _serve(_ImmichStub)
proxy_srv = _serve(_ProxyStub)
IMMICH = f"http://127.0.0.1:{_port(immich_srv)}/api"
FAKE_PROXY = f"http://127.0.0.1:{_port(proxy_srv)}"
DEAD_PROXY = f"http://127.0.0.1:{_dead_port()}"
DEAD_BASE = f"http://127.0.0.1:{_dead_port()}/api"

try:
    # 1. 干净环境 + off：直连成功（基线）
    _set_env()
    c = ImmichClient(IMMICH, "k", timeout=5)
    check("干净环境 off 直连成功", c.version().get("major") == 1, f"got {c.version()}")
    check("off 模式 trust_env=False", c.session.trust_env is False)
    check("off 模式不带显式代理", not c.session.proxies, f"got {c.session.proxies}")

    # 2. ★核心修复：环境变量指向死代理，off 模式仍直连成功
    _set_env(HTTP_PROXY=DEAD_PROXY, HTTPS_PROXY=DEAD_PROXY, ALL_PROXY=DEAD_PROXY)
    c = ImmichClient(IMMICH, "k", timeout=5)
    try:
        ver = c.version()
        check("HTTP_PROXY 指向死代理时 off 仍直连成功", ver.get("major") == 1, f"got {ver}")
    except ImmichError as exc:
        check("HTTP_PROXY 指向死代理时 off 仍直连成功", False, f"抛错: {exc}")

    # 3. 小写环境变量同样免疫
    _set_env(**{"http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY})
    try:
        ver = ImmichClient(IMMICH, "k", timeout=5).version()
        check("小写 http_proxy/https_proxy 也被 off 忽略", ver.get("major") == 1, f"got {ver}")
    except ImmichError as exc:
        check("小写 http_proxy/https_proxy 也被 off 忽略", False, f"抛错: {exc}")

    # 4. env 模式：环境变量生效 → 被劫持到死代理失败（旧行为，向后兼容）
    _set_env(HTTP_PROXY=DEAD_PROXY, HTTPS_PROXY=DEAD_PROXY)
    c_env = ImmichClient(IMMICH, "k", timeout=5, proxy_mode="env")
    check("env 模式 trust_env=True", c_env.session.trust_env is True)
    try:
        c_env.version()
        hijacked = False
    except ImmichError:
        hijacked = True
    check("env 模式确实被环境变量劫持（复现旧行为）", hijacked)

    # 5. custom 模式：请求真的到了代理（假代理 + 死 base_url，拿到代理的 9.9.9）
    _set_env(HTTP_PROXY=DEAD_PROXY)  # 环境变量仍在，但 custom 应忽略它
    c_custom = ImmichClient(DEAD_BASE, "k", timeout=5, proxy_mode="custom", proxy_url=FAKE_PROXY)
    check("custom 模式 trust_env=False（不受环境变量二次劫持）", c_custom.session.trust_env is False)
    check(
        "custom 模式显式设置 http/https 代理",
        c_custom.session.proxies.get("http") == FAKE_PROXY
        and c_custom.session.proxies.get("https") == FAKE_PROXY,
        f"got {c_custom.session.proxies}",
    )
    try:
        ver = c_custom.version()
        check("custom 模式的请求确实经代理发出（死 base_url 也拿到响应）", ver.get("major") == 9, f"got {ver}")
    except ImmichError as exc:
        check("custom 模式的请求确实经代理发出（死 base_url 也拿到响应）", False, f"抛错: {exc}")

    # 6. custom + 代理不可用：报错里带代理地址
    c_dead = ImmichClient(IMMICH, "k", timeout=5, proxy_mode="custom", proxy_url=DEAD_PROXY)
    try:
        c_dead.version()
        msg = ""
    except ImmichError as exc:
        msg = str(exc)
    check("代理不可用时抛 ImmichError", bool(msg), f"msg={msg[:80]}")
    check("报错文案带「经代理 + 地址」便于排查", "经代理" in msg and DEAD_PROXY in msg, f"msg={msg[:120]}")

    # 7. 脏配置兜底：custom 但地址为空 / 非法 / 非字符串 → 降级 off 并直连成功
    _set_env()
    for bad, label in (
        ("", "空地址"),
        ("socks5://127.0.0.1:1080", "socks5 协议"),
        ("http://169.254.169.254:80", "link-local 地址"),
        ("http://127.0.0.1:7897/path", "带路径"),
        ("http://user:pass@127.0.0.1:7897", "带凭据"),
        (None, "None"),
    ):
        c_bad = ImmichClient(IMMICH, "k", timeout=5, proxy_mode="custom", proxy_url=bad)
        ok_mode = c_bad.proxy_mode == "off"
        try:
            ok_link = c_bad.version().get("major") == 1
        except ImmichError:
            ok_link = False
        check(f"custom + {label} → 降级 off 且直连成功", ok_mode and ok_link, f"mode={c_bad.proxy_mode}")

    # 8. 非法 proxy_mode → 降级 off
    c_junk = ImmichClient(IMMICH, "k", timeout=5, proxy_mode="bogus", proxy_url=FAKE_PROXY)
    check("非法 proxy_mode → 降级 off", c_junk.proxy_mode == "off" and c_junk.session.trust_env is False)

    # 9. 直连时（off）即便配置了地址，也不应把地址用上
    c_off_url = ImmichClient(IMMICH, "k", timeout=5, proxy_mode="off", proxy_url=FAKE_PROXY)
    check("off 模式忽略已填地址（不生效）", not c_off_url.session.proxies, f"got {c_off_url.session.proxies}")
finally:
    _restore_env()
    immich_srv.shutdown()
    proxy_srv.shutdown()
    immich_srv.server_close()
    proxy_srv.server_close()

fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
