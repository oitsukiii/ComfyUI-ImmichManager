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
"""
import logging
import os
import sys

log = logging.getLogger("comfyui_immichmanager")

# 运行时依赖（包名 -> pip 安装名）。imageio 不再需要：P4 视频编码走官方 save_to(PyAV)。
RUNTIME_DEPS = {
    "requests": "requests",
    "PIL": "pillow",
    "aiohttp": "aiohttp",
}


def _check_deps() -> bool:
    """检测运行时依赖，打印明确状态与失败原因。

    返回 True = 依赖齐全（可加载节点/前端/路由）；
    返回 False = 有缺失（跳过注册，避免 ComfyUI 加载插件时报裸 traceback）。
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
        import aiohttp  # noqa: PLC0415

        log.info(
            "[ComfyUI-ImmichManager] 依赖检查通过：requests / Pillow / aiohttp %s（面板后端可用）",
            aiohttp.__version__,
        )
    except Exception:  # noqa: BLE001 — 仅版本打印，不影响
        log.info("[ComfyUI-ImmichManager] 依赖检查通过：requests / Pillow / aiohttp（面板后端可用）")

    # 视频节点依赖说明（P4 起走官方 save_to / PyAV，ComfyUI ≥0.30 自带，无需额外安装）
    log.info(
        "[ComfyUI-ImmichManager] 视频节点 ImmichSaveVideo 使用官方 VIDEO 类型 + PyAV（ComfyUI ≥0.30 自带）"
    )
    return True


if _check_deps():
    from .upload_node import ImmichSaveImage, ImmichSaveVideo

    # 节点映射（旧 ImmichUpload 已按用户决策移除，不保留 legacy）
    NODE_CLASS_MAPPINGS = {
        "ImmichSaveImage": ImmichSaveImage,
        "ImmichSaveVideo": ImmichSaveVideo,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        "ImmichSaveImage": "🖼️ Immich Save Image",
        "ImmichSaveVideo": "🎬 Immich Save Video",
    }

    # 前端扩展目录
    WEB_DIRECTORY = "./web"

    # 挂载后端路由（ComfyUI 启动时自动执行）
    try:
        from server import PromptServer

        from .server_routes import setup_routes

        setup_routes(PromptServer.instance)
    except Exception as exc:  # noqa: BLE001 — 路由挂载失败不应阻断 ComfyUI 启动
        log.error("[ComfyUI-ImmichManager] 路由挂载失败（节点/面板后端不可用）: %s", exc)
else:
    # 依赖缺失：不注册节点/前端（ComfyUI 对 None 自动跳过）
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
    WEB_DIRECTORY = None

__version__ = "1.0.0"
