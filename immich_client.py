"""Immich REST 客户端。

全插件唯一调 Immich API 的模块（路由、上传节点都经它）。
已按 Immich v2.7.5 实测校准（见 docs/PLAN.md 第 9 节）：
- 资产列表 = /timeline/buckets + /timeline/bucket（列式返回）
- 删除 = DELETE /assets + body {ids}（v2 无单资产删除端点）
- 更新 = PUT /assets/{id}（v2 不是 PATCH）
- 上传 = POST /assets multipart，必须带 deviceAssetId/deviceId
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

# Immich UpdateAssetDto 中允许前端修改的字段白名单
UPDATE_ASSET_FIELDS = {"isFavorite", "description", "rating", "visibility"}

# 合法文件扩展名：字母数字、1~10 字符（防 rsplit 解析出路径片段/脏串）
_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

# 疑似凭据形状：20+ 位字母/数字/下划线连续串（无连字符——uuid 8-4-4-4-12 含
# 连字符会被保留，排查时仍能辨认资产 id；Immich API key 为无连字符 hex）
_SENSITIVE_RE = re.compile(r"\b[A-Za-z0-9_]{20,}\b")


def _redact(text: str) -> str:
    """脱敏错误详情文本：遮罩疑似长凭据串（防进 ComfyUI 日志）。"""
    return _SENSITIVE_RE.sub("***", text)


def _extract_ext(path: str) -> str:
    """从 originalPath 提取文件扩展名（小写）。

    只接受纯字母数字 1~10 字符：`2/img`（无扩展名）、`dir.with.dot/file`（点目录）
    这类脏串不会产出 "img"/"dot" 之类假 ext，避免视频被误判成图片。
    """
    if not path:
        return ""
    # 隐藏文件（如 .hidden）或文件名本身以点开头：无扩展名
    if path.rsplit("/", 1)[-1].startswith("."):
        return ""
    _, sep, tail = path.rpartition(".")
    if not sep or not tail:
        return ""
    if not _EXT_RE.match(tail):
        return ""
    return tail.lower()


class ImmichError(Exception):
    """Immich API 调用失败。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = DEFAULT_TIMEOUT):
        # SSRF 第二道防线：即使绕过配置层，客户端也拒绝非 http/https base_url
        try:
            from .config import normalize_base_url
        except ImportError:  # noqa: BLE001 — 直接脚本导入（测试）
            from config import normalize_base_url

        try:
            self.base_url = normalize_base_url(base_url or "")
        except ValueError as exc:
            raise ImmichError(f"非法的 base_url: {exc}") from exc
        self.api_key = api_key or ""
        self.timeout = timeout
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"x-api-key": self.api_key})
        self.session.headers.update({"Accept": "application/json"})

    # ---------- 内部 ----------

    def _url(self, path: str) -> str:
        """拼接完整 API URL。"""
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        # SSRF 防线：禁止重定向，防 base_url 可控域名 302 到内网/metadata
        kwargs.setdefault("allow_redirects", False)
        try:
            resp = self.session.request(method, self._url(path), **kwargs)
        except requests.RequestException as exc:
            raise ImmichError(f"Immich 连接失败: {exc}") from exc
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

    # ---------- 服务器 ----------

    def ping(self) -> bool:
        """服务器存活探测（/server/ping，失败返回 False 不抛错）。"""
        try:
            resp = self._request("GET", "/server/ping")
            return resp.status_code == 200
        except ImmichError:
            return False

    def version(self) -> dict:
        """服务端版本（公开端点，health 校验地址可达用）。"""
        return self._json("GET", "/server/version")

    # ---------- 时间轴（资产列表） ----------

    def get_buckets(self, order: str = "desc") -> list[dict]:
        """时间桶列表，如 [{timeBucket: '2026-08-01', count: 557}]（月粒度）。"""
        data = self._json("GET", "/timeline/buckets", params={"order": order})
        return data if isinstance(data, list) else []

    def get_bucket(self, time_bucket: str, order: str = "desc") -> list[dict]:
        """桶内资产。Immich 返回列式结构（各字段为数组），这里转成对象数组。"""
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

    # ---------- 资产 ----------

    def get_asset(self, asset_id: str) -> dict:
        return self._json("GET", f"/assets/{asset_id}")

    def get_thumbnail(self, asset_id: str, size: str = "preview", headers: dict | None = None) -> requests.Response:
        """缩略图，返回原始响应（流式转发用，不整读进内存）。

        headers 用于在**发请求前**透传条件请求头（If-None-Match 等），
        让 Immich 能回 304 命中浏览器缓存。
        """
        kwargs: dict = {"params": {"size": size}, "stream": True}
        if headers:
            kwargs["headers"] = headers
        return self._request("GET", f"/assets/{asset_id}/thumbnail", **kwargs)

    def get_original(self, asset_id: str, headers: dict | None = None) -> requests.Response:
        """资产原文件（图片 PNG / 视频 MP4），流式转发用。

        注意：客户端 _request 禁重定向（SSRF 防线），原文件直接走 /original 端点，
        不走 fullsize 的 302（避免跟随 Location 引入攻击面）。
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
        """更新资产（v2 是 PUT）。字段白名单过滤，防前端传任意字段。"""
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
        """
        if not asset_ids:
            return True
        resp = self._request(
            "DELETE", "/assets", json={"ids": asset_ids, "force": force}
        )
        return resp.status_code in (200, 204)

    # ---------- 相册 ----------

    def get_albums(self) -> list[dict]:
        """相册列表（节点按名加入相册时先用它查找）。"""
        data = self._json("GET", "/albums")
        return data if isinstance(data, list) else []

    def find_album(self, name: str) -> dict | None:
        """按名查找相册，找不到返回 None。"""
        for album in self.get_albums():
            if album.get("albumName") == name:
                return album
        return None

    def create_album(self, name: str) -> dict:
        """创建相册（按名幂等性由 ensure_album 的"先查后建"保证）。"""
        return self._json("POST", "/albums", json={"albumName": name})

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> dict:
        """批量加入相册。"""
        return self._json("PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids})

    def ensure_album(self, name: str) -> dict | None:
        """按名查找相册，不存在则创建。返回相册对象。"""
        if not name:
            return None
        album = self.find_album(name)
        if album is None:
            album = self.create_album(name)
        return album

    # ---------- 上传 ----------

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
        """内存直传。返回 {id, status}（status: created | duplicate）。"""
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        files = {"assetData": (filename, data, mime_type)}
        # deviceAssetId 用随机 uuid（不依赖文件名/内容，避免弱去重）
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
        """
        if not os.path.isfile(file_path):
            raise ImmichError(f"文件不存在: {file_path}")
        filename = filename or os.path.basename(file_path)
        # 简化 MIME 推断（Immich 支持常见图片/视频）
        ext = os.path.splitext(filename)[1].lower()
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
            ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
        }.get(ext, "application/octet-stream")
        # 大文件流式：先算 sha1（分块），再上传（分块）
        sha1 = hashlib.sha1()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                sha1.update(chunk)
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        # deviceAssetId 用随机 uuid
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
