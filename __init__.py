"""ComfyUI-ImmichManager — 上传生成产物到 Immich + 面板预览/管理资产。

v5 范围（P4+：视频节点 VIDEO 输入 + 预览修复）：
- 后端：/api/immich_plus/* 路由（配置管理 + Immich 代理 + 任务查询）
- 节点：🖼️ ImmichSaveImage（图片简化版，PNG + workflow metadata 可拖回恢复）
       🎬 ImmichSaveVideo（视频节点，输入官方 VIDEO 类型，save_to(PyAV) 编码 MP4 h264
       或 LoadVideo 文件直拷 + 容器 metadata 可拖回恢复；节点视频预览与官方 PreviewVideo
       同机制——ui.images + animated，0.30 前端据此渲染 <video>）
- 前端：工具栏图标 + 资产时间轴面板（月桶折叠 + 缩略图懒加载 +
  详情/收藏/删除/描述）+ 配置页（连接测试、保存即时生效）

依赖：requests、Pillow、aiohttp、PyAV —— ComfyUI ≥0.30 自带，无需额外安装。
旧版 ComfyUI 若缺失，请 `python -m pip install requests pillow aiohttp`。
*English translation:*
* ComfyUI-ImmichManager — upload generated artefacts to Immich + panel preview/asset management.
* v5 scope (P4+: video node with a VIDEO input + preview fix):
* Backend: /api/immich_plus/* routes (config management + Immich proxy + job queries)
* Nodes: 🖼️ ImmichSaveImage (simplified image node, PNG + workflow metadata that can be dragged back to restore)
* 🎬 ImmichSaveVideo (video node, takes the official VIDEO type, save_to(PyAV) encodes MP4 h264
* or copies LoadVideo files directly, container metadata can be dragged back to restore; the node video preview uses the
* same mechanism as the official PreviewVideo — ui.images + animated, which the 0.30 frontend renders as <video>)
* Frontend: sidebar entry + asset panel (filters/grouping/pinning + lazy-loaded thumbnails +
* details/favourites/delete/description) + the Immich group in ComfyUI's Settings dialog
* Dependencies: requests, Pillow, aiohttp, PyAV — all bundled with ComfyUI ≥0.30, nothing extra to install.
* On older ComfyUI, run `python -m pip install requests pillow aiohttp`.
"""
import logging
import os
import sys

log = logging.getLogger("comfyui_immichmanager")

# 运行时依赖（包名 -> pip 安装名）。imageio 不再需要：P4 视频编码走官方 save_to(PyAV)。
# Runtime dependencies (import name -> pip package name). imageio is no longer needed: since P4 video encoding goes through the official save_to(PyAV).
RUNTIME_DEPS = {
    "requests": "requests",
    "PIL": "pillow",
    "aiohttp": "aiohttp",
}


def _check_deps() -> bool:
    """检测运行时依赖，打印明确状态与失败原因。

    返回 True = 依赖齐全（可加载节点/前端/路由）；
    返回 False = 有缺失（跳过注册，避免 ComfyUI 加载插件时报裸 traceback）。
    *English translation:* check the runtime dependencies and print a clear status plus the failure reason.
    * Returns True = everything present (nodes/frontend/routes can load);
    * returns False = something is missing (registration is skipped so ComfyUI does not emit a bare traceback when loading the plugin).
    """
    missing = []
    for mod, pkg in RUNTIME_DEPS.items():
        try:
            __import__(mod)
        except ImportError as exc:
            missing.append((mod, pkg, exc))

    if missing:
        for mod, pkg, exc in missing:
            log.error("[ComfyUI-ImmichManager] 依赖缺失 %s（包名 %s）: %s", mod, pkg, exc)
        names = " ".join(pkg for _, pkg, _ in missing)
        log.error(
            "[ComfyUI-ImmichManager] 节点与面板未加载。请安装缺失依赖后重启 ComfyUI："
            "python -m pip install %s",
            names,
        )
        log.info("[ComfyUI-ImmichManager] 检测结果：%d 个依赖缺失（%s）", len(missing), names)
        return False

    try:
        import aiohttp  # noqa: PLC0415 — 仅用于读取版本号  # only to read the version string

        log.info(
            "[ComfyUI-ImmichManager] 依赖检查通过：requests / Pillow / aiohttp %s（面板后端可用）",
            aiohttp.__version__,
        )
    except Exception:  # noqa: BLE001 — 仅版本打印，不影响  # only prints the version, no effect
        log.info("[ComfyUI-ImmichManager] 依赖检查通过：requests / Pillow / aiohttp（面板后端可用）")

    # 视频节点依赖说明（P4 起走官方 save_to / PyAV，ComfyUI ≥0.30 自带，无需额外安装）
    # Video-node dependency note (since P4 it goes through the official save_to / PyAV, bundled with ComfyUI ≥0.30, nothing extra to install)
    log.info(
        "[ComfyUI-ImmichManager] 视频节点 ImmichSaveVideo 使用官方 VIDEO 类型 + PyAV（ComfyUI ≥0.30 自带）"
    )
    return True


if _check_deps():
    from .picker_node import ImmichPickImage
    from .upload_node import ImmichSaveImage, ImmichSaveVideo

    # 节点映射（旧 ImmichUpload 已按用户决策移除，不保留 legacy）
    # Node mappings (the old ImmichUpload was removed by user decision; no legacy is kept)
    NODE_CLASS_MAPPINGS = {
        "ImmichPickImage": ImmichPickImage,
        "ImmichSaveImage": ImmichSaveImage,
        "ImmichSaveVideo": ImmichSaveVideo,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        "ImmichPickImage": "📥 Load Image from Immich",
        "ImmichSaveImage": "🖼️ Save Image into Immich",
        "ImmichSaveVideo": "🎬 Save Video into Immich",
    }

    # 前端扩展目录  # Frontend extension directory
    WEB_DIRECTORY = "./web"

    # 挂载后端路由（ComfyUI 启动时自动执行）  # Mount the backend routes (runs automatically when ComfyUI starts)
    try:
        from server import PromptServer

        from .server_routes import setup_routes

        setup_routes(PromptServer.instance)
    except Exception as exc:  # noqa: BLE001 — 路由挂载失败不应阻断 ComfyUI 启动  # a failed route mount must not stop ComfyUI from starting
        log.error("[ComfyUI-ImmichManager] 路由挂载失败（节点/面板后端不可用）: %s", exc)
else:
    # 依赖缺失：不注册节点/前端（ComfyUI 对 None 自动跳过）
    # Dependencies missing: register no nodes/frontend (ComfyUI skips None automatically)
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
    WEB_DIRECTORY = None

__version__ = "1.1.0"
