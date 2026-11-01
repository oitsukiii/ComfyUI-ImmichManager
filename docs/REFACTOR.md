# Refactor plan — one implementation instead of two

Status: planning. Nothing here is implemented yet.

## Why

`web/immich_panel.js` is ~2,500 lines holding two configuration UIs that grew
independently:

| | old config view (overlay) | new settings page (ComfyUI Settings) |
|---|---|---|
| I/O | `api()` | `apiGet()` / `apiSend()` |
| feedback | `showStatus()` | `toast()` |
| styling | inline strings | `WIDE` / `BTN` / `HINT_STYLE` |
| field list | hard-coded markup | `CONFIG_SETTINGS` table |
| action handlers | `testConnection()`, `showToken()`, ... | `runHealth()`, `runTokenShow()`, ... |

Both `testConnection` and `testConnection(group)` existed at the same time. A
patch anchored on the wrong one deleted 25 KB of the old view; `node --check`
still passed. That is the cost of two implementations with the same names.

Bugs this duplication already produced:

- buttons that did nothing because they called the removed `setStatus()` while
  the working one called `toast()`
- duplicate registrations of `immich.entryMode`, which made "move it to group 6"
  impossible for three attempts
- settings that rendered in one UI and not the other

## Target layout

```
web/
  core/
    api.js       the only place that talks to /api/immich_plus
    notify.js    the only place that reports a result to the user
    styles.js    the only place with widths, colours, border radii
    schema.js    the only place that lists fields, groups and annotations
  views/
    settings.js  renders schema into ComfyUI's Settings dialog
    overlay.js   renders schema into the panel's own config view
  entry.js       extension registration, rail button, i18n bootstrap
```

Constraint: ComfyUI loads every `web/**/*.js` as an ES module and they may
import each other by relative path. Verify that before splitting files; if the
loader does not resolve relative imports, keep one file and separate by section.

## Phases

**Phase 1 — core section (no behaviour change).**
Introduce `core` inside the existing IIFE: `api`, `notify`, `styles`, `schema`.
Point the settings page at them. Old view untouched.
Acceptance: settings page behaves exactly as now; file still loads; CDP check
finds the same control count and every button toasts.

**Phase 2 — declarative settings.**
`schema.js` holds one row per field (key, group, label, kind, options, hint,
validate). The settings renderer reads it and contains no per-field code.
Acceptance: adding a field means adding one schema row and nothing else.

**Phase 3 — old view reuses core.**
Delete `showStatus()` and `api()` from the old view; it calls `core` too.
Acceptance: `grep -c showStatus` is 0, both views raise the same toasts.

**Phase 4 — consistency pass.**
Checklist: every button is an inline handler calling `notify`; every request
goes through `core/api`; every colour and width comes from `core/styles`;
every field comes from `core/schema`; no two functions share a name.
Acceptance: the checklist is grep-able and green.

**Phase 5 — verification and docs.**
`tests/cdp/` holds a script that opens the dialog, asserts the expected number
of groups / controls / buttons, clicks each button and asserts a toast, and
saves a screenshot. README gains a "how to verify a UI change" section.

## Known unknowns to settle in Phase 1

1. Does the dialog render a setting whose `type` is a function **and** that
   carries a `tooltip`? Text inputs were visible before `tooltip` was added to
   every field and the user reports them missing now. Verify over CDP before
   assuming.
2. Does ComfyUI resolve relative imports between files under `web/`? If not,
   Phase 1 stays in one file.
3. Which sub-category ordering does the dialog use — registration or
   alphabetical? Merging fields into one sub-category fixed row order, so it is
   registration, but confirm.

## Rules for each phase

- One commit per phase, message says what changed and what was verified.
- Run `node --check` **and** compare file size before deploying: a 25 KB drop is
  the signature of the accidental-deletion bug above.
- Verify over CDP before claiming it works. `--dump-dom` returns empty when the
  page never goes idle; use `Runtime.evaluate` instead.

---

## Backlog（明确决定「暂不做」的项）

| 项 | 状态 | 决定 |
|---|---|---|
| **选图窗口的 MASK 预览** | 不做 | 2026-11-01 决定：节点已经输出 MASK（带 alpha 的 PNG 拆出真实遮罩），只缺可视化预览。等有人提 PR 再做。 |
| **多条件筛选 + 条件 chip（带 x 移除）** | 待定 | 设计已定：相册下拉整合成「相册」条件，再加「类型」条件，每个条件一个 x。约 100 行前端。 |
| **选图窗口的搜索/筛选** | 待定 | 现在只能翻页；大相册（ComfyuiOutput 3,783 张）找图慢。 |
| **老配置页退场** | 待定 | 菜单页已覆盖全部配置；但远程接入带 token 的通路在老页里，删之前要确认菜单页远程可用。 |
