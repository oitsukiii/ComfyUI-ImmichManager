#!/usr/bin/env python3
"""离线单测：ImmichClient.update_assets_bulk 批量收藏逻辑（不触网，mock session）。

覆盖三条路径：
1. 批量端点 PUT /assets 成功（204）→ 全成
2. 批量端点失败（400）→ fallback 逐条全部成功
3. 批量端点失败 + 部分逐条失败 → 部分成功（updated/failed 明细）

用法: python3 tests/test_client_bulk_offline.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from immich_client import ImmichClient, ImmichError  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, json_data=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json = json_data

    def json(self):
        return self._json if self._json is not None else {}


def make_client(handler):
    """构造 ImmichClient，session.request 换成 handler(method, url, **kwargs) → FakeResponse。"""
    c = ImmichClient("http://immich.test/api", "k")
    c.session.request = handler
    return c


# ---- 1. 批量端点成功 ----
def h1(method, url, **kwargs):
    assert method == "PUT" and url.endswith("/assets"), (method, url)
    assert kwargs["json"] == {"ids": ["a", "b"], "isFavorite": True}, kwargs["json"]
    return FakeResponse(204)


c1 = make_client(h1)
r1 = c1.update_assets_bulk(["a", "b"], True)
check("批量成功 updated=2", r1 == {"updated": 2, "failed": []}, f"-> {r1}")

# ---- 2. 批量端点失败 → fallback 逐条全成 ----
def h2(method, url, **kwargs):
    if method == "PUT" and url.endswith("/assets"):
        return FakeResponse(400, text="Not found or no asset.update access")
    # 逐条：PUT /assets/{id}
    assert url.endswith("/assets/a") or url.endswith("/assets/b"), url
    assert kwargs["json"] == {"isFavorite": True}, kwargs["json"]
    return FakeResponse(200, json_data={"id": url.rsplit("/", 1)[-1], "isFavorite": True})


c2 = make_client(h2)
r2 = c2.update_assets_bulk(["a", "b"], True)
check("批量失败 fallback 全成 updated=2", r2 == {"updated": 2, "failed": []}, f"-> {r2}")

# ---- 3. 批量端点失败 + 部分逐条失败 ----
def h3(method, url, **kwargs):
    if method == "PUT" and url.endswith("/assets"):
        return FakeResponse(400, text="Not found or no asset.update access")
    if url.endswith("/assets/bad"):
        raise ImmichError("HTTP 500", 500)
    return FakeResponse(200, json_data={"id": url.rsplit("/", 1)[-1], "isFavorite": True})


c3 = make_client(h3)
r3 = c3.update_assets_bulk(["a", "bad", "c"], True)
check("部分成功 updated=2 failed=[bad]", r3 == {"updated": 2, "failed": ["bad"]}, f"-> {r3}")

# ---- 4. 空 ids：直接返回，不调网络 ----
def h4(method, url, **kwargs):
    raise AssertionError("空 ids 不应发起请求")


c4 = make_client(h4)
r4 = c4.update_assets_bulk([], True)
check("空 ids updated=0", r4 == {"updated": 0, "failed": []}, f"-> {r4}")

# ---- 5. isFavorite False（取消收藏）透传 ----
def h5(method, url, **kwargs):
    assert kwargs["json"] == {"ids": ["a"], "isFavorite": False}, kwargs["json"]
    return FakeResponse(204)


c5 = make_client(h5)
r5 = c5.update_assets_bulk(["a"], False)
check("取消收藏透传 isFavorite=False", r5 == {"updated": 1, "failed": []}, f"-> {r5}")

# ---- 6. 5xx/网络错误：不降级，直接抛（避免 N×3 次重试） ----
def h6(method, url, **kwargs):
    return FakeResponse(500, text="Internal Server Error")


c6 = make_client(h6)
try:
    c6.update_assets_bulk(["a", "b"], True)
    check("5xx 直接抛（不降级）", False, "-> 未抛异常")
except ImmichError as exc:
    check("5xx 直接抛（不降级）", exc.status_code == 500, f"-> {exc}")

# ---- 7. 错误详情脱敏 _redact（防长凭据串进日志） ----
from immich_client import _redact  # noqa: E402

check("_redact 遮罩长 token",
      "***" in _redact("Unauthorized: invalid key abcdefghijklmnopqrstuvwxyz012345") and
      "abcdefghijklmnopqrstuvwxyz012345" not in _redact("Unauthorized: invalid key abcdefghijklmnopqrstuvwxyz012345"),
      f"-> {_redact('Unauthorized: invalid key abcdefghijklmnopqrstuvwxyz012345')}")
check("_redact 保留 uuid（36 位含连字符不误伤）",
      "550e8400-e29b-41d4-a716-446655440000" in _redact("asset 550e8400-e29b-41d4-a716-446655440000 not found"),
      f"-> {_redact('asset 550e8400-e29b-41d4-a716-446655440000 not found')}")
check("_redact 保留短文本",
      _redact("Forbidden") == "Forbidden", f"-> {_redact('Forbidden')}")

# 错误详情经 _request 脱敏后进 ImmichError（模拟 401 带 key 回显）
def h7(method, url, **kwargs):
    return FakeResponse(401, text="bad key averylongsecretkey0123456789abcdef")


c7 = make_client(h7)
try:
    c7.get_albums()
    check("_request 错误详情脱敏", False, "-> 未抛异常")
except ImmichError as exc:
    msg = str(exc)
    check("_request 错误详情脱敏",
          "averylongsecretkey0123456789abcdef" not in msg and "***" in msg,
          f"-> {msg}")

fails = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(fails)}/{len(results)} PASS =====")
if fails:
    print("失败项:", fails)
    sys.exit(1)
