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

// 缩略图缓存：LRU 上限，超出时释放最旧的 blob URL（否则切页会一直累积内存）
const filterState = { albumId: "", type: "", order: "desc", page: 1 };  // 跨次打开记住筛选与页码

const THUMB_MAX = 120;
const thumbCache = new Map();

function evictThumbs() {
  while (thumbCache.size > THUMB_MAX) {
    const [oldId, oldP] = thumbCache.entries().next().value;
    thumbCache.delete(oldId);
    Promise.resolve(oldP)
      .then((u) => { if (u) URL.revokeObjectURL(u); })
      .catch(() => {});
  }
}

async function thumbURL(assetId) {
  if (thumbCache.has(assetId)) {
    // 命中就移到队尾，维持 LRU 顺序
    const p = thumbCache.get(assetId);
    thumbCache.delete(assetId);
    thumbCache.set(assetId, p);
    return p;
  }
  const p = fetch(`${API}/assets/${assetId}/thumbnail`, { headers: headers() })
    .then((r) => (r.ok ? r.blob() : null))
    .then((b) => (b ? URL.createObjectURL(b) : ""))
    .catch(() => "");
  thumbCache.set(assetId, p);
  evictThumbs();
  return p;
}

const CSS = `
.immich-pick-mask{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;display:flex;align-items:center;justify-content:center}
.immich-pick-win{width:min(920px,92vw);max-height:84vh;background:#1b1e24;color:#e6e6e6;border:1px solid #3a3f4a;
  border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.6)}
.immich-pick-filters{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid #3a3f4a;flex-wrap:wrap}
.immich-pick-filters label{display:flex;align-items:center;gap:6px;font-size:12px;opacity:.9}
.immich-pick-filters select{padding:6px 8px;border-radius:6px;border:1px solid #3a3f4a;background:#252a33;color:inherit;font:inherit}
.immich-pick-chips{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.immich-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
  background:#2a3038;border:1px solid #3a3f4a;font-size:12px}
.immich-chip b{cursor:pointer;opacity:.7;font-weight:400}
.immich-chip b:hover{opacity:1;color:#ff6b6b}
.immich-pick-head select{flex:1;padding:8px 10px;border-radius:6px;border:1px solid #3a3f4a;background:#252a33;color:inherit;font:inherit}
.immich-pick-body{padding:12px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
.immich-pick-cell{aspect-ratio:1;background:#252a33;border-radius:6px;overflow:hidden;cursor:pointer;border:2px solid transparent}
.immich-pick-cell:hover{border-color:#5b8def}
.immich-pick-cell.immich-pick-selected{border-color:#4ade80;box-shadow:0 0 0 2px #4ade80 inset}
.immich-pick-cell.immich-pick-selected::after{content:"✓";position:absolute;top:4px;right:6px;color:#4ade80;font-weight:700}
.immich-pick-cell{position:relative}
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
      <div class="immich-pick-filters">
        <label>相册<select class="f-album"></select></label>
        <label>类型<select class="f-type">
          <option value="">全部</option><option value="IMAGE">图片</option><option value="VIDEO">视频</option>
        </select></label>
        <label>排序<select class="f-order">
          <option value="desc">最新优先</option><option value="asc">最早优先</option>
        </select></label>
        <span class="immich-pick-chips"></span>
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

  const selAlbum = mask.querySelector(".f-album");
  const selType = mask.querySelector(".f-type");
  const selOrder = mask.querySelector(".f-order");
  const chips = mask.querySelector(".immich-pick-chips");
  const body = mask.querySelector(".immich-pick-body");
  const info = mask.querySelector(".immich-pick-info");
  const prev = mask.querySelector(".immich-pick-prev");
  const next = mask.querySelector(".immich-pick-next");

  // 与上次打开保持一致的筛选状态
  selType.value = filterState.type || "";
  selOrder.value = filterState.order || "desc";

  const selectedId = getWidget(node, "asset_id") || "";
  let page = 1;
  let nextPage = null;

  const close = () => mask.remove();
  mask.querySelector(".immich-pick-close").onclick = close;
  mask.addEventListener("click", (e) => { if (e.target === mask) close(); });

  function syncState() {
    filterState.albumId = selAlbum.value;
    filterState.type = selType.value;
    filterState.order = selOrder.value;
    filterState.page = page;
  }

  // 已生效的筛选条件，每个带 ×
  function renderChips() {
    chips.innerHTML = "";
    const add = (text, onRemove) => {
      const c = document.createElement("span");
      c.className = "immich-chip";
      c.textContent = text;
      const x = document.createElement("b");
      x.textContent = "×";
      x.title = "移除这个条件";
      x.onclick = onRemove;
      c.appendChild(x);
      chips.appendChild(c);
    };
    const alb = selAlbum.options[selAlbum.selectedIndex];
    if (alb) add("相册: " + alb.textContent, () => { selAlbum.selectedIndex = 0; onChange(); });
    if (selType.value) {
      add("类型: " + (selType.value === "IMAGE" ? "图片" : "视频"),
          () => { selType.value = ""; onChange(); });
    }
    if (selOrder.value !== "desc") {
      add("排序: 最早优先", () => { selOrder.value = "desc"; onChange(); });
    }
  }

  function onChange() {
    syncState();
    renderChips();
    loadPage(1);
  }

  async function loadPage(p) {
    body.innerHTML = "";
    info.textContent = "加载中…";
    try {
      const q = [`page=${p}`, "size=24", `order=${selOrder.value}`];
      if (selType.value) q.push(`type=${selType.value}`);
      const d = await getJSON(`/albums/${selAlbum.value}/assets?${q.join("&")}`);
      page = d.page || p;
      nextPage = d.nextPage || null;
      syncState();
      prev.disabled = page <= 1;
      next.disabled = !nextPage;
      const items = d.items || [];
      info.textContent = `第 ${page} 页 · ${items.length} 张${nextPage ? "（还有下一页）" : ""}`;
      for (const it of items) {
        const cell = document.createElement("div");
        cell.className = "immich-pick-cell" + (it.id === selectedId ? " immich-pick-selected" : "");
        cell.title = it.originalFileName || it.id;
        const img = document.createElement("img");
        img.loading = "lazy";
        cell.appendChild(img);
        cell.onclick = () => {
          setWidget(node, "asset_id", it.id);
          applyNodePreview(node, it.id);
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
    selAlbum.innerHTML = "";
    for (const a of albums) {
      const o = document.createElement("option");
      o.value = a.id;
      o.textContent = `${a.albumName}（${a.assetCount ?? "?"}）`;
      selAlbum.appendChild(o);
    }
    if (!albums.length) { info.textContent = "Immich 里还没有相册"; return; }
    const remembered = filterState.albumId;
    selAlbum.value = (remembered && albums.some((a) => a.id === remembered)) ? remembered : albums[0].id;
    selAlbum.onchange = onChange;
    selType.onchange = onChange;
    selOrder.onchange = onChange;
    renderChips();
    loadPage(filterState.page || 1);
  } catch (e) {
    info.textContent = "读取相册失败：" + e.message;
  }

  prev.onclick = () => { if (page > 1) loadPage(page - 1); };
  next.onclick = () => { if (nextPage) loadPage(Number(nextPage)); };
}

// 选完立刻在节点上画缩略图 —— ui.images 只有「执行后」才渲染，
// 这里直接用 node.imgs（LiteGraph 会画它），不等跑图。
async function applyNodePreview(node, assetId) {
  const url = await thumbURL(assetId);
  if (!url) return;
  const img = new Image();
  img.onload = () => {
    node.imgs = [img];
    if (node.images) node.images = [img];
    node.setDirtyCanvas(true, true);
    app.graph.setDirtyCanvas(true, true);
  };
  img.src = url;
}

function getWidget(node, name) {
  const w = (node.widgets || []).find((x) => x.name === name);
  return w ? w.value : "";
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
