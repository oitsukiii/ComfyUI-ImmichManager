#!/usr/bin/env python3
"""P1 自测：直接验证 immich_client.py 各方法（测试专用，不提交 key）。

用法: IMMICH_TEST_KEY=xxx python3 tests/test_client.py
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from immich_client import ImmichClient, ImmichError  # noqa: E402

KEY = os.environ.get("IMMICH_TEST_KEY", "")
BASE = os.environ.get("IMMICH_TEST_BASE", "http://127.0.0.1:2283/api")
if not KEY:
    print("需要环境变量 IMMICH_TEST_KEY")
    sys.exit(1)

client = ImmichClient(BASE, KEY)
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


# 1. 服务器
check("ping", client.ping())
ver = client.version()
check("version", ver.get("major") == 2, f"-> {ver}")

# 2. 时间轴
buckets = client.get_buckets()
check("get_buckets", len(buckets) > 0, f"-> {len(buckets)} 个桶")
if buckets:
    tb = buckets[0]["timeBucket"]
    assets = client.get_bucket(tb)
    check("get_bucket(列式转对象)", len(assets) > 0, f"-> {tb} {len(assets)} 条")
    if assets:
        first = assets[0]
        check("bucket 字段完整", "id" in first and "fileCreatedAt" in first,
              f"-> 首条 id={first.get('id')}")

        # 3. 详情
        detail = client.get_asset(first["id"])
        check("get_asset", detail.get("id") == first.get("id"))

        # 4. 缩略图（流式）
        thumb = client.get_thumbnail(first["id"])
        check("get_thumbnail", thumb.status_code == 200 and len(thumb.content) > 0,
              f"-> {thumb.headers.get('content-type')} {len(thumb.content)}B")
        thumb.close()

# 5. 上传 + 去重 + 更新 + 删除（造数据测完清理；随机内容避免与历史数据撞 sha1）
from PIL import Image  # noqa: E402

buf = io.BytesIO()
Image.new("RGB", (64, 64), (os.urandom(1)[0], os.urandom(1)[0], os.urandom(1)[0])).save(buf, format="PNG")
png = buf.getvalue()

up1 = client.upload_bytes(png, "p1_test.png", "image/png")
check("upload_bytes", up1.get("status") == "created", f"-> {up1}")
id1 = up1.get("id")

up2 = client.upload_bytes(png, "p1_test_dup.png", "image/png", checksum=up1.get("checksum"))
check("上传去重", up2.get("status") == "duplicate" and up2.get("id") == id1, f"-> {up2}")

if id1:
    upd = client.update_asset(id1, isFavorite=True)
    check("update_asset(favorite)", upd.get("isFavorite") is True)

# 5b. 批量收藏（v0.6）：批量端点 PUT /assets 实测（至少 1 个 id）
if id1:
    r_bulk = client.update_assets_bulk([id1], True)
    check("update_assets_bulk(批量收藏)", r_bulk.get("updated") == 1 and not r_bulk.get("failed"),
          f"-> {r_bulk}")
    detail_b = client.get_asset(id1)
    check("批量收藏生效 isFavorite", detail_b.get("isFavorite") is True)

# 6. 相册（测试后删除）
album = client.ensure_album("comfyui-immichmanager-test")
check("ensure_album(创建/查找)", album is not None and "id" in album, f"-> {album.get('albumName')}")
if id1:
    add = client.add_to_album(album["id"], [id1])
    ok_add = isinstance(add, (list, dict)) and len(add) > 0
    check("add_to_album", ok_add, f"-> {str(add)[:120]}")

# 7. 删除：v2 实测删除总是进回收站（force 不绕过），验证软删 + isTrashed
if id1:
    ok = client.delete_assets([id1], force=False)
    check("delete_assets", ok)
    try:
        detail_after = client.get_asset(id1)
        check("删除进回收站(isTrashed)", detail_after.get("isTrashed") is True)
    except ImmichError:
        check("删除进回收站(isTrashed)", False, "-> 删除后直接 404（非预期）")
    # force=true 同样进回收站（v2 实测行为），断言其不抛错
    ok2 = client.delete_assets([id1], force=True)
    check("delete_assets(force=true 不报错)", ok2)

if album:
    try:
        resp = client._request("DELETE", f"/albums/{album['id']}")
        check("删除测试相册", resp.status_code in (200, 204))
    except ImmichError as exc:
        check("删除测试相册", False, f"-> {exc}")

# 8. 上传文件（独立随机 png，避免与上面内存图撞 sha1）
tmp = "/tmp/ci_p1_test.png"
buf2 = io.BytesIO()
Image.new("RGB", (64, 64), (os.urandom(1)[0], os.urandom(1)[0], os.urandom(1)[0])).save(buf2, format="PNG")
with open(tmp, "wb") as fh:
    fh.write(buf2.getvalue())
try:
    upf = client.upload_file(tmp)
    check("upload_file", upf.get("status") in ("created", "duplicate"), f"-> {upf}")
    if upf.get("id"):
        client.delete_assets([upf["id"]], force=True)
except ImmichError as exc:
    check("upload_file", False, f"-> {exc}")
os.remove(tmp)

# 汇总
fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
