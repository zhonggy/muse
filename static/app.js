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
  };

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

  // ------------------------------------------------------------------
  // WebSocket
  // ------------------------------------------------------------------

  function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const q = state.wsToken ? `?token=${encodeURIComponent(state.wsToken)}` : '';
    return `${proto}://${location.host}/ws${q}`;
  }

  function connect() {
    let ws;
    try { ws = new WebSocket(wsUrl()); } catch (e) { setTimeout(connect, 2500); return; }
    state.ws = ws;

    ws.onopen = () => {
      state.wsOk = true;
      $('conn').textContent = '已连接';
      $('conn').className = 'pill on';
    };
    ws.onclose = () => {
      state.wsOk = false;
      $('conn').textContent = '未连接';
      $('conn').className = 'pill off';
      setTimeout(connect, 2000);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      handle(msg);
    };
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
        fillSettings();
        renderCards();
        renderTasks();
        break;
      case 'log':
        if (msg.task_id === state.selected) appendLog(msg);
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
        <div class="task-top">${badge}${needs}<span class="task-email">${esc(t.email)}</span></div>
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
    $('live').removeAttribute('src');
    $('viewer').classList.remove('live');
    renderTasks();
    renderDetail();
    if (!id) { $('detailTitle').textContent = '实时画面'; $('detailActions').innerHTML = ''; return; }
    const t = state.tasks.find((x) => x.id === id);
    if (t) (t.logs || []).forEach(appendLog);
    const shot = state.shots[id];
    if (shot) showShot(shot);
  }

  function renderDetail() {
    const t = state.tasks.find((x) => x.id === state.selected);
    if (!t) return;
    $('detailTitle').textContent = t.email;
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
    const img = $('live');
    img.src = 'data:image/jpeg;base64,' + msg.data;
    $('viewer').classList.add('live');
  }

  // ------------------------------------------------------------------
  // 实时接管
  // ------------------------------------------------------------------

  function setupViewer() {
    const viewer = $('viewer');
    const img = $('live');

    viewer.addEventListener('click', (e) => {
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
    const dkeep = state.settings.default_card_id || '';
    dsel.innerHTML = '<option value="">（无）</option>'
      + state.cards.map((c) => `<option value="${c.id}">${esc(c.label)} ${esc(c.pan_masked)}</option>`).join('');
    dsel.value = dkeep;
  }

  function setupCards() {
    $('btnAddCard').addEventListener('click', guard(async () => {
      const payload = {
        label: $('c_label').value.trim(),
        number: $('c_number').value.trim(),
        exp_month: $('c_month').value.trim(),
        exp_year: $('c_year').value.trim(),
        cvc: $('c_cvc').value.trim(),
        holder: $('c_holder').value.trim(),
        postal: $('c_postal').value.trim(),
        country: $('c_country').value.trim(),
      };
      await api('POST', '/api/cards', payload);
      ['c_label', 'c_number', 'c_cvc', 'c_holder'].forEach((id) => { $(id).value = ''; });
      toast('卡片已加密保存', 'ok');
    }));
  }

  // ------------------------------------------------------------------
  // 设置
  // ------------------------------------------------------------------

  const SETTING_FIELDS = {
    s_base: 'skymail_base_url',
    s_email: 'skymail_email',
    s_password: 'skymail_password',
    s_code_timeout: 'code_timeout',
    s_code_poll_interval: 'code_poll_interval',
    s_muse_url: 'muse_url',
    s_birthday: 'birthday',
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
  };

  function fillSettings() {
    const s = state.settings || {};
    for (const [id, key] of Object.entries(SETTING_FIELDS)) {
      const el = $(id);
      if (!el) continue;
      if (key === 'skymail_password') {
        el.value = '';
        el.placeholder = s.skymail_password_set ? '已设置（留空表示不修改）' : '未设置';
      } else {
        el.value = s[key] ?? '';
      }
    }
    for (const [id, key] of Object.entries(SETTING_CHECKS)) {
      const el = $(id);
      if (el) el.checked = !!s[key];
    }
    if (!$('birthday').value) $('birthday').value = s.birthday || '1996-07-22';
    fillCardSelect();
  }

  function setupSettings() {
    $('btnSaveSettings').addEventListener('click', guard(async () => {
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
      $('settingsResult').textContent = '已保存 ✓';
      toast('设置已保存', 'ok');
    }));

    $('btnTestSkymail').addEventListener('click', guard(async () => {
      $('skymailResult').textContent = '测试中…';
      const r = await api('POST', '/api/skymail/test', {
        skymail_base_url: $('s_base').value.trim(),
        skymail_email: $('s_email').value.trim(),
        skymail_password: $('s_password').value,
      });
      $('skymailResult').textContent =
        `✓ ${r.user}，可见邮箱 ${r.accounts.length} 个：`
        + r.accounts.slice(0, 6).map((a) => a.email).join(', ');
    }));

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
    const emails = $('emails').value;
    if (!emails.trim()) return toast('请填写邮箱', 'err');
    const r = await api('POST', '/api/tasks', {
      emails,
      birthday: $('birthday').value || state.settings.birthday,
      card_id: $('taskCard').value,
      autostart: $('autostart').checked,
    });
    toast(`已创建 ${r.created} 个任务`, 'ok');
    $('emails').value = '';
    if (r.tasks && r.tasks.length) selectTask(r.tasks[0].id);
  }

  // ------------------------------------------------------------------
  // 弹窗（提供卡信息）
  // ------------------------------------------------------------------

  function openCardModal(task) {
    const opts = state.cards.map((c) =>
      `<option value="${c.id}">${esc(c.label)} ${esc(c.pan_masked)}</option>`).join('');
    $('modalTitle').textContent = '提供支付卡信息';
    $('modalDesc').textContent = `任务 ${task.email} 正停在年龄验证页，需要一张卡继续。`;
    $('modalBody').innerHTML = `
      <label class="fld"><span>使用已保存的卡片</span>
        <select id="m_card"><option value="">— 手动填写 —</option>${opts}</select></label>
      <div id="m_manual">
        <div class="grid3">
          <label class="fld"><span>卡号</span><input id="m_number" inputmode="numeric"></label>
          <label class="fld"><span>月</span><input id="m_month" placeholder="07"></label>
          <label class="fld"><span>年</span><input id="m_year" placeholder="2029"></label>
          <label class="fld"><span>CVV</span><input id="m_cvc" inputmode="numeric"></label>
          <label class="fld"><span>持卡人</span><input id="m_holder"></label>
          <label class="fld"><span>邮编</span><input id="m_postal"></label>
        </div>
        <label class="chk"><input type="checkbox" id="m_save" checked> 同时保存到卡片库</label>
      </div>`;
    $('modal').classList.remove('hidden');

    const sync = () => {
      $('m_manual').style.display = $('m_card').value ? 'none' : 'block';
    };
    $('m_card').addEventListener('change', sync);
    sync();

    $('modalCancel').onclick = () => { $('modal').classList.add('hidden'); };
    $('modalOk').onclick = guard(async () => {
      const cardId = $('m_card').value;
      const payload = cardId ? { card_id: cardId } : {
        number: $('m_number').value.trim(),
        exp_month: $('m_month').value.trim(),
        exp_year: $('m_year').value.trim(),
        cvc: $('m_cvc').value.trim(),
        holder: $('m_holder').value.trim(),
        postal: $('m_postal').value.trim(),
        save_as_card: $('m_save').checked,
        label: `手动录入 ${new Date().toLocaleDateString()}`,
      };
      await api('POST', `/api/tasks/${task.id}/card`, payload);
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
    fillSettings();
    renderCards();
    renderTasks();
    if (state.selected) selectTask(state.selected);
    const running = state.tasks.filter((t) => t.status === 'running').length;
    $('statLine').textContent = `共 ${state.tasks.length} 个任务 / 运行中 ${running}`;
  }

  async function boot() {
    document.querySelectorAll('.tab-btn').forEach((b) =>
      b.addEventListener('click', () => showTab(b.dataset.tab)));
    setupViewer();
    setupCards();
    setupSettings();

    try {
      const t = await api('GET', '/api/ws-token');
      state.wsToken = t.token || '';
    } catch (_) { state.wsToken = ''; }

    try { await reload(); } catch (e) { toast('加载状态失败：' + e.message, 'err'); }
    connect();
    // 定时兜底刷新，防止漏掉 WebSocket 消息
    setInterval(() => {
      const running = state.tasks.filter((t) => t.status === 'running').length;
      $('statLine').textContent = `共 ${state.tasks.length} 个任务 / 运行中 ${running}`;
      if (document.visibilityState === 'visible') {
        reload().catch(() => {});
      }
    }, 8000);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
