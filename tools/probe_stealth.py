"""反检测自测：把常见的自动化特征逐项打出来，看还漏不漏。

用法：
    python tools/probe_stealth.py

每一项都会打印「实际值」和「期望值」，✗ 的就是还在漏的。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import browser as browser_mod  # noqa: E402

SETTINGS = {
    "headless": True,
    "slow_mo": 0,
    "viewport_w": 1280,
    "viewport_h": 820,
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
    "step_timeout": 30,
}

#: 在页面里跑的检测脚本。返回 [{key, value, expect}] 形态的原始数据
DETECT_JS = r"""
() => {
  const out = [];
  const add = (key, value, expect) => out.push({ key, value, expect });

  // --- 1. 自动化标记 ---
  add('navigator.webdriver', String(navigator.webdriver), 'undefined');

  // --- 2. window.chrome ---
  const c = window.chrome;
  add('window.chrome', c ? 'object' : 'undefined', 'object');
  add('chrome.runtime', c && c.runtime ? 'object' : 'undefined', 'object');
  add('chrome.app', c && c.app ? 'object' : 'undefined', 'object');
  add('chrome.csi()', (c && c.csi && typeof c.csi === 'function')
      ? (JSON.stringify(c.csi()) !== '{}' ? 'object' : 'empty') : 'missing', 'object');
  add('chrome.loadTimes()', (c && c.loadTimes && typeof c.loadTimes === 'function')
      ? (JSON.stringify(c.loadTimes()) !== '{}' ? 'object' : 'empty') : 'missing', 'object');

  // --- 3. plugins / mimeTypes（headless 常为 0）---
  add('navigator.plugins.length', navigator.plugins.length, '>0');
  add('navigator.mimeTypes.length', navigator.mimeTypes.length, '>0');
  add('plugins[0].name', navigator.plugins[0] ? navigator.plugins[0].name : 'none',
      'PDF Viewer');
  add('pdfViewerEnabled', String(navigator.pdfViewerEnabled), 'true');

  // --- 4. permissions ---
  add('Notification.permission', Notification.permission, 'default');
  add('navigator.permissions', navigator.permissions ? 'object' : 'undefined', 'object');

  // --- 5. 环境一致性 ---
  add('navigator.languages', JSON.stringify(navigator.languages), '["zh-CN","zh","en"]');
  add('navigator.platform', navigator.platform, 'Win32');
  add('navigator.hardwareConcurrency', navigator.hardwareConcurrency, '>=2');
  add('navigator.deviceMemory', String(navigator.deviceMemory), '>=4');
  add('navigator.maxTouchPoints', navigator.maxTouchPoints, '0');
  add('navigator.connection', navigator.connection ? 'object' : 'undefined', 'object');
  add('navigator.userAgentData', navigator.userAgentData ? 'object' : 'undefined', 'object');

  // --- 6. 窗口尺寸（headless 常为 0）---
  add('window.outerWidth', window.outerWidth, '>0');
  add('window.outerHeight', window.outerHeight, '>0');
  add('screen.width', screen.width, '>0');
  add('screen.colorDepth', screen.colorDepth, '24');
  add('document.hasFocus()', String(document.hasFocus()), 'true');

  // --- 7. WebGL（SwiftShader 是 headless 的招牌）---
  try {
    const cv = document.createElement('canvas');
    const gl = cv.getContext('webgl') || cv.getContext('experimental-webgl');
    if (gl) {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      const vendor = gl.getParameter(gl.VENDOR);
      const renderer = gl.getParameter(gl.RENDERER);
      const uv = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : '(no ext)';
      const ur = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '(no ext)';
      add('webgl.VENDOR', String(vendor), 'WebKit');
      add('webgl.UNMASKED_VENDOR', String(uv), '非 (Google)');
      add('webgl.UNMASKED_RENDERER', String(ur), '非 SwiftShader');
      add('webgl.swiftShader泄漏', /swiftshader|llvmpipe|software/i.test(String(ur)) ? 'YES' : 'no', 'no');
    } else {
      add('webgl', 'unavailable', 'available');
    }
  } catch (e) { add('webgl', 'error:' + e.message, 'available'); }

  // --- 8. Playwright / CDP 残留 ---
  const pwKeys = Object.keys(window).filter((k) =>
    /playwright|__pw|__driver|cdc_|selenium|\$cdc|_phantom|nightmare|callSelenium/i.test(k));
  add('window 上的自动化残留键', pwKeys.length ? pwKeys.join(',') : 'none', 'none');
  const docKeys = Object.keys(document).filter((k) => /playwright|__pw|cdc_/i.test(k));
  add('document 上的自动化残留键', docKeys.length ? docKeys.join(',') : 'none', 'none');

  // --- 9. 原生函数是否被改过 ---
  const nf = navigator.permissions.query;
  const src = Function.prototype.toString.call(nf);
  add('permissions.query 是原生代码', /native code/.test(src) ? 'yes' : 'NO', 'yes');
  const fts = Function.prototype.toString;
  add('toString 自身也是原生代码',
      /native code/.test(Function.prototype.toString.call(fts)) ? 'yes' : 'NO', 'yes');
  const wd = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
  add('webdriver 描述符', wd ? (wd.get ? '有 getter' : '数据属性') : '不存在', '不存在或数据属性');

  // --- 11. Client Hints 与 UA 是否自洽 ---
  const uad = navigator.userAgentData;
  if (uad) {
    const uaMajor = (navigator.userAgent.match(/Chrome\/(\d+)/) || [])[1];
    const chMajor = (uad.brands.find((b) => b.brand === 'Google Chrome') || {}).version;
    add('UA 与 Client Hints 版本一致', uaMajor && chMajor && uaMajor === chMajor ? 'yes' : 'NO',
        'yes');
    add('userAgentData.platform', String(uad.platform), 'Windows');
  }

  // --- 12. plugins 是否像真的 ---
  add('plugins 是 PluginArray',
      Object.prototype.toString.call(navigator.plugins), '[object PluginArray]');
  add('mimeTypes 是 MimeTypeArray',
      Object.prototype.toString.call(navigator.mimeTypes), '[object MimeTypeArray]');
  add('plugins.namedItem 可用',
      typeof navigator.plugins.namedItem === 'function' ? 'yes' : 'NO', 'yes');

  // --- 10. headless UA 标记 ---
  add('UA 含 HeadlessChrome', /HeadlessChrome/i.test(navigator.userAgent) ? 'YES' : 'no', 'no');

  return out;
}
"""


async def main() -> int:
    ctx = await browser_mod.manager.new_context(SETTINGS)
    page = await ctx.new_page()
    await page.goto("about:blank")

    rows = await page.evaluate(DETECT_JS)

    # permissions.query 的返回要单独 await
    perm = await page.evaluate(
        "() => navigator.permissions.query({name:'notifications'})"
        ".then(r => r.state).catch(e => 'error:' + e.message)"
    )
    rows.append({"key": "permissions.query(notifications).state",
                 "value": perm, "expect": "default"})

    print(f"{'检测项':<34} {'实际值':<46} 期望")
    print("-" * 100)
    bad = 0
    for r in rows:
        val = str(r["value"])
        if len(val) > 44:
            val = val[:41] + "..."
        mark = ""
        exp = str(r["expect"])
        if exp.startswith("非"):
            if exp.replace("非", "") in str(r["value"]):
                mark = " ✗"
                bad += 1
        elif exp == ">0":
            if not (str(r["value"]).isdigit() and int(r["value"]) > 0):
                mark = " ✗"
                bad += 1
        elif exp == ">=2" or exp == ">=4":
            n = int(exp[2:])
            if not (str(r["value"]).isdigit() and int(r["value"]) >= n):
                mark = " ✗"
                bad += 1
        elif exp == "no":
            if str(r["value"]) != "no":
                mark = " ✗"
                bad += 1
        elif exp == "none":
            if str(r["value"]) != "none":
                mark = " ✗"
                bad += 1
        elif exp == "yes":
            if str(r["value"]) != "yes":
                mark = " ✗"
                bad += 1
        elif exp.startswith("不存在"):
            if "存在" in str(r["value"]) and "不存在" not in str(r["value"]):
                mark = " ✗"
                bad += 1
        elif str(r["value"]) != exp:
            mark = " ✗"
            bad += 1
        print(f"{r['key']:<34} {val:<46} {exp}{mark}")

    print("-" * 100)
    print(f"不通过 {bad} / {len(rows)}")

    await ctx.close()
    await browser_mod.manager.stop()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
