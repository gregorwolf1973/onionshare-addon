// OnionShare HA addon frontend
// Single-page vanilla JS - no build step.

const state = {
  mode: 'share',
  root: 'share',
  currentPath: '/share',
  selected: [],   // list of absolute paths
  sessions: [],
};

const MODE_DESCRIPTIONS = {
  share:   'Pick files or folders to share. Recipients get a download link.',
  receive: 'Anyone with the link can upload files. Uploads land in /share/onionshare-uploads.',
  website: 'Pick a folder containing a static website. It will be served read-only over Tor.',
  chat:    'Anonymous chat room. No files needed - just start it and share the link.',
};

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const url = './api' + path;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Mode tabs
// ---------------------------------------------------------------------------

document.querySelectorAll('.mode-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    state.mode = tab.dataset.mode;
    document.getElementById('mode-description').textContent = MODE_DESCRIPTIONS[state.mode];

    const picker = document.getElementById('picker-section');
    // Chat doesn't need files; receive needs none either
    if (state.mode === 'chat' || state.mode === 'receive') {
      picker.classList.add('hidden');
    } else {
      picker.classList.remove('hidden');
    }
  });
});

// ---------------------------------------------------------------------------
// Filesystem browser
// ---------------------------------------------------------------------------

document.getElementById('root-select').addEventListener('change', (e) => {
  state.root = e.target.value;
  state.currentPath = '/' + state.root;
  loadDir('');
});

document.getElementById('up-btn').addEventListener('click', () => {
  const rootPath = '/' + state.root;
  if (state.currentPath === rootPath) return;
  // pop one segment
  const parts = state.currentPath.split('/').filter(Boolean);
  parts.pop();
  loadDir(parts.slice(1).join('/'));  // skip the root prefix
});

async function loadDir(subpath) {
  const list = document.getElementById('file-list');
  list.innerHTML = '<li>Loading…</li>';
  try {
    const params = new URLSearchParams({ root: state.root, path: subpath });
    const data = await api('/browse?' + params);
    state.currentPath = data.path;
    document.getElementById('current-path').textContent = data.path;

    if (data.entries.length === 0) {
      list.innerHTML = '<li><em>Empty folder</em></li>';
      return;
    }

    list.innerHTML = '';
    for (const entry of data.entries) {
      const li = document.createElement('li');
      li.className = entry.is_dir ? 'dir' : 'file';

      const icon = entry.is_dir ? '📁' : '📄';
      const nameSpan = document.createElement('span');
      nameSpan.className = 'file-name';
      nameSpan.textContent = `${icon} ${entry.name}`;

      const meta = document.createElement('span');
      meta.className = 'file-meta';
      if (entry.is_dir) {
        meta.textContent = 'dir';
      } else {
        meta.textContent = humanSize(entry.size);
      }

      const addBtn = document.createElement('button');
      addBtn.className = 'add-btn';
      addBtn.textContent = '+';
      addBtn.title = 'Add to selection';
      addBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        addToSelection(entry.path);
      });

      li.appendChild(nameSpan);
      li.appendChild(meta);
      li.appendChild(addBtn);

      if (entry.is_dir) {
        nameSpan.style.cursor = 'pointer';
        nameSpan.addEventListener('click', () => {
          const rel = entry.path.replace('/' + state.root, '').replace(/^\//, '');
          loadDir(rel);
        });
      }

      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li style="color: var(--error)">Error: ${e.message}</li>`;
  }
}

function addToSelection(path) {
  if (state.selected.includes(path)) return;
  state.selected.push(path);
  renderSelection();
}

function removeFromSelection(path) {
  state.selected = state.selected.filter(p => p !== path);
  renderSelection();
}

function renderSelection() {
  const ul = document.getElementById('selected-list');
  ul.innerHTML = '';
  if (state.selected.length === 0) {
    ul.innerHTML = '<li><em>(nothing selected)</em></li>';
    return;
  }
  for (const path of state.selected) {
    const li = document.createElement('li');
    const span = document.createElement('span');
    span.textContent = path;
    const btn = document.createElement('button');
    btn.textContent = '×';
    btn.title = 'Remove';
    btn.addEventListener('click', () => removeFromSelection(path));
    li.appendChild(span);
    li.appendChild(btn);
    ul.appendChild(li);
  }
}

function humanSize(bytes) {
  if (bytes == null) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

// ---------------------------------------------------------------------------
// Start session
// ---------------------------------------------------------------------------

document.getElementById('start-btn').addEventListener('click', async () => {
  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  btn.textContent = 'Starting…';

  const body = {
    mode: state.mode,
    paths: state.selected,
    public: document.getElementById('opt-public').checked,
    title: document.getElementById('opt-title').value,
    label: document.getElementById('opt-label').value,
  };

  try {
    await api('/session', { method: 'POST', body: JSON.stringify(body) });
    // Clear selection on success
    state.selected = [];
    renderSelection();
    document.getElementById('opt-title').value = '';
    document.getElementById('opt-label').value = '';
    document.getElementById('opt-public').checked = false;
    await refreshStatus();
  } catch (e) {
    alert('Failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Start session';
  }
});

// ---------------------------------------------------------------------------
// Sessions list
// ---------------------------------------------------------------------------

function renderSessions(sessions) {
  const container = document.getElementById('sessions-list');
  if (sessions.length === 0) {
    container.innerHTML = '<div class="empty">No active sessions.</div>';
    return;
  }

  container.innerHTML = '';
  for (const s of sessions) {
    const div = document.createElement('div');
    div.className = 'session';

    const header = document.createElement('div');
    header.className = 'session-header';
    header.innerHTML = `
      <div>
        <span class="session-mode-badge">${s.mode}</span>
        <span class="session-label">${escapeHtml(s.label)}</span>
      </div>
      <div>
        <span class="session-status ${s.status}">${s.status}</span>
        <button class="btn-danger" data-stop="${s.id}">Stop</button>
      </div>
    `;
    div.appendChild(header);

    if (s.paths && s.paths.length > 0) {
      const paths = document.createElement('div');
      paths.className = 'session-paths';
      paths.textContent = '📁 ' + s.paths.join(', ');
      div.appendChild(paths);
    }

    if (s.onion_url) {
      const urlBox = document.createElement('div');
      urlBox.className = 'onion-url-box';
      urlBox.textContent = s.onion_url;
      const copyBtn = document.createElement('button');
      copyBtn.className = 'copy-btn';
      copyBtn.textContent = 'Copy';
      copyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(s.onion_url);
        copyBtn.textContent = '✓';
        setTimeout(() => copyBtn.textContent = 'Copy', 1500);
      });
      urlBox.appendChild(copyBtn);
      div.appendChild(urlBox);

      if (s.private_key) {
        const keyBox = document.createElement('div');
        keyBox.className = 'onion-url-box';
        keyBox.innerHTML = `<small style="color: var(--text-dim)">Private key (needed to access):</small><br>${escapeHtml(s.private_key)}`;
        div.appendChild(keyBox);
      }
    }

    if (s.log_tail && s.log_tail.length > 0) {
      const logBox = document.createElement('div');
      logBox.className = 'session-log';
      logBox.textContent = s.log_tail.join('\n');
      div.appendChild(logBox);
    }

    container.appendChild(div);
  }

  container.querySelectorAll('[data-stop]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const sid = btn.dataset.stop;
      if (!confirm('Stop this session?')) return;
      try {
        await api('/session/' + sid, { method: 'DELETE' });
        await refreshStatus();
      } catch (e) {
        alert('Failed: ' + e.message);
      }
    });
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// ---------------------------------------------------------------------------
// Status polling
// ---------------------------------------------------------------------------

async function refreshStatus() {
  try {
    const data = await api('/status');
    const pill = document.getElementById('tor-status');
    if (data.tor.running) {
      pill.textContent = 'Tor ✓ running';
      pill.className = 'status-pill ok';
    } else {
      pill.textContent = 'Tor ✗ not reachable';
      pill.className = 'status-pill bad';
    }
    state.sessions = data.sessions;
    renderSessions(data.sessions);
  } catch (e) {
    console.error('Status error:', e);
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

renderSelection();
loadDir('');
refreshStatus();
setInterval(refreshStatus, 3000);
