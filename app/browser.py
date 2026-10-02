"""浏览器生命周期管理（进程级单例 Browser，任务级独立 Context）。

支持两种内核，由 `browser_engine` 切换：

| 内核 | 说明 |
|---|---|
| `camoufox`（默认） | 基于 Firefox 152 的反检测内核，指纹在**浏览器层**伪造，每个 context 一套独立身份 |
| `chromium` | Playwright 自带 Chromium + `app/stealth.py` 的 JS 层抹特征 |

两者互斥：Camoufox 自己就把指纹做完了，再往上叠一层面向 Chromium 的
JS 伪装（`window.chrome`、`PluginArray`、ANGLE 显卡串）反而会**自相矛盾**
（Firefox 不该有 `window.chrome`），所以 stealth 脚本只在 chromium 下注入。
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
    PLAYWRIGHT_CLEANUP_JS,
    build_client_hint_headers,
    build_stealth_js,
    build_user_agent,
    chrome_major,
)

log = logging.getLogger("muse.browser")

ENGINE_CAMOUFOX = "camoufox"
ENGINE_CHROMIUM = "chromium"


def _accept_languages(settings: dict) -> list[str]:
    """构造 Accept-Language 列表。真浏览器很少只带一种语言。"""
    base = (settings.get("locale") or "zh-CN").strip() or "zh-CN"
    if base.lower().startswith("zh"):
        return ["zh-CN", "zh", "en-US", "en"]
    return [base, "en-US", "en"]


class BrowserManager:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._key: tuple | None = None
        self._engine: str = ENGINE_CAMOUFOX
        self._chrome_major: str = FALLBACK_MAJOR

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def _engine_of(self, settings: dict) -> str:
        raw = str(settings.get("browser_engine") or ENGINE_CAMOUFOX).strip().lower()
        return raw if raw in (ENGINE_CAMOUFOX, ENGINE_CHROMIUM) else ENGINE_CAMOUFOX

    async def start(self, settings: dict) -> Browser:
        engine = self._engine_of(settings)
        headless = bool(settings.get("headless", True))
        slow_mo = int(settings.get("slow_mo", 0) or 0)
        key = (engine, headless, slow_mo)

        async with self._lock:
            if self._browser and self._browser.is_connected() and self._key == key:
                return self._browser
            if self._browser:
                log.info("浏览器配置变化，重启内核（%s）", engine)
                await self._close_locked()

            self._key = key
            self._engine = engine
            self._pw = await async_playwright().start()

            if engine == ENGINE_CAMOUFOX:
                self._browser = await self._launch_camoufox(settings)
            else:
                self._browser = await self._launch_chromium(settings)
            return self._browser

    async def _launch_camoufox(self, settings: dict) -> Browser:
        try:
            from camoufox.async_api import AsyncNewBrowser
            from camoufox.utils import launch_options
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 camoufox。请 `pip install camoufox` 并 "
                "`python -m camoufox fetch` 下载内核，"
                "或把 browser_engine 改回 chromium。"
            ) from exc

        headless = bool(settings.get("headless", True))
        # Linux 上 'virtual' 会用 Xvfb 跑真实渲染，比原生 headless 更不容易被识别
        if headless and str(settings.get("camoufox_headless_mode") or "") == "virtual":
            headless = "virtual"  # type: ignore[assignment]

        locales = _accept_languages(settings)

        opts = launch_options(
            headless=headless,
            os=str(settings.get("camoufox_os") or "windows"),
            humanize=bool(settings.get("camoufox_humanize", True)),
            # 多语言列表只能在 launch 阶段传（映射到 intl.accept_languages），
            # context 的 locale 参数只收字符串
            locale=locales,
            window=(
                int(settings.get("viewport_w", 1280)),
                int(settings.get("viewport_h", 820)),
            ),
            main_world_eval=True,
            # navigator.languages 取自这个 pref；只给一种语言是弱特征
            firefox_user_prefs={"intl.accept_languages": ",".join(locales)},
        )
        browser = await AsyncNewBrowser(self._pw, **opts)
        log.info(
            "camoufox 已启动: %s（os=%s humanize=%s headless=%s）",
            browser.version,
            settings.get("camoufox_os") or "windows",
            settings.get("camoufox_humanize", True),
            headless,
        )
        return browser

    async def _launch_chromium(self, settings: dict) -> Browser:
        browser = await self._pw.chromium.launch(  # type: ignore[union-attr]
            headless=bool(settings.get("headless", True)),
            slow_mo=int(settings.get("slow_mo", 0) or 0),
            args=list(LAUNCH_ARGS),
            # Playwright 默认会加 --enable-automation，
            # 那正是 navigator.webdriver 的来源，必须去掉
            ignore_default_args=["--enable-automation"],
        )
        self._chrome_major = chrome_major(browser.version)
        log.info(
            "chromium 已启动: %s（伪装大版本 %s，已剔除 --enable-automation）",
            browser.version,
            self._chrome_major,
        )
        return browser

    @property
    def browser(self) -> Browser:
        if not self._browser:
            raise RuntimeError("BrowserManager 尚未 start()")
        return self._browser

    @property
    def engine(self) -> str:
        return self._engine

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    async def new_context(
        self,
        settings: dict,
        storage_state: Any = None,
        proxy: dict | None = None,
    ) -> BrowserContext:
        """proxy 显式传入时优先（Resin 正向代理走这里）。"""
        browser = await self.start(settings)
        if self._engine == ENGINE_CAMOUFOX:
            return await self._camoufox_context(browser, settings, storage_state, proxy)
        return await self._chromium_context(browser, settings, storage_state, proxy)

    async def _camoufox_context(
        self,
        browser: Browser,
        settings: dict,
        storage_state: Any,
        proxy: dict | None,
    ) -> BrowserContext:
        from camoufox.async_api import AsyncNewContext

        viewport = {
            "width": int(settings.get("viewport_w", 1280)),
            "height": int(settings.get("viewport_h", 820)),
        }
        # 真浏览器一般带多个备选语言，只给一个反而奇怪
        locales = _accept_languages(settings)

        kwargs: dict[str, Any] = {
            "viewport": viewport,
            "locale": locales[0],          # context 只接受字符串
            "device_scale_factor": 1,
        }
        if storage_state:
            kwargs["storage_state"] = storage_state

        use_geoip = bool(settings.get("camoufox_geoip", True)) and proxy is not None
        if not use_geoip:
            # 不让 Camoufox 去查出口 IP 的时区，直接用配置值
            kwargs["timezone_id"] = settings.get("timezone") or "Asia/Shanghai"

        os_name = str(settings.get("camoufox_os") or "windows")
        try:
            context = await AsyncNewContext(
                browser, os=os_name, proxy=proxy, **kwargs
            )
        except Exception as exc:
            if not use_geoip:
                raise
            # 查不到出口 IP 的地理信息就退回配置的时区，不要让整个任务挂掉
            log.warning("按出口 IP 推导时区失败（%s），改用配置时区", exc)
            kwargs["timezone_id"] = settings.get("timezone") or "Asia/Shanghai"
            context = await AsyncNewContext(
                browser, os=os_name, proxy=proxy, **kwargs
            )

        context.set_default_timeout(int(settings.get("step_timeout", 60)) * 1000)
        context.set_default_navigation_timeout(60_000)
        # Playwright 自己注入的全局键两种内核都要清
        await context.add_init_script(PLAYWRIGHT_CLEANUP_JS)
        await context.add_init_script(HELPERS_JS)
        return context

    async def _chromium_context(
        self,
        browser: Browser,
        settings: dict,
        storage_state: Any,
        proxy: dict | None,
    ) -> BrowserContext:
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

        context = await browser.new_context(**ctx_kwargs)
        context.set_default_timeout(int(settings.get("step_timeout", 60)) * 1000)
        context.set_default_navigation_timeout(60_000)

        if stealth_on:
            await context.add_init_script(build_stealth_js(major))
        await context.add_init_script(PLAYWRIGHT_CLEANUP_JS)
        await context.add_init_script(HELPERS_JS)
        return context

    # ------------------------------------------------------------------

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
        self._key = None

    async def stop(self) -> None:
        async with self._lock:
            await self._close_locked()


manager = BrowserManager()
