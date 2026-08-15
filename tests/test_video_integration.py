#!/usr/bin/env python3
"""P4 视频节点集成测试（在 Windows ComfyUI venv 上跑）。

验证 ImmichSaveVideo 接官方 VIDEO 类型后的全链路：
  A. VideoFromComponents（CreateVideo 等价物）→ 节点 → 上传 → 下载：
     h264 流 + prompt/workflow metadata（PyAV 读回）
  B. VideoFromComponents + audio → 下载：音频流存在
  C. VideoFromFile（LoadVideo 等价物，源 mp4 带旧 prompt）→ 节点（新 prompt）→ 下载：
     纯 copy 不重编码（codec/profile/尺寸/帧数一致）+ metadata 覆盖而非叠加
  D. 中文 prompt 端到端：官方 ensure_ascii=True（\\uXXXX）序列化，下载 json.loads 中文一致

注意：VideoFromComponents 的 images 必须是 torch tensor（官方 save_to 内部用
.clamp/.byte/.cpu()），不能是 numpy 数组。

用法（在有 venv 的 ComfyUI 安装里运行本文件即可，例如 Windows）:
  & <你的ComfyUI目录>/venv/Scripts/python.exe <插件目录>/tests/test_video_integration.py
"""
import json
import os
import sys
import tempfile
from fractions import Fraction

import numpy as np
import torch

sys.path.insert(0, r"C:\ComfyUI")  # comfy_api / av / folder_paths
PLUGIN_DIR = r"C:\ComfyUI\custom_nodes\ComfyUI-ImmichManager"
sys.path.insert(0, PLUGIN_DIR)

from comfy_api.latest import InputImpl, Types  # noqa: E402
import av  # noqa: E402

from upload_node import ImmichSaveVideo  # noqa: E402
from immich_client import ImmichClient  # noqa: E402
import upload_node as un  # noqa: E402

KEY = os.environ.get("IMMICH_TEST_KEY", "")
BASE = os.environ.get("IMMICH_TEST_BASE", "http://127.0.0.1:2283/api")
if not KEY:
    # 从插件 config.json 读用户面板保存的 api_key（P3 同款做法，key 不硬编码）
    cfg_path = os.path.join(PLUGIN_DIR, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        KEY = cfg.get("api_key", "")
        if cfg.get("base_url"):
            BASE = cfg["base_url"]
    except Exception as exc:  # noqa: BLE001
        print(f"config.json 读取失败: {exc}")
if not KEY:
    print("需要 IMMICH_TEST_KEY 环境变量（或插件 config.json 已配置 api_key）")
    sys.exit(2)

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


un._CONFIG = {"base_url": BASE, "api_key": KEY, "default_album": ""}
client = ImmichClient(BASE, KEY)
node = ImmichSaveVideo()
uploaded = []


def cleanup(asset_id, tmp_path=None):
    if tmp_path and os.path.exists(tmp_path):
        os.remove(tmp_path)
    if asset_id:
        uploaded.append(asset_id)


# ---------- A. VideoFromComponents（CreateVideo 等价物） ----------
frames = torch.from_numpy((np.random.rand(8, 32, 32, 3) * 255).astype(np.float32) / 255.0)
video_components = InputImpl.VideoFromComponents(
    Types.VideoComponents(images=frames, audio=None, frame_rate=Fraction(8)),
)
wf_a = {"nodes": [{"id": 1, "type": "CreateVideo", "inputs": {}}]}
out = node.save_video(video_components, filename_prefix="ci_p4_a", prompt={"test": "A"},
                      extra_pnginfo={"workflow": wf_a})
entries = out.get("ui", {}).get("images", [])
check("A: 返回 ui.images + animated[True]（前端视频预览）",
      "images" in out.get("ui", {}) and out.get("ui", {}).get("animated") == [True],
      f"-> {out.get('ui')}")
a_id = entries[0].get("asset_id") if entries else None
check("A: 上传返回 asset_id", bool(a_id), f"-> {a_id}")
if a_id:
    dl = client.get_original(a_id).content
    tmp = tempfile.mkstemp(suffix=".mp4")
    os.close(tmp[0])
    with open(tmp[1], "wb") as fh:
        fh.write(dl)
    with av.open(tmp[1]) as cont:
        md = cont.metadata
        check("A: prompt metadata 恢复", md.get("prompt") and json.loads(md["prompt"]) == {"test": "A"}, f"-> {md.get('prompt')}")
        check("A: workflow metadata 恢复", md.get("workflow") and json.loads(md["workflow"]) == wf_a, f"-> {md.get('workflow')}")
        vs = cont.streams.video[0] if cont.streams.video else None
        check("A: 视频流 h264", vs is not None and "h264" in vs.codec.name, f"-> {vs.codec.name if vs else None}")
        check("A: 尺寸 32x32", vs is not None and vs.width == 32 and vs.height == 32, f"-> {vs.width}x{vs.height}" if vs else None)
        # 帧数：容器 duration / fps 近似；直接数帧更稳
        n = 0
        cont.seek(0)
        for frame in cont.decode(vs):
            n += 1
        check("A: 帧数 >= 8", n >= 8, f"-> {n}")
    cleanup(a_id, tmp[1])

# ---------- B. VideoFromComponents + audio ----------
# 官方 audio["waveform"] 是 [batch, channels, samples]；save_to 取 waveform[0] → [channels, samples]
audio_wave = torch.from_numpy(np.random.rand(1, 1, 8000).astype(np.float32))  # batch=1, mono, 8000 samples
audio = {"waveform": audio_wave, "sample_rate": 8000}
video_ab = InputImpl.VideoFromComponents(
    Types.VideoComponents(images=frames, audio=audio, frame_rate=Fraction(8)),
)
out_b = node.save_video(video_ab, filename_prefix="ci_p4_b", prompt={"test": "B"},
                        extra_pnginfo={"workflow": {"nodes": []}})
entries_b = out_b.get("ui", {}).get("images", [])
b_id = entries_b[0].get("asset_id") if entries_b else None
check("B: 带 audio 上传成功", bool(b_id), f"-> {b_id}")
if b_id:
    dl_b = client.get_original(b_id).content
    tmp_b = tempfile.mkstemp(suffix=".mp4")
    os.close(tmp_b[0])
    with open(tmp_b[1], "wb") as fh:
        fh.write(dl_b)
    with av.open(tmp_b[1]) as cont:
        check("B: 音频流存在", len(cont.streams.audio) > 0, f"-> {len(cont.streams.audio)} 条")
    cleanup(b_id, tmp_b[1])

# ---------- C. VideoFromFile（LoadVideo 等价物）：copy 不重编码 + metadata 覆盖 ----------
src_tmp = tempfile.mkstemp(suffix=".mp4")
os.close(src_tmp[0])
# 源文件生成：必须开 movflags=use_metadata_tags，否则 MP4 muxer 丢弃自定义 key（custom_tag 写不进去）
with av.open(src_tmp[1], "w", options={"movflags": "use_metadata_tags"}) as cont:
    cont.metadata["prompt"] = json.dumps({"old": True})  # 源带旧 prompt
    cont.metadata["custom_tag"] = "keep-me"  # 源自定义标签应保留（overlay 语义）
    stream = cont.add_stream("h264", rate=10)
    stream.width, stream.height = 48, 48
    stream.pix_fmt = "yuv420p"
    for i in range(6):
        frame = np.random.randint(0, 256, (48, 48, 3), dtype=np.uint8)
        vf = av.VideoFrame.from_ndarray(frame, format="rgb24")
        for pkt in stream.encode(vf):
            cont.mux(pkt)
    for pkt in stream.encode():
        cont.mux(pkt)

video_file = InputImpl.VideoFromFile(src_tmp[1])
wf_c = {"nodes": [{"id": 2, "type": "LoadVideo", "inputs": {}}]}
out_c = node.save_video(video_file, filename_prefix="ci_p4_c", prompt={"test": "C"},
                        extra_pnginfo={"workflow": wf_c})
entries_c = out_c.get("ui", {}).get("images", [])
c_id = entries_c[0].get("asset_id") if entries_c else None
check("C: VideoFromFile 上传成功", bool(c_id), f"-> {c_id}")
if c_id:
    dl_c = client.get_original(c_id).content
    tmp_c = tempfile.mkstemp(suffix=".mp4")
    os.close(tmp_c[0])
    with open(tmp_c[1], "wb") as fh:
        fh.write(dl_c)
    with av.open(tmp_c[1]) as cont:
        vs_c = cont.streams.video[0]
        # copy 不重编码：codec 名与源一致（若转码会变 libx264 之外或 profile 变化）
        check("C: 视频流仍 h264", "h264" in vs_c.codec.name, f"-> {vs_c.codec.name}")
        check("C: 尺寸保持 48x48", vs_c.width == 48 and vs_c.height == 48, f"-> {vs_c.width}x{vs_c.height}")
        n_c = 0
        cont.seek(0)
        for _ in cont.decode(vs_c):
            n_c += 1
        check("C: 帧数保持 6（copy 无重编码帧数变化）", n_c == 6, f"-> {n_c}")
        md_c = cont.metadata
        check("C: 新 prompt 覆盖旧 prompt", md_c.get("prompt") and json.loads(md_c["prompt"]) == {"test": "C"}, f"-> {md_c.get('prompt')}")
        check("C: workflow metadata 写入", md_c.get("workflow") and json.loads(md_c["workflow"]) == wf_c, f"-> {md_c.get('workflow')}")
        check("C: 源自定义标签保留", md_c.get("custom_tag") == "keep-me", f"-> {md_c.get('custom_tag')}")
    cleanup(c_id, tmp_c[1])
os.remove(src_tmp[1])

# ---------- D. 中文 prompt 端到端（官方 ensure_ascii=True → \\uXXXX，下载后 json.loads 中文一致） ----------
zh_frames = torch.from_numpy((np.random.rand(4, 24, 24, 3) * 255).astype(np.float32) / 255.0)
zh_video = InputImpl.VideoFromComponents(
    Types.VideoComponents(images=zh_frames, audio=None, frame_rate=Fraction(4)),
)
zh_prompt = {"模型": "你好/模型.safetensors", "引号": 'a"b', "换行": "x\ny"}
out_d = node.save_video(zh_video, filename_prefix="ci_p4_d", prompt=zh_prompt,
                        extra_pnginfo={"workflow": {"nodes": [{"id": 3, "type": "CreateVideo"}]}})
entries_d = out_d.get("ui", {}).get("images", [])
d_id = entries_d[0].get("asset_id") if entries_d else None
check("D: 中文 prompt 上传成功", bool(d_id), f"-> {d_id}")
if d_id:
    dl_d = client.get_original(d_id).content
    tmp_d = tempfile.mkstemp(suffix=".mp4")
    os.close(tmp_d[0])
    with open(tmp_d[1], "wb") as fh:
        fh.write(dl_d)
    with av.open(tmp_d[1]) as cont:
        md_d = cont.metadata
        ok_zh = False
        if md_d.get("prompt"):
            parsed = json.loads(md_d["prompt"])
            ok_zh = parsed == zh_prompt
        check("D: 中文 prompt 无损恢复", ok_zh, f"-> {md_d.get('prompt')}")
    cleanup(d_id, tmp_d[1])

# 清理上传的测试资产
if uploaded:
    client.delete_assets(uploaded)
    print(f"已删除 {len(uploaded)} 个测试资产")

fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
