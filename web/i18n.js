// 🐾 ComfyUI-ImmichManager 轻量 i18n（v0.6 P3）
// 用法：window.ImmichI18n.t("btn.select") → 当前语言文案；{n}/{msg} 为占位符，
// 调用时传参数：t("bulk.count", { n: 5 })。
// 语言检测：localStorage("immich_lang") → 无记录/"auto" 时跟随 navigator.language
// （安装后默认跟随系统/浏览器语言）→ en 兜底。
// 节点名（🖼️ Immich Save Image / 🎬 ImmichSaveVideo）保持英文，不做翻译。
(function () {
  "use strict";

  var LANG_KEY = "immich_lang";
  var AUTO = "auto"; // 跟随系统（自动）

  var dict = {
    en: {
      // 节点悬停文本（tooltip DESCRIPTION，语言跟随面板设置）
      "node.desc.image": "[ComfyUI-ImmichManager] Preview the image and upload it to Immich. Saves as PNG by default, with workflow metadata embedded. More settings in the config panel.",
      "node.desc.video": "[ComfyUI-ImmichManager] Preview the video and upload it to Immich. Saves as MP4 by default, with workflow metadata embedded. More settings in the config panel.",

      // 工具栏/顶栏
      "title.panel": "Immich Manager",
      "title.zoom": "Thumbnail resolution",
      "zoom.level.title": "{label} zoom",
      "zoom.small": "S",
      "zoom.medium": "M",
      "zoom.large": "L",
      "btn.timeline": "🖼️ Timeline",
      "btn.timeline.title": "Show asset timeline",
      "btn.copy": "📋 Copy",
      "btn.select": "✅ Select",
      "btn.select.title": "Select multiple assets for batch operations",
      "btn.select.title.active": "Exit selection mode",
      "btn.favonly": "❤️ Show Favorites",
      "btn.favonly.active": "❤️ Show Favorites",
      "btn.favonly.title": "Show favorites only",
      "btn.config": "⚙️ Config",
      "btn.config.title": "Settings",
      "btn.open": "🔗 Immich",
      "btn.open.title": "Open Immich web",
      "btn.close.title": "Close",

      // 批量操作栏
      "bulk.count": "{n} selected",
      "bulk.label": "Selection mode",
      "btn.bulk.all": "Select all",
      "btn.bulk.all.title": "Select all loaded assets in the timeline",
      "btn.bulk.fav": "♡ Favorite",
      "btn.bulk.fav.title": "Favorite selected assets",
      "btn.bulk.del": "🗑 Move to Trash",
      "btn.bulk.del.title": "Delete selected assets (to trash)",
      "btn.bulk.unselect": "Clear selection",
      "btn.bulk.unselect.title": "Clear selection, stay in selection mode",
      "btn.bulk.clear": "✕",
      "btn.bulk.clear.title": "Exit selection mode",
      "select.mode.hint": "Selection mode: click thumbnails to select, then favorite or delete in batch",
      "bulk.fav.done": "Favorited {n} item(s)",
      "bulk.fav.failed": " ({n} failed)",
      "bulk.fav.fail": "Bulk favorite failed: {msg}",
      "bulk.all.confirm.fav": "You selected ALL {n} assets. Favorite them all?",
      "bulk.all.confirm.del": "You selected ALL {n} assets. Move them all to trash?",
      "bulk.del.confirm": "Move {n} asset(s) to trash?\nYou can restore them later from Immich's trash.",
      "btn.del.confirm": "Move to Trash",
      "bulk.del.done": "Deleted {n} asset(s) to trash",
      "bulk.del.fail": "Bulk delete failed: {msg}",

      // 时间轴
      "range.today": "Today",
      "range.3d": "Last 3 days",
      "range.7d": "Last 7 days",
      "range.current": "This period",
      "count.unit": "{n} items",
      "empty.current": "No assets in this period",
      "empty.month": "No assets this month",
      "empty.month.fav": "No favorites this month",
      "month.loaded": "Loaded {n} items",
      "month.format": "{y}-{m}",
      "loading.config": "Loading config…",
      "loading.timeline": "Loading timeline…",
      "loading.thumb": "Loading…",
      "empty.noassets": "📭 No assets found. Check the config or upload to Immich first.",
      "error.auth": "🔒 Access token (panel_token) required.",
      "error.auth.btn": "Go to config",
      "error.auth.hint": "Backend requires panel_token. Enter it below and save.",
      "error.load": "❌ Load failed",
      "error.load.fail": "Load failed: {msg}",
      "error.auth.status": "Backend requires panel_token. Enter it in the config page.",
      "error.token.needed": "Token required",
      "error.load.thumb": "Failed",

      // 详情栏
      "detail.title": "Details",
      "detail.close.title": "Deselect",
      "detail.fullscreen.title": "Fullscreen",
      "detail.empty.hint": "Click an asset on the left to view details. Images support fullscreen; use ♥ to favorite.",
      "detail.created": "Created",
      "detail.type": "Type",
      "detail.video": "Video",
      "detail.image": "Image",
      "detail.duration": "Duration",
      "detail.dimension": "Dimension",
      "detail.desc.placeholder": "Description (saved to Immich)",
      "detail.desc.save": "Save Description",
      "detail.fav.add": "♡ Favorite",
      "detail.fav.remove": "♥ Unfavorite",
      "detail.delete.confirm": "Delete this asset? It goes to Immich trash and can be restored.",
      "detail.delete.btn": "Delete",

      // 收藏/状态
      "fav.title.add": "Favorite",
      "fav.title.remove": "Unfavorite",
      "status.fav.added": "Favorited",
      "status.copied": "Copied",
      "status.fav.removed": "Unfavorited",
      "status.fav.fail": "Favorite failed: {msg}",
      "status.desc.saved": "Description saved",
      "status.desc.fail": "Save failed: {msg}",
      "status.del.done": "Deleted (to trash)",
      "status.del.fail": "Delete failed: {msg}",
      "video.load.fail": "Video load failed: {msg}",

      // 配置页
      "cfg.group.connection": "🔗 Connection",
      "cfg.group.assets": "📦 Timeline settings",
      "cfg.group.security": "🔒 Security",
      "cfg.group.other": "⚙️ Other",
      "cfg.base_url.label": "Immich server URL",
      "cfg.base_url.desc": "For LAN use http://192.168.x.x:2283/api",
      "cfg.api_key.label": "API Key",
      "cfg.api_key.desc": "Leave empty = unchanged; {status}",
      "cfg.api_key.configured": "Configured ✅",
      "cfg.api_key.unconfigured": "Not configured",
      "cfg.api_key.placeholder.configured": "Configured; leave empty to keep",
      "cfg.api_key.placeholder.empty": "Paste Immich API key",
      "cfg.panel_token.label": "Panel access token",
      "cfg.panel_token.desc": "🤔 What is it? If your ComfyUI listens on the LAN, any device on the LAN can indirectly access your Immich assets through this plugin (with the permission of the Immich API key configured above) — the panel access token closes that hole. 🤔 How does it work? Once a token is set, other LAN devices must provide it to access Immich assets through this plugin. 🧐 It is auto-generated once when you fill in the Immich API key and save — but access from this machine (localhost/127.0.0.1) never requires the token, so you can't lock yourself out.",
      "cfg.panel_token.warn": "⚠️ Full-access token: whoever holds it can view/delete assets, change config, and regenerate or clear the token itself. It is designed for one person using multiple devices — do NOT share it with others.",
      "cfg.panel_token.enabled": "🔒Enabled",
      "cfg.panel_token.disabled": "🔓Disabled",
      "cfg.panel_token.generate": "✨ Generate",
      "cfg.panel_token.show": "🔍 Show",
      "cfg.panel_token.regenerate": "🔄 Regenerate",
      "cfg.panel_token.clear": "🗑 Clear",
      "cfg.panel_token.auto_generated": "Panel access token generated",
      "cfg.panel_token.cleared": "Token cleared — panel is back to local trust mode (saving the config again will auto-generate a new token)",
      "cfg.album.label": "Upload to which album",
      "cfg.album.placeholder": "Used when upload node has no album",
      "cfg.album.desc": "Decides which album in Immich the node saves images/videos to",
      "cfg.album.none": "(default)",
      "cfg.range.label": "Pin recent days' assets on top",
      "cfg.range.desc": "Refers to the range of the \"current\" group on the timeline; assets outside this range collapse into past periods",
      "cfg.interval.label": "Timeline grouping basis",
      "cfg.interval.desc": "Refers to the subdivision basis within the \"current\" group",
      "cfg.interval.min": "min",
      "cfg.interval.hour": "hour",
      "cfg.interval.day": "day",
      "cfg.current": "(current)",
      "cfg.lang.label": "🌎Language（语言）",
      "cfg.lang.auto": "🌐 Follow system (auto)",
      "cfg.lang.desc": "Default: follows your system/browser language. Switching saves your choice on this device.",
      "cfg.reset.btn": "🗑 Reset config",
      "cfg.reset.confirm": "Reset ALL settings to factory defaults?\n\nThis clears the Immich URL, API key and panel token — like a fresh install.",
      "cfg.reset.confirm_btn": "Reset",
      "cfg.reset.done": "Config reset to defaults.",
      "cfg.back": "Back to Timeline",
      "cfg.test.btn": "Test",
      "cfg.test.ing": "Testing…",
      "cfg.test.unsaved": "⚠️ Unsaved changes. Save first, then test.",
      "cfg.test.ok": "✅ Connected{v} · {b} bucket(s) · {n} asset(s)",
      "cfg.test.fail": "❌ {msg}",
      "cfg.test.fail.connect": "❌ Cannot reach Immich server: {msg}",
      "cfg.test.fail.auth": "❌ Invalid API Key: {msg}",
      "cfg.save.ok": "✅ Settings saved",
      "cfg.save.fail": "❌ Save failed: {msg}",
      "cfg.token.prompt": "Enter the panel_token from backend config",
      "cfg.token.empty": "Please enter a token",
      "cfg.token.remember": "☑ Remember token on this device",
      "cfg.token.remember.desc": "Checked: the token is saved to this browser's localStorage (persists across browser restarts). Unchecked: kept only in this tab's sessionStorage, cleared when the tab closes.",
      "cfg.token.remember.warn": "⚠️ Any same-origin script (including other custom nodes' JS) can read localStorage — only check this on a trusted browser.",
      "btn.save": "Save",
      "confirm.ok": "OK",
      "confirm.cancel": "Cancel",
      "lang.switch.confirm": "Switch language? Unsaved settings will be lost.",
    },

    zh: {
      // 节点悬停文本（tooltip DESCRIPTION，语言跟随面板设置）
      "node.desc.image": "[ComfyUI-ImmichManager] 将图片预览并上传到 Immich。默认保存为PNG，内含工作流metadata。更多设置在配置面板。",
      "node.desc.video": "[ComfyUI-ImmichManager] 将视频预览并上传到 Immich。默认保存为MP4，内含工作流metadata。更多设置在配置面板。",

      // 工具栏/顶栏
      "title.panel": "Immich Manager",
      "title.zoom": "缩略图清晰度",
      "zoom.level.title": "{label}挡",
      "zoom.small": "小",
      "zoom.medium": "中",
      "zoom.large": "大",
      "btn.timeline": "🖼️ 时间线",
      "btn.timeline.title": "显示资产时间线",
      "btn.copy": "📋 复制",
      "btn.select": "✅ 选择",
      "btn.select.title": "选择多个资产批量操作",
      "btn.select.title.active": "退出选择模式",
      "btn.favonly": "❤️ 只看收藏",
      "btn.favonly.active": "❤️ 只看收藏中",
      "btn.favonly.title": "仅显示收藏",
      "btn.config": "⚙️ 配置",
      "btn.config.title": "配置",
      "btn.open": "🔗 Immich",
      "btn.open.title": "打开 Immich 网页",
      "btn.close.title": "关闭",

      // 批量操作栏
      "bulk.count": "已选 {n} 项",
      "bulk.label": "选择模式",
      "btn.bulk.all": "全选",
      "btn.bulk.all.title": "选中当前时间轴已加载的所有资产",
      "btn.bulk.fav": "♡ 收藏",
      "btn.bulk.fav.title": "收藏选中的资产",
      "btn.bulk.del": "🗑 移到回收站",
      "btn.bulk.del.title": "删除选中的资产（进回收站）",
      "btn.bulk.unselect": "取消选择",
      "btn.bulk.unselect.title": "清空选中，保持选择模式",
      "btn.bulk.clear": "✕",
      "btn.bulk.clear.title": "退出选择模式",
      "select.mode.hint": "选择模式：点击缩略图勾选，可批量收藏/删除",
      "bulk.fav.done": "已收藏 {n} 项",
      "bulk.fav.failed": "（失败 {n} 项）",
      "bulk.fav.fail": "批量收藏失败: {msg}",
      "bulk.all.confirm.fav": "当前选中了所有资产（{n} 项），确定全部收藏吗？",
      "bulk.all.confirm.del": "当前选中了所有资产（{n} 项），确定全部移到回收站吗？",
      "bulk.del.confirm": "将 {n} 个资产移到回收站？\n删除后可在 Immich 网页的回收站中恢复。",
      "btn.del.confirm": "移到回收站",
      "bulk.del.done": "已删除 {n} 个资产（进回收站）",
      "bulk.del.fail": "批量删除失败: {msg}",

      // 时间轴
      "range.today": "今天",
      "range.3d": "近3天",
      "range.7d": "近7天",
      "range.current": "本期",
      "count.unit": "{n} 张",
      "empty.current": "本期暂无资产",
      "empty.month": "本月暂无资产",
      "empty.month.fav": "本月暂无收藏",
      "month.loaded": "已加载 {n} 张",
      "month.format": "{y}年{m}月",
      "loading.config": "加载配置…",
      "loading.timeline": "加载时间轴…",
      "loading.thumb": "加载中…",
      "empty.noassets": "📭 没有找到资产。检查配置或确认 Immich 里已有上传。",
      "error.auth": "🔒 需要访问令牌（panel_token）。",
      "error.auth.btn": "去配置",
      "error.auth.hint": "后端已启用 panel_token 鉴权，请在下方输入后保存",
      "error.load": "❌ 加载失败",
      "error.load.fail": "加载失败: {msg}",
      "error.auth.status": "后端已启用 panel_token 鉴权，请在配置页输入令牌",
      "error.token.needed": "需令牌",
      "error.load.thumb": "加载失败",

      // 详情栏
      "detail.title": "详情",
      "detail.close.title": "取消选中",
      "detail.fullscreen.title": "全屏预览",
      "detail.empty.hint": "点击左侧资产查看详情，图片支持全屏预览，收藏用 ♥",
      "detail.created": "创建",
      "detail.type": "类型",
      "detail.video": "视频",
      "detail.image": "图片",
      "detail.duration": "时长",
      "detail.dimension": "尺寸",
      "detail.desc.placeholder": "描述（保存后写入 Immich）",
      "detail.desc.save": "保存描述",
      "detail.fav.add": "♡ 收藏",
      "detail.fav.remove": "♥ 取消收藏",
      "detail.delete.confirm": "确认删除该资产？删除进入 Immich 回收站，可在 Immich 网页恢复。",
      "detail.delete.btn": "删除",

      // 收藏/状态
      "fav.title.add": "收藏",
      "fav.title.remove": "取消收藏",
      "status.fav.added": "已收藏",
      "status.copied": "已复制",
      "status.fav.removed": "已取消收藏",
      "status.fav.fail": "收藏失败: {msg}",
      "status.desc.saved": "描述已保存",
      "status.desc.fail": "保存失败: {msg}",
      "status.del.done": "已删除（进回收站）",
      "status.del.fail": "删除失败: {msg}",
      "video.load.fail": "视频加载失败: {msg}",

      // 配置页
      "cfg.group.connection": "🔗 连接配置",
      "cfg.group.assets": "📦 时间线配置",
      "cfg.group.security": "🔒 安全",
      "cfg.group.other": "⚙️ 其他",
      "cfg.base_url.label": "Immich 服务地址",
      "cfg.base_url.desc": "局域网场景可填 http://192.168.x.x:2283/api",
      "cfg.api_key.label": "API Key",
      "cfg.api_key.desc": "留空 = 不修改；{status}",
      "cfg.api_key.configured": "已配置 ✅",
      "cfg.api_key.unconfigured": "未配置",
      "cfg.api_key.placeholder.configured": "已配置，留空保持不变",
      "cfg.api_key.placeholder.empty": "粘贴 Immich API key",
      "cfg.panel_token.label": "面板访问令牌",
      "cfg.panel_token.desc": "🤔这是什么？如果你的 ComfyUI 正在监听局域网，那么局域网内任何设备都可以通过 ComfyUI 上的本插件（以上面填入的Immich API的权限）间接访问你的 Immich 资产，面板访问令牌正是为了堵住这个漏洞。🤔它怎么工作？配置好面板访问令牌后，局域网内其他设备需要提供该令牌才可通过本插件访问 Immich 资产。🧐它会在你填好Immich API并保存后自动生成一次，但本机（localhost/127.0.0.1）访问无需令牌——你永远不会把自己锁在门外。",
      "cfg.panel_token.warn": "⚠️ 全权限令牌：持有者可以查看/删除资产、修改配置，甚至可以重新生成或清除令牌本身。它只适用于【个人多台设备】的场景——请勿分发给他人。",
      "cfg.panel_token.enabled": "🔒已启用",
      "cfg.panel_token.disabled": "🔓未启用",
      "cfg.panel_token.generate": "✨ 生成",
      "cfg.panel_token.show": "🔍 显示",
      "cfg.panel_token.regenerate": "🔄 重新生成",
      "cfg.panel_token.clear": "🗑 清除",
      "cfg.panel_token.auto_generated": "面板访问令牌已生成",
      "cfg.panel_token.cleared": "令牌已清除——面板回到本机信任模式（再次保存配置时会自动重新生成）",
      "cfg.album.label": "上传到哪个相册",
      "cfg.album.placeholder": "上传节点未指定相册时使用",
      "cfg.album.desc": "这会决定节点将图片或视频保存到Immich的哪个相册里",
      "cfg.album.none": "（默认）",
      "cfg.range.label": "将最近几天的资产置顶显示",
      "cfg.range.desc": "指的是时间线\"本期\"大组的范围，此范围之外的资产将归入往期折叠",
      "cfg.interval.label": "时间轴分组依据",
      "cfg.interval.desc": "指的是\"本期\"大组内的细分依据",
      "cfg.interval.min": "分钟",
      "cfg.interval.hour": "小时",
      "cfg.interval.day": "天",
      "cfg.current": "（当前）",
      "cfg.lang.label": "🌏语言（Language）",
      "cfg.lang.auto": "🌐 跟随系统（自动）",
      "cfg.lang.desc": "默认跟随系统/浏览器语言；手动切换后记住本机选择。",
      "cfg.reset.btn": "🗑 清空配置",
      "cfg.reset.confirm": "确定清空全部配置吗？\n\n将清除 Immich 地址、API Key 和面板令牌，恢复为刚安装插件时的出厂状态。",
      "cfg.reset.confirm_btn": "清空",
      "cfg.reset.done": "已清空配置，恢复出厂状态。",
      "cfg.back": "返回时间线",
      "cfg.test.btn": "测试连接",
      "cfg.test.ing": "测试中…",
      "cfg.test.unsaved": "⚠️ 有未保存的更改，请先点「保存配置」再测试",
      "cfg.test.ok": "✅ 连接成功{v} · {b} 个时间桶 · {n} 个资产",
      "cfg.test.fail": "❌ {msg}",
      "cfg.test.fail.connect": "❌ 无法连接 Immich 服务：{msg}",
      "cfg.test.fail.auth": "❌ API Key 无效：{msg}",
      "cfg.save.ok": "✅ 配置已保存",
      "cfg.save.fail": "❌ 保存失败: {msg}",
      "cfg.token.prompt": "输入后端配置的 panel_token",
      "cfg.token.empty": "请输入令牌",
      "cfg.token.remember": "☑ 记住令牌到本机",
      "cfg.token.remember.desc": "勾选：令牌保存到本机浏览器 localStorage（重启浏览器仍有效）。不勾选：仅保存在当前标签页 sessionStorage，关闭标签页即失效。",
      "cfg.token.remember.warn": "⚠️ 任何同源脚本（包括其他自定义节点的 JS）都能读取 localStorage——请仅在可信浏览器上勾选。",
      "btn.save": "保存配置",
      "confirm.ok": "确认",
      "confirm.cancel": "取消",
      "lang.switch.confirm": "切换语言？未保存的设置将丢失。",
    },
  };

  function getLangSetting() {
    // 返回语言设置："auto"（跟随系统） | "en" | "zh"。
    // localStorage 无记录 / 禁用 / 存了非法值 → "auto"（安装后默认跟随系统）。
    try {
      var s = localStorage.getItem(LANG_KEY);
      if (s === "en" || s === "zh") return s;
    } catch (e) { /* localStorage 可能禁用 */ }
    return AUTO;
  }

  function detectLang() {
    var s = getLangSetting();
    if (s !== AUTO) return s; // 用户显式指定
    var nl = (navigator.language || "en").toLowerCase();
    if (nl.indexOf("zh") === 0) return "zh";
    return "en";
  }

  var lang = detectLang();

  function t(key, params) {
    var d = dict[lang] || dict.en;
    var v = d[key] != null ? d[key] : key;
    if (params) {
      Object.keys(params).forEach(function (k) {
        v = v.split("{" + k + "}").join(params[k]);
      });
    }
    return v;
  }

  function setLang(l) {
    if (l === AUTO || dict[l]) {
      try { localStorage.setItem(LANG_KEY, l); } catch (e) { /* ignore */ }
      lang = detectLang();
    }
    return lang;
  }

  function getLang() { return lang; }
  function langs() { return [AUTO, "en", "zh"]; }

  window.ImmichI18n = { t: t, setLang: setLang, getLang: getLang, getLangSetting: getLangSetting, langs: langs };
})();
