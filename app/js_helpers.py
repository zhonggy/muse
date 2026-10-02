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

  const fire = (el, type, Ctor, x, y, buttons) => {
    el.dispatchEvent(new Ctor(type, {
      bubbles: true, cancelable: true, composed: true, view: window,
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

  window.__museHelpers = {
    deepAll, visible, text, realClick, setValue,
    findByText, clickByText, clickOption, clickSelector,
    comboboxLabels, describe,
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
