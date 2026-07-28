/* SRT Restreamer Dashboard Frontend
 *
 * Security model:
 * - Authentication is a server-side opaque session in an HttpOnly cookie.
 *   No token is ever stored in localStorage or placed in a URL.
 * - Mutating requests carry the CSRF token read from the `csrf_token`
 *   (non-HttpOnly) cookie in the `X-CSRF-Token` header.
 * - All dynamic content is built with DOM methods and textContent.
 *   No innerHTML with dynamic data, no inline event handlers.
 */
const API_BASE = '/api';
let ws = null;
let streamsData = [];
let lastStatsData = null;

// ==================== CSRF ====================
function getCookie(name) {
    const prefix = name + '=';
    for (const part of document.cookie.split(';')) {
        const trimmed = part.trim();
        if (trimmed.startsWith(prefix)) {
            return decodeURIComponent(trimmed.slice(prefix.length));
        }
    }
    return null;
}

function getCsrfToken() {
    return getCookie('csrf_token');
}

// ==================== AUTH ====================
async function checkAuth() {
    try {
        const res = await fetch(`${API_BASE}/auth/me`, { credentials: 'same-origin' });
        if (res.status === 401) {
            window.location.href = '/login';
            return false;
        }
        return res.ok;
    } catch (err) {
        window.location.href = '/login';
        return false;
    }
}

async function logout() {
    try {
        await fetch(`${API_BASE}/auth/logout`, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRF-Token': getCsrfToken() || '' }
        });
    } catch (err) {
        // Best effort; session may already be gone.
    }
    window.location.href = '/login';
}

async function apiRequest(endpoint, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = { ...(options.headers || {}) };

    if (options.json !== undefined) {
        headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(options.json);
        delete options.json;
    }
    if (method !== 'GET' && method !== 'HEAD') {
        headers['X-CSRF-Token'] = getCsrfToken() || '';
    }

    try {
        const res = await fetch(`${API_BASE}${endpoint}`, {
            ...options,
            headers,
            credentials: 'same-origin'
        });
        if (res.status === 401) {
            window.location.href = '/login';
            return null;
        }
        return res;
    } catch (err) {
        showToast('Connection error', 'error');
        return null;
    }
}

// ==================== UI HELPERS ====================
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function showToast(message, type = 'success') {
    let container = document.querySelector('.toast-container');
    if (!container) {
        container = el('div', 'toast-container');
        document.body.appendChild(container);
    }
    const toast = el('div', `toast ${type}`, message);
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function showModal(id) {
    document.getElementById(id).classList.add('show');
}

function closeModal(id) {
    document.getElementById(id).classList.remove('show');
}

function makeStatusBadge(status) {
    const badge = el('span', `status-badge ${status || 'disconnected'}`);
    badge.appendChild(el('span', 'dot'));
    badge.appendChild(document.createTextNode((status || 'disconnected').toUpperCase()));
    return badge;
}

// ==================== STREAMS ====================
async function loadStreams() {
    const res = await apiRequest('/inputs');
    if (!res || !res.ok) return;

    const inputs = await res.json();
    for (const input of inputs) {
        const outRes = await apiRequest(`/outputs/${input.id}`);
        if (outRes && outRes.ok) {
            input.outputs = await outRes.json();
            input.outputs_count = input.outputs.length;
        } else {
            input.outputs = [];
            input.outputs_count = 0;
        }
    }
    streamsData = inputs;
    renderStreams();
}

function renderStreams() {
    const container = document.getElementById('streamsContainer');
    container.replaceChildren();

    if (streamsData.length === 0) {
        const empty = el('div', 'empty-state');
        empty.style.gridColumn = '1 / -1';
        empty.appendChild(el('div', 'icon', '📡'));
        empty.appendChild(el('h3', null, 'No input streams configured'));
        empty.appendChild(el('p', null, 'Add your first SRT input stream to get started'));
        container.appendChild(empty);
        return;
    }

    for (const stream of streamsData) {
        container.appendChild(renderStreamCard(stream));
    }
}

function renderStreamCard(stream) {
    const card = el('div', 'stream-card');
    card.dataset.id = stream.id;

    // ---- header ----
    const header = el('div', 'card-header');
    const title = el('div', 'card-title');
    title.appendChild(makeStatusBadge(stream.status));
    const nameEl = el('span', null, stream.name);
    nameEl.style.cssText = 'font-weight: 600; font-size: 15px;';
    title.appendChild(nameEl);

    const actions = el('div', 'card-actions');
    if (stream.is_active) {
        const stopBtn = el('button', 'btn-stop', 'Stop');
        stopBtn.addEventListener('click', () => stopInput(stream.id));
        actions.appendChild(stopBtn);
    } else {
        const startBtn = el('button', 'btn-start', 'Start');
        startBtn.addEventListener('click', () => startInput(stream.id));
        actions.appendChild(startBtn);
    }

    const slateInput = el('input');
    slateInput.type = 'file';
    slateInput.accept = 'image/*';
    slateInput.style.display = 'none';
    slateInput.addEventListener('change', () => uploadSlate(stream.id, slateInput));

    const slateBtn = el('button', 'btn-icon', '🖼');
    slateBtn.title = 'Upload slate';
    slateBtn.addEventListener('click', () => slateInput.click());
    const delSlateBtn = el('button', 'btn-icon', '🚫');
    delSlateBtn.title = 'Remove slate';
    delSlateBtn.addEventListener('click', () => deleteSlate(stream.id));
    const editBtn = el('button', 'btn-icon', '✎');
    editBtn.title = 'Edit';
    editBtn.addEventListener('click', () => editInput(stream.id));
    const delBtn = el('button', 'btn-icon', '🗑');
    delBtn.title = 'Delete';
    delBtn.addEventListener('click', () => deleteInput(stream.id));

    actions.append(slateInput, slateBtn, delSlateBtn, editBtn, delBtn);
    header.append(title, actions);
    card.appendChild(header);

    // ---- body ----
    const body = el('div', 'card-body');

    const thumbWrap = el('div', 'thumbnail-container');
    const img = el('img');
    img.alt = 'Stream preview';
    // Session cookie is sent automatically; no token in the URL.
    img.src = `/api/inputs/${stream.id}/thumbnail?t=${Date.now()}`;
    img.addEventListener('error', () => {
        img.style.display = 'none';
        if (!thumbWrap.querySelector('.thumbnail-placeholder')) {
            thumbWrap.prepend(el('div', 'thumbnail-placeholder', 'No preview available'));
        }
    });
    const overlay = el('div', 'thumbnail-overlay', stream.srt_url);
    thumbWrap.append(img, overlay);
    body.appendChild(thumbWrap);

    const statsGrid = el('div', 'stats-grid');
    statsGrid.id = `stats-${stream.id}`;
    for (const [key, label] of [['bitrate', 'Bitrate'], ['fps', 'FPS'], ['speed', 'Speed']]) {
        const box = el('div', 'stat-box');
        const value = el('div', 'value', '-');
        value.id = `${key}-${stream.id}`;
        box.append(value, el('div', 'label', label));
        statsGrid.appendChild(box);
    }
    body.appendChild(statsGrid);

    const outputsSection = el('div', 'outputs-section');
    const outputsHeader = el('div', 'outputs-header');
    outputsHeader.appendChild(el('h4', null, `Output Destinations (${stream.outputs_count || 0})`));
    const addOutBtn = el('button', 'btn-primary btn-small', '+ Add Output');
    addOutBtn.addEventListener('click', () => showAddOutputModal(stream.id));
    outputsHeader.appendChild(addOutBtn);

    const outputsList = el('div', 'outputs-list');
    outputsList.id = `outputs-${stream.id}`;
    renderOutputsList(stream, outputsList);

    outputsSection.append(outputsHeader, outputsList);
    body.appendChild(outputsSection);
    card.appendChild(body);
    return card;
}

function renderOutputsList(stream, container) {
    container.replaceChildren();

    if (!stream.outputs || stream.outputs.length === 0) {
        const empty = el('div', null, 'No outputs configured');
        empty.style.cssText = 'color: var(--text-muted); font-size: 13px; text-align: center; padding: 16px;';
        container.appendChild(empty);
        return;
    }

    for (const out of stream.outputs) {
        const item = el('div', 'output-item');
        item.dataset.id = out.id;
        item.title = out.srt_url;

        const info = el('div', 'output-info');
        info.appendChild(el('span', 'name', out.name));
        info.appendChild(el('span', 'mode-badge', out.mode));
        info.appendChild(makeStatusBadge(out.status));

        const actions = el('div', 'output-actions');
        if (out.is_active) {
            const stopBtn = el('button', 'btn-stop btn-small', 'Stop');
            stopBtn.addEventListener('click', () => stopOutput(out.id));
            actions.appendChild(stopBtn);
        } else {
            const startBtn = el('button', 'btn-start btn-small', 'Start');
            startBtn.addEventListener('click', () => startOutput(out.id));
            actions.appendChild(startBtn);
        }
        const editBtn = el('button', 'btn-icon', '✎');
        editBtn.title = 'Edit';
        editBtn.addEventListener('click', () => editOutput(out.id));
        const delBtn = el('button', 'btn-icon', '🗑');
        delBtn.title = 'Delete';
        delBtn.addEventListener('click', () => deleteOutput(out.id));
        actions.append(editBtn, delBtn);

        item.append(info, actions);
        container.appendChild(item);
    }
}

// ==================== IMPORT / EXPORT ====================
async function exportConfig() {
    const res = await apiRequest('/export');
    if (!res) return;
    if (!res.ok) {
        showToast('Failed to export configuration', 'error');
        return;
    }
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'restreamer-config.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
    showToast('Configuration exported');
}

async function importConfig(input) {
    const file = input.files[0];
    input.value = '';
    if (!file) return;

    const mode = confirm('Replace existing configuration?\nOK = replace all streams\nCancel = append to existing streams')
        ? 'replace'
        : 'append';

    const formData = new FormData();
    formData.append('file', file);

    const res = await apiRequest(`/import?mode=${mode}`, {
        method: 'POST',
        body: formData
    });
    if (!res) return;
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showToast(data.detail || 'Failed to import configuration', 'error');
        return;
    }
    const data = await res.json();
    showToast(`Imported ${data.created_inputs} inputs, ${data.created_outputs} outputs`);
    loadStreams();
}

// ==================== ACTIONS ====================
async function startInput(id) {
    const res = await apiRequest(`/inputs/${id}/start`, { method: 'POST' });
    if (res && res.ok) {
        showToast('Input stream started');
        loadStreams();
    } else {
        showToast('Failed to start stream', 'error');
    }
}

async function stopInput(id) {
    const res = await apiRequest(`/inputs/${id}/stop`, { method: 'POST' });
    if (res && res.ok) {
        showToast('Input stream stopped');
        loadStreams();
    } else {
        showToast('Failed to stop stream', 'error');
    }
}

async function deleteInput(id) {
    if (!confirm('Delete this input stream and all its outputs?')) return;
    const res = await apiRequest(`/inputs/${id}`, { method: 'DELETE' });
    if (res && res.ok) {
        showToast('Input stream deleted');
        loadStreams();
    } else {
        showToast('Failed to delete stream', 'error');
    }
}

async function uploadSlate(id, input) {
    if (!input.files.length) return;
    const formData = new FormData();
    formData.append('file', input.files[0]);
    const res = await apiRequest(`/inputs/${id}/slate`, {
        method: 'POST',
        body: formData
    });
    if (res && res.ok) {
        showToast('Slate image updated');
    } else {
        showToast('Failed to upload slate', 'error');
    }
    input.value = '';
}

async function deleteSlate(id) {
    const res = await apiRequest(`/inputs/${id}/slate`, { method: 'DELETE' });
    if (res && res.ok) {
        showToast('Slate image removed');
    } else {
        showToast('Failed to remove slate', 'error');
    }
}

async function startOutput(id) {
    const res = await apiRequest(`/outputs/${id}/start`, { method: 'POST' });
    if (res && res.ok) {
        showToast('Output stream started');
        loadStreams();
    } else {
        showToast('Failed to start output', 'error');
    }
}

async function stopOutput(id) {
    const res = await apiRequest(`/outputs/${id}/stop`, { method: 'POST' });
    if (res && res.ok) {
        showToast('Output stream stopped');
        loadStreams();
    } else {
        showToast('Failed to stop output', 'error');
    }
}

async function deleteOutput(id) {
    if (!confirm('Delete this output stream?')) return;
    const res = await apiRequest(`/outputs/${id}`, { method: 'DELETE' });
    if (res && res.ok) {
        showToast('Output deleted');
        loadStreams();
    } else {
        showToast('Failed to delete output', 'error');
    }
}

// ==================== EDIT OUTPUT ====================
function editOutput(id) {
    const stream = streamsData.find(s => s.outputs && s.outputs.some(o => o.id === id));
    if (!stream) return;
    const out = stream.outputs.find(o => o.id === id);
    if (!out) return;

    document.getElementById('editOutputId').value = out.id;
    document.getElementById('editOutputName').value = out.name;
    document.getElementById('editOutputUrl').value = out.srt_url;
    document.getElementById('editOutputPassphrase').value = '';
    showModal('editOutputModal');
}

async function updateOutputStream() {
    const id = parseInt(document.getElementById('editOutputId').value, 10);
    const name = document.getElementById('editOutputName').value.trim();
    const url = document.getElementById('editOutputUrl').value.trim();
    const passphrase = document.getElementById('editOutputPassphrase').value;

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const payload = { name, srt_url: url };
    if (passphrase) payload.passphrase = passphrase;

    const res = await apiRequest(`/outputs/${id}`, { method: 'PUT', json: payload });

    if (res && res.ok) {
        showToast('Output stream updated');
        closeModal('editOutputModal');
        loadStreams();
    } else {
        const err = await res.json().catch(() => ({}));
        showToast(err.detail || 'Failed to update output', 'error');
    }
}

// ==================== MODALS ====================
function showAddInputModal() {
    document.getElementById('inputName').value = '';
    document.getElementById('inputUrl').value = '';
    document.getElementById('inputPassphrase').value = '';
    showModal('addInputModal');
}

async function addInputStream() {
    const name = document.getElementById('inputName').value.trim();
    const url = document.getElementById('inputUrl').value.trim();
    const passphrase = document.getElementById('inputPassphrase').value;

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const payload = { name, srt_url: url };
    if (passphrase) payload.passphrase = passphrase;

    const res = await apiRequest('/inputs', { method: 'POST', json: payload });

    if (res && res.ok) {
        showToast('Input stream added');
        closeModal('addInputModal');
        loadStreams();
    } else {
        const err = res ? await res.json().catch(() => ({})) : {};
        showToast(err.detail || 'Failed to add stream', 'error');
    }
}

function editInput(id) {
    const stream = streamsData.find(s => s.id === id);
    if (!stream) return;

    document.getElementById('editInputId').value = stream.id;
    document.getElementById('editInputName').value = stream.name;
    document.getElementById('editInputUrl').value = stream.srt_url;
    document.getElementById('editInputPassphrase').value = '';
    showModal('editInputModal');
}

async function updateInputStream() {
    const id = parseInt(document.getElementById('editInputId').value, 10);
    const name = document.getElementById('editInputName').value.trim();
    const url = document.getElementById('editInputUrl').value.trim();
    const passphrase = document.getElementById('editInputPassphrase').value;

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const payload = { name, srt_url: url };
    if (passphrase) payload.passphrase = passphrase;

    const res = await apiRequest(`/inputs/${id}`, { method: 'PUT', json: payload });

    if (res && res.ok) {
        showToast('Input stream updated');
        closeModal('editInputModal');
        loadStreams();
    } else {
        const err = await res.json().catch(() => ({}));
        showToast(err.detail || 'Failed to update input', 'error');
    }
}

function showAddOutputModal(inputId) {
    document.getElementById('outputInputId').value = inputId;
    document.getElementById('outputName').value = '';
    document.getElementById('outputUrl').value = '';
    document.getElementById('outputPassphrase').value = '';
    showModal('addOutputModal');
}

async function addOutputStream() {
    const inputId = parseInt(document.getElementById('outputInputId').value, 10);
    const name = document.getElementById('outputName').value.trim();
    const url = document.getElementById('outputUrl').value.trim();
    const passphrase = document.getElementById('outputPassphrase').value;

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const payload = { input_stream_id: inputId, name, srt_url: url };
    if (passphrase) payload.passphrase = passphrase;

    const res = await apiRequest('/outputs', { method: 'POST', json: payload });

    if (res && res.ok) {
        showToast('Output stream added');
        closeModal('addOutputModal');
        loadStreams();
    } else {
        const err = res ? await res.json().catch(() => ({})) : {};
        showToast(err.detail || 'Failed to add output', 'error');
    }
}

// ==================== CHANGE PASSWORD ====================
function showChangePasswordModal() {
    document.getElementById('currentPassword').value = '';
    document.getElementById('newPassword').value = '';
    document.getElementById('confirmPassword').value = '';
    const errorEl = document.getElementById('changePasswordError');
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }
    showModal('changePasswordModal');
}

async function changePassword() {
    const current = document.getElementById('currentPassword').value;
    const newPass = document.getElementById('newPassword').value;
    const confirm = document.getElementById('confirmPassword').value;
    const errorEl = document.getElementById('changePasswordError');

    if (!current || !newPass || !confirm) {
        errorEl.textContent = 'All fields are required';
        errorEl.style.display = 'block';
        return;
    }
    if (newPass.length < 12) {
        errorEl.textContent = 'New password must be at least 12 characters';
        errorEl.style.display = 'block';
        return;
    }
    if (newPass === current) {
        errorEl.textContent = 'New password must differ from the current password';
        errorEl.style.display = 'block';
        return;
    }
    if (newPass !== confirm) {
        errorEl.textContent = 'New passwords do not match';
        errorEl.style.display = 'block';
        return;
    }

    const res = await apiRequest('/auth/change-password', {
        method: 'POST',
        json: { current_password: current, new_password: newPass }
    });

    if (res && res.ok) {
        // All sessions are revoked server-side; return to the login page.
        window.location.href = '/login';
    } else {
        const data = res ? await res.json().catch(() => ({})) : {};
        errorEl.textContent = data.detail || 'Failed to update password';
        errorEl.style.display = 'block';
    }
}

// ==================== WEBSOCKET ====================
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Session cookie is included automatically; no token in the URL.
    const wsUrl = `${protocol}//${window.location.host}/api/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        updateWsStatus('connected');
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'stats') {
                updateStats(data.data);
            }
        } catch (e) {
            console.error('WS parse error:', e);
        }
    };

    ws.onclose = () => {
        updateWsStatus('disconnected');
        setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = () => {
        updateWsStatus('error');
    };
}

function updateWsStatus(state) {
    const indicator = document.getElementById('wsStatus');
    if (!indicator) return;

    const dot = indicator.querySelector('.dot');
    const text = indicator.querySelector('.text');

    indicator.className = 'status-indicator';
    if (state === 'connected') {
        indicator.classList.add('connected');
        text.textContent = 'Live';
        dot.style.background = 'var(--success)';
    } else if (state === 'disconnected') {
        indicator.classList.add('disconnected');
        text.textContent = 'Disconnected';
        dot.style.background = 'var(--danger)';
    } else {
        text.textContent = 'Connecting...';
        dot.style.background = 'var(--warning)';
    }
}

function updateStats(statsData) {
    lastStatsData = statsData;
    statsData.forEach(item => {
        const bitrateEl = document.getElementById(`bitrate-${item.input_id}`);
        const fpsEl = document.getElementById(`fps-${item.input_id}`);
        const speedEl = document.getElementById(`speed-${item.input_id}`);

        if (bitrateEl && item.input_stats.bitrate) {
            bitrateEl.textContent = item.input_stats.bitrate;
        }
        if (fpsEl && item.input_stats.fps) {
            fpsEl.textContent = item.input_stats.fps.toFixed(1);
        }
        if (speedEl && item.input_stats.speed) {
            speedEl.textContent = item.input_stats.speed;
        }

        // Update input status badge
        const inputStatusEl = document.querySelector(`.stream-card[data-id="${item.input_id}"] .status-badge`);
        if (inputStatusEl && item.input_status) {
            inputStatusEl.replaceWith(makeStatusBadge(item.input_status));
        }

        // Update output statuses
        if (item.outputs) {
            item.outputs.forEach(out => {
                const outEl = document.querySelector(`.output-item[data-id="${out.id}"] .status-badge`);
                if (outEl) {
                    outEl.replaceWith(makeStatusBadge(out.status));
                }
            });
        }
    });
}

// ==================== INIT ====================
function wireStaticHandlers() {
    const bindings = [
        ['btnLogout', logout],
        ['btnChangePassword', showChangePasswordModal],
        ['btnExportConfig', exportConfig],
        ['btnImportConfig', () => document.getElementById('importConfigInput').click()],
        ['btnAddInput', showAddInputModal],
        ['btnAddInputSubmit', addInputStream],
        ['btnEditInputSubmit', updateInputStream],
        ['btnAddOutputSubmit', addOutputStream],
        ['btnEditOutputSubmit', updateOutputStream],
        ['btnChangePasswordSubmit', changePassword],
    ];
    for (const [id, fn] of bindings) {
        const node = document.getElementById(id);
        if (node) node.addEventListener('click', fn);
    }

    document.querySelectorAll('[data-close-modal]').forEach(node => {
        node.addEventListener('click', () => closeModal(node.dataset.closeModal));
    });

    const importInput = document.getElementById('importConfigInput');
    if (importInput) {
        importInput.addEventListener('change', () => importConfig(importInput));
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    if (!(await checkAuth())) return;

    wireStaticHandlers();
    loadStreams();
    connectWebSocket();

    // Refresh thumbnails every 5 seconds
    setInterval(() => {
        document.querySelectorAll('.thumbnail-container img').forEach(img => {
            const url = new URL(img.src);
            url.searchParams.set('t', Date.now());
            img.src = url.toString();
        });
    }, 5000);

    // Refresh streams list every 10 seconds as fallback
    setInterval(() => {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            loadStreams();
        }
    }, 10000);
});

// Close modals on backdrop click
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('show');
    }
});
