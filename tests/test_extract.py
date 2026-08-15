#!/usr/bin/env python3
"""_extract_ext 边界测试：originalPath → 扩展名提取。

覆盖审小爪 P2-1：带点目录、无扩展名文件、脏串不产出假 ext。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from immich_client import _extract_ext  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


cases = [
    # (输入 originalPath, 期望 ext, 说明)
    ("/data/library/upload/ComfyUI_01834_.png", "png", "普通 png"),
    ("/data/library/upload/video_01.mp4", "mp4", "普通 mp4"),
    ("C:\\Users\\x\\Pictures\\a.JPG", "jpg", "大写扩展名转小写"),
    ("/upload/2/img", "", "无扩展名（脏串：img 是路径片段）"),
    ("/dir.with.dot/file", "", "带点目录（脏串：dot 是目录名）"),
    ("/upload/noext", "", "无点"),
    ("", "", "空串"),
    ("/upload/file.tar.gz", "gz", "复合扩展名取最后一段"),
    ("/upload/a.verylongextensionname", "", "超长扩展名拒绝"),
    ("/upload/file.PNG", "png", "大写 PNG"),
    ("/upload/.hidden", "", "隐藏文件（无扩展名）"),
    ("/upload/1.2.3", "3", "数字扩展名（Immich 不会出但不应崩）"),
]

for inp, want, desc in cases:
    got = _extract_ext(inp)
    check(f"ext({inp!r}) -> {want!r} [{desc}]", got == want, f"got {got!r}")

fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
