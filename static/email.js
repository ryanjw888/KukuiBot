/**
 * KukuiBot Email Drafter — frontend module.
 * Split pane: drafts/settings on top, dedicated AI chat on bottom.
 * Loaded lazily when user switches to email mode.
 */

const EmailModule = (function () {
  'use strict';

  // --- State ---
  let activeTab = 'drafts'; // 'drafts' | 'history' | 'settings' | 'profile'
  let drafts = [];
  let status = null; // null = not loaded yet
  let config = {};
  let history = [];
  let historyTotal = 0;
  let profile = '';
  let loading = false;
  let runningOp = ''; // 'run' | 'dry-run' | 'rebuild' | 'send:uid' | 'discard:uid'
  let expandedDrafts = new Set();
  let initialized = false;
  let pollTimer = null;

  // Chat state
  let chatMessages = []; // [{role, text, ts}]
  let chatStreaming = false;
  let chatStreamText = '';
  let chatModelKey = ''; // populated dynamically from available models
  let chatWorkerKey = 'assistant';
  let chatAbortController = null;
  let availableModels = []; // [{key, label}] — from MODELS
  let availableWorkers = []; // [{key, name}] — from /api/workers

  // --- Helpers ---

  function escHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function timeAgo(ts) {
    if (!ts) return 'never';
    const secs = Math.floor(Date.now() / 1000) - ts;
    if (secs < 60) return 'just now';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
    return Math.floor(secs / 86400) + 'd ago';
  }

  function fmtDate(ts) {
    if (!ts) return '--';
    return new Date(ts * 1000).toLocaleString();
  }

  function fmtTime(ts) {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  async function apiFetch(url, opts) {
    try {
      const res = await fetch(url, opts);
      return await res.json();
    } catch (e) {
      console.error('EmailModule fetch error:', url, e);
      return { error: e.message };
    }
  }

  function rerender() {
    if (typeof requestRender === 'function') requestRender({});
  }

  // Simple markdown-ish rendering for chat
  function renderMd(text) {
    if (typeof DOMPurify !== 'undefined' && typeof marked !== 'undefined') {
      try { return DOMPurify.sanitize(marked.parse(text)); } catch {}
    }
    return escHtml(text).replace(/\n/g, '<br>');
  }

  // --- Data fetching ---

  async function fetchStatus() {
    status = await apiFetch('/api/drafter/status');
  }

  async function fetchConfig() {
    config = await apiFetch('/api/drafter/config');
  }

  async function fetchDrafts() {
    const resp = await apiFetch('/api/drafter/drafts');
    drafts = resp.drafts || [];
  }

  async function fetchHistory() {
    const resp = await apiFetch('/api/drafter/history?limit=100');
    history = resp.items || [];
    historyTotal = resp.total || 0;
  }

  async function fetchProfile() {
    const resp = await apiFetch('/api/drafter/profile');
    profile = resp.text || '';
  }

  async function refreshAll() {
    loading = true;
    rerender();
    await Promise.all([fetchStatus(), fetchDrafts()]);
    loading = false;
    rerender();
  }

  // --- Build model/worker lists from the main app's MODELS ---

  function buildModelList() {
    availableModels = [];
    if (typeof MODELS === 'undefined') return;
    // Claude Code models first (they're the primary backend), then others
    const order = ['claude', 'anthropic', 'openrouter', 'spark', 'codex'];
    const groupOf = (key, def) => {
      if (key.startsWith('claude_')) return 'claude';
      const m = (def.model || '').toLowerCase();
      if (m.includes('anthropic') || key.startsWith('anthropic_')) return 'anthropic';
      if (m.includes('openrouter') || key.startsWith('openrouter_')) return 'openrouter';
      if (key === 'spark') return 'spark';
      return 'codex';
    };
    const sorted = Object.entries(MODELS).sort((a, b) => {
      const ai = order.indexOf(groupOf(a[0], a[1]));
      const bi = order.indexOf(groupOf(b[0], b[1]));
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
    for (const [key, def] of sorted) {
      availableModels.push({ key, label: def.shortName || def.name || key });
    }
    // Pick default model: prefer claude_sonnet (fast), then claude_opus, then first available
    if (!chatModelKey || !availableModels.find(m => m.key === chatModelKey)) {
      const sonnet = availableModels.find(m => m.key === 'claude_sonnet');
      const opus = availableModels.find(m => m.key === 'claude_opus');
      chatModelKey = sonnet ? sonnet.key : (opus ? opus.key : (availableModels[0]?.key || 'claude_sonnet'));
    }
  }

  async function loadWorkers() {
    try {
      const res = await fetch('/api/workers');
      const data = await res.json();
      availableWorkers = data.workers || [];
    } catch {}
  }

  // --- Drafter Actions ---

  async function runDrafter(dryRun) {
    runningOp = dryRun ? 'dry-run' : 'run';
    rerender();
    const url = dryRun ? '/api/drafter/dry-run' : '/api/drafter/run';
    const resp = await apiFetch(url, { method: 'POST' });
    runningOp = '';
    if (resp.error) {
      alert('Drafter error: ' + resp.error);
    } else {
      const msg = `Drafted: ${resp.drafted || 0}, Skipped: ${resp.skipped || 0}, Errors: ${resp.errors || 0}`;
      if (dryRun) {
        alert('Dry Run Results\n\n' + msg);
      }
    }
    await refreshAll();
  }

  async function sendDraft(uid) {
    if (!confirm('Send this draft email?')) return;
    runningOp = 'send:' + uid;
    rerender();
    const resp = await apiFetch(`/api/drafter/drafts/${uid}/send`, { method: 'POST' });
    runningOp = '';
    if (resp.error) {
      alert('Send error: ' + resp.error);
    }
    await refreshAll();
  }

  async function discardDraft(uid) {
    if (!confirm('Discard this draft? It will be permanently deleted.')) return;
    runningOp = 'discard:' + uid;
    rerender();
    const resp = await apiFetch(`/api/drafter/drafts/${uid}/discard`, { method: 'POST' });
    runningOp = '';
    if (resp.error) {
      alert('Discard error: ' + resp.error);
    }
    expandedDrafts.delete(uid);
    await refreshAll();
  }

  async function rebuildProfile() {
    runningOp = 'rebuild';
    rerender();
    const resp = await apiFetch('/api/drafter/profile/rebuild', { method: 'POST' });
    runningOp = '';
    if (resp.error) {
      alert('Rebuild error: ' + resp.error);
    } else {
      await fetchProfile();
      await fetchStatus();
    }
    rerender();
  }

  async function saveConfig(updates) {
    const resp = await apiFetch('/api/drafter/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    if (resp.config) config = resp.config;
    if (resp.error) alert('Save error: ' + resp.error);
    await fetchStatus();
    rerender();
  }

  function toggleDraftExpand(uid) {
    if (expandedDrafts.has(uid)) expandedDrafts.delete(uid);
    else expandedDrafts.add(uid);
    rerender();
  }

  function saveExclusions(type, value) {
    const current = config.exclusions || { senders: [], subjects: [] };
    const items = value.split(',').map(s => s.trim()).filter(Boolean);
    current[type] = items;
    saveConfig({ exclusions: current });
  }

  // --- Tab switch ---

  async function setTab(tab) {
    activeTab = tab;
    rerender();
    if (tab === 'history' && history.length === 0) {
      await fetchHistory();
      rerender();
    }
    if (tab === 'profile') {
      await fetchProfile();
      rerender();
    }
    if (tab === 'settings' && !config.enabled && config.enabled !== false) {
      await fetchConfig();
      rerender();
    }
  }

  // --- Chat ---

  function _resolveSessionId() {
    // Session ID prefix determines backend provider routing in server_helpers.py:
    //   tab-claude_*  → Claude Code subprocess (is_claude_session)
    //   tab-anthropic_* → Anthropic API (is_anthropic_session)
    //   tab-openrouter_* → OpenRouter (is_openrouter_session)
    //   anything else → Codex fallback
    // Use a stable suffix so the session persists across page loads.
    const key = chatModelKey || 'claude_sonnet';
    return 'tab-' + key + '-emailchat';
  }

  async function chatSend() {
    const textarea = document.getElementById('email-chat-input');
    if (!textarea) return;
    const text = (textarea.value || '').trim();
    if (!text || chatStreaming) return;

    chatMessages.push({ role: 'user', text, ts: Date.now() });
    textarea.value = '';
    textarea.style.height = 'auto';
    chatStreaming = true;
    chatStreamText = '';
    rerender();
    _scrollChatToBottom();

    chatAbortController = new AbortController();
    const sid = _resolveSessionId();

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, session_id: sid }),
        signal: chatAbortController.signal,
      });

      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.error || 'Request failed: ' + res.status);
      }

      // Consume SSE stream
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        buf = buf.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        const frames = buf.split('\n\n');
        buf = frames.pop() || '';
        for (const frame of frames) {
          const dataLines = [];
          for (const line of frame.split('\n')) {
            if (line.startsWith('data: ')) dataLines.push(line.slice(6));
            else if (line.startsWith('data:')) dataLines.push(line.slice(5));
          }
          if (!dataLines.length) continue;
          try {
            const evt = JSON.parse(dataLines.join('\n'));
            if (evt.type === 'text' || evt.type === 'chunk') {
              chatStreamText += evt.text || '';
              _patchStreamBubble();
            }
            if (evt.type === 'done') break;
          } catch {}
        }
      }

      // Finalize
      if (chatStreamText) {
        chatMessages.push({ role: 'assistant', text: chatStreamText, ts: Date.now() });
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        const errText = err.message || String(err);
        chatMessages.push({ role: 'assistant', text: '\u26a0\ufe0f ' + errText, ts: Date.now() });
      }
    } finally {
      chatStreaming = false;
      chatStreamText = '';
      chatAbortController = null;
      rerender();
      _scrollChatToBottom();
    }
  }

  function chatKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      chatSend();
    }
  }

  function chatAutoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  }

  function _scrollChatToBottom() {
    requestAnimationFrame(() => {
      const el = document.getElementById('email-chat-messages');
      if (el) el.scrollTop = el.scrollHeight;
    });
  }

  function _patchStreamBubble() {
    const el = document.getElementById('email-chat-stream-bubble');
    if (el) {
      el.innerHTML = renderMd(chatStreamText) + '<span class="email-chat-streaming"></span>';
      _scrollChatToBottom();
    } else {
      rerender();
      _scrollChatToBottom();
    }
  }

  function chatSetModel(key) {
    chatModelKey = key;
    try { localStorage.setItem('email_chat_model', key); } catch {}
    _registerChatSession();
    rerender();
  }

  function chatSetWorker(key) {
    chatWorkerKey = key;
    try { localStorage.setItem('email_chat_worker', key); } catch {}
    _registerChatSession();
    rerender();
  }

  function _registerChatSession() {
    const sid = _resolveSessionId();
    // Register in tab_meta so worker_identity resolves correctly for this session
    fetch('/api/drafter/chat/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid, worker_identity: chatWorkerKey, model_key: chatModelKey }),
    }).catch(() => {});
    // Also push to /api/tabs/sync so Claude Code pool picks it up
    fetch('/api/tabs/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tabs: [{
          id: 'emailchat',
          session_id: sid,
          label: 'Email Chat',
          model_key: chatModelKey,
          worker_identity: chatWorkerKey,
          created_explicitly: true,
          sort_order: 999,
        }],
      }),
    }).catch(() => {});
  }

  function chatClear() {
    chatMessages = [];
    rerender();
  }

  // --- Lifecycle ---

  async function init() {
    if (initialized) return;
    initialized = true;
    activeTab = 'drafts';
    expandedDrafts.clear();
    chatMessages = [];

    // Restore saved model/worker prefs
    try {
      const savedModel = localStorage.getItem('email_chat_model');
      if (savedModel) chatModelKey = savedModel;
      const savedWorker = localStorage.getItem('email_chat_worker');
      if (savedWorker) chatWorkerKey = savedWorker;
    } catch {}

    // Load dynamic models if available
    if (typeof _loadAnthropicModels === 'function') await _loadAnthropicModels();
    if (typeof _loadOpenRouterModels === 'function') await _loadOpenRouterModels();
    buildModelList();
    await loadWorkers();
    _registerChatSession();

    await Promise.all([fetchStatus(), fetchConfig(), fetchDrafts()]);
    rerender();

    // Poll for new drafts every 30s while in email mode
    pollTimer = setInterval(async () => {
      if (typeof appMode !== 'undefined' && appMode === 'email') {
        await Promise.all([fetchStatus(), fetchDrafts()]);
        if (activeTab === 'drafts') rerender();
      }
    }, 30000);
  }

  function destroy() {
    initialized = false;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (chatAbortController) { try { chatAbortController.abort(); } catch {} }
  }

  // --- Render: Status Banner ---

  function renderStatusBanner() {
    if (status === null) {
      return `<div class="email-status-banner">
        <span class="email-spinner"></span>
        <span class="email-status-text">Loading status...</span>
      </div>`;
    }

    const connected = status.gmail_connected;
    const hasKey = status.has_api_key;
    const hasPerm = status.has_auto_draft_perm;
    const enabled = status.enabled;

    if (status.error) {
      return `<div class="email-status-banner">
        <span class="email-status-dot yellow"></span>
        <span class="email-status-text">Status unavailable</span>
        <span class="email-status-sub">${escHtml(status.error)}</span>
      </div>`;
    }

    if (!connected) {
      return `<div class="email-status-banner">
        <span class="email-status-dot red"></span>
        <span class="email-status-text">Gmail not connected</span>
        <span class="email-status-sub">Configure in <a href="/settings-v2.html#gmail" style="color:var(--accent)">Settings</a></span>
      </div>`;
    }
    if (!hasKey) {
      return `<div class="email-status-banner">
        <span class="email-status-dot red"></span>
        <span class="email-status-text">No AI backend available</span>
        <span class="email-status-sub">Claude Code or an API key is required. Check <a href="/settings-v2.html#anthropic" style="color:var(--accent)">Settings</a></span>
      </div>`;
    }
    if (!hasPerm) {
      return `<div class="email-status-banner">
        <span class="email-status-dot yellow"></span>
        <span class="email-status-text">Auto-draft permission not enabled</span>
        <span class="email-status-sub">Enable in <a href="/settings-v2.html#gmail" style="color:var(--accent)">Settings &gt; Gmail</a></span>
      </div>`;
    }
    if (!enabled) {
      return `<div class="email-status-banner">
        <span class="email-status-dot muted"></span>
        <span class="email-status-text">Drafter disabled</span>
        <span class="email-status-sub">Enable it in the Settings tab</span>
      </div>`;
    }

    const lastRun = status.last_run_at ? timeAgo(status.last_run_at) : 'never';
    return `<div class="email-status-banner">
      <span class="email-status-dot green"></span>
      <span class="email-status-text">Active &middot; Last run: ${escHtml(lastRun)}</span>
      <span class="email-status-sub">${status.last_drafts_count || 0} drafted &middot; Profile ${status.profile_fresh ? 'fresh' : 'stale'}</span>
    </div>`;
  }

  // --- Render: Drafts Tab ---

  function renderDraftsTab() {
    let html = renderStatusBanner();

    if (loading) {
      html += '<div class="email-empty"><span class="email-spinner"></span><div>Loading drafts...</div></div>';
      return html;
    }

    if (drafts.length === 0) {
      html += `<div class="email-empty">
        <div class="email-empty-icon">&#9993;</div>
        <div class="email-empty-title">No auto-drafted emails</div>
        <div class="email-empty-desc">
          ${status && status.enabled
            ? 'The drafter will create draft replies when new emails arrive. Click "Run Now" to check immediately.'
            : 'Enable the drafter in the Settings tab, then click "Run Now" to generate draft replies.'}
        </div>
      </div>`;
      return html;
    }

    html += '<div class="email-draft-list">';
    for (const d of drafts) {
      const expanded = expandedDrafts.has(d.uid);
      const isSending = runningOp === 'send:' + d.uid;
      const isDiscarding = runningOp === 'discard:' + d.uid;
      const busy = isSending || isDiscarding;

      html += `<div class="email-draft-card">
        <div class="email-draft-header">
          <div class="email-draft-meta">
            <div class="email-draft-subject">${escHtml(d.subject)}</div>
            <div class="email-draft-from">To: ${escHtml(d.to)}</div>
          </div>
          <div class="email-draft-actions">
            <button class="email-action-btn primary" onclick="EmailModule.sendDraft('${escHtml(d.uid)}')" ${busy ? 'disabled' : ''}>
              ${isSending ? '<span class="email-spinner"></span>' : 'Send'}
            </button>
            <button class="email-action-btn danger" onclick="EmailModule.discardDraft('${escHtml(d.uid)}')" ${busy ? 'disabled' : ''}>
              ${isDiscarding ? '<span class="email-spinner"></span>' : 'Discard'}
            </button>
          </div>
        </div>
        <button class="email-draft-toggle" onclick="EmailModule.toggleDraftExpand('${escHtml(d.uid)}')">${expanded ? 'Hide preview' : 'Show preview'}</button>
        ${expanded ? `<div class="email-draft-body">${escHtml(d.body || '(empty)')}</div>` : ''}
      </div>`;
    }
    html += '</div>';
    return html;
  }

  // --- Render: History Tab ---

  function renderHistoryTab() {
    if (history.length === 0) {
      return `<div class="email-empty">
        <div class="email-empty-icon">&#128203;</div>
        <div class="email-empty-title">No history yet</div>
        <div class="email-empty-desc">Run the drafter to start building history.</div>
      </div>`;
    }

    let html = `<div style="font-size:12px;color:var(--muted);margin-bottom:10px">${historyTotal} total entries</div>`;
    html += '<div class="email-history-list">';
    for (const h of history) {
      const action = (h.action || '').replace('skipped_auto', 'skipped').replace('skipped_excluded', 'skipped').replace('skipped_empty', 'skipped');
      const cls = action.startsWith('skip') ? 'skipped' : action;
      html += `<div class="email-history-row">
        <span class="email-history-action ${escHtml(cls)}">${escHtml(action)}</span>
        <span class="email-history-subject" title="${escHtml(h.subject)}">${escHtml(h.subject)}</span>
        <span class="email-history-from" title="${escHtml(h.from_addr)}">${escHtml(h.from_addr)}</span>
        <span class="email-history-time">${fmtDate(h.created_at)}</span>
      </div>`;
    }
    html += '</div>';
    return html;
  }

  // --- Render: Settings Tab ---

  function renderSettingsTab() {
    const enabled = config.enabled || false;
    const interval = config.check_interval_min || 15;
    const maxPerRun = config.max_per_run || 10;
    const sig = config.signature_html || '';
    const excl = config.exclusions || { senders: [], subjects: [] };

    // Build model/worker options for the chat section
    const modelOpts = availableModels.map(m =>
      `<option value="${escHtml(m.key)}" ${m.key === chatModelKey ? 'selected' : ''}>${escHtml(m.label)}</option>`
    ).join('');
    const workerOpts = availableWorkers.map(w =>
      `<option value="${escHtml(w.key)}" ${w.key === chatWorkerKey ? 'selected' : ''}>${escHtml(w.name)}</option>`
    ).join('');

    return `<div class="email-settings">
      <div class="email-settings-group">
        <h3>General</h3>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Enable Auto-Drafter</div>
            <div class="email-setting-desc">Automatically draft replies to new emails</div>
          </div>
          <label class="email-toggle">
            <input type="checkbox" ${enabled ? 'checked' : ''} onchange="EmailModule.saveConfig({enabled: this.checked})">
            <span class="email-toggle-slider"></span>
          </label>
        </div>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Check Interval</div>
            <div class="email-setting-desc">Minutes between automatic checks (5-60)</div>
          </div>
          <input type="number" class="email-setting-input" value="${interval}" min="5" max="60"
            onchange="EmailModule.saveConfig({check_interval_min: parseInt(this.value)})" />
        </div>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Max Drafts per Run</div>
            <div class="email-setting-desc">Limit drafts created per check (1-50)</div>
          </div>
          <input type="number" class="email-setting-input" value="${maxPerRun}" min="1" max="50"
            onchange="EmailModule.saveConfig({max_per_run: parseInt(this.value)})" />
        </div>
      </div>

      <div class="email-settings-group">
        <h3>Email Chat Assistant</h3>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Model</div>
            <div class="email-setting-desc">AI model for the chat pane</div>
          </div>
          <select class="email-model-select" style="width:auto;min-width:140px" onchange="EmailModule.chatSetModel(this.value)">
            ${modelOpts}
          </select>
        </div>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Worker Profile</div>
            <div class="email-setting-desc">System prompt / personality</div>
          </div>
          <select class="email-model-select" style="width:auto;min-width:140px" onchange="EmailModule.chatSetWorker(this.value)">
            ${workerOpts}
          </select>
        </div>
      </div>

      <div class="email-settings-group">
        <h3>Exclusions</h3>
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">
          Comma-separated patterns. Use * as wildcard.
        </div>
        <div class="email-setting-row" style="flex-direction:column;align-items:stretch;gap:6px">
          <div class="email-setting-label">Excluded Senders</div>
          <input type="text" class="email-setting-input" style="width:100%;text-align:left"
            value="${escHtml(excl.senders.join(', '))}"
            placeholder="e.g. *@noreply.github.com, alerts@*"
            onchange="EmailModule.saveExclusions('senders', this.value)" />
        </div>
        <div class="email-setting-row" style="flex-direction:column;align-items:stretch;gap:6px;margin-top:8px">
          <div class="email-setting-label">Excluded Subjects</div>
          <input type="text" class="email-setting-input" style="width:100%;text-align:left"
            value="${escHtml(excl.subjects.join(', '))}"
            placeholder="e.g. *newsletter*, *unsubscribe*"
            onchange="EmailModule.saveExclusions('subjects', this.value)" />
        </div>
      </div>

      <div class="email-settings-group">
        <h3>Signature</h3>
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">
          HTML signature appended to drafted emails. Leave empty for none.
        </div>
        <textarea style="width:100%;min-height:80px;padding:8px;border-radius:6px;border:1px solid var(--border-light);background:var(--bg);color:var(--text);font-size:12px;font-family:monospace;resize:vertical"
          onchange="EmailModule.saveConfig({signature_html: this.value})">${escHtml(sig)}</textarea>
      </div>
    </div>`;
  }

  // --- Render: Profile Tab ---

  function renderProfileTab() {
    if (runningOp === 'rebuild') {
      return `<div class="email-empty">
        <span class="email-spinner"></span>
        <div>Rebuilding style profile from sent emails...</div>
        <div class="email-empty-desc">This analyzes your last 1,000 sent emails. May take a minute.</div>
      </div>`;
    }

    const age = status ? status.profile_age_days : null;
    const fresh = status ? status.profile_fresh : false;

    let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <span style="font-size:14px;font-weight:600;color:var(--text)">Writing Style Profile</span>
      ${age !== null && age !== undefined ? `<span style="font-size:12px;color:${fresh ? 'var(--green)' : 'var(--yellow)'}">
        ${fresh ? 'Fresh' : 'Stale'} (${age}d old)
      </span>` : '<span style="font-size:12px;color:var(--muted)">Not built yet</span>'}
      <button class="email-action-btn" onclick="EmailModule.rebuildProfile()" style="margin-left:auto">
        ${profile ? 'Rebuild' : 'Build Now'}
      </button>
    </div>`;

    if (profile) {
      html += `<div class="email-profile-view">${escHtml(profile)}</div>`;
    } else {
      html += `<div class="email-empty">
        <div class="email-empty-icon">&#128221;</div>
        <div class="email-empty-title">No style profile yet</div>
        <div class="email-empty-desc">
          Click "Build Now" to analyze your sent emails and create a writing style profile.
          This is required before the drafter can generate replies.
        </div>
      </div>`;
    }

    return html;
  }

  // --- Render: Chat pane ---

  function renderChatPane() {
    const currentModel = availableModels.find(m => m.key === chatModelKey);
    const modelLabel = currentModel ? currentModel.label : chatModelKey;
    const currentWorker = availableWorkers.find(w => w.key === chatWorkerKey);
    const workerLabel = currentWorker ? currentWorker.name : (chatWorkerKey || 'Assistant');

    let msgsHtml = '';
    for (const m of chatMessages) {
      const cls = m.role === 'user' ? 'user' : 'assistant';
      const content = m.role === 'user' ? escHtml(m.text) : renderMd(m.text);
      msgsHtml += `<div class="email-chat-msg ${cls}">
        <div class="email-chat-bubble">${content}</div>
        <div class="email-chat-time">${fmtTime(m.ts)}</div>
      </div>`;
    }

    // Streaming bubble
    if (chatStreaming) {
      msgsHtml += `<div class="email-chat-msg assistant">
        <div class="email-chat-bubble" id="email-chat-stream-bubble">${chatStreamText ? renderMd(chatStreamText) : ''}<span class="email-chat-streaming"></span></div>
      </div>`;
    }

    if (!chatMessages.length && !chatStreaming) {
      msgsHtml = `<div style="text-align:center;padding:20px 10px;color:var(--muted);font-size:13px">
        Ask about emails, get help drafting replies, or discuss anything.
      </div>`;
    }

    // Model picker inline in header
    const modelOpts = availableModels.map(m =>
      `<option value="${escHtml(m.key)}" ${m.key === chatModelKey ? 'selected' : ''}>${escHtml(m.label)}</option>`
    ).join('');

    return `<div class="email-chat-header">
      <div class="email-chat-header-left">
        <span class="email-chat-header-title">&#128172; ${escHtml(workerLabel)}</span>
        <select class="email-model-select" onchange="EmailModule.chatSetModel(this.value)" title="Chat model">${modelOpts}</select>
      </div>
      <button class="email-action-btn" onclick="EmailModule.chatClear()" style="font-size:11px;padding:2px 8px" title="Clear chat">Clear</button>
    </div>
    <div class="email-chat-messages" id="email-chat-messages">${msgsHtml}</div>
    <div class="email-chat-input-area">
      <textarea class="email-chat-textarea" id="email-chat-input" rows="1"
        placeholder="Message ${escHtml(workerLabel)}..."
        onkeydown="EmailModule.chatKeyDown(event)"
        oninput="EmailModule.chatAutoResize(this)"
      ></textarea>
      <button class="email-chat-send" onclick="EmailModule.chatSend()" ${chatStreaming ? 'disabled' : ''}>
        ${chatStreaming ? '<span class="email-spinner"></span>' : 'Send'}
      </button>
    </div>`;
  }

  // --- Main Render ---

  function renderPanel() {
    const isRunning = runningOp === 'run' || runningOp === 'dry-run';
    const canRun = status && status.gmail_connected && status.has_api_key && status.has_auto_draft_perm;

    return `<div class="email-toolbar">
      <div class="email-toolbar-left">
        <button class="email-tab${activeTab === 'drafts' ? ' active' : ''}" onclick="EmailModule.setTab('drafts')">Drafts${drafts.length ? ' (' + drafts.length + ')' : ''}</button>
        <button class="email-tab${activeTab === 'history' ? ' active' : ''}" onclick="EmailModule.setTab('history')">History</button>
        <button class="email-tab${activeTab === 'settings' ? ' active' : ''}" onclick="EmailModule.setTab('settings')">Settings</button>
        <button class="email-tab${activeTab === 'profile' ? ' active' : ''}" onclick="EmailModule.setTab('profile')">Profile</button>
      </div>
      <div class="email-toolbar-right">
        <button class="email-action-btn" onclick="EmailModule.runDrafter(true)" ${!canRun || isRunning ? 'disabled' : ''} title="Preview what would be drafted">
          ${runningOp === 'dry-run' ? '<span class="email-spinner"></span> Checking...' : 'Dry Run'}
        </button>
        <button class="email-action-btn primary" onclick="EmailModule.runDrafter(false)" ${!canRun || isRunning ? 'disabled' : ''} title="Check inbox and create drafts">
          ${runningOp === 'run' ? '<span class="email-spinner"></span> Running...' : 'Run Now'}
        </button>
      </div>
    </div>
    <div class="email-split">
      <div class="email-split-top email-content">
        ${activeTab === 'drafts' ? renderDraftsTab() : ''}
        ${activeTab === 'history' ? renderHistoryTab() : ''}
        ${activeTab === 'settings' ? renderSettingsTab() : ''}
        ${activeTab === 'profile' ? renderProfileTab() : ''}
      </div>
      <div class="email-split-bottom">
        ${renderChatPane()}
      </div>
    </div>`;
  }

  function renderSidebar() {
    const connected = status ? status.gmail_connected : false;
    const enabled = status ? status.enabled : false;
    const lastRun = status && status.last_run_at ? timeAgo(status.last_run_at) : 'never';
    const profileAge = status ? status.profile_age_days : null;

    return `<div class="email-sidebar-status">
      <div class="email-sidebar-stat">
        <span class="email-sidebar-stat-label">Gmail</span>
        <span class="email-sidebar-stat-val" style="color:${connected ? 'var(--green)' : 'var(--red)'}">${connected ? 'Connected' : status === null ? '...' : 'Disconnected'}</span>
      </div>
      <div class="email-sidebar-stat">
        <span class="email-sidebar-stat-label">Drafter</span>
        <span class="email-sidebar-stat-val" style="color:${enabled ? 'var(--green)' : 'var(--muted)'}">${enabled ? 'Enabled' : 'Disabled'}</span>
      </div>
      <div class="email-sidebar-stat">
        <span class="email-sidebar-stat-label">Last Run</span>
        <span class="email-sidebar-stat-val">${escHtml(lastRun)}</span>
      </div>
      <div class="email-sidebar-stat">
        <span class="email-sidebar-stat-label">Drafts</span>
        <span class="email-sidebar-stat-val">${drafts.length}</span>
      </div>
      <div class="email-sidebar-stat">
        <span class="email-sidebar-stat-label">Profile</span>
        <span class="email-sidebar-stat-val">${profileAge !== null && profileAge !== undefined ? profileAge + 'd old' : 'None'}</span>
      </div>
    </div>
    <div class="email-sidebar-actions">
      <button class="email-sidebar-btn" onclick="EmailModule.runDrafter(false)" ${runningOp ? 'disabled' : ''}>
        ${runningOp === 'run' ? '<span class="email-spinner"></span>' : '&#9993;'} Run Drafter
      </button>
      <button class="email-sidebar-btn" onclick="EmailModule.setTab('settings')">
        &#9881; Settings
      </button>
    </div>`;
  }

  // --- Public API ---

  return {
    init,
    destroy,
    renderPanel,
    renderSidebar,
    setTab,
    runDrafter,
    sendDraft,
    discardDraft,
    rebuildProfile,
    saveConfig,
    saveExclusions,
    toggleDraftExpand,
    refreshAll,
    chatSend,
    chatKeyDown,
    chatAutoResize,
    chatSetModel,
    chatSetWorker,
    chatClear,
  };
})();
