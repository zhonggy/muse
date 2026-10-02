"""skymail.ink 邮件 API 客户端。

文档：https://doc.skymail.ink/api/api-doc.html
流程：POST /api/login 拿 token（请求头 Authorization，不带 Bearer 前缀）
      → GET /api/account/list 找目标邮箱的 accountId
      → GET /api/email/list?accountId=..&size=.. 轮询新邮件
      → 从 subject / text / content 里正则抽 6 位验证码
"""
from __future__ import annotations

import asyncio
import html as html_mod
import random
import re
import string
import time
from typing import Any

import httpx

CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
TAG_RE = re.compile(r"<[^>]+>")

#: 命中这些关键词附近的 6 位数字优先级更高
CODE_KEYWORDS = (
    "验证码", "驗證碼", "安全码", "安全碼", "动态码", "动态密码",
    "verification code", "verify code", "code", "otp", "one-time",
    "passcode", "pin",
)

#: 明显不是验证码的 6 位数字（年份区间 / 常见噪声）
NOISE = {"197001", "000000", "123456"}


class SkymailError(RuntimeError):
    pass


def _random_local_part() -> str:
    """生成看起来正常的邮箱前缀，如 kx7f2m9q。"""
    letters = string.ascii_lowercase
    head = "".join(random.choices(letters, k=3))
    mid = "".join(random.choices(string.digits, k=4))
    tail = "".join(random.choices(letters, k=2))
    return f"{head}{mid}{tail}"


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_code(*sources: str) -> str | None:
    """从若干文本里抽取 6 位验证码（保留前导零，按字符串返回）。"""
    best: tuple[int, int, str] | None = None  # (score, -distance, code)
    for src in sources:
        if not src:
            continue
        low = src.lower()
        for m in CODE_RE.finditer(src):
            code = m.group(1)
            if code in NOISE:
                continue
            start = max(0, m.start() - 120)
            window = low[start:m.end() + 120]
            score = 0
            for kw in CODE_KEYWORDS:
                idx = window.find(kw)
                if idx >= 0:
                    score = 2
                    # 关键词越靠近数字越可信
                    dist = abs(idx - min(120, m.start() - start))
                    cand = (score, -dist, code)
                    if best is None or cand > best:
                        best = cand
                    break
            else:
                # 没命中关键词，作为低优先候选
                cand = (1, 0, code)
                if best is None or cand > best:
                    best = cand
    return best[2] if best else None


class SkymailClient:
    """一个 skymail 登录账号（通常是主账号），可管理其下多个邮箱地址。"""

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        self._token: str | None = None
        self._accounts: list[dict] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def configure(self, base_url: str, email: str, password: str) -> None:
        """热更新凭据；base_url 变化时重建底层 httpx client。"""
        new_base = (base_url or self.base_url).rstrip("/")
        if new_base != self.base_url:
            old = self._client
            self.base_url = new_base
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=20.0,
                headers={"Content-Type": "application/json"},
            )
            try:
                await old.aclose()
            except Exception:
                pass
        self.email = email
        self.password = password
        self._token = None
        self._accounts = None

    async def __aenter__(self) -> "SkymailClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---------------- 基础请求 ----------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self._token:
            headers["Authorization"] = self._token
        resp = await self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise SkymailError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            payload = resp.json()
        except ValueError:
            raise SkymailError(f"返回不是 JSON: {resp.text[:300]}") from None
        code = payload.get("code")
        if code != 200:
            raise SkymailError(f"接口返回 code={code} message={payload.get('message')}")
        return payload.get("data")

    def reset_auth(self) -> None:
        self._token = None
        self._accounts = None

    async def login(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        if not self.email or not self.password:
            raise SkymailError("未配置 skymail 登录邮箱/密码")
        resp = await self._client.post(
            "/api/login",
            json={"email": self.email, "password": self.password},
        )
        if resp.status_code >= 400:
            raise SkymailError(f"登录失败 HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("code") != 200:
            raise SkymailError(f"登录失败：{payload.get('message')}")
        token = (payload.get("data") or {}).get("token")
        if not token:
            raise SkymailError("登录返回里没有 token")
        self._token = token
        return token

    async def me(self) -> dict:
        await self.login()
        return await self._request("GET", "/api/my/loginUserInfo")

    # ---------------- 邮箱地址 ----------------

    async def list_accounts(self, size: int = 30) -> list[dict]:
        await self.login()
        data = await self._request("GET", "/api/account/list", params={"size": size})
        self._accounts = list(data or [])
        return self._accounts

    async def find_account(self, email: str) -> dict | None:
        target = email.strip().lower()
        accounts = self._accounts if self._accounts is not None else await self.list_accounts()
        for acc in accounts:
            if str(acc.get("email", "")).strip().lower() == target:
                return acc
        # 退一步：按前缀匹配
        for acc in accounts:
            if str(acc.get("email", "")).strip().lower().startswith(target.split("@")[0]):
                return acc
        return None

    async def add_account(self, email: str) -> dict:
        await self.login()
        self._accounts = None
        return await self._request("POST", "/api/account/add", json={"email": email})

    # ---------------- 自动生成邮箱 ----------------

    async def list_domains(self, size: int = 50) -> list[str]:
        """从已有邮箱里推断可用域名（skymail 没有专门的域名列表接口）。"""
        accounts = self._accounts if self._accounts is not None else await self.list_accounts(size)
        domains: list[str] = []
        for acc in accounts:
            email = str(acc.get("email") or "").strip().lower()
            if "@" in email:
                dom = email.rsplit("@", 1)[1]
                if dom and dom not in domains:
                    domains.append(dom)
        return domains

    async def create_random_email(
        self,
        domain: str | None = None,
        attempts: int = 12,
    ) -> dict:
        """随机生成一个邮箱地址并通过 POST /api/account/add 创建。

        返回 {"email": ..., "accountId": ...}
        """
        await self.login()
        domains = await self.list_domains()
        if not domains:
            raise SkymailError(
                "无法推断可用邮箱域名：skymail 账号下没有任何已有邮箱。"
                "请先在 skymail 后台添加一个邮箱，或在控制台手动指定域名。"
            )
        dom = (domain or random.choice(domains)).strip().lower()

        last_err: Exception | None = None
        for _ in range(attempts):
            email = f"{_random_local_part()}@{dom}"
            try:
                acc = await self.add_account(email)
            except SkymailError as exc:
                msg = str(exc)
                last_err = exc
                if "人机验证" in msg or "验证" in msg and "token" in msg.lower():
                    raise SkymailError(
                        f"skymail 开启了「添加邮箱」人机验证，无法自动建邮箱：{msg}"
                    ) from exc
                # 多半是重名，换一个前缀重试
                continue
            if acc is not None and acc.get("addVerifyOpen"):
                raise SkymailError(
                    "skymail 开启了「添加邮箱」人机验证，无法自动建邮箱"
                )
            self._accounts = None
            return {
                "email": email,
                "accountId": (acc or {}).get("accountId"),
            }
        raise SkymailError(f"连续 {attempts} 次都无法创建邮箱：{last_err}")

    async def ensure_account(self, email: str) -> dict:
        """确保目标邮箱存在于 skymail 账号下，返回 account 记录。"""
        acc = await self.find_account(email)
        if acc:
            return acc
        created = await self.add_account(email)
        return created or {"email": email}

    # ---------------- 邮件 ----------------

    async def list_emails(
        self,
        account_id: int,
        size: int = 10,
        full: int = 1,
    ) -> dict:
        await self.login()
        return await self._request(
            "GET",
            "/api/email/list",
            params={"accountId": account_id, "size": size, "full": full},
        )

    async def list_all_emails(
        self,
        email: str,
        size: int = 10,
        full: int = 1,
    ) -> dict:
        """管理接口：按收发件邮箱查全局邮件（管理员账号可用）。"""
        await self.login()
        return await self._request(
            "GET",
            "/api/allEmail/list",
            params={
                "type": "receive",
                "accountEmail": email,
                "size": size,
                "full": full,
            },
        )

    async def latest_email_id(self, account_id: int) -> int:
        data = await self.list_emails(account_id, size=1, full=0)
        latest = (data or {}).get("latestEmail") or {}
        return int(latest.get("emailId") or 0)

    async def poll_once(self, account_id: int, size: int = 8) -> list[dict]:
        data = await self.list_emails(account_id, size=size, full=1)
        return list((data or {}).get("list") or [])

    async def wait_for_code(
        self,
        fetch,
        seen_ids: set[int],
        timeout: float = 240.0,
        interval: float = 3.0,
        on_tick=None,
    ) -> tuple[str, dict]:
        """轮询等待新邮件里的 6 位验证码。fetch 是 async () -> list[dict]。

        返回 (code, email_dict)。
        """
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                mails = await fetch()
                for mail in mails:
                    mid = int(mail.get("emailId") or 0)
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    code = extract_code(
                        str(mail.get("subject") or ""),
                        str(mail.get("text") or ""),
                        strip_html(str(mail.get("content") or "")),
                    )
                    if code:
                        return code, mail
                last_err = None
            except Exception as exc:  # 网络抖动重试
                last_err = exc
            if on_tick:
                await on_tick()
            await asyncio.sleep(interval)
        hint = f"（最后一次错误：{last_err}）" if last_err else ""
        raise SkymailError(f"等待验证码超时（{int(timeout)}s）{hint}")
