"""Resin 接入自测。

起一个假的 Resin 服务，验证：
  1. skymail 的每个请求确实被改写成了反代 URL
  2. X-Resin-Account 头传的是正确的身份（账号级操作 vs 任务级操作）
  3. 建邮箱那一步用的是「新邮箱」作为身份
  4. 正向代理的凭据 / URL / Playwright 参数符合 Resin 规范

用法：python tools/test_resin.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.resin import ResinConfig, resin_account_ctx  # noqa: E402
from app.skymail import SkymailClient  # noqa: E402

RECORDS: list[dict] = []


class FakeResin(BaseHTTPRequestHandler):
    """只做两件事：记录请求，按路径返回假的 skymail 响应。"""

    protocol_version = "HTTP/1.1"

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        RECORDS.append({
            "method": self.command,
            "path": self.path,
            "account": self.headers.get("X-Resin-Account"),
            "body": raw.decode() if raw else "",
        })
        # 目标路径在 /<token>/<platform>/<proto>/<host>/... 之后
        target = self.path
        if "/api/login" in target:
            return self._reply({"code": 200, "message": "success",
                                "data": {"token": "FAKE-TOKEN"}})
        if "/api/account/add" in target:
            return self._reply({"code": 200, "message": "success",
                                "data": {"accountId": 4242, "email": "new@d.com"}})
        if "/api/account/list" in target:
            return self._reply({"code": 200, "message": "success",
                                "data": [{"accountId": 1, "email": "admin@d.com"}]})
        return self._reply({"code": 200, "message": "success", "data": []})

    def do_GET(self) -> None:      # noqa: N802
        self._handle()

    def do_POST(self) -> None:     # noqa: N802
        self._handle()

    def log_message(self, *args) -> None:
        pass


PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail and not ok else ""))


def last(prefix: str) -> dict | None:
    for r in reversed(RECORDS):
        if prefix in r["path"]:
            return r
    return None


async def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeResin)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    resin = ResinConfig(f"http://127.0.0.1:{port}/my-token", "Default")
    print(f"假 Resin 监听在 127.0.0.1:{port}\n")

    # ---------- 1. 配置解析 ----------
    print("[1] 配置解析")
    check("base", resin.base == f"http://127.0.0.1:{port}")
    check("prefix", resin.prefix == f"http://127.0.0.1:{port}/my-token")
    check("token", resin.token == "my-token")
    check(
        "反代 URL 与规范一致",
        resin.reverse_url("https://api.example.com/healthz")
        == f"http://127.0.0.1:{port}/my-token/Default/https/api.example.com/healthz",
    )
    u, p = resin.proxy_credentials("Tom")
    check("正代凭据 Platform.Account", (u, p) == ("Default.Tom", "my-token"), f"{u!r},{p!r}")
    check(
        "Playwright proxy",
        resin.playwright_proxy("Tom")
        == {"server": f"http://127.0.0.1:{port}", "username": "Default.Tom",
            "password": "my-token"},
    )
    check(
        "Account 含 @ 时正代 URL 转义",
        "%40" in resin.forward_proxy_url("a@b.com"),
        resin.forward_proxy_url("a@b.com"),
    )

    # ---------- 2. 反向代理实际生效 ----------
    print("\n[2] skymail 走反向代理")
    client = SkymailClient("https://skymail.ink", "admin@d.com", "pw", resin=resin)
    await client.login()
    r = last("/api/login")
    check("登录请求被改写",
          bool(r) and r["path"].startswith("/my-token/Default/https/skymail.ink/api/login"),
          str(r))
    check("登录用默认身份（skymail 主账号）", r and r["account"] == "admin@d.com",
          str(r and r["account"]))

    await client.list_accounts()
    r = last("/api/account/list")
    check("枚举邮箱用默认身份", r and r["account"] == "admin@d.com", str(r and r["account"]))

    await client.poll_once(7, size=3)
    r = last("/api/email/list")
    check("无任务上下文时收信也用默认身份", r and r["account"] == "admin@d.com",
          str(r and r["account"]))
    check("查询参数被保留", r and "accountId=7" in r["path"] and "size=3" in r["path"],
          str(r and r["path"]))

    # ---------- 3. 任务身份切换 ----------
    print("\n[3] 任务级请求切到任务邮箱身份")
    task_mail = "fse53@d.com"
    with resin_account_ctx(task_mail):
        await client.poll_once(9, size=2)
    r = last("/api/email/list")
    check("任务内收信使用任务邮箱身份", r and r["account"] == task_mail,
          str(r and r["account"]))

    await client.poll_once(9, size=2)
    r = last("/api/email/list")
    check("离开上下文后回到默认身份", r and r["account"] == "admin@d.com",
          str(r and r["account"]))

    # ---------- 4. 建邮箱用新邮箱作为身份 ----------
    print("\n[4] 建邮箱：用「新邮箱」当身份（登录前就已确定）")
    RECORDS.clear()
    created = await client.create_random_email()
    r = last("/api/account/add")
    check("新邮箱已创建", bool(created.get("email")), str(created))
    check("account/add 的身份 == 新邮箱", r and r["account"] == created["email"],
          f"{r and r['account']} vs {created.get('email')}")
    check("请求体里也是这个邮箱", r and created["email"] in r["body"], str(r and r["body"]))
    check("建邮箱走的是反代路径",
          r and "/Default/https/skymail.ink/api/account/add" in r["path"], str(r and r["path"]))

    # ---------- 5. 浏览器走正向代理 ----------
    print("\n[5] 浏览器（Playwright）走正向代理")
    import app.browser as bmod

    captured: dict = {}

    class FakeContext:
        def set_default_timeout(self, *_a) -> None:
            pass

        def set_default_navigation_timeout(self, *_a) -> None:
            pass

        async def add_init_script(self, *_a) -> None:
            pass

    class FakeBrowser:
        async def new_context(self, **kw):
            captured.update(kw)
            return FakeContext()

    bm = bmod.BrowserManager()

    async def fake_start(headless: bool = True, slow_mo: int = 0):
        bm._browser = FakeBrowser()
        return bm._browser

    bm.start = fake_start            # type: ignore[method-assign]
    settings = {"headless": True, "slow_mo": 0, "viewport_w": 1280,
                "viewport_h": 820, "locale": "zh-CN", "timezone": "Asia/Shanghai"}

    await bm.new_context(settings, proxy=resin.playwright_proxy("fse53@d.com"))
    p = captured.get("proxy")
    check("浏览器 context 带上了 Resin 正代", p is not None and
          p.get("username") == "Default.fse53@d.com" and
          p.get("password") == "my-token" and
          p.get("server") == f"http://127.0.0.1:{port}", str(p))

    captured.clear()
    await bm.new_context(settings, proxy=None)
    check("不传 proxy 且没配 MUSE_PROXY 时不用代理", captured.get("proxy") is None,
          str(captured.get("proxy")))

    # ---------- 6. 关闭 Resin 时直连 ----------
    print("\n[6] 未启用 Resin 时直连")
    plain = SkymailClient("https://skymail.ink", "admin@d.com", "pw")
    RECORDS.clear()
    try:
        await plain.login()
    except Exception:
        pass  # 直连真实的 skymail.ink 可能失败，不重要
    check("没有 Resin 时不带 X-Resin-Account 头",
          all(r["account"] is None for r in RECORDS), str(RECORDS[:1]))

    await client.close()
    await plain.close()
    srv.shutdown()

    print(f"\n{'=' * 46}")
    print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败：" + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
