# muse-auto · muse.ai 注册自动化 + Web 控制台

把 [`muse-ai-login-sop.md`](./muse-ai-login-sop.md) 里手工记录的那套「邮箱 + 验证码」流程，
做成一个**跑在服务器上、带 Web 控制台**的自动化服务：

- 后端：FastAPI + Playwright
- 浏览器内核：**Camoufox**（Firefox 152 反检测内核，默认）/ Chromium（备选）
- 前端：单页控制台（无构建，原生 JS）
- 验证码：对接 [skymail.ink](https://doc.skymail.ink/api/api-doc.html) 邮件 API 自动收码
- 部署：Docker / docker-compose，一条命令起来
- 支持单个 / 批量任务、并发、实时截图、**人工点击接管**

---

## 1. 它做什么

对每个邮箱执行一遍 SOP 里的完整流程：

| # | 阶段 | 实现要点 |
|---|---|---|
| 1 | 通过 skymail API 自动创建收件邮箱 | `POST /api/account/add`，域名取自已有邮箱 |
| 2 | 打开 `https://muse.ai/` | SPA，等待 hydration |
| 3 | 展开登录表单 | 若邮箱框已可见则自动跳过 |
| 4 | 填邮箱 | 逐字符输入 + 补派发 `input`/`change`（React 受控输入） |
| 5 | 点「继续」 | `button[type=submit]`，URL 不变，只能看界面 |
| 6 | 填 6 位验证码 | skymail 轮询收码；**保留前导零**；满 6 位自动提交 |
| 7 | 生日（新邮箱） | 在 1995–2002 年内**随机**，3 个 Radix Select 用完整 `pointerdown→mousedown→pointerup→mouseup→click` 序列 |
| 8 | 提交生日 | 容忍页面上下文短暂销毁，最多等 90s |
| 9 | 点「开始」 | 该页有两个 submit，**按文案精确匹配**，不误点「设置」 |
| 10 | 年龄验证 | 点「验证年龄」→ 结账页 → 自动填卡 → 提交 → 回到主页 |

结束后把登录态写进 `data/sessions/<task_id>.json`（Playwright `storage_state`），可下载复用。

---

## 2. 快速开始（服务器部署）

### 2.1 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Linux（Ubuntu 20.04+ / Debian 11+ 实测可用），Windows 请用 WSL2 |
| Docker | 20.10+，且带 `docker compose` v2 插件（`docker compose version` 能跑通） |
| 内存 | ≥ 2 GB。Chromium 每个并发实例约占 300–500 MB，`MUSE_CONCURRENCY=3` 建议 ≥ 4 GB |
| 磁盘 | ≥ 2 GB（镜像内含 Chromium 及中文字体） |
| 网络 | 服务器需能直连 `muse.ai` 与 `skymail.ink` |
| 账号 | 一个 [skymail.ink](https://skymail.ink) 账号，用于收验证码 |

还没装 Docker 的话（Ubuntu / Debian 一行搞定）：

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 重新登录后生效，之后不用再加 sudo
docker compose version          # 确认 v2 插件存在
```

### 2.2 拉代码

```bash
git clone https://github.com/zhonggy/muse.git
cd muse
```

### 2.3 一键部署

```bash
bash scripts/deploy.sh
```

脚本依次做五件事：

1. 检查 `docker` 与 `docker compose`（v2 插件）是否可用
2. 若 `.env` 不存在 → 从 `.env.example` 生成，写入**随机控制台密码**与**随机 Fernet 加密密钥**，并 `chmod 600`
3. `mkdir -p data`（挂载给容器的持久化目录）
4. `docker compose build` → `docker compose up -d`
5. 轮询 `/healthz` 等容器就绪（最多 60s），并打印访问地址与密码

首次 build 约 3–6 分钟（要装 Playwright 依赖 + 下载 Chromium）。

跑完会看到：

```
===================================================
 控制台地址： http://10.0.0.12:8080/
 登录账号：   admin
 登录密码：   Xk9mQ2vT7pLn4bRz
 （已写入 .env，chmod 600）
===================================================

下一步：打开控制台 → 「设置」页配 skymail 账号 → 点「测试连接」
查看日志： docker compose logs -f
```

> 想自己定密码 / 密钥？在跑脚本**之前**手动 `cp .env.example .env` 并改好，脚本就不会覆盖它。

### 2.4 确认服务起来了

```bash
docker compose ps
```

```
NAME        IMAGE            STATUS
muse-auto   muse-auto:latest running (healthy)
```

```bash
curl -s localhost:8080/healthz
```

```json
{"ok":true,"ts":1790914874.85,"browser":false}
```

`"browser":false` 是**正常的** —— Chromium 要到第一次跑任务时才启动，不是启动即拉起。

```bash
docker compose logs -f      # 看启动日志，Ctrl+C 退出（不影响容器）
```

### 2.5 打开控制台

浏览器访问 `http://<服务器IP>:8080/`，输入 `.env` 里的 `MUSE_CONSOLE_USER` / `MUSE_CONSOLE_PASSWORD`。

右上角出现绿色 **「已连接」** = WebSocket 通了。若一直是「未连接」，见[第 10 节](#10-故障排查)（多半是反向代理没转发 WebSocket 头）。

### 2.6 首次配置（三件事）

**① 配 skymail 收码** —— 「设置」页 → 「skymail 收码」

| 字段 | 填什么 |
|---|---|
| API 地址 | 保持 `https://skymail.ink` |
| 登录邮箱 / 密码 | 你的 skymail **主账号**（就是能登录 skymail 网页后台那个） |
| 验证码超时 | 默认 240s，够用 |

填完点 **「测试连接」**，期望看到：

```
✓ admin，可见邮箱 3 个：user1@example.com, user2@example.com, ...
```

看到邮箱列表 = 收码链路通了。

**邮箱不用你准备** —— 每个任务启动时会通过 `POST /api/account/add` 自动创建一个随机邮箱，
前缀格式为 **3 个小写字母 + 2 位数字**（如 `fse53`），域名取自你 skymail 里已有的邮箱。
建重了就自动换一个前缀重试（最多 12 次）。
点「测试连接」旁边勾上「顺便试建一个邮箱」还能顺带验证这个能力。

> 如果 skymail 开了「添加邮箱需人机验证」，自动建邮箱会失败并明确报错，需要先去后台关掉。

**② 录入支付卡** —— 「卡片」页

只要三个输入框：**卡号 / 有效期（MM/YY）/ CVV** → 「保存卡片」。
保存后列表只显示 `************4444` 这样的脱敏卡号；卡号与 CVV 用 Fernet 加密写进 `data/store.json`。

> 只想到验证页为止、暂不绑卡？跳过这步，并在「设置」里勾上 **「到年龄验证页即停止」**。

**③ 建第一个任务** —— 「任务」页

```
并发数   跑多少次   支付卡
  1         1        不绑卡（到验证页暂停）
☑ 启用实时画面    ☑ 创建后立即开始
```

- **并发数**：同时跑几个，建议 1–3
- **跑多少次**：一次性排多少个任务，每个任务自动用一个新邮箱
- **生日不用填**，每个任务在 1995–2002 年内随机

点「开始运行」。右侧实时画面每 1.5s 刷新一帧（可在同一张卡片里关掉），日志会逐条打印：

```
[step] ▶ 创建收件邮箱
[info] 已通过 skymail API 创建邮箱：fse53@your-domain.com
[info] 目标邮箱：fse53@your-domain.com
[step] ▶ 打开站点
[info] 已打开 https://muse.ai/（标题：Muse — Your Personal AI Agent）
[step] ▶ 展开登录表单
[info] 邮箱输入框已可见，跳过展开步骤
[step] ▶ 填写邮箱
[info] 收件箱就绪：accountId=12，基线邮件 0 封
[info] 邮箱已填入：fse53@your-domain.com
[step] ▶ 提交邮箱并等待验证码
[info] 已点击「继续」
[info] 已进入验证码界面，开始轮询 skymail 收件箱
[info] 收到邮件：「你的 Muse 验证码」
[info] 取到验证码：063209（按字符串写入，保留前导零）
...
[step] ▶ 年龄验证
[info] 配置为「到年龄验证即停止」，保存登录态后结束
[success] 年龄验证流程结束，登录态已保存
```

> **建议第一次就这么跑**：并发 1、跑 1 次、勾上「到年龄验证页即停止」。
> 只要日志能走到 `▶ 年龄验证`，说明前 9 步全通了；确认没问题再放开绑卡、上批量。

登录态会落到 `data/sessions/<task_id>.json`，可在「设置」页底部下载。

### 2.7 反向代理 + HTTPS（公网部署必做）

控制台持有支付卡信息，**不要**把 8080 直接暴露到公网。用 Nginx 挡一层：

```nginx
server {
    listen 443 ssl http2;
    server_name muse.example.com;

    ssl_certificate     /etc/letsencrypt/live/muse.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/muse.example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;   # ← 这两行缺了 WebSocket 会一直「未连接」
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_set_header   X-Real-IP  $remote_addr;
        proxy_read_timeout 3600s;                      # 长连接别被 60s 掐断
    }
}
```

配合 `docker-compose.yml` 里把端口改成只监听本机：`127.0.0.1:8080:8080`。

证书用 certbot：`sudo certbot --nginx -d muse.example.com`。

### 2.8 日常运维

| 操作 | 命令 |
|---|---|
| 看日志 | `docker compose logs -f --tail=200` |
| 重启 | `docker compose restart` |
| 停止 | `docker compose down` |
| 停止并删镜像 | `docker compose down --rmi local` |
| 改代码后重建 | `git pull && docker compose up -d --build` |
| 进容器排查 | `docker compose exec muse-auto bash` |
| 备份数据 | `tar czf muse-backup-$(date +%F).tgz data/ .env` |
| 恢复数据 | `tar xzf muse-backup-YYYY-MM-DD.tgz` |

**数据都在 `./data/` 里**，容器重建不丢：

```
data/
├── store.json    设置 / 卡片（加密）/ 任务记录
├── secret.key    卡片加密密钥（设了 MUSE_SECRET_KEY 就不会用这个）
├── sessions/     每个任务的登录态 storage_state
└── shots/        关键步骤截图
```

> ⚠️ `data/secret.key` 丢了，已存卡片就解不开了。生产环境请在 `.env` 里显式设 `MUSE_SECRET_KEY` 并单独备份它。

### 2.9 手动部署（不想用脚本）

```bash
cp .env.example .env
# 生成密钥：
python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
# 把输出填进 .env 的 MUSE_SECRET_KEY，并改掉 MUSE_CONSOLE_PASSWORD

mkdir -p data
docker compose build
docker compose up -d
docker compose logs -f
```

### 2.10 卸载

```bash
docker compose down --rmi local -v
cd .. && rm -rf muse          # ⚠️ 会一并删掉 data/，先备份
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

## 4. 控制台功能详解

> 第一次上手怎么点，看 [2.6 首次配置](#26-首次配置三件事)；这一节只讲各项功能本身。

### 4.1 「设置」页

**skymail 收码**

| 字段 | 默认 | 说明 |
|---|---|---|
| API 地址 | `https://skymail.ink` | 自建实例可换 |
| 登录邮箱 / 密码 | — | skymail **主账号**（不是被注册的那个邮箱） |
| 验证码超时 | 240s | 超过就判失败 |
| 轮询间隔 | 3s | 每轮拉 8 封最新邮件 |

「测试连接」调 `GET /api/my/loginUserInfo` + `GET /api/account/list`，返回可见邮箱列表。

收码链路分三段：

1. `GET /api/account/list` 找目标邮箱的 `accountId`；找不到就 `POST /api/account/add` 把它加进来
2. 记下当前最新的 `emailId` 作为基线，再以 `GET /api/email/list?accountId=..` 增量轮询
3. 从新邮件的 `subject / text / content` 里抽 6 位数字（保留前导零，关键词就近加权）

若账号有管理员权限，第 1 步失败时会退回 `GET /api/allEmail/list?accountEmail=..`。

**浏览器与任务**

| 项 | 默认 | 说明 |
|---|---|---|
| 并发数 | 1 | 同时跑几个任务；每个任务一个独立 `BrowserContext`。建议 ≤ 3 |
| 无头模式 | 开 | 服务器上保持开启 |
| 截图间隔 | 1.5s | 实时画面帧率，调大省带宽 |
| 单步超时 | 60s | 单个页面操作的等待上限 |
| 到年龄验证页即停止 | 关 | 勾上则不绑卡，跑到验证页就保存登录态收工 |
| 自动填写已保存的卡片 | 开 | 关掉则每次都在控制台弹窗手动给卡 |
| 允许控制台点击接管 | 开 | 关掉则实时画面变只读 |
| 代理 / 时区 / 语言 | — | 直接透传给浏览器上下文 |

### 4.2 「卡片」页

就三个输入框：**卡号 / 有效期 / CVV**。

- 有效期支持 `MM/YY`、`MM/YYYY`、`MMYY`、`M/YY` 四种写法，输入时自动补斜杠
- 卡号输入时自动每 4 位加空格，落库前去掉非数字字符
- 卡号与 CVV 用 **Fernet 对称加密**后写进 `data/store.json`，其他字段明文
- 密钥优先取 `MUSE_SECRET_KEY`，没有才用 `data/secret.key`
- 所有 API 只返回脱敏卡号（`************4444`），备注名自动生成为 `卡 ****4444`
- 任务日志里**永不出现完整卡号 / CVV**

### 4.3 「任务」页

```
并发数   跑多少次   支付卡
  2         10       主力卡 ****4444
☑ 启用实时画面    ☑ 创建后立即开始
```

- **并发数**（1–8）：同时跑几个任务；会自动写回全局设置
- **跑多少次**（1–500）：一次排多少个任务
- **收件邮箱不用填** —— 每个任务启动时调 `POST /api/account/add` 自动创建一个随机邮箱，
  前缀为 3 个小写字母 + 2 位数字（如 `fse53`），域名取 `MUSE_EMAIL_DOMAIN`，
  为空则取你 skymail 里已有邮箱的域名
- **生日不用填** —— 每个任务在 `[birthday_year_min, birthday_year_max]`（默认 1995–2002）年内随机
- 支付卡选「不绑卡」→ 跑到年龄验证页会**暂停**，控制台弹窗让你现场给卡
- 选「自动轮换」→ 多张卡按任务顺序轮流分配
- **启用实时画面**：关掉则不推截图（任务照跑，关键步骤仍会存盘到 `data/shots/`），省带宽和 CPU

每条任务可 **开始 / 停止 / 重试 / 删除**；顶部有 **全部开始 / 全部停止 / 清理已完成 / 导出 CSV·JSON**。
导出字段：`email, status, step, code, account_created, reached_verification, session_file, error, attempts, started_at, finished_at`。

### 4.4 实时画面 + 人工接管

右侧「实时画面」按 `MUSE_SCREENSHOT_INTERVAL` 推送 JPEG：

- **直接在画面上点击** → 坐标按缩放比例换算后注入浏览器真实鼠标点击
- **画面获得焦点后敲键盘** → 透传 `Enter / Tab / Esc / 方向键 / Backspace / 普通字符`
- 输入框打字 → 「发送文本」把整段文字打进当前焦点元素
- `↑ / ↓` 滚动页面

遇到 **3DS / 短信验证码 / 银行验证** 时任务会自动暂停（`needs=manual`），
你在实时画面上手点完成后，点「我已完成人工操作」继续。

> 不需要盯实时画面时，把「新建任务」里的 **启用实时画面** 取消勾选，
> 后端会完全停掉截图循环（也接受 WebSocket 上的实时切换）。
> 想彻底禁掉远程控制，再到「设置」里取消「允许控制台点击接管」。

---

## 5. Resin 代理池接入

Resin 用 `Platform + Account` 识别业务身份，据此给出**基于身份的粘性代理**。

### 5.1 两种方式怎么用的

| 出口 | 方式 | 为什么 |
|---|---|---|
| skymail HTTP API | **反向代理** | 纯 Web API，路径拼接最省事，不用碰客户端的代理配置 |
| muse.ai 浏览器流量 | **正向代理** | Playwright 原生支持带认证的 HTTP 代理；浏览器没法做反代 |

同一个项目里混用是允许的 —— 按每个请求的特征选，不必二选一。

### 5.2 Account 怎么选（必须稳定）

**同一个账号的标识一定要稳定**，否则 Resin 会把同一个业务认成两个身份、发两个 IP。

| 请求 | Account | 说明 |
|---|---|---|
| 建邮箱 / 收信 / muse 注册 | **任务邮箱**（如 `fse53@1313223.cyou`） | 登录前就已生成 |
| skymail 登录 / 枚举邮箱 / 推断域名 | **skymail 登录邮箱** | 账号级操作 |

任务邮箱在发第一个请求之前就确定了，所以本项目**不需要 TempIdentity，
也就用不上 `inherit-lease`**。（`POST /api/resin/inherit-lease` 仍然提供，
留给以后「登录前拿不到标识」的场景 —— 注意别把 TempIdentity 写死，
否则所有账号会继承同一个租约。）

### 5.3 配置

「设置」页 → **Resin 代理池**：

| 字段 | 说明 |
|---|---|
| `resin_url` | 含代理基础地址与 Token，如 `http://127.0.0.1:2260/my-token` |
| `Platform` | 默认 `Default`；必须是单个完整路径段（不能含 `/`） |
| 启用 Resin | 配了 `resin_url` 就默认启用；调试时可临时关掉直连 |

点 **「测试连通性 / 粘性」** 会：

1. 走反代打**两次** IP 回显 → 验证粘性（同一身份两次应是同一个 IP）
2. 走正代打一次 IP 回显
3. 对比正反代是否落在同一个出口

顶栏也会显示 `Resin: <Platform>` 的绿色标记，一眼看出当前是否走代理。

### 5.4 实际发出的请求长什么样

**反向代理（skymail）**：

```
GET http://127.0.0.1:2260/my-token/Default/https/skymail.ink/api/email/list?accountId=9&size=8
X-Resin-Account: fse53@1313223.cyou
Authorization: <skymail token>
```

**正向代理（浏览器）**：

```js
// Playwright context proxy
{ server: "http://127.0.0.1:2260",
  username: "Default.fse53@1313223.cyou",
  password: "my-token" }
```

即 Resin 规范的 `Platform.Account:RESIN_TOKEN`。Account 里的 `@` 等字符在需要拼成
URL 时（`forward_proxy_url()`）会自动百分号编码。

### 5.5 自测

```bash
python tools/test_resin.py
```

起一个**假 Resin 服务**，逐项断言（共 21 项）：

- 反代 URL 与规范给的例子完全一致
- 正代凭据是 `Platform.Account:RESIN_TOKEN`
- skymail 的每个请求（含**登录**）都被改写成反代 URL 且带上正确的身份
- 任务上下文里收信切到任务邮箱身份，离开后回到默认身份
- 建邮箱那一步用的是「新邮箱」当身份
- 浏览器 context 确实带上了正代参数
- 未启用 Resin 时请求直连、不带 `X-Resin-Account`

---

## 6. 反检测

muse.ai 是 Meta 的产品，对自动化很敏感。浏览器侧做了两层处理，都在
「设置 → 浏览器与任务 → 反检测」一个开关控制（`MUSE_STEALTH`，默认开）。

### 6.1 启动参数

```python
"--disable-blink-features=AutomationControlled",   # 去掉「被自动化控制」标记
"--no-first-run", "--no-default-browser-check", "--disable-infobars",
"--disable-component-update", "--disable-background-networking",
"--disable-sync", "--disable-extensions", "--mute-audio",
"--force-color-profile=srgb", "--lang=zh-CN",
```

另外用 `ignore_default_args=["--enable-automation"]` **剔掉 Playwright 自己加的
`--enable-automation`** —— 那正是 `navigator.webdriver` 的来源，光靠上面那个
blink 开关盖不住。

### 6.2 页面注入（文档创建前）

| 特征 | 处理 |
|---|---|
| `navigator.webdriver` | 从 `Navigator.prototype` 上**整个 delete**，删不掉才退化成返回 undefined 的 getter |
| `window.chrome` | 补全 `runtime` / `app` / `csi()` / `loadTimes()`（headless 下是个空壳） |
| `navigator.plugins` / `mimeTypes` | 造出 5 个 PDF 插件、10 个 mime；**挂到原生 `PluginArray.prototype` 上**，否则 `instanceof PluginArray` 是 false |
| `navigator.pdfViewerEnabled` | `true` |
| `Notification.permission` | `default`（Playwright 默认设成 `denied`） |
| `permissions.query` | 通知类返回 `default`，其余透传原生实现 |
| Client Hints | 补 `navigator.userAgentData` + 同值的 `Sec-CH-UA` 请求头 |
| WebGL | `UNMASKED_VENDOR/RENDERER` 换成 Intel UHD 630，盖掉 SwiftShader 软渲染 |
| 媒体编解码器 | `canPlayType` 补上 H.264/AAC（Playwright 的 Chromium 不含专有编解码器） |
| `deviceMemory` / `maxTouchPoints` / `hardwareConcurrency` | 补成正常桌面值 |
| `__playwright__binding__` / `__pwInitScripts` | 先设为不可枚举，DOMContentLoaded 后再删 |
| `Function.prototype.toString` | 让被替换的函数仍报 `[native code]` |

> ⚠️ 最后一项的顺序很关键：**不能在 document-start 阶段删 `__pwInitScripts`**。
> Playwright 靠它注册并注入所有 `add_init_script` 脚本，提前删掉会把
> `window.__museHelpers` 一起弄没，整个流程直接崩。所以是「先隐藏、后删除」。

### 6.3 所有伪造值同源

UA、`Sec-CH-UA` 请求头、`navigator.userAgentData`、`chrome.*` 里的版本号
全部从**同一个 Chrome 大版本号**派生（取自 Playwright 报的真实浏览器版本）。
检测器最常抓的不是某个特征单独存在，而是**几个特征互相矛盾** ——
比如 UA 写 Chrome/131 而 Client Hints 写 130。

### 6.4 自测

```bash
python tools/probe_stealth.py     # 40 项特征逐条比对
```

实测结果：

```
自己的探针          40 项  ->  失败 0
bot.sannysoft.com   57 项  ->  失败 0，警告 0
```

改之前是 **34 项挂 16 项**（plugins 为 0、SwiftShader 软渲染、
`__playwright__binding__` 暴露、`permissions.query` 非原生代码……）。

---

## 7. 任务状态机

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

## 8. 配置项

`.env`（容器环境变量）与控制台「设置」页等价，控制台优先级更高。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MUSE_CONSOLE_USER` / `MUSE_CONSOLE_PASSWORD` | `admin` / 空 | 空 = 控制台不鉴权 |
| `MUSE_SECRET_KEY` | 自动生成 | 卡片加密密钥（Fernet key 或任意字符串） |
| `HOST_PORT` | `8080` | 宿主机端口 |
| `SKYMAIL_BASE_URL` / `SKYMAIL_EMAIL` / `SKYMAIL_PASSWORD` | — | 收码账号 |
| `MUSE_BROWSER_ENGINE` | `camoufox` | 浏览器内核：`camoufox` / `chromium` |
| `MUSE_HEADLESS` | `true` | 无头模式 |
| `MUSE_CAMOUFOX_OS` | `windows` | Camoufox 伪造的目标系统 |
| `MUSE_CAMOUFOX_HUMANIZE` | `true` | 鼠标轨迹人性化 |
| `MUSE_CAMOUFOX_GEOIP` | `true` | 按代理出口 IP 推导时区 |
| `MUSE_CAMOUFOX_HEADLESS_MODE` | 空 | 空=原生 headless；`virtual`=Linux 上用 Xvfb 真渲染 |
| `MUSE_STEALTH` | `true` | 反检测：抹掉自动化特征 |
| `MUSE_CONCURRENCY` | `1` | 并行任务数（每个任务一个独立 BrowserContext） |
| `MUSE_BIRTHDAY_YEAR_MIN` / `MUSE_BIRTHDAY_YEAR_MAX` | `1995` / `2002` | 生日随机年份范围（不手填） |
| `MUSE_EMAIL_DOMAIN` | 空 | 自动建邮箱用哪个域名；空则取 skymail 已有邮箱的域名 |
| `MUSE_LIVE_VIEW` | `true` | 是否推送实时画面 |
| `MUSE_CODE_TIMEOUT` | `240` | 等验证码超时（秒） |
| `MUSE_STEP_TIMEOUT` | `60` | 单步超时（秒） |
| `MUSE_SCREENSHOT_INTERVAL` | `1.5` | 实时画面帧间隔（秒） |
| `MUSE_STOP_AT_VERIFICATION` | `false` | `true` = 到年龄验证页就停，不绑卡 |
| `MUSE_AUTO_FILL_CARD` | `true` | 自动填卡 |
| `MUSE_MANUAL_TAKEOVER` | `true` | 允许控制台点击接管 |
| `MUSE_PROXY` | 空 | 直连代理，如 `http://user:pass@host:port`；**仅在未配置 Resin 时生效** |
| `RESIN_URL` | 空 | Resin 入口（含 Token），如 `http://127.0.0.1:2260/my-token` |
| `RESIN_PLATFORM_NAME` | `Default` | Resin Platform，必须是单个路径段 |
| `RESIN_ENABLED` | `true` | 配了 `RESIN_URL` 就默认启用 |
| `MUSE_LOCALE` / `MUSE_TIMEZONE` | `zh-CN` / `Asia/Shanghai` | 浏览器上下文 |
| `MUSE_VIEWPORT_W` / `MUSE_VIEWPORT_H` | `1280` / `820` | 视口 |

---

## 9. 选择器失效了怎么办

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

## 10. 故障排查

| 现象 | 排查 |
|---|---|
| 控制台一直「未连接」 | 反代没转发 WebSocket（缺 `Upgrade`/`Connection` 头）；或 Basic 密码错 |
| 任务卡在「创建收件邮箱」 | skymail 没配好，或它开了「添加邮箱人机验证」。设置页点「测试连接」看报错 |
| 任务卡在「等待验证码」 | 设置页「测试连接」；看任务日志里 skymail 的报错；确认 muse 的邮件确实进了这个邮箱 |
| `我们无法创建你的账户，请重试。` | SOP 里记录过的服务端偶发失败 → 点「重试」 |
| 找不到按钮 / 输入框 | 跑 `python tools/probe_muse.py` 看当前页面结构，按第 7 节覆盖选择器 |
| 生日下拉选不中 | 确认 `data/selectors.json` 里 `option` 仍为 `[role="option"]`；页面可能换成了原生 `<select>` |
| 结账页没弹出来 | 代码会自动点兜底按钮「打开安全结账」；若仍失败，日志里会提示，用实时画面手动打开 |
| Chromium 起不来 / 崩溃 | `docker-compose.yml` 里 `shm_size: 1gb`；确认容器有 `--no-sandbox`（已内置） |
| 卡片解密失败 | `MUSE_SECRET_KEY` 与写入时不一致；要么恢复原密钥，要么删掉旧卡重新录入 |
| 中文截图乱码 | Dockerfile 已装 `fonts-noto-cjk`；裸机部署需自行安装中文字体 |
| 顶栏 Resin 标记是红色 | 没配 `resin_url` 或 `resin_enabled=false`。设置页填好后点保存 |
| Resin 测试报「粘性异常」 | 同一个 Account 两次拿到不同 IP。检查 `resin_url` 里的 Token 是否正确、Platform 是否写错 |
| 所有请求都失败且日志提示 Account | Resin 已启用但拿不到身份。确认任务邮箱已生成，或 skymail 登录邮箱已填 |
| 想临时绕过 Resin 排查 | 设置页取消勾选「启用 Resin」→ 保存，所有请求改直连 |
| 页面渲染异常 / 元素找不到 | 先试关掉「反检测」确认是不是 stealth 脚本引起的。`python tools/probe_stealth.py` 可单独验证 |
| `__museHelpers is undefined` | stealth 脚本把 Playwright 的 init script 机制弄坏了。检查 `app/stealth.py` 里删 `__pwInitScripts` 的时机，必须在 DOMContentLoaded 之后 |

---

## 11. 目录结构

```
.
├── app/
│   ├── main.py          FastAPI 路由 + WebSocket + 静态托管
│   ├── runner.py        TaskRuntime（流程对外接口）/ TaskRunner（并发调度）
│   ├── muse_flow.py     muse.ai 全流程（注册资料 + 支付表单 + 等待验证完成）
│   ├── browser.py       Playwright Browser 生命周期 + 启动参数 + 正代注入
│   ├── stealth.py       反检测（chromium 用）：启动参数 + JS 层伪装 + Playwright 残留清理
│   ├── resin.py         Resin 代理池（反代 URL / 正代凭据 / 身份上下文）
│   ├── skymail.py       skymail API 客户端（经 Resin 反代）+ 验证码正则抽取
│   ├── names.py         英文姓名池（100 名 / 100 姓）
│   ├── js_helpers.py    注入页面的 JS（穿透 shadow DOM / 真实鼠标事件序列）
│   ├── selectors.py     选择器与文案表（可被 data/selectors.json 覆盖）
│   ├── store.py         JSON 持久化（设置 / 卡片 / 任务）
│   ├── crypto.py        Fernet 加解密 + 卡号脱敏
│   ├── util.py          随机生日
│   ├── events.py        pub/sub 事件总线
│   └── config.py        配置默认值
├── static/              控制台前端（index.html / app.js / style.css）
├── tools/
│   ├── probe_muse.py    真实站点选择器探针
│   ├── probe_stealth.py 反检测自测（内核感知，两种内核分别比对）
│   └── test_resin.py    Resin 接入自测（起假 Resin 服务跑断言）
├── scripts/             dev.sh / deploy.sh
├── data/                运行时数据（store.json、secret.key、sessions/、shots/）
├── Dockerfile
└── docker-compose.yml
```

---

## 12. 安全与合规须知

- 控制台持有**支付卡信息**，请务必：设置强 `MUSE_CONSOLE_PASSWORD`、走 HTTPS、只在内网或 VPN 暴露、别把 8080 直接开到公网。
- `MUSE_SECRET_KEY` 一定要显式设置并妥善保存；它丢了，已存卡片就解不开。
- 卡片信息只在「年龄验证 → 结账页」填写时被读取，**不写入任何日志、不通过 WebSocket 回传**。
- 第 8 步点「开始」等于**代用户同意 Muse 条款 / Meta 的 AI 条款 / 隐私政策**，请确认你有权这么做。
- 本项目只做浏览器自动化，不做验证码识别绕过、不伪造设备指纹、不规避风控。请遵守 muse.ai 的服务条款与当地法律。

---

## 13. 已知限制

- **年龄验证依赖第三方结账页结构**，字段探测是启发式的（`autocomplete` / `name` / `id` / `placeholder` 多路匹配）。
  结账页大改时可能识别不到 → 任务会暂停，用实时画面人工完成。
- **3DS 无法自动化**，必然要人工介入（已设计成暂停 + 接管）。
- 卡是否能通过年龄验证由发卡行 / Muse 风控决定，脚本不保证成功。
- 单机并发受 CPU/内存限制，建议 `MUSE_CONCURRENCY` 不超过 3。
