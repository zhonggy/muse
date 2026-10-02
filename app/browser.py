"""Playwright 浏览器生命周期管理（进程级单例 Browser，任务级独立 Context）。

反检测分两层，都在这里落地：
  - 启动参数：`app.stealth.LAUNCH_ARGS`，并剔除 Playwright 默认加的
    `--enable-automation`（就是它让页面能读到 navigator.webdriver）
  - 页面注入：`app.stealth.build_stealth_js()`，在文档创建前改写可探测特征
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .js_helpers import HELPERS_JS
from .stealth import (
    FALLBACK_MAJOR,
    LAUNCH_ARGS,
    build_client_hint_headers,
    build_stealth_js,
    build_user_agent,
    chrome_major,
)

log = logging.getLogger("muse.browser")


class BrowserManager:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._headless: bool | None = None
        self._slow_mo: int | None = None
        self._chrome_major: str = FALLBACK_MAJOR

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
                args=list(LAUNCH_ARGS),
                # Playwright 默认会加 --enable-automation，
                # 那正是 navigator.webdriver 的来源，必须去掉
                ignore_default_args=["--enable-automation"],
            )
            self._chrome_major = chrome_major(self._browser.version)
            log.info(
                "chromium 已启动: %s（伪装大版本 %s，已剔除 --enable-automation）",
                self._browser.version,
                self._chrome_major,
            )
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

        stealth_on = bool(settings.get("stealth", True))
        custom_ua = (settings.get("user_agent") or "").strip()

        # 大版本号以 UA 为准，保证 UA / Client Hints / JS 三者一致
        major = self._chrome_major
        if custom_ua:
            m = re.search(r"Chrome/(\d+)", custom_ua)
            if m:
                major = m.group(1)
        ua = custom_ua or build_user_agent(major)

        ctx_kwargs: dict[str, Any] = {
            "viewport": {
                "width": int(settings.get("viewport_w", 1280)),
                "height": int(settings.get("viewport_h", 820)),
            },
            "locale": settings.get("locale") or "zh-CN",
            "timezone_id": settings.get("timezone") or "Asia/Shanghai",
            "user_agent": ua,
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "java_script_enabled": True,
            "ignore_https_errors": True,
            "accept_downloads": False,
        }
        if stealth_on:
            # 请求头的 Client Hints 必须和 JS 里的 userAgentData 完全一致
            ctx_kwargs["extra_http_headers"] = build_client_hint_headers(major)

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

        if stealth_on:
            await context.add_init_script(build_stealth_js(major))
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
