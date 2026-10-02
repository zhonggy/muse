"""Playwright 浏览器生命周期管理（进程级单例 Browser，任务级独立 Context）。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .js_helpers import HELPERS_JS

log = logging.getLogger("muse.browser")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(p);
}
"""

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BrowserManager:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._headless: bool | None = None
        self._slow_mo: int | None = None

    async def start(self, headless: bool = True, slow_mo: int = 0) -> Browser:
        async with self._lock:
            changed = (
                self._headless is not None
                and (self._headless != bool(headless) or self._slow_mo != int(slow_mo))
            )
            if changed:
                log.info("headless/slow_mo 变化，重启 chromium")
                await self._close_locked()
            if self._browser and self._browser.is_connected():
                return self._browser
            self._pw = await async_playwright().start()
            self._headless = bool(headless)
            self._slow_mo = int(slow_mo)
            self._browser = await self._pw.chromium.launch(
                headless=bool(headless),
                slow_mo=int(slow_mo) or 0,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--window-size=1280,820",
                ],
            )
            log.info("chromium 已启动: %s", self._browser.version)
            return self._browser

    @property
    def browser(self) -> Browser:
        if not self._browser:
            raise RuntimeError("BrowserManager 尚未 start()")
        return self._browser

    async def new_context(
        self,
        settings: dict,
        storage_state: Any = None,
        proxy: dict | None = None,
    ) -> BrowserContext:
        """proxy 显式传入时优先（Resin 正向代理走这里）。"""
        await self.start(
            headless=bool(settings.get("headless", True)),
            slow_mo=int(settings.get("slow_mo", 0) or 0),
        )
        ctx_kwargs: dict[str, Any] = {
            "viewport": {
                "width": int(settings.get("viewport_w", 1280)),
                "height": int(settings.get("viewport_h", 820)),
            },
            "locale": settings.get("locale") or "zh-CN",
            "timezone_id": settings.get("timezone") or "Asia/Shanghai",
            "user_agent": settings.get("user_agent") or DEFAULT_UA,
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "java_script_enabled": True,
            "ignore_https_errors": True,
            "accept_downloads": False,
        }
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        if proxy:
            ctx_kwargs["proxy"] = proxy
        else:
            plain = (settings.get("proxy") or "").strip()
            if plain:
                ctx_kwargs["proxy"] = {"server": plain}

        context = await self.browser.new_context(**ctx_kwargs)
        context.set_default_timeout(int(settings.get("step_timeout", 60)) * 1000)
        context.set_default_navigation_timeout(60_000)
        await context.add_init_script(STEALTH_JS)
        await context.add_init_script(HELPERS_JS)
        return context

    async def _close_locked(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._headless = None
        self._slow_mo = None

    async def stop(self) -> None:
        async with self._lock:
            await self._close_locked()


manager = BrowserManager()
