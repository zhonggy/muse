# muse.ai「邮箱 + 验证码」登录 / 注册 操作步骤记录（SOP）

- **记录日期**：2026-10-01
- **目标站点**：https://muse.ai/ （登录前标题 `Muse — Your Personal AI Agent`，登录后标题 `Muse`）
- **操作方式**：由 Agent 通过浏览器自动化代为操作，逐步记录
- **执行上下文**：**无 Cookie 的隔离上下文**（内置浏览器），与用户本机 Chrome 中已登录的会话完全隔离，未触碰原会话
- **最终状态**：✅ **账户创建成功、登录态已建立**；但 ⛔ **未进入应用主界面** —— 被 Muse 的**年龄验证**关卡拦住（需绑卡或绑社交账号）

> 说明：操作过程中的截图工具仅返回 base64，未成功落盘为文件，因此本文档为纯文字步骤记录。

---



## 二、操作步骤总览（成功路径）

| # | 操作 | 目标元素 | 填入值 / 选择值 | 结果 |
|---|---|---|---|---|
| 1 | 打开站点 | 地址栏 | `https://muse.ai/` | 落在「登录或创建账户」页 |
| 2 | 展开登录表单 | 按钮「使用手机号或邮箱」 | — | 出现邮箱输入框 + 「继续」 |
| 3 | 输入账号 | `input[placeholder="手机号或邮箱"]` | `user1@example.com` | — |
| 4 | 提交账号 | `button[type="submit"]`（「继续」） | — | 切到验证码界面 |
| 5 | 输入验证码 | `input[autocomplete="one-time-code"]` | `063209` | **满 6 位自动提交** |
| 6 | 填写生日 | 三个 Radix 下拉：年 / 月 / 日 | `1996` / `7 月` / `22` | — |
| 7 | 提交生日 | `button[type="submit"]`（「确认」） | — | ✅ 账户创建成功 → 跳 `/access/disclosure` |
| 8 | 同意须知事项 | 按钮「开始」（**非**「设置」） | — | 跳 `/access/verification` |
| 9 | 年龄验证 | 按钮「验证年龄」 | — | ⛔ 弹窗被拦截，**流程终止** |

---

## 三、逐步详解

### 步骤 1：打开 muse.ai
- 在隔离上下文访问 `https://muse.ai/`（首次 URL 可能带 `?aymh_complete=1`，随后归一）
- 页面为 SPA（自定义元素 + Shadow DOM）
- 未登录时落在**「登录或创建账户」**初始页，页脚显示 `© Meta 2026`

### 步骤 2：展开登录表单
- 点击按钮 **「使用手机号或邮箱」** 展开账号表单

### 步骤 3：输入邮箱
- 输入框定位（三者等价，推荐第一个）：
  - `input[placeholder="手机号或邮箱"]`
  - `input[autocomplete="username"]`
  - `input[inputmode="email"]`
- ⚠️ 该站是 SPA，直接设 `value` 框架不感知。需设置 `value` 后**补派发 `input` 与 `change` 事件（`bubbles: true`）**，再读回校验。

### 步骤 4：点击「继续」
- 目标按钮：**`button[type="submit"]`**（全页唯一 submit 按钮，文案「继续」）
- 点击前 `disabled = false`
- 点击后 **URL 不变**（仍 `https://muse.ai/`），仅界面切换

### 步骤 5：输入 6 位验证码
- 界面文案：`请输入你的验证码` / `如需验证你的账户，请输入我们发送到 <邮箱> 的 6 位数验证码。重发验证码`
- 输入框定位：
  - `input[autocomplete="one-time-code"]`（推荐）
  - `input[inputmode="numeric"]`
  - `input[aria-label="6 位数安全码"]`
- ⚠️ **前导零陷阱**：验证码可能是 `063209` 这种带前导零的 6 位串，**必须按字符串写入**，不要被转成数字。
- ⚠️ **满 6 位自动提交**：无需手动点击。提交后按钮文案变 **「正在确认…」** 且 `disabled = true`
- 该页另有按钮「返回」「重发验证码」——**全程未点「重发验证码」**（会作废当前验证码）
- 若一次派发事件后未生效，改用**逐字符模拟输入**（逐个追加并派发事件）

### 步骤 6：填写生日（注册流程的额外步骤）
- 触发原因：该邮箱未注册过，验证码通过后进入**注册信息补全**
- 界面文案：`请输入生日` / `这项信息不会公开显示。` / `为什么需要提供我的出生日期？`
- 三个下拉均为**自定义下拉（Radix UI Select）**，**该页 `input` 数量为 0**：

| 下拉框 | 控件 | 可选范围 |
|---|---|---|
| 年 | `button[role="combobox"]` | 1880 – 2026 |
| 月 | `button[role="combobox"]` | 1 – 12 月 |
| 日 | `button[role="combobox"]` | 依所选年月而定 |

- ⚠️ 技术要点（实测）：选项为门户渲染的**真实 DOM** `[role="option"]`（非 canvas）；**必须用 MouseEvent 序列** `pointerdown → mousedown → pointerup → mouseup → click`（`bubbles:true`）才能选中，**仅用 PointerEvent 序列无效**

### 步骤 7：提交生日
- 目标按钮：`button[type="submit"]`（文案「确认」），提交前 `disabled = false`
- **成功时的表现**：点击后约 8 秒页面上下文短暂销毁（读取返回 `Uncaught`、标签页 url/title 短暂为空），累计约 **15–20 秒**完成跳转 → `https://muse.ai/access/disclosure`，标题变为 `Muse`
- **失败时的表现**（第一次尝试）：按钮变「正在创建账户...」并 `disabled = true`，约 75 秒后恢复为「确认」并弹出 `我们无法创建你的账户，请重试。`

### 步骤 8：同意须知事项
- 页面 `https://muse.ai/access/disclosure`，正文为「须知事项」，三块说明：`可以代你执行操作` / `全天候运行` / `保持掌控`
- 页面有**两个** `button[type="submit"]`：**`开始`** 与 **`设置`** → **必须按文案精确匹配点「开始」**
- ⚠️ 点「开始」等于**同意 Muse 条款 / Meta 的 AI 条款 / 隐私政策**，属于代用户接受协议，**执行前必须取得用户明确授权**
- 另注：SSR→hydration 期间元素会短暂重挂载，`querySelector` 可能首次返回空，需**轮询重试**（如每 300ms、最多 20 次）

### 步骤 9：年龄验证（当前阻塞点）⛔
- 点击「开始」后跳到 `https://muse.ai/access/verification`
- 页面文案：`我们需要验证你的年龄` / `有效的信用卡有助于我们验证你是否已达到可使用我们服务的年龄。`
- 可选入口：

| 按钮 | 类型 | 说明 |
|---|---|---|
| `验证年龄` | `button[type="button"]` | 需绑信用卡 |
| `绑定 Instagram 账户` | `button[type="button"]` | 第三方授权 |
| `绑定 Facebook 账户` | `button[type="button"]` | 第三方授权 |

- 无「跳过 / 稍后再说 / 返回」出口
- 点「验证年龄」后：Muse 尝试**弹出新标签页**承载结账，但**被浏览器弹窗拦截**，页面提示
  `我们无法打开结账选项卡。请为此站点启用弹窗，然后继续操作。`，并出现兜底按钮 **`打开安全结账`**（`button[type="submit"]`）
- **卡号等支付信息未由 Agent 触碰**（红线：不读取、不输入、不回传任何支付数据）

---

## 四、首次尝试的失败记录（`user2@example.com`）

| 时间点 | 现象 |
|---|---|
| 生日提交后约 45 秒 | 一直停在「正在创建账户...」，**无跳转、无报错、无新增网络请求** |
| 继续等待约 30 秒 | 按钮恢复为「确认」，弹出错误 **`我们无法创建你的账户，请重试。`** |
| 刷新页面 | 第 1 次导航报 `ERR_CONNECTION_CLOSED`，第 2 次重试成功 |
| 刷新后落点 | 回到**未登录的登录页**；`document.cookie` 仅有 `wd`/`dpr`/`_fbp`，无 auth/session；`localStorage` 为空 → 未建立登录态 |

**未执行的操作（避免副作用）**：未点「重发验证码」、未重复点击提交、未回退或改邮箱。

---

## 五、成功后的会话证据（第二次尝试）

| 证据 | 内容 |
|---|---|
| URL | 已进入需登录态的受保护页 `/access/disclosure` |
| `localStorage` | 由 2 项增至 3 项，新增 **`hatch:debug:device-id`** |
| `document.cookie` | 仍只有 `wd`/`dpr`/`_fbp`（auth 会话应为 **httpOnly**，JS 读不到） |
| 网络 | 跳转后新增 `_next` chunk 与 `/api/analytics`、`/api/falco`、`/monitoring`、`/api/marketing/*` 等真实请求 |

---

## 六、该站自动化技术备忘

| 项 | 结论 |
|---|---|
| 页面架构 | SPA + 自定义元素 / Shadow DOM |
| 无障碍树 | 基本为空（可交互元素计数为 0）→ **无法用 ref 定位，必须用 CSS 选择器 + evaluate** |
| 输入框赋值 | 设 `value` 后必须派发 `input` + `change` 事件（`bubbles: true`） |
| 下拉框 | Radix UI Select：`button[role="combobox"]` + 门户渲染的 `[role="option"]`；**需 MouseEvent 序列**，PointerEvent 无效 |
| 提交按钮 | 登录/验证码步骤中 `button[type="submit"]` 是唯一 submit 按钮；**但 `/access/disclosure` 页有两个**（`开始` / `设置`），必须按文案精确匹配 |
| 验证码 | 满 6 位**自动提交**；注意**前导零** |
| URL 变化 | 登录/注册前几步 URL 始终为 `https://muse.ai/`，**不能靠 URL 判断步骤，只能看界面文案/元素** |
| 渲染时序 | SSR→hydration 期间元素会短暂重挂载，定位需**轮询重试** |
| 网络稳定性 | 偶发 `ERR_CONNECTION_CLOSED`，重试一次即恢复 |

---

## 七、复现本流程所需输入

1. 邮箱（或手机号）
2. 该邮箱收到的 **6 位验证码**（注意前导零）
3. 若为新邮箱：**出生日期**（年 / 月 / 日，年份范围 1880–2026）
4. 完成注册后，还需**年龄验证**：绑定信用卡，或绑定 Instagram / Facebook 账户
