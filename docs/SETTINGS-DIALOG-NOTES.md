# 开发日志：ComfyUI 设置对话框的坑

写给未来的自己。这些结论**全部来自实测**（CDP 读 DOM / 探针注册），
没有官方文档可查 —— ComfyUI 的 `app.ui.settings.addSetting` 只有源码。

上下文溢出后重新接手时，**先读这篇，别再从头试错。**

---

## 0. 一句话总结

**这个对话框的失败是静默的。** 注册错误不会抛异常、不会打 console，
只是"那一行不出现"。所以**每一次改动都必须用 CDP 读真实 DOM 验证**，
不能靠"我看起来写对了"。

---

## 1. category 是数组路径，长度决定层级

```js
category: ["Immich", "1 连接", "连接设置", "服务"]
//          └插件┘   └分组┘   └子页┘    └行锚点┘
```

| 级数 | 结果 |
|---|---|
| 1 级 `["Immich"]` | 落在插件根下，能用 |
| 2 级 `["Immich","连接"]` | **行消失**（实测 inputs 5→4、selects 4→2） |
| 3 级 | 能用，但**同一路径下的多行会互相挤占** |
| **4 级** | ✅ **当前采用**，最稳 |

**实测证据**：把 9 个字段的 category 从三级砍到两级后，
`inputs: 5 → 4`、`selects: 4 → 2`，一半的控件不见了。

**推测机制**：最后一级是"行锚点"，倒数第二级是"页"；
级数不足时多行被归到同一页 → 只画一个。

**⚠️ 但别把它当唯一性要求**：两个时间轴字段曾共用同一锚点 `"时间轴"`
却都能渲染。保险起见**每行给不同锚点**（现在用 `时间轴A` / `时间轴B`）。

---

## 2. 分组顺序由 category[1] 的**字母序**决定

不是注册顺序。中文按拼音排：安全(a) < 连接(l) < 面板(m) < 前端(q) < 网络(w)。

**所以用数字前缀固定顺序**：

```js
["Immich", "1 连接", ...]
["Immich", "2 安全", ...]
["Immich", "3 网络", ...]
["Immich", "4 相册配置", ...]
["Immich", "5 资源", ...]
["Immich", "6 前端面板配置", ...]
```

---

## 3. `name` 不能为空字符串 ← **最坑的一条**

我为了"避免标题重复"把 `name: ""`，结果：

| 后果 | 表现 |
|---|---|
| 内置类型行（`type:"text"`） | **整行消失** |
| 自定义类型行 | `type(name, setter, value)` 的第一个参数是空 → **标签空白**，只剩一个 `!` |

**正确做法**：`name` 写真实标题，让对话框渲染左侧标题列。

---

## 4. `type` 可以是字符串或函数

```js
type: "text"        // 内置：text / combo / number / boolean / slider / color
type: (name, setter, value) => HTMLElement
```

**自定义函数的返回值**，参考成熟插件（它们都能用）：

| 插件 | 返回什么 |
|---|---|
| **KJNodes** | 裸 `<button>` |
| **pysssss** | `<tr>` → `[<td><label></td>, <td><input></td>]` |
| **rgthree** | `<tr class="...">` → 两个 `<td>` |
| **我们**（当前） | `<div>` 包控件 ✅ 能用 |

**⚠️ 别信"必须返回 `<tr>`"这类结论** —— 我在不同状态下得到过相反结果。
**唯一可靠的是：改完立刻用 CDP 看它有没有画出来。**

---

## 5. 自画标签的陷阱

如果自己画 `<label>` 又想保留对话框的标题列，会**重复**。
如果把 `name` 清空来避免重复，会触发第 3 条的连锁问题。

**当前方案（能用）**：

```
name: 真实标题        → 对话框画左侧标题
type 函数只返回控件格  → 不画 label
! 感叹号放在控件前面   → 视觉上「标题 ! 控件」
```

---

## 6. `tooltip` 字段 → 对话框自带 ⓘ，在最右边

```js
{ tooltip: "说明文字" }
```

会渲染成右侧的 ⓘ（PrimeVue tooltip）。

**和自画 `!` 同时用会出现两个标记** —— 二选一。
用户偏好：自画的 `!` 靠标题，所以**不要同时设 `tooltip`**（或接受两个）。

---

## 7. 内置 combo 的样式是对的，原生 `<select>` 不是

原生 `<select>` 的**弹层无法用 CSS 染色**（`color-scheme: dark` 只在部分浏览器生效），
弹出来是刺眼的白板。

**要用内置 `type: "combo"`** —— 它渲染成 PrimeVue 组件，弹层跟主题走。

加宽靠 CSS 注入：

```js
'[role="dialog"] [role="combobox"]{min-width:420px!important}' +
'[role="dialog"] input[type="text"]{min-width:420px!important;width:420px!important}'
```

---

## 8. 设置页的请求必须走带 token 的封装

设置页曾经用裸 `fetch("/api/immich_plus/...")`，**没有 Authorization 头**
→ 远程访问时敏感路由全 401 → **改配置存不上，且没有任何提示**。

**必须复用面板已有的 `api()`**（它附 token、处理 401）。

---

## 9. 反馈不要写进可能不存在的元素

```js
// ❌ 之前：写进一个 span，那行还没渲染时就是 null
function setStatus(msg, isErr) { if (!_statusEl) return; ... }
```

**症状**：按钮点了没反应，且不报错。

**正确**：复用面板自己的 toast（`showStatus`），它是全局的，不依赖行是否渲染。

---

## 10. 验证方法（唯一可靠的）

### ❌ 不要用 `--dump-dom`

```bash
chromium --headless=new --dump-dom --virtual-time-budget=N
```

页面不空闲时**返回 0 字节**，看起来像"代码没运行"，会误导排查。

### ✅ 用 CDP + Runtime.evaluate

```bash
chromium --headless=new --no-sandbox --disable-gpu \
         --remote-debugging-port=9222 \
         --user-data-dir=$(mktemp -d) http://<comfyui>:8188/
pip install websocket-client
python tests/cdp/verify.py
```

`tests/cdp/verify.py` 已经是门禁脚本，断言：

```
rail marker / settings marker / rail button
inputs 数量 / selects 数量 / 按钮文案
```

**改任何设置页代码后都必须跑它，退出码非 0 就是回归。**

### 读注册表（比看 DOM 更早发现问题）

```js
Object.keys(app.ui.settings.settingsLookup).filter(k => k.startsWith('immich'))
  .map(k => ({ id: k, cat: settingsLookup[k].category, type: typeof settingsLookup[k].type }))
```

能立刻看出：注册了没有、category 对不对、type 是函数还是字符串。

---

## 11. 工程事故记录（血泪）

### 事故一：锚点匹配到同名函数，删掉 25 KB 代码

`web/immich_panel.js` 里有**两个** `testConnection` ——
老配置页一个、设置页一个。我用 `s.find('function testConnection')` 定位，
**命中的是老面板那份**，于是把中间 25 KB 全删了。

**`node --check` 依然通过**（删完仍是合法 JS）。

**防复发**：
```python
# 1. 锚点限定在目标区域之后
anchor = s.index('function registerConfigSettings')
start  = s.index('function testConnection', anchor)

# 2. 改完必须核对文件大小
#    108 KB → 83 KB 就是信号
stat -c%s web/immich_panel.js

# 3. 核对关键函数计数
grep -c 'function testConnection' web/immich_panel.js   # 应该是 2
```

### 事故二：文件写了一半就 deploy

**防复发**：先写临时文件 → `node --check` → 通过才 `cp` 到真文件。

```python
open('/tmp/cand.js','w').write(cand)
# node --check /tmp/cand.js && cp /tmp/cand.js web/immich_panel.js
```

### 事故三：改了没验证就报"完成"

我被截图打脸过至少 4 次。**现在规则：没跑 `verify.py` 不算完成。**

---

## 12. 当前可用基线

```
commit c1a58f5
9 个字段（5 输入 + 5 下拉，含搜索框）
4 组按钮（测试连接 / 显示 / 重新生成 / 清空 / 刷新相册）
9 个 ! 悬停注释
4 级 category
verify.py → [OK]
```

改动前先 `git log --oneline` 找这个点，出问题就 `git checkout <它> -- web/immich_panel.js`。
