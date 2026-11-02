"""Immich REST 客户端。

全插件唯一调 Immich API 的模块（路由、上传节点都经它）。
已按 Immich v2.7.5 实测校准：
- 资产列表 = /timeline/buckets + /timeline/bucket（列式返回）
- 删除 = DELETE /assets + body {ids}（v2 无单资产删除端点）
- 更新 = PUT /assets/{id}（v2 不是 PATCH）
- 上传 = POST /assets multipart，必须带 deviceAssetId/deviceId

Immich REST client.

The only module in the plugin that talks to the Immich API (routes and the
upload node all go through it). Calibrated against a real Immich v2.7.5:
- asset listing = /timeline/buckets + /timeline/bucket (columnar response)
- delete = DELETE /assets + body {ids} (v2 has no single-asset delete endpoint)
- update = PUT /assets/{id} (v2 does not use PATCH)
- upload = POST /assets multipart, must carry deviceAssetId/deviceId
"""

import hashlib
import io
import os
import re
import time
import uuid
from typing import Any, BinaryIO

import requests

DEFAULT_TIMEOUT = 30

# Immich UpdateAssetDto 中允许前端修改的字段白名单  # allow-list of the fields the frontend may change in Immich's UpdateAssetDto
UPDATE_ASSET_FIELDS = {"isFavorite", "description", "rating", "visibility"}

# 合法文件扩展名：字母数字、1~10 字符（防 rsplit 解析出路径片段/脏串）  # valid file extensions: alphanumeric, 1–10 chars (stops rsplit from yielding path fragments / junk strings)
_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

# 疑似凭据形状：20+ 位字母/数字/下划线连续串（无连字符——uuid 8-4-4-4-12 含
# 连字符会被保留，排查时仍能辨认资产 id；Immich API key 为无连字符 hex）
# Credential-looking shape: a run of 20+ letters/digits/underscores (no hyphen —
# a uuid 8-4-4-4-12 keeps its hyphens, so an asset id stays recognisable while
# debugging; Immich API keys are hyphen-free hex)
_SENSITIVE_RE = re.compile(r"\b[A-Za-z0-9_]{20,}\b")


def _redact(text: str) -> str:
    """脱敏错误详情文本：遮罩疑似长凭据串（防进 ComfyUI 日志）。English: redact error detail text — mask credential-looking long strings so they never reach the ComfyUI log."""
    return _SENSITIVE_RE.sub("***", text)


def _extract_ext(path: str) -> str:
    """从 originalPath 提取文件扩展名（小写）。

    只接受纯字母数字 1~10 字符：`2/img`（无扩展名）、`dir.with.dot/file`（点目录）
    这类脏串不会产出 "img"/"dot" 之类假 ext，避免视频被误判成图片。

    English: extract the file extension from originalPath (lowercase).

    Only pure alphanumeric strings of 1–10 chars are accepted: junk such as
    `2/img` (no extension) or `dir.with.dot/file` (dot directory) never yields a
    fake ext like "img"/"dot", so a video is not mistaken for an image.
    """
    if not path:
        return ""
    # 隐藏文件（如 .hidden）或文件名本身以点开头：无扩展名  # hidden file (e.g. .hidden) or a basename starting with a dot: no extension
    if path.rsplit("/", 1)[-1].startswith("."):
        return ""
    _, sep, tail = path.rpartition(".")
    if not sep or not tail:
        return ""
    if not _EXT_RE.match(tail):
        return ""
    return tail.lower()


class ImmichError(Exception):
    """Immich API 调用失败。English: an Immich API call failed."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _ext_of(name) -> str:
    """从原文件名取扩展名（小写，无点）。English: take the extension from the original file name (lowercase, no dot)."""
    n = str(name or "")
    return n.rsplit(".", 1)[-1].lower() if "." in n else ""


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = DEFAULT_TIMEOUT,
        proxy_mode: str = "off",
        proxy_url: str = "",
    ):
        # SSRF 第二道防线：即使绕过配置层，客户端也拒绝非 http/https base_url  # SSRF second line of defence: even if the config layer is bypassed, the client rejects a non-http/https base_url
        try:
            from .config import normalize_base_url, resolve_proxy_settings
        except ImportError:  # noqa: BLE001 — 直接脚本导入（测试）  # plain script import (tests)
            from config import normalize_base_url, resolve_proxy_settings

        try:
            self.base_url = normalize_base_url(base_url or "")
        except ValueError as exc:
            raise ImmichError(f"非法的 base_url: {exc}") from exc
        self.api_key = api_key or ""
        self.timeout = timeout
        # 代理策略（面板「🌐 网络代理」）：
        # - off（默认）：trust_env=False → 忽略 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY，
        #   局域网 Immich 直连。防止"启动脚本设了代理、代理又没运行"时
        #   局域网请求被劫持到死代理（WinError 10061 / ProxyError）。
        # - custom：显式走用户配置的代理地址；同样关掉 trust_env，避免环境变量二次劫持。
        # - env：trust_env=True → 沿用启动 ComfyUI 时的环境变量（旧行为，向后兼容）。
        # Proxy policy (panel "🌐 Network proxy"):
        # - off (default): trust_env=False → ignore HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
        #   and reach the LAN Immich directly. This prevents "the launcher set a
        #   proxy but the proxy is not running" from hijacking LAN requests to a
        #   dead proxy (WinError 10061 / ProxyError).
        # - custom: explicitly use the user-configured proxy address; trust_env is
        #   off here too so environment variables cannot hijack it a second time.
        # - env: trust_env=True → inherit the environment variables of the ComfyUI
        #   process (old behaviour, kept for backward compatibility).
        self.proxy_mode, self.proxy_url = resolve_proxy_settings(proxy_mode, proxy_url)
        self.session = requests.Session()
        if self.proxy_mode == "custom":
            self.session.trust_env = False
            self.session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})
        elif self.proxy_mode == "env":
            self.session.trust_env = True
        else:
            self.session.trust_env = False
        if self.api_key:
            self.session.headers.update({"x-api-key": self.api_key})
        self.session.headers.update({"Accept": "application/json"})

    # ---------- 内部 / Internals ----------

    def _url(self, path: str) -> str:
        """拼接完整 API URL。English: build the complete API URL."""
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        # SSRF 防线：禁止重定向，防 base_url 可控域名 302 到内网/metadata  # SSRF guard: no redirects, so a controllable base_url domain cannot 302 into the LAN / metadata
        kwargs.setdefault("allow_redirects", False)
        try:
            resp = self.session.request(method, self._url(path), **kwargs)
        except requests.RequestException as exc:
            # 走自定义代理时把代理地址带进报错，便于区分"代理挂了"和"Immich 挂了"  # with a custom proxy, include the proxy address in the error so "proxy is down" can be told from "Immich is down"
            hint = f"（当前经代理 {self.proxy_url}）" if self.proxy_mode == "custom" else ""
            raise ImmichError(f"Immich 连接失败{hint}: {exc}") from exc
        if resp.status_code == 304:
            return resp
        if resp.status_code >= 300:
            detail = _redact(resp.text[:300])
            raise ImmichError(
                f"Immich {method} {path} -> HTTP {resp.status_code}: {detail}",
                resp.status_code,
            )
        return resp

    def _json(self, method: str, path: str, **kwargs) -> Any:
        return self._request(method, path, **kwargs).json()

    # ---------- 服务器 / Server ----------

    def ping(self) -> bool:
        """服务器存活探测（/server/ping，失败返回 False 不抛错）。English: server liveness probe (/server/ping; returns False instead of raising on failure)."""
        try:
            resp = self._request("GET", "/server/ping")
            return resp.status_code == 200
        except ImmichError:
            return False

    def version(self) -> dict:
        """服务端版本（公开端点，health 校验地址可达用）。English: server version (public endpoint; health uses it to check the address is reachable)."""
        return self._json("GET", "/server/version")

    # ---------- 时间轴（资产列表） / Timeline (asset listing) ----------

    def get_buckets(self, order: str = "desc") -> list[dict]:
        """时间桶列表，如 [{timeBucket: '2026-08-01', count: 557}]（月粒度）。English: time bucket list, e.g. [{timeBucket: '2026-08-01', count: 557}] (month granularity)."""
        data = self._json("GET", "/timeline/buckets", params={"order": order})
        return data if isinstance(data, list) else []

    def get_bucket(self, time_bucket: str, order: str = "desc") -> list[dict]:
        """桶内资产。Immich 返回列式结构（各字段为数组），这里转成对象数组。English: assets inside a bucket. Immich returns a columnar structure (every field is an array); converted into an array of objects here."""
        data = self._json(
            "GET", "/timeline/bucket",
            params={"timeBucket": time_bucket, "order": order},
        )
        if not isinstance(data, dict) or not isinstance(data.get("id"), list):
            return []
        ids = data["id"]
        keys = list(data.keys())
        assets = []
        for i in range(len(ids)):
            row = {}
            for k in keys:
                col = data.get(k)
                if isinstance(col, list) and i < len(col):
                    row[k] = col[i]
            assets.append(row)
        return assets

    # ---------- 资产 / Assets ----------

    def get_asset(self, asset_id: str) -> dict:
        return self._json("GET", f"/assets/{asset_id}")

    def get_thumbnail(self, asset_id: str, size: str = "preview", headers: dict | None = None) -> requests.Response:
        """缩略图，返回原始响应（流式转发用，不整读进内存）。

        headers 用于在**发请求前**透传条件请求头（If-None-Match 等），
        让 Immich 能回 304 命中浏览器缓存。

        English: thumbnail; returns the raw response (for streaming pass-through
        instead of reading it all into memory).

        headers passes conditional request headers (If-None-Match etc.) through
        **before the request is sent**, so Immich can answer 304 and hit the
        browser cache.
        """
        kwargs: dict = {"params": {"size": size}, "stream": True}
        if headers:
            kwargs["headers"] = headers
        return self._request("GET", f"/assets/{asset_id}/thumbnail", **kwargs)

    def get_original(self, asset_id: str, headers: dict | None = None) -> requests.Response:
        """资产原文件（图片 PNG / 视频 MP4），流式转发用。

        注意：客户端 _request 禁重定向（SSRF 防线），原文件直接走 /original 端点，
        不走 fullsize 的 302（避免跟随 Location 引入攻击面）。

        English: the asset's original file (image PNG / video MP4), for streaming
        pass-through.

        Note: the client's _request forbids redirects (SSRF defence), so the
        original goes straight to the /original endpoint instead of the fullsize
        302 (following Location would add attack surface).
        """
        kwargs: dict = {"stream": True}
        if headers:
            kwargs["headers"] = headers
        return self._request("GET", f"/assets/{asset_id}/original", **kwargs)

    def asset_ext_map(self) -> dict[str, str]:
        """全量资产 id → 文件扩展名映射（格式角标用）。

        桶列表（列式响应）不带扩展名，Immich 也没有批量详情端点；
        用 search/metadata 分页拉全（实测 size=1000 一次可取 1000 条，含 originalPath）。
        返回 {asset_id: "png"|"mp4"|...}。

        English: full map of asset id → file extension (used by the format badge).

        Bucket listings (columnar response) carry no extension, and Immich has no
        bulk detail endpoint; so page through search/metadata instead (verified:
        size=1000 returns 1000 entries at a time, including originalPath).
        Returns {asset_id: "png"|"mp4"|...}.
        """
        ext_map: dict[str, str] = {}
        page = 1
        while True:
            try:
                data = self._json(
                    "POST", "/search/metadata",
                    json={"page": page, "size": 1000, "withExif": False},
                )
            except ImmichError:
                break
            items = ((data or {}).get("assets") or {}).get("items") or []
            if not items:
                break
            for a in items:
                op = (a or {}).get("originalPath") or ""
                ext = _extract_ext(op)
                if a.get("id"):
                    ext_map[a["id"]] = ext
            total = ((data or {}).get("assets") or {}).get("total") or 0
            if len(items) < 1000 or page * 1000 >= total:
                break
            page += 1
        return ext_map

    def update_asset(self, asset_id: str, **fields) -> dict:
        """更新资产（v2 是 PUT）。字段白名单过滤，防前端传任意字段。English: update an asset (v2 uses PUT). Fields are filtered through an allow-list so the frontend cannot pass arbitrary ones."""
        body = {
            k: v for k, v in fields.items()
            if k in UPDATE_ASSET_FIELDS and v is not None
        }
        if not body:
            return self.get_asset(asset_id)
        return self._json("PUT", f"/assets/{asset_id}", json=body)

    def update_assets_bulk(self, asset_ids: list[str], is_favorite: bool) -> dict:
        """批量收藏/取消收藏（set 语义，幂等）。

        Immich 有原生批量端点 PUT /assets（body {ids, isFavorite}），但官方标了
        Deprecated（实测 v2.7.5 仍可用，204 生效；混入不可更新 id 时 400 全败）。
        优先批量端点一次请求；失败时降级逐条 PUT /assets/{id}（防端点被移除后
        功能瘫痪，且能提供"部分成功"语义——批量 400 时循环单条仍可收藏能收的）。

        返回 {"updated": int, "failed": [asset_id, ...]}。

        English: bulk favourite/unfavourite (set semantics, idempotent).

        Immich has a native bulk endpoint PUT /assets (body {ids, isFavorite}),
        but it is marked Deprecated (verified on v2.7.5: still works, 204 applies;
        mixing in a non-updatable id fails the whole batch with 400). The bulk
        endpoint is tried first in a single request; on failure it degrades to
        one-by-one PUT /assets/{id} (so the feature survives the endpoint being
        removed, and it can provide "partial success" semantics — after a 400 the
        per-asset loop still favourites what it can).

        Returns {"updated": int, "failed": [asset_id, ...]}.
        """
        if not asset_ids:
            return {"updated": 0, "failed": []}
        fav = bool(is_favorite)
        try:
            resp = self._request("PUT", "/assets", json={"ids": asset_ids, "isFavorite": fav})
            if resp.status_code in (200, 204):
                return {"updated": len(asset_ids), "failed": []}
        except ImmichError as exc:
            # 只对 4xx 降级（端点移除 404 / 部分无效 400 是 fallback 的初衷）；
            # 5xx/网络断循环单条必然同样失败，直接抛——避免 N×3 次指数退避
            # 重试（最多 1000 个 id 时延迟分钟级且前端无反馈）。
            # Only degrade on 4xx (a removed endpoint 404 / partly invalid 400 is
            # what the fallback is for); on 5xx or a broken connection the per-asset
            # loop would fail just the same, so re-raise — that avoids N×3
            # exponential-backoff retries (minutes of delay with up to 1000 ids and
            # no feedback in the frontend).
            if not (exc.status_code and 400 <= exc.status_code < 500):
                raise
        updated = 0
        failed = []
        for aid in asset_ids:
            try:
                self.update_asset(aid, isFavorite=fav)
                updated += 1
            except ImmichError:
                failed.append(aid)
        return {"updated": updated, "failed": failed}

    def delete_assets(self, asset_ids: list[str], force: bool = False) -> bool:
        """删除资产（v2 批量端点：DELETE /assets + body {ids}）。

        实测（v2.7.5）：无论 force 与否，资产都进回收站（isTrashed=true）。
        force 仅表示"即使被引用也删除"。彻底删除只能清空整个回收站
        （POST /trash/empty），影响面太大，本插件不提供。

        English: delete assets (v2 bulk endpoint: DELETE /assets + body {ids}).

        Verified on v2.7.5: with or without force, assets go to the trash
        (isTrashed=true). force only means "delete even if referenced". Permanent
        deletion would require emptying the whole trash (POST /trash/empty), which
        affects far too much, so this plugin does not offer it.
        """
        if not asset_ids:
            return True
        resp = self._request(
            "DELETE", "/assets", json={"ids": asset_ids, "force": force}
        )
        return resp.status_code in (200, 204)

    # ---------- 相册 / Albums ----------

    def get_albums(self) -> list[dict]:
        """相册列表（节点按名加入相册时先用它查找）。English: album list (the node uses it to look up the album by name before adding)."""
        data = self._json("GET", "/albums")
        return data if isinstance(data, list) else []

    def find_album(self, name: str) -> dict | None:
        """按名查找相册，找不到返回 None。English: find an album by name; returns None when there is none."""
        for album in self.get_albums():
            if album.get("albumName") == name:
                return album
        return None

    def get_album_assets(self, album_id: str, page: int = 1, size: int = 24,
                         asset_type: str = "", order: str = "desc") -> dict:
        """相册内的资产（服务端分页）。

        不用 GET /albums/{id}（那个默认不展开 assets），改用 search/metadata
        按 albumIds 过滤 —— 它支持真正的分页，几千张的相册也不会一次全取。

        English: assets inside an album (server-side pagination).

        GET /albums/{id} is not used (it does not expand assets by default);
        search/metadata filtered by albumIds is used instead — it supports real
        pagination, so an album with thousands of shots is never fetched at once.
        """
        page = max(1, int(page or 1))
        size = max(1, min(200, int(size or 24)))
        body: dict = {"albumIds": [album_id], "page": page, "size": size, "withExif": False}
        # 这里原本按 favorite 过滤，但本函数没有 favorite 参数 —— 一旦执行到就是
        # NameError。收藏筛选走 search_assets()，因此把这段死代码删掉。
        # This used to filter by favourite, but the function takes no such
        # parameter, so every call raised NameError. Favourite filtering
        # belongs to search_assets(); the dead code is removed.
        if asset_type in ("IMAGE", "VIDEO"):
            body["type"] = asset_type          # 类型筛选：图片 / 视频  # type filter: image / video
        if order in ("asc", "desc"):
            body["order"] = order              # 排序：按拍摄时间  # ordering: by capture time
        res = self._json("POST", "/search/metadata", json=body)
        box = (res or {}).get("assets") or {}
        raw = box.get("items") or []
        # 这版 Immich 的 assets.total 等于本页条数，不是总数；
        # 用 nextPage 判断是否还有下一页（它为空即到底）。
        # In this Immich version assets.total equals the number of rows on this
        # page, not the grand total; nextPage is used to tell whether another page
        # exists (empty means this is the end).
        next_page = box.get("nextPage")
        total = box.get("total") if (box.get("total") or 0) > len(raw) else None
        items = []
        for a in raw:
            items.append({
                "id": a.get("id"),
                "originalFileName": a.get("originalFileName"),
                "type": a.get("type"),
                "fileCreatedAt": a.get("fileCreatedAt"),
                "fileModifiedAt": a.get("fileModifiedAt"),
                "thumbhash": a.get("thumbhash"),
            })
        return {
            "albumName": None,
            "total": total,
            "page": page,
            "size": size,
            "nextPage": next_page,
            "items": items,
        }

    def get_people(self) -> list[dict]:
        """人脸列表（按出现次数排序，前端做下拉用）。English: face (people) list sorted by occurrence count, used by the frontend dropdown."""
        data = self._json("GET", "/people")
        people = (data or {}).get("people") if isinstance(data, dict) else data
        out = []
        for p in people or []:
            out.append({"id": p.get("id"), "name": p.get("name"), "isHidden": p.get("isHidden")})
        return out

    def get_tags(self) -> list[dict]:
        """标签列表。English: tag list."""
        data = self._json("GET", "/tags")
        out = []
        for t in data or []:
            out.append({"id": t.get("id"), "name": t.get("value") or t.get("name")})
        return out

    def search_assets(self, album_ids=None, person_ids=None, tag_ids=None,
                      taken_after="", taken_before="", asset_type="",
                      favorite: str = "",
                      page: int = 1, size: int = 24, order: str = "desc") -> dict:
        """按条件搜索资产（服务端分页）。

        Immich 的 search/metadata 只接受 id 精确匹配与时间范围，
        没有文件名/名称模糊匹配 —— 前端因此全部用下拉，不做文本搜索。

        English: search assets by criteria (server-side pagination).

        Immich's search/metadata only accepts exact id matches and time ranges,
        with no fuzzy matching on file names / names — that is why the frontend
        uses dropdowns everywhere and offers no text search.
        """
        page = max(1, int(page or 1))
        size = max(1, min(200, int(size or 24)))
        body: dict = {"page": page, "size": size, "withExif": False}
        if album_ids:
            body["albumIds"] = list(album_ids)
        if person_ids:
            body["personIds"] = list(person_ids)
        if tag_ids:
            body["tagIds"] = list(tag_ids)
        if taken_after:
            body["takenAfter"] = taken_after
        if taken_before:
            body["takenBefore"] = taken_before
        if favorite in ("1", "true", "True"):
            body["isFavorite"] = True          # 只看收藏  # favourites only
        elif favorite in ("0", "false", "False"):
            body["isFavorite"] = False         # 只看未收藏  # non-favourites only
        if asset_type in ("IMAGE", "VIDEO"):
            body["type"] = asset_type
        if order in ("asc", "desc"):
            body["order"] = order

        res = self._json("POST", "/search/metadata", json=body)
        box = (res or {}).get("assets") or {}
        raw = box.get("items") or []
        return {
            "total": box.get("total") if (box.get("total") or 0) > len(raw) else None,
            "page": page,
            "size": size,
            "nextPage": box.get("nextPage"),
            "items": [{
                "id": a.get("id"),
                "originalFileName": a.get("originalFileName"),
                "type": a.get("type"),
                # ext / isImage 时间轴渲染要用（判断视频、显示格式角标）  # ext / isImage are needed by the timeline render (detect video, show the format badge)
                "ext": _ext_of(a.get("originalFileName")),
                "isImage": a.get("type") != "VIDEO",
                "duration": a.get("duration"),
                "isFavorite": bool(a.get("isFavorite")),   # 收藏状态（角标爱心用）  # favourite state (for the heart badge)
                "fileCreatedAt": a.get("fileCreatedAt"),
                "thumbhash": a.get("thumbhash"),
            } for a in raw],
        }

    def create_album(self, name: str) -> dict:
        """创建相册（按名幂等性由 ensure_album 的"先查后建"保证）。English: create an album (name idempotency is guaranteed by ensure_album's "look up first, then create")."""
        return self._json("POST", "/albums", json={"albumName": name})

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> dict:
        """批量加入相册。English: add assets to an album in bulk."""
        return self._json("PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids})

    def ensure_album(self, name: str) -> dict | None:
        """按名查找相册，不存在则创建。返回相册对象。English: find an album by name, creating it when missing. Returns the album object."""
        if not name:
            return None
        album = self.find_album(name)
        if album is None:
            album = self.create_album(name)
        return album

    # ---------- 上传 / Upload ----------

    @staticmethod
    def _sha1(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def upload_bytes(
        self,
        data: bytes,
        filename: str,
        mime_type: str,
        created_at: str | None = None,
        checksum: str | None = None,
        is_favorite: bool = False,
    ) -> dict:
        """内存直传。返回 {id, status}（status: created | duplicate）。English: upload straight from memory. Returns {id, status} (status: created | duplicate)."""
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        files = {"assetData": (filename, data, mime_type)}
        # deviceAssetId 用随机 uuid（不依赖文件名/内容，避免弱去重）  # deviceAssetId is a random uuid (not derived from name/content, avoiding weak deduplication)
        form = {
            "deviceAssetId": str(uuid.uuid4()),
            "deviceId": "comfyui-immichmanager",
            "fileCreatedAt": created_at,
            "fileModifiedAt": created_at,
            "isFavorite": "true" if is_favorite else "false",
        }
        checksum = checksum or self._sha1(data)
        resp = self._request(
            "POST", "/assets",
            headers={"x-immich-checksum": checksum},
            files=files, data=form,
        )
        return resp.json()

    def upload_file(
        self,
        file_path: str,
        created_at: str | None = None,
        is_favorite: bool = False,
        filename: str | None = None,
    ) -> dict:
        """按路径上传（视频等非图片文件）。流式读文件，不整载入内存。

        filename 可覆盖上传展示名（默认取文件 basename）；MIME 按扩展名推断。

        English: upload from a path (videos and other non-image files). The file is
        read as a stream, never fully loaded into memory.

        filename can override the displayed upload name (defaults to the file
        basename); MIME is inferred from the extension.
        """
        if not os.path.isfile(file_path):
            raise ImmichError(f"文件不存在: {file_path}")
        filename = filename or os.path.basename(file_path)
        # 简化 MIME 推断（Immich 支持常见图片/视频）  # simplified MIME inference (Immich supports the common image/video types)
        ext = os.path.splitext(filename)[1].lower()
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
            ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
        }.get(ext, "application/octet-stream")
        # 大文件流式：先算 sha1（分块），再上传（分块）  # large files are streamed: hash sha1 in chunks first, then upload in chunks
        sha1 = hashlib.sha1()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                sha1.update(chunk)
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        # deviceAssetId 用随机 uuid  # deviceAssetId is a random uuid
        form = {
            "deviceAssetId": str(uuid.uuid4()),
            "deviceId": "comfyui-immichmanager",
            "fileCreatedAt": created_at,
            "fileModifiedAt": created_at,
            "isFavorite": "true" if is_favorite else "false",
        }
        with open(file_path, "rb") as fh:
            resp = self._request(
                "POST", "/assets",
                headers={"x-immich-checksum": sha1.hexdigest()},
                files={"assetData": (filename, fh, mime)},
                data=form,
            )
        return resp.json()
