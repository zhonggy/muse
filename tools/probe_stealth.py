"""反检测自测：把常见的自动化特征逐项打出来，看还漏不漏。

用法：
    python tools/probe_stealth.py            # 默认 camoufox
    python tools/probe_stealth.py chromium   # 测 chromium + JS 层伪装

两种内核要检查的东西不一样：
  - chromium：靠 app/stealth.py 在 JS 层抹特征，要逐条验证伪装是否到位
  - camoufox：指纹在浏览器层伪造，UA 是 **Firefox**，
    所以 `window.chrome` / `PluginArray` / Client Hints 这些
    Chrome 专有特征**本就不该存在**，检查它们没有意义
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import browser as browser_mod  # noqa: E402

#: 在页面里跑的检测脚本。返回 [{key, value, expect}] 形态的原始数据
DETECT_JS = r"""
() => {
  const out = [];
  const add = (key, value, expect) => out.push({ key, value, expect });

  // --- 1. 自动化标记 ---
  add('navigator.webdriver', String(navigator.webdriver), 'false');

  // --- 2. 引擎自洽：UA 说的引擎要和实际能力对上 ---
  const isFF = /Firefox\//.test(navigator.userAgent);
  const isChrome = /Chrome\//.test(navigator.userAgent);
  add('UA 含 Headless', /headless/i.test(navigator.userAgent) ? 'YES' : 'no', 'no');
  add('UA 引擎', isFF ? 'Firefox' : (isChrome ? 'Chrome' : '?'), 'Firefox|Chrome');
  add('window.chrome 与 UA 自洽',
      isFF ? (typeof window.chrome === 'undefined' ? 'yes' : 'NO')
           : (typeof window.chrome === 'object' ? 'yes' : 'NO'),
      'yes');

  // --- 3. 基础环境 ---
  add('navigator.plugins.length', navigator.plugins.length, '>0');
  add('navigator.mimeTypes.length', navigator.mimeTypes.length, '>0');
  add('navigator.languages', JSON.stringify(navigator.languages), '非空');
  add('navigator.platform', navigator.platform, '非空');
  add('navigator.hardwareConcurrency', navigator.hardwareConcurrency, '>=2');

  // 触屏声明要看「自洽」而不是只看数值。
  // real headless 的经典特征是：maxTouchPoints > 0 却没有任何触屏媒体查询。
  // Camoufox 声称有触屏时会同时补上 (any-pointer: coarse)，这是自洽的。
  const mtp = navigator.maxTouchPoints;
  const coarse = matchMedia('(any-pointer: coarse)').matches;
  add('maxTouchPoints 数值', mtp, '>=0');
  add('maxTouchPoints 与触屏声明自洽',
      (mtp === 0) === (!coarse) ? 'yes' : 'NO', 'yes');
  add('screen.width', screen.width, '>0');
  add('screen.colorDepth', screen.colorDepth, '24');
  add('document.hasFocus()', String(document.hasFocus()), 'true');
  add('时区', Intl.DateTimeFormat().resolvedOptions().timeZone || '空', '非空');
  add('语言', Intl.DateTimeFormat().resolvedOptions().locale || '空', '非空');

  // --- 4. WebGL（软件渲染是 headless 最硬的招牌）---
  try {
    const cv = document.createElement('canvas');
    const gl = cv.getContext('webgl') || cv.getContext('experimental-webgl');
    if (gl) {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      const ur = dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) : '(no ext)';
      add('webgl.UNMASKED_RENDERER', ur, '非软件渲染');
      add('webgl 软渲染泄漏',
          /swiftshader|llvmpipe|software|mesa/i.test(ur) ? 'YES' : 'no', 'no');
    } else {
      add('webgl', 'unavailable', 'available');
    }
  } catch (e) { add('webgl', 'error', 'available'); }

  // --- 5. 自动化残留 ---
  const KEYS = /playwright|__pw|__driver|cdc_|selenium|_phantom|nightmare|webdriver|juggler/i;
  const wKeys = Object.keys(window).filter((k) => KEYS.test(k));
  add('window 上的自动化残留键', wKeys.length ? wKeys.join(',') : 'none', 'none');

  // --- 6. 视口/屏幕自洽（窗口不能比屏幕大）---
  add('窗口不超过屏幕',
      window.outerWidth <= screen.width && window.outerHeight <= screen.height
        ? 'yes' : 'NO', 'yes');

  return out;
}
"""


def _verdict(value: str, expect: str) -> str:
    """返回 '' 表示通过，' ✗' 表示不通过。"""
    v = str(value)
    if expect == ">0":
        return "" if (v.isdigit() and int(v) > 0) else " ✗"
    if expect == ">=2":
        return "" if (v.isdigit() and int(v) >= 2) else " ✗"
    if expect == "非空":
        return "" if v and v not in ("空", '""', "[]") else " ✗"
    if expect == "no":
        return "" if v == "no" else " ✗"
    if expect == "none":
        return "" if v == "none" else " ✗"
    if expect == "yes":
        return "" if v == "yes" else " ✗"
    if expect.startswith("非软件"):
        return "" if not __import__("re").search(
            r"swiftshader|llvmpipe|software|mesa", v, __import__("re").I) else " ✗"
    if expect == ">=0":
        return "" if v.isdigit() else " ✗"
    if expect.startswith("yes 或"):
        return "" if v.startswith("yes") else " ✗"
    if "|" in expect:
        return "" if v in expect.split("|") else " ✗"
    return "" if v == expect else " ✗"


async def main() -> int:
    engine = sys.argv[1] if len(sys.argv) > 1 else "camoufox"
    settings = {
        "browser_engine": engine,
        "headless": True,
        "slow_mo": 0,
        "viewport_w": 1280,
        "viewport_h": 820,
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "step_timeout": 30,
    }
    ctx = await browser_mod.manager.new_context(settings)
    page = await ctx.new_page()
    await page.goto("about:blank")

    real_engine = browser_mod.manager.engine
    print(f"内核: {real_engine}  版本: {browser_mod.manager.browser.version}\n")

    rows = await page.evaluate(DETECT_JS)
    if real_engine == "chromium":
        rows.append({
            "key": "permissions.query 是原生代码",
            "value": await page.evaluate(
                "() => /native code/.test(Function.prototype.toString"
                ".call(navigator.permissions.query)) ? 'yes' : 'NO'"),
            "expect": "yes",
        })

    print(f"{'检测项':<34} {'实际值':<44} 期望")
    print("-" * 98)
    bad = 0
    for r in rows:
        val = str(r["value"])
        if len(val) > 42:
            val = val[:39] + "..."
        mark = _verdict(r["value"], str(r["expect"]))
        bad += 1 if mark else 0
        print(f"{r['key']:<34} {val:<44} {r['expect']}{mark}")

    print("-" * 98)
    print(f"不通过 {bad} / {len(rows)}")
    if bad:
        # 单独列一行，方便日志里直接 grep 到失败项
        names = [r["key"] for r in rows if _verdict(r["value"], str(r["expect"]))]
        print("失败项：" + " | ".join(names))

    await ctx.close()
    await browser_mod.manager.stop()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
