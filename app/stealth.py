"""反检测（stealth）：抹掉自动化特征。

分两层：

1. **启动参数** —— 去掉 Chrome 自带的「被自动化控制」标记，
   并关掉一批会暴露无人值守状态的开关。

2. **页面注入** —— 在文档创建前改写 JS 层可探测的痕迹：
   `navigator.webdriver`、`window.chrome`、`navigator.plugins`、
   `permissions.query`、Client Hints、WebGL 渲染器、Playwright 残留键。

设计原则：**所有伪造值都从同一个 Chrome 大版本号派生**。
检测器最常抓的不是某个特征单独存在，而是几个特征之间自相矛盾
（UA 写 Chrome/131、Client Hints 写 130、WebGL 又报 SwiftShader）。
"""
from __future__ import annotations

import json
import re

#: 拿不到真实版本时的兜底大版本号
FALLBACK_MAJOR = "131"

#: 伪造的平台信息。改这些要保持一整套自洽（UA / platform / Client Hints）
PLATFORM = {
    "ua_platform": "Windows NT 10.0; Win64; x64",
    "navigator_platform": "Win32",
    "client_hint_platform": "Windows",
    "client_hint_platform_version": "15.0.0",
    "webgl_vendor": "Google Inc. (Intel)",
    "webgl_renderer": (
        "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    ),
}

#: GREASE 品牌。真实 Chrome 每次会话随机选一组，这里固定一组保证前后一致
GREASE_BRAND = "Not_A Brand"
GREASE_VERSION = "24"

#: 启动参数
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    # 关键：去掉 Chrome 自带的「被自动化控制」标记
    "--disable-blink-features=AutomationControlled",
    # 关掉无人值守环境才会有的状态
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-component-update",
    "--disable-background-networking",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-extensions",
    "--metrics-recording-only",
    "--mute-audio",
    "--force-color-profile=srgb",
    "--window-size=1280,820",
    "--lang=zh-CN",
]


def chrome_major(version: str | None) -> str:
    """从 Playwright 报的浏览器版本里取大版本号，如 131.0.6778.33 -> 131。"""
    if version:
        m = re.match(r"(\d+)", version.strip())
        if m:
            return m.group(1)
    return FALLBACK_MAJOR


def build_user_agent(major: str) -> str:
    return (
        f"Mozilla/5.0 ({PLATFORM['ua_platform']}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def build_client_hint_headers(major: str) -> dict[str, str]:
    """Sec-CH-UA 系列请求头，必须和 JS 里的 userAgentData 完全一致。"""
    ua = (
        f'"{GREASE_BRAND}";v="{GREASE_VERSION}", '
        f'"Chromium";v="{major}", "Google Chrome";v="{major}"'
    )
    return {
        "sec-ch-ua": ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{PLATFORM["client_hint_platform"]}"',
    }


def _brands(major: str) -> list[dict[str, str]]:
    return [
        {"brand": GREASE_BRAND, "version": GREASE_VERSION},
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
    ]


def build_stealth_js(major: str) -> str:
    """生成注入脚本。所有值都从 major 派生，保证自洽。"""
    cfg = {
        "major": major,
        "brands": _brands(major),
        "ua_platform": PLATFORM["ua_platform"],
        "navigator_platform": PLATFORM["navigator_platform"],
        "ch_platform": PLATFORM["client_hint_platform"],
        "ch_platform_version": PLATFORM["client_hint_platform_version"],
        "webgl_vendor": PLATFORM["webgl_vendor"],
        "webgl_renderer": PLATFORM["webgl_renderer"],
    }
    return _STEALTH_JS.replace("__CFG__", json.dumps(cfg, ensure_ascii=False))


_STEALTH_JS = r"""
(() => {
  'use strict';
  const CFG = __CFG__;
  const safe = (fn) => { try { fn(); } catch (e) { /* 尽力而为 */ } };

  // =====================================================================
  // 0. 让被替换掉的函数看起来仍是浏览器原生实现
  //    否则 Object.prototype.toString / Function.prototype.toString
  //    一眼就能看出 navigator.permissions.query 被改过
  // =====================================================================
  const nativeToString = Function.prototype.toString;
  const fakeNative = new WeakMap();
  const markNative = (fn, label) => {
    try { fakeNative.set(fn, 'function ' + label + '() { [native code] }'); } catch (e) {}
    return fn;
  };
  safe(() => {
    const patched = function toString() {
      if (fakeNative.has(this)) return fakeNative.get(this);
      return nativeToString.call(this);
    };
    fakeNative.set(patched, 'function toString() { [native code] }');
    Function.prototype.toString = patched;
  });

  const defineGetter = (obj, prop, getter, label) => {
    const g = markNative(getter, 'get ' + prop);
    try {
      Object.defineProperty(obj, prop, {
        get: g, configurable: true, enumerable: true,
      });
      return true;
    } catch (e) { return false; }
  };

  // 造一个真·PluginArray / MimeTypeArray。
  // 关键：必须挂到原生原型上，否则 `navigator.plugins instanceof PluginArray`
  // 是 false（bot.sannysoft.com 就是拿这个抓的），光设 Symbol.toStringTag 不够。
  const makeArrayLike = (items, tag, proto, keyName) => {
    const arr = proto ? Object.create(proto) : {};
    items.forEach((it, i) => {
      Object.defineProperty(arr, i, {
        value: it, enumerable: true, configurable: true, writable: false,
      });
    });
    Object.defineProperty(arr, 'length', {
      value: items.length, enumerable: false, configurable: true,
    });

    // 原生原型上的 item/namedItem 未必肯在自造对象上工作，不行就自己补
    let nativeOk = false;
    try { nativeOk = typeof arr.item === 'function' && arr.item(0) === items[0]; } catch (e) {}
    if (!nativeOk) {
      arr.item = markNative(function item(i) { return items[i] || null; }, 'item');
      arr.namedItem = markNative(
        function namedItem(n) { return items.find((it) => it[keyName] === n) || null; },
        'namedItem',
      );
    }
    if (!proto) {
      Object.defineProperty(arr, Symbol.toStringTag, { value: tag });
    }
    return arr;
  };

  // =====================================================================
  // 1. navigator.webdriver
  //    Chrome 把它放在 Navigator.prototype 上。只在实例上 defineProperty
  //    不够 —— 原型上还留着一个可枚举的 getter，描述符一查就露。
  //    先尝试彻底 delete，删不掉再退化成返回 undefined 的 getter。
  // =====================================================================
  safe(() => { delete Navigator.prototype.webdriver; });
  if ('webdriver' in Navigator.prototype) {
    defineGetter(Navigator.prototype, 'webdriver', function () { return undefined; });
  }
  safe(() => { delete Object.getPrototypeOf(navigator).webdriver; });

  // =====================================================================
  // 2. window.chrome —— headless 下是个空壳，正常 Chrome 有这些
  // =====================================================================
  safe(() => {
    const c = window.chrome || {};
    if (!c.runtime) {
      const noop = markNative(function () {}, 'noop');
      const evt = { addListener: noop, removeListener: noop, hasListener: () => false };
      c.runtime = {
        id: undefined,
        connect: markNative(function connect() {
          return { onMessage: evt, onDisconnect: evt, postMessage: noop, disconnect: noop };
        }, 'connect'),
        sendMessage: noop,
        onMessage: evt,
        onConnect: evt,
      };
    }
    if (!c.app) {
      c.app = {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
        getDetails: markNative(function getDetails() { return null; }, 'getDetails'),
        getIsInstalled: markNative(function getIsInstalled() { return false; }, 'getIsInstalled'),
      };
    }
    if (!c.csi) {
      c.csi = markNative(function csi() {
        const t = Date.now();
        return { onloadT: t, startE: t, pageT: 120 + Math.random() * 80, tran: 15 };
      }, 'csi');
    }
    if (!c.loadTimes) {
      c.loadTimes = markNative(function loadTimes() {
        const s = Date.now() / 1000;
        return {
          requestTime: s - 0.35, startLoadTime: s - 0.35, commitLoadTime: s - 0.22,
          finishDocumentLoadTime: s - 0.12, finishLoadTime: s, firstPaintTime: s - 0.06,
          firstPaintAfterLoadTime: 0, navigationType: 'Other',
          wasFetchedViaSpdy: true, wasNpnNegotiated: true, npnNegotiatedProtocol: 'h2',
          wasAlternateProtocolAvailable: false, connectionInfo: 'h2',
        };
      }, 'loadTimes');
    }
    window.chrome = c;
  });

  // =====================================================================
  // 3. navigator.plugins / mimeTypes
  //    headless 下都是 0，而正常桌面 Chrome 一定有 PDF 插件
  // =====================================================================
  safe(() => {
    const mk = (name, desc) => ({ name, filename: 'internal-pdf-viewer', description: desc });
    const mimeTypes = [];
    const plugins = [
      mk('PDF Viewer', 'Portable Document Format'),
      mk('Chrome PDF Viewer', 'Portable Document Format'),
      mk('Chromium PDF Viewer', 'Portable Document Format'),
      mk('Microsoft Edge PDF Viewer', 'Portable Document Format'),
      mk('WebKit built-in PDF', 'Portable Document Format'),
    ];
    plugins.forEach((p) => {
      const mimes = [
        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: p },
        { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: p },
      ];
      p.length = mimes.length;
      mimes.forEach((m, i) => { p[i] = m; if (!mimeTypes.includes(m)) mimeTypes.push(m); });
      p.item = markNative(function item(i) { return mimes[i] || null; }, 'item');
      p.namedItem = markNative(
        function namedItem(n) { return mimes.find((m) => m.type === n) || null; },
        'namedItem',
      );
      Object.defineProperty(p, Symbol.toStringTag, { value: 'Plugin' });
    });

    const pluginArray = makeArrayLike(
      plugins, 'PluginArray', window.PluginArray && PluginArray.prototype, 'name');
    if (!window.PluginArray) {
      pluginArray.refresh = markNative(function refresh() {}, 'refresh');
    }
    const mimeArray = makeArrayLike(
      mimeTypes, 'MimeTypeArray', window.MimeTypeArray && MimeTypeArray.prototype, 'type');

    defineGetter(Navigator.prototype, 'plugins', function () { return pluginArray; });
    defineGetter(Navigator.prototype, 'mimeTypes', function () { return mimeArray; });
    defineGetter(Navigator.prototype, 'pdfViewerEnabled', function () { return true; });
  });

  // =====================================================================
  // 4. permissions —— Playwright 默认把通知权限设成 denied
  //    正常首次访问应该是 default（询问）
  // =====================================================================
  safe(() => {
    defineGetter(Notification, 'permission', function () { return 'default'; });

    const origQuery = window.navigator.permissions.query.bind(navigator.permissions);
    const patchedQuery = markNative(function query(desc) {
      if (desc && desc.name === 'notifications') {
        return Promise.resolve({ state: 'default', name: 'notifications', onchange: null });
      }
      return origQuery(desc);
    }, 'query');
    navigator.permissions.query = patchedQuery;
  });

  // =====================================================================
  // 5. 环境一致性
  // =====================================================================
  safe(() => {
    defineGetter(Navigator.prototype, 'platform', function () { return CFG.navigator_platform; });
    defineGetter(Navigator.prototype, 'languages', function () { return ['zh-CN', 'zh', 'en']; });
    defineGetter(Navigator.prototype, 'language', function () { return 'zh-CN'; });
    defineGetter(Navigator.prototype, 'maxTouchPoints', function () { return 0; });
    defineGetter(Navigator.prototype, 'deviceMemory', function () { return 8; });
    defineGetter(Navigator.prototype, 'hardwareConcurrency', function () { return 8; });
    defineGetter(Navigator.prototype, 'pdfViewerEnabled', function () { return true; });
  });

  // =====================================================================
  // 6. Client Hints（userAgentData）
  //    必须和请求头 Sec-CH-UA 一致，否则一比就露
  // =====================================================================
  safe(() => {
    const full = CFG.major + '.0.0.0';
    const fullList = [
      { brand: CFG.brands[0].brand, version: CFG.brands[0].version + '.0.0.0' },
      { brand: 'Chromium', version: full },
      { brand: 'Google Chrome', version: full },
    ];
    const data = {
      brands: CFG.brands,
      mobile: false,
      platform: CFG.ch_platform,
      getHighEntropyValues: markNative(function getHighEntropyValues(hints) {
        const all = {
          architecture: 'x86', bitness: '64', brands: CFG.brands,
          formFactors: ['Desktop'], fullVersionList: fullList,
          mobile: false, model: '', platform: CFG.ch_platform,
          platformVersion: CFG.ch_platform_version,
          uaFullVersion: full, wow64: false,
        };
        const out = {};
        (hints || []).forEach((h) => { if (h in all) out[h] = all[h]; });
        return Promise.resolve(out);
      }, 'getHighEntropyValues'),
      toJSON: markNative(function toJSON() {
        return { brands: CFG.brands, mobile: false, platform: CFG.ch_platform };
      }, 'toJSON'),
    };
    defineGetter(Navigator.prototype, 'userAgentData', function () { return data; });
  });

  // =====================================================================
  // 7. WebGL —— 软件渲染（SwiftShader / llvmpipe）是 headless 最硬的招牌
  // =====================================================================
  safe(() => {
    const UNMASKED_VENDOR = 37445;    // UNMASKED_VENDOR_WEBGL
    const UNMASKED_RENDERER = 37446;  // UNMASKED_RENDERER_WEBGL
    const VENDOR = 7936;
    const RENDERER = 7937;
    const patch = (proto) => {
      if (!proto || !proto.getParameter) return;
      const orig = proto.getParameter;
      proto.getParameter = markNative(function getParameter(p) {
        if (p === UNMASKED_VENDOR) return CFG.webgl_vendor;
        if (p === UNMASKED_RENDERER) return CFG.webgl_renderer;
        if (p === VENDOR) return 'WebKit';
        if (p === RENDERER) return 'WebKit WebGL';
        return orig.call(this, p);
      }, 'getParameter');
    };
    patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  });

  // =====================================================================
  // 8. 媒体编解码器
  //    Playwright 自带的 Chromium 不含 H.264/AAC 等专有编解码器，
  //    而正常桌面 Chrome 都有 —— 这是一项很硬的 headless 指纹
  // =====================================================================
  safe(() => {
    const orig = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = markNative(function canPlayType(type) {
      const real = orig.call(this, type);
      if (real) return real;
      const t = String(type || '').toLowerCase();
      if (/avc1|h264|mp4a|^video\/mp4|^audio\/mp4/.test(t)) return 'probably';
      if (/^video\/webm|^audio\/webm|vp8|vp9|opus|vorbis/.test(t)) return 'maybe';
      return real;
    }, 'canPlayType');
  });

  // =====================================================================
  // 9. Playwright 残留
  //    注意：**不能在 document-start 阶段删 __pwInitScripts**。
  //    Playwright 靠它注册并注入所有 add_init_script 脚本，提前删掉会把
  //    它自己的机制弄坏（表现为 __museHelpers 等后续脚本全都不执行）。
  //    做法：先让它不可枚举，等 DOMContentLoaded 之后再删。
  // =====================================================================
  safe(() => {
    const KEYS = [
      '__playwright__binding__', '__pwInitScripts',
      '__playwright', '__pw_manual', '__pwEventListeners',
    ];
    const hide = () => KEYS.forEach((k) => {
      try {
        const d = Object.getOwnPropertyDescriptor(window, k);
        if (d && d.enumerable) Object.defineProperty(window, k, { enumerable: false });
      } catch (e) {}
    });
    const wipe = () => KEYS.forEach((k) => {
      try { delete window[k]; } catch (e) {}
    });

    hide();
    const later = () => { hide(); setTimeout(wipe, 0); };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', later, { once: true });
    } else {
      later();
    }
  });

  // =====================================================================
  // 10. 屏幕 / 窗口尺寸
  // =====================================================================
  safe(() => {
    if (!window.outerWidth || !window.outerHeight) {
      defineGetter(window, 'outerWidth', function () { return window.innerWidth || 1280; });
      defineGetter(window, 'outerHeight', function () { return (window.innerHeight || 820) + 90; });
    }
  });
})();
"""
