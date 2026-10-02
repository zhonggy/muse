"""Resin 代理池接入。

Resin 用 `Platform + Account` 识别业务身份，据此提供**基于身份的粘性代理**。

本项目两种接入方式都用（同一个项目里混用是允许的）：

| 出口 | 方式 | 原因 |
|---|---|---|
| skymail HTTP API | **反向代理** | 纯 Web API，路径拼接最省事，不用碰客户端的代理配置 |
| muse.ai 浏览器流量 | **正向代理** | Playwright 原生支持带认证的 HTTP 代理；浏览器没法做反代 |

**Account 的选择**（必须稳定，否则 Resin 会把同一个业务识别成两个身份）：

- 任务级请求（建邮箱、收信、muse 注册）→ 用**任务邮箱**
  它是我们在登录前就生成好的，天然满足「登录前就有标识」的推荐做法，
  也让同一个任务的信箱操作与注册流量落在同一个粘性 IP 上。
- skymail 账号级操作（登录、枚举邮箱、推断域名）→ 用**配置的 skymail 登录邮箱**。

因为任务邮箱在发第一个请求之前就已确定，本项目**不需要 TempIdentity**，
也就用不上 `inherit-lease`。该方法仍然提供（见 `inherit_lease`），
供后续接入登录前拿不到标识的场景使用。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from urllib.parse import quote, urlparse

#: 当前请求所属的业务身份。每个 asyncio 任务有独立的 context，天然隔离。
current_account: contextvars.ContextVar[str] = contextvars.ContextVar(
    "resin_account", default=""
)


class resin_account_ctx:
    """临时切换 Resin Account 的上下文管理器（同步 set/reset）。"""

    def __init__(self, account: str) -> None:
        self.account = account
        self._token = None

    def __enter__(self) -> "resin_account_ctx":
        if self.account:
            self._token = current_account.set(self.account)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            current_account.reset(self._token)


def set_account(account: str):
    """在长时间运行的任务里设置身份，返回可用于 reset 的 token。"""
    return current_account.set(account or "")


class ResinError(RuntimeError):
    pass


@dataclass
class ResinConfig:
    """从 `resin_url` + `resin_platform_name` 派生出的全部调用参数。

    `resin_url` 形如 `http://127.0.0.1:2260/my-token`，
    其中最后一段路径是 Token，前面是代理基础地址。
    """

    url: str
    platform: str = "Default"

    # ---------------- 解析 ----------------

    def __post_init__(self) -> None:
        parsed = urlparse(self.url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ResinError(
                f"resin_url 格式不对：{self.url!r}，"
                "应形如 http://127.0.0.1:2260/my-token"
            )
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) > 1:
            raise ResinError(
                f"resin_url 的 Token 必须是单个路径段，实际为 {'/'.join(segments)!r}"
            )
        self._scheme = parsed.scheme
        self._netloc = parsed.netloc
        # 允许空 Token：Resin 的 RESIN_PROXY_TOKEN 未设置时是「代理免认证」，
        # 此时 resin_url 就是纯基础地址，正/反向代理都不带令牌
        self._token = segments[0] if segments else ""
        self.platform = (self.platform or "Default").strip() or "Default"
        if "/" in self.platform:
            raise ResinError(
                f"resin_platform_name 必须是单个完整路径段，不能含 '/'：{self.platform!r}"
            )

    @property
    def scheme(self) -> str:
        return self._scheme

    @property
    def netloc(self) -> str:
        """host:port，正向代理的服务器地址用这个。"""
        return self._netloc

    @property
    def base(self) -> str:
        """代理基础地址，如 http://127.0.0.1:2260。"""
        return f"{self._scheme}://{self._netloc}"

    @property
    def token(self) -> str:
        return self._token

    @property
    def auth_required(self) -> bool:
        """是否配置了代理令牌（未配置 = Resin 的「代理免认证」）。"""
        return bool(self._token)

    @property
    def prefix(self) -> str:
        """反向代理前缀。免认证时就是基础地址。"""
        return f"{self.base}/{self._token}" if self._token else self.base

    # ---------------- 反向代理 ----------------

    def reverse_url(self, target: str) -> str:
        """把目标 URL 改写成 Resin 反代 URL。

        推荐格式：`<resin_url>/Platform/protocol/host/path?query`
        例：https://api.example.com/healthz
          → http://127.0.0.1:2260/my-token/Default/https/api.example.com/healthz
        """
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https"):
            raise ResinError(f"只支持 http/https 目标，收到：{target!r}")
        if not parsed.netloc:
            raise ResinError(f"目标 URL 缺少 host：{target!r}")
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return (
            f"{self.prefix}/{self.platform}/{parsed.scheme}/"
            f"{parsed.netloc}{path}{query}"
        )

    def ws_reverse_url(self, target: str) -> str:
        """WebSocket 的反代 URL。

        两条强制约定：
          1. 客户端连到 Resin 这一段只能是 `ws`（不能是 wss）
          2. 路径里的 protocol 段必须写目标的底层协议 `http`/`https`
             （目标 wss → 写 https），不能写 ws/wss

        本项目目前没有对外发起的 WebSocket 连接（控制台的 WS 是入站流量，
        不经过 Resin），这个方法留给后续接入使用。
        """
        parsed = urlparse(target)
        if parsed.scheme not in ("ws", "wss"):
            raise ResinError(f"ws_reverse_url 只接受 ws/wss 目标，收到：{target!r}")
        inner = f"{'https' if parsed.scheme == 'wss' else 'http'}://{parsed.netloc}" \
                f"{parsed.path or '/'}"
        query = f"?{parsed.query}" if parsed.query else ""
        return self.reverse_url(inner).replace("http://", "ws://", 1) + query

    def account_header(self, account: str) -> dict[str, str]:
        """反向代理用请求头传身份。"""
        return {"X-Resin-Account": account}

    # ---------------- 正向代理 ----------------

    def proxy_credentials(self, account: str) -> tuple[str, str]:
        """正向代理的 Proxy Auth：`Platform.Account` / `RESIN_TOKEN`。

        Resin 用**第一个 `.`** 切分 Platform，用**最后一个 `:`** 切分 Token，
        所以 Account 里带 `.` 或 `:` 都没问题。
        代理免认证时 Token 为空字符串。
        """
        if not account:
            raise ResinError("正向代理必须提供 Account 标识")
        return f"{self.platform}.{account}", self._token

    def forward_proxy_url(self, account: str) -> str:
        """带认证信息的完整代理 URL（给 httpx / requests 这类库用）。

        Account 里的 `@` 等字符会被百分号编码，避免破坏 userinfo 结构。
        """
        user, password = self.proxy_credentials(account)
        cred = f"{quote(user, safe='')}:{quote(password, safe='')}"
        return f"{self._scheme}://{cred}@{self._netloc}"

    def playwright_proxy(self, account: str) -> dict[str, str]:
        """Playwright 的 proxy 参数（用户名/密码分开传，不走 URL 解析）。"""
        user, password = self.proxy_credentials(account)
        return {"server": self.base, "username": user, "password": password}

    # ---------------- 其它 ----------------

    def inherit_lease_url(self) -> str:
        return f"{self.prefix}/api/v1/{self.platform}/actions/inherit-lease"

    def describe(self) -> dict:
        """给控制台展示用（不泄露 Token）。"""
        if not self._token:
            masked = "（免认证）"
        elif len(self._token) > 4:
            masked = self._token[:2] + "***" + self._token[-2:]
        else:
            masked = "***"
        return {
            "base": self.base,
            "platform": self.platform,
            "token_masked": masked,
            "auth_required": self.auth_required,
        }


def from_settings(settings: dict) -> ResinConfig | None:
    """settings → ResinConfig。未配置或已关闭时返回 None。"""
    if not settings.get("resin_enabled", True):
        return None
    url = str(settings.get("resin_url") or "").strip()
    if not url:
        return None
    return ResinConfig(
        url=url,
        platform=str(settings.get("resin_platform_name") or "Default").strip() or "Default",
    )


async def inherit_lease(
    config: ResinConfig, parent_account: str, new_account: str
) -> dict:
    """把临时身份的 IP 租约平滑继承给新的稳定身份。

    `POST <resin_url>/api/v1/<PLATFORM>/actions/inherit-lease`
    body: {"parent_account": ..., "new_account": ...}

    本项目正常流程用不到：任务邮箱在发第一个请求之前就已生成，
    从一开始就是稳定身份。留给后续「登录前拿不到标识」的场景。
    """
    import httpx

    if not parent_account or not new_account:
        raise ResinError("inherit_lease 需要 parent_account 与 new_account")
    if parent_account == new_account:
        raise ResinError("parent_account 与 new_account 不能相同")
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            config.inherit_lease_url(),
            json={"parent_account": parent_account, "new_account": new_account},
        )
    if resp.status_code >= 400:
        raise ResinError(f"inherit-lease 失败 HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:200]}


async def probe(config: ResinConfig, account: str, mode: str = "reverse") -> dict:
    """探测当前出口 IP。

    mode="reverse" 走反向代理（路径拼接 + X-Resin-Account 头），
    mode="forward" 走正向代理（Proxy Auth）。
    同一个 Account 两次调用应得到同一个 IP（粘性）。
    """
    import httpx

    target = "https://api.ipify.org/?format=json"
    if mode == "forward":
        async with httpx.AsyncClient(
            proxy=config.forward_proxy_url(account), timeout=12.0
        ) as client:
            resp = await client.get(target)
    else:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                config.reverse_url(target), headers=config.account_header(account)
            )
    if resp.status_code >= 400:
        raise ResinError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        ip = resp.json().get("ip")
    except ValueError:
        ip = resp.text.strip()[:64]
    return {"mode": mode, "account": account, "ip": ip}
