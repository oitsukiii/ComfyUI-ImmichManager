"""ComfyUI-ImmichManager 上传节点（P3 简化版）。

🖼️ ImmichSaveImage（需求 14/15/16，替代旧 ImmichUpload，不保留 legacy）：
- 输入：images（IMAGE）+ filename_prefix；隐藏 prompt/extra_pnginfo
- 固定 PNG 上传（工作流 prompt/workflow 写入 PNG metadata，
  从 Immich 下载该 PNG 后拖回 ComfyUI 可恢复工作流）
- 本地 temp 保存一份用于节点预览（模仿官方 SaveImage/PreviewImage 的 ui.images）
- base_url/api_key/相册走面板配置（config.json），节点不再暴露连接/同步输入槽

🎬 ImmichSaveVideo（需求 17，P4 改为 VIDEO 输入，参考官方 SaveVideo）：
- 输入：video（官方 VIDEO 类型，来自 CreateVideo/LoadVideo 等）+ filename_prefix；
  隐藏 prompt/extra_pnginfo
- 用官方 VideoInput.save_to()（PyAV）落盘 MP4 h264——CreateVideo 来的组件由 PyAV
  编码；LoadVideo 来的文件（mp4+h264）纯 copy 不重编码；prompt/workflow 由官方
  机制写容器 metadata（use_metadata_tags），Immich 下载视频拖回可恢复工作流
- 本地 temp 保存 .mp4（节点视频预览），同时上传 Immich；连接/相册走面板配置

兼容保留：UploadJobManager / job_manager（server_routes 的 /jobs 端点仍引用）。

上传纪律（沿用旧节点）：
- 去重：x-immich-checksum（sha1）；重试：3 次指数退避（仅网络/5xx/429/408）
- 全局并发上限 3（信号量）；相册创建/加入带锁
*English translation:*
* ComfyUI-ImmichManager upload nodes (P3 simplified).
* 🖼️ ImmichSaveImage (requirements 14/15/16, replaces the old ImmichUpload; no legacy is kept):
* Inputs: images (IMAGE) + filename_prefix; hidden prompt/extra_pnginfo
* Fixed PNG upload (the workflow prompt/workflow is written into the PNG metadata, so downloading
* the PNG from Immich and dragging it back into ComfyUI restores the workflow)
* One copy is saved to the local temp dir for the node preview (mimics the official SaveImage/PreviewImage ui.images)
* base_url/api_key/album come from the panel config (config.json); the node no longer exposes connection/sync input slots
* 🎬 ImmichSaveVideo (requirement 17, P4 switches to a VIDEO input, modelled on the official SaveVideo):
* Inputs: video (official VIDEO type, from CreateVideo/LoadVideo etc.) + filename_prefix;
* hidden prompt/extra_pnginfo
* The official VideoInput.save_to() (PyAV) writes an MP4 h264 — components from CreateVideo are encoded
* by PyAV; files from LoadVideo (mp4+h264) are copied without re-encoding; prompt/workflow are written
* into the container metadata by the official mechanism (use_metadata_tags), so a video downloaded from
* Immich and dragged back restores the workflow
* A .mp4 is saved to the local temp dir (node video preview) and uploaded to Immich; connection/album come from the panel config
* Kept for compatibility: UploadJobManager / job_manager (the /jobs endpoint in server_routes still references them).
* Upload discipline (kept from the old node):
* Deduplication: x-immich-checksum (sha1); retries: 3 times with exponential backoff (network/5xx/429/408 only)
* Global concurrency cap of 3 (semaphore); album creation/joining is locked
"""

import io
import json
import os
import tempfile
import threading
import time
import uuid

import numpy as np

# 兼容包内导入（ComfyUI 加载）与直接脚本导入（测试）  # Works for both in-package imports (ComfyUI loading) and direct script imports (tests)
try:
    from .config import get_config
    from .immich_client import ImmichClient, ImmichError
except ImportError:  # noqa: BLE001 — 直接 python3 运行（测试/调试）  # running as a plain python3 script (tests/debugging)
    from config import get_config
    from immich_client import ImmichClient, ImmichError

# 与 server_routes 共用同一 config 实例（面板配置变更即时对节点生效）
# Share the same config instance as server_routes (panel config changes reach the nodes immediately)
_CONFIG = get_config()

# 全局并发上限（防多节点同时上传导致内存/连接爆炸）
# Global concurrency cap (several nodes uploading at once cannot blow up memory/connections)
UPLOAD_CONCURRENCY = 3
_UPLOAD_SEMAPHORE = threading.Semaphore(UPLOAD_CONCURRENCY)

# 相册创建/加入的全局锁（防并发建同名相册）  # Global lock for album creation/joining (no two albums with the same name)
_ALBUM_LOCK = threading.Lock()

# 重试：3 次指数退避（1s/2s）  # Retries: 3 attempts with exponential backoff (1s/2s)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0


# ============================================================
#  异步任务登记表（兼容保留：server_routes /jobs 端点引用）
#  Async upload job registry (kept for compatibility: referenced by the server_routes /jobs endpoint)
# ============================================================

class UploadJobManager:
    """异步上传任务登记表（线程安全）。P3 起节点不再创建任务，仅兼容旧端点。
    *English translation:* async upload job registry (thread-safe). Since P3 the nodes no longer create jobs; kept only for the old endpoint.
    """

    def __init__(self, max_jobs: int = 200):
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._max_jobs = max_jobs

    def create(self, total: int) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            if len(self._jobs) >= self._max_jobs:
                for old_id in list(self._jobs):
                    if self._jobs[old_id]["status"] in ("done", "error"):
                        del self._jobs[old_id]
                    if len(self._jobs) < self._max_jobs:
                        break
            self._jobs[job_id] = {
                "status": "running",
                "total": total,
                "done": 0,
                "success": 0,
                "duplicate": 0,
                "failed": 0,
                "asset_ids": [],
                "errors": [],
                "started_at": time.time(),
            }
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kwargs)

    def finish(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["finished_at"] = time.time()

    def submit(self, fn, total: int) -> str:
        job_id = self.create(total)

        def runner():
            try:
                fn(job_id)
                self.update(job_id, status="done")
            except Exception as exc:  # noqa: BLE001 — 兜底，防止线程静默死亡  # last-resort guard so the thread never dies silently
                self.update(job_id, status="error", errors=[f"任务异常: {exc}"])
            finally:
                self.finish(job_id)

        threading.Thread(target=runner, daemon=True, name=f"immich-upload-{job_id}").start()
        return job_id


job_manager = UploadJobManager()


# ============================================================
#  上传工具（同步；P3 节点固定同步执行）
#  Upload helpers (synchronous; P3 nodes always run synchronously)
# ============================================================

def _should_retry(exc: ImmichError) -> bool:
    """网络错误、5xx、429 限流、408 超时才重试；4xx（请求本身有问题）不重试。
    *English translation:* retry only on network errors, 5xx, 429 rate limiting and 408 timeouts; 4xx (the request itself is wrong) is not retried.
    """
    if exc.status_code is None:
        return True
    return exc.status_code >= 500 or exc.status_code in (408, 429)


def _upload_with_retry(client: ImmichClient, upload_fn) -> dict:
    """带重试的上传调用（总尝试 MAX_RETRIES 次 = 1 初始 + 2 重试）。返回 {id, status}。
    *English translation:* upload call with retries (MAX_RETRIES attempts in total = 1 initial + 2 retries). Returns {id, status}.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            with _UPLOAD_SEMAPHORE:
                return upload_fn()
        except ImmichError as exc:
            last_exc = exc
            if not _should_retry(exc) or attempt == MAX_RETRIES - 1:
                break
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
    raise last_exc if last_exc else ImmichError("上传失败")


def _build_client(base_url: str | None = None, api_key: str | None = None) -> ImmichClient:
    """从 config 构建客户端（P3 节点不再接受输入覆盖，仅面板配置）。
    *English translation:* build the client from config (P3 nodes no longer accept input overrides; panel config only).
    """
    base_url = base_url or _CONFIG.get("base_url", "")
    api_key = api_key or _CONFIG.get("api_key", "")
    if not base_url:
        raise RuntimeError(
            "Immich 未配置：请在 Immich 面板配置页填写 base_url "
            "（如 http://192.168.1.50:2283/api）与 api_key"
        )
    return ImmichClient(
        base_url=base_url,
        api_key=api_key,
        proxy_mode=_CONFIG.get("proxy_mode", "off"),
        proxy_url=_CONFIG.get("proxy_url", ""),
    )


def _upload_single(client: ImmichClient, data: bytes, filename: str, mime: str) -> dict:
    """单文件上传（带重试）。返回 {id, status}。
    *English translation:* single-file upload (with retries). Returns {id, status}.
    """
    return _upload_with_retry(
        client,
        lambda: client.upload_bytes(data, filename, mime),
    )


def _upload_file(client: ImmichClient, file_path: str, filename: str | None = None) -> dict:
    """按路径流式上传（视频等大文件，带重试）。返回 {id, status}。

    走 client.upload_file：分块 sha1 + multipart 流式，不整读文件进内存
    （大视频防 OOM）。每次重试重新调用 upload_file（内部重新 open 文件），
    不存在"流式读后指针到末尾"问题。
    *English translation:* stream the upload by path (large files such as video, with retries). Returns {id, status}.
    * Uses client.upload_file: chunked sha1 + multipart streaming, never reading the whole file into memory
    * (avoids OOM on large videos). Each retry calls upload_file again (it reopens the file internally),
    * so there is no "streaming read left the pointer at EOF" problem.
    """
    return _upload_with_retry(
        client,
        lambda: client.upload_file(file_path, filename=filename),
    )


def _add_to_album(client: ImmichClient, album_name: str, asset_ids: list[str]) -> None:
    """按名查找/创建相册并批量加入（带锁防并发建同名）。
    *English translation:* look an album up by name / create it and add the assets in one batch (locked against concurrent duplicates).
    """
    if not album_name or not asset_ids:
        return
    with _ALBUM_LOCK:
        album = client.ensure_album(album_name)
        if album and album.get("id"):
            client.add_to_album(album["id"], asset_ids)


# ============================================================
#  图片编码（固定 PNG，写入 workflow metadata）
#  Image encoding (always PNG, workflow metadata written in)
# ============================================================

def _tensor_to_png(tensor, metadata=None) -> bytes:
    """IMAGE tensor [H,W,C]（float32 0-1）→ PNG bytes（可带 metadata）。
    *English translation:* IMAGE tensor [H,W,C] (float32 0-1) → PNG bytes (metadata optional).
    """
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    arr = np.clip(tensor, 0.0, 1.0)
    img = Image.fromarray((arr * 255.0).astype(np.uint8))
    buf = io.BytesIO()
    pnginfo = None
    if metadata:
        pnginfo = PngInfo()
        for key, value in metadata.items():
            if value is not None:
                pnginfo.add_text(key, value)
    img.save(buf, format="PNG", pnginfo=pnginfo)
    return buf.getvalue()


def _build_metadata(prompt, extra_pnginfo) -> dict | None:
    """组装 PNG text chunks（prompt + extra_pnginfo 中的 workflow 等）。
    *English translation:* assemble the PNG text chunks (prompt + the workflow etc. from extra_pnginfo).
    """
    meta = {}
    if prompt is not None:
        meta["prompt"] = json.dumps(prompt, ensure_ascii=False)
    if extra_pnginfo is not None:
        for key, value in extra_pnginfo.items():
            meta[key] = json.dumps(value, ensure_ascii=False)
    return meta or None


def _build_video_metadata(prompt, extra_pnginfo) -> dict | None:
    """组装传给 video.save_to() 的容器 metadata。

    与图片节点的 _build_metadata 不同：value 保留**原始 dict/str**（不提前 json.dumps），
    由官方 save_to 内部 `output.metadata[key] = json.dumps(value)` 统一序列化
    （VideoFromFile 对 str 直写、dict 才 dumps；VideoFromComponents 无条件 dumps）。
    顺序照抄官方 SaveVideo：先 extra_pnginfo 再 prompt（prompt 覆盖同名项）。
    *English translation:* build the container metadata passed to video.save_to().
    * Unlike _build_metadata in the image node, values keep the **raw dict/str** (no early json.dumps):
    * the official save_to serialises them via `output.metadata[key] = json.dumps(value)`
    * (VideoFromFile writes str directly and only dumps dicts; VideoFromComponents always dumps).
    * The order follows the official SaveVideo: extra_pnginfo first, then prompt (prompt overrides same-named keys).
    """
    meta = {}
    if extra_pnginfo is not None:
        meta.update(extra_pnginfo)
    if prompt is not None:
        meta["prompt"] = prompt
    return meta or None


def _save_temp_png(png_bytes: bytes, filename_prefix: str, width: int, height: int, asset_id: str | None = None, idx: int = 0) -> dict | None:
    """本地 temp 保存 PNG（ComfyUI 环境）→ ui entry。

    有 folder_paths 时保存真实文件（节点预览）；无 folder_paths（纯测试环境）时
    返回带 asset_id 的 entry（无本地文件，仅供集成测试回读验证）。
    *English translation:* save the PNG to the local temp dir (ComfyUI environment) → ui entry.
    * With folder_paths the real file is written (node preview); without it (pure test environment)
    * an entry carrying asset_id is returned (no local file, only so integration tests can read it back).
    """
    try:
        import folder_paths  # noqa: PLC0415 — ComfyUI 运行时模块，延迟导入  # ComfyUI runtime module, imported lazily

        full_output_folder, filename, counter, subfolder, _prefix = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_temp_directory(), width, height
        )
        fname = f"{filename}_{counter:05}_.png"
        path = os.path.join(full_output_folder, fname)
        with open(path, "wb") as fh:
            fh.write(png_bytes)
        entry: dict = {"filename": fname, "subfolder": subfolder, "type": "temp"}
    except Exception:  # noqa: BLE001 — 无 folder_paths 时跳过本地保存，仅返回 entry  # no folder_paths: skip the local save and return the entry only
        entry = {"filename": _unique_filename(filename_prefix, idx, "png"), "subfolder": "", "type": "temp"}
    if asset_id:
        entry["asset_id"] = asset_id
    return entry


def _unique_filename(filename_prefix: str, idx: int, ext: str) -> str:
    """Immich 上传文件名：{prefix}_{毫秒时间戳}_{idx}.{ext}（保证唯一性）。
    *English translation:* Immich upload filename: {prefix}_{millisecond timestamp}_{idx}.{ext} (guaranteed unique).
    """
    return f"{filename_prefix}_{int(time.time() * 1000)}_{idx}.{ext}"


# ============================================================
#  🖼️ ImmichSaveImage（需求 14/15/16）
#  🖼️ ImmichSaveImage (requirements 14/15/16)
# ============================================================

class ImmichSaveImage:
    """上传生成图片到 Immich（简化版，模仿官方 SaveImage）。

    - images + filename_prefix 两个可见输入；base_url/api_key/相册走面板配置
    - 固定 PNG：写入 prompt/workflow metadata，Immich 下载后拖回 ComfyUI 可恢复工作流
    - 本地 temp 保存一份（节点预览），同时上传 Immich；上传失败抛错中断（部分成功不浪费）
    *English translation:* upload generated images to Immich (simplified, modelled on the official SaveImage).
    * Two visible inputs: images + filename_prefix; base_url/api_key/album come from the panel config
    * Always PNG: prompt/workflow metadata is written in, so a file downloaded from Immich and dragged back restores the workflow
    * One copy is saved to the local temp dir (node preview) and also uploaded to Immich; an upload failure raises (no silent partial success)
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    # 输出：把收到的批次原样透传，方便继续往下游连
    # （本节点同时是 OUTPUT_NODE，所以既能在预览里看结果，也能接着接线）
    # Output: pass the received batch through unchanged so it can feed downstream nodes
    # (this node is also an OUTPUT_NODE, so the result shows in the preview and can stay wired up)
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = "image"
    # 悬停文本语言跟随面板设置（自动/中文/英文）：web/immich_panel.js 的
    # applyNodeLang() 会按 localStorage("immich_lang") 覆盖 LiteGraph 类
    # DESCRIPTION 与已存在节点的 description。
    # ⚠️ 默认必须是英文：ComfyUI 注册表面向国际用户，中文只在面板语言选中文时替换。
    # Tooltip text follows the panel language (auto/Chinese/English): applyNodeLang() in
    # web/immich_panel.js overrides the LiteGraph DESCRIPTION and the description of existing nodes
    # according to localStorage("immich_lang").
    # ⚠️ The default must stay English: the ComfyUI registry faces an international audience;
    # Chinese is only swapped in when the panel language is Chinese.
    DESCRIPTION = (
        "[ComfyUI-ImmichManager] Preview the image and upload it to Immich. "
        "Saves as PNG by default, with workflow metadata embedded. "
        "More settings in the config panel."
    )
    DESCRIPTION_ZH = (
        "[ComfyUI-ImmichManager] 将图片预览并上传到 Immich。"
        "默认保存为PNG，内含工作流metadata。更多设置在配置面板。"
    )

    def save_images(self, images, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None):
        client = _build_client()
        album_name = _CONFIG.get("default_album", "") or ""
        metadata = _build_metadata(prompt, extra_pnginfo)

        # 兼容 torch tensor 与 numpy 数组  # Accept both torch tensors and numpy arrays
        if hasattr(images, "detach"):
            images = images.detach().cpu().numpy()

        uploaded_ids = []
        errors = []
        ui_entries = []
        for idx in range(images.shape[0]):
            tensor = images[idx]
            png_bytes = _tensor_to_png(tensor, metadata)
            height, width = tensor.shape[:2]
            filename = _unique_filename(filename_prefix, idx, "png")
            entry = None
            try:
                result = _upload_single(client, png_bytes, filename, "image/png")
                uploaded_ids.append(result.get("id"))
                # 本地 temp 保存（节点预览；entry 始终带 asset_id 供测试回读）
                # Save to the local temp dir (node preview; the entry always carries asset_id for tests to read back)
                entry = _save_temp_png(png_bytes, filename_prefix, width, height, result.get("id"), idx)
            except Exception as exc:  # noqa: BLE001 — 逐张兜底，最后统一抛错  # per-image guard; errors are raised together at the end
                errors.append(f"[{filename}] {exc}")
            if entry:
                ui_entries.append(entry)

        if uploaded_ids:
            try:
                _add_to_album(client, album_name, uploaded_ids)
            except Exception as exc:  # noqa: BLE001 — 相册失败不影响已上传  # an album failure must not undo the upload
                errors.append(f"加入相册失败: {exc}")

        if errors:
            raise RuntimeError("Immich 上传失败：" + "；".join(errors))

        return {"ui": {"images": ui_entries}, "result": (images,)}


# ============================================================
#  🎬 ImmichSaveVideo（需求 17，P4：官方 VIDEO 输入）
#  🎬 ImmichSaveVideo (requirement 17, P4: official VIDEO input)
# ============================================================

# 官方 VIDEO 类型来自 comfy_api（ComfyUI ≥0.30 自带）。
# 真实 ComfyUI 环境必然可用；离线测试环境（NAS 无 comfy_api）fallback 为字符串，
# 仅被 mock VideoInput 消费（真实 save_to 内部对字符串 format 有 .value 访问，不可混用）。
# The official VIDEO type comes from comfy_api (bundled with ComfyUI ≥0.30).
# It is always available in a real ComfyUI environment; the offline test environment (no comfy_api on the NAS) falls back to strings,
# consumed only by the mock VideoInput (the real save_to accesses .value on the format string, so the two cannot be mixed).
try:
    from comfy_api.latest import Types as _ComfyTypes

    _VIDEO_CONTAINER_MP4 = _ComfyTypes.VideoContainer.MP4
    _VIDEO_CODEC_H264 = _ComfyTypes.VideoCodec.H264
except ImportError:  # pragma: no cover — 离线测试环境  # offline test environment
    _VIDEO_CONTAINER_MP4 = "mp4"
    _VIDEO_CODEC_H264 = "h264"


class ImmichSaveVideo:
    """上传视频到 Immich（P4：输入官方 VIDEO 类型，模仿官方 SaveVideo）。

    - video（VIDEO，来自 CreateVideo / LoadVideo 等）+ filename_prefix 两个可见输入
    - 用官方 VideoInput.save_to()（PyAV）落盘 MP4 h264：
      * VideoFromComponents（CreateVideo 打包）：PyAV h264 编码（保持创建位深）
      * VideoFromFile（LoadVideo 引用）：源 mp4+h264 → 纯 copy 不重编码；不匹配才转码
      crf/bit_depth 均不传（None）——官方 save_to 默认保持源/创建位深、编码器默认质量
    - prompt/workflow 由官方 save_to 写容器 metadata（use_metadata_tags），
      Immich 下载视频拖回可恢复工作流（与官方 SaveVideo 同机制）
    - 本地 temp 保存 .mp4（节点视频预览），同时上传 Immich；连接/相册走面板配置
    - 预览：与官方 SaveVideo 同机制——返回 {"ui": {"images": [entry], "animated": [True]}}，
      前端 isVideoOutput(animated && .mp4) → useNodeVideo 渲染 <video>（0.30 前端只认
      images+animated，不认 ui.videos）
    *English translation:* upload videos to Immich (P4: takes the official VIDEO type, modelled on the official SaveVideo).
    * Two visible inputs: video (VIDEO, from CreateVideo / LoadVideo etc.) + filename_prefix
    * Uses the official VideoInput.save_to() (PyAV) to write an MP4 h264:
    * VideoFromComponents (packed by CreateVideo): PyAV h264 encoding (keeps the creation bit depth)
    * VideoFromFile (LoadVideo reference): source mp4+h264 → plain copy, no re-encode; transcode only when it does not match
    * crf/bit_depth are not passed (None) — the official save_to keeps the source/creation bit depth and the encoder default quality
    * prompt/workflow are written into the container metadata by the official save_to (use_metadata_tags),
    * so a video downloaded from Immich and dragged back restores the workflow (same mechanism as the official SaveVideo)
    * A .mp4 is saved to the local temp dir (node video preview) and uploaded to Immich; connection/album come from the panel config
    * Preview: same mechanism as the official SaveVideo — returns {"ui": {"images": [entry], "animated": [True]}},
    * the frontend's isVideoOutput(animated && .mp4) → useNodeVideo renders a <video> (the 0.30 frontend only knows
    * images+animated, not ui.videos)
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    # 输出：把收到的 VIDEO 原样透传，方便继续往下游连
    # （本节点同时是 OUTPUT_NODE，所以既能在预览里看结果，也能接着接线）
    # Output: pass the received VIDEO through unchanged so it can feed downstream nodes
    # (this node is also an OUTPUT_NODE, so the result shows in the preview and can stay wired up)
    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    FUNCTION = "save_video"
    OUTPUT_NODE = True
    CATEGORY = "video"
    DESCRIPTION = (
        "[ComfyUI-ImmichManager] Preview the video and upload it to Immich. "
        "Saves as MP4 by default, with workflow metadata embedded. "
        "More settings in the config panel."
    )
    DESCRIPTION_ZH = (
        "[ComfyUI-ImmichManager] 将视频预览并上传到 Immich。"
        "默认保存为MP4，内含工作流metadata。更多设置在配置面板。"
    )

    def save_video(self, video, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None):
        if video is None:
            raise RuntimeError("ImmichSaveVideo: video 为空")

        client = _build_client()
        album_name = _CONFIG.get("default_album", "") or ""

        # 目标路径：ComfyUI 环境走 temp 目录（节点视频预览）；测试环境 fallback 临时文件。
        # import folder_paths 单独 try：无 folder_paths（纯测试环境）才 fallback；
        # get_dimensions()/get_save_image_path() 异常应传播（真实环境 video 损坏不该静默丢 temp 语义）
        # Target path: the ComfyUI environment uses the temp dir (node video preview); the test environment falls back to a temp file.
        # import folder_paths is guarded on its own: only a missing folder_paths (pure test environment) falls back;
        # get_dimensions()/get_save_image_path() errors must propagate (a corrupt video must not silently lose the temp-file semantics).
        file_path = None
        fname = None
        subfolder = ""
        try:
            import folder_paths  # noqa: PLC0415 — ComfyUI 运行时模块，延迟导入  # ComfyUI runtime module, imported lazily
        except ImportError:
            tmp_fd, file_path = tempfile.mkstemp(suffix=".mp4")
            os.close(tmp_fd)
            fname = os.path.basename(file_path)
            subfolder = ""
        else:
            width, height = video.get_dimensions()
            full_output_folder, filename, counter, subfolder, _prefix = folder_paths.get_save_image_path(
                filename_prefix, folder_paths.get_temp_directory(), width, height
            )
            fname = f"{filename}_{counter:05}_.mp4"
            file_path = os.path.join(full_output_folder, fname)

        # 官方 save_to 落盘（PyAV 编码/复制 + 容器 metadata）
        # Official save_to writes the file (PyAV encode/copy + container metadata)
        video_metadata = _build_video_metadata(prompt, extra_pnginfo)
        try:
            video.save_to(
                file_path,
                format=_VIDEO_CONTAINER_MP4,
                codec=_VIDEO_CODEC_H264,
                metadata=video_metadata,
            )
        except Exception as exc:  # noqa: BLE001 — 统一包装为可读错误  # wrap everything into a readable error
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            raise RuntimeError(f"ImmichSaveVideo 视频处理失败: {exc}") from exc

        # 上传 Immich（本地预览文件即上传源文件，不另复制）
        # 流式上传不整读内存（大视频防 OOM）；失败保留本地预览文件（有预览价值），异常直接抛给前端
        # Upload to Immich (the local preview file is the upload source, no extra copy)
        # Streaming upload never reads the whole file into memory (OOM safety for large videos); on failure the
        # local preview file is kept (it is still worth previewing) and the exception goes straight to the frontend
        filename = _unique_filename(filename_prefix, 0, "mp4")
        result = _upload_file(client, file_path, filename)

        ui_entry = {"filename": fname, "subfolder": subfolder, "type": "temp"}
        ui_entry["asset_id"] = result.get("id")
        if result.get("id"):
            try:
                _add_to_album(client, album_name, [result["id"]])
            except Exception as exc:  # noqa: BLE001 — 相册失败不影响已上传  # an album failure must not undo the upload
                raise RuntimeError(f"视频已上传但加入相册失败: {exc}") from exc

        # 预览：官方 PreviewVideo 同款返回（images + animated），0.30 前端据此渲染 <video>；
        # ui.videos 键前端不识别（旧写法导致节点无预览）
        # result：把 VIDEO 原样透传（与 RETURN_TYPES/RETURN_NAMES 对应）
        # Preview: same return as the official PreviewVideo (images + animated), so the 0.30 frontend renders a <video>;
        # the frontend does not recognise a ui.videos key (the old form left the node without a preview)
        # result: pass the VIDEO through unchanged (matches RETURN_TYPES/RETURN_NAMES)
        return {"ui": {"images": [ui_entry], "animated": [True]}, "result": (video,)}
