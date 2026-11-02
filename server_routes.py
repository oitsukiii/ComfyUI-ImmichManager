"""ComfyUI-ImmichManager 后端 API 路由。

统一挂载 /api/immich_plus/*：
- 前端只认识这些路由，不认识 Immich 原生 API（CORS、鉴权、配置全在后端消化）
- 鉴权：ComfyUI 本身没有服务器级 --api-key（实测 0.30.0 无此参数），
  插件自带最小鉴权——配置了 panel_token 后，所有 /api/immich_plus/* 请求
  必须带 `Authorization: Bearer <panel_token>`，否则 401。
  未配置 panel_token 时视为"本机信任模式"（README 建议 --listen 127.0.0.1）。

Backend API routes for ComfyUI-ImmichManager.

Everything is mounted under /api/immich_plus/*:
- The frontend only knows these routes, never Immich's native API (CORS, auth
  and configuration are all absorbed by the backend).
- Auth: ComfyUI itself has no server-level --api-key (verified on 0.30.0, no
  such flag), so the plugin ships minimal auth of its own — once panel_token is
  configured, every /api/immich_plus/* request must carry
  `Authorization: Bearer <panel_token>`, otherwise 401. With no panel_token
  configured the plugin runs in "local trust mode" (README suggests
  --listen 127.0.0.1).
"""

import asyncio
import hmac
import json
import os
import re
import logging
import threading
import time
from typing import Optional

from aiohttp import web

# 兼容包内导入（ComfyUI 加载）与直接脚本导入（测试）  # supports both package-relative import (ComfyUI) and plain script import (tests)
try:
    from .config import (
        ALLOWED_PROXY_MODES,
        ALLOWED_TIMELINE_INTERVALS,
        ALLOWED_TIMELINE_RANGES,
        ConfigManager,
        get_config as _get_shared_config,
        normalize_base_url,
        normalize_proxy_url,
        parse_bool,
        sanitize_order,
    )
    from .immich_client import ImmichClient, ImmichError
    from .upload_node import job_manager
except ImportError:  # noqa: BLE001 — 直接 python3 运行（测试/调试）  # running directly through python3 (tests/debug)
    from config import (
        ALLOWED_PROXY_MODES,
        ALLOWED_TIMELINE_INTERVALS,
        ALLOWED_TIMELINE_RANGES,
        ConfigManager,
        get_config as _get_shared_config,
        normalize_base_url,
        normalize_proxy_url,
        parse_bool,
        sanitize_order,
    )
    from immich_client import ImmichClient, ImmichError
    from upload_node import job_manager

ROUTE_PREFIX = "/api/immich_plus"

# 资产 id 只允许 Immich 实际使用的形态。放行 "/" 或 ".." 会让 requests 做 URL
# 规范化，把 f"/assets/{asset_id}" 变成别的 Immich 端点，等于用操作者的 API key
# 去打插件从未暴露的接口（见 THREAT_MODEL.md F2）。
# Only the shapes Immich actually uses are accepted for asset ids. Allowing "/" or
# ".." would let requests normalise the URL and turn f"/assets/{asset_id}" into a
# different Immich endpoint, i.e. use the operator's API key against interfaces
# this plugin never exposes (see THREAT_MODEL.md F2).
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_asset_id(asset_id: str) -> bool:
    return bool(_ASSET_ID_RE.match(asset_id or ""))

ALLOWED_SIZES = {"thumbnail", "preview", "original"}
MAX_DELETE_IDS = 1000

# 资产扩展名映射缓存：桶列表不带扩展名，search/metadata 全量拉一次，短 TTL 缓存  # asset extension map cache: bucket listings carry no extension, so pull them all once via search/metadata and keep them under a short TTL
_ext_cache: dict = {"ts": 0.0, "map": {}}
_EXT_CACHE_TTL = 60.0
_ext_cache_lock = threading.Lock()


async def _get_ext_map(client: ImmichClient) -> dict:
    """id → 扩展名映射（带 TTL 缓存，失败返回空 dict 不影响主流程）。

    - 网络拉取走 asyncio.to_thread（与 _call 纪律一致），不阻塞事件循环
    - 兜底捕 Exception（含 JSONDecodeError 等），保证 /bucket 永不因角标数据失败

    English: id → extension-name map (TTL-cached; on failure returns an empty dict
    so the main flow is unaffected).
    - The network fetch runs through asyncio.to_thread (same discipline as _call)
      and never blocks the event loop
    - Catches Exception broadly (including JSONDecodeError) so /bucket never fails
      because of badge data
    """
    getter = getattr(client, "asset_ext_map", None)
    if getter is None:
        return {}
    now = time.monotonic()
    # ts 为 0 表示从未成功拉过。不能直接参与比较：刚启动的机器 time.monotonic()
    # 可能还小于 TTL，那样"从未拉过"会被误判成"缓存新鲜"，第一次 /bucket 就会
    # 返回空的角标映射（CI runner 上就是这么暴露的）。
    # ts == 0 means "never fetched successfully". It must not take part in the
    # comparison: on a freshly booted machine time.monotonic() can still be smaller
    # than the TTL, so "never fetched" would be mistaken for "cache is fresh" and
    # the first /bucket call would return an empty badge map (that is exactly how
    # it surfaced on the CI runner).
    if _ext_cache["ts"] and now - _ext_cache["ts"] <= _EXT_CACHE_TTL:
        return _ext_cache["map"]
    try:
        mapping = await asyncio.to_thread(getter)
    except Exception as exc:  # noqa: BLE001 — 角标数据失败只影响显示，不阻断时间轴  # badge-data failures only affect display and never block the timeline
        logger.warning("扩展名映射获取失败（角标可能缺失）: %r", exc)
        return _ext_cache["map"]
    with _ext_cache_lock:
        _ext_cache["map"] = mapping
        _ext_cache["ts"] = time.monotonic()
    return _ext_cache["map"]

logger = logging.getLogger("comfyui_immichmanager")

# client 复用缓存：config 未变时复用同一 Session（连接池），变更时重建  # client reuse cache: reuse the same Session (connection pool) while config is unchanged, rebuild it when config changes
_client_cache: dict = {"sig": None, "client": None}
_client_lock = threading.Lock()


def _config_signature(config: ConfigManager) -> tuple:
    """客户端缓存的配置指纹：base_url + api_key + 代理配置变则重建 Session。English: fingerprint of the config behind the client cache — rebuild the Session whenever base_url, api_key or the proxy config changes."""
    return (
        config.get("base_url", ""),
        config.get("api_key", ""),
        config.get("proxy_mode", "off"),
        config.get("proxy_url", ""),
    )


def _get_client(config: ConfigManager) -> ImmichClient:
    """获取（必要时重建）Immich 客户端。

    面板保存配置后 base_url/api_key/代理配置变化，这里按指纹检测并重建，
    旧 Session 主动 close 释放连接池——保证配置变更即时生效、不泄漏连接。

    English: get (rebuilding if needed) the Immich client.

    After the panel saves config, base_url/api_key/proxy settings may change; the
    fingerprint is checked here and the client is rebuilt, closing the old Session
    to release its connection pool — so changes take effect immediately and no
    connections leak.
    """
    sig = _config_signature(config)
    with _client_lock:
        if _client_cache["sig"] != sig:
            old = _client_cache["client"]
            _client_cache["client"] = ImmichClient(
                base_url=config.get("base_url", ""),
                # 只有当 key 是为当前 base_url 填的才用它（配置被手改时也拦住）  # only use the key if it was entered for the current base_url (also catches hand-edited config)
                api_key=config.api_key_for_current_host(),
                proxy_mode=config.get("proxy_mode", "off"),
                proxy_url=config.get("proxy_url", ""),
            )
            _client_cache["sig"] = sig
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
        return _client_cache["client"]


async def _call(fn, *args, **kwargs):
    """把同步 client 调用丢到线程池，避免阻塞 aiohttp 事件循环。English: push the synchronous client call into a thread pool so the aiohttp event loop never blocks."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# 本机（127.0.0.1/::1）免令牌开关：本机永远能打开面板管理令牌，
# 避免用户把令牌重新生成/清除后把自己锁在门外。局域网设备仍须 Bearer。
# No-token bypass for local requests (127.0.0.1/::1): the local machine must always
# be able to open the panel and manage the token, so regenerating or clearing the
# token cannot lock the user out. LAN devices still need Bearer.
_TRUST_LOCALHOST = True


def _is_localhost(remote: str | None) -> bool:
    """请求来源是否本机（127.0.0.1 / ::1 / IPv4-mapped ::ffff:127.0.0.1）。

    只信 TCP 层真实来源 request.remote；绝不看 X-Forwarded-For（客户端可伪造）。

    English: is the request source the local machine (127.0.0.1 / ::1 /
    IPv4-mapped ::ffff:127.0.0.1)?

    Only the real TCP-layer source request.remote is trusted; X-Forwarded-For is
    never consulted (clients can forge it).
    """
    if not remote:
        return False  # 无来源信息不盲目放行（保守）  # no source information: do not let it through blindly (conservative)
    r = remote.strip().lower()
    if r in ("127.0.0.1", "::1", "localhost"):
        return True
    if r.startswith("::ffff:") and r.rsplit(":", 1)[-1] == "127.0.0.1":
        return True
    return False


def _check_auth(request: web.Request, config: ConfigManager) -> bool:
    """panel_token 鉴权。未配置 token = 本机信任模式（放行）。

    本机来源（127.0.0.1/::1）一律放行，保证本机永远能进面板管理令牌；
    局域网其他设备必须带 Authorization: Bearer <panel_token>。
    一律从 Authorization header 取 token（RFC 6750，scheme 大小写不敏感）。
    不提供 query 参数通道：?token= 会泄入访问日志，且 <img src> 场景
    由前端 fetch + blob URL 携带 header 解决（见 web/immich_panel.js）。

    English: panel_token authentication. No token configured = local trust mode
    (allow everything).

    Local sources (127.0.0.1/::1) are always allowed so the local machine can
    always reach the panel to manage the token; other LAN devices must send
    Authorization: Bearer <panel_token>. The token is always read from the
    Authorization header (RFC 6750, the scheme is case-insensitive). There is no
    query-parameter channel: ?token= would leak into access logs, and the
    <img src> case is solved by the frontend fetching with a header and using a
    blob URL (see web/immich_panel.js).
    """
    if _TRUST_LOCALHOST and _is_localhost(request.remote):
        return True
    token = config.get("panel_token", "")
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        presented = auth[len("Bearer "):]
        if hmac.compare_digest(presented, token):
            return True
    return False


def _sensitive_denied(request: web.Request, config: ConfigManager) -> Optional[web.Response]:
    """敏感操作守卫：配置写入、重置、令牌管理。

    ⚠️ 这些路由不能只靠 _check_auth —— 默认安装时没有 panel_token，
    _check_auth 会放行**所有人**。那样局域网里任何设备都能：

        PUT /config  {"base_url": "http://attacker.example/api"}

    把 Immich 地址改成自己的服务器，下一次上传就会带着用户的 Immich
    API key 发过去。所以这里要求「本机来源」或「已持有令牌」二者之一。

    允许持有令牌的远程调用，是为了不破坏反向代理场景：ComfyUI 挂在 nginx
    后面时 request.remote 是代理的内网地址，不是回环。

    ⚠️ 这里**故意不看 _TRUST_LOCALHOST** —— 那个开关管的是「未配置令牌时
    本机是否免鉴权」，与「来源是不是本机」是两件事。若绑在一起，一旦有人
    关掉本机信任，管理员在本机上就再也改不了配置（把自己锁在门外）。

    English: guard for sensitive operations: config writes, reset, token management.

    ⚠️ These routes cannot rely on _check_auth alone — a default install has no
    panel_token, so _check_auth would let **everyone** through. Any LAN device
    could then do:

        PUT /config  {"base_url": "http://attacker.example/api"}

    pointing Immich at a server of its own, so the next upload would carry the
    user's Immich API key there. Hence one of "local source" or "holds a valid
    token" is required here.

    Remote calls holding a token are allowed so the reverse-proxy setup keeps
    working: with ComfyUI behind nginx, request.remote is the proxy's internal
    address, not loopback.

    ⚠️ _TRUST_LOCALHOST is **deliberately ignored** here — that switch controls
    "is a token-less local request allowed", which is a different question from
    "is the source local". Tying them together would mean that turning off local
    trust locks the administrator out of editing config on the local machine.
    """
    if _is_localhost(request.remote):
        return None
    if config.get("panel_token", "") and _check_auth(request, config):
        return None
    return web.json_response(
        {"error": "该操作仅允许在本机执行，或携带有效的面板令牌（Authorization: Bearer <panel_token>）"},
        status=403,
    )


def _unauthorized() -> web.Response:
    """401 统一响应：提示客户端需携带面板令牌。English: the uniform 401 response — tells the client to send a panel token."""
    return web.json_response({"error": "未授权：需要 Authorization: Bearer <panel_token>"}, status=401)


def _error(message: str, status: int = 400) -> web.Response:
    """统一错误响应 JSON。English: the uniform JSON error response."""
    return web.json_response({"error": message}, status=status)


def _sanitize_order(value: str, default: str = "desc") -> str:
    return sanitize_order(value, default)


def _parse_bool(value) -> bool:
    return parse_bool(value)


def setup_routes(server) -> None:
    routes = server.routes
    # 与 upload_node 共用同一 config 实例（面板配置变更即时对节点生效）  # share the same config instance as upload_node (panel changes hit the nodes immediately)
    config = _get_shared_config()

    # ---------- 配置 / Config ----------

    @routes.get(f"{ROUTE_PREFIX}/config")
    async def get_config(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        return web.json_response(config.public_config())

    @routes.put(f"{ROUTE_PREFIX}/config")
    async def put_config(request: web.Request) -> web.Response:
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if not _check_auth(request, config):
            return _unauthorized()
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error("无效的 JSON body")
        allowed = {
            "base_url", "api_key", "panel_token", "default_album",
            "page_size",
            "proxy_mode", "proxy_url",
            "timeline_range", "timeline_interval",
        }
        updates = {}
        for k, v in payload.items():
            if k not in allowed:
                continue
            if k == "api_key":
                # 前端只拿到 api_key_configured 布尔；误回传真实 key 形状的
                # 值（布尔/空串/True/False）一律忽略，避免覆盖
                # The frontend only ever sees the api_key_configured boolean; any
                # value shaped like a real key (bool/empty string/True/False) is
                # ignored so the stored key is never overwritten
                cleaned = ConfigManager.sanitize_api_key(v)
                if cleaned is None:
                    continue
                updates[k] = cleaned
            elif k == "panel_token":
                # 空串忽略（避免误清空导致安全降级回信任模式）；
                # 清空令牌走设置对话框 🔒 安全分组的「清空」按钮
                # Empty strings are ignored (an accidental clear would silently
                # downgrade to trust mode); clearing the token is done with the
                # "Clear" button of the 🔒 security group in the Settings dialog
                if isinstance(v, str) and v.strip():
                    updates[k] = v.strip()
            elif k == "base_url":
                # SSRF 防线：协议白名单 + host 校验（非法直接 400）  # SSRF guard: scheme allow-list + host validation (invalid input returns 400)
                try:
                    updates[k] = normalize_base_url(v)
                except ValueError as exc:
                    return _error(str(exc))
            elif k == "page_size":
                try:
                    num = max(0, int(v))
                except (TypeError, ValueError):
                    return _error(f"{k} 必须是数字")
                # 下界 1，避免 0/负数导致 Immich 分页异常  # lower bound 1, so 0/negative values cannot break Immich pagination
                updates[k] = min(max(num, 1), 1000)
            elif k == "timeline_range":
                if not isinstance(v, str) or v not in ALLOWED_TIMELINE_RANGES:
                    return _error(f"timeline_range 必须是 {sorted(ALLOWED_TIMELINE_RANGES)} 之一")
                updates[k] = v
            elif k == "timeline_interval":
                if not isinstance(v, str) or v not in ALLOWED_TIMELINE_INTERVALS:
                    return _error(f"timeline_interval 必须是 {sorted(ALLOWED_TIMELINE_INTERVALS)} 之一")
                updates[k] = v
            elif k == "proxy_mode":
                if not isinstance(v, str) or v not in ALLOWED_PROXY_MODES:
                    return _error(f"proxy_mode 必须是 {sorted(ALLOWED_PROXY_MODES)} 之一")
                updates[k] = v
            elif k == "proxy_url":
                # ""/空白 = 清除已存地址（用户在面板清空输入框并保存）；
                # 非空则校验（任何模式下都校验：地址写错要当场报错，而不是静默吞掉）
                # ""/whitespace = clear the stored URL (the user emptied the field
                # in the panel and saved); non-empty is validated (in every mode: a
                # typo must fail loudly instead of being swallowed silently)
                if not isinstance(v, str):
                    return _error("proxy_url 必须是字符串")
                if not v.strip():
                    updates[k] = ""
                    continue
                try:
                    updates[k] = normalize_proxy_url(v)
                except ValueError as exc:
                    return _error(str(exc))
            else:
                updates[k] = v
        if not updates:
            return web.json_response(config.public_config())
        # custom 模式必须带地址：按"本次改动合并后的最终态"校验，
        # 避免只切模式不填地址（或只清地址不切模式）留下脏配置
        # custom mode requires a URL: validate against "the final state after
        # merging this update" so that switching mode without a URL (or clearing
        # the URL without switching mode) cannot leave a dirty config behind
        eff_mode = updates.get("proxy_mode", config.get("proxy_mode", "off"))
        eff_url = updates.get("proxy_url", config.get("proxy_url", ""))
        if eff_mode == "custom" and not eff_url:
            return _error("选择自定义代理时必须填写 proxy_url")
        try:
            config.update(**updates)
        except ValueError as exc:  # 兜底：ConfigManager 的二次校验（正常不会走到）
            return _error(str(exc))
        result = config.public_config()
        # 自动生成面板令牌：连接配置好后（api_key 已配置）仍无 token → 生成并一次性下发明文。
        # 已有 token 或本次请求显式带了 panel_token 时保持不动（不触发）。
        # Auto-generate the panel token: once the connection is configured
        # (api_key set) and there is still no token → generate one and hand the
        # plaintext down once. If a token already exists, or this request
        # explicitly carried panel_token, nothing happens.
        if not config.get("panel_token", "") and config.get("api_key", ""):
            token = config.generate_panel_token()
            result = config.public_config()
            result["panel_token_plain"] = token
        return web.json_response(result)

    @routes.post(f"{ROUTE_PREFIX}/config/reset")
    async def reset_config(request: web.Request) -> web.Response:
        """清空配置：恢复出厂默认（清空 Immich 地址 / API Key / 面板令牌等），
        回到刚安装插件时的状态。仅本机、或携带有效面板令牌。

        English: wipe the config back to factory defaults (Immich URL / API key /
        panel token etc.), i.e. the state right after installing the plugin.
        Local requests or a valid panel token only.
        """
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if not _check_auth(request, config):
            return _unauthorized()
        config.reset()
        return web.json_response(config.public_config())

    # ---------- 面板令牌 / Panel token ----------

    @routes.post(f"{ROUTE_PREFIX}/panel-token")
    async def panel_token_generate(request: web.Request) -> web.Response:
        """生成/重新生成面板令牌。

        无令牌时（首次）直接生成；已有令牌时要求鉴权，防止他人"换锁"
        把现有会话全部锁在外面。明文只在本次响应中出现一次（阅后即焚）。
        仅本机、或携带有效面板令牌 —— 否则远程可以抢先设置令牌把人锁在外面。

        English: generate/regenerate the panel token.

        With no token yet (first run) it is generated directly; once a token
        exists, authentication is required so nobody can "change the lock" and
        shut every existing session out. The plaintext appears only in this
        response, once (burn after reading). Local requests or a valid panel
        token only — otherwise a remote caller could set the token first and lock
        the user out.
        """
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if config.get("panel_token", "") and not _check_auth(request, config):
            return _unauthorized()
        token = config.generate_panel_token()
        return web.json_response({"token": token})

    @routes.get(f"{ROUTE_PREFIX}/panel-token")
    async def panel_token_show(request: web.Request) -> web.Response:
        """显示当前面板令牌明文（仅本次响应下发，前端展示后即焚）。

        令牌等于管理员凭据，所以同样限制为本机或已持牌者。

        English: show the current panel token in plaintext (sent only in this
        response; the frontend burns it right after displaying).

        The token is effectively an administrator credential, so it is likewise
        limited to local requests or token holders.
        """
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if not _check_auth(request, config):
            return _unauthorized()
        token = config.get("panel_token", "")
        if not token:
            return _error("尚未生成面板令牌")
        return web.json_response({"token": token})

    @routes.delete(f"{ROUTE_PREFIX}/panel-token")
    async def panel_token_clear(request: web.Request) -> web.Response:
        """清除面板令牌（回到信任模式）。仅本机或已持牌者。English: clear the panel token (back to trust mode). Local requests or token holders only."""
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if not _check_auth(request, config):
            return _unauthorized()
        config.clear_panel_token()
        return web.json_response({"cleared": True})

    # ---------- 健康检查 / Health check ----------

    @routes.get(f"{ROUTE_PREFIX}/health")
    async def health(request: web.Request) -> web.Response:
        """测试连接：分阶段校验并给出明确反馈。

        1. stage=config  未配置 API key
        2. stage=connect version 拿不到（地址不可达 / 非 Immich / 网络错误）
        3. stage=auth    buckets 401（API key 无效）或其他请求失败
        4. ok=true       version + buckets 都通过，附带资产统计

        English: test the connection: staged validation with explicit feedback.

        1. stage=config  no API key configured
        2. stage=connect cannot get version (address unreachable / not Immich /
           network error)
        3. stage=auth    buckets returns 401 (invalid API key) or another request
           fails
        4. ok=true       both version and buckets passed, with asset counts
        """
        if not _check_auth(request, config):
            return _unauthorized()
        client = _get_client(config)
        # 自定义代理时把代理地址带回前端：connect 失败要能区分"代理挂了"和"Immich 挂了"  # with a custom proxy, echo the proxy URL back to the frontend so connect failures can tell "proxy is down" from "Immich is down"
        _proxy_field = (
            {"proxy_url": config.get("proxy_url", "")}
            if config.get("proxy_mode", "off") == "custom" and config.get("proxy_url", "")
            else {}
        )
        if not config.get("api_key", ""):
            return web.json_response(
                {"ok": False, "stage": "config", "error": "未配置 Immich API key"}, status=200
            )
        # 1. 地址可达 + 是 Immich：version（公开端点，不校验 key）  # 1. address reachable and really Immich: version (public endpoint, no key check)
        try:
            version = await _call(client.version)
        except ImmichError as exc:
            if exc.status_code is None:
                return web.json_response(
                    {"ok": False, "stage": "connect", "error": "请检查地址/端口/网络", **_proxy_field},
                    status=200,
                )
            return web.json_response(
                {"ok": False, "stage": "connect", "error": f"服务响应异常 (HTTP {exc.status_code})", **_proxy_field},
                status=200,
            )
        # 2. API key 有效性 + 资产统计：buckets（需鉴权）  # 2. API key validity + asset counts: buckets (requires auth)
        try:
            buckets = await _call(client.get_buckets, "desc")
        except ImmichError as exc:
            if exc.status_code == 401:
                return web.json_response(
                    {"ok": False, "stage": "auth", "error": "请检查 API Key 是否有效或已被删除"},
                    status=200,
                )
            if exc.status_code is None:
                return web.json_response(
                    {"ok": False, "stage": "connect", "error": "请检查地址/端口/网络", **_proxy_field},
                    status=200,
                )
            return web.json_response(
                {"ok": False, "stage": "auth", "error": f"请求失败 (HTTP {exc.status_code})"},
                status=200,
            )
        bucket_list = buckets if isinstance(buckets, list) else []
        assets_count = sum(b.get("count", 0) for b in bucket_list if isinstance(b, dict))
        return web.json_response(
            {
                "ok": True,
                "base_url": config.get("base_url"),
                "version": version,
                "buckets_count": len(bucket_list),
                "assets_count": assets_count,
            }
        )

    # ---------- 时间轴 / Timeline ----------

    @routes.get(f"{ROUTE_PREFIX}/buckets")
    async def buckets(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        order = _sanitize_order(request.query.get("order", "desc"))
        client = _get_client(config)
        try:
            data = await _call(client.get_buckets, order)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        return web.json_response(data)

    @routes.get(f"{ROUTE_PREFIX}/bucket")
    async def bucket(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        time_bucket = request.query.get("timeBucket", "")
        if not time_bucket:
            return _error("缺少 timeBucket 参数")
        order = _sanitize_order(request.query.get("order", "desc"))
        client = _get_client(config)
        try:
            data = await _call(client.get_bucket, time_bucket, order)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        # 附加扩展名（格式角标用）：桶列式响应不带，从缓存映射补  # attach the extension (used by the format badge): the bucket listing omits it, fill it in from the cached map
        ext_map = await _get_ext_map(client)
        if ext_map:
            for a in data:
                if isinstance(a, dict) and a.get("id") in ext_map:
                    a["ext"] = ext_map[a["id"]]
        return web.json_response(data)

    # ---------- 资产 / Assets ----------

    @routes.get(f"{ROUTE_PREFIX}/assets/{{asset_id}}")
    async def asset_detail(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        asset_id = request.match_info["asset_id"]
        if not _valid_asset_id(asset_id):
            return _error("非法 asset_id")
        client = _get_client(config)
        try:
            data = await _call(client.get_asset, asset_id)
        except ImmichError as exc:
            return _error(_safe_error(exc), exc.status_code or 502)
        return web.json_response(data)

    @routes.get(f"{ROUTE_PREFIX}/assets/{{asset_id}}/thumbnail")
    async def asset_thumbnail(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        asset_id = request.match_info["asset_id"]
        if not _valid_asset_id(asset_id):
            return _error("非法 asset_id")
        size = request.query.get("size", "preview")
        if size not in ALLOWED_SIZES:
            return _error(f"size 必须是 {sorted(ALLOWED_SIZES)} 之一")
        client = _get_client(config)
        # 条件请求头必须在发请求前传给上游（事后对 PreparedRequest 赋值无效）  # conditional request headers must reach upstream before the request is sent (assigning them to a PreparedRequest afterwards has no effect)
        cond_headers = {}
        for h in ("If-None-Match", "If-Modified-Since"):
            if request.headers.get(h):
                cond_headers[h] = request.headers[h]
        try:
            upstream = await _call(client.get_thumbnail, asset_id, size, cond_headers or None)
        except ImmichError as exc:
            return _error(_safe_error(exc), exc.status_code or 502)
        # 透传缓存头，让浏览器缓存真正生效  # pass cache headers through so browser caching actually works
        headers = {}
        for h in ("Content-Type", "ETag", "Last-Modified", "Cache-Control"):
            if h in upstream.headers:
                headers[h] = upstream.headers[h]
        # 上游判定未修改 → 直接回 304（浏览器命中缓存）  # upstream says not modified → reply 304 directly (browser cache hit)
        if upstream.status_code == 304:
            upstream.close()
            return web.Response(status=304, headers=headers)
        response = web.StreamResponse(status=upstream.status_code, headers=headers)
        await response.prepare(request)
        try:
            async for chunk in _iter_upstream(upstream):
                await response.write(chunk)
        except Exception as exc:  # noqa: BLE001 — 流式中断不应让整个请求 500  # a streaming interruption must not turn the whole request into a 500
            logger.warning("缩略图流式转发中断: %s", exc)
        finally:
            upstream.close()
        await response.write_eof()
        return response

    def _png_text_chunks(data: bytes) -> dict:
        """从 PNG 里取 tEXt / iTXt 文本块（ComfyUI 把 prompt / workflow 写在这）。English: pull the tEXt / iTXt text chunks out of a PNG (that is where ComfyUI writes prompt / workflow)."""
        out: dict = {}
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return out
        pos = 8
        n = len(data)
        while pos + 12 <= n:
            length = int.from_bytes(data[pos:pos + 4], "big")
            ctype = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + length]
            if ctype == b"tEXt":
                z = chunk.find(b"\x00")
                if z > 0:
                    out[chunk[:z].decode("latin-1")] = chunk[z + 1:].decode("utf-8", "replace")
            elif ctype == b"iTXt":
                z1 = chunk.find(b"\x00")
                if z1 > 0:
                    key = chunk[:z1].decode("latin-1")
                    comp = chunk[z1 + 1] if z1 + 1 < len(chunk) else 0
                    p2 = z1 + 3
                    z2 = chunk.find(b"\x00", p2)
                    z3 = chunk.find(b"\x00", z2 + 1) if z2 >= 0 else -1
                    if not comp and z3 >= 0:
                        out[key] = chunk[z3 + 1:].decode("utf-8", "replace")
            elif ctype == b"IEND":
                break
            pos += 12 + length
        return out

    def _video_text_metadata(path: str) -> dict:
        """从视频容器里取 metadata（ComfyUI 用 ffmpeg tag 存 prompt / workflow）。English: read metadata from the video container (ComfyUI stores prompt / workflow in ffmpeg tags)."""
        out: dict = {}
        try:
            import av  # noqa: PLC0415 — ComfyUI 自带 PyAV  # ComfyUI ships PyAV
        except Exception:
            return out
        try:
            with av.open(path) as container:
                for k, v in (container.metadata or {}).items():
                    out[str(k).lower()] = v
        except Exception:
            return out
        return out

    @routes.get(f"{ROUTE_PREFIX}/assets/{{asset_id}}/workflow")
    async def asset_workflow(request: web.Request) -> web.Response:
        """返回该资产里嵌入的 ComfyUI 工作流（图片读 PNG 文本块，视频读容器 metadata）。English: return the ComfyUI workflow embedded in this asset (images: PNG text chunks, videos: container metadata)."""
        if not _check_auth(request, config):
            return _unauthorized()
        asset_id = request.match_info["asset_id"]
        if not _valid_asset_id(asset_id):
            return _error("asset_id 格式不正确", 400)
        client = _get_client(config)

        def _collect():
            import tempfile  # noqa: PLC0415
            resp = client.get_original(asset_id)
            if getattr(resp, "status_code", 200) != 200:
                raise ImmichError(f"HTTP {resp.status_code}")
            data = resp.content
            meta = {}
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                meta = _png_text_chunks(data)
            else:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as fh:
                    fh.write(data)
                    tmp = fh.name
                try:
                    meta = _video_text_metadata(tmp)
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            wf = meta.get("workflow")
            pm = meta.get("prompt")
            # 有些容器把 JSON 存在 comment/description 里  # some containers keep the JSON in comment/description
            if not wf:
                for key in ("comment", "description", "com.apple.quicktime.description"):
                    v = meta.get(key)
                    if v and str(v).strip().startswith("{"):
                        wf = v
                        break
            return {"workflow": wf, "prompt": pm}

        try:
            payload = await _call(_collect)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        except Exception as exc:  # noqa: BLE001
            return _error(f"读取元数据失败：{exc}", 500)
        return web.json_response(payload)

    @routes.get(f"{ROUTE_PREFIX}/assets/{{asset_id}}/original")
    async def asset_original(request: web.Request) -> web.Response:
        """资产原文件代理（图片大图 / 视频播放）。

        - fetch + blob 播放：前端先 GET（带鉴权 header）整读再播放，故无需 Range 支持
        - 透传 Content-Type / Content-Length / ETag / Last-Modified / Cache-Control
        - 客户端 _request 禁重定向：直接调 /original 端点，不走 fullsize 302

        English: proxy for the asset's original file (large images / video playback).

        - fetch + blob playback: the frontend first GETs the whole file (with the
          auth header) and then plays it, so Range support is not needed
        - Passes through Content-Type / Content-Length / ETag / Last-Modified /
          Cache-Control
        - The client's _request forbids redirects: call the /original endpoint
          directly instead of following the fullsize 302
        """
        if not _check_auth(request, config):
            return _unauthorized()
        asset_id = request.match_info["asset_id"]
        if not _valid_asset_id(asset_id):
            return _error("非法 asset_id")
        asset_id = request.match_info["asset_id"]
        client = _get_client(config)
        try:
            upstream = await _call(client.get_original, asset_id)
        except ImmichError as exc:
            return _error(_safe_error(exc), exc.status_code or 502)
        headers = {}
        for h in ("Content-Type", "Content-Length", "ETag", "Last-Modified", "Cache-Control", "Content-Disposition"):
            if h in upstream.headers:
                headers[h] = upstream.headers[h]
        response = web.StreamResponse(status=upstream.status_code, headers=headers)
        await response.prepare(request)
        try:
            async for chunk in _iter_upstream(upstream):
                await response.write(chunk)
        except Exception as exc:  # noqa: BLE001 — 流式中断不应让整个请求 500  # a streaming interruption must not turn the whole request into a 500
            logger.warning("原文件流式转发中断: %s", exc)
        finally:
            upstream.close()
        await response.write_eof()
        return response

    @routes.put(f"{ROUTE_PREFIX}/assets")
    async def assets_bulk_update(request: web.Request) -> web.Response:
        """批量收藏/取消收藏：PUT /assets + body {ids, isFavorite}（set 语义，幂等）。

        内部优先 Immich 批量端点 PUT /assets，失败降级逐条（见
        ImmichClient.update_assets_bulk），返回部分成功语义。

        English: bulk favourite/unfavourite: PUT /assets + body {ids, isFavorite}
        (set semantics, idempotent).

        Internally prefers Immich's bulk endpoint PUT /assets and falls back to
        one-by-one calls when it fails (see ImmichClient.update_assets_bulk); the
        result reports partial success.
        """
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if not _check_auth(request, config):
            return _unauthorized()
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error("无效的 JSON body")
        ids = payload.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return _error("缺少 ids")
        if len(ids) > MAX_DELETE_IDS:
            return _error(f"一次最多操作 {MAX_DELETE_IDS} 个资产")
        if not isinstance(payload.get("isFavorite"), bool):
            return _error("isFavorite 必须是布尔值")
        ids = [str(i) for i in ids]
        client = _get_client(config)
        try:
            result = await _call(client.update_assets_bulk, ids, payload["isFavorite"])
        except ImmichError as exc:
            return _error(_safe_error(exc), exc.status_code or 502)
        # 部分成功：返回 200 + failed 明细；全部失败 → 502  # partial success: 200 + failed detail; all failed → 502
        if result.get("updated", 0) == 0 and result.get("failed"):
            return _error("资产更新失败", 502)
        return web.json_response(result)

    @routes.put(f"{ROUTE_PREFIX}/assets/{{asset_id}}")
    async def asset_update(request: web.Request) -> web.Response:
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if not _check_auth(request, config):
            return _unauthorized()
        asset_id = request.match_info["asset_id"]
        if not _valid_asset_id(asset_id):
            return _error("非法 asset_id")
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error("无效的 JSON body")
        client = _get_client(config)
        try:
            data = await _call(client.update_asset, asset_id, **payload)
        except ImmichError as exc:
            return _error(_safe_error(exc), exc.status_code or 502)
        return web.json_response(data)

    @routes.delete(f"{ROUTE_PREFIX}/assets")
    async def assets_delete(request: web.Request) -> web.Response:
        """批量删除：DELETE /assets + body {ids}。删除总进回收站（v2 实测）。English: bulk delete: DELETE /assets + body {ids}. Deletes always land in the trash (verified on v2)."""
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        if not _check_auth(request, config):
            return _unauthorized()
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error("无效的 JSON body")
        ids = payload.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return _error("缺少 ids")
        if len(ids) > MAX_DELETE_IDS:
            return _error(f"一次最多删除 {MAX_DELETE_IDS} 个资产")
        ids = [str(i) for i in ids]
        force = _parse_bool(payload.get("force", False))
        client = _get_client(config)
        try:
            ok = await _call(client.delete_assets, ids, force)
        except ImmichError as exc:
            return _error(_safe_error(exc), exc.status_code or 502)
        return web.json_response({"deleted": ok})

    # ---------- 异步上传任务 / Async upload jobs ----------

    @routes.get(f"{ROUTE_PREFIX}/jobs/{{job_id}}")
    async def job_status(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        job = job_manager.get(request.match_info["job_id"])
        if job is None:
            return _error("任务不存在", 404)
        return web.json_response(job)

    # ---------- 相册 / Albums ----------

    # 主面板 UI 状态（跟着服务器走，不进工作流）  # main panel UI state (server-side, never part of the workflow)
    def _ui_state_path() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(config.config_path)), "panel_state.json")

    @routes.get(f"{ROUTE_PREFIX}/panel-state")
    async def get_panel_state(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        try:
            with open(_ui_state_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        return web.json_response(data if isinstance(data, dict) else {})

    @routes.post(f"{ROUTE_PREFIX}/panel-state")
    async def post_panel_state(request: web.Request) -> web.Response:
        denied = _sensitive_denied(request, config)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except Exception:
            return _error("请求体不是合法 JSON", 400)
        if not isinstance(payload, dict):
            return _error("请求体必须是对象", 400)
        path = _ui_state_path()
        try:
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            return _error(f"写入失败：{exc}", 500)
        return web.json_response({"ok": True})

    @routes.get(f"{ROUTE_PREFIX}/people")
    async def people(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        client = _get_client(config)
        try:
            data = await _call(client.get_people)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        return web.json_response(data)

    @routes.get(f"{ROUTE_PREFIX}/tags")
    async def tags(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        client = _get_client(config)
        try:
            data = await _call(client.get_tags)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        return web.json_response(data)

    @routes.get(f"{ROUTE_PREFIX}/search")
    async def search(request: web.Request) -> web.Response:
        """按条件搜索：album / person / tag / after / before / type / order。

        多个同名参数可重复（?album=a&album=b），前端按条件拼接。

        English: search by criteria: album / person / tag / after / before / type /
        order.

        The same parameter may be repeated (?album=a&album=b); the frontend
        combines the criteria.
        """
        if not _check_auth(request, config):
            return _unauthorized()
        q = request.query
        album_ids = [v for v in q.getall("album", []) if _valid_asset_id(v)]
        person_ids = [v for v in q.getall("person", []) if _valid_asset_id(v)]
        tag_ids = [v for v in q.getall("tag", []) if _valid_asset_id(v)]
        try:
            page = int(q.get("page", "1"))
            size = int(q.get("size", "24"))
        except ValueError:
            return _error("page/size 必须是整数", 400)
        after = (q.get("after", "") or "").strip()
        before = (q.get("before", "") or "").strip()
        asset_type = (q.get("type", "") or "").upper()
        order = (q.get("order", "desc") or "desc").lower()
        client = _get_client(config)
        try:
            favorite = (q.get("favorite", "") or "").strip()
            data = await _call(client.search_assets, album_ids, person_ids, tag_ids,
                               after, before, asset_type, favorite, page, size, order)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        return web.json_response(data)

    @routes.get(f"{ROUTE_PREFIX}/albums/{{album_id}}/assets")
    async def album_assets(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        album_id = request.match_info["album_id"]
        if not _valid_asset_id(album_id):        # 同一套 UUID 白名单  # same UUID allow-list
            return _error("相册 id 格式不正确", 400)
        try:
            page = int(request.query.get("page", "1"))
            size = int(request.query.get("size", "24"))
        except ValueError:
            return _error("page/size 必须是整数", 400)
        client = _get_client(config)
        try:
            asset_type = (request.query.get("type", "") or "").upper()
            order = (request.query.get("order", "desc") or "desc").lower()
            data = await _call(client.get_album_assets, album_id, page, size, asset_type, order)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        return web.json_response(data)

    @routes.get(f"{ROUTE_PREFIX}/albums")
    async def albums(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        client = _get_client(config)
        try:
            data = await _call(client.get_albums)
        except ImmichError as exc:
            return _error(_safe_error(exc), 502)
        return web.json_response(data)


def _safe_error(exc: ImmichError) -> str:
    """错误信息脱敏：不回传 Immich 内部细节/路径，只回状态码。English: sanitise error messages: never return Immich internals/paths, only the status code."""
    if exc.status_code:
        return f"Immich 请求失败 (HTTP {exc.status_code})"
    return "Immich 连接失败"


async def _iter_upstream(upstream):
    """分块读取上游响应（在 executor 里跑，避免阻塞事件循环）。English: read the upstream response in chunks (runs in an executor so the event loop never blocks)."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            chunk = await loop.run_in_executor(None, upstream.raw.read, 64 * 1024)
        except Exception:  # noqa: BLE001 — 上游断流按流结束处理  # an upstream disconnect is treated as end of stream
            break
        if not chunk:
            break
        yield chunk
