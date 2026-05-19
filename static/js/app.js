let lastLogCount = 0;
let lastLogSignature = '';
let latestLogs = [];
let logsPaused = false;
let connectNotificationPending = false;
let tunnelStartRequested = false;

(function () {
  const btn = document.querySelector('[data-theme-toggle]');
  const html = document.documentElement;
  const savedTheme = localStorage.getItem('theme');
  let theme = savedTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

  function applyTheme(nextTheme) {
    theme = nextTheme;
    html.setAttribute('data-theme', theme);
    btn.textContent = theme === 'dark' ? '☾' : '☀';
    btn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
    btn.setAttribute('title', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  }

  applyTheme(theme);

  btn.addEventListener('click', () => {
    btn.classList.add('is-switching');
    window.setTimeout(() => {
      applyTheme(theme === 'dark' ? 'light' : 'dark');
      localStorage.setItem('theme', theme);
      btn.classList.remove('is-switching');
    }, 90);
  });
})();

function showToast(msg, type = 'info') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ'}</span><span>${msg}</span>`;
  c.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

async function ensureNotificationPermission() {
  if (!('Notification' in window)) return false;
  if (Notification.permission === 'granted') return true;
  if (Notification.permission !== 'default') return false;

  try {
    const permission = await Notification.requestPermission();
    return permission === 'granted';
  } catch (e) {
    return false;
  }
}

async function notifyAction(title, message, type = 'info') {
  showToast(message, type);

  if (await ensureNotificationPermission()) {
    try {
      new Notification(title, {
        body: message,
        icon: '/assets/logo.png'
      });
    } catch (e) {
    }
  }
}

function escHtml(str = '') {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function renderLogs(logs) {
  latestLogs = Array.isArray(logs) ? logs : [];

  if (logsPaused) return;

  const signature = latestLogs.length
    ? `${latestLogs.length}:${latestLogs[latestLogs.length - 1].time}:${latestLogs[latestLogs.length - 1].message}`
    : '0';

  if (signature === lastLogSignature) return;
  lastLogSignature = signature;

  const body = document.getElementById('log-body');
  const empty = document.getElementById('log-empty');

  body.querySelectorAll('.log-entry').forEach(e => e.remove());

  if (latestLogs.length === 0) {
    empty.style.display = 'block';
    lastLogCount = 0;
    return;
  }

  empty.style.display = 'none';

  latestLogs.forEach(entry => {
    const level = ['info', 'success', 'warning', 'error'].includes(entry.level) ? entry.level : 'info';
    const row = document.createElement('div');
    row.className = 'log-entry';
    row.innerHTML = `
      <span class="log-time">${escHtml(entry.time || '')}</span>
      <span class="log-badge badge-${level}">${escHtml(level)}</span>
      <span class="log-msg">${escHtml(entry.message || '')}</span>
    `;
    body.appendChild(row);
  });

  lastLogCount = latestLogs.length;
  body.scrollTop = body.scrollHeight;
}

function setCardState(cardId, state) {
  const card = document.getElementById(cardId);
  if (!card) return;

  if (state === 'success') {
    card.style.background = 'var(--success-hl)';
  } else if (state === 'warning') {
    card.style.background = 'var(--warning-hl)';
  } else {
    card.style.background = 'var(--surface-2)';
  }
}

function updateStatus(data) {
  const vpnConnected = !!data.vpn_connected;
  const ssoInProgress = !!data.sso_in_progress;
  const ssoCompleted = !!data.sso_completed;
  const cookieFound = !!data.svpn_cookie_found;
  const configLoaded = !!data.vpn_config_loaded;
  const config = data.vpn_config_summary || {};
  const authId = data.last_auth_id || '';
  const callbackAt = data.last_callback || '';

  const ssoText = document.getElementById('sso-status-text');
  const ssoMeta = document.getElementById('sso-status-meta');
  const vpnText = document.getElementById('vpn-status-text');
  const vpnMeta = document.getElementById('vpn-status-meta');
  const gatewayText = document.getElementById('gateway-status-text');
  const gatewayMeta = document.getElementById('gateway-status-meta');
  const btnConnect = document.getElementById('btn-vpn-connect');
  const btnDisconnect = document.getElementById('btn-vpn-disconnect');

  if (ssoInProgress) {
    ssoText.textContent = 'Sedang Login...';
    ssoMeta.textContent = `Menunggu callback di ${data.callback_url || 'callback listener'}`;
    setCardState('status-sso', 'warning');
  } else if (ssoCompleted) {
    ssoText.textContent = 'Selesai';
    ssoMeta.textContent = authId ? `Auth ID: ${authId}` : `Callback diterima ${callbackAt}`;
    setCardState('status-sso', 'success');
  } else {
    ssoText.textContent = 'Idle';
    ssoMeta.textContent = 'Belum ada callback.';
    setCardState('status-sso', 'default');
  }

  if (vpnConnected) {
    vpnText.textContent = 'Terhubung';
    vpnMeta.textContent = 'Tunnel VPN aktif.';
    setCardState('status-vpn', 'success');
  } else {
    vpnText.textContent = 'Tidak Terhubung';
    vpnMeta.textContent = configLoaded
      ? 'Sesi gateway siap, engine tunnel belum berjalan.'
      : ssoCompleted
        ? 'SSO selesai, sesi gateway belum siap.'
        : 'Tunnel belum aktif.';
    setCardState('status-vpn', 'default');
  }

  if (configLoaded) {
    gatewayText.textContent = 'Config Terbaca';
    gatewayMeta.textContent = [
      config.assigned_addr ? `IP: ${config.assigned_addr}` : '',
      Number.isFinite(config.route_count) ? `Routes: ${config.route_count}` : '',
      Array.isArray(config.dns_servers) ? `DNS: ${config.dns_servers.length}` : ''
    ].filter(Boolean).join(' | ') || 'Konfigurasi diterima.';
    setCardState('status-gateway', 'success');
  } else if (cookieFound) {
    gatewayText.textContent = 'Cookie Ada';
    gatewayMeta.textContent = 'Sesi gateway diterima, config belum terbaca.';
    setCardState('status-gateway', 'warning');
  } else if (data.last_error) {
    gatewayText.textContent = 'Gagal';
    gatewayMeta.textContent = data.last_error;
    setCardState('status-gateway', 'warning');
  } else {
    gatewayText.textContent = 'Belum Siap';
    gatewayMeta.textContent = 'Menunggu sesi gateway.';
    setCardState('status-gateway', 'default');
  }

  btnConnect.disabled = ssoInProgress || vpnConnected;
  btnConnect.textContent = ssoInProgress ? 'Connecting...' : 'Connect';
  btnDisconnect.disabled = !(vpnConnected || ssoInProgress || ssoCompleted || cookieFound || configLoaded);

  if (connectNotificationPending && configLoaded && !vpnConnected && !tunnelStartRequested) {
    tunnelStartRequested = true;
    apiCall('/api/connect/vpn', {})
      .catch(e => {
        connectNotificationPending = false;
        tunnelStartRequested = false;
        showToast(e.message || 'Gagal memulai tunnel VPN.', 'error');
      });
  }

  if (connectNotificationPending && vpnConnected) {
    connectNotificationPending = false;
    tunnelStartRequested = false;
    notifyAction('VPN Helper - VPN connected', 'VPN connected.', 'success');
  }
}

async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    updateStatus(data);
    renderLogs(data.logs || []);
  } catch (e) {
  }
}

setInterval(pollStatus, 1500);
pollStatus();

async function apiCall(url, body = {}) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = { success: false, message: 'Respons server tidak valid.' };
  }

  if (!res.ok) {
    throw new Error(data.message || `HTTP ${res.status}`);
  }

  return data;
}

async function connectVPN() {
  const btn = document.getElementById('btn-vpn-connect');
  btn.disabled = true;

  try {
    const data = await apiCall('/api/connect/sso', {});
    connectNotificationPending = true;
    tunnelStartRequested = false;
    showToast(data.message || 'Connect dimulai.', 'success');
    await pollStatus();
  } catch (e) {
    connectNotificationPending = false;
    showToast(e.message || 'Gagal connect.', 'error');
    btn.disabled = false;
  }
}

async function disconnectVPN() {
  try {
    const data = await apiCall('/api/logout', {});
    connectNotificationPending = false;
    tunnelStartRequested = false;
    await notifyAction('VPN Helper - VPN disconnected', 'VPN disconnected.', 'info');
    await pollStatus();
  } catch (e) {
    showToast(e.message || 'Gagal disconnect.', 'error');
  }
}

async function clearLogs() {
  try {
    await apiCall('/api/logs/clear', {});
    lastLogCount = 0;
    lastLogSignature = '';
    latestLogs = [];
    await pollStatus();
  } catch (e) {
    showToast(e.message || 'Gagal membersihkan log.', 'error');
  }
}

function toggleLogPause() {
  logsPaused = !logsPaused;
  const btn = document.getElementById('log-pause-btn');
  btn.textContent = logsPaused ? 'Resume' : 'Pause';

  if (!logsPaused) {
    lastLogSignature = '';
    renderLogs(latestLogs);
  }
}

async function copyLogs() {
  const text = latestLogs.map(entry => {
    const time = entry.time || '';
    const level = (entry.level || 'info').toUpperCase();
    const message = entry.message || '';
    return `[${time}] [${level}] ${message}`;
  }).join('\n');

  try {
    await navigator.clipboard.writeText(text);
    showToast('Log disalin.', 'success');
  } catch (e) {
    showToast('Browser menolak clipboard. Pause lalu copy manual.', 'error');
  }
}

