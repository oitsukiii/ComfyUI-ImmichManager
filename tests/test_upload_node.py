#!/usr/bin/env python3
"""P4 自测：图片节点（PNG + workflow metadata）、视频节点（官方 VIDEO 输入，save_to）+ 真实 Immich 集成。

用法:
  python3 tests/test_upload_node.py                     # 仅离线部分
  IMMICH_TEST_KEY=xxx python3 tests/test_upload_node.py  # 含真实 Immich 集成

P4 视频节点变化：
- 输入从 images+fps 改为官方 VIDEO 类型（video + filename_prefix）
- 编码/写 metadata 交给官方 VideoInput.save_to()（PyAV）——离线测试用 mock VideoInput
  验证调用契约；有 comfy_api 的环境（Windows ComfyUI venv）走真实 VideoFromComponents 全链路
"""
import io
import json
import os
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from upload_node import (  # noqa: E402
    ImmichSaveImage,
    ImmichSaveVideo,
    _build_metadata,
    _build_video_metadata,
    _should_retry,
    _tensor_to_png,
    job_manager,
)
from config import ConfigManager  # noqa: E402
from immich_client import ImmichClient, ImmichError  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


# ---------- 0. 节点 DESCRIPTION（老李定稿 2026-08-16，中英双语） ----------
check("图片节点 DESCRIPTION 含项目名", "ComfyUI-ImmichManager" in ImmichSaveImage.DESCRIPTION)
check("图片节点 DESCRIPTION 中文", "将图片预览并上传" in ImmichSaveImage.DESCRIPTION
      and "PNG" in ImmichSaveImage.DESCRIPTION)
check("图片节点 DESCRIPTION_EN 英文", "Preview the image and upload it to Immich" in ImmichSaveImage.DESCRIPTION_EN
      and "PNG" in ImmichSaveImage.DESCRIPTION_EN)
check("视频节点 DESCRIPTION 含项目名", "ComfyUI-ImmichManager" in ImmichSaveVideo.DESCRIPTION)
check("视频节点 DESCRIPTION 中文", "将视频预览并上传" in ImmichSaveVideo.DESCRIPTION
      and "MP4" in ImmichSaveVideo.DESCRIPTION)
check("视频节点 DESCRIPTION_EN 英文", "Preview the video and upload it to Immich" in ImmichSaveVideo.DESCRIPTION_EN
      and "MP4" in ImmichSaveVideo.DESCRIPTION_EN)


# ---------- 1. 离线：PNG encode + metadata ----------
tensor = np.random.rand(64, 64, 3).astype(np.float32)
meta = _build_metadata({"test": 1}, {"workflow": {"nodes": [1]}})
data = _tensor_to_png(tensor, meta)
check("png 魔数", data[:4] == b"\x89PNG", f"-> {len(data)}B")
check("metadata 非空", meta is not None and "prompt" in meta and "workflow" in meta)

# metadata 无输入时返回 None
check("metadata 空输入 -> None", _build_metadata(None, None) is None)

# PIL 读回 pnginfo 验证
from PIL import Image  # noqa: E402

buf = io.BytesIO(data)
img = Image.open(buf)
check("pnginfo prompt 写入", img.info.get("prompt") == '{"test": 1}', f"-> {img.info.get('prompt')}")
check("pnginfo workflow 写入", img.info.get("workflow") == '{"nodes": [1]}')

# ---------- 2. 离线：视频 metadata 构建（P4：value 保留原始 dict，供 save_to 内部 dumps） ----------
vmeta = _build_video_metadata({"test": 1}, {"workflow": {"nodes": [1]}})
check("视频 metadata 有 prompt/workflow", vmeta is not None and "prompt" in vmeta and "workflow" in vmeta)
check("视频 metadata value 是原始 dict（未预序列化）",
      isinstance(vmeta["prompt"], dict) and isinstance(vmeta["workflow"], dict),
      f"-> {type(vmeta['prompt']).__name__}")
check("视频 metadata 空输入 -> None", _build_video_metadata(None, None) is None)
# 顺序：extra_pnginfo 先、prompt 后覆盖（照抄官方 SaveVideo）
vmeta2 = _build_video_metadata({"role": "prompt"}, {"prompt": {"role": "pnginfo"}, "workflow": 1})
check("视频 metadata prompt 覆盖 extra_pnginfo 同名项", vmeta2["prompt"] == {"role": "prompt"}, f"-> {vmeta2['prompt']}")

# ---------- 2.5 离线：Mock VideoInput（P4 契约验证） ----------

class MockVideoInput:
    """鸭子类型 VideoInput：记录 save_to 调用参数 + 写一个假 mp4 文件。"""

    def __init__(self, width=48, height=32, error=None):
        self.width, self.height = width, height
        self.error = error
        self.save_calls = []

    def get_dimensions(self):
        return (self.width, self.height)

    def save_to(self, path, format=None, codec=None, metadata=None, bit_depth=None, crf=None):
        if self.error:
            raise self.error
        self.save_calls.append(dict(
            path=path, format=format, codec=codec, metadata=metadata,
            bit_depth=bit_depth, crf=crf,
        ))
        with open(path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128)


class FakeClient:
    """上传链路替身：记录 upload_bytes / 相册调用，可注入上传错误。"""

    def __init__(self, upload_error=None):
        self.uploads = []
        self.album_ids = []
        self.upload_error = upload_error

    def upload_bytes(self, data, filename, mime):
        if self.upload_error:
            raise self.upload_error
        self.uploads.append((filename, mime, len(data)))
        return {"id": "fake-id", "status": "created"}

    def upload_file(self, file_path, filename=None):
        if self.upload_error:
            raise self.upload_error
        name = filename or os.path.basename(file_path)
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        self.uploads.append((name, "video/mp4", size))
        return {"id": "fake-id", "status": "created"}

    def ensure_album(self, name):
        return {"id": "album-id"}

    def add_to_album(self, album_id, asset_ids):
        self.album_ids.append((album_id, list(asset_ids)))


node_img = ImmichSaveImage()
node_vid = ImmichSaveVideo()
ipt_img = node_img.INPUT_TYPES()
check("图片节点 INPUT_TYPES 契约", "images" in ipt_img["required"] and "filename_prefix" in ipt_img["required"])
ipt = node_vid.INPUT_TYPES()["required"]
check("视频节点 INPUT_TYPES 契约（video+filename_prefix）",
      set(ipt.keys()) == {"video", "filename_prefix"}, f"-> {set(ipt.keys())}")
check("视频节点无 images/fps 旧输入", "images" not in ipt and "fps" not in ipt)

# 未配置连接时上传应报清晰错误
import config as config_mod  # noqa: E402

orig_get = config_mod.get_config


def fake_empty_config():
    return {"base_url": "", "api_key": "", "default_album": ""}


config_mod.get_config = fake_empty_config
import upload_node  # noqa: E402

upload_node._CONFIG = fake_empty_config()
try:
    node_img.save_images(tensor[None], filename_prefix="t")
    check("未配置连接抛错", False, "-> 竟然没抛错")
except RuntimeError as exc:
    check("未配置连接抛错", True, f"-> {exc}")
config_mod.get_config = orig_get

# ---------- 2.6 离线：save_video 调用契约（fake client + mock video） ----------
orig_build_client = upload_node._build_client
orig_config = upload_node._CONFIG
upload_node._CONFIG = {"base_url": "http://test/api", "api_key": "k", "default_album": "album"}

fc = FakeClient()
upload_node._build_client = lambda base_url=None, api_key=None: fc
mv = MockVideoInput()
out = node_vid.save_video(mv, filename_prefix="ci_p4_vid", prompt={"test": 3}, extra_pnginfo={"workflow": {"nodes": [2]}})
check("save_to 被调用一次", len(mv.save_calls) == 1, f"-> {len(mv.save_calls)}")
call = mv.save_calls[0]
check("save_to format=mp4", str(call["format"]) == "mp4" or call["format"] == "mp4", f"-> {call['format']!r}")
check("save_to codec=h264", str(call["codec"]) == "h264" or call["codec"] == "h264", f"-> {call['codec']!r}")
check("save_to metadata 传 dict", isinstance(call["metadata"], dict)
      and call["metadata"].get("prompt") == {"test": 3}
      and call["metadata"].get("workflow") == {"nodes": [2]},
      f"-> {call['metadata']}")
check("save_to bit_depth/crf 不传（None）", call["bit_depth"] is None and call["crf"] is None)
check("save_to 目标路径存在（file_path 传给 save_to）", os.path.exists(call["path"]), f"-> {call['path']}")
check("上传调用（mime=video/mp4）", len(fc.uploads) == 1 and fc.uploads[0][1] == "video/mp4", f"-> {fc.uploads}")
check("相册加入", len(fc.album_ids) == 1 and fc.album_ids[0][0] == "album-id", f"-> {fc.album_ids}")
check("ui.images entry 带 asset_id（预览=官方 PreviewVideo 同款返回）",
      out.get("ui", {}).get("images", [{}])[0].get("asset_id") == "fake-id",
      f"-> {out.get('ui')}")
os.remove(call["path"])

# save_to 抛错 -> 包装为可读错误；且覆盖"半写文件"场景：先写部分文件再抛错，
# 验证 except 里的 os.remove 清理（不留残缺文件污染 temp 目录）
class FailAfterWriteVideo(MockVideoInput):
    def save_to(self, path, format=None, codec=None, metadata=None, bit_depth=None, crf=None):
        # 模拟编码中途失败：先落一个半成品文件，再抛错
        with open(path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)
        self.save_calls.append(dict(
            path=path, format=format, codec=codec, metadata=metadata,
            bit_depth=bit_depth, crf=crf,
        ))
        raise RuntimeError("磁盘满")


mv_err = FailAfterWriteVideo()
try:
    node_vid.save_video(mv_err, filename_prefix="x")
    check("save_to 抛错包装", False, "-> 竟然没抛错")
except RuntimeError as exc:
    check("save_to 抛错包装", "ImmichSaveVideo 视频处理失败" in str(exc), f"-> {exc}")
check("save_to 失败清理半写文件",
      bool(mv_err.save_calls) and not os.path.exists(mv_err.save_calls[0]["path"]),
      f"-> 残留: {mv_err.save_calls[0]['path'] if mv_err.save_calls else None}")

# video=None -> 清晰报错
try:
    node_vid.save_video(None, filename_prefix="x")
    check("video=None 抛错", False, "-> 竟然没抛错")
except RuntimeError as exc:
    check("video=None 抛错", "video 为空" in str(exc), f"-> {exc}")

# 上传失败 -> 抛错
fc2 = FakeClient(upload_error=ImmichError("连接失败", None))
upload_node._build_client = lambda base_url=None, api_key=None: fc2
mv2 = MockVideoInput()
try:
    node_vid.save_video(mv2, filename_prefix="y")
    check("上传失败抛错", False, "-> 竟然没抛错")
except Exception:
    check("上传失败抛错", True, "-> ImmichError 冒泡")
if os.path.exists(mv2.save_calls[0]["path"]):
    os.remove(mv2.save_calls[0]["path"])

upload_node._build_client = orig_build_client
upload_node._CONFIG = orig_config

# ---------- 3. 离线：job 管理（兼容保留） ----------
jid = job_manager.create(3)
check("job create", jid and job_manager.get(jid)["status"] == "running")
job_manager.update(jid, done=1, success=1)
check("job update", job_manager.get(jid)["success"] == 1)
job_manager.finish(jid)
job_fin = job_manager.get(jid)
check("job finish 只写 finished_at 不动 status", job_fin["status"] == "running" and "finished_at" in job_fin)
check("job 不存在", job_manager.get("nope") is None)

# ---------- 4. 离线：重试判定 ----------
check("_should_retry 5xx", _should_retry(ImmichError("x", 502)) is True)
check("_should_retry 4xx", _should_retry(ImmichError("x", 400)) is False)
check("_should_retry 网络错", _should_retry(ImmichError("连接失败")) is True)

# ---------- 5. 集成：连真实 Immich（有 key 时） ----------
KEY = os.environ.get("IMMICH_TEST_KEY", "")
if KEY:
    BASE = os.environ.get("IMMICH_TEST_BASE", "http://127.0.0.1:2283/api")
    import upload_node as un  # noqa: E402

    _ORIG_CONFIG = un._CONFIG
    un._CONFIG = {"base_url": BASE, "api_key": KEY, "default_album": ""}
    client = ImmichClient(BASE, KEY)

    # 5.1 图片节点：上传 → 下载 → 验证 PNG metadata（拖回恢复工作流的关键路径）
    t = np.random.rand(1, 48, 48, 3).astype(np.float32)
    fake_workflow = {"nodes": [{"id": 1, "type": "ImmichSaveImage", "inputs": {}}]}
    out = node_img.save_images(t, filename_prefix="ci_p4_img", prompt={"test": 1}, extra_pnginfo={"workflow": fake_workflow})
    check("图片节点返回 ui.images", "images" in out.get("ui", {}), f"-> {out.get('ui')}")
    check("图片节点无 result 输出", "result" not in out, f"-> {list(out.keys())}")
    img_entries = out.get("ui", {}).get("images", [])
    asset_id = img_entries[0].get("asset_id") if img_entries else None
    check("图片上传返回 asset_id", bool(asset_id), f"-> {asset_id}")
    if asset_id:
        dl_bytes = client.get_original(asset_id).content
        check("下载图片非空", dl_bytes[:4] == b"\x89PNG", f"-> {len(dl_bytes)}B")
        dl_img = Image.open(io.BytesIO(dl_bytes))
        check("下载图 prompt metadata 恢复", "prompt" in dl_img.info and json.loads(dl_img.info["prompt"]) == {"test": 1}, f"-> {dl_img.info.get('prompt')}")
        check("下载图 workflow metadata 恢复", "workflow" in dl_img.info and json.loads(dl_img.info["workflow"]) == fake_workflow, f"-> {dl_img.info.get('workflow')}")
        client.delete_assets([asset_id])

    # 5.2 视频节点：真实 ComfyUI 环境（有 comfy_api）走 VideoFromComponents 全链路；
    #      离线环境（无 ComfyUI）fallback MockVideoInput 验证上传链路。
    try:
        from comfy_api.latest import InputImpl, Types  # noqa: E402
        from fractions import Fraction

        HAS_COMFY_API = True
    except ImportError:
        HAS_COMFY_API = False

    if HAS_COMFY_API:
        # 真实 CreateVideo 等价物：VideoFromComponents 打包（不编码），交给节点 save_to
        vid_frames = (np.random.rand(8, 32, 32, 3) * 255).astype(np.float32) / 255.0
        real_video = InputImpl.VideoFromComponents(
            Types.VideoComponents(images=vid_frames, audio=None, frame_rate=Fraction(8)),
        )
        fake_video_workflow = {"nodes": [{"id": 2, "type": "CreateVideo", "inputs": {}}]}
        out_v = node_vid.save_video(real_video, filename_prefix="ci_p4_vid", prompt={"test": 2},
                                    extra_pnginfo={"workflow": fake_video_workflow})
        check("真实 VIDEO 节点返回 ui.images+animated", "images" in out_v.get("ui", {})
              and out_v.get("ui", {}).get("animated") == [True], f"-> {out_v.get('ui')}")
        vid_entries = out_v.get("ui", {}).get("images", [])
        v_id = vid_entries[0].get("asset_id") if vid_entries else None
        check("真实 VIDEO 上传返回 asset_id", bool(v_id), f"-> {v_id}")
        if v_id:
            dl_v = client.get_original(v_id).content
            check("下载视频为 MP4", dl_v[4:8] == b"ftyp", f"-> {len(dl_v)}B")
            # PyAV 读回容器 metadata（拖回恢复工作流的关键路径）
            tmpv = tempfile.mkstemp(suffix=".mp4")
            os.close(tmpv[0])
            with open(tmpv[1], "wb") as fh:
                fh.write(dl_v)
            import av  # noqa: E402 — ComfyUI 自带 PyAV

            with av.open(tmpv[1]) as cont:
                md = cont.metadata
                check("下载视频 prompt metadata 恢复", md.get("prompt") and json.loads(md["prompt"]) == {"test": 2}, f"-> {md.get('prompt')}")
                check("下载视频 workflow metadata 恢复", md.get("workflow") and json.loads(md["workflow"]) == fake_video_workflow, f"-> {md.get('workflow')}")
                vstream = cont.streams.video[0] if cont.streams.video else None
                check("视频流存在 h264", vstream is not None and "h264" in vstream.codec.name, f"-> {vstream.codec.name if vstream else None}")
            os.remove(tmpv[1])
            client.delete_assets([v_id])
    else:
        # 离线：mock video 走完整上传链路（save_to 参数由 2.6 已验）
        mv_i = MockVideoInput()
        out_v = node_vid.save_video(mv_i, filename_prefix="ci_p4_vid", prompt={"test": 2},
                                    extra_pnginfo={"workflow": {"nodes": [2]}})
        check("离线 mock 视频节点返回 ui.images+animated", "images" in out_v.get("ui", {})
              and out_v.get("ui", {}).get("animated") == [True], f"-> {out_v.get('ui')}")
        vid_entries = out_v.get("ui", {}).get("images", [])
        v_id = vid_entries[0].get("asset_id") if vid_entries else None
        check("离线 mock 上传返回 asset_id", bool(v_id), f"-> {v_id}")
        if os.path.exists(mv_i.save_calls[0]["path"]):
            os.remove(mv_i.save_calls[0]["path"])
        if v_id:
            client.delete_assets([v_id])

    un._CONFIG = _ORIG_CONFIG  # 测试卫生：恢复原配置

fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
