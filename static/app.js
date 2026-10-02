/* muse-auto 控制台前端（无构建，纯原生） */
(() => {
  'use strict';

  const state = {
    tasks: [],
    cards: [],
    settings: {},
    selected: null,
    ws: null,
    wsToken: '',
    shots: {},
    wsOk: false,
    renderedLogs: 0,
    resin: null,
  };

  // 用户改过、还没保存的字段。定时刷新时不能覆盖它们，
  // 否则每 8 秒一次 reload() 会把正在输的内容冲掉。
  const dirtyFields = new Set();

  function markDirty(id, on) {
    const el = $(id);
    if (el) el.classList.toggle('unsaved', !!on);
  }

  function markFieldDirty(id) {
    dirtyFields.add(id);
    markDirty(id, true);
  }

  function clearDirty() {
    dirtyFields.forEach((id) => markDirty(id, false));
    dirtyFields.clear();
  }

  function isEditing(id) {
    if (dirtyFields.has(id)) return true;
    const el = $(id);
    return !!el && document.activeElement === el;
  }

  // 给所有会被定时刷新同步的输入控件挂上脏标记
  function bindDirtyGuard() {
    const ids = [
      ...Object.keys(SETTING_FIELDS),
      ...Object.keys(SETTING_CHECKS),
      'concurrency', 'count', 'taskCard', 's_default_card_id',
    ];
    ids.forEach((id) => {
      const el = $(id);
      if (!el) return;
      const mark = () => markFieldDirty(id);
      el.addEventListener('input', mark);
      el.addEventListener('change', mark);
    });
  }

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const STATUS_LABEL = {
    pending: '待运行', running: '运行中', paused: '已暂停',
    success: '成功', failed: '失败', stopped: '已停止',
  };
  const NEEDS_LABEL = {
    card: '需要卡信息', manual: '需要人工接管', code: '等待验证码',
  };

  // ------------------------------------------------------------------
  // HTTP
  // ------------------------------------------------------------------

  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || JSON.stringify(j);
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get('content-type') || '';
    return ct.includes('application/json') ? res.json() : res.text();
  }

  let toastTimer = null;
  function toast(msg, kind = '') {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast show ' + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast ' + kind; }, 4200);
  }

  const guard = (fn) => (...args) => Promise.resolve(fn(...args)).catch((e) => {
    toast(e.message || String(e), 'err');
  });

  // 耗时操作期间把按钮置灰，否则用户不知道到底点没点上
  async function withBusy(btn, label, fn) {
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = label;
    try {
      return await fn();
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  // ------------------------------------------------------------------
  // WebSocket
  // ------------------------------------------------------------------

  function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const q = state.wsToken ? `?token=${encodeURIComponent(state.wsToken)}` : '';
    return `${proto}://${location.host}/ws${q}`;
  }

  async function ensureToken() {
    if (state.wsToken) return state.wsToken;
    try {
      const t = await api('GET', '/api/ws-token');
      state.wsToken = t.token || '';
    } catch (_) {
      // 取不到就保持空，靠浏览器缓存的 Basic 凭据
    }
    return state.wsToken;
  }

  function connect() {
    // 每次重连都重新确认 token：容器重启后 wsToken 可能是空的
    ensureToken().then(() => {
      let ws;
      try { ws = new WebSocket(wsUrl()); } catch (e) { setTimeout(connect, 2500); return; }
      state.ws = ws;

      ws.onopen = () => {
        state.wsOk = true;
        $('conn').textContent = '已连接';
        $('conn').className = 'pill on';
      };
      ws.onclose = (ev) => {
        state.wsOk = false;
        $('conn').textContent = '未连接';
        $('conn').className = 'pill off';
        // 4401 = 鉴权失败，下次重连前重新取 token
        if (ev && ev.code === 4401) state.wsToken = '';
        setTimeout(connect, 2000);
      };
      ws.onerror = () => { try { ws.close(); } catch (_) {} };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        handle(msg);
      };
    });
  }

  function wsSend(payload) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(payload));
      return true;
    }
    toast('WebSocket 未连接', 'err');
    return false;
  }

  function handle(msg) {
    switch (msg.type) {
      case 'hello':
        state.settings = msg.settings || {};
        state.cards = msg.cards || [];
        state.tasks = msg.tasks || [];
        state.resin = msg.resin || null;
        fillSettings();
        renderCards();
        renderResinPill();
        renderTasks();
        break;
      case 'log':
        if (msg.task_id === state.selected) {
          appendLog(msg);
          state.renderedLogs += 1;
        }
        break;
      case 'shot':
        state.shots[msg.task_id] = msg;
        if (msg.task_id === state.selected) showShot(msg);
        break;
      case 'task':
        upsertTask(msg.task);
        break;
      case 'tasks_deleted':
        state.tasks = state.tasks.filter((t) => !msg.ids.includes(t.id));
        if (msg.ids.includes(state.selected)) selectTask(null);
        renderTasks();
        break;
      case 'tasks_cleared':
        reload();
        break;
      case 'cards':
        state.cards = msg.cards || [];
        renderCards();
        break;
      case 'settings':
        state.settings = msg.settings || {};
        fillSettings();
        break;
      default:
        break;
    }
  }

  function upsertTask(task) {
    if (!task) return;
    const i = state.tasks.findIndex((t) => t.id === task.id);
    if (i >= 0) state.tasks[i] = task; else state.tasks.unshift(task);
    renderTasks();
    if (task.id === state.selected) renderDetail();
  }

  // ------------------------------------------------------------------
  // 任务渲染
  // ------------------------------------------------------------------

  function renderResinPill() {
    const el = $('resinPill');
    if (!el) return;
    const r = state.resin;
    if (r) {
      el.textContent = `Resin: ${r.platform}`;
      el.className = 'pill on';
      el.title = `反向代理（skymail）+ 正向代理（浏览器）\n${r.base}\n`
        + `Platform=${r.platform} Token=${r.token_masked}`;
    } else {
      el.textContent = 'Resin 未启用';
      el.className = 'pill off';
      el.title = '未配置 resin_url 或已关闭，所有请求直连';
    }
  }

  function renderTasks() {
    const box = $('taskList');
    $('taskCount').textContent = state.tasks.length ? `（${state.tasks.length}）` : '';
    if (!state.tasks.length) {
      box.innerHTML = '<div class="muted small" style="padding:8px 2px">暂无任务</div>';
      return;
    }
    const order = { running: 0, paused: 1, pending: 2, failed: 3, success: 4, stopped: 5 };
    const list = [...state.tasks].sort((a, b) => {
      const d = (order[a.status] ?? 9) - (order[b.status] ?? 9);
      return d !== 0 ? d : (b.created_at || 0) - (a.created_at || 0);
    });
    box.innerHTML = list.map((t) => {
      const badge = `<span class="badge ${esc(t.status)}">${esc(STATUS_LABEL[t.status] || t.status)}</span>`;
      const needs = t.needs ? `<span class="badge paused">${esc(NEEDS_LABEL[t.needs] || t.needs)}</span>` : '';
      const sub = t.error || t.message || t.step || '';
      const acts = [
        `<button class="mini" data-act="start" data-id="${t.id}">开始</button>`,
        `<button class="mini" data-act="stop" data-id="${t.id}">停止</button>`,
        `<button class="mini" data-act="retry" data-id="${t.id}">重试</button>`,
        `<button class="mini danger" data-act="del" data-id="${t.id}">删除</button>`,
      ].join('');
      return `<div class="task-item ${t.id === state.selected ? 'sel' : ''}" data-id="${t.id}">
        <div class="task-top">${badge}${needs}<span class="task-email">${esc(t.email || '待生成邮箱')}</span></div>
        <div class="task-sub"><span class="grow">${esc(sub)}</span></div>
        <div class="task-acts">${acts}</div>
      </div>`;
    }).join('');

    box.querySelectorAll('.task-item').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.dataset && e.target.dataset.act) return;
        selectTask(el.dataset.id);
      });
    });
    box.querySelectorAll('button[data-act]').forEach((b) => {
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        taskAction(b.dataset.act, b.dataset.id);
      });
    });
  }

  async function taskAction(act, id) {
    try {
      if (act === 'start') await api('POST', `/api/tasks/${id}/start`);
      else if (act === 'stop') await api('POST', `/api/tasks/${id}/stop`);
      else if (act === 'retry') await api('POST', `/api/tasks/${id}/retry`);
      else if (act === 'del') {
        await api('DELETE', `/api/tasks/${id}`);
        state.tasks = state.tasks.filter((t) => t.id !== id);
        if (state.selected === id) selectTask(null);
        renderTasks();
      }
    } catch (e) { toast(e.message, 'err'); }
  }

  function selectTask(id) {
    state.selected = id;
    $('logs').innerHTML = '';
    state.renderedLogs = 0;
    $('live').removeAttribute('src');
    $('viewer').classList.remove('live');
    renderTasks();
    renderDetail();
    if (!id) { $('detailTitle').textContent = '实时画面'; $('detailActions').innerHTML = ''; return; }
    syncLogs();
    const shot = state.shots[id];
    if (shot) showShot(shot);
  }

  // 只补增量，不重画。否则每 8 秒一次的定时刷新会让日志面板闪一下。
  function syncLogs() {
    const t = state.tasks.find((x) => x.id === state.selected);
    if (!t) return;
    const logs = t.logs || [];
    if (logs.length < state.renderedLogs) {
      // 服务端截断过日志，只能整体重画
      $('logs').innerHTML = '';
      state.renderedLogs = 0;
    }
    for (let i = state.renderedLogs; i < logs.length; i += 1) appendLog(logs[i]);
    state.renderedLogs = logs.length;
  }

  function renderDetail() {
    const t = state.tasks.find((x) => x.id === state.selected);
    if (!t) return;
    $('detailTitle').textContent = t.email || '新任务';
    const acts = [];
    if (t.needs === 'card') acts.push('<button class="primary mini" data-d="card">提供卡信息</button>');
    if (t.needs === 'manual') acts.push('<button class="primary mini" data-d="manual">我已完成人工操作</button>');
    acts.push('<button class="mini" data-d="start">开始</button>');
    acts.push('<button class="mini" data-d="stop">停止</button>');
    acts.push('<a class="btn mini" href="/api/tasks/' + t.id + '/shot" target="_blank">原图</a>');
    $('detailActions').innerHTML = acts.join('');
    $('detailActions').querySelectorAll('[data-d]').forEach((b) => {
      b.addEventListener('click', () => detailAction(b.dataset.d, t));
    });
  }

  async function detailAction(kind, task) {
    try {
      if (kind === 'card') return openCardModal(task);
      if (kind === 'manual') {
        await api('POST', `/api/tasks/${task.id}/manual-done`);
        toast('已通知任务继续', 'ok');
        return;
      }
      await taskAction(kind, task.id);
    } catch (e) { toast(e.message, 'err'); }
  }

  // ------------------------------------------------------------------
  // 日志 / 截图
  // ------------------------------------------------------------------

  function appendLog(entry) {
    const box = $('logs');
    const near = box.scrollTop + box.clientHeight > box.scrollHeight - 40;
    const line = document.createElement('div');
    line.className = 'log-line ' + (entry.level || 'info');
    const t = entry.ts ? new Date(entry.ts * 1000).toTimeString().slice(0, 8) : '';
    line.innerHTML = `<span class="t">${t}</span><span class="m">${esc(entry.msg)}</span>`;
    box.appendChild(line);
    while (box.childElementCount > 600) box.removeChild(box.firstChild);
    if (near) box.scrollTop = box.scrollHeight;
  }

  function showShot(msg) {
    if (state.settings.live_view === false) return;
    const img = $('live');
    img.src = 'data:image/jpeg;base64,' + msg.data;
    $('viewer').classList.add('live');
  }

  function renderViewerState() {
    const on = state.settings.live_view !== false;
    const viewer = $('viewer');
    const hint = $('viewerHint');
    viewer.classList.toggle('live-off', !on);
    if (!on) {
      viewer.classList.remove('live');
      hint.innerHTML = '实时画面已关闭<br><span class="muted">勾选「新建任务」里的「启用实时画面」后重跑任务即可</span>';
    } else {
      hint.innerHTML = '选择左侧任务，查看浏览器实时画面<br><span class="muted">可直接点击画面进行人工接管</span>';
    }
  }

  // ------------------------------------------------------------------
  // 实时接管
  // ------------------------------------------------------------------

  function setupViewer() {
    const viewer = $('viewer');
    const img = $('live');

    viewer.addEventListener('click', (e) => {
      if (state.settings.live_view === false) return;
      if (!state.selected || !img.naturalWidth) return;
      const rect = img.getBoundingClientRect();
      if (e.clientX < rect.left || e.clientX > rect.right) return;
      if (e.clientY < rect.top || e.clientY > rect.bottom) return;
      const sx = img.naturalWidth / rect.width;
      const sy = img.naturalHeight / rect.height;
      wsSend({
        type: 'click', task_id: state.selected,
        x: (e.clientX - rect.left) * sx,
        y: (e.clientY - rect.top) * sy,
      });
    });

    viewer.addEventListener('keydown', (e) => {
      if (!state.selected) return;
      const passthrough = ['Enter', 'Tab', 'Escape', 'Backspace', 'ArrowUp',
        'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Delete'];
      if (passthrough.includes(e.key)) {
        e.preventDefault();
        wsSend({ type: 'key', task_id: state.selected, key: e.key });
      } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        wsSend({ type: 'type', task_id: state.selected, text: e.key });
      }
    });

    document.querySelectorAll('.typebar [data-key]').forEach((b) => {
      b.addEventListener('click', () => {
        if (!state.selected) return toast('请先选择任务', 'err');
        wsSend({ type: 'key', task_id: state.selected, key: b.dataset.key });
      });
    });
    document.querySelectorAll('.typebar [data-scroll]').forEach((b) => {
      b.addEventListener('click', () => {
        if (!state.selected) return toast('请先选择任务', 'err');
        wsSend({ type: 'scroll', task_id: state.selected, dy: Number(b.dataset.scroll) });
      });
    });
    $('btnType').addEventListener('click', sendTypedText);
    $('typeInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); sendTypedText(); }
    });
    $('btnClearLogs').addEventListener('click', () => { $('logs').innerHTML = ''; });
  }

  function sendTypedText() {
    const input = $('typeInput');
    const text = input.value;
    if (!text) return;
    if (!state.selected) return toast('请先选择任务', 'err');
    wsSend({ type: 'type', task_id: state.selected, text });
    input.value = '';
  }

  // ------------------------------------------------------------------
  // 卡片
  // ------------------------------------------------------------------

  function renderCards() {
    const box = $('cardList');
    if (!state.cards.length) {
      box.innerHTML = '<div class="muted small">暂无卡片</div>';
    } else {
      box.innerHTML = state.cards.map((c) => `
        <div class="task-item">
          <div class="task-top">
            <span class="badge running">${esc(c.brand || 'Card')}</span>
            <span class="task-email">${esc(c.label || '')}</span>
          </div>
          <div class="task-sub">
            <span class="grow">${esc(c.pan_masked)} · ${esc(c.exp_month)}/${esc(c.exp_year)} · ${esc(c.holder || '-')}</span>
          </div>
          <div class="task-acts">
            <button class="mini" data-card-start="${c.id}">用于新任务</button>
            <button class="mini danger" data-card-del="${c.id}">删除</button>
          </div>
        </div>`).join('');
      box.querySelectorAll('[data-card-del]').forEach((b) => {
        b.addEventListener('click', guard(async () => {
          if (!confirm('确认删除该卡片？')) return;
          await api('DELETE', `/api/cards/${b.dataset.cardDel}`);
          toast('已删除', 'ok');
        }));
      });
      box.querySelectorAll('[data-card-start]').forEach((b) => {
        b.addEventListener('click', () => {
          $('taskCard').value = b.dataset.cardStart;
          showTab('tasks');
        });
      });
    }
    fillCardSelect();
  }

  function fillCardSelect() {
    const sel = $('taskCard');
    const keep = sel.value;
    sel.innerHTML = '<option value="">不绑卡（到验证页暂停）</option><option value="auto">自动轮换</option>'
      + state.cards.map((c) => `<option value="${c.id}">${esc(c.label)} ${esc(c.pan_masked)}</option>`).join('');
    if (keep) sel.value = keep;

    const dsel = $('s_default_card_id');
    const dkeep = isEditing('s_default_card_id')
      ? dsel.value
      : (state.settings.default_card_id || '');
    dsel.innerHTML = '<option value="">（无）</option>'
      + state.cards.map((c) => `<option value="${c.id}">${esc(c.label)} ${esc(c.pan_masked)}</option>`).join('');
    dsel.value = dkeep;
  }

  function setupCards() {
    // 有效期输入时自动补斜杠：0729 → 07/29
    $('c_exp').addEventListener('input', (e) => {
      const d = e.target.value.replace(/\D/g, '').slice(0, 4);
      e.target.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
    });
    // 卡号输入时每 4 位加空格
    $('c_number').addEventListener('input', (e) => {
      const d = e.target.value.replace(/\D/g, '').slice(0, 19);
      e.target.value = d.replace(/(.{4})/g, '$1 ').trim();
    });

    $('btnAddCard').addEventListener('click', guard(async () => {
      const number = $('c_number').value.replace(/\D/g, '');
      const exp = parseExpiry($('c_exp').value);
      const cvc = $('c_cvc').value.replace(/\D/g, '');
      if (number.length < 12) return toast('卡号无效', 'err');
      if (!exp) return toast('有效期格式应为 MM/YY', 'err');
      if (!cvc) return toast('请填写 CVV', 'err');
      await api('POST', '/api/cards', {
        number,
        exp_month: exp.month,
        exp_year: exp.year,
        cvc,
      });
      $('c_number').value = '';
      $('c_exp').value = '';
      $('c_cvc').value = '';
      toast('卡片已加密保存', 'ok');
    }));
  }

  // 支持 MM/YY、MM/YYYY、MMYY、M/YY
  function parseExpiry(raw) {
    const s = String(raw || '').trim();
    if (!s) return null;
    const m = s.match(/^(\d{1,2})\s*[\/\-.]\s*(\d{2,4})$/);
    if (m) {
      const mm = m[1].padStart(2, '0');
      if (+mm < 1 || +mm > 12) return null;
      return { month: mm, year: m[2].length === 2 ? '20' + m[2] : m[2] };
    }
    const d = s.replace(/\D/g, '');
    if (d.length === 3) return { month: '0' + d[0], year: '20' + d.slice(1) };
    if (d.length === 4) return { month: d.slice(0, 2), year: '20' + d.slice(2) };
    if (d.length === 6) return { month: d.slice(0, 2), year: d.slice(2) };
    return null;
  }

  // ------------------------------------------------------------------
  // 设置
  // ------------------------------------------------------------------

  const SETTING_FIELDS = {
    s_base: 'skymail_base_url',
    s_email: 'skymail_email',
    s_password: 'skymail_password',
    s_resin_url: 'resin_url',
    s_resin_platform_name: 'resin_platform_name',
    s_code_timeout: 'code_timeout',
    s_code_poll_interval: 'code_poll_interval',
    s_muse_url: 'muse_url',
    s_default_card_id: 'default_card_id',
    s_concurrency: 'concurrency',
    s_screenshot_interval: 'screenshot_interval',
    s_step_timeout: 'step_timeout',
    s_proxy: 'proxy',
    s_timezone: 'timezone',
    s_locale: 'locale',
  };
  const SETTING_CHECKS = {
    s_headless: 'headless',
    s_auto_fill_card: 'auto_fill_card',
    s_stop_at_verification: 'stop_at_verification',
    s_manual_takeover: 'manual_takeover',
    s_resin_enabled: 'resin_enabled',
  };

  function fillSettings() {
    const s = state.settings || {};
    for (const [id, key] of Object.entries(SETTING_FIELDS)) {
      const el = $(id);
      if (!el) continue;
      if (isEditing(id)) continue;   // 正在编辑 / 未保存，不要覆盖
      if (key === 'skymail_password') {
        el.value = '';
        el.placeholder = s.skymail_password_set ? '已设置（留空表示不修改）' : '未设置';
      } else {
        el.value = s[key] ?? '';
      }
    }
    for (const [id, key] of Object.entries(SETTING_CHECKS)) {
      const el = $(id);
      if (el && !isEditing(id)) el.checked = !!s[key];
    }
    const cEl = $('concurrency');
    if (cEl && !isEditing('concurrency')) cEl.value = s.concurrency || 1;
    const lvEl = $('liveView');
    if (lvEl && !isEditing('liveView')) lvEl.checked = s.live_view !== false;
    renderViewerState();
    fillCardSelect();
  }

  function setupSettings() {
    $('btnSaveSettings').addEventListener('click', guard(() => withBusy(
      $('btnSaveSettings'), '保存中…', async () => {
        const patch = {};
        for (const [id, key] of Object.entries(SETTING_FIELDS)) {
          const el = $(id);
          if (!el) continue;
          if (key === 'skymail_password' && !el.value) continue;
          let v = el.value;
          if (['code_timeout', 'concurrency', 'step_timeout'].includes(key)) v = parseInt(v, 10) || 0;
          if (['code_poll_interval', 'screenshot_interval'].includes(key)) v = parseFloat(v) || 0;
          patch[key] = v;
        }
        for (const [id, key] of Object.entries(SETTING_CHECKS)) {
          const el = $(id);
          if (el) patch[key] = el.checked;
        }
        await api('PUT', '/api/settings', patch);
        clearDirty();
        $('settingsResult').textContent = '已保存 ✓';
        toast('设置已保存', 'ok');
      },
    )));

    $('btnTestSkymail').addEventListener('click', guard(() => withBusy(
      $('btnTestSkymail'), '测试中…', async () => {
        $('skymailResult').textContent = '测试中…';
        const r = await api('POST', '/api/skymail/test', {
          skymail_base_url: $('s_base').value.trim(),
          skymail_email: $('s_email').value.trim(),
          skymail_password: $('s_password').value,
          create: $('skymailCreate').checked,
        });
        const parts = [`✓ ${r.user}`];
        parts.push(`可用域名 ${(r.domains || []).join(', ') || '无'}`);
        parts.push(`可见邮箱 ${r.accounts.length} 个`);
        if (r.email_list_ok === false) {
          parts.push('⚠️ /api/email/list 不可用，将改用 /api/allEmail/list 收码');
        }
        if (r.created) parts.push(`试建成功：${r.created.email}`);
        $('skymailResult').textContent = parts.join(' | ');
      },
    )));

    $('btnTestResin').addEventListener('click', guard(() => withBusy(
      $('btnTestResin'), '测试中…', async () => {
        $('resinResult').textContent = '测试中…（反代打两次验粘性 + 正代一次）';
        const r = await api('POST', '/api/resin/test', {
          resin_url: $('s_resin_url').value.trim(),
          resin_platform_name: $('s_resin_platform_name').value.trim(),
        });
        const parts = [`Platform=${r.config.platform}`];
        if (r.reverse) {
          parts.push(`反代 IP=${r.reverse.ip}${r.reverse.sticky ? ' ✓粘性' : ' ✗粘性异常'}`);
        } else {
          parts.push(`反代失败：${r.reverse_error || '未知'}`);
        }
        if (r.forward) {
          parts.push(`正代 IP=${r.forward.ip}`);
        } else {
          parts.push(`正代失败：${r.forward_error || '未知'}`);
        }
        if (typeof r.same_ip === 'boolean') {
          parts.push(r.same_ip ? '✓ 正反代同一出口' : '⚠ 正反代出口不同');
        }
        $('resinResult').textContent = parts.join(' | ');
        if (r.hint) toast(r.hint, 'err');
      },
    )));

    $('btnCreate').addEventListener('click', guard(createTasks));
    $('btnStartAll').addEventListener('click', guard(async () => {
      const r = await api('POST', '/api/tasks/start-all', {});
      toast(`已启动 ${r.started} 个任务`, 'ok');
    }));
    $('btnStopAll').addEventListener('click', guard(async () => {
      const r = await api('POST', '/api/tasks/stop-all');
      toast(`已停止 ${r.stopped} 个任务`, 'ok');
    }));
    $('btnClear').addEventListener('click', guard(async () => {
      const r = await api('POST', '/api/tasks/clear', { only_done: true });
      toast(`已清理 ${r.removed} 条记录`, 'ok');
    }));
  }

  async function createTasks() {
    const count = Math.max(1, Math.min(500, Number($('count').value) || 1));
    const concurrency = Math.max(1, Math.min(8, Number($('concurrency').value) || 1));
    $('count').value = count;
    $('concurrency').value = concurrency;
    const r = await api('POST', '/api/tasks', {
      count,
      concurrency,
      card_id: $('taskCard').value,
      live_view: $('liveView').checked,
      autostart: $('autostart').checked,
    });
    toast(`已创建 ${r.created} 个任务，并发 ${r.concurrency}`, 'ok');
    if (r.tasks && r.tasks.length) selectTask(r.tasks[0].id);
  }

  // ------------------------------------------------------------------
  // 弹窗（提供卡信息）
  // ------------------------------------------------------------------

  function openCardModal(task) {
    const opts = state.cards.map((c) =>
      `<option value="${c.id}">${esc(c.label)} ${esc(c.pan_masked)}</option>`).join('');
    $('modalTitle').textContent = '提供支付卡信息';
    $('modalDesc').textContent =
      `任务 ${task.email || ''} 正停在年龄验证页，需要一张卡继续。`;
    $('modalBody').innerHTML = `
      <label class="fld"><span>使用已保存的卡片</span>
        <select id="m_card"><option value="">— 手动填写 —</option>${opts}</select></label>
      <div id="m_manual">
        <div class="row">
          <label class="fld" style="flex:2 1 200px"><span>卡号</span>
            <input id="m_number" inputmode="numeric" placeholder="4242424242424242"></label>
          <label class="fld"><span>有效期</span>
            <input id="m_exp" placeholder="MM/YY" maxlength="7"></label>
          <label class="fld"><span>CVV</span>
            <input id="m_cvc" inputmode="numeric" placeholder="123" maxlength="4"></label>
        </div>
        <label class="chk"><input type="checkbox" id="m_save" checked> 同时保存到卡片库</label>
      </div>`;
    $('modal').classList.remove('hidden');

    const sync = () => {
      $('m_manual').style.display = $('m_card').value ? 'none' : 'block';
    };
    $('m_card').addEventListener('change', sync);
    sync();

    $('m_exp').addEventListener('input', (e) => {
      const d = e.target.value.replace(/\D/g, '').slice(0, 4);
      e.target.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
    });

    $('modalCancel').onclick = () => { $('modal').classList.add('hidden'); };
    $('modalOk').onclick = guard(async () => {
      const cardId = $('m_card').value;
      if (cardId) {
        await api('POST', `/api/tasks/${task.id}/card`, { card_id: cardId });
      } else {
        const number = $('m_number').value.replace(/\D/g, '');
        const exp = parseExpiry($('m_exp').value);
        const cvc = $('m_cvc').value.replace(/\D/g, '');
        if (number.length < 12) return toast('卡号无效', 'err');
        if (!exp) return toast('有效期格式应为 MM/YY', 'err');
        if (!cvc) return toast('请填写 CVV', 'err');
        await api('POST', `/api/tasks/${task.id}/card`, {
          number,
          exp_month: exp.month,
          exp_year: exp.year,
          cvc,
          save_as_card: $('m_save').checked,
        });
      }
      $('modal').classList.add('hidden');
      toast('卡信息已提交，任务继续', 'ok');
    });
  }

  // ------------------------------------------------------------------
  // 会话文件
  // ------------------------------------------------------------------

  async function renderSessions() {
    try {
      const list = await api('GET', '/api/sessions');
      const box = $('sessionList');
      box.innerHTML = list.length
        ? list.map((s) => `<div class="task-item"><div class="task-top">
             <span class="task-email">${esc(s.name)}</span>
             <a class="btn mini" href="/api/sessions/${encodeURIComponent(s.name)}" target="_blank">下载</a>
           </div><div class="task-sub"><span class="grow">${esc(s.mtime)} · ${s.size} B</span></div></div>`).join('')
        : '<div class="muted small">暂无登录态文件</div>';
    } catch (_) { /* ignore */ }
  }

  // ------------------------------------------------------------------
  // Tab
  // ------------------------------------------------------------------

  function showTab(name) {
    document.querySelectorAll('.tab-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-panel').forEach((p) =>
      p.classList.toggle('active', p.id === 'tab-' + name));
    if (name === 'settings') renderSessions();
  }

  // ------------------------------------------------------------------
  // 启动
  // ------------------------------------------------------------------

  async function reload() {
    const s = await api('GET', '/api/state');
    state.settings = s.settings;
    state.cards = s.cards;
    state.tasks = s.tasks;
    state.resin = s.resin || null;
    fillSettings();
    renderCards();
    renderResinPill();
    renderTasks();
    // 不要重新 selectTask：那会清空日志和实时画面，每 8 秒闪一次
    if (state.selected && !state.tasks.some((t) => t.id === state.selected)) {
      state.selected = null;
      $('logs').innerHTML = '';
      state.renderedLogs = 0;
    }
    renderDetail();
    syncLogs();
    const running = state.tasks.filter((t) => t.status === 'running').length;
    $('statLine').textContent = `共 ${state.tasks.length} 个任务 / 运行中 ${running}`;
  }

  async function boot() {
    document.querySelectorAll('.tab-btn').forEach((b) =>
      b.addEventListener('click', () => showTab(b.dataset.tab)));
    setupViewer();
    setupCards();
    setupSettings();

    // 实时画面开关随手切换就生效（不用等下次建任务）
    $('liveView').addEventListener('change', guard(async (e) => {
      const s = await api('PUT', '/api/settings', { live_view: e.target.checked });
      state.settings = s;
      dirtyFields.delete('liveView');
      markDirty('liveView', false);
      renderViewerState();
      toast(e.target.checked ? '实时画面已开启' : '实时画面已关闭', 'ok');
    }));

    bindDirtyGuard();

    try {
      await ensureToken();
    } catch (_) { /* ignore */ }

    try { await reload(); } catch (e) { toast('加载状态失败：' + e.message, 'err'); }
    connect();
    // 定时兜底刷新，防止漏掉 WebSocket 消息
    setInterval(() => {
      const running = state.tasks.filter((t) => t.status === 'running').length;
      $('statLine').textContent = `共 ${state.tasks.length} 个任务 / 运行中 ${running}`;
      if (document.visibilityState === 'visible') {
        reload().catch(() => {});
      }
    }, 8000);  }

  document.addEventListener('DOMContentLoaded', boot);
})();
