const tg = window.Telegram?.WebApp;
const notice = document.querySelector('#notice');
const accountNode = document.querySelector('#account');
const tasksNode = document.querySelector('#tasks');
const adminNode = document.querySelector('#admin');
const editor = document.querySelector('#editor');
const form = document.querySelector('#taskForm');
let token = sessionStorage.getItem('nb_token') || '';
let tasks = [];

const lines = value => value.split('\n').map(v => v.trim()).filter(Boolean);
const textLines = value => (value || []).join('\n');

async function request(path, options = {}, retried = false) {
  const headers = {...(options.headers || {})};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {...options, headers});
  if (response.status === 401 && token && !retried && path !== '/api/auth/miniapp') {
    token = '';
    sessionStorage.removeItem('nb_token');
    await authenticate();
    return request(path, options, true);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

async function authenticate() {
  if (token) return;
  if (!tg?.initData) throw new Error('请从 Telegram Bot 打开 Mini App');
  const result = await request('/api/auth/miniapp', {method: 'POST', body: JSON.stringify({init_data: tg.initData})});
  token = result.token;
  sessionStorage.setItem('nb_token', token);
}

async function load() {
  try {
    await authenticate();
    const [me, account, taskList] = await Promise.all([
      request('/api/me'),
      request('/api/account').catch(error => ({error: error.message})),
      request('/api/tasks')
    ]);
    notice.textContent = `租户 ${me.tenant_id}${me.is_admin ? ' · 管理员' : ''}`;
    accountNode.innerHTML = account.error
      ? `<strong>尚未绑定账号</strong><p class="muted">请返回 Bot 使用 /login。</p>`
      : `<strong>${escapeHtml(account.display_name || account.username || account.tenant_id)}</strong><p class="muted">账号状态：${account.status}</p>`;
    tasks = taskList;
    renderTasks();
    if (me.is_admin) await loadAdmin();
  } catch (error) {
    notice.className = 'error';
    notice.textContent = error.message;
  }
}

function escapeHtml(value) {
  const node = document.createElement('div'); node.textContent = String(value); return node.innerHTML;
}

function renderTasks() {
  tasksNode.innerHTML = tasks.length ? '' : '<div class="card">暂无任务</div>';
  for (const task of tasks) {
    const card = document.createElement('article'); card.className = 'card';
    card.innerHTML = `<div class="task-head"><strong>${escapeHtml(task.name)}</strong><span>${task.status}</span></div>
      <p class="muted">${task.mode} · ${task.sources.length} 个来源 → ${task.destinations.length} 个目标</p>
      ${task.last_error ? `<p class="error">${escapeHtml(task.last_error)}</p>` : ''}
      <div class="task-actions"><button data-action="edit">编辑</button><button data-action="validate">校验</button><button data-action="run" class="primary">运行</button><button data-action="pause">暂停</button><button data-action="watermark">上传水印</button><button data-action="delete">删除</button></div>`;
    card.querySelectorAll('button').forEach(button => button.onclick = () => taskAction(task, button.dataset.action));
    tasksNode.appendChild(card);
  }
}

async function taskAction(task, action) {
  try {
    if (action === 'edit') return openEditor(task);
    if (action === 'watermark') return uploadWatermark(task);
    if (action === 'delete') {
      if (!confirm(`删除任务“${task.name}”？`)) return;
      await request(`/api/tasks/${task.id}`, {method: 'DELETE'});
    } else {
      await request(`/api/tasks/${task.id}/${action}`, {method: 'POST'});
    }
    await load();
  } catch (error) { alert(error.message); }
}

function uploadWatermark(task) {
  const input = document.createElement('input');
  input.type = 'file'; input.accept = 'image/png';
  input.onchange = async () => {
    if (!input.files?.[0]) return;
    const body = new FormData(); body.append('file', input.files[0]);
    try {
      await request(`/api/tasks/${task.id}/watermark`, {method: 'POST', body});
      alert('水印已上传'); await load();
    } catch (error) { alert(error.message); }
  };
  input.click();
}

async function loadAdmin() {
  const [tenants, workers, errors] = await Promise.all([
    request('/api/admin/tenants'), request('/api/admin/workers'), request('/api/admin/errors')
  ]);
  adminNode.hidden = false;
  adminNode.innerHTML = `<h2>平台管理</h2>
    <details class="card"><summary>租户（${tenants.length}）</summary><pre>${escapeHtml(JSON.stringify(tenants, null, 2))}</pre></details>
    <details class="card"><summary>Worker 租约（${workers.length}）</summary><pre>${escapeHtml(JSON.stringify(workers, null, 2))}</pre></details>
    <details class="card"><summary>系统错误（${errors.length}）</summary><pre>${escapeHtml(JSON.stringify(errors, null, 2))}</pre></details>`;
}

function defaultTask() {
  return {name: '新转发任务', mode: 'live', sources: [], destinations: [], show_forwarded_from: false,
    comments: {}, bot_media: {}, live: {}, past: {}, schedule: {}, plugins: {filter: {check: false, users: {}, files: {}, text: {blacklist: [], whitelist: [], case_sensitive: false, regex: false}}}, watermark_asset_id: null};
}

function openEditor(task = null) {
  const value = task ? structuredClone(task) : defaultTask();
  document.querySelector('#taskId').value = value.id || '';
  document.querySelector('#revision').value = value.revision || '';
  document.querySelector('#name').value = value.name;
  document.querySelector('#mode').value = value.mode;
  document.querySelector('#sources').value = textLines(value.sources);
  document.querySelector('#destinations').value = textLines(value.destinations);
  const filter = value.plugins?.filter || {};
  document.querySelector('#filterEnabled').checked = !!filter.check;
  document.querySelector('#whitelist').value = textLines(filter.text?.whitelist);
  document.querySelector('#blacklist').value = textLines(filter.text?.blacklist);
  document.querySelector('#regex').checked = !!filter.text?.regex;
  document.querySelector('#caseSensitive').checked = !!filter.text?.case_sensitive;
  for (const key of ['id','tenant_id','status','revision','pending_action','resolved_sources','resolved_destinations','offsets','last_error','created_at','updated_at','name','mode','sources','destinations']) delete value[key];
  document.querySelector('#advanced').value = JSON.stringify(value, null, 2);
  editor.showModal();
}

form.onsubmit = async event => {
  event.preventDefault();
  try {
    const advanced = JSON.parse(document.querySelector('#advanced').value || '{}');
    advanced.name = document.querySelector('#name').value.trim();
    advanced.mode = document.querySelector('#mode').value;
    advanced.sources = lines(document.querySelector('#sources').value);
    advanced.destinations = lines(document.querySelector('#destinations').value);
    advanced.plugins ||= {}; advanced.plugins.filter ||= {}; advanced.plugins.filter.text ||= {};
    advanced.plugins.filter.check = document.querySelector('#filterEnabled').checked;
    advanced.plugins.filter.text.whitelist = lines(document.querySelector('#whitelist').value);
    advanced.plugins.filter.text.blacklist = lines(document.querySelector('#blacklist').value);
    advanced.plugins.filter.text.regex = document.querySelector('#regex').checked;
    advanced.plugins.filter.text.case_sensitive = document.querySelector('#caseSensitive').checked;
    const id = document.querySelector('#taskId').value;
    if (id) {
      await request(`/api/tasks/${id}`, {method: 'PUT', body: JSON.stringify({revision: Number(document.querySelector('#revision').value), task: advanced})});
    } else {
      await request('/api/tasks', {method: 'POST', body: JSON.stringify(advanced)});
    }
    editor.close(); await load();
  } catch (error) { alert(error.message); }
};

document.querySelector('#cancel').onclick = () => editor.close();
document.querySelector('#newTask').onclick = () => openEditor();
document.querySelector('#refresh').onclick = load;
tg?.ready(); tg?.expand(); load();
