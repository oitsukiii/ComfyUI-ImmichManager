#!/usr/bin/env python3
"""UI verification for the Immich manager settings page.

Needs a headless Chromium on :9222 pointed at ComfyUI:

  chromium --headless=new --no-sandbox --disable-gpu --remote-debugging-port=9222 \
           --user-data-dir=$(mktemp -d) http://<comfyui>:8188/

Then: python tests/cdp/verify.py

Checks the rail button, the settings registration marker, and what the
Settings dialog actually draws -- labels, inputs, selects, buttons. Exits
non-zero if anything is missing, so it can gate a deploy.
"""
import json, sys, urllib.request

EXPECT = {"inputs": 5, "selects": 0, "hints": 10, "dropdowns": 6,  # 4 文本框 + 搜索框；下拉改为自绘；10 条提示
          "buttons": ["显示", "重新生成", "清空", "测试连接"]}

def page_url():
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=10).read())
    for t in d:
        if t.get("type") == "page" and "8188" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    sys.exit("no ComfyUI page target on :9222")

def main():   # 注：按钮文案是「测试连接」不是「连接测试」
    try:
        import websocket
    except ImportError:
        sys.exit("pip install websocket-client")
    ws = websocket.create_connection(page_url(), timeout=40)
    seq = [0]

    def ev(expr):
        seq[0] += 1
        ws.send(json.dumps({"id": seq[0], "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == seq[0]:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    return {"__error": str(r["exceptionDetails"])[:300]}
                return r.get("result", {}).get("value")

    def open_settings():
        ev("""(() => { const b=[...document.querySelectorAll('button')]
              .find(x => (x.getAttribute('aria-label')||'').indexOf('设置') >= 0); if (b) b.click(); })()""")
        ev("""(() => { const els=[...document.querySelectorAll('[role=dialog] *')]
              .filter(e => (e.textContent||'').trim() === 'Immich' && e.children.length <= 2);
              if (els.length) (els[els.length-1].closest('button,[role=button],li,a') || els[els.length-1]).click(); })()""")

    import time
    time.sleep(7)      # 等插件注册完成（配置是异步 fetch）
    marks = ev("""JSON.stringify({rail: document.documentElement.dataset.immichRail,
                                 settings: document.documentElement.dataset.immichSettings,
                                 railBtn: !!document.getElementById('immich-rail-btn')})""")
    marks = json.loads(marks) if isinstance(marks, str) else marks
    open_settings()
    time.sleep(3)      # 等对话框把行画出来
    seen = ev("""(() => { const dlg=document.querySelector('[role=dialog]')||document.body;
      return JSON.stringify({inputs: dlg.querySelectorAll('input').length,
        selects: dlg.querySelectorAll('select').length,
        hints: dlg.querySelectorAll('[data-immich-hint]').length,
        dropdowns: [...dlg.querySelectorAll('[data-immich-row] button')].filter(b=>b.textContent.indexOf('▾')>=0).length,
        buttons: [...dlg.querySelectorAll('button')].map(b=>b.textContent.trim())
                   .filter(t=>['显示','重新生成','清空','测试连接','刷新相册'].includes(t))}); })()""")
    seen = json.loads(seen) if isinstance(seen, str) else seen

    fails = []
    for k in ("rail", "settings"):
        if marks.get(k) != "ok":
            fails.append(f"{k} marker = {marks.get(k)}")
    if not marks.get("railBtn"):
        fails.append("rail button missing")
    if seen.get("inputs") != EXPECT["inputs"]:
        fails.append(f"inputs {seen.get('inputs')} != {EXPECT['inputs']}")
    if seen.get("selects") != EXPECT["selects"]:
        fails.append(f"selects {seen.get('selects')} != {EXPECT['selects']}")
    if seen.get("hints") != EXPECT["hints"]:
        fails.append(f"hints {seen.get('hints')} != {EXPECT['hints']}")
    if seen.get("dropdowns") != EXPECT["dropdowns"]:
        fails.append(f"dropdowns {seen.get('dropdowns')} != {EXPECT['dropdowns']}")
    for b in EXPECT["buttons"]:
        if b not in seen.get("buttons", []):
            fails.append(f"button missing: {b}")

    print("marks :", marks)
    print("dialog:", seen)
    if fails:
        print("\n[FAIL]")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("\n[OK]")

if __name__ == "__main__":
    main()
