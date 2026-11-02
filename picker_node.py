"""从 Immich 指定相册选图，并作为 IMAGE 输出给后续节点。

设计：节点只持有 asset_id（字符串 widget），选图由前端按钮写回；
执行时用服务端的 API Key 直连 Immich 取原图，解码成 IMAGE/MASK。
*English translation:* pick an image from a chosen Immich album and output it as IMAGE for downstream nodes.
* Design: the node only holds asset_id (a string widget), which the frontend button writes back;
* on execution the server-side API Key is used to fetch the original from Immich and decode it into IMAGE/MASK.
"""
from __future__ import annotations

import io
import logging

import numpy as np
import torch
from PIL import Image, ImageOps

from .immich_client import ImmichError
from .upload_node import _build_client, _save_temp_png

log = logging.getLogger(__name__)


class ImmichPickImage:
    """读取 Immich 资产 → IMAGE / MASK。
    *English translation:* read an Immich asset → IMAGE / MASK.
    """

    CATEGORY = "Immich"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
    RETURN_NAMES = ("image", "mask", "asset_id")

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802 — ComfyUI 约定  # ComfyUI convention
        return {
            "required": {
                "asset_id": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "点节点上的「选择图片」从相册里挑，不用手填",
                }),
            },
        }

    def load(self, asset_id: str):
        asset_id = (asset_id or "").strip()
        if not asset_id:
            raise RuntimeError("未选择图片：请点节点上的「选择图片」按钮")

        client = _build_client()
        try:
            resp = client.get_original(asset_id)
        except ImmichError as exc:
            raise RuntimeError(f"从 Immich 取原图失败：{exc}") from exc
        if getattr(resp, "status_code", 200) != 200:
            raise RuntimeError(f"从 Immich 取原图失败：HTTP {resp.status_code}")

        img = Image.open(io.BytesIO(resp.content))
        # 手机照片常见 EXIF 旋转，不处理会歪
        # Phone photos often carry an EXIF rotation; without handling it the image comes out sideways
        img = ImageOps.exif_transpose(img)

        alpha = None
        if img.mode in ("RGBA", "LA"):
            alpha = img.convert("RGBA").getchannel("A")
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 给 ComfyUI 画节点缩略图用（temp 目录 + ui.images，前端自动渲染）
        # For ComfyUI to draw the node thumbnail (temp dir + ui.images, rendered automatically by the frontend)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        ui_entry = _save_temp_png(buf.getvalue(), "immich_pick", img.width, img.height, asset_id)

        arr = np.asarray(img, dtype=np.float32) / 255.0
        image = torch.from_numpy(arr)[None, ...]

        if alpha is not None:
            mask = torch.from_numpy(np.asarray(alpha, dtype=np.float32) / 255.0)[None, ...]
        else:
            mask = torch.zeros((1, img.height, img.width), dtype=torch.float32)

        return {"ui": {"images": [ui_entry]}, "result": (image, mask, asset_id)}
