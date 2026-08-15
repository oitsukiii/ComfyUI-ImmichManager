// 🐾 ComfyUI-ImmichManager 前端面板
// 功能：工具栏按钮 → 资产时间轴面板（月桶折叠/展开、缩略图懒加载、详情浮层、
// 收藏/删除/描述） + 配置页（连接测试、保存）+ 打开 Immich 网页。
// 后端 API：/api/immich_plus/*（详见 docs/PLAN.md）
(function () {
  "use strict";

  // ── 轻量 i18n（v0.6 P3）──────────────────────────────
  // 优先用独立 web/i18n.js 的 window.ImmichI18n；若加载顺序问题（ComfyUI 扩展
  // js 加载时序不可控）导致未定义，退回最小 fallback（en，原样返回 key）。
  // 语言切换入口：配置页「语言」下拉 → setLang() → localStorage("immich_lang")。
  var I18N = window.ImmichI18n || {
    t: function (key) { return key; },
    setLang: function () {},
    getLang: function () { return "en"; },
    getLangSetting: function () { return "auto"; },
    langs: function () { return ["auto", "en", "zh"]; },
  };
  var t = I18N.t;
  function setLang(l) { I18N.setLang(l); }

  // ComfyUI 的 WEB_DIRECTORY 机制只自动加载 .js 扩展，.css 不会自动注入，
  // 必须由 JS 手动挂 <link>，否则浮窗所有样式（fixed 定位/z-index/遮罩）都不生效，
  // overlay 只是 body 末尾的普通 div，会被 canvas 盖住或落在视口外。
  function injectCss() {
    if (document.getElementById("immich-css")) return;
    var src = "";
    var cur = document.currentScript;
    if (cur && cur.src) src = cur.src;
    else src = "/extensions/ComfyUI-ImmichManager/immich_panel.js";
    var link = document.createElement("link");
    link.id = "immich-css";
    link.rel = "stylesheet";
    link.href = src.replace(/immich_panel\.js$/, "immich_panel.css");
    document.head.appendChild(link);
  }
  injectCss();

  var API = "/api/immich_plus";
  // token 存储：默认 sessionStorage（当前标签页会话，关标签页即失效）；
  // 用户勾选「记住令牌到本机」后存 localStorage（跨会话持久化，需主动选择）。
  var LS_TOKEN = "immich_panel_token";
  var token = sessionStorage.getItem(LS_TOKEN) || localStorage.getItem(LS_TOKEN) || "";
  // 详情页状态记忆（sessionStorage）：开关状态 + 选中资产（老李需求 2026-08-16）
  var LS_DETAIL_OPEN = "immich_detail_open";   // "1" 详情开着 / "0" 用户主动关闭
  var LS_DETAIL_ASSET = "immich_detail_asset"; // 上次选中的资产 id
  // 时间轴状态记忆（sessionStorage，老李需求 2026-08-16）：滚动位置 + 往期月份展开态
  var LS_TIMELINE_SCROLL = "immich_timeline_scroll";     // scrollTop（px）
  var LS_TIMELINE_EXPANDED = "immich_timeline_expanded"; // JSON 数组：展开的月份 key（"2026-8"）+ 本期 "current"

  var state = {
    config: null,       // GET /config 返回的半脱敏配置
    buckets: [],        // 时间桶列表
    currentAsset: null, // 详情浮层当前资产
    assetsById: {},     // 时间线已渲染资产的 {id: asset} 索引（还原详情选中用）
    blobUrls: [],       // 缩略图 blob objectURL，面板重建时统一 revoke
    zoomSize: "preview",// 三挡缩放：thumbnail / preview / original（sessionStorage 持久化）
    videoAbort: null,   // 视频下载 AbortController（关闭详情时中止大视频下载）
    favOnly: false,     // "仅显示收藏"开关（需求 2.6）
    selectMode: false,  // 批量选择模式（v0.6：勾选多个资产批量收藏/删除）
    selectedIds: {},    // 选中资产 id 集合 {id: true}（对象查重 O(1)）
    detailBeforeSelect: null, // 进入选择模式前详情状态快照（退出时还原）
    shiftAnchorCell: null,    // shift 范围选择锚点 cell（资源管理器风格：非 shift 点击重置）
  };

  // 三挡缩放记忆（sessionStorage，不跨会话残留）
  (function () {
    var z = sessionStorage.getItem("immich_zoom");
    if (z === "thumbnail" || z === "preview" || z === "original") state.zoomSize = z;
  })();

  var ZOOM_LEVELS = [
    { key: "thumbnail", labelKey: "zoom.small" },
    { key: "preview", labelKey: "zoom.medium" },
    { key: "original", labelKey: "zoom.large" },
  ];

  // 全屏图标（SVG，仿视频播放器右下角控制条样式）
  var FULLSCREEN_ICON =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>' +
    "</svg>";

  // 爱心图标（SVG）：填充/空心共用同一 path，尺寸完全一致（P2-2）
  var HEART_PATH = "M12 21s-8-4.6-8-10a4.6 4.6 0 0 1 8-3.1A4.6 4.6 0 0 1 20 11c0 5.4-8 10-8 10z";
  function heartIcon(filled) {
    return filled
      ? '<svg class="immich-heart" viewBox="0 0 24 24" width="15" height="15" fill="#ff4d6d" aria-hidden="true"><path d="' + HEART_PATH + '"/></svg>'
      : '<svg class="immich-heart" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="#fff" stroke-width="2" aria-hidden="true"><path d="' + HEART_PATH + '"/></svg>';
  }

  // ─────────────── 基础请求 ───────────────

  function headers(extra) {
    var h = Object.assign({ "Content-Type": "application/json" }, extra || {});
    if (token) h["Authorization"] = "Bearer " + token;
    return h;
  }

  // 统一后端 API 调用（全部走 /api/immich_plus/*，前端不直接碰 Immich）。
  // 401 → 抛 AUTH_REQUIRED（调用方弹令牌输入）；其他错误解析后端 {error} 文案。
  function api(method, path, body) {
    var opt = { method: method, headers: headers() };
    if (body !== undefined) opt.body = JSON.stringify(body);
    return fetch(API + path, opt).then(function (resp) {
      if (resp.status === 401) throw new Error("AUTH_REQUIRED");
      if (!resp.ok) {
        return resp.json().then(function (d) {
          throw new Error((d && d.error) || ("HTTP " + resp.status));
        }).catch(function (e) {
          if (e.message && e.message !== ("HTTP " + resp.status)) throw e;
          throw new Error("HTTP " + resp.status);
        });
      }
      return resp.json();
    });
  }

  // 缩略图加载：fetch 带 Authorization header → blob → objectURL。
  // 不用 <img src=...?token=>：token 进 URL 会泄入访问日志。
  // 懒加载保证只拉可见区域的图，内存峰值可控。
  // size: thumbnail(250px WEBP) / preview(原尺寸 JPEG) / original(原文件，仅图片)
  function loadThumbInto(img, id, size) {
    size = size || "preview";
    // 视频网格缩略图永远用视频帧（preview），不拉原文件当缩略图
    if (size === "original" && img.getAttribute("data-is-video") === "1") size = "preview";
    var path = size === "original"
      ? "/assets/" + encodeURIComponent(id) + "/original"
      : "/assets/" + encodeURIComponent(id) + "/thumbnail?size=" + size;
    var opt = { method: "GET", headers: {} };
    if (token) opt.headers["Authorization"] = "Bearer " + token;
    fetch(API + path, opt)
      .then(function (resp) {
        if (resp.status === 401) throw new Error("AUTH_REQUIRED");
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        state.blobUrls.push(url);
        img.src = url;
        img.onload = function () {
          img.style.opacity = "1";
          var sk = img.parentNode.querySelector(".immich-cell-skeleton");
          if (sk) sk.remove();
        };
      })
      .catch(function (e) {
        var sk = img.parentNode.querySelector(".immich-cell-skeleton");
        if (sk) sk.textContent = e.message === "AUTH_REQUIRED" ? t("error.token.needed") : t("error.load.thumb");
      });
  }

  function revokeBlobs() {
    state.blobUrls.forEach(function (u) { URL.revokeObjectURL(u); });
    state.blobUrls = [];
  }

  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function monthLabel(tb) {
    // timeBucket 形如 "2026-08-01" → "2026年8月"
    var m = /^(\d{4})-(\d{2})/.exec(tb || "");
    if (!m) return esc(tb || "");
    return t("month.format", { y: m[1], m: parseInt(m[2], 10) });
  }

  // ─────────────── 状态提示 ───────────────

  // 消息提示：toast 小窗（需求 P2-6），右下角固定、2s 后自动消失。
  // ok/err/warn 颜色样式沿用原状态条（.immich-toast.ok/.err/.warn）。
  // 可选 action：actionLabel + actionFn（如"去回收站"）；带 action 的 toast 停留 4s，
  // 避免用户来不及点按钮（v0.6 批量删除场景）。
  function showStatus(text, cls, actionLabel, actionFn) {
    var box = document.getElementById("immich-toast-box");
    if (!box) {
      box = document.createElement("div");
      box.id = "immich-toast-box";
      document.body.appendChild(box);
    }
    var t = document.createElement("div");
    t.className = "immich-toast " + (cls || "ok");
    t.textContent = text;
    box.appendChild(t);
    if (actionLabel && actionFn) {
      var btn = document.createElement("button");
      btn.className = "immich-toast-action";
      btn.textContent = actionLabel;
      btn.onclick = function (e) {
        e.stopPropagation();
        t.classList.add("out");
        setTimeout(function () { t.remove(); }, 300);
        actionFn();
      };
      t.appendChild(btn);
    }
    // P3（审小爪）：toast 堆叠上限 5 条，超出移除最旧
    while (box.children.length > 5) box.firstChild.remove();
    if (cls === "err") console.error("[ComfyUI-ImmichManager]", text);
    setTimeout(function () {
      t.classList.add("out");
      setTimeout(function () { t.remove(); }, 300);
    }, actionLabel && actionFn ? 4000 : 2000);
  }

  function clearStatus() {
    var box = document.getElementById("immich-toast-box");
    if (box) box.innerHTML = "";
  }

  // 自定义确认弹窗（v0.6 批量删除/单条删除用）。
  // 不用原生 confirm：原生 confirm 同步阻塞，用户按 Esc 取消时同一个 keydown
  // 会在 confirm 返回后继续冒泡到全局 Esc 处理器，把面板误关（无法可靠拦截）。
  // 自定义弹窗是异步的：_confirming 期间 Esc 由弹窗消费（全局处理器跳过），
  // 弹窗关闭（点按钮/遮罩/Esc）时再重置 _confirming。
  // 返回 Promise<boolean>：true=确认，false=取消。
  // okClass 可选：确认按钮样式类（默认 "danger" 红字红框；清空配置传 "danger-outline" 白字红框与主按钮统一）。
  function confirmDialog(message, okLabel, okClass) {
    return new Promise(function (resolve) {
      var overlay = document.createElement("div");
      overlay.className = "immich-confirm-overlay";
      overlay.innerHTML =
        '<div class="immich-confirm-box">' +
        '  <div class="immich-confirm-msg"></div>' +
        '  <div class="immich-confirm-actions">' +
        '    <button class="immich-btn" data-act="cancel">' + t("confirm.cancel") + "</button>" +
        '    <button class="immich-btn ' + (okClass || "danger") + '" data-act="ok">' + esc(okLabel || t("confirm.ok")) + "</button>" +
        "  </div>" +
        "</div>";
      overlay.querySelector(".immich-confirm-msg").textContent = message;
      function close(result) {
        overlay.remove();
        state._confirming = false;
        resolve(result);
      }
      overlay.querySelector('[data-act="cancel"]').onclick = function () { close(false); };
      overlay.querySelector('[data-act="ok"]').onclick = function () { close(true); };
      overlay.addEventListener("click", function (e) {
        if (e.target === overlay) close(false); // 点遮罩 = 取消
      });
      state._confirming = true;
      document.body.appendChild(overlay);
      var box = overlay.querySelector(".immich-confirm-box");
      var okBtn = overlay.querySelector('[data-act="ok"]');
      if (okBtn) okBtn.focus(); // 焦点进弹窗，Enter 可确认
    });
  }

  // ─────────────── 工具栏按钮 ───────────────

  // 创建工具栏按钮（幂等：重复调用只创建元素，不重复插入）
  function createImmichBtn() {
    var btn = document.createElement("button");
    btn.id = "immich-toolbar-btn";
    btn.title = t("title.panel");
    btn.innerHTML = "🖼️ Immich";
    btn.className = "comfyui-button";
    btn.onclick = togglePanel;
    // 面板正开着时被重绘补回，恢复 active 高亮
    var overlay = document.getElementById("immich-panel-overlay");
    if (overlay && overlay.style.display === "flex") btn.classList.add("active");
    return btn;
  }

  function addToolbarButton() {
    if (document.getElementById("immich-toolbar-btn")) return;
    var actionBar = document.querySelector(".actionbar-container");
    if (!actionBar) { setTimeout(addToolbarButton, 500); return; }
    actionBar.appendChild(createImmichBtn());
    watchToolbar();
  }

  // ComfyUI 0.30 切换「属性面板」等操作会重绘顶部工具栏，把 appendChild 进去的
  // 自定义按钮清掉（只有刷新页面才回来）。MutationObserver 兜底：按钮被移除时
  // 自动补回，用户感知不到。
  var __immichToolbarWatched = false;
  function watchToolbar() {
    if (__immichToolbarWatched) return;
    __immichToolbarWatched = true;
    var observer = new MutationObserver(function () {
      // 延迟一拍再补：避免和 ComfyUI 的重绘/虚拟 DOM 更新竞争导致反复插入
      setTimeout(function () {
        if (document.getElementById("immich-toolbar-btn")) return;
        var actionBar = document.querySelector(".actionbar-container");
        if (!actionBar) return;
        actionBar.appendChild(createImmichBtn());
      }, 60);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  function togglePanel() {
    var overlay = document.getElementById("immich-panel-overlay");
    // 判断可见性而不是存在性：closePanel 只置 display:none 不移除元素，
    // 若按存在性判断，第二次关闭后按钮就永远打不开了
    if (overlay && overlay.style.display !== "none") closePanel();
    else openPanel();
  }

  // ─────────────── 面板骨架 ───────────────

  function ensureSkeleton() {
    if (document.getElementById("immich-panel-overlay")) return;
    var overlay = document.createElement("div");
    overlay.id = "immich-panel-overlay";
    overlay.innerHTML =
      '<div id="immich-panel" data-zoom="' + esc(state.zoomSize) + '">' +
      '  <div id="immich-panel-header">' +
      '    <h2>🖼️ ' + t("title.panel") + "</h2>" +
      '    <button class="immich-btn" id="immich-btn-timeline" title="' + t("btn.timeline.title") + '">' + t("btn.timeline") + "</button>" +
      '    <div id="immich-timeline-nav">' +
      '      <div class="inner">' +
      '        <div class="immich-zoom-group timeline-only" title="' + t("title.zoom") + '">' +
      ZOOM_LEVELS.map(function (z) {
        return '<button class="immich-btn immich-zoom-btn" data-zoom="' + z.key + '" title="' + t("zoom.level.title", { label: t(z.labelKey) }) + '">' + t(z.labelKey) + "</button>";
      }).join("") +
      "        </div>" +
      '        <button class="immich-btn timeline-only" id="immich-btn-select" title="' + t("btn.select.title") + '">' + t("btn.select") + "</button>" +
      '        <button class="immich-btn timeline-only" id="immich-btn-favonly" title="' + t("btn.favonly.title") + '">' + t("btn.favonly") + "</button>" +
      "      </div>" +
      "    </div>" +
      '    <span class="immich-header-sep"></span>' +
      '    <button class="immich-btn" id="immich-btn-config" title="' + t("btn.config.title") + '">' + t("btn.config") + "</button>" +
      '    <span class="immich-header-sep"></span>' +
      '    <button class="immich-btn" id="immich-btn-open" title="' + t("btn.open.title") + '">' + t("btn.open") + "</button>" +
      '    <button class="immich-btn" id="immich-btn-close" title="' + t("btn.close.title") + '">✕</button>' +
      "  </div>" +
      '  <div id="immich-config-bar" style="display:none">' +
      '    <button class="immich-btn" id="immich-btn-test">' + t("cfg.test.btn") + "</button>" +
      '    <button class="immich-btn primary" id="immich-btn-save">' + t("btn.save") + "</button>" +
      '    <button class="immich-btn danger-outline" id="immich-btn-reset">' + t("cfg.reset.btn") + "</button>" +
      "  </div>" +
      '  <div id="immich-bulkbar" style="display:none">' +
      '    <span class="immich-bulk-label">' + t("bulk.label") + "</span>" +
      '    <span id="immich-bulk-count">' + t("bulk.count", { n: 0 }) + "</span>" +
      '    <button class="immich-btn" id="immich-btn-bulk-all" title="' + t("btn.bulk.all.title") + '">' + t("btn.bulk.all") + "</button>" +
      '    <button class="immich-btn" id="immich-btn-bulk-unselect" title="' + t("btn.bulk.unselect.title") + '">' + t("btn.bulk.unselect") + "</button>" +
      '    <button class="immich-btn" id="immich-btn-bulk-fav" title="' + t("btn.bulk.fav.title") + '">' + t("btn.bulk.fav") + "</button>" +
      '    <button class="immich-btn danger-outline" id="immich-btn-bulk-del" title="' + t("btn.bulk.del.title") + '">' + t("btn.bulk.del") + "</button>" +
      '    <button class="immich-btn" id="immich-btn-bulk-clear" title="' + t("btn.bulk.clear.title") + '">' + t("btn.bulk.clear") + "</button>" +
      "  </div>" +
      '  <div id="immich-panel-main">' +
      '    <div id="immich-timeline"></div>' +
      '    <div id="immich-detail-pane"></div>' +
      "  </div>" +
      "</div>";
    document.body.appendChild(overlay);

    document.getElementById("immich-btn-close").onclick = closePanel;
    document.getElementById("immich-btn-timeline").onclick = function () {
      exitSelectMode();
      renderTimeline();
    };
    document.getElementById("immich-btn-select").onclick = function () {
      if (state.selectMode) exitSelectMode();
      else enterSelectMode();
    };
    document.getElementById("immich-btn-open").onclick = function () {
      var base = (state.config && state.config.base_url) || "http://127.0.0.1:2283";
      window.open(base.replace(/\/api\/?$/, ""), "_blank");
    };
    document.getElementById("immich-btn-config").onclick = function () {
      exitSelectMode();
      renderConfig();
    };
    // 配置操作栏（顶栏下方横条，复用批量栏样式）：测试连接 / 保存 / 清空
    document.getElementById("immich-btn-test").onclick = testConnection;
    document.getElementById("immich-btn-save").onclick = saveConfig;
    document.getElementById("immich-btn-reset").onclick = resetConfig;
    // "仅显示收藏"开关（需求 2.6）：切换后按现有时间大组/小组样式只显示收藏项
    document.getElementById("immich-btn-favonly").onclick = function () {
      state.favOnly = !state.favOnly;
      updateFavOnlyButton();
      refreshAll();
    };
    // 批量操作栏（v0.6：选择模式下的批量收藏/删除；老李需求 2026-08-16：
    // 「取消选择」清空选中保留选择模式，原「取消」改「✕」退出选择模式）
    document.getElementById("immich-btn-bulk-all").onclick = bulkSelectAll;
    document.getElementById("immich-btn-bulk-fav").onclick = bulkFavorite;
    document.getElementById("immich-btn-bulk-del").onclick = bulkDelete;
    document.getElementById("immich-btn-bulk-unselect").onclick = clearSelection;
    document.getElementById("immich-btn-bulk-clear").onclick = exitSelectMode;
    updateFavOnlyButton();
    // 三挡缩放切换（分辨率 + 显示大小联动）
    overlay.querySelectorAll(".immich-zoom-btn").forEach(function (btn) {
      btn.onclick = function () {
        var z = btn.getAttribute("data-zoom");
        if (z === state.zoomSize) return;
        state.zoomSize = z;
        sessionStorage.setItem("immich_zoom", z);
        updateZoomButtons();
        var panel = document.getElementById("immich-panel");
        if (panel) panel.setAttribute("data-zoom", z);
        refreshAll(); // 重拉缩略图（懒加载只拉可见区，挡位切换成本可控）
      };
    });
    updateZoomButtons();
    overlay.addEventListener("mousedown", function (e) {
      if (e.target === overlay) closePanel();
    });
    // 时间轴滚动位置记忆（防抖保存，老李需求 2026-08-16）——ensureSkeleton 只执行一次
    var tlEl = document.getElementById("immich-timeline");
    if (tlEl) tlEl.addEventListener("scroll", scheduleSaveTimelineScroll);
  }

  // 视图模式切换：header 当前页按钮 active 提示 + 配置操作栏显隐 +
  // 时间线导航组滑动动画（纯 CSS transition）——切配置时「时间线+小中大+选择+
  // 只看收藏」四件套一起向右滑：时间线按钮 margin 滑一小段，三按钮 transform 滑出，
  // nav 容器 max-width 同步收窄，h2(flex:1) 推动时间线按钮平滑滑到最终位置停住
  // （不被遮住）；缩放/选择/只看收藏 在分隔线处被裁剪 + 渐变羽化；切回时反向。
  function setViewMode(mode) {
    var overlay = document.getElementById("immich-panel-overlay");
    if (!overlay) return;
    var isConfig = mode === "config";
    var tl = document.getElementById("immich-btn-timeline");
    var cfg = document.getElementById("immich-btn-config");
    if (tl) tl.classList.toggle("active", !isConfig);
    if (cfg) cfg.classList.toggle("active", isConfig);
    var bar = document.getElementById("immich-config-bar");
    if (bar) bar.style.display = isConfig ? "flex" : "none";
    var header = document.getElementById("immich-panel-header");
    if (header) header.classList.toggle("config-mode", isConfig);
  }

  function updateZoomButtons() {
    var overlay = document.getElementById("immich-panel-overlay");
    if (!overlay) return;
    overlay.querySelectorAll(".immich-zoom-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-zoom") === state.zoomSize);
    });
  }

  function updateFavOnlyButton() {
    var overlay = document.getElementById("immich-panel-overlay");
    if (!overlay) return;
    var btn = document.getElementById("immich-btn-favonly");
    if (!btn) return;
    btn.classList.toggle("active", state.favOnly);
    btn.textContent = state.favOnly ? t("btn.favonly.active") : t("btn.favonly");
  }

  // ─────────────── 批量选择模式（v0.6） ───────────────

  function enterSelectMode() {
    state.selectMode = true;
    state.selectedIds = {};
    state.shiftAnchorCell = null; // 进入选择模式清 shift 锚
    // 进入选择模式前快照详情状态（退出时还原，老李需求 2026-08-16）
    // 存 id 而非对象引用：批量删除后引用会指向已删资产（P2，审小爪 2026-08-16）
    var pane = document.getElementById("immich-detail-pane");
    state.detailBeforeSelect = {
      visible: !!pane && pane.style.display !== "none",
      assetId: (state.currentAsset && state.currentAsset.id) || null,
    };
    closeDetail(); // 选择模式专注勾选，不保留详情
    updateSelectUI();
    showStatus(t("select.mode.hint"), "warn");
  }

  function exitSelectMode() {
    state.selectMode = false;
    state.selectedIds = {};
    state.shiftAnchorCell = null; // 退出选择模式清 shift 锚
    var saved = state.detailBeforeSelect;
    state.detailBeforeSelect = null;
    updateSelectUI();
    // 还原进入选择模式前的详情状态（开着资产 → 恢复；开着占位 → 占位；关闭 → 关闭）
    if (saved) {
      if (saved.assetId) {
        var asset = state.assetsById[saved.assetId];
        if (asset) openDetail(asset);
        else showDetailPlaceholder(); // 批量删除等：资产已不在时间线 → 占位
      } else if (saved.visible) showDetailPlaceholder();
      else closeDetail();
    }
  }

  function toggleSelect(id) {
    if (!id) return;
    if (state.selectedIds[id]) delete state.selectedIds[id];
    else state.selectedIds[id] = true;
    updateSelectUI();
  }

  // shift 范围选择（资源管理器风格）：从 shift 锚点 cell 到当前点击 cell 之间
  // 的所有已渲染 cell 全部选中（追加，不清已有选择）；锚点由非 shift 点击重置
  function shiftSelect(cell) {
    var cells = Array.prototype.slice.call(document.querySelectorAll("#immich-timeline .immich-cell"));
    var cur = cells.indexOf(cell);
    if (cur < 0) return;
    if (!state.shiftAnchorCell) state.shiftAnchorCell = cell; // 首个 shift：以当前为锚起点，后续保持
    var anchor = cells.indexOf(state.shiftAnchorCell);
    if (anchor < 0) anchor = cur;
    var lo = Math.min(anchor, cur), hi = Math.max(anchor, cur);
    for (var i = lo; i <= hi; i++) {
      var id = cells[i].getAttribute("data-cell-id");
      if (id) state.selectedIds[id] = true;
    }
    updateSelectUI();
  }

  // 全选：当前时间轴已加载（DOM 已渲染）的所有资产。
  // 折叠的月份没有 DOM，不参与；已渲染的含本期/溢出/已展开月份。
  function bulkSelectAll() {
    if (!state.selectMode) return;
    var ids = {};
    document.querySelectorAll("#immich-timeline .immich-cell").forEach(function (c) {
      var id = c.getAttribute("data-cell-id");
      if (id) ids[id] = true;
    });
    state.selectedIds = ids;
    updateSelectUI();
  }

  // 同步时间轴每个 cell 的选中态（老李需求 2026-08-16）：
  // 选择模式 → selectedIds 勾选的；普通模式 → 详情当前资产（黄框，无复选框）
  function syncCellSelectedState() {
    document.querySelectorAll("#immich-timeline .immich-cell").forEach(function (c) {
      var id = c.getAttribute("data-cell-id");
      var sel = !!(id && (
        (state.selectMode && state.selectedIds[id]) ||
        (!state.selectMode && state.currentAsset && state.currentAsset.id === id)
      ));
      c.classList.toggle("selected", sel);
    });
  }

  // 同步选择模式 UI：面板 class、header 选择按钮态、bulkbar 显隐与计数、cell 选中态
  function updateSelectUI() {
    var overlay = document.getElementById("immich-panel-overlay");
    if (!overlay) return;
    overlay.classList.toggle("select-mode", state.selectMode);
    var selBtn = document.getElementById("immich-btn-select");
    if (selBtn) {
      selBtn.classList.toggle("active", state.selectMode);
      // 老李 2026-08-16：进入选择模式后标题栏按钮保持「✅ 选择」文本，只用黄色边框高亮；
      // ✕ 退出只在批量操作栏（bulkbar）里
      selBtn.textContent = t("btn.select");
      selBtn.title = state.selectMode ? t("btn.select.title.active") : t("btn.select.title");
    }
    var bar = document.getElementById("immich-bulkbar");
    var count = Object.keys(state.selectedIds).length;
    if (bar) {
      bar.style.display = state.selectMode ? "flex" : "none";
      var cnt = document.getElementById("immich-bulk-count");
      if (cnt) cnt.textContent = t("bulk.count", { n: count });
      var favBtn = document.getElementById("immich-btn-bulk-fav");
      if (favBtn) favBtn.disabled = count === 0;
      var delBtn = document.getElementById("immich-btn-bulk-del");
      if (delBtn) delBtn.disabled = count === 0;
    }
    syncCellSelectedState();
  }

  // 取消选择：清空当前选中，但保留在选择模式（老李需求 2026-08-16）
  function clearSelection() {
    if (!state.selectMode) return;
    state.selectedIds = {};
    updateSelectUI();
  }

  // 选中所有资产判断：当前时间轴已渲染的全部 cell（全选/逐点都算；折叠月份无 DOM 不参与）。
  // 集合相等判断（审小爪 P2 2026-08-16）：数量相等 + 逐 cell 比对 data-cell-id，
  // 防「数量巧合相等」误判——折叠月份移除 cell 时 selectedIds 有残留（既有设计），
  // 若只比数量，展开/折叠组合可能让选中数恰好等于可见数，误弹全选警告。
  function isAllAssetsSelected() {
    var cells = document.querySelectorAll("#immich-timeline .immich-cell");
    if (!cells.length) return false;
    var ids = Object.keys(state.selectedIds);
    if (ids.length !== cells.length) return false;
    for (var i = 0; i < cells.length; i++) {
      var id = cells[i].getAttribute("data-cell-id");
      if (!id || !state.selectedIds[id]) return false;
    }
    return true;
  }

  // 批量收藏（set 语义：全部置为收藏，非 toggle）
  function bulkFavorite() {
    var ids = Object.keys(state.selectedIds);
    if (!ids.length) return;
    // 全选二级确认（老李需求 2026-08-16）：选中所有资产时提示，防误操作。
    // 收藏非破坏性，确认按钮用 primary（琥珀）区分删除的 danger（审小爪 P3）
    if (isAllAssetsSelected()) {
      confirmDialog(t("bulk.all.confirm.fav", { n: ids.length }), t("btn.bulk.fav"), "primary").then(function (confirmed) {
        if (confirmed) doBulkFavorite(ids);
      });
      return;
    }
    doBulkFavorite(ids);
  }

  function doBulkFavorite(ids) {
    var btn = document.getElementById("immich-btn-bulk-fav");
    if (btn) btn.disabled = true;
    api("PUT", "/assets", { ids: ids, isFavorite: true }).then(function (r) {
      var updated = (r && r.updated) || 0;
      var failed = (r && r.failed) || [];
      var idsSet = {};
      ids.forEach(function (i) { idsSet[i] = true; });
      failed.forEach(function (f) { delete idsSet[f]; });
      // 同步 cell 爱心态（未失败的置为已收藏）
      Object.keys(idsSet).forEach(function (id) {
        document.querySelectorAll('[data-cell-id="' + CSS.escape(id) + '"]').forEach(function (c) {
          c.classList.add("fav");
          var heart = c.querySelector(".immich-cell-fav");
          if (heart) {
            heart.innerHTML = heartIcon(true);
            heart.title = t("fav.title.remove");
          }
        });
      });
      var msg = t("bulk.fav.done", { n: updated });
      if (failed.length) msg += t("bulk.fav.failed", { n: failed.length });
      showStatus(msg, failed.length ? "warn" : "ok");
      state.selectedIds = {};
      updateSelectUI();
    }).catch(function (e) {
      showStatus(t("bulk.fav.fail", { msg: e.message }), "err");
      updateSelectUI();
    });
  }

  // 批量删除：二次确认（破坏性动词 + 数量 + 可恢复），删除进回收站
  // 全选时替换为全选警告（老李需求 2026-08-16）
  function bulkDelete() {
    var ids = Object.keys(state.selectedIds);
    if (!ids.length) return;
    var msg = isAllAssetsSelected()
      ? t("bulk.all.confirm.del", { n: ids.length })
      : t("bulk.del.confirm", { n: ids.length });
    confirmDialog(
      msg,
      t("btn.del.confirm")
    ).then(function (confirmed) {
      if (!confirmed) return;
      var btn = document.getElementById("immich-btn-bulk-del");
      if (btn) btn.disabled = true;
      api("DELETE", "/assets", { ids: ids, force: false }).then(function () {
        state.selectedIds = {};
        refreshAll();
        updateSelectUI(); // 重置批量栏计数/按钮态（refreshAll 是异步重建 DOM）
        showStatus(t("bulk.del.done", { n: ids.length }), "ok");
      }).catch(function (e) {
        showStatus(t("bulk.del.fail", { msg: e.message }), "err");
        updateSelectUI();
      });
    });
  }

  function openPanel() {
    ensureSkeleton();
    var overlay = document.getElementById("immich-panel-overlay");
    overlay.style.display = "flex";
    var btn = document.getElementById("immich-toolbar-btn");
    if (btn) btn.classList.add("active");
    renderTimeline();
  }

  function closePanel() {
    viewToken++; // 关面板：在跑的异步链作废，避免迟到回调操作隐藏面板
    // P2（审小爪）：关面板前清理详情内容——视频 loop 会后台无限循环播放，
    // blob 不回收；复用 closeDetail 的 abort+pause+revoke
    closeDetail();
    // 关面板：丢弃选择模式快照（不还原详情，下次打开按 sessionStorage 记忆还原）
    state.detailBeforeSelect = null;
    exitSelectMode(); // v0.6：关面板退出选择模式，避免下次打开残留勾选态
    var overlay = document.getElementById("immich-panel-overlay");
    if (overlay) overlay.style.display = "none";
    var btn = document.getElementById("immich-toolbar-btn");
    if (btn) btn.classList.remove("active");
  }

  // ─────────────── 时间轴视图 ───────────────

  // 视图代际令牌：renderTimeline / renderConfig 共用 #immich-timeline 容器，
  // 切换视图时 ++viewToken 使旧异步链失效（每个回调先比对令牌），防止
  // 加载成功/失败的迟到回调覆盖当前视图（修复：加载资产中切到配置页，
  // 失败提醒强制覆盖回时间轴的问题）。
  var viewToken = 0;

  // 大组间隔（需求 8/10：本期范围由配置 timeline_range 控制）
  var RANGE_DAYS = { today: 0, "3d": 3, "7d": 7 };
  var RANGE_LABEL = { today: "range.today", "3d": "range.3d", "7d": "range.7d" };
  // 小组间隔（需求 8：组内按 timeline_interval 分块）
  var INTERVAL_MS = { "15m": 15 * 60 * 1000, "30m": 30 * 60 * 1000, "1h": 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000 };

  // 渲染时间轴视图：拉桶列表 → 分桶渲染 → 还原滚动/展开记忆。
  // viewToken 用于废弃过期异步结果（快速切换视图时旧请求不覆盖新 DOM）。
  function renderTimeline() {
    var token = ++viewToken;
    setViewMode("timeline");
    clearStatus();
    revokeBlobs(); // 旧缩略图 blob 统一回收
    var timeline = document.getElementById("immich-timeline");
    timeline.innerHTML = '<div class="immich-loading">' + t("loading.config") + "</div>";
    // 详情 pane 显隐先按记忆快速还原（内容等数据加载完 restoreDetailState 完整还原）
    var pane0 = document.getElementById("immich-detail-pane");
    if (pane0) pane0.style.display = (sessionStorage.getItem(LS_DETAIL_OPEN) === "0") ? "none" : "flex";
    state.assetsById = {}; // 重建资产索引（时间线刷新后旧对象作废）

    // 先取配置（含 timeline_range / timeline_interval / base_url），再取时间桶
    api("GET", "/config").then(function (cfg) {
      if (token !== viewToken) return;
      state.config = cfg;
      timeline.innerHTML = '<div class="immich-loading">' + t("loading.timeline") + "</div>";
      return api("GET", "/buckets?order=desc");
    }).then(function (buckets) {
      if (token !== viewToken) return;
      state.buckets = Array.isArray(buckets) ? buckets : [];
      if (!state.buckets.length) {
        timeline.innerHTML = '<div class="immich-empty">' + t("empty.noassets") + "</div>";
        restoreDetailState(); // 无资产也还原详情显隐（关闭态隐藏，其他显示占位）
        return;
      }
      timeline.innerHTML = "";

      // 本期起点：由 timeline_range 决定（今天 0 点 / 3 天前 / 7 天前）
      var rangeKey = (state.config && state.config.timeline_range) || "today";
      var rangeDays = RANGE_DAYS[rangeKey] != null ? RANGE_DAYS[rangeKey] : 0;
      var now = new Date();
      var periodStart = new Date(now.getFullYear(), now.getMonth(), now.getDate() - rangeDays, 0, 0, 0, 0);

      // 拆分本期桶（覆盖 periodStart 的桶）与往期桶
      var periodBuckets = [], pastBuckets = [];
      state.buckets.forEach(function (b) {
        var y = parseInt(String(b.timeBucket).slice(0, 4), 10);
        var m = parseInt(String(b.timeBucket).slice(5, 7), 10);
        // 桶的结束时刻 = 下月 1 号 0 点
        var end = new Date(y, m, 1, 0, 0, 0, 0);
        if (end.getTime() > periodStart.getTime()) periodBuckets.push(b);
        else pastBuckets.push(b);
      });

      // 本期桶拉资产（跨月边界可能 2 个桶，Promise.all 合并）
      var fetches = periodBuckets.map(function (b) {
        return api("GET", "/bucket?timeBucket=" + encodeURIComponent(b.timeBucket) + "&order=desc").then(function (assets) {
          return { tb: b.timeBucket, assets: assets };
        });
      });
      return Promise.all(fetches).then(function (results) {
        if (token !== viewToken) return;
        var merged = [];
        results.forEach(function (r) { merged = merged.concat(r.assets || []); });

        // P1 修复（审小爪）：跨界桶拆分——periodStart 落在上月中间时，
        // 上月 1 号~periodStart-1 的资产会被本期 filter 掉，但桶已不在 pastBuckets，
        // 导致"既不在本期也不在往期"彻底不可见。把溢出资产按月份重组为往期折叠块。
        // 需求 2.6：favOnly 开启时，本期与溢出都只保留收藏项。
        var fav = function (a) { return !state.favOnly || a.isFavorite; };
        var inPeriod = [], overflow = [];
        merged.forEach(function (a) {
          var ts = Date.parse(a.fileCreatedAt || a.createdAt);
          if (fav(a) && !isNaN(ts) && ts >= periodStart.getTime()) inPeriod.push(a);
          else if (fav(a)) overflow.push(a);
        });

        timeline.appendChild(renderPeriodSection(rangeKey, periodStart, inPeriod));

        // 溢出资产按月份折叠（资产已随整桶拉回，无需额外请求）
        var overflowByMonth = {};
        overflow.forEach(function (a) {
          var ts = Date.parse(a.fileCreatedAt || a.createdAt);
          var d = isNaN(ts) ? null : new Date(ts);
          var mk = d
            ? d.getFullYear() + "-" + ((d.getMonth() + 1) < 10 ? "0" : "") + (d.getMonth() + 1)
            : "_unknown";
          (overflowByMonth[mk] = overflowByMonth[mk] || []).push(a);
        });
        Object.keys(overflowByMonth).sort().reverse().forEach(function (mk) {
          if (mk === "_unknown") return;
          var assets = overflowByMonth[mk];
          assets.sort(function (a, b) {
            return Date.parse(b.fileCreatedAt || b.createdAt) - Date.parse(a.fileCreatedAt || a.createdAt);
          });
          var sec = renderMonth({ timeBucket: mk + "-01", count: assets.length });
          sec._assets = assets; // 预加载：展开时直接显示，不重复请求
          if (getExpandedMonths().indexOf(sec._mk) >= 0) expandMonth(sec); // 记忆展开态还原
          timeline.appendChild(sec);
        });

        // 异步展开月份（pastBuckets）完成前不还原滚动位置（P2-1 审小爪 2026-08-16）
        var restorePromises = [];
        pastBuckets.forEach(function (b) {
          var sec = renderMonth(b);
          if (getExpandedMonths().indexOf(sec._mk) >= 0) restorePromises.push(expandMonth(sec)); // 记忆展开态还原（拉取月份资产）
          timeline.appendChild(sec);
        });
        // 时间线渲染完成后还原详情状态（选中资产在 assetsById 里则恢复，否则占位/保持关闭）
        restoreDetailState();
        // 滚动位置还原（审小爪 P2-1 2026-08-16）：等所有异步展开月份加载完再设 scrollTop，
        // 否则内容未撑开时被浏览器 clamp 到旧高度；溢出月份同步展开已含在 Promise 里
        Promise.all(restorePromises).then(function () {
          if (token !== viewToken) return; // 视图已切走（如配置页），放弃还原
          restoreTimelineState();
        });
      });
    }).catch(function (e) {
      // 视图已切走（如配置页）：失败提醒不覆盖当前视图，只放弃
      if (token !== viewToken) return;
      if (e.message === "AUTH_REQUIRED") {
        timeline.innerHTML = '<div class="immich-empty">' + t("error.auth") + "</div>" +
          '<div style="text-align:center"><button class="immich-btn primary" onclick="document.getElementById(\'immich-btn-config\').click()">' + t("error.auth.btn") + "</button></div>";
        showStatus(t("error.auth.status"), "warn");
      } else {
        timeline.innerHTML = '<div class="immich-empty">' + t("error.load") + "</div>";
        showStatus(t("error.load.fail", { msg: e.message }), "err");
      }
      // 加载失败也还原详情显隐（关闭态保持隐藏，其他显示占位）
      restoreDetailState();
    });
  }

  // 本期大组：组内按 timeline_interval 分块（15m/30m/1h/1d）。
  // head 可点击折叠/展开（P2-1 审小爪：原版只有 ▼ 无交互）。
  function renderPeriodSection(rangeKey, periodStart, inPeriod) {
    var sec = document.createElement("div");
    sec.className = "immich-month is-current";
    // 本期默认展开；记忆里 "current" = 用户折叠过 → 初始折叠（老李需求 2026-08-16）
    sec._expanded = getExpandedMonths().indexOf("current") < 0;
    sec._assets = inPeriod;

    sec.innerHTML =
      '<div class="immich-month-head">' +
      '  <span class="immich-month-title">' + t(RANGE_LABEL[rangeKey] || "range.current") + '</span>' +
      '  <span class="immich-month-count">' + t("count.unit", { n: inPeriod ? inPeriod.length : 0 }) + "</span>" +
      '  <span class="immich-month-arrow" data-role="arrow">' + (sec._expanded ? "▼" : "▶") + "</span>" +
      "</div>";

    var head = sec.querySelector(".immich-month-head");
    head.onclick = function () {
      if (sec._expanded) collapsePeriod(sec);
      else expandPeriod(sec);
    };
    if (sec._expanded) buildPeriodGroups(sec, inPeriod);
    return sec;
  }

  function expandPeriod(sec) {
    if (sec._expanded) return;
    sec._expanded = true;
    var arrow = sec.querySelector('[data-role="arrow"]');
    if (arrow) arrow.textContent = "▼";
    setMonthExpanded("current", false); // 本期默认展开："current" 在集合 = 折叠标记，展开时移除
    buildPeriodGroups(sec, sec._assets);
  }

  function collapsePeriod(sec) {
    sec._expanded = false;
    var arrow = sec.querySelector('[data-role="arrow"]');
    if (arrow) arrow.textContent = "▶";
    setMonthExpanded("current", true); // 本期折叠 → 集合加 "current" 标记
    sec.querySelectorAll(".immich-interval, .immich-empty").forEach(function (el) { el.remove(); });
  }

  function buildPeriodGroups(sec, assets) {
    // 小组分块：同一 interval 桶内的资产放一块，标题显示块起始时间
    var intervalKey = (state.config && state.config.timeline_interval) || "1h";
    var intervalMs = INTERVAL_MS[intervalKey] || 60 * 60 * 1000;
    var groups = [];
    var byKey = {};
    (assets || []).forEach(function (a) {
      var ts = Date.parse(a.fileCreatedAt || a.createdAt);
      if (isNaN(ts)) return;
      var k = Math.floor(ts / intervalMs) * intervalMs;
      if (!byKey[k]) { byKey[k] = []; groups.push(k); }
      byKey[k].push(a);
    });
    groups.sort(function (a, b) { return b - a; });

    groups.forEach(function (startTs) {
      var g = document.createElement("div");
      g.className = "immich-interval";
      var head = document.createElement("div");
      head.className = "immich-interval-head";
      head.textContent = fmtInterval(startTs, intervalMs);
      g.appendChild(head);
      var grid = document.createElement("div");
      grid.className = "immich-grid";
      byKey[startTs].forEach(function (a) { grid.appendChild(renderCell(a)); });
      g.appendChild(grid);
      sec.appendChild(g);
      lazyLoad(grid);
    });

    if (!groups.length) {
      var empty = document.createElement("div");
      empty.className = "immich-empty";
      empty.textContent = t("empty.current");
      sec.appendChild(empty);
    }
  }

  // 小组块标题：1d 显示 "2025/8/1"，更细粒度显示 "2025/8/1 10:00"（需求 8 组内按小时）
  function fmtInterval(ts, intervalMs) {
    var d = new Date(ts);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    var date = d.getFullYear() + "/" + (d.getMonth() + 1) + "/" + d.getDate();
    if (intervalMs >= 24 * 60 * 60 * 1000) return date;
    return date + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function renderMonth(bucket) {
    var tb = bucket.timeBucket;
    var sec = document.createElement("div");
    sec.className = "immich-month";
    var count = bucket.count != null ? bucket.count : "";
    sec._bucket = bucket;
    sec._mk = monthExpandKey(tb); // 展开态记忆 key（"2026-8"）

    sec.innerHTML =
      '<div class="immich-month-head">' +
      '  <span class="immich-month-title">' + monthLabel(tb) + '</span>' +
      '  <span class="immich-month-count" data-role="count">' + t("count.unit", { n: count }) + "</span>" +
      '  <span class="immich-month-arrow" data-role="arrow">▶</span>' +
      "</div>";
    sec._assets = null;
    sec._expanded = false;

    var head = sec.querySelector(".immich-month-head");
    head.onclick = function () {
      if (sec._expanded) collapseMonth(sec);
      else expandMonth(sec);
    };
    return sec;
  }

  function expandMonth(sec) {
    if (sec._expanded) return Promise.resolve();
    sec._expanded = true;
    var arrow = sec.querySelector('[data-role="arrow"]');
    if (arrow) arrow.textContent = "▼";
    setMonthExpanded(sec._mk, true); // 往期月份展开态记忆

    // 已加载过 → 直接显示（同步完成）
    if (sec._assets) {
      showMonthAssets(sec, sec._assets);
      return Promise.resolve();
    }
    var grid = document.createElement("div");
    grid.className = "immich-grid";
    grid.innerHTML = '<div class="immich-loading">' + t("loading.timeline") + "</div>";
    sec.appendChild(grid);

    var tb = sec._bucket && sec._bucket.timeBucket;
    // 返回 Promise：renderTimeline 等所有异步展开完成后再还原滚动位置（审小爪 P2-1 2026-08-16，
    // 否则内容未撑开时 scrollTop 被 clamp 到旧高度，fetch 完成内容变高后回不到记忆位置）
    return api("GET", "/bucket?timeBucket=" + encodeURIComponent(tb) + "&order=desc").then(function (assets) {
      // P1-1a（审小爪）：加载期间用户已收起 → 不渲染，避免"箭头 ▶ 但内容弹回"
      if (!sec._expanded) return;
      sec._assets = assets;
      showMonthAssets(sec, assets);
    }).catch(function (e) {
      if (!sec._expanded) return;
      grid.innerHTML = '<div class="immich-empty">' + t("error.load.fail", { msg: esc(e.message) }) + "</div>";
      if (e.message === "AUTH_REQUIRED") showStatus(t("error.auth.status"), "warn");
    });
  }

  function collapseMonth(sec) {
    sec._expanded = false;
    var arrow = sec.querySelector('[data-role="arrow"]');
    if (arrow) arrow.textContent = "▶";
    setMonthExpanded(sec._mk, false); // 往期月份折叠态记忆
    // 移除整个月内主体（含所有天分组/空提示），不能只删第一个 .immich-grid
    // P1-1b/1c（审小爪）：统一清 body + 加载占位 + empty，防止竞态残留
    var body = sec.querySelector(".immich-month-body");
    if (body) body.remove();
    var empty = sec.querySelector(".immich-empty");
    if (empty) empty.remove();
    // 兜底：加载中的占位 grid（直接挂在 month 下，尚未包进 body）
    var grid = sec.querySelector(":scope > .immich-grid");
    if (grid) grid.remove();
    var countEl = sec.querySelector('[data-role="count"]');
    if (countEl && sec._bucket && sec._bucket.count != null) {
      countEl.textContent = t("count.unit", { n: sec._bucket.count });
    }
  }

  // 往期月份展开：第一层=年月（月份折叠头），第二层=天（月内按天分组，固定间隔，
  // 不跟随面板 timeline_interval 参数——需求 2.4）。天分组只显示标题，不额外加框。
  function showMonthAssets(sec, assets) {
    // 移除旧内容：body（天分组）/ 加载占位 grid / empty（P1-1b 审小爪：
    // 竞态残留时旧 body 必须整体清掉，不能只删第一个 .immich-grid）
    var oldBody = sec.querySelector(".immich-month-body");
    if (oldBody) oldBody.remove();
    var oldEmpty = sec.querySelector(".immich-empty");
    if (oldEmpty) oldEmpty.remove();
    var oldGrid = sec.querySelector(":scope > .immich-grid");
    if (oldGrid) oldGrid.remove();
    var fav = function (a) { return !state.favOnly || a.isFavorite; };
    var list = (assets || []).filter(fav);
    var countEl = sec.querySelector('[data-role="count"]');
    if (countEl) countEl.textContent = t("month.loaded", { n: list.length });
    if (!list.length) {
      var empty = document.createElement("div");
      empty.className = "immich-empty";
      empty.textContent = state.favOnly ? t("empty.month.fav") : t("empty.month");
      sec.appendChild(empty);
      return;
    }
    // 按天分组（本地日界线）：同日资产放一块，标题 "2026/8/3"。
    // 统一包在 .immich-month-body 里，折叠时整个移除（修复点月标题收起的是天的 bug）
    var body = document.createElement("div");
    body.className = "immich-month-body";
    var byDay = {};
    var days = [];
    list.forEach(function (a) {
      var d = new Date(a.fileCreatedAt || a.createdAt);
      if (isNaN(d.getTime())) return;
      var k = d.getFullYear() + "/" + (d.getMonth() + 1) + "/" + d.getDate();
      if (!byDay[k]) { byDay[k] = []; days.push(k); }
      byDay[k].push(a);
    });
    days.sort(function (x, y) { return y.localeCompare(x); });
    days.forEach(function (dayKey) {
      var g = document.createElement("div");
      g.className = "immich-interval";
      var head = document.createElement("div");
      head.className = "immich-interval-head";
      head.textContent = dayKey;
      g.appendChild(head);
      var grid = document.createElement("div");
      grid.className = "immich-grid";
      byDay[dayKey].forEach(function (a) { grid.appendChild(renderCell(a)); });
      g.appendChild(grid);
      body.appendChild(g);
      lazyLoad(grid);
    });
    sec.appendChild(body);
  }

  // 视频判断：isImage 是 Immich 权威信号（false=视频，含 3gp/mts 等非白名单格式），
  // ext 白名单/duration 只在 isImage 缺失时兜底。
  function isVideoAsset(asset) {
    if (!asset) return false;
    if (asset.isImage === false) return true;
    var e = String(asset.ext || "").toLowerCase();
    if (e) return e === "mp4" || e === "mov" || e === "webm" || e === "mkv" || e === "m4v" || e === "avi";
    if (asset.duration && /^\d+:\d{2}/.test(String(asset.duration)) && asset.duration !== "0:00:00.00000") return true;
    return false;
  }

  // 格式角标文案：ext 优先，fallback 大类
  function assetBadge(asset) {
    if (!asset) return "";
    if (asset.ext) return String(asset.ext).toUpperCase();
    return isVideoAsset(asset) ? "MP4" : "IMG";
  }

  function renderCell(asset) {
    var cell = document.createElement("div");
    var isVideo = isVideoAsset(asset);
    cell.className = "immich-cell" + (asset.isFavorite ? " fav" : "") + (isVideo ? " is-video" : "");
    cell.setAttribute("data-cell-id", asset.id || "");
    // 资产索引（详情状态还原：restoreDetailState 按记忆 id 找回完整对象）
    if (asset.id) state.assetsById[asset.id] = asset;
    // 渲染时恢复选中态（刷新/折叠重渲染后不丢）：选择模式勾选 或 普通模式详情当前资产
    if (state.selectMode) {
      if (state.selectedIds[asset.id]) cell.classList.add("selected");
    } else if (state.currentAsset && state.currentAsset.id === asset.id) {
      cell.classList.add("selected");
    }
    cell.innerHTML =
      '<span class="immich-cell-check" title="' + t("btn.bulk.clear.title") + '">✓</span>' +
      '<div class="immich-cell-skeleton">' + t("loading.thumb") + "</div>" +
      '<img data-asset-id="' + esc(asset.id || "") + '" data-is-video="' + (isVideo ? "1" : "0") + '" alt="" loading="lazy" style="opacity:0">' +
      '<button class="immich-cell-fav" title="' + (asset.isFavorite ? t("fav.title.remove") : t("fav.title.add")) + '">' + heartIcon(asset.isFavorite) + "</button>" +
      '<span class="immich-cell-badge">' + esc(assetBadge(asset)) + "</span>" +
      '<div class="immich-cell-date">' + fmtDate(asset.fileCreatedAt || asset.createdAt) + "</div>";
    // v0.6：选择模式下点击缩略图 = 切换勾选（不进详情）；普通模式 = 打开详情
    // v0.6 老李需求 2026-08-16：shift 范围多选（普通模式自动进入选择状态）
    // 资源管理器直觉（审小爪 P2-2 2026-08-16）：普通模式先点 A 再看 C 之前 shift 点 C
    // → 范围起点 = 进入选择模式前最后普通点击的 cell（A），即 [A..C]
    // 老李 2026-08-16：去掉 ctrl/meta 多选（ctrl+点选不再进入选择模式）
    cell.onclick = function (e) {
      if (e.shiftKey) {
        if (!state.selectMode) {
          var prevAnchor = state.shiftAnchorCell; // 进入选择模式前的普通点击锚
          enterSelectMode(); // 内部清锚 + 快照详情
          if (prevAnchor) state.shiftAnchorCell = prevAnchor; // 恢复为 shift 范围起点
        }
        shiftSelect(cell);
        return;
      }
      if (state.selectMode) {
        toggleSelect(asset.id);
        state.shiftAnchorCell = cell; // 选择模式普通点击 = 切换勾选 + 更新 shift 锚
      } else {
        openDetail(asset);
        state.shiftAnchorCell = cell; // 普通点击更新 shift 锚（下次 shift 从这开始）
      }
    };
    cell.querySelector(".immich-cell-check").onclick = function (e) {
      e.stopPropagation();
      toggleSelect(asset.id);
    };
    // 需求 2.5：缩略图右上角爱心，点击收藏（stopPropagation 防止触发 cell 打开详情）
    cell.querySelector(".immich-cell-fav").onclick = function (e) {
      e.stopPropagation();
      if (state.selectMode) return; // 选择模式下爱心不响应，专注勾选
      var next = !asset.isFavorite;
      api("PUT", "/assets/" + encodeURIComponent(asset.id), { isFavorite: next }).then(function () {
        asset.isFavorite = next;
        cell.classList.toggle("fav", next);
        var heart = cell.querySelector(".immich-cell-fav");
        if (heart) {
          heart.innerHTML = heartIcon(next);
          heart.title = next ? t("fav.title.remove") : t("fav.title.add");
        }
        // 当前详情 pane 若正是该资产，同步按钮状态+颜色（老李 UI 一致性反馈）
        if (state.currentAsset && state.currentAsset.id === asset.id) {
          var b = document.getElementById("immich-btn-fav");
          if (b) {
            b.textContent = next ? t("detail.fav.remove") : t("detail.fav.add");
            b.classList.toggle("active", next);
          }
        }
        // 仅显示收藏模式下取消收藏 → 从列表移除
        if (state.favOnly && !next) refreshAll();
        showStatus(next ? t("status.fav.added") : t("status.fav.removed"), "ok");
      }).catch(function (err) {
        showStatus(t("status.fav.fail", { msg: err.message }), "err");
      });
    };
    return cell;
  }

  // 缩略图懒加载（size 跟随三挡：thumbnail / preview / original）
  function lazyLoad(grid) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var img = en.target;
        if (img.getAttribute("data-asset-id")) {
          loadThumbInto(img, img.getAttribute("data-asset-id"), state.zoomSize);
          img.removeAttribute("data-asset-id");
        }
        observer.unobserve(img);
      });
    }, { root: document.getElementById("immich-timeline"), rootMargin: "200px" });
    grid.querySelectorAll("img[data-asset-id]").forEach(function (img) { observer.observe(img); });
  }

  // ─────────────── 右侧固定详情（需求 11：不弹二级浮层） ───────────────

  function openDetail(asset) {
    state.currentAsset = asset;
    // 记忆详情状态：开着 + 选中资产 id（切换页面/重开面板后还原）
    sessionStorage.setItem(LS_DETAIL_OPEN, "1");
    sessionStorage.setItem(LS_DETAIL_ASSET, asset.id || "");
    syncCellSelectedState(); // 时间轴该 cell 显示选中态黄框（需求 2026-08-16）
    var isVideo = isVideoAsset(asset);
    var pane = document.getElementById("immich-detail-pane");
    pane.style.display = "flex";
    pane.innerHTML =
      '<div id="immich-detail">' +
      '  <div id="immich-detail-bar">' +
      '    <span>' + t("detail.title") + "</span>" +
      '    <button class="immich-btn" id="immich-btn-detail-close" title="' + t("detail.close.title") + '">✕</button>' +
      "  </div>" +
      '  <div id="immich-detail-img">' +
      (isVideo
        ? '<video id="immich-detail-video" controls loop playsinline style="opacity:0"></video>'
        : '<img alt="" data-detail-img="1" style="opacity:0">' +
          '  <button class="immich-detail-fs-btn" id="immich-btn-detail-fullscreen" title="' + t("detail.fullscreen.title") + '">' + FULLSCREEN_ICON + "</button>") +
      "  </div>" +
      '  <div id="immich-detail-info">' +
      '    <div class="immich-detail-row"><span class="k">ID</span>' + esc(asset.id || "") + "</div>" +
      '    <div class="immich-detail-row"><span class="k">' + t("detail.created") + "</span>" + fmtDate(asset.fileCreatedAt || asset.createdAt) + "</div>" +
      '    <div class="immich-detail-row"><span class="k">' + t("detail.type") + "</span>" + esc(assetBadge(asset)) + (isVideo ? " · " + t("detail.video") : " · " + t("detail.image")) + "</div>" +
      (isVideo && asset.duration ? '<div class="immich-detail-row"><span class="k">' + t("detail.duration") + "</span>" + esc(asset.duration) + "</div>" : "") +
      (asset.width && asset.height ? '<div class="immich-detail-row"><span class="k">' + t("detail.dimension") + "</span>" + esc(asset.width) + " × " + esc(asset.height) + "</div>" : "") +
      '    <textarea id="immich-detail-desc" placeholder="' + t("detail.desc.placeholder") + '">' + esc(asset.description || "") + "</textarea>" +
      '    <div class="immich-detail-actions">' +
      '      <button class="immich-btn" id="immich-btn-fav">' + (asset.isFavorite ? t("detail.fav.remove") : t("detail.fav.add")) + "</button>" +
      '      <button class="immich-btn" id="immich-btn-save-desc">' + t("detail.desc.save") + "</button>" +
      '      <button class="immich-btn danger" id="immich-btn-del">' + t("detail.delete.btn") + "</button>" +
      "    </div>" +
      "  </div>" +
      "</div>";

    document.getElementById("immich-btn-detail-close").onclick = userCloseDetail;
    // 已收藏资产：详情收藏按钮加 active 高亮
    if (asset.isFavorite) {
      var favBtnEl = document.getElementById("immich-btn-fav");
      if (favBtnEl) favBtnEl.classList.add("active");
    }
    // 需求 2.2/2.5：图片全屏预览按钮（右下角，播放器控制条样式）。
    // 视频不显示（P2-4：视频原生 controls 自带全屏）
    var fsBtn = document.getElementById("immich-btn-detail-fullscreen");
    if (fsBtn) {
      fsBtn.onclick = function () {
        var el = document.getElementById("immich-detail-img");
        if (el) {
          if (document.fullscreenElement) document.exitFullscreen();
          else el.requestFullscreen().catch(function () { /* 被浏览器拒绝（如 iframe 权限）时忽略 */ });
        }
      };
    }
    if (isVideo) {
      // 视频：fetch + blob（header 鉴权，token 不进 URL）→ 整读后播放，可拖动进度。
      // AbortController：关闭详情时中止仍在下载的大视频，避免占用带宽/内存。
      if (state.videoAbort) state.videoAbort.abort();
      state.videoAbort = new AbortController();
      var vid = document.getElementById("immich-detail-video");
      var opt = { method: "GET", headers: {}, signal: state.videoAbort.signal };
      if (token) opt.headers["Authorization"] = "Bearer " + token;
      fetch(API + "/assets/" + encodeURIComponent(asset.id) + "/original", opt)
        .then(function (resp) {
          if (resp.status === 401) throw new Error("AUTH_REQUIRED");
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.blob();
        })
        .then(function (blob) {
          var url = URL.createObjectURL(blob);
          state.blobUrls.push(url);
          vid.src = url;
          vid.style.opacity = "1";
          vid.play().catch(function () { /* 自动播放被浏览器拦截时用户手动点 */ });
        })
        .catch(function (e) {
          // AbortError：用户主动关闭详情，不提示
          if (e && e.name === "AbortError") return;
          var box = document.getElementById("immich-detail-img");
          if (box) box.innerHTML = '<div class="immich-empty">' + t("video.load.fail", { msg: esc(e.message === "AUTH_REQUIRED" ? t("error.token.needed") : e.message) }) + "</div>";
        })
        .finally(function () {
          if (state.videoAbort && state.videoAbort.signal.aborted) state.videoAbort = null;
        });
    } else {
      // 大图走 fetch + blob（header 鉴权，token 不进 URL）
      loadThumbInto(pane.querySelector("[data-detail-img]"), asset.id, "original");
    }
    document.getElementById("immich-btn-fav").onclick = function () {
      var next = !asset.isFavorite;
      api("PUT", "/assets/" + encodeURIComponent(asset.id), { isFavorite: next }).then(function () {
        asset.isFavorite = next;
        // 详情按钮即时更新
        var b = document.getElementById("immich-btn-fav");
        if (b) {
          b.textContent = next ? t("detail.fav.remove") : t("detail.fav.add");
          b.classList.toggle("active", next);
        }
        // 时间轴对应 cell 即时更新（爱心 + fav class）
        document.querySelectorAll('[data-cell-id="' + CSS.escape(asset.id) + '"]').forEach(function (c) {
          c.classList.toggle("fav", next);
          var heart = c.querySelector(".immich-cell-fav");
          if (heart) heart.innerHTML = heartIcon(next);
        });
        // 仅显示收藏模式下取消收藏 → 该资产应从列表消失，刷新
        if (state.favOnly && !next) refreshAll();
        showStatus(next ? t("status.fav.added") : t("status.fav.removed"), "ok");
      }).catch(function (e) {
        showStatus(t("status.fav.fail", { msg: e.message }), "err");
      });
    };
    document.getElementById("immich-btn-save-desc").onclick = function () {
      var desc = document.getElementById("immich-detail-desc").value;
      api("PUT", "/assets/" + encodeURIComponent(asset.id), { description: desc }).then(function () {
        asset.description = desc;
        showStatus(t("status.desc.saved"), "ok");
      }).catch(function (e) {
        showStatus(t("status.desc.fail", { msg: e.message }), "err");
      });
    };
    document.getElementById("immich-btn-del").onclick = function () {
      // 同 bulkDelete：自定义确认弹窗（原生 confirm 的 Esc 冒泡会误关面板）
      confirmDialog(t("detail.delete.confirm"), t("detail.delete.btn")).then(function (confirmed) {
        if (!confirmed) return;
        api("DELETE", "/assets", { ids: [asset.id], force: false }).then(function () {
          closeDetail();
          refreshAll();
          showStatus(t("status.del.done"), "ok");
        }).catch(function (e) {
          showStatus(t("status.del.fail", { msg: e.message }), "err");
        });
      });
    };
  }

  // 需求 2.1/2.3：详情 pane 打开面板/刷新时默认显示占位；
  // 但用户点 ✕ 可真正关闭（隐藏 pane），不是"一直显示"（P2-3）。
  function showDetailPlaceholder() {
    var pane = document.getElementById("immich-detail-pane");
    if (!pane) return;
    if (state.videoAbort) {
      state.videoAbort.abort();
      state.videoAbort = null;
    }
    // 回收详情大图/视频 blob（时间轴缩略图由 renderTimeline 的 revokeBlobs 统一回收）
    var img = pane.querySelector("[data-detail-img]");
    if (img && img.src && img.src.indexOf("blob:") === 0) URL.revokeObjectURL(img.src);
    var vid = pane.querySelector("#immich-detail-video");
    if (vid) {
      vid.pause();
      if (vid.src && vid.src.indexOf("blob:") === 0) URL.revokeObjectURL(vid.src);
    }
    pane.style.display = "flex";
    pane.innerHTML =
      '<div id="immich-detail">' +
      '  <div id="immich-detail-bar"><span>' + t("detail.title") + "</span>" +
      '    <button class="immich-btn" id="immich-btn-detail-close" title="' + t("detail.close.title") + '">✕</button>' +
      "  </div>" +
      '  <div class="immich-detail-empty">👈 ' + t("detail.empty.hint") + "</div>" +
      "</div>";
    document.getElementById("immich-btn-detail-close").onclick = userCloseDetail;
    state.currentAsset = null;
    syncCellSelectedState(); // 占位（无选中资产）→ 取消时间轴选中态黄框（审小爪 P3 2026-08-16）
    // 记忆详情状态：开着但未选中资产
    sessionStorage.setItem(LS_DETAIL_OPEN, "1");
    sessionStorage.removeItem(LS_DETAIL_ASSET);
  }

  // 用户主动关闭详情（✕ 按钮）：真正隐藏 pane，并记忆"关闭"状态
  function userCloseDetail() {
    closeDetail();
    sessionStorage.setItem(LS_DETAIL_OPEN, "0");
    sessionStorage.removeItem(LS_DETAIL_ASSET);
  }

  // 还原详情状态（打开面板/切回时间线/刷新后）：
  // 记忆为关闭 → 保持隐藏；记忆为开着且有选中资产 → 恢复资产详情（本期 assetsById
  // 未命中时单查兜底，覆盖往期/溢出资产，P1 审小爪 2026-08-16）；
  // 其他（开着但无/找不到资产，或首次无记忆）→ 显示占位（默认开启）
  function restoreDetailState() {
    var pane = document.getElementById("immich-detail-pane");
    if (!pane) return;
    if (sessionStorage.getItem(LS_DETAIL_OPEN) === "0") {
      closeDetail();
      return;
    }
    var id = sessionStorage.getItem(LS_DETAIL_ASSET);
    var asset = id && state.assetsById ? state.assetsById[id] : null;
    if (asset) {
      openDetail(asset);
      return;
    }
    if (!id) {
      showDetailPlaceholder();
      return;
    }
    // 本期未渲染（往期折叠/溢出月份）：单资产端点兜底查询
    var token = viewToken;
    api("GET", "/assets/" + encodeURIComponent(id)).then(function (a) {
      if (token !== viewToken) return; // 视图已切走，放弃
      if (a && a.id) {
        state.assetsById[a.id] = a;
        openDetail(a);
      } else {
        showDetailPlaceholder();
      }
    }).catch(function () {
      if (token !== viewToken) return;
      showDetailPlaceholder(); // 资产已删除/不存在 → 占位
    });
  }

  // ─────────────── 时间轴状态记忆（老李需求 2026-08-16） ───────────────
  // 滚动位置 + 往期月份展开/折叠态，sessionStorage 持久化，重开面板/切页还原

  // 月份统一展开 key：tb "2026-08-01" / "2026-8-01" → "2026-8"（无前导零，10-12 月原样）
  function monthExpandKey(tbOrMk) {
    var m = String(tbOrMk || "").match(/^(\d{4})-0?(\d{1,2})/);
    return m ? m[1] + "-" + (+m[2]) : "";
  }

  function getExpandedMonths() {
    try {
      var v = JSON.parse(sessionStorage.getItem(LS_TIMELINE_EXPANDED) || "[]");
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }

  function saveExpandedMonths(list) {
    sessionStorage.setItem(LS_TIMELINE_EXPANDED, JSON.stringify(list));
  }

  function setMonthExpanded(key, expanded) {
    if (!key) return;
    var list = getExpandedMonths();
    var i = list.indexOf(key);
    if (expanded && i < 0) list.push(key);
    if (!expanded && i >= 0) list.splice(i, 1);
    saveExpandedMonths(list);
  }

  // 保存时间轴滚动位置（防抖：滚动停止 300ms 后落盘）
  var _scrollTimer = null;
  function scheduleSaveTimelineScroll() {
    if (_scrollTimer) clearTimeout(_scrollTimer);
    _scrollTimer = setTimeout(function () {
      var tl = document.getElementById("immich-timeline");
      if (tl) sessionStorage.setItem(LS_TIMELINE_SCROLL, String(tl.scrollTop));
    }, 300);
  }

  // 还原时间轴滚动位置（renderTimeline 数据加载完后调用；展开态在月份渲染处各自还原）
  function restoreTimelineState() {
    var tl = document.getElementById("immich-timeline");
    if (!tl) return;
    var saved = parseInt(sessionStorage.getItem(LS_TIMELINE_SCROLL) || "0", 10);
    if (!isNaN(saved) && saved > 0) tl.scrollTop = saved;
  }

  // 用户主动关闭详情（✕ 按钮）：真正隐藏 pane
  function closeDetail() {
    var pane = document.getElementById("immich-detail-pane");
    if (pane) {
      if (state.videoAbort) {
        state.videoAbort.abort();
        state.videoAbort = null;
      }
      var img = pane.querySelector("[data-detail-img]");
      if (img && img.src && img.src.indexOf("blob:") === 0) URL.revokeObjectURL(img.src);
      var vid = pane.querySelector("#immich-detail-video");
      if (vid) {
        vid.pause();
        if (vid.src && vid.src.indexOf("blob:") === 0) URL.revokeObjectURL(vid.src);
      }
      pane.style.display = "none";
      pane.innerHTML = "";
    }
    state.currentAsset = null;
    syncCellSelectedState(); // 关闭详情 → 取消时间轴该 cell 的选中态黄框
  }

  // 刷新时间轴（重新拉桶列表 + 重置已加载）
  function refreshAll() {
    state.buckets = [];
    closeDetail();
    var timeline = document.getElementById("immich-timeline");
    if (timeline) timeline.innerHTML = "";
    renderTimeline();
  }

  // ─────────────── 配置视图 ───────────────

  function renderConfig() {
    var token = ++viewToken; // 使进行中的 renderTimeline 旧链失效
    setViewMode("config");
    clearStatus();
    revokeBlobs();
    closeDetail();
    exitSelectMode(); // v0.6：进配置页退出选择模式
    var body = document.getElementById("immich-timeline");
    body.innerHTML = '<div class="immich-loading">' + t("loading.config") + "</div>";

    api("GET", "/config").then(function (cfg) {
      if (token !== viewToken) return;
      state.config = cfg;
      body.innerHTML =
        '<div class="immich-config-grid">' +
        // 🔗 连接配置
        '  <div class="immich-group" style="grid-column:1/-1">' +
        '    <h3 class="immich-group-title">' + t("cfg.group.connection") + "</h3>" +
        '    <div class="immich-field">' +
        '      <label class="immich-field-title">' + t("cfg.base_url.label") + "</label>" +
        '      <span class="immich-field-desc">' + t("cfg.base_url.desc") + "</span>" +
        '      <input id="cfg-base-url" value="' + esc(cfg.base_url || "") + '" placeholder="http://127.0.0.1:2283/api">' +
        "    </div>" +
        '    <div class="immich-field">' +
        '      <label class="immich-field-title">' + t("cfg.api_key.label") + "</label>" +
        '      <span class="immich-field-desc">' + t("cfg.api_key.desc", { status: cfg.api_key_configured ? t("cfg.api_key.configured") : t("cfg.api_key.unconfigured") }) + "</span>" +
        '      <input id="cfg-api-key" type="password" placeholder="' + (cfg.api_key_configured ? t("cfg.api_key.placeholder.configured") : t("cfg.api_key.placeholder.empty")) + '">' +
        "    </div>" +
        '    <div class="immich-field">' +
        '      <label class="immich-field-title">' + t("cfg.album.label") + "</label>" +
        '      <span class="immich-field-desc" id="cfg-album-desc">' + t("cfg.album.desc") + "</span>" +
        '      <input id="cfg-album" value="' + esc(cfg.default_album || "") + '" placeholder="' + t("cfg.album.placeholder") + '">' +
        "    </div>" +
        "  </div>" +
        // 📦 时间线配置
        '  <div class="immich-group" style="grid-column:1/-1">' +
        '    <h3 class="immich-group-title">' + t("cfg.group.assets") + "</h3>" +
        '    <div class="immich-field">' +
        '      <label class="immich-field-title">' + t("cfg.range.label") + "</label>" +
        '      <span class="immich-field-desc">' + t("cfg.range.desc") + "</span>" +
        '      <select id="cfg-range">' +
        '        <option value="today"' + ((cfg.timeline_range || "today") === "today" ? " selected" : "") + ">" + t("range.today") + "</option>" +
        '        <option value="3d"' + ((cfg.timeline_range || "today") === "3d" ? " selected" : "") + ">" + t("range.3d") + "</option>" +
        '        <option value="7d"' + ((cfg.timeline_range || "today") === "7d" ? " selected" : "") + ">" + t("range.7d") + "</option>" +
        "      </select>" +
        "    </div>" +
        '    <div class="immich-field">' +
        '      <label class="immich-field-title">' + t("cfg.interval.label") + "</label>" +
        '      <span class="immich-field-desc">' + t("cfg.interval.desc") + "</span>" +
        '      <select id="cfg-interval">' +
        '        <option value="15m"' + ((cfg.timeline_interval || "1h") === "15m" ? " selected" : "") + ">15 " + t("cfg.interval.min") + "</option>" +
        '        <option value="30m"' + ((cfg.timeline_interval || "1h") === "30m" ? " selected" : "") + ">30 " + t("cfg.interval.min") + "</option>" +
        '        <option value="1h"' + ((cfg.timeline_interval || "1h") === "1h" ? " selected" : "") + ">1 " + t("cfg.interval.hour") + "</option>" +
        '        <option value="1d"' + ((cfg.timeline_interval || "1h") === "1d" ? " selected" : "") + ">1 " + t("cfg.interval.day") + "</option>" +
        "      </select>" +
        "    </div>" +
        "  </div>" +
        // 🔒 安全
        '  <div class="immich-group" style="grid-column:1/-1">' +
        '    <h3 class="immich-group-title">' + t("cfg.group.security") + "</h3>" +
        '    <div class="immich-field">' +
        '      <label class="immich-field-title">' + t("cfg.panel_token.label") + "</label>" +
        '      <span class="immich-field-desc">' + t("cfg.panel_token.desc") + "</span>" +
        '      <span class="immich-field-warn">' + t("cfg.panel_token.warn") + "</span>" +
        '      <div class="immich-panel-token-row" id="cfg-panel-token-row"></div>' +
        "    </div>" +
        "  </div>" +
        // ⚙️ 其他
        '  <div class="immich-group" style="grid-column:1/-1">' +
        '    <h3 class="immich-group-title">' + t("cfg.group.other") + "</h3>" +
        '    <div class="immich-field">' +
        '      <label class="immich-field-title">' + t("cfg.lang.label") + "</label>" +
        '      <span class="immich-field-desc">' + t("cfg.lang.desc") + "</span>" +
        '      <select id="cfg-lang">' +
        I18N.langs().map(function (l) {
          var label = l === "auto" ? t("cfg.lang.auto") : (l === "zh" ? "中文" : "English");
          return '<option value="' + l + '"' + (I18N.getLangSetting() === l ? " selected" : "") + ">" + label + "</option>";
        }).join("") +
        "      </select>" +
        "    </div>" +
        "  </div>" +
        "  </div>" +
        "</div>";

      // 语言切换：移除 overlay 重建整个面板（header/时间轴/配置页全部刷新文案）
      var langSel = document.getElementById("cfg-lang");
      if (langSel) {
        langSel.onchange = function () {
          // 有未保存的表单草稿时先确认（切语言会重建面板、丢弃草稿）
          var apply = function () {
            setLang(langSel.value);
            applyNodeLang(); // 节点悬停文本跟随新语言
            var old = document.getElementById("immich-panel-overlay");
            if (old) old.remove();
            openPanel();
          };
          if (hasUnsavedChanges()) {
            confirmDialog(t("lang.switch.confirm"), t("confirm.ok")).then(function (ok) {
              if (ok) apply();
              else {
                // 取消：把下拉恢复为当前语言设置（含 auto）
                langSel.value = I18N.getLangSetting();
              }
            });
          } else {
            apply();
          }
        };
      }
      // 默认相册：连接成功后可下拉选择 Immich 相册
      fillAlbumSelect(cfg.default_album || "");
      // 🔒 安全分组：面板令牌状态区（生成/显示/重新生成/清除）
      renderPanelTokenRow();
    }).catch(function (e) {
      // 视图已切走：放弃，不覆盖当前视图
      if (token !== viewToken) return;
      if (e.message === "AUTH_REQUIRED") {
        // 401 时仍允许打开配置页（但要带 token 才能保存）
        body.innerHTML = '<div class="immich-empty">' + t("error.auth") + "</div>" +
          '<div class="immich-empty-sub">' + t("error.auth.hint") + "</div>" +
          '<div class="immich-config-grid">' +
          '  <div class="immich-field" style="grid-column:1/-1">' +
          '    <label class="immich-field-title">' + t("cfg.token.prompt") + "</label>" +
          '    <input id="cfg-panel-token" type="password" placeholder="' + t("cfg.token.prompt") + '">' +
          "  </div>" +
          '  <div class="immich-field" style="grid-column:1/-1">' +
          '    <label class="immich-remember-row"><input type="checkbox" id="immich-remember-token"> <span>' + t("cfg.token.remember") + "</span></label>" +
          '    <span class="immich-field-desc">' + t("cfg.token.remember.desc") + "</span>" +
          '    <span class="immich-field-warn">' + t("cfg.token.remember.warn") + "</span>" +
          "  </div>" +
          '  <div class="immich-config-actions" style="grid-column:1/-1">' +
          '    <button class="immich-btn primary" id="immich-btn-save-token">' + t("btn.save") + "</button>" +
          "  </div>" +
          "</div>";
        document.getElementById("immich-btn-save-token").onclick = function () {
          var v = document.getElementById("cfg-panel-token").value.trim();
          if (!v) { showStatus(t("cfg.token.empty"), "err"); return; }
          token = v;
          if (document.getElementById("immich-remember-token").checked) {
            // 勾选记住 → 持久化 localStorage；同时清掉 sessionStorage 副本避免双份
            localStorage.setItem(LS_TOKEN, v);
            sessionStorage.removeItem(LS_TOKEN);
          } else {
            // 默认会话级 → sessionStorage；若之前记住过则撤销持久化
            sessionStorage.setItem(LS_TOKEN, v);
            localStorage.removeItem(LS_TOKEN);
          }
          renderConfig();
        };
      } else {
        body.innerHTML = '<div class="immich-empty">❌ ' + t("error.load.fail", { msg: esc(e.message) }) + "</div>";
      }
    });
  }

  // 表单是否有未保存的更改（用于测试连接前拦截）
  function hasUnsavedChanges() {
    var cfg = state.config || {};
    if (val("cfg-base-url") !== (cfg.base_url || "")) return true;
    if (val("cfg-album") !== (cfg.default_album || "")) return true;
    if (val("cfg-range") !== (cfg.timeline_range || "today")) return true;
    if (val("cfg-interval") !== (cfg.timeline_interval || "1h")) return true;
    // API Key 不回显明文，填了就算未保存；面板令牌走按钮即时生效（不算草稿）
    if (val("cfg-api-key")) return true;
    return false;
  }

  // 默认相册：连接成功后替换为 Immich 相册下拉
  function fillAlbumSelect(current) {
    api("GET", "/albums").then(function (albums) {
      if (!Array.isArray(albums)) return;
      var wrap = document.getElementById("cfg-album");
      if (!wrap || wrap.tagName === "SELECT") return;
      var cur = current || "";
      var opts = '<option value="">' + t("cfg.album.none") + "</option>";
      var found = cur === "";
      albums.forEach(function (a) {
        var name = (a && (a.albumName || a.album_name)) || "";
        if (!name) return;
        if (name === cur) found = true;
        opts += '<option value="' + esc(name) + '"' + (name === cur ? " selected" : "") + ">" + esc(name) + "</option>";
      });
      if (cur && !found) opts += '<option value="' + esc(cur) + '" selected>' + esc(cur) + " " + t("cfg.current") + "</option>";
      var sel = document.createElement("select");
      sel.id = "cfg-album";
      sel.innerHTML = opts;
      wrap.replaceWith(sel);
      // 说明保持用途文案不变（"这会决定节点将图片或视频保存到Immich的哪个相册里"）
    }).catch(function () {
      // 连接未配置/失败：保持手输，不改动
    });
  }

  function testConnection() {
    // 未保存的更改不允许测试：避免用旧配置测试新填写的地址
    if (hasUnsavedChanges()) {
      showStatus(t("cfg.test.unsaved"), "warn");
      return;
    }
    var btn = document.getElementById("immich-btn-test");
    btn.disabled = true;
    btn.textContent = t("cfg.test.ing");
    api("GET", "/health").then(function (d) {
      if (d.ok) {
        var v = d.version ? " v" + d.version.major + "." + d.version.minor + "." + d.version.patch : "";
        showStatus(t("cfg.test.ok", { v: v, b: d.buckets_count || 0, n: d.assets_count || 0 }), "ok");
      } else {
        // 按后端 stage 给具体提示：connect=地址不可达 / auth=API Key 无效
        var k = d.stage === "connect" ? "cfg.test.fail.connect" : (d.stage === "auth" ? "cfg.test.fail.auth" : "cfg.test.fail");
        showStatus(t(k, { msg: d.error || "" }), "err");
      }
    }).catch(function (e) {
      showStatus(t("cfg.test.fail", { msg: e.message }), "err");
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = t("cfg.test.btn");
    });
  }

  function saveConfig() {
    var payload = {
      base_url: val("cfg-base-url"),
      default_album: val("cfg-album"),
      timeline_range: val("cfg-range"),
      timeline_interval: val("cfg-interval"),
    };
    var ak = val("cfg-api-key");
    if (ak) payload.api_key = ak;

    var btn = document.getElementById("immich-btn-save");
    btn.disabled = true;
    api("PUT", "/config", payload).then(function (cfg) {
      state.config = cfg;
      // 连接配置好后后端自动生成了面板令牌（仅本次响应下发明文）。
      // 不弹窗打扰：只给系统提醒，令牌可在配置页 🔒 安全分组「显示」查看
      if (cfg.panel_token_plain) {
        adoptToken(cfg.panel_token_plain);
        showStatus(t("cfg.panel_token.auto_generated"), "ok");
        renderPanelTokenRow();
      }
      showStatus(t("cfg.save.ok"), "ok");
      // 清空密钥输入框：避免残留值被 hasUnsavedChanges 误判为未保存
      var akEl = document.getElementById("cfg-api-key");
      if (akEl) akEl.value = "";
      // 不跳转：留在配置页，用户可接着点「测试连接」验证
    }).catch(function (e) {
      showStatus(t("cfg.save.fail", { msg: e.message }), "err");
    }).finally(function () {
      btn.disabled = false;
    });
  }

  // 🗑 清空配置：恢复出厂默认（清空 Immich 地址/API Key/面板令牌），带二次确认。
  // 清空 panel_token 后回到本机信任模式；前端同时清掉本地 sessionStorage 令牌。
  function resetConfig() {
    confirmDialog(t("cfg.reset.confirm"), t("cfg.reset.confirm_btn"), "danger-outline").then(function (ok) {
      if (!ok) return;
      api("POST", "/config/reset").then(function (cfg) {
        state.config = cfg;
        token = "";
        sessionStorage.removeItem(LS_TOKEN);
        localStorage.removeItem(LS_TOKEN);
        showStatus(t("cfg.reset.done"), "ok");
        renderConfig(); // 重新拉取默认配置并渲染表单
      }).catch(function (e) {
        showStatus(t("cfg.save.fail", { msg: e.message }), "err");
      });
    });
  }

  // 🔒 面板令牌状态区：按是否已配置渲染按钮组
  function renderPanelTokenRow() {
    var row = document.getElementById("cfg-panel-token-row");
    if (!row) return;
    var cfg = state.config || {};
    if (!cfg.panel_token_configured) {
      row.innerHTML = '<span class="immich-field-desc">' + t("cfg.panel_token.disabled") + "</span>" +
        '<button class="immich-btn" id="pt-gen">' + t("cfg.panel_token.generate") + "</button>";
      document.getElementById("pt-gen").onclick = function () {
        api("POST", "/panel-token").then(function (d) {
          if (d && d.token) {
            adoptToken(d.token);
            state.config.panel_token_configured = true;
            showTokenOnce(t("cfg.panel_token.generate"), d.token);
            renderPanelTokenRow();
          }
        }).catch(function (e) { showStatus(t("cfg.save.fail", { msg: e.message }), "err"); });
      };
      return;
    }
    row.innerHTML = '<span class="immich-field-desc">' + t("cfg.panel_token.enabled") + "</span>" +
      '<button class="immich-btn" id="pt-show">' + t("cfg.panel_token.show") + "</button>" +
      '<button class="immich-btn" id="pt-regen">' + t("cfg.panel_token.regenerate") + "</button>" +
      '<button class="immich-btn" id="pt-clear">' + t("cfg.panel_token.clear") + "</button>";
    document.getElementById("pt-show").onclick = function () {
      api("GET", "/panel-token").then(function (d) {
        if (d && d.token) showTokenOnce(t("cfg.panel_token.show"), d.token);
      }).catch(function (e) { showStatus(t("cfg.save.fail", { msg: e.message }), "err"); });
    };
    document.getElementById("pt-regen").onclick = function () {
      api("POST", "/panel-token").then(function (d) {
        if (d && d.token) {
          adoptToken(d.token);
          showTokenOnce(t("cfg.panel_token.regenerate"), d.token);
          renderPanelTokenRow();
        }
      }).catch(function (e) { showStatus(t("cfg.save.fail", { msg: e.message }), "err"); });
    };
    document.getElementById("pt-clear").onclick = function () {
      confirmDialog(t("cfg.panel_token.clear") + "?").then(function (ok) {
        if (!ok) return;
        api("DELETE", "/panel-token").then(function () {
          token = "";
          sessionStorage.removeItem(LS_TOKEN);
          localStorage.removeItem(LS_TOKEN);
          state.config.panel_token_configured = false;
          showStatus(t("cfg.panel_token.cleared"), "ok");
          renderPanelTokenRow();
        }).catch(function (e) { showStatus(t("cfg.save.fail", { msg: e.message }), "err"); });
      });
    };
  }

  // 采纳新令牌：更新内存 token + sessionStorage（保证当前会话继续可用）。
  // 若之前勾选过「记住令牌到本机」（localStorage 有值），同步更新为新令牌，
  // 保持持久化选择连续有效（否则下次会话会读到已作废的旧令牌）。
  function adoptToken(tok) {
    token = tok;
    sessionStorage.setItem(LS_TOKEN, tok);
    if (localStorage.getItem(LS_TOKEN) !== null) {
      localStorage.setItem(LS_TOKEN, tok);
    }
  }

  // 阅后即焚：弹窗展示令牌明文，关闭即销毁，不落任何存储
  function showTokenOnce(title, tok) {
    var overlay = document.createElement("div");
    overlay.className = "immich-confirm-overlay";
    overlay.innerHTML =
      '<div class="immich-confirm-box immich-token-box">' +
      '  <div class="immich-token-title">' + esc(title) + "</div>" +
      '  <div class="immich-token-value"></div>' +
      '  <div class="immich-confirm-actions">' +
      '    <button class="immich-btn" data-act="copy">' + t("btn.copy") + "</button>" +
      '    <button class="immich-btn primary" data-act="close">' + t("confirm.ok") + "</button>" +
      "  </div>" +
      "</div>";
    overlay.querySelector(".immich-token-value").textContent = tok;
    overlay.querySelector('[data-act="copy"]').onclick = function () {
      copyText(tok).then(function () { showStatus(t("status.copied"), "ok"); });
    };
    function close() {
      overlay.remove();
      state._confirming = false;
    }
    overlay.querySelector('[data-act="close"]').onclick = close;
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close(); // 点遮罩 = 关闭即焚
    });
    state._confirming = true;
    document.body.appendChild(overlay);
  }

  // 复制文本：优先 Clipboard API（安全上下文），http 局域网回退 execCommand
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  function val(id) {
    var el = document.getElementById(id);
    return el ? el.value : "";
  }

  // ─────────────── 节点 DESCRIPTION 语言跟随（老李 2026-08-16）───────────────
  // 节点悬停文本跟随面板语言设置（localStorage "immich_lang"：auto/zh/en），
  // 而不是 ComfyUI 自身的 Locale。
  //
  // ⚠️ HACK 标注（审小爪 2026-08-16 复审）：ComfyUI 0.30 Vue 前端 tooltip 显示
  // 来源是 Pinia nodeDefStore 的 nodeDefsByName[type].description（GraphView
  // onIdle: showTooltip(def?.description)），不再读 LiteGraph 类/节点实例。
  // 当前无公开 API 可改 store（IIFE 的 window.app compat 层不暴露 nodeDefs），
  // 只能经 Vue 3 / pinia 内部结构（__vue_app__ / _context.provides /
  // piniaSymbol / pinia._s）访问：
  //   1. 根 DOM #vue-app 的 __vue_app__ → Vue app 实例（Vue 3 mount 标准行为）；
  //   2. app._context.provides 里找 pinia（pinia 用 Symbol('pinia') 作 provide
  //      key，for...in 枚举不到，须加 Object.getOwnPropertySymbols 遍历）；
  //   3. pinia._s（Map）按 id 'nodeDef' 找 store，改 nodeDefsByName[type].description。
  //   长期解：官方暴露 useNodeDefStore()/公开 API 后，评估 TS extension 打包
  //   替代本 hack（当前 IIFE window.app compat 层不暴露 nodeDefs）。
  // 【升级回归检查项】Vue / pinia / ComfyUI 前端升级后，需回归验证节点悬停
  // 文本是否仍跟随面板语言（失败会静默降级为后端默认中文，见 console.warn）。
  var NODE_DESC_KEYS = {
    "ImmichSaveImage": "node.desc.image",
    "ImmichSaveVideo": "node.desc.video",
  };

  function getNodeDefsMap() {
    try {
      var rootEl = document.getElementById("vue-app")   // ComfyUI 0.30 Vue 挂载点
        || document.getElementById("app")
        || document.getElementById("graph-canvas-container")
        || document.querySelector(".app");
      var vueApp = rootEl && rootEl.__vue_app__;
      if (!vueApp) return null;
      var provides = vueApp._context && vueApp._context.provides;
      if (!provides) return null;
      // pinia 以 Symbol 作为 provide key（pinia 源码 piniaSymbol），
      // for...in 枚举不到 Symbol 属性，必须用 Object.getOwnPropertySymbols
      var pinia = null;
      for (var k in provides) {
        var v = provides[k];
        if (v && typeof v === "object" && v._s && typeof v._s.get === "function") { pinia = v; break; }
      }
      if (!pinia) {
        var syms = Object.getOwnPropertySymbols(provides);
        for (var i = 0; i < syms.length; i++) {
          var sv = provides[syms[i]];
          if (sv && typeof sv === "object" && sv._s && typeof sv._s.get === "function") { pinia = sv; break; }
        }
      }
      if (!pinia) return null;
      var stores = pinia._s;
      var s = stores.get("nodeDef");           // 精确匹配优先（nodeDefStore.ts:326）
      if (!s || !s.nodeDefsByName) {
        for (var id of stores.keys()) {        // 兜底：兼容未来改名
          if (/^nodeDefs?$/i.test(id)) {
            s = stores.get(id);
            if (s && s.nodeDefsByName) break;
          }
        }
      }
      return s && s.nodeDefsByName ? s.nodeDefsByName : null;
    } catch (e) {
      // 前端/pinia 升级后此处可能静默失败 → tooltip 语言跟随失效（降级为后端
      // 默认中文）。必须 warn 便于排查，且不要被误认为 LiteGraph 兜底有效。
      if (typeof console !== "undefined" && console.warn) {
        console.warn("[ComfyUI-ImmichManager] nodeDefStore 访问失败，节点悬停文本将不跟随面板语言：", e && e.message);
      }
      return null;
    }
  }

  function applyNodeLang() {
    var txt = {};
    Object.keys(NODE_DESC_KEYS).forEach(function (type) {
      txt[type] = t(NODE_DESC_KEYS[type]);
    });
    // 1) Pinia nodeDef store：ComfyUI 0.30 tooltip 的实际数据源
    var defs = getNodeDefsMap();
    if (defs) {
      Object.keys(txt).forEach(function (type) {
        var def = defs[type];
        if (def && typeof def === "object") def.description = txt[type];
      });
    }
    // 2) LiteGraph 注册类：仅兼容老前端渲染/新拖出节点的默认描述
    //    （新前端 0.30 tooltip 不读它，主路径是上面的 nodeDef store）
    if (window.LiteGraph && LiteGraph.registered_node_types) {
      Object.keys(txt).forEach(function (type) {
        var nt = LiteGraph.registered_node_types[type];
        if (nt) nt.DESCRIPTION = txt[type];
      });
    }
    // 3) 已存在节点实例：仅兼容老前端 tooltip 兜底（新前端读全局 store 不读实例）
    var g = window.app && app.graph;
    if (g && g._nodes) {
      g._nodes.forEach(function (n) {
        if (txt[n.type] != null) n.description = txt[n.type];
      });
    }
  }

  if (window.app && window.app.registerExtension) {
    try {
      app.registerExtension({
        name: "ComfyUI-ImmichManager.NodeDesc",
        beforeRegisterNodeDef: function (nodeType, nodeData) {
          var key = NODE_DESC_KEYS[nodeData.name];
          if (key) nodeType.DESCRIPTION = t(key);
        },
      });
    } catch (e) { /* app 未就绪时忽略，applyNodeLang() 兜底 */ }
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ─────────────── 启动 ───────────────

  function init() {
    addToolbarButton();
    applyNodeLang();
    // graph / nodeDef store 可能尚未就绪（扩展 JS 加载时序不可控），
    // 轮询重试直到两者都可用（或达到上限）
    var retries = 0;
    (function retryNodeLang() {
      if (window.app && app.graph && getNodeDefsMap()) {
        applyNodeLang();
        return;
      }
      if (++retries < 30) setTimeout(retryNodeLang, 500);
    })();
    // 面板打开时 Esc 关闭；确认弹窗打开时 Esc 归弹窗（关闭弹窗，不关面板）
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (state._confirming) {
        var ov = document.querySelector(".immich-confirm-overlay");
        if (ov) {
          // 复用 close(false)：找到按钮触发（事件处理器会 remove + 重置 flag）
          var cancel = ov.querySelector('[data-act="cancel"]');
          if (cancel) cancel.click();
        }
        return;
      }
      var pv = document.getElementById("immich-panel-overlay");
      if (pv && pv.style.display !== "none") closePanel();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
