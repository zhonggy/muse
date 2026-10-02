# muse-auto · muse.ai 注册自动化 + Web 控制台

把 [`muse-ai-login-sop.md`](./muse-ai-login-sop.md) 里手工记录的那套「邮箱 + 验证码」流程，
做成一个**跑在服务器上、带 Web 控制台**的自动化服务：

- 后端：FastAPI + Playwright（Chromium）
- 前端：单页控制台（无构建，原生 JS）
- 验证码：对接 [skymail.ink](https://doc.skymail.ink/api/api-doc.html) 邮件 API 自动收码
- 部署：Docker / docker-compose，一条命令起来
- 支持单个 / 批量任务、并发、实时截图、**人工点击接管**

---

## 1. 它做什么

对每个邮箱执行一遍 SOP 里的完整流程：

| # | 阶段 | 实现要点 |
|---|---|---|
| 1 | 打开 `https://muse.ai/` | SPA，等待 hydration |
| 2 | 展开登录表单 | 若邮箱框已可见则自动跳过 |
| 3 | 填邮箱 | 逐字符输入 + 补派发 `input`/`change`（React 受控输入） |
| 4 | 点「继续」 | `button[type=submit]`，URL 不变，只能看界面 |
| 5 | 填 6 位验证码 | skymail 轮询收码；**保留前导零**；满 6 位自动提交 |
| 6 | 生日（新邮箱） | 3 个 Radix Select，用完整 `pointerdown→mousedown→pointerup→mouseup→click` 序列 |
| 7 | 提交生日 | 容忍页面上下文短暂销毁，最多等 90s |
| 8 | 点「开始」 | 该页有两个 submit，**按文案精确匹配**，不误点「设置」 |
| 9 | 年龄验证 | 点「验证年龄」→ 结账页 → 自动填卡 → 提交 → 回到主页 |

结束后把登录态写进 `data/sessions/<task_id>.json`（Playwright `storage_state`），可下载复用。

---

## 2. 快速开始（Docker，推荐）

```bash
git clone <repo> muse && cd muse
cp .env.example .env      # 至少改 MUSE_CONSOLE_PASSWORD
bash scripts/deploy.sh    # 自动生成密码/密钥 → build → up
```

打开 `http://<服务器IP>:8080/`，用 `.env` 里的 `MUSE_CONSOLE_USER / MUSE_CONSOLE_PASSWORD` 登录。

> `scripts/deploy.sh` 会在 `.env` 不存在时自动生成一份随机控制台密码和 Fernet 密钥，并把密码打印在终端。

手动方式：

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

### 反向代理 + HTTPS（可选但强烈建议）

```nginx
location / {
    proxy_pass         http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header   Upgrade $http_upgrade;     # WebSocket
    proxy_set_header   Connection "upgrade";
    proxy_set_header   Host $host;
    proxy_read_timeout 3600s;
}
```

---

## 3. 本地开发

```bash
bash scripts/dev.sh          # 建 venv、装依赖、装 chromium、启动 --reload
# 或
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
uvicorn app.main:app --reload --port 8080
```

探针脚本（验证 muse.ai 页面选择器是否仍有效，不提交任何数据）：

```bash
python tools/probe_muse.py
```

---

## 4. 控制台使用

### 4.1 设置（首次必做）

「设置」页 → **skymail 收码**：

| 字段 | 说明 |
|---|---|
| API 地址 | 默认 `https://skymail.ink` |
| 登录邮箱 / 密码 | skymail 主账号（`POST /api/login`） |
| 验证码超时 | 默认 240s |
| 轮询间隔 | 默认 3s |

点「测试连接」会调用 `/api/my/loginUserInfo` + `/api/account/list`，返回可见邮箱列表。

**收码逻辑**：先用 `/api/account/list` 找到目标邮箱对应的 `accountId`，找不到就 `POST /api/account/add`
把它加进来；然后以 `/api/email/list?accountId=..` 增量轮询（用 `emailId` 做游标），
从 `subject / text / content` 里抽 6 位数字。若账号是管理员，还会退回 `/api/allEmail/list?accountEmail=..`。

**浏览器与任务**页可以调：并发数、无头模式、代理、时区/语言、截图间隔、
「到年龄验证即停止（不绑卡）」、「自动填写控制台保存的卡片」、「允许控制台点击接管」。

### 4.2 卡片

「卡片」页录入卡号 / 有效期 / CVV / 持卡人 / 邮编。

- 卡号与 CVV 用 **Fernet 对称加密**后写入 `data/store.json`
- 密钥来自 `MUSE_SECRET_KEY`，未设置时自动生成 `data/secret.key`（**容器重建会丢，生产环境务必设 `MUSE_SECRET_KEY`**）
- 所有 API 只返回脱敏卡号（`************4242`），日志里**永不出现完整卡号 / CVV**

### 4.3 创建任务

「任务」页：

```
邮箱列表（每行一个）      生日        支付卡
user1@example.com          1996-07-22  主力卡 ****4242
user2@example.com                      自动轮换
```

- 支付卡选「不绑卡」→ 任务跑到年龄验证页会**暂停**，控制台弹窗让你现场提供卡
- 选「自动轮换」→ 多张卡按邮箱顺序轮流分配
- 勾选「创建后立即开始」则建完就跑

任务列表里每条都能 **开始 / 停止 / 重试 / 删除**；顶部有 **全部开始 / 全部停止 / 清理已完成 / 导出 CSV·JSON**。

### 4.4 实时画面 + 人工接管

右侧「实时画面」按 `MUSE_SCREENSHOT_INTERVAL`（默认 1.5s）推送 JPEG：

- **直接在画面上点击** → 坐标换算后注入浏览器真实鼠标点击
- **画面获得焦点后敲键盘** → 透传 `Enter / Tab / Esc / 方向键 / 普通字符`
- 输入框打字 → 「发送文本」把整段文字打进当前焦点元素
- `↑ / ↓` 滚动页面

遇到 **3DS / 短信验证码 / 银行验证** 时任务会自动暂停并提示，
你在实时画面上手点完成后，点「我已完成人工操作」继续。

---

## 5. 任务状态机

```
pending ──start──▶ running ──┬──▶ success   （已回到主页，登录态已保存）
                             ├──▶ failed    （看 error 字段 / 日志）
                             ├──▶ paused    （needs=card|manual，等你操作）
                             └──▶ stopped   （手动停止 / 服务重启）
```

`paused` 的 `needs` 含义：

| needs | 触发时机 | 恢复方式 |
|---|---|---|
| `card` | 走到年龄验证页但没有可用卡片 | 弹窗提交卡信息，或选一张已保存的卡 |
| `manual` | 3DS / 二次验证 / 找不到支付表单字段 | 在实时画面上手动操作后点「我已完成人工操作」 |

---

## 6. 配置项

`.env`（容器环境变量）与控制台「设置」页等价，控制台优先级更高。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MUSE_CONSOLE_USER` / `MUSE_CONSOLE_PASSWORD` | `admin` / 空 | 空 = 控制台不鉴权 |
| `MUSE_SECRET_KEY` | 自动生成 | 卡片加密密钥（Fernet key 或任意字符串） |
| `HOST_PORT` | `8080` | 宿主机端口 |
| `SKYMAIL_BASE_URL` / `SKYMAIL_EMAIL` / `SKYMAIL_PASSWORD` | — | 收码账号 |
| `MUSE_HEADLESS` | `true` | 无头模式 |
| `MUSE_CONCURRENCY` | `1` | 并行任务数（每个任务一个独立 BrowserContext） |
| `MUSE_BIRTHDAY` | `1996-07-22` | 默认生日 |
| `MUSE_CODE_TIMEOUT` | `240` | 等验证码超时（秒） |
| `MUSE_STEP_TIMEOUT` | `60` | 单步超时（秒） |
| `MUSE_SCREENSHOT_INTERVAL` | `1.5` | 实时画面帧间隔（秒） |
| `MUSE_STOP_AT_VERIFICATION` | `false` | `true` = 到年龄验证页就停，不绑卡 |
| `MUSE_AUTO_FILL_CARD` | `true` | 自动填卡 |
| `MUSE_MANUAL_TAKEOVER` | `true` | 允许控制台点击接管 |
| `MUSE_PROXY` | 空 | 如 `http://user:pass@host:port` |
| `MUSE_LOCALE` / `MUSE_TIMEZONE` | `zh-CN` / `Asia/Shanghai` | 浏览器上下文 |
| `MUSE_VIEWPORT_W` / `MUSE_VIEWPORT_H` | `1280` / `820` | 视口 |

---

## 7. 选择器失效了怎么办

muse.ai 改版时**不需要改代码**：在 `data/selectors.json` 里覆盖即可。

```json
{
  "selectors": {
    "email_input": ["input[placeholder=\"手机号或邮箱\"]", "input[type=\"email\"]"],
    "submit": ["button[type=\"submit\"]"]
  },
  "texts": {
    "expand_login": ["使用手机号或邮箱", "登录"],
    "disclosure_start": ["开始"]
  },
  "urls": { "disclosure": "/access/disclosure" }
}
```

改完重启服务生效。键名见 [`app/selectors.py`](./app/selectors.py)。

---

## 8. 故障排查

| 现象 | 排查 |
|---|---|
| 控制台一直「未连接」 | 反代没转发 WebSocket（缺 `Upgrade`/`Connection` 头）；或 Basic 密码错 |
| 任务卡在「等待验证码」 | 设置页「测试连接」；确认目标邮箱在 skymail 可见邮箱列表里；看任务日志里 skymail 的报错 |
| `我们无法创建你的账户，请重试。` | SOP 里记录过的服务端偶发失败 → 点「重试」 |
| 找不到按钮 / 输入框 | 跑 `python tools/probe_muse.py` 看当前页面结构，按第 7 节覆盖选择器 |
| 生日下拉选不中 | 确认 `data/selectors.json` 里 `option` 仍为 `[role="option"]`；页面可能换成了原生 `<select>` |
| 结账页没弹出来 | 代码会自动点兜底按钮「打开安全结账」；若仍失败，日志里会提示，用实时画面手动打开 |
| Chromium 起不来 / 崩溃 | `docker-compose.yml` 里 `shm_size: 1gb`；确认容器有 `--no-sandbox`（已内置） |
| 卡片解密失败 | `MUSE_SECRET_KEY` 与写入时不一致；要么恢复原密钥，要么删掉旧卡重新录入 |
| 中文截图乱码 | Dockerfile 已装 `fonts-noto-cjk`；裸机部署需自行安装中文字体 |

---

## 9. 目录结构

```
.
├── app/
│   ├── main.py          FastAPI 路由 + WebSocket + 静态托管
│   ├── runner.py        TaskRuntime（流程对外接口）/ TaskRunner（并发调度）
│   ├── muse_flow.py     muse.ai 全流程（9 个步骤 + 支付表单 + 等待验证完成）
│   ├── browser.py       Playwright Browser 生命周期 + stealth
│   ├── skymail.py       skymail API 客户端 + 验证码正则抽取
│   ├── js_helpers.py    注入页面的 JS（穿透 shadow DOM / 真实鼠标事件序列）
│   ├── selectors.py     选择器与文案表（可被 data/selectors.json 覆盖）
│   ├── store.py         JSON 持久化（设置 / 卡片 / 任务）
│   ├── crypto.py        Fernet 加解密 + 卡号脱敏
│   ├── events.py        pub/sub 事件总线
│   └── config.py        配置默认值
├── static/              控制台前端（index.html / app.js / style.css）
├── tools/probe_muse.py  真实站点选择器探针
├── scripts/             dev.sh / deploy.sh
├── data/                运行时数据（store.json、secret.key、sessions/、shots/）
├── Dockerfile
└── docker-compose.yml
```

---

## 10. 安全与合规须知

- 控制台持有**支付卡信息**，请务必：设置强 `MUSE_CONSOLE_PASSWORD`、走 HTTPS、只在内网或 VPN 暴露、别把 8080 直接开到公网。
- `MUSE_SECRET_KEY` 一定要显式设置并妥善保存；它丢了，已存卡片就解不开。
- 卡片信息只在「年龄验证 → 结账页」填写时被读取，**不写入任何日志、不通过 WebSocket 回传**。
- 第 8 步点「开始」等于**代用户同意 Muse 条款 / Meta 的 AI 条款 / 隐私政策**，请确认你有权这么做。
- 本项目只做浏览器自动化，不做验证码识别绕过、不伪造设备指纹、不规避风控。请遵守 muse.ai 的服务条款与当地法律。

---

## 11. 已知限制

- **年龄验证依赖第三方结账页结构**，字段探测是启发式的（`autocomplete` / `name` / `id` / `placeholder` 多路匹配）。
  结账页大改时可能识别不到 → 任务会暂停，用实时画面人工完成。
- **3DS 无法自动化**，必然要人工介入（已设计成暂停 + 接管）。
- 卡是否能通过年龄验证由发卡行 / Muse 风控决定，脚本不保证成功。
- 单机并发受 CPU/内存限制，建议 `MUSE_CONCURRENCY` 不超过 3。
