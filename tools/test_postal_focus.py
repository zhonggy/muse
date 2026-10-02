"""邮编填写自测。

关键行为（实测）：结账页在填完 CVV 后**会把焦点自动移到邮编输入框**。
这时直接敲键盘就行 —— 多一次点击反而会把焦点移走，后面那串数字
就不知道敲到哪去了。

四种情况都跑一遍：
  A. 焦点自动跳过去   -> 直接键盘输入
  B. 焦点没跳         -> 走属性兜底定位
  C. 探针标了 postal  -> 走重探定位
  D. 页面没有邮编框   -> 快速跳过，不能卡到 30s 超时

用法：python tools/test_postal_focus.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import browser as browser_mod  # noqa: E402
from app.muse_flow import MuseFlow  # noqa: E402

S = {
    "browser_engine": "camoufox",
    "headless": True,
    "slow_mo": 0,
    "viewport_w": 1280,
    "viewport_h": 820,
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
    "step_timeout": 30,
    "checkout_postal": "97538",
}

#: 填完 CVV 自动把焦点移到邮编框
PAGE_AUTO = """<!doctype html><html><body>
<input id="cvc" autocomplete="cc-csc" placeholder="CVV"
       style="position:absolute;left:20px;top:40%;width:180px">
<input id="zip" autocomplete="postal-code" placeholder="ZIP"
       style="position:absolute;left:220px;top:40%;width:180px">
<script>
document.getElementById('cvc').addEventListener('input', function () {
  if (this.value.length >= 3) document.getElementById('zip').focus();
});
</script></body></html>"""

#: 焦点不动
PAGE_PLAIN = """<!doctype html><html><body>
<input id="cvc" autocomplete="cc-csc" placeholder="CVV"
       style="position:absolute;left:20px;top:40%;width:180px">
<input id="zip" autocomplete="postal-code" placeholder="ZIP"
       style="position:absolute;left:220px;top:40%;width:180px">
</body></html>"""

PAGE_NO_POSTAL = "<html><body><input id='cvc' autocomplete='cc-csc'></body></html>"


class FakeFrame:
    """只为拿 page 引用 —— _fill_postal 需要 frame.page 来跨 frame 找焦点。"""

    def __init__(self, page, marked=()):
        self.page = page
        self._marked = list(marked)

    async def evaluate(self, *_args, **_kwargs):
        return {"marked": self._marked}

    def locator(self, sel):
        return self.page.locator(sel)


class FakeRT:
    """MuseFlow 需要的最小运行时。"""

    def __init__(self, page, settings):
        self.page = page
        self.settings = settings
        self.task = {"id": "t_test", "email": ""}

    async def log(self, msg, level="info"):
        print(f"      [{level}] {msg}")

    async def check_stop(self):
        return None


async def main() -> int:
    settings = dict(S)
    ctx = await browser_mod.manager.new_context(settings)
    page = await ctx.new_page()
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, ok: bool) -> None:
        (passed if ok else failed).append(name)
        print(f"  {'✓' if ok else '✗'} {name}")

    # ---- A ----
    print("\n[A] 填完 CVV 焦点自动跳到邮编框")
    await page.set_content(PAGE_AUTO)
    flow = MuseFlow(FakeRT(page, settings))
    await page.click("#cvc")
    await page.keyboard.type("123", delay=30)
    await page.wait_for_timeout(300)
    check("焦点确实已跳到 #zip",
          await page.evaluate("() => document.activeElement.id") == "zip")
    await flow._fill_postal(FakeFrame(page), "97538")
    check("邮编已填入", await page.input_value("#zip") == "97538")
    check("焦点没被点击带跑（仍在 #zip）",
          await page.evaluate("() => document.activeElement.id") == "zip")

    # ---- B ----
    print("\n[B] 焦点没跳，走属性兜底")
    await page.set_content(PAGE_PLAIN)
    flow2 = MuseFlow(FakeRT(page, settings))
    await page.click("#cvc")
    await page.keyboard.type("123", delay=30)
    await page.wait_for_timeout(200)
    check("焦点仍在 #cvc",
          await page.evaluate("() => document.activeElement.id") == "cvc")
    await flow2._fill_postal(FakeFrame(page), "97538")
    check("兜底后邮编也填上了", await page.input_value("#zip") == "97538")

    # ---- C ----
    print("\n[C] 探针标记了 postal 字段，走重探定位")
    await page.set_content(PAGE_PLAIN)
    await page.eval_on_selector(
        "#zip", "el => el.setAttribute('data-muse-fill','postal')")
    await page.click("#cvc")
    flow3 = MuseFlow(FakeRT(page, settings))
    await flow3._fill_postal(FakeFrame(page, ["postal"]), "97538")
    check("重探定位路径也能填上", await page.input_value("#zip") == "97538")

    # ---- D ----
    print("\n[D] 页面没有邮编框（应快速跳过而不是超时）")
    await page.set_content(PAGE_NO_POSTAL)
    flow4 = MuseFlow(FakeRT(page, settings))
    t0 = time.monotonic()
    await flow4._fill_postal(FakeFrame(page), "97538")
    dt = time.monotonic() - t0
    check(f"快速跳过（{dt:.1f}s，不该拖到 30s）", dt < 20)

    await ctx.close()
    await browser_mod.manager.stop()
    print("\n" + "=" * 46)
    print(f"  通过 {len(passed)} / {len(passed) + len(failed)}")
    if failed:
        print("  失败：" + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
