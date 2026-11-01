// Immich 选图节点：节点上一个「选择图片」按钮 → 弹窗选图 → 写回 asset_id
import { app } from "../../scripts/app.js";

const API = "/api/immich_plus";
const LS_TOKEN = "immich_panel_token";

function token() {
  try {
    return sessionStorage.getItem(LS_TOKEN) || localStorage.getItem(LS_TOKEN) || "";
  } catch (e) { return ""; }
}

function headers(extra) {
  const h = Object.assign({}, extra || {});
  const t = token();
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

async function getJSON(path) {
  const r = await fetch(API + path, { headers: headers({ Accept: "application/json" }) });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

const thumbCache = new Map();
async function thumbURL(assetId) {
  if (thumbCache.has(assetId)) return thumbCache.get(assetId);
  const p = fetch(`${API}/assets/${assetId}/thumbnail`, { headers: headers() })
    .then((r) => (r.ok ? r.blob() : null))
    .then((b) => (b ? URL.createObjectURL(b) : ""))
    .catch(() => "");
  thumbCache.set(assetId, p);
  return p;
}

const CSS = `
.immich-pick-mask{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;display:flex;align-items:center;justify-content:center}
.immich-pick-win{width:min(920px,92vw);max-height:84vh;background:#1b1e24;color:#e6e6e6;border:1px solid #3a3f4a;
  border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.6)}
.immich-pick-head{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid #3a3f4a}
.immich-pick-head select{flex:1;padding:8px 10px;border-radius:6px;border:1px solid #3a3f4a;background:#252a33;color:inherit;font:inherit}
.immich-pick-body{padding:12px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
.immich-pick-cell{aspect-ratio:1;background:#252a33;border-radius:6px;overflow:hidden;cursor:pointer;border:2px solid transparent}
.immich-pick-cell:hover{border-color:#5b8def}
.immich-pick-cell img{width:100%;height:100%;object-fit:cover;display:block}
.immich-pick-foot{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-top:1px solid #3a3f4a;font-size:12px;opacity:.85}
.immich-pick-btn{padding:6px 14px;border-radius:6px;border:1px solid #3a3f4a;background:#252a33;color:inherit;cursor:pointer}
.immich-pick-btn:disabled{opacity:.4;cursor:default}
`;

function ensureCSS() {
  if (document.getElementById("immich-pick-css")) return;
  const s = document.createElement("style");
  s.id = "immich-pick-css";
  s.textContent = CSS;
  document.head.appendChild(s);
}

async function openPicker(node) {
  ensureCSS();
  const mask = document.createElement("div");
  mask.className = "immich-pick-mask";
  mask.innerHTML = `
    <div class="immich-pick-win">
      <div class="immich-pick-head">
        <select class="immich-pick-album"></select>
        <button class="immich-pick-btn immich-pick-close">关闭</button>
      </div>
      <div class="immich-pick-body"></div>
      <div class="immich-pick-foot">
        <span class="immich-pick-info">加载中…</span>
        <span>
          <button class="immich-pick-btn immich-pick-prev">上一页</button>
          <button class="immich-pick-btn immich-pick-next">下一页</button>
        </span>
      </div>
    </div>`;
  document.body.appendChild(mask);

  const sel = mask.querySelector(".immich-pick-album");
  const body = mask.querySelector(".immich-pick-body");
  const info = mask.querySelector(".immich-pick-info");
  const prev = mask.querySelector(".immich-pick-prev");
  const next = mask.querySelector(".immich-pick-next");

  let albumId = "";
  let page = 1;
  let nextPage = null;

  const close = () => mask.remove();
  mask.querySelector(".immich-pick-close").onclick = close;
  mask.addEventListener("click", (e) => { if (e.target === mask) close(); });

  async function loadPage(p) {
    body.innerHTML = "";
    info.textContent = "加载中…";
    try {
      const d = await getJSON(`/albums/${albumId}/assets?page=${p}&size=24`);
      page = d.page || p;
      nextPage = d.nextPage || null;
      prev.disabled = page <= 1;
      next.disabled = !nextPage;
      const items = d.items || [];
      info.textContent = `第 ${page} 页 · ${items.length} 张${nextPage ? "（还有下一页）" : ""}`;
      for (const it of items) {
        const cell = document.createElement("div");
        cell.className = "immich-pick-cell";
        cell.title = it.originalFileName || it.id;
        const img = document.createElement("img");
        img.loading = "lazy";
        cell.appendChild(img);
        cell.onclick = () => {
          setWidget(node, "asset_id", it.id);
          close();
        };
        body.appendChild(cell);
        thumbURL(it.id).then((u) => { if (u) img.src = u; });
      }
    } catch (e) {
      info.textContent = "加载失败：" + e.message;
    }
  }

  try {
    const albums = await getJSON("/albums");
    sel.innerHTML = "";
    for (const a of albums) {
      const o = document.createElement("option");
      o.value = a.id;
      o.textContent = `${a.albumName}（${a.assetCount ?? "?"}）`;
      sel.appendChild(o);
    }
    if (!albums.length) { info.textContent = "Immich 里还没有相册"; return; }
    albumId = albums[0].id;
    sel.onchange = () => { albumId = sel.value; loadPage(1); };
    loadPage(1);
  } catch (e) {
    info.textContent = "读取相册失败：" + e.message;
  }

  prev.onclick = () => { if (page > 1) loadPage(page - 1); };
  next.onclick = () => { if (nextPage) loadPage(Number(nextPage)); };
}

function setWidget(node, name, value) {
  const w = (node.widgets || []).find((x) => x.name === name);
  if (!w) return;
  w.value = value;
  // 同步到 widgets_values，否则保存工作流后丢失
  try { node.widgets_values = (node.widgets || []).map((x) => x.value); } catch (e) {}
  node.setDirtyCanvas(true, true);
  app.graph.setDirtyCanvas(true, true);
}

app.registerExtension({
  name: "Immich.PickImage",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "ImmichPickImage") return;
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated ? onCreated.apply(this, arguments) : undefined;
      const btn = this.addWidget("button", "选择图片", null, () => openPicker(this));
      btn.serialize = false;
      return r;
    };
  },
});
