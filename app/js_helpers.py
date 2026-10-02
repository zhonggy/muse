"""注入到页面的 JS 工具集。

muse.ai 是 SPA + 自定义元素 / Shadow DOM，无障碍树基本为空，
因此所有定位都走「穿透 shadow root 的 CSS 查询 + 真实鼠标事件序列」。
"""
from __future__ import annotations

#: 通过 add_init_script 注入到每个页面 / iframe，挂到 window.__museHelpers
HELPERS_JS = r"""
(() => {
  if (window.__museHelpers) return;

  // 穿透 shadow root 的深度查询
  const deepAll = (sel, root) => {
    const out = [];
    const queue = [root || document];
    const seen = new Set();
    while (queue.length) {
      const node = queue.shift();
      if (!node || seen.has(node)) continue;
      seen.add(node);
      try { node.querySelectorAll(sel).forEach((e) => out.push(e)); } catch (e) {}
      let all = [];
      try { all = node.querySelectorAll('*'); } catch (e) {}
      for (const el of all) { if (el.shadowRoot) queue.push(el.shadowRoot); }
    }
    return out;
  };

  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') === 0) return false;
    if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  };

  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();

  // 完整 MouseEvent / PointerEvent 序列（Radix Select 只认这个）
  //
  // 注意：**不能传 view: window**。Camoufox 会给 window 套一层代理，
  // 它不再是真正的 Window 实例，Firefox 会报
  // “'view' member of UIEventInit does not implement interface Window”，
  // 整个构造失败 —— 所有 JS 兜底点击（Radix 下拉、姓名框、区域点击）都会挂。
  // view 本身是可选的，去掉不影响事件分发。
  const fire = (el, type, Ctor, x, y, buttons) => {
    el.dispatchEvent(new Ctor(type, {
      bubbles: true, cancelable: true, composed: true,
      clientX: x, clientY: y, button: 0, buttons: buttons,
      pointerId: 1, pointerType: 'mouse', isPrimary: true,
    }));
  };

  // 完整 MouseEvent / PointerEvent 序列（Radix Select 只认这个）
  const realClick = (el) => {
    if (!el) return false;
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    try { el.focus({ preventScroll: true }); } catch (e) {}
    try { fire(el, 'pointerover', PointerEvent, x, y, 0); } catch (e) {}
    fire(el, 'mouseover', MouseEvent, x, y, 0);
    fire(el, 'mousemove', MouseEvent, x, y, 0);
    try { fire(el, 'pointerdown', PointerEvent, x, y, 1); } catch (e) {}
    fire(el, 'mousedown', MouseEvent, x, y, 1);
    try { fire(el, 'pointerup', PointerEvent, x, y, 0); } catch (e) {}
    fire(el, 'mouseup', MouseEvent, x, y, 0);
    fire(el, 'click', MouseEvent, x, y, 0);
    return true;
  };

  // React 受控输入：必须走原生 setter + 派发 input/change
  const setValue = (el, value) => {
    if (!el) return false;
    let proto = HTMLInputElement.prototype;
    if (el instanceof HTMLTextAreaElement) proto = HTMLTextAreaElement.prototype;
    else if (el instanceof HTMLSelectElement) proto = HTMLSelectElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    try { el.focus(); } catch (e) {}
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    return el.value === value;
  };

  const findByText = (sel, label, exact) => {
    const els = deepAll(sel).filter(visible);
    let hit = els.find((e) => text(e) === label);
    if (!hit && exact === false) hit = els.find((e) => text(e).includes(label));
    return hit || null;
  };

  const clickByText = (sel, label, exact) => realClick(findByText(sel, label, exact));

  // 点击下拉选项：先滚动到视口内再派发完整事件序列
  const clickOption = (label) => {
    const opts = deepAll('[role="option"]').filter(visible);
    const norm = (s) => s.replace(/\s+/g, '');
    let hit = opts.find((o) => text(o) === label);
    if (!hit) hit = opts.find((o) => norm(text(o)) === norm(label));
    if (!hit) hit = opts.find((o) => text(o).startsWith(label));
    return realClick(hit);
  };

  const clickSelector = (sel, index) => {
    const els = deepAll(sel).filter(visible);
    const el = els[index || 0];
    return realClick(el);
  };

  const comboboxLabels = () => deepAll('button[role="combobox"]').map((c) => {
    let label = '';
    const ids = (c.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    for (const id of ids) {
      const e = document.getElementById(id);
      if (e) label += ' ' + text(e);
    }
    if (!label) label = c.getAttribute('aria-label') || '';
    if (!label) {
      const lab = c.closest('label');
      if (lab) label = text(lab);
    }
    if (!label) {
      let prev = c.previousElementSibling;
      while (prev && !label) { label = text(prev); prev = prev.previousElementSibling; }
    }
    if (!label) {
      const parent = c.parentElement;
      if (parent) label = text(parent);
    }
    return label.trim();
  });

  const describe = () => ({
    url: location.href,
    title: document.title,
    body: (document.body ? text(document.body) : '').slice(0, 4000),
    inputs: deepAll('input').length,
    buttons: deepAll('button, [role="button"]').length,
    comboboxes: deepAll('button[role="combobox"]').length,
    options: deepAll('[role="option"]').length,
  });

  // ------------------------------------------------------------------
  // 按屏幕区域定位可点元素。
  // muse 的 DOM 结构不稳定，左下角菜单、中间弹窗这类只能靠位置兜底。
  // ------------------------------------------------------------------
  const clickablesIn = (region) => {
    const vw = innerWidth, vh = innerHeight;
    const inRegion = (el) => {
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (region === 'bottom-left') return cx < vw * 0.45 && cy > vh * 0.35;
      if (region === 'bottom-right') return cx > vw * 0.55 && cy > vh * 0.35;
      if (region === 'center') {
        return cx > vw * 0.2 && cx < vw * 0.8 && cy > vh * 0.1 && cy < vh * 0.9;
      }
      return true;
    };
    const els = deepAll(
      'button, [role="button"], [role="menuitem"], [role="option"], a, li')
      .filter(visible).filter(inRegion);
    // 只留最内层，避免父子元素重复计数
    return els.filter((e) => !els.some((o) => o !== e && e.contains(o)));
  };

  const clickNthInRegion = (n, region) => {
    const els = clickablesIn(region);
    const el = els[(n | 0) - 1];
    return el ? realClick(el) : false;
  };

  // 把当前可点元素标记为「已存在」。
  // 用途：点开菜单前先标记，然后只数新冒出来的元素 —— 否则入口按钮
  // 本身也会被计入，导致「第 4 项」实际点到第 3 项。
  const markSeen = (region) => {
    clickablesIn(region).forEach((e) => {
      try { e.setAttribute('data-muse-seen', '1'); } catch (err) {}
    });
    return true;
  };

  const clickNthNewInRegion = (n, region) => {
    const els = clickablesIn(region).filter((e) => !e.hasAttribute('data-muse-seen'));
    const el = els[(n | 0) - 1];
    return el ? realClick(el) : false;
  };

  const listNewInRegion = (region) =>
    clickablesIn(region).filter((e) => !e.hasAttribute('data-muse-seen'))
      .map((e) => text(e).slice(0, 40));

  const clickCorner = (region) => {
    const els = clickablesIn(region);
    if (!els.length) return false;
    const vw = innerWidth, vh = innerHeight;
    const tgt = region === 'bottom-left' ? [0, vh]
              : region === 'bottom-right' ? [vw, vh] : [vw / 2, vh / 2];
    let best = null, bestD = Infinity;
    for (const el of els) {
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const d = Math.hypot(cx - tgt[0], cy - tgt[1]);
      if (d < bestD) { bestD = d; best = el; }
    }
    return best ? realClick(best) : false;
  };

  const listClickables = (region) =>
    clickablesIn(region).map((e) => text(e).slice(0, 40));

  // 填第一个可见的文本输入框（邀请码这种只有一个输入框的场景）
  const fillFirstInput = (value) => {
    const SKIP = new Set(['hidden', 'checkbox', 'radio', 'submit', 'button',
                          'file', 'image', 'range', 'color']);
    const el = deepAll('input, textarea').filter(visible).find((e) => {
      const t = (e.getAttribute('type') || 'text').toLowerCase();
      return !SKIP.has(t) && !e.disabled && !e.readOnly;
    });
    if (!el) return false;
    el.focus();
    setValue(el, value);
    return el.value === value;
  };

  window.__museHelpers = {
    deepAll, visible, text, realClick, setValue,
    findByText, clickByText, clickOption, clickSelector,
    comboboxLabels, describe,
    clickablesIn, clickNthInRegion, clickCorner, listClickables, fillFirstInput,
    markSeen, clickNthNewInRegion, listNewInRegion,
  };
})();
"""


#: 探测支付表单字段并打标记（支持 iframe 内独立执行）
CARD_PROBE_JS = r"""
(spec) => {
  const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
  inputs.forEach((el) => el.removeAttribute('data-muse-fill'));

  const sig = (el) => [
    el.getAttribute('autocomplete') || '',
    el.getAttribute('name') || '',
    el.id || '',
    el.getAttribute('placeholder') || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('data-testid') || '',
    el.getAttribute('inputmode') || '',
    el.getAttribute('type') || '',
  ].join(' ').toLowerCase();

  const usable = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    if (el.disabled || el.readOnly) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };

  const rules = [
    ['number', /cc-number|card.?number|cardnumber|card_no|cardno|card-number|\bpan\b|卡号|银行卡号|卡號/],
    ['cvc',    /cc-csc|cc-cvv|\bcvc\b|\bcvv\b|\bcsc\b|security.?code|安全码|安全碼/],
    ['exp',    /cc-exp|expir|\bexp\b|expiry|有效期|有效期限|mm\s*\/?\s*yy|mm\s*\/?\s*yyyy/],
    ['postal', /postal|zip|邮编|郵遞區號/],
    ['name',   /cc-name|cardholder|card.?holder|name.?on.?card|持卡人|卡片上的姓名/],
    ['address',/address.?line|street|地址|街道/],
    ['city',   /city|城市/],
    ['state',  /state|province|省|州/],
    ['country',/country|国家|國家/],
  ];

  const found = {};
  for (const el of inputs) {
    if (!usable(el)) continue;
    const s = sig(el);
    for (const [kind, re] of rules) {
      if (found[kind]) continue;
      if (re.test(s)) { found[kind] = el; el.setAttribute('data-muse-fill', kind); break; }
    }
  }

  // 兜底：type=tel 且长度较长 => 卡号；type=password/短数字 => cvc
  if (!found.number) {
    const tel = inputs.find((el) => usable(el) && !el.getAttribute('data-muse-fill') &&
      (el.getAttribute('type') === 'tel' || el.getAttribute('inputmode') === 'numeric') &&
      (el.getAttribute('maxlength') || '99') >= 12);
    if (tel) { tel.setAttribute('data-muse-fill', 'number'); found.number = tel; }
  }

  const expFields = Array.from(document.querySelectorAll('[data-muse-fill="exp"]'));
  let splitExp = false;
  if (found.exp && found.exp.tagName === 'SELECT') splitExp = true;
  const selects = Array.from(document.querySelectorAll('select')).filter(usable);
  const monthSel = selects.find((s) => /month|月/i.test((s.name || '') + (s.id || '') + (s.getAttribute('aria-label') || '')));
  const yearSel = selects.find((s) => /year|年/i.test((s.name || '') + (s.id || '') + (s.getAttribute('aria-label') || '')));
  if (monthSel) { monthSel.setAttribute('data-muse-fill', 'exp-month'); splitExp = true; }
  if (yearSel) { yearSel.setAttribute('data-muse-fill', 'exp-year'); splitExp = true; }

  // 兜底 1：两个相邻短数字输入框 => MM / YY
  if (!found.exp && !monthSel && !yearSel) {
    const free = inputs.filter((el) => usable(el) && !el.getAttribute('data-muse-fill'));
    const short = free.filter((el) => {
      const ml = parseInt(el.getAttribute('maxlength') || '0', 10);
      return ml === 2 || ml === 3 || /mm|yy/i.test((el.getAttribute('placeholder') || '') + (el.getAttribute('name') || ''));
    });
    if (short.length >= 2) {
      short[0].setAttribute('data-muse-fill', 'exp-month');
      short[1].setAttribute('data-muse-fill', 'exp-year');
      splitExp = true;
    }
  }

  // 兜底 2：名字字段（billingName / cardholder-name 之类）
  if (!found.name) {
    const cand = inputs.find((el) => usable(el) && !el.getAttribute('data-muse-fill') &&
      /name|姓名|holder|持卡/.test(sig(el)));
    if (cand) { cand.setAttribute('data-muse-fill', 'name'); found.name = cand; }
  }

  return {
    fields: Object.keys(found),
    marked: Array.from(document.querySelectorAll('[data-muse-fill]'))
      .map((el) => el.getAttribute('data-muse-fill')),
    splitExp,
    count: inputs.length,
  };
}
"""


#: 探测页面上的姓名输入框（muse 的「完成账户创建」页有时会多出「名 / 姓」两栏）
NAME_PROBE_JS = r"""
() => {
  const h = window.__museHelpers;

  const SKIP_TYPE = new Set(['hidden', 'checkbox', 'radio', 'submit', 'button',
                             'file', 'image', 'range', 'color', 'date', 'time']);
  const inputs = h.deepAll('input').filter((el) => {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (SKIP_TYPE.has(t)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1 && !el.disabled && !el.readOnly;
  });

  // 拿到跟这个 input 关联的可见文案
  const labelOf = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    let s = ids.map((id) => {
      const e = document.getElementById(id);
      return e ? h.text(e) : '';
    }).join(' ');
    if (!s) s = el.getAttribute('aria-label') || '';
    if (!s) {
      const lab = el.closest('label');
      if (lab) s = h.text(lab);
    }
    if (!s) {
      let prev = el.previousElementSibling;
      while (prev && !s) { s = h.text(prev); prev = prev.previousElementSibling; }
    }
    if (!s && el.parentElement) s = h.text(el.parentElement);
    return s || '';
  };

  const attrSig = (el) => [
    el.getAttribute('autocomplete') || '',
    el.getAttribute('name') || '',
    el.id || '',
    el.getAttribute('placeholder') || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('data-testid') || '',
  ].join(' ').toLowerCase();

  // 去掉「必填 / 选填 / *」之类的标记后再比
  const cleanLabel = (s) => s.replace(/必填|选填|選填|\*/g, '').replace(/\s+/g, '').trim();

  const classify = (el) => {
    const sig = attrSig(el);
    if (/given[-_]?name|first[-_]?name|\bgiven\b|\bfname\b/.test(sig)) return 'first';
    if (/family[-_]?name|last[-_]?name|surname|\blast\b|\blname\b/.test(sig)) return 'last';
    const lab = cleanLabel(labelOf(el));
    if (lab === '名' || lab === '名字') return 'first';
    if (lab === '姓' || lab === '姓氏') return 'last';
    return '';
  };

  inputs.forEach((el) => el.removeAttribute('data-muse-name'));

  const found = { first: null, last: null };
  const marks = [];
  for (const el of inputs) {
    const kind = classify(el);
    marks.push({ kind: kind || '?', label: cleanLabel(labelOf(el)).slice(0, 40),
                 sig: attrSig(el).slice(0, 70) });
    if (kind && !found[kind]) {
      found[kind] = el;
      el.setAttribute('data-muse-name', kind);
    }
  }

  // 兜底：页面上正好剩两个未分类的文本输入 → 按 DOM 顺序当作 名 / 姓
  // （中文页面里「名」在「姓」前面）
  const rest = inputs.filter((el) => !el.getAttribute('data-muse-name'));
  if ((!found.first || !found.last) && rest.length === 2) {
    if (!found.first) { found.first = rest[0]; rest[0].setAttribute('data-muse-name', 'first'); }
    if (!found.last) { found.last = rest[1]; rest[1].setAttribute('data-muse-name', 'last'); }
  } else if (!found.first && rest.length === 1) {
    found.first = rest[0];
    rest[0].setAttribute('data-muse-name', 'first');
  }

  return {
    found: !!(found.first || found.last),
    hasFirst: !!found.first,
    hasLast: !!found.last,
    inputCount: inputs.length,
    marks,
  };
}
"""


#: 探测页面上的提交按钮文案
SUBMIT_BUTTONS_JS = r"""
() => {
  const h = window.__museHelpers;
  const els = h.deepAll('button, [role="button"], input[type="submit"], a[role="button"]');
  return els.filter(h.visible).map((el) => ({
    text: h.text(el) || el.value || '',
    type: el.getAttribute('type') || '',
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
  }));
}
"""
