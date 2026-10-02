import asyncio, os, sys
from playwright.async_api import async_playwright
from camoufox.async_api import AsyncNewBrowser, AsyncNewContext
from camoufox.utils import launch_options

PROBE = """() => { try {
  const c = document.createElement('canvas');
  const g = c.getContext('webgl') || c.getContext('experimental-webgl');
  if (!g) return 'null';
  const d = g.getExtension('WEBGL_debug_renderer_info');
  return d ? String(g.getParameter(d.UNMASKED_RENDERER_WEBGL)) : 'ok(no ext)';
} catch(e) { return 'err:' + e.message; } }"""

async def run(label, headless, prefs=None, env=None):
    old = {k: os.environ.get(k) for k in (env or {})}
    if env:
        os.environ.update(env)
    try:
        pw = await async_playwright().start()
        o = launch_options(headless=headless, os="windows")
        if prefs:
            o.setdefault("firefox_user_prefs", {}).update(prefs)
        b = await AsyncNewBrowser(pw, **o)
        ctx = await AsyncNewContext(b, os="windows")
        p = await ctx.new_page()
        await p.goto("about:blank")
        r = await p.evaluate(PROBE)
        print(f"  {label:<34} webgl = {r}")
        await b.close(); await pw.stop()
    except Exception as e:
        print(f"  {label:<34} 失败: {type(e).__name__} {str(e)[:70]}")
    finally:
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

async def main():
    await run("headless=True (基线)", True)
    await run("headless=True + LIBGL_ALWAYS_SOFTWARE", True,
              env={"LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe"})
    await run("headless=True + webgl.force-enabled", True,
              prefs={"webgl.force-enabled": True, "webgl.disabled": False,
                     "gfx.webrender.all": True, "webgl.out-of-process": False},
              env={"LIBGL_ALWAYS_SOFTWARE": "1"})
    await run("headless='virtual' (Xvfb)", "virtual",
              env={"LIBGL_ALWAYS_SOFTWARE": "1"})

asyncio.run(main())
