"""针对真实 muse.ai 的选择器/流程探针（不提交邮箱，只验证定位能力）。

用法：python tools/probe_muse.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import manager  # noqa: E402

from app.selectors import SELECTORS, TEXTS  # noqa: E402

SETTINGS = {
    "headless": True,
    "viewport_w": 1280,
    "viewport_h": 820,
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
    "step_timeout": 30,
}


async def main() -> None:
    ctx = await manager.new_context(SETTINGS)
    page = await ctx.new_page()
    page.set_default_timeout(30000)

    print("→ 打开 https://muse.ai/")
    await page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(4000)
    print("   title:", await page.title())

    info = await page.evaluate("() => window.__museHelpers.describe()")
    print("   describe:", json.dumps(
        {k: (v[:120] + "…" if isinstance(v, str) and len(v) > 120 else v)
         for k, v in info.items()}, ensure_ascii=False))

    buttons = await page.evaluate("""() => {
        const h = window.__museHelpers;
        return h.deepAll('button, [role="button"], a').filter(h.visible)
                .map(e => h.text(e)).filter(Boolean).slice(0, 40);
    }""")
    print("   可见按钮:", buttons)

    # 找「使用手机号或邮箱」
    clicked = False
    for label in TEXTS["expand_login"]:
        try:
            loc = page.get_by_role("button", name=label, exact=True)
            if await loc.count() and await loc.first.is_visible():
                await loc.first.click(timeout=8000)
                clicked = True
                print(f"   ✓ 已点击「{label}」")
                break
        except Exception as exc:
            print(f"   role 定位 {label} 失败: {type(exc).__name__}")
        try:
            ok = await page.evaluate(
                "([s, l]) => window.__museHelpers.clickByText(s, l, true)",
                ["button, [role='button'], a", label],
            )
            if ok:
                clicked = True
                print(f"   ✓ JS 点击「{label}」")
                break
        except Exception as exc:
            print(f"   JS 定位 {label} 失败: {type(exc).__name__}")
    if not clicked:
        print("   ✗ 没找到展开按钮（可能页面已改版或需要 JS 等待）")

    await page.wait_for_timeout(2500)

    for key in ("email_input", "submit"):
        found = None
        for sel in SELECTORS[key]:
            try:
                loc = page.locator(sel)
                n = await loc.count()
            except Exception:
                continue
            for i in range(min(n, 5)):
                try:
                    if await loc.nth(i).is_visible():
                        found = sel
                        break
                except Exception:
                    continue
            if found:
                break
        print(f"   {key}: {'✓ ' + found if found else '✗ 未找到'}")

    shot = Path(__file__).parent.parent / "probe-muse.png"
    await page.screenshot(path=str(shot), full_page=False)
    print("   截图:", shot)

    await ctx.close()
    await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
