/* SRT Restreamer Dashboard Frontend */
const API_BASE = '/api';
let ws = null;
let streamsData = [];

// ==================== AUTH ====================
function getToken() {
    return localStorage.getItem('token');
}

function checkAuth() {
    const token = getToken();
    if (!token) {
        window.location.href = '/login';
        return false;
    }
    return true;
}

function logout() {
    localStorage.removeItem('token');
    window.location.href = '/login';
}

async function apiRequest(endpoint, options = {}) {
    const token = getToken();
    const defaults = {
        headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
        }
    };

    try {
        const res = await fetch(`${API_BASE}${endpoint}`, { ...defaults, ...options });
        if (res.status === 401) {
            logout();
            return null;
        }
        return res;
    } catch (err) {
        showToast('Connection error', 'error');
        return null;
    }
}

// ==================== UI HELPERS ====================
function showToast(message, type = 'success') {
    const container = document.querySelector('.toast-container') || createToastContainer();
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function createToastContainer() {
    const div = document.createElement('div');
    div.className = 'toast-container';
    document.body.appendChild(div);
    return div;
}

function showModal(id) {
    document.getElementById(id).classList.add('show');
}

function closeModal(id) {
    document.getElementById(id).classList.remove('show');
}

// ==================== STREAMS ====================
async function loadStreams() {
    const res = await apiRequest('/inputs');
    if (!res) return;

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

    if (streamsData.length === 0) {
        container.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <div class="icon">📡</div>
                <h3>No input streams configured</h3>
                <p>Add your first SRT input stream to get started</p>
            </div>
        `;
        return;
    }

    container.innerHTML = streamsData.map(stream => renderStreamCard(stream)).join('');
}

function renderStreamCard(stream) {
    const statusClass = stream.status || 'disconnected';
    const statusText = stream.status ? stream.status.toUpperCase() : 'DISCONNECTED';
    const token = getToken();
    const thumbnail = `/api/inputs/${stream.id}/thumbnail?t=${Date.now()}&token=${token}`;

    return `
        <div class="stream-card" data-id="${stream.id}">
            <div class="card-header">
                <div class="card-title">
                    <span class="status-badge ${statusClass}">
                        <span class="dot"></span>
                        ${statusText}
                    </span>
                    <span style="font-weight: 600; font-size: 15px;">${escapeHtml(stream.name)}</span>
                </div>
                <div class="card-actions">
                    ${stream.is_active ? 
                        `<button class="btn-stop" onclick="stopInput(${stream.id})">Stop</button>` :
                        `<button class="btn-start" onclick="startInput(${stream.id})">Start</button>`
                    }
                    <input type="file" id="slate-input-${stream.id}" accept="image/*" style="display:none" onchange="uploadSlate(${stream.id}, this)">
                    <button class="btn-icon" onclick="document.getElementById('slate-input-${stream.id}').click()" title="Upload slate">🖼</button>
                    <button class="btn-icon" onclick="deleteSlate(${stream.id})" title="Remove slate">🚫</button>
                    <button class="btn-icon" onclick="editInput(${stream.id})" title="Edit">✎</button>
                    <button class="btn-icon" onclick="deleteInput(${stream.id})" title="Delete">🗑</button>
                </div>
            </div>

            <div class="card-body">
                <div class="thumbnail-container">
                    <img src="${thumbnail}" alt="Stream preview" onerror="this.onerror=null; this.style.display='none'; this.parentElement.querySelector('.thumbnail-placeholder') || this.parentElement.insertAdjacentHTML('afterbegin', '<div class=\\'thumbnail-placeholder\\'>No preview available</div>')">
                    <div class="thumbnail-overlay">${escapeHtml(stream.srt_url)}</div>
                </div>

                <div class="stats-grid" id="stats-${stream.id}">
                    <div class="stat-box">
                        <div class="value" id="bitrate-${stream.id}">-</div>
                        <div class="label">Bitrate</div>
                    </div>
                    <div class="stat-box">
                        <div class="value" id="fps-${stream.id}">-</div>
                        <div class="label">FPS</div>
                    </div>
                    <div class="stat-box">
                        <div class="value" id="speed-${stream.id}">-</div>
                        <div class="label">Speed</div>
                    </div>
                </div>

                <div class="outputs-section">
                    <div class="outputs-header">
                        <h4>Output Destinations (${stream.outputs_count || 0})</h4>
                        <button class="btn-primary btn-small" onclick="showAddOutputModal(${stream.id})">
                            + Add Output
                        </button>
                    </div>
                    <div class="outputs-list" id="outputs-${stream.id}">
                        ${renderOutputsList(stream.id)}
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderOutputsList(inputId) {
    const stream = streamsData.find(s => s.id === inputId);
    if (!stream || !stream.outputs || stream.outputs.length === 0) {
        return '<div style="color: var(--text-muted); font-size: 13px; text-align: center; padding: 16px;">No outputs configured</div>';
    }

    return stream.outputs.map(out => {
        const statusClass = out.status || 'disconnected';
        return `
            <div class="output-item" data-id="${out.id}" title="${escapeHtml(out.srt_url)}">
                <div class="output-info">
                    <span class="name">${escapeHtml(out.name)}</span>
                    <span class="mode-badge">${out.mode}</span>
                    <span class="status-badge ${statusClass}" style="font-size: 10px; padding: 2px 8px;">
                        <span class="dot"></span>
                        ${(out.status || 'disconnected').toUpperCase()}
                    </span>
                </div>
                <div class="output-actions">
                    ${out.is_active ? 
                        `<button class="btn-stop btn-small" onclick="stopOutput(${out.id})">Stop</button>` :
                        `<button class="btn-start btn-small" onclick="startOutput(${out.id})">Start</button>`
                    }
                    <button class="btn-icon" onclick="editOutput(${out.id})" title="Edit">✎</button>
                    <button class="btn-icon" onclick="deleteOutput(${out.id})" title="Delete">🗑</button>
                </div>
            </div>
        `;
    }).join('');
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ==================== IMPORT / EXPORT ====================

async function exportConfig() {
    const token = getToken();
    try {
        const res = await fetch(`${API_BASE}/export`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res) return;
        if (res.status === 401) {
            logout();
            return;
        }
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
    } catch (err) {
        showToast('Connection error', 'error');
    }
}

async function importConfig(input) {
    const file = input.files[0];
    input.value = '';
    if (!file) return;

    const mode = confirm('Replace existing configuration?\nOK = replace all streams\nCancel = append to existing streams')
        ? 'replace'
        : 'append';

    const token = getToken();
    const formData = new FormData();
    formData.append('file', file);

    try {
        const res = await fetch(`${API_BASE}/import?mode=${mode}`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: formData
        });
        if (res.status === 401) {
            logout();
            return;
        }
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            showToast(data.detail || 'Failed to import configuration', 'error');
            return;
        }
        const data = await res.json();
        showToast(`Imported ${data.created_inputs} inputs, ${data.created_outputs} outputs`);
        loadStreams();
    } catch (err) {
        showToast('Connection error', 'error');
    }
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
    const token = getToken();
    const formData = new FormData();
    formData.append('file', input.files[0]);
    try {
        const res = await fetch(`${API_BASE}/inputs/${id}/slate`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: formData
        });
        if (res && res.ok) {
            showToast('Slate image updated');
        } else {
            showToast('Failed to upload slate', 'error');
        }
    } catch (err) {
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
    document.getElementById('editOutputMode').value = out.mode;
    document.getElementById('editOutputUrl').value = out.srt_url;
    showModal('editOutputModal');
}

async function updateOutputStream() {
    const id = parseInt(document.getElementById('editOutputId').value);
    const name = document.getElementById('editOutputName').value.trim();
    const mode = document.getElementById('editOutputMode').value;
    const url = document.getElementById('editOutputUrl').value.trim();

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const res = await apiRequest(`/outputs/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ name, srt_url: url, mode })
    });

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
    showModal('addInputModal');
}

async function addInputStream() {
    const name = document.getElementById('inputName').value.trim();
    const url = document.getElementById('inputUrl').value.trim();

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const res = await apiRequest('/inputs', {
        method: 'POST',
        body: JSON.stringify({ name, srt_url: url })
    });

    if (res && res.ok) {
        showToast('Input stream added');
        closeModal('addInputModal');
        loadStreams();
    } else {
        showToast('Failed to add stream', 'error');
    }
}

function editInput(id) {
    const stream = streamsData.find(s => s.id === id);
    if (!stream) return;

    document.getElementById('editInputId').value = stream.id;
    document.getElementById('editInputName').value = stream.name;
    document.getElementById('editInputUrl').value = stream.srt_url;
    showModal('editInputModal');
}

async function updateInputStream() {
    const id = parseInt(document.getElementById('editInputId').value);
    const name = document.getElementById('editInputName').value.trim();
    const url = document.getElementById('editInputUrl').value.trim();

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const res = await apiRequest(`/inputs/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ name, srt_url: url })
    });

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
    document.getElementById('outputMode').value = 'caller';
    document.getElementById('outputUrl').value = '';
    showModal('addOutputModal');
}

async function addOutputStream() {
    const inputId = parseInt(document.getElementById('outputInputId').value);
    const name = document.getElementById('outputName').value.trim();
    const mode = document.getElementById('outputMode').value;
    const url = document.getElementById('outputUrl').value.trim();

    if (!name || !url) {
        showToast('Please fill all fields', 'warning');
        return;
    }

    const res = await apiRequest('/outputs', {
        method: 'POST',
        body: JSON.stringify({ input_stream_id: inputId, name, srt_url: url, mode })
    });

    if (res && res.ok) {
        showToast('Output stream added');
        closeModal('addOutputModal');
        loadStreams();
    } else {
        showToast('Failed to add output', 'error');
    }
}

// ==================== WEBSOCKET ====================
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = getToken();
    const wsUrl = `${protocol}//${window.location.host}/api/ws?token=${token}`;

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
            inputStatusEl.className = `status-badge ${item.input_status}`;
            inputStatusEl.innerHTML = `<span class="dot"></span>${item.input_status.toUpperCase()}`;
        }

        // Update output statuses
        if (item.outputs) {
            item.outputs.forEach(out => {
                const outEl = document.querySelector(`.output-item[data-id="${out.id}"] .status-badge`);
                if (outEl) {
                    outEl.className = `status-badge ${out.status}`;
                    outEl.innerHTML = `<span class="dot"></span>${out.status.toUpperCase()}`;
                }
            });
        }
    });
}

// ==================== INIT ====================
document.addEventListener('DOMContentLoaded', () => {
    if (!checkAuth()) return;

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
