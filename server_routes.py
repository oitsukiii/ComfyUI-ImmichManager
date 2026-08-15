"""ComfyUI-ImmichManager 后端 API 路由。

统一挂载 /api/immich_plus/*：
- 前端只认识这些路由，不认识 Immich 原生 API（CORS、鉴权、配置全在后端消化）
- 鉴权：ComfyUI 本身没有服务器级 --api-key（实测 0.30.0 无此参数），
  插件自带最小鉴权——配置了 panel_token 后，所有 /api/immich_plus/* 请求
  必须带 `Authorization: Bearer <panel_token>`，否则 401。
  未配置 panel_token 时视为"本机信任模式"（README 建议 --listen 127.0.0.1）。
"""

import asyncio
import hmac
import json
import logging
import threading
import time
from typing import Optional

from aiohttp import web

# 兼容包内导入（ComfyUI 加载）与直接脚本导入（测试）
try:
    from .config import (
        ALLOWED_TIMELINE_INTERVALS,
        ALLOWED_TIMELINE_RANGES,
        ConfigManager,
        get_config as _get_shared_config,
        normalize_base_url,
        parse_bool,
        sanitize_order,
    )
    from .immich_client import ImmichClient, ImmichError
    from .upload_node import job_manager
except ImportError:  # noqa: BLE001 — 直接 python3 运行（测试/调试）
    from config import (
        ALLOWED_TIMELINE_INTERVALS,
        ALLOWED_TIMELINE_RANGES,
        ConfigManager,
        get_config as _get_shared_config,
        normalize_base_url,
        parse_bool,
        sanitize_order,
    )
    from immich_client import ImmichClient, ImmichError
    from upload_node import job_manager

ROUTE_PREFIX = "/api/immich_plus"
ALLOWED_SIZES = {"thumbnail", "preview", "original"}
MAX_DELETE_IDS = 1000

# 资产扩展名映射缓存：桶列表不带扩展名，search/metadata 全量拉一次，短 TTL 缓存
_ext_cache: dict = {"ts": 0.0, "map": {}}
_EXT_CACHE_TTL = 60.0
_ext_cache_lock = threading.Lock()


async def _get_ext_map(client: ImmichClient) -> dict:
    """id → 扩展名映射（带 TTL 缓存，失败返回空 dict 不影响主流程）。

    - 网络拉取走 asyncio.to_thread（与 _call 纪律一致），不阻塞事件循环
    - 兜底捕 Exception（含 JSONDecodeError 等），保证 /bucket 永不因角标数据失败
    """
    getter = getattr(client, "asset_ext_map", None)
    if getter is None:
        return {}
    now = time.monotonic()
    if now - _ext_cache["ts"] <= _EXT_CACHE_TTL:
        return _ext_cache["map"]
    try:
        mapping = await asyncio.to_thread(getter)
    except Exception as exc:  # noqa: BLE001 — 角标数据失败只影响显示，不阻断时间轴
        logger.warning("扩展名映射获取失败（角标可能缺失）: %r", exc)
        return _ext_cache["map"]
    with _ext_cache_lock:
        _ext_cache["map"] = mapping
        _ext_cache["ts"] = time.monotonic()
    return _ext_cache["map"]

logger = logging.getLogger("comfyui_immichmanager")

# client 复用缓存：config 未变时复用同一 Session（连接池），变更时重建
_client_cache: dict = {"sig": None, "client": None}
_client_lock = threading.Lock()


def _config_signature(config: ConfigManager) -> tuple:
    """客户端缓存的配置指纹：base_url + api_key 变则重建 Session。"""
    return (config.get("base_url", ""), config.get("api_key", ""))


def _get_client(config: ConfigManager) -> ImmichClient:
    """获取（必要时重建）Immich 客户端。

    面板保存配置后 base_url/api_key 变化，这里按指纹检测并重建，
    旧 Session 主动 close 释放连接池——保证配置变更即时生效、不泄漏连接。
    """
    sig = _config_signature(config)
    with _client_lock:
        if _client_cache["sig"] != sig:
            old = _client_cache["client"]
            _client_cache["client"] = ImmichClient(
                base_url=config.get("base_url", ""),
                api_key=config.get("api_key", ""),
            )
            _client_cache["sig"] = sig
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
        return _client_cache["client"]


async def _call(fn, *args, **kwargs):
    """把同步 client 调用丢到线程池，避免阻塞 aiohttp 事件循环。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


# 本机（127.0.0.1/::1）免令牌开关：本机永远能打开面板管理令牌，
# 避免用户把令牌重新生成/清除后把自己锁在门外。局域网设备仍须 Bearer。
_TRUST_LOCALHOST = True


def _is_localhost(remote: str | None) -> bool:
    """请求来源是否本机（127.0.0.1 / ::1 / IPv4-mapped ::ffff:127.0.0.1）。

    只信 TCP 层真实来源 request.remote；绝不看 X-Forwarded-For（客户端可伪造）。
    """
    if not remote:
        return False  # 无来源信息不盲目放行（保守）
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


def _unauthorized() -> web.Response:
    """401 统一响应：提示客户端需携带面板令牌。"""
    return web.json_response({"error": "未授权：需要 Authorization: Bearer <panel_token>"}, status=401)


def _error(message: str, status: int = 400) -> web.Response:
    """统一错误响应 JSON。"""
    return web.json_response({"error": message}, status=status)


def _sanitize_order(value: str, default: str = "desc") -> str:
    return sanitize_order(value, default)


def _parse_bool(value) -> bool:
    return parse_bool(value)


def setup_routes(server) -> None:
    routes = server.routes
    # 与 upload_node 共用同一 config 实例（面板配置变更即时对节点生效）
    config = _get_shared_config()

    # ---------- 配置 ----------

    @routes.get(f"{ROUTE_PREFIX}/config")
    async def get_config(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        return web.json_response(config.public_config())

    @routes.put(f"{ROUTE_PREFIX}/config")
    async def put_config(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error("无效的 JSON body")
        allowed = {
            "base_url", "api_key", "panel_token", "default_album",
            "page_size",
            "timeline_range", "timeline_interval",
        }
        updates = {}
        for k, v in payload.items():
            if k not in allowed:
                continue
            if k == "api_key":
                # 前端只拿到 api_key_configured 布尔；误回传真实 key 形状的
                # 值（布尔/空串/True/False）一律忽略，避免覆盖
                cleaned = ConfigManager.sanitize_api_key(v)
                if cleaned is None:
                    continue
                updates[k] = cleaned
            elif k == "panel_token":
                # 空串忽略（避免误清空导致安全降级回信任模式）；
                # 清空令牌走配置页 🔒 安全分组的「清除」按钮
                if isinstance(v, str) and v.strip():
                    updates[k] = v.strip()
            elif k == "base_url":
                # SSRF 防线：协议白名单 + host 校验（非法直接 400）
                try:
                    updates[k] = normalize_base_url(v)
                except ValueError as exc:
                    return _error(str(exc))
            elif k == "page_size":
                try:
                    num = max(0, int(v))
                except (TypeError, ValueError):
                    return _error(f"{k} 必须是数字")
                # 下界 1，避免 0/负数导致 Immich 分页异常
                updates[k] = min(max(num, 1), 1000)
            elif k == "timeline_range":
                if not isinstance(v, str) or v not in ALLOWED_TIMELINE_RANGES:
                    return _error(f"timeline_range 必须是 {sorted(ALLOWED_TIMELINE_RANGES)} 之一")
                updates[k] = v
            elif k == "timeline_interval":
                if not isinstance(v, str) or v not in ALLOWED_TIMELINE_INTERVALS:
                    return _error(f"timeline_interval 必须是 {sorted(ALLOWED_TIMELINE_INTERVALS)} 之一")
                updates[k] = v
            else:
                updates[k] = v
        if not updates:
            return web.json_response(config.public_config())
        config.update(**updates)
        result = config.public_config()
        # 自动生成面板令牌：连接配置好后（api_key 已配置）仍无 token → 生成并一次性下发明文。
        # 已有 token 或本次请求显式带了 panel_token 时保持不动（不触发）。
        if not config.get("panel_token", "") and config.get("api_key", ""):
            token = config.generate_panel_token()
            result = config.public_config()
            result["panel_token_plain"] = token
        return web.json_response(result)

    @routes.post(f"{ROUTE_PREFIX}/config/reset")
    async def reset_config(request: web.Request) -> web.Response:
        """清空配置：恢复出厂默认（清空 Immich 地址 / API Key / 面板令牌等），
        回到刚安装插件时的状态。需鉴权（本机免令牌，局域网需带当前令牌）。"""
        if not _check_auth(request, config):
            return _unauthorized()
        config.reset()
        return web.json_response(config.public_config())

    # ---------- 面板令牌 ----------

    @routes.post(f"{ROUTE_PREFIX}/panel-token")
    async def panel_token_generate(request: web.Request) -> web.Response:
        """生成/重新生成面板令牌。

        无令牌时（首次）直接生成；已有令牌时要求鉴权，防止他人"换锁"
        把现有会话全部锁在外面。明文只在本次响应中出现一次（阅后即焚）。
        """
        if config.get("panel_token", "") and not _check_auth(request, config):
            return _unauthorized()
        token = config.generate_panel_token()
        return web.json_response({"token": token})

    @routes.get(f"{ROUTE_PREFIX}/panel-token")
    async def panel_token_show(request: web.Request) -> web.Response:
        """显示当前面板令牌明文（仅本次响应下发，前端展示后即焚）。"""
        if not _check_auth(request, config):
            return _unauthorized()
        token = config.get("panel_token", "")
        if not token:
            return _error("尚未生成面板令牌")
        return web.json_response({"token": token})

    @routes.delete(f"{ROUTE_PREFIX}/panel-token")
    async def panel_token_clear(request: web.Request) -> web.Response:
        """清除面板令牌（回到信任模式）。必须鉴权。"""
        if not _check_auth(request, config):
            return _unauthorized()
        config.clear_panel_token()
        return web.json_response({"cleared": True})

    # ---------- 健康检查 ----------

    @routes.get(f"{ROUTE_PREFIX}/health")
    async def health(request: web.Request) -> web.Response:
        """测试连接：分阶段校验并给出明确反馈。

        1. stage=config  未配置 API key
        2. stage=connect version 拿不到（地址不可达 / 非 Immich / 网络错误）
        3. stage=auth    buckets 401（API key 无效）或其他请求失败
        4. ok=true       version + buckets 都通过，附带资产统计
        """
        if not _check_auth(request, config):
            return _unauthorized()
        client = _get_client(config)
        if not config.get("api_key", ""):
            return web.json_response(
                {"ok": False, "stage": "config", "error": "未配置 Immich API key"}, status=200
            )
        # 1. 地址可达 + 是 Immich：version（公开端点，不校验 key）
        try:
            version = await _call(client.version)
        except ImmichError as exc:
            if exc.status_code is None:
                return web.json_response(
                    {"ok": False, "stage": "connect", "error": "请检查地址/端口/网络"},
                    status=200,
                )
            return web.json_response(
                {"ok": False, "stage": "connect", "error": f"服务响应异常 (HTTP {exc.status_code})"},
                status=200,
            )
        # 2. API key 有效性 + 资产统计：buckets（需鉴权）
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
                    {"ok": False, "stage": "connect", "error": "请检查地址/端口/网络"},
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

    # ---------- 时间轴 ----------

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
        # 附加扩展名（格式角标用）：桶列式响应不带，从缓存映射补
        ext_map = await _get_ext_map(client)
        if ext_map:
            for a in data:
                if isinstance(a, dict) and a.get("id") in ext_map:
                    a["ext"] = ext_map[a["id"]]
        return web.json_response(data)

    # ---------- 资产 ----------

    @routes.get(f"{ROUTE_PREFIX}/assets/{{asset_id}}")
    async def asset_detail(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        asset_id = request.match_info["asset_id"]
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
        size = request.query.get("size", "preview")
        if size not in ALLOWED_SIZES:
            return _error(f"size 必须是 {sorted(ALLOWED_SIZES)} 之一")
        client = _get_client(config)
        # 条件请求头必须在发请求前传给上游（事后对 PreparedRequest 赋值无效）
        cond_headers = {}
        for h in ("If-None-Match", "If-Modified-Since"):
            if request.headers.get(h):
                cond_headers[h] = request.headers[h]
        try:
            upstream = await _call(client.get_thumbnail, asset_id, size, cond_headers or None)
        except ImmichError as exc:
            return _error(_safe_error(exc), exc.status_code or 502)
        # 透传缓存头，让浏览器缓存真正生效
        headers = {}
        for h in ("Content-Type", "ETag", "Last-Modified", "Cache-Control"):
            if h in upstream.headers:
                headers[h] = upstream.headers[h]
        # 上游判定未修改 → 直接回 304（浏览器命中缓存）
        if upstream.status_code == 304:
            upstream.close()
            return web.Response(status=304, headers=headers)
        response = web.StreamResponse(status=upstream.status_code, headers=headers)
        await response.prepare(request)
        try:
            async for chunk in _iter_upstream(upstream):
                await response.write(chunk)
        except Exception as exc:  # noqa: BLE001 — 流式中断不应让整个请求 500
            logger.warning("缩略图流式转发中断: %s", exc)
        finally:
            upstream.close()
        await response.write_eof()
        return response

    @routes.get(f"{ROUTE_PREFIX}/assets/{{asset_id}}/original")
    async def asset_original(request: web.Request) -> web.Response:
        """资产原文件代理（图片大图 / 视频播放）。

        - fetch + blob 播放：前端先 GET（带鉴权 header）整读再播放，故无需 Range 支持
        - 透传 Content-Type / Content-Length / ETag / Last-Modified / Cache-Control
        - 客户端 _request 禁重定向：直接调 /original 端点，不走 fullsize 302
        """
        if not _check_auth(request, config):
            return _unauthorized()
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
        except Exception as exc:  # noqa: BLE001 — 流式中断不应让整个请求 500
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
        """
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
        # 部分成功：返回 200 + failed 明细；全部失败 → 502
        if result.get("updated", 0) == 0 and result.get("failed"):
            return _error("资产更新失败", 502)
        return web.json_response(result)

    @routes.put(f"{ROUTE_PREFIX}/assets/{{asset_id}}")
    async def asset_update(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        asset_id = request.match_info["asset_id"]
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
        """批量删除：DELETE /assets + body {ids}。删除总进回收站（v2 实测）。"""
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

    # ---------- 异步上传任务 ----------

    @routes.get(f"{ROUTE_PREFIX}/jobs/{{job_id}}")
    async def job_status(request: web.Request) -> web.Response:
        if not _check_auth(request, config):
            return _unauthorized()
        job = job_manager.get(request.match_info["job_id"])
        if job is None:
            return _error("任务不存在", 404)
        return web.json_response(job)

    # ---------- 相册 ----------

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
    """错误信息脱敏：不回传 Immich 内部细节/路径，只回状态码。"""
    if exc.status_code:
        return f"Immich 请求失败 (HTTP {exc.status_code})"
    return "Immich 连接失败"


async def _iter_upstream(upstream):
    """分块读取上游响应（在 executor 里跑，避免阻塞事件循环）。"""
    loop = asyncio.get_running_loop()
    while True:
        try:
            chunk = await loop.run_in_executor(None, upstream.raw.read, 64 * 1024)
        except Exception:  # noqa: BLE001 — 上游断流按流结束处理
            break
        if not chunk:
            break
        yield chunk
