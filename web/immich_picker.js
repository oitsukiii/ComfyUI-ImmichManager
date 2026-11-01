// Immich 选图节点：节点上一个「选择图片」按钮 → 弹窗选图 → 写回 asset_id
// 筛选模型：一组「条件」，每条对应顶部一个胶囊，也对应条件窗口里的一行。
import { app } from "../../scripts/app.js";

const API = "/api/immich_plus";
const LS_TOKEN = "immich_panel_token";

function token() {
  try { return sessionStorage.getItem(LS_TOKEN) || localStorage.getItem(LS_TOKEN) || ""; }
  catch (e) { return ""; }
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

// ── 缩略图缓存（LRU 上限，超出释放 blob URL）──
const THUMB_MAX = 120;
const thumbCache = new Map();
function evictThumbs() {
  while (thumbCache.size > THUMB_MAX) {
    const [oldId, oldP] = thumbCache.entries().next().value;
    thumbCache.delete(oldId);
    Promise.resolve(oldP).then((u) => { if (u) URL.revokeObjectURL(u); }).catch(() => {});
  }
}
async function thumbURL(assetId) {
  if (thumbCache.has(assetId)) {
    const p = thumbCache.get(assetId);
    thumbCache.delete(assetId); thumbCache.set(assetId, p);
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

// ── 筛选字段定义 ──
const FIELDS = [
  { key: "album",  label: "相册",   ops: ["为"],                     value: "albumList" },
  { key: "person", label: "人脸",   ops: ["为"],                     value: "personList" },
  { key: "tag",    label: "标签",   ops: ["为"],                     value: "tagList" },
  { key: "type",   label: "类型",   ops: ["为"],                     value: "typeList" },
  { key: "date",   label: "时间段", ops: ["早于", "晚于", "介于"],    value: "date" },
];
const TYPE_OPTIONS = [{ id: "IMAGE", name: "图片" }, { id: "VIDEO", name: "视频" }];

// 筛选状态存两份：
//   node.properties.immichFilters —— 随工作流 JSON 保存/还原（跨重启、跨机器）
//   模块级 lastFilters           —— 本页会话内的兜底（新建节点时还没属性）
const lastFilters = { conditions: [], order: "desc", page: 1 };

function readFilters(node) {
  const p = node && node.properties && node.properties.immichFilters;
  if (p && Array.isArray(p.conditions)) {
    return { conditions: p.conditions, order: p.order || "desc", page: p.page || 1 };
  }
  return lastFilters;
}

function writeFilters(node, st) {
  lastFilters.conditions = st.conditions;
  lastFilters.order = st.order;
  lastFilters.page = st.page;
  if (!node) return;
  node.properties = node.properties || {};
  node.properties.immichFilters = {
    conditions: st.conditions, order: st.order, page: st.page,
  };
  try { node.setDirtyCanvas(true, false); } catch (e) {}
}

const CSS = `
.immich-pick-mask{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;display:flex;align-items:center;justify-content:center}
.immich-pick-win{width:min(940px,92vw);height:min(720px,86vh);background:#1b1e24;color:#e6e6e6;border:1px solid #3a3f4a;
  border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.6)}
.immich-pick-bar{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid #3a3f4a;flex-wrap:wrap}
.immich-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
  background:#2a3038;border:1px solid #3a3f4a;font-size:12px}
.immich-chip b{cursor:pointer;opacity:.7;font-weight:400}
.immich-chip b:hover{opacity:1;color:#ff6b6b}
.immich-pick-body{flex:1 1 auto;min-height:0;padding:12px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
  grid-auto-rows:150px;gap:8px}
.immich-pick-cell{height:150px;background:#252a33;border-radius:6px;overflow:hidden;cursor:pointer;
  border:2px solid transparent;box-sizing:border-box;position:relative}
.immich-pick-cell:hover{border-color:#5b8def}
.immich-pick-cell.immich-pick-selected{border-color:#4ade80;box-shadow:0 0 0 2px #4ade80 inset}
.immich-pick-cell.immich-pick-selected::after{content:"✓";position:absolute;top:4px;right:6px;color:#4ade80;font-weight:700}
.immich-pick-cell img{width:100%;height:100%;object-fit:cover;display:block}
.immich-pick-foot{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;
  border-top:1px solid #3a3f4a;font-size:12px;opacity:.9}
.immich-pick-btn{padding:6px 14px;border-radius:6px;border:1px solid #3a3f4a;background:#252a33;color:inherit;
  cursor:pointer;font:inherit}
.immich-pick-btn.primary{background:#2f6feb;border-color:#2f6feb}
.immich-pick-btn:disabled{opacity:.4;cursor:default}
/* 条件窗口 */
.immich-cond-mask{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:10001;display:flex;align-items:center;justify-content:center}
.immich-cond-win{width:min(720px,92vw);max-height:80vh;background:#1b1e24;color:#e6e6e6;border:1px solid #3a3f4a;
  border-radius:10px;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.6)}
.immich-cond-head{padding:12px 16px;border-bottom:1px solid #3a3f4a;font-weight:600}
.immich-cond-rows{padding:12px 16px;overflow:auto;display:flex;flex-direction:column;gap:10px}
.immich-cond-row{display:grid;grid-template-columns:150px 120px 1fr 28px;gap:8px;align-items:center}
.immich-cond-row select,.immich-cond-row input{padding:7px 9px;border-radius:6px;border:1px solid #3a3f4a;
  background:#252a33;color:#e6e6e6;font:inherit;width:100%;box-sizing:border-box;color-scheme:dark}
.immich-cond-row .immich-cond-del{cursor:pointer;text-align:center;opacity:.7}
.immich-cond-row .immich-cond-del:hover{opacity:1;color:#ff6b6b}
.immich-cond-foot{display:flex;justify-content:space-between;padding:12px 16px;border-top:1px solid #3a3f4a;gap:8px}
.immich-cond-range{display:flex;gap:6px}
`;

function ensureCSS() {
  if (document.getElementById("immich-pick-css")) return;
  const s = document.createElement("style");
  s.id = "immich-pick-css";
  s.textContent = CSS;
  document.head.appendChild(s);
}

function fieldDef(key) { return FIELDS.find((f) => f.key === key) || FIELDS[0]; }

function condText(c, lists) {
  const f = fieldDef(c.field);
  const name = (arr, id) => (arr.find((x) => x.id === id) || {}).name || id;
  let val = "";
  if (c.field === "album") val = name(lists.albums, c.value);
  else if (c.field === "person") val = name(lists.people, c.value);
  else if (c.field === "tag") val = name(lists.tags, c.value);
  else if (c.field === "type") val = name(TYPE_OPTIONS, c.value);
  else if (c.field === "date") {
    val = c.op === "介于" ? `${c.value || "?"} ~ ${c.value2 || "?"}` : c.value || "?";
  }
  return `${f.label}: ${val}`;
}

function buildQuery(conditions, order, page, size) {
  const q = [`page=${page}`, `size=${size}`, `order=${order}`];
  for (const c of conditions) {
    if (c.field === "album" && c.value) q.push(`album=${encodeURIComponent(c.value)}`);
    else if (c.field === "person" && c.value) q.push(`person=${encodeURIComponent(c.value)}`);
    else if (c.field === "tag" && c.value) q.push(`tag=${encodeURIComponent(c.value)}`);
    else if (c.field === "type" && c.value) q.push(`type=${encodeURIComponent(c.value)}`);
    else if (c.field === "date" && c.value) {
      if (c.op === "早于") q.push(`before=${encodeURIComponent(c.value)}`);
      else if (c.op === "晚于") q.push(`after=${encodeURIComponent(c.value)}`);
      else if (c.op === "介于") {
        q.push(`after=${encodeURIComponent(c.value)}`);
        if (c.value2) q.push(`before=${encodeURIComponent(c.value2)}`);
      }
    }
  }
  return q.join("&");
}

// ── 条件编辑窗口：一行 = 一个胶囊 ──
function openConditionEditor(initial, lists, onApply) {
  ensureCSS();
  const mask = document.createElement("div");
  mask.className = "immich-cond-mask";
  mask.innerHTML = `
    <div class="immich-cond-win">
      <div class="immich-cond-head">筛选条件</div>
      <div class="immich-cond-rows"></div>
      <div class="immich-cond-foot">
        <button class="immich-pick-btn immich-cond-add">新增条件</button>
        <span>
          <button class="immich-pick-btn immich-cond-cancel">取消</button>
          <button class="immich-pick-btn primary immich-cond-ok">确定</button>
        </span>
      </div>
    </div>`;
  document.body.appendChild(mask);

  const rowsEl = mask.querySelector(".immich-cond-rows");
  let rows = initial.map((c) => ({ ...c }));
  if (!rows.length) rows.push({ field: "album", op: "为", value: "", value2: "" });

  function valueOptions(field) {
    if (field === "album") return lists.albums;
    if (field === "person") return lists.people;
    if (field === "tag") return lists.tags;
    if (field === "type") return TYPE_OPTIONS;
    return [];
  }

  function render() {
    rowsEl.innerHTML = "";
    rows.forEach((c, idx) => {
      const row = document.createElement("div");
      row.className = "immich-cond-row";

      // A 字段
      const selA = document.createElement("select");
      FIELDS.forEach((f) => {
        const o = document.createElement("option");
        o.value = f.key; o.textContent = f.label;
        if (f.key === c.field) o.selected = true;
        selA.appendChild(o);
      });
      selA.onchange = () => {
        c.field = selA.value;
        c.op = fieldDef(c.field).ops[0];
        c.value = ""; c.value2 = "";
        render();
      };

      // B 判断
      const selB = document.createElement("select");
      fieldDef(c.field).ops.forEach((op) => {
        const o = document.createElement("option");
        o.value = op; o.textContent = op;
        if (op === c.op) o.selected = true;
        selB.appendChild(o);
      });
      selB.onchange = () => { c.op = selB.value; render(); };

      // C 值
      const wrapC = document.createElement("div");
      const def = fieldDef(c.field);
      if (def.value === "date") {
        const span = document.createElement("div");
        span.className = "immich-cond-range";
        const d1 = document.createElement("input");
        d1.type = "date"; d1.value = c.value || "";
        d1.onchange = () => { c.value = d1.value; };
        span.appendChild(d1);
        if (c.op === "介于") {
          const d2 = document.createElement("input");
          d2.type = "date"; d2.value = c.value2 || "";
          d2.onchange = () => { c.value2 = d2.value; };
          span.appendChild(d2);
        }
        wrapC.appendChild(span);
      } else {
        const sel = document.createElement("select");
        const opts = valueOptions(c.field);
        const empty = document.createElement("option");
        empty.value = ""; empty.textContent = opts.length ? "（请选择）" : "（没有可选项）";
        sel.appendChild(empty);
        opts.forEach((o) => {
          const op = document.createElement("option");
          op.value = o.id; op.textContent = o.name || o.id;
          if (o.id === c.value) op.selected = true;
          sel.appendChild(op);
        });
        sel.onchange = () => { c.value = sel.value; };
        wrapC.appendChild(sel);
      }

      // 删除本行
      const del = document.createElement("span");
      del.className = "immich-cond-del";
      del.textContent = "×";
      del.title = "删除这个条件";
      del.onclick = () => { rows.splice(idx, 1); render(); };

      row.appendChild(selA);
      row.appendChild(selB);
      row.appendChild(wrapC);
      row.appendChild(del);
      rowsEl.appendChild(row);
    });
  }

  mask.querySelector(".immich-cond-add").onclick = () => {
    rows.push({ field: "album", op: "为", value: "", value2: "" });
    render();
  };
  mask.querySelector(".immich-cond-cancel").onclick = () => mask.remove();
  mask.querySelector(".immich-cond-ok").onclick = () => {
    const clean = rows.filter((c) => {
      if (c.field === "date") return !!(c.value || c.value2);
      return !!c.value;
    });
    onApply(clean);
    mask.remove();
  };
  render();
}

async function openPicker(node) {
  ensureCSS();
  const mask = document.createElement("div");
  mask.className = "immich-pick-mask";
  mask.innerHTML = `
    <div class="immich-pick-win">
      <div class="immich-pick-bar">
        <button class="immich-pick-btn immich-add-cond">+ 添加条件</button>
        <span class="immich-pick-chips"></span>
        <span style="margin-left:auto">
          <select class="immich-order" style="padding:6px 8px;border-radius:6px;border:1px solid #3a3f4a;background:#252a33;color:inherit;font:inherit;color-scheme:dark">
            <option value="desc">最新优先</option><option value="asc">最早优先</option>
          </select>
          <button class="immich-pick-btn immich-close">关闭</button>
        </span>
      </div>
      <div class="immich-pick-body"></div>
      <div class="immich-pick-foot">
        <span class="immich-pick-info">加载中…</span>
        <span>
          <button class="immich-pick-btn immich-prev">上一页</button>
          <button class="immich-pick-btn immich-next">下一页</button>
        </span>
      </div>
    </div>`;
  document.body.appendChild(mask);

  const chipsEl = mask.querySelector(".immich-pick-chips");
  const body = mask.querySelector(".immich-pick-body");
  const info = mask.querySelector(".immich-pick-info");
  const prev = mask.querySelector(".immich-prev");
  const next = mask.querySelector(".immich-next");
  const orderSel = mask.querySelector(".immich-order");

  const selectedId = getWidget(node, "asset_id") || "";
  let page = 1;
  let nextPage = null;
  let lists = { albums: [], people: [], tags: [] };
  // 从节点属性还原（工作流里存的），没有就用会话兜底
  const filterState = readFilters(node);

  const close = () => mask.remove();
  mask.querySelector(".immich-close").onclick = close;
  mask.addEventListener("click", (e) => { if (e.target === mask) close(); });

  function renderChips() {
    chipsEl.innerHTML = "";
    filterState.conditions.forEach((c, i) => {
      const chip = document.createElement("span");
      chip.className = "immich-chip";
      chip.textContent = condText(c, lists);
      const x = document.createElement("b");
      x.textContent = "×";
      x.title = "移除这个条件";
      x.onclick = () => { filterState.conditions.splice(i, 1); writeFilters(node, filterState); renderChips(); loadPage(1); };
      chip.appendChild(x);
      chipsEl.appendChild(chip);
    });
  }

  async function loadPage(p) {
    body.innerHTML = "";
    info.textContent = "加载中…";
    try {
      const d = await getJSON("/search?" + buildQuery(filterState.conditions, filterState.order, p, 24));
      page = d.page || p;
      nextPage = d.nextPage || null;
      filterState.page = page;
      writeFilters(node, filterState);
      prev.disabled = page <= 1;
      next.disabled = !nextPage;
      const items = d.items || [];
      info.textContent = `第 ${page} 页 · ${items.length} 张${nextPage ? "（还有下一页）" : ""}`;
      if (!items.length) {
        body.style.display = "block";
        body.innerHTML = '<div style="opacity:.6;font-size:13px;padding:24px 8px">' +
          '没有符合条件的资产。试试减少条件，或换一个相册 / 人脸。</div>';
        return;
      }
      body.style.display = "grid";
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

  mask.querySelector(".immich-add-cond").onclick = () => {
    openConditionEditor(filterState.conditions, lists, (conds) => {
      filterState.conditions = conds;
      writeFilters(node, filterState);
      renderChips();
      loadPage(1);
    });
  };

  orderSel.value = filterState.order || "desc";
  orderSel.onchange = () => { filterState.order = orderSel.value; writeFilters(node, filterState); loadPage(1); };

  try {
    const [albums, people, tags] = await Promise.all([
      getJSON("/albums").catch(() => []),
      getJSON("/people").catch(() => []),
      getJSON("/tags").catch(() => []),
    ]);
    lists = {
      albums: (albums || []).map((a) => ({ id: a.id, name: a.albumName })),
      people: (people || []).map((p) => ({ id: p.id, name: p.name || "(未命名)" })),
      tags: (tags || []).map((t) => ({ id: t.id, name: t.name })),
    };
    renderChips();
    loadPage(filterState.page || 1);
  } catch (e) {
    info.textContent = "加载失败：" + e.message;
  }

  prev.onclick = () => { if (page > 1) loadPage(page - 1); };
  next.onclick = () => { if (nextPage) loadPage(Number(nextPage)); };
}

// 选完立刻在节点上画缩略图（ui.images 只有执行后才渲染）
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
