/**
 * KukuiBot Email Drafter — frontend module.
 * Split pane: drafts/settings on top, dedicated AI chat on bottom.
 * Loaded lazily when user switches to email mode.
 */

const EmailModule = (function () {
  'use strict';

  // --- State ---
  let activeTab = 'inbox'; // 'inbox' | 'drafts' | 'history' | 'settings' | 'profile' | 'spam'
  let drafts = [];
  let status = null; // null = not loaded yet
  let config = {};
  let history = [];
  let historyTotal = 0;
  let profile = '';
  let profileOriginal = '';
  let profileDirty = false;
  let profileAce = null;
  let loading = false;
  let runningOp = ''; // 'run' | 'dry-run' | 'rebuild' | 'send:uid' | 'discard:uid'
  let expandedDrafts = new Set();
  let initialized = false;
  let pollTimer = null;

  // Inbox state
  let inboxMessages = [];       // [{from, to, subject, date, uid, folder, is_read, snippet}]
  let inboxFolder = 'INBOX';    // current folder
  let inboxSearch = '';          // IMAP search query
  let inboxLoading = false;
  let selectedMessage = null;   // full message object from /api/gmail/message
  let selectedUid = null;       // uid of currently selected message
  let messageLoading = false;
  let inboxFolders = [];        // available IMAP folders
  let showCompose = false;      // compose form visible
  let composeData = { to: '', subject: '', body: '' }; // compose form state
  let composeSending = false;
  let aiReplyLoading = false;
  let syncStatus = null;      // {total_cached, last_sync_ts, last_sync_ago_str, folders}
  let syncTimer = null;

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

  async function apiFetch(url, opts, timeoutMs) {
    try {
      if (timeoutMs) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), timeoutMs);
        opts = { ...opts, signal: ctrl.signal };
        const res = await fetch(url, opts);
        clearTimeout(timer);
        return await res.json();
      }
      const res = await fetch(url, opts);
      return await res.json();
    } catch (e) {
      console.error('EmailModule fetch error:', url, e);
      if (e.name === 'AbortError') return { error: 'Request timed out — try again or check server logs' };
      return { error: e.message };
    }
  }

  function rerender() {
    // Skip full re-renders while the profile Ace editor is active —
    // requestRender does root.innerHTML=... which would destroy the editor.
    if (profileAce && activeTab === 'profile') return;
    // Signal that this is an intentional email module render (not a background SSE event).
    // Without this flag, requestRender skips renders in email mode to prevent DOM destruction.
    if (typeof _emailRenderRequested !== 'undefined') _emailRenderRequested = true;
    if (typeof requestRender === 'function') requestRender({});
  }

  // Simple markdown-ish rendering for chat
  function renderMd(text) {
    if (typeof DOMPurify !== 'undefined' && typeof marked !== 'undefined') {
      try { return DOMPurify.sanitize(marked.parse(text)); } catch {}
    }
    return escHtml(text).replace(/\n/g, '<br>');
  }

  // --- Inbox helpers ---

  function parseSender(from) {
    if (!from) return 'Unknown';
    const match = from.match(/^"?([^"<]+?)"?\s*</);
    return match ? match[1].trim() : from.split('@')[0];
  }

  function fmtInboxDate(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    if (isNaN(d)) return dateStr;
    const now = new Date();
    const diffMs = now - d;
    const diffDays = Math.floor(diffMs / 86400000);
    if (diffDays === 0) return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return d.toLocaleDateString([], {weekday: 'short'});
    return d.toLocaleDateString([], {month: 'short', day: 'numeric'});
  }

  // --- Inbox data fetching ---

  async function fetchInbox(skipCache) {
    inboxLoading = true;
    rerender();
    const payload = { folder: inboxFolder, max_results: 50, search: inboxSearch };
    if (skipCache) payload.cache = false;
    const resp = await apiFetch('/api/gmail/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp.ok && resp.messages) {
      inboxMessages = resp.messages;
    } else if (resp.error) {
      console.error('fetchInbox error:', resp.error);
    }
    inboxLoading = false;
    rerender();
  }

  async function fetchMessageDetail(folder, uid) {
    messageLoading = true;
    selectedUid = uid;
    rerender();
    const resp = await apiFetch('/api/gmail/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uid: String(uid) }),
    });
    if (resp.ok && resp.message) {
      selectedMessage = resp.message;
      // Mark as read if not already
      const msg = inboxMessages.find(m => String(m.uid) === String(uid));
      if (msg && !msg.is_read) {
        msg.is_read = true;
        apiFetch('/api/gmail/flags', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder, uid: String(uid), flags: { seen: true } }),
        });
      }
    } else if (resp.error) {
      console.error('fetchMessageDetail error:', resp.error);
    }
    messageLoading = false;
    rerender();
  }

  async function fetchFolders() {
    const resp = await apiFetch('/api/gmail/folders');
    if (resp.ok && resp.folders) {
      inboxFolders = resp.folders;
    }
  }

  async function trashMessage(folder, uid) {
    if (!confirm('Move this message to trash?')) return;
    const resp = await apiFetch('/api/gmail/trash', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uid: String(uid) }),
    });
    if (resp.ok) {
      inboxMessages = inboxMessages.filter(m => String(m.uid) !== String(uid));
      if (String(selectedUid) === String(uid)) {
        selectedMessage = null;
        selectedUid = null;
      }
      rerender();
    } else if (resp.error) {
      alert('Trash error: ' + resp.error);
    }
  }

  async function toggleReadStatus(folder, uid) {
    const msg = inboxMessages.find(m => String(m.uid) === String(uid));
    if (!msg) return;
    const newSeen = !msg.is_read;
    const resp = await apiFetch('/api/gmail/flags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uid: String(uid), flags: { seen: newSeen } }),
    });
    if (resp.ok) {
      msg.is_read = newSeen;
      rerender();
    }
  }

  async function sendCompose() {
    if (!composeData.to || !composeData.subject) {
      alert('To and Subject are required.');
      return;
    }
    composeSending = true;
    rerender();
    const resp = await apiFetch('/api/gmail/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(composeData),
    });
    composeSending = false;
    if (resp.error) {
      alert('Send error: ' + resp.error);
      rerender();
    } else {
      composeData = { to: '', subject: '', body: '' };
      showCompose = false;
      rerender();
      // Brief success feedback
      const el = document.querySelector('.inbox-compose-success');
      if (el) { el.style.display = 'block'; setTimeout(() => el.style.display = 'none', 2000); }
    }
  }

  async function saveDraftCompose() {
    if (!composeData.to && !composeData.subject && !composeData.body) return;
    const resp = await apiFetch('/api/gmail/draft', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(composeData),
    });
    if (resp.error) {
      alert('Draft error: ' + resp.error);
    } else {
      composeData = { to: '', subject: '', body: '' };
      showCompose = false;
      rerender();
    }
  }

  async function fetchSyncStatus() {
    const resp = await apiFetch('/api/gmail/sync-status');
    if (resp.ok) {
      syncStatus = resp;
    }
  }

  function renderAttachments(attachments) {
    if (!attachments || !attachments.length) return '';
    const chips = attachments.map(a => {
      const sizeKb = a.size ? Math.round(a.size / 1024) : 0;
      const sizeStr = sizeKb > 1024 ? (sizeKb / 1024).toFixed(1) + ' MB' : sizeKb + ' KB';
      const folder = selectedMessage ? encodeURIComponent(selectedMessage.folder || inboxFolder) : encodeURIComponent(inboxFolder);
      const uid = selectedMessage ? encodeURIComponent(selectedMessage.uid || selectedUid) : encodeURIComponent(selectedUid);
      const fname = encodeURIComponent(a.filename);
      const url = '/api/gmail/attachment?folder=' + folder + '&uid=' + uid + '&filename=' + fname;
      return '<a class="inbox-attachment-chip" href="' + escHtml(url) + '" download title="' + escHtml(a.filename) + '">'
        + '<span class="inbox-attachment-icon">&#128206;</span>'
        + '<span class="inbox-attachment-name">' + escHtml(a.filename) + '</span>'
        + '<span class="inbox-attachment-size">' + escHtml(sizeStr) + '</span>'
        + '</a>';
    }).join('');
    return '<div class="inbox-attachments">' + chips + '</div>';
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
    profileOriginal = profile;
    profileDirty = false;
  }

  function initProfileEditor() {
    if (profileAce) return;
    if (typeof ace === 'undefined') return;
    const container = document.getElementById('profile-ace-editor');
    if (!container) return;

    profileAce = ace.edit(container);
    profileAce.setShowPrintMargin(false);
    profileAce.setFontSize(13);
    profileAce.session.setUseWrapMode(true);
    profileAce.session.setTabSize(2);
    profileAce.setOption('scrollPastEnd', 0.3);
    profileAce.session.setMode('ace/mode/markdown');

    // Match theme
    const theme = localStorage.getItem('kukuibot.theme') || 'blue';
    const themeMap = { blue: 'ace/theme/one_dark', claudia: 'ace/theme/one_dark', 'sol-dark': 'ace/theme/solarized_dark', 'sol-light': 'ace/theme/tomorrow' };
    profileAce.setTheme(themeMap[theme] || themeMap.blue);

    profileAce.session.setValue(profile);
    profileAce.clearSelection();

    profileAce.session.on('change', () => {
      const newDirty = profileAce.getValue() !== profileOriginal;
      if (newDirty !== profileDirty) {
        profileDirty = newDirty;
        _updateProfileToolbar();
      }
    });

    profileAce.commands.addCommand({
      name: 'save',
      bindKey: { win: 'Ctrl-S', mac: 'Cmd-S' },
      exec: () => saveProfile(),
    });
  }

  function destroyProfileEditor() {
    if (profileAce) {
      profileAce.destroy();
      profileAce = null;
    }
    profileDirty = false;
  }

  function _updateProfileToolbar() {
    const saveBtn = document.getElementById('profile-save-btn');
    const revertBtn = document.getElementById('profile-revert-btn');
    const dirtyDot = document.getElementById('profile-dirty-dot');
    const indicator = document.getElementById('profile-save-indicator');
    if (saveBtn) saveBtn.disabled = !profileDirty;
    if (revertBtn) revertBtn.disabled = !profileDirty;
    if (dirtyDot) dirtyDot.style.display = profileDirty ? 'inline-block' : 'none';
  }

  async function saveProfile() {
    if (!profileAce || !profileDirty) return;
    const text = profileAce.getValue();
    const resp = await apiFetch('/api/drafter/profile/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (resp.error) {
      alert('Save failed: ' + resp.error);
      return;
    }
    profile = text;
    profileOriginal = text;
    profileDirty = false;
    _updateProfileToolbar();
    await fetchStatus();
    // Flash save indicator
    const indicator = document.getElementById('profile-save-indicator');
    if (indicator) {
      indicator.textContent = 'Saved';
      indicator.classList.add('show');
      setTimeout(() => indicator.classList.remove('show'), 1500);
    }
  }

  function revertProfile() {
    if (!profileAce || !profileDirty) return;
    if (!confirm('Revert to last saved version?')) return;
    profileAce.session.setValue(profileOriginal);
    profileAce.clearSelection();
    profileDirty = false;
    _updateProfileToolbar();
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
    // Use the same availability check as the new-worker modal in app.js
    const isAvailable = typeof _isModelAvailable === 'function' ? _isModelAvailable : () => true;
    // Only include models that are actually connected/available
    const available = Object.entries(MODELS).filter(([k, m]) => isAvailable(k, m));
    // Claude Code models first, then Anthropic API, then OpenRouter
    const order = ['claude', 'anthropic', 'openrouter'];
    const groupOf = (key, def) => {
      if (key.startsWith('claude_')) return 'claude';
      if ((def.model || '') === 'anthropic' || key.startsWith('anthropic_')) return 'anthropic';
      if ((def.model || '') === 'openrouter' || key.startsWith('openrouter_')) return 'openrouter';
      return 'other';
    };
    const sorted = available.sort((a, b) => {
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
    const resp = await apiFetch(url, { method: 'POST' }, 300000);
    runningOp = '';
    if (resp.error) {
      alert('Drafter error: ' + resp.error);
    } else {
      const msg = `Drafted: ${resp.drafted || 0} | Skipped: ${resp.skipped || 0} | Spam: ${resp.spam || 0} | Errors: ${resp.errors || 0}`;
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
    destroyProfileEditor();
    runningOp = 'rebuild';
    rerender();
    const resp = await apiFetch('/api/drafter/profile/rebuild', { method: 'POST' }, 300000);
    runningOp = '';
    if (resp.error) {
      alert('Rebuild error: ' + resp.error);
    } else {
      await fetchProfile();
      await fetchStatus();
    }
    rerender();
    if (activeTab === 'profile' && profile) {
      requestAnimationFrame(() => initProfileEditor());
    }
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

  function toggleFilter(filterId, enabled) {
    const filters = (config.filters || []).map(f =>
      f.id === filterId ? { ...f, enabled } : f
    );
    saveConfig({ filters });
  }

  function saveFilterPatterns(filterId, value) {
    const items = value.split(',').map(s => s.trim()).filter(Boolean);
    const filters = (config.filters || []).map(f =>
      f.id === filterId ? { ...f, patterns: items } : f
    );
    saveConfig({ filters });
  }

  // --- Tab switch ---

  async function setTab(tab) {
    // Destroy profile editor when leaving profile tab
    if (activeTab === 'profile' && tab !== 'profile') {
      destroyProfileEditor();
    }
    // Skip if already on this tab with an active editor
    if (tab === 'profile' && profileAce) return;
    activeTab = tab;
    rerender();
    if (tab === 'inbox') {
      if (inboxMessages.length === 0) fetchInbox();
      if (inboxFolders.length === 0) fetchFolders();
    }
    if ((tab === 'history' || tab === 'spam') && history.length === 0) {
      await fetchHistory();
      rerender();
    }
    if (tab === 'profile') {
      await fetchProfile();
      rerender();
      // Init Ace editor after DOM has rendered
      requestAnimationFrame(() => initProfileEditor());
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

  function _buildInboxContext() {
    // Build a summary of recent inbox emails for AI context
    const msgs = inboxMessages.slice(0, 50);
    if (!msgs.length) return '';
    const lines = msgs.map((m, i) =>
      `${i + 1}. From: ${m.from || 'Unknown'} | Subject: ${m.subject || '(no subject)'} | Date: ${m.date || ''}`
    );
    return `[EMAIL CONTEXT — Recent inbox (${msgs.length} messages)]\n${lines.join('\n')}\n[END EMAIL CONTEXT]\n\n`;
  }

  async function chatSend() {
    const textarea = document.getElementById('email-chat-input');
    if (!textarea) return;
    const text = (textarea.value || '').trim();
    if (!text || chatStreaming) return;

    // Check if this is the first message in a fresh chat
    const isFirstMessage = chatMessages.filter(m => m.role === 'user').length === 0;

    chatMessages.push({ role: 'user', text, ts: Date.now() });
    textarea.value = '';
    textarea.style.height = 'auto';
    chatStreaming = true;
    chatStreamText = '';
    rerender();
    _scrollChatToBottom();

    chatAbortController = new AbortController();
    const sid = _resolveSessionId();

    // Prepend inbox context on first message so the AI knows about recent emails
    let apiMessage = text;
    if (isFirstMessage) {
      const ctx = _buildInboxContext();
      if (ctx) apiMessage = ctx + text;
    }

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: apiMessage, session_id: sid }),
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
    activeTab = 'inbox';
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

    // Fetch inbox on init
    fetchInbox();
    fetchFolders();
    fetchSyncStatus();

    // Refresh sync status every 60s
    syncTimer = setInterval(() => {
      if (typeof appMode !== 'undefined' && appMode === 'email') {
        fetchSyncStatus();
      }
    }, 60000);

    // Poll for new drafts every 30s while in email mode
    pollTimer = setInterval(async () => {
      if (typeof appMode !== 'undefined' && appMode === 'email') {
        await Promise.all([fetchStatus(), fetchDrafts()]);
        if (activeTab === 'drafts') rerender();
        if (activeTab === 'inbox') fetchInbox();
      }
    }, 30000);
  }

  function destroy() {
    initialized = false;
    destroyProfileEditor();
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
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

  // --- Render: Inbox Tab ---

  function renderInboxTab() {
    // Compose overlay (Gmail-style floating panel anchored bottom-right of preview)
    let composeHtml = '';
    if (showCompose) {
      const title = composeData.subject && composeData.subject.startsWith('Re: ') ? 'Reply' : 'New Message';
      composeHtml = `<div class="inbox-compose-overlay">
        <div class="inbox-compose-titlebar">
          <span class="inbox-compose-title">${title}</span>
          <button class="inbox-compose-close" onclick="EmailModule.closeCompose()" title="Close">&times;</button>
        </div>
        <div class="inbox-compose-form">
          <input class="inbox-compose-field" type="text" placeholder="To" value="${escHtml(composeData.to)}"
            oninput="EmailModule.updateCompose('to', this.value)" />
          <input class="inbox-compose-field" type="text" placeholder="Subject" value="${escHtml(composeData.subject)}"
            oninput="EmailModule.updateCompose('subject', this.value)" />
          <textarea class="inbox-compose-field inbox-compose-body" rows="8" placeholder="Message body..."
            oninput="EmailModule.updateCompose('body', this.value)">${escHtml(composeData.body)}</textarea>
        </div>
        <div class="inbox-compose-footer">
          <button class="email-action-btn primary" onclick="EmailModule.sendCompose()" ${composeSending ? 'disabled' : ''}>
            ${composeSending ? '<span class="email-spinner"></span> Sending...' : 'Send'}
          </button>
          <button class="email-action-btn" onclick="EmailModule.saveDraftCompose()">Save Draft</button>
          <button class="email-action-btn danger" onclick="EmailModule.closeCompose()" style="margin-left:auto">Discard</button>
        </div>
      </div>`;
    }

    // Folder dropdown options
    const folderOpts = (inboxFolders.length ? inboxFolders : ['INBOX']).map(f =>
      `<option value="${escHtml(f)}" ${f === inboxFolder ? 'selected' : ''}>${escHtml(f)}</option>`
    ).join('');

    // Toolbar
    const toolbarHtml = `<div class="inbox-toolbar">
      <select class="inbox-folder-select" onchange="EmailModule.setInboxFolder(this.value)">${folderOpts}</select>
      <input class="inbox-search" type="text" placeholder="Search mail..." value="${escHtml(inboxSearch)}"
        onkeydown="if(event.key==='Enter'){EmailModule.setInboxSearch(this.value)}" />
      <button class="email-action-btn" onclick="EmailModule.openCompose()" title="Compose">Compose</button>
      <button class="email-action-btn" onclick="EmailModule.refreshInbox()" title="Refresh">Refresh</button>
      ${syncStatus && syncStatus.last_sync_ago_str ? '<span class="inbox-sync-status">Synced ' + escHtml(syncStatus.last_sync_ago_str) + '</span>' : ''}
    </div>`;

    // Message list
    let listHtml = '';
    if (inboxLoading) {
      listHtml = '<div class="inbox-empty"><span class="email-spinner"></span><div>Loading...</div></div>';
    } else if (inboxMessages.length === 0) {
      listHtml = '<div class="inbox-empty"><div style="font-size:32px;opacity:0.3">&#128235;</div><div>No messages</div></div>';
    } else {
      listHtml = inboxMessages.map(m => {
        const isSelected = String(m.uid) === String(selectedUid);
        const unreadCls = m.is_read ? '' : ' unread';
        const selectedCls = isSelected ? ' selected' : '';
        return `<div class="inbox-msg-row${unreadCls}${selectedCls}" onclick="EmailModule.selectMessage('${escHtml(m.folder || inboxFolder)}', '${escHtml(String(m.uid))}')">
          <div class="inbox-msg-sender">${escHtml(parseSender(m.from))}</div>
          <div class="inbox-msg-subject">${escHtml(m.subject || '(no subject)')}</div>
          <div class="inbox-msg-snippet">${escHtml(m.snippet || '')}</div>
          <div class="inbox-msg-date">${fmtInboxDate(m.date)}</div>
        </div>`;
      }).join('');
    }

    // Preview pane
    let previewHtml = '';
    if (messageLoading) {
      previewHtml = '<div class="inbox-empty"><span class="email-spinner"></span><div>Loading message...</div></div>';
    } else if (selectedMessage) {
      const sm = selectedMessage;
      let bodyContent = '';
      if (sm.body_html) {
        // Render HTML in sandboxed iframe
        const safeHtml = sm.body_html.replace(/"/g, '&quot;');
        bodyContent = `<iframe class="inbox-preview-iframe" sandbox="allow-same-origin" srcdoc="${safeHtml}"></iframe>`;
      } else {
        bodyContent = `<pre class="inbox-preview-text">${escHtml(sm.body || '(empty)')}</pre>`;
      }

      const attachmentsHtml = (sm.attachments && sm.attachments.length) ? renderAttachments(sm.attachments) : '';

      previewHtml = `<div class="inbox-preview-header">
          <div style="font-size:15px;font-weight:600;color:var(--text);margin-bottom:4px">${escHtml(sm.subject || '(no subject)')}</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:2px">From: ${escHtml(sm.from || '')}</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:2px">To: ${escHtml(sm.to || '')}</div>
          <div style="font-size:12px;color:var(--muted)">Date: ${escHtml(sm.date || '')}</div>
          <div class="inbox-preview-actions">
            <button class="email-action-btn primary" onclick="EmailModule.aiReply()" ${aiReplyLoading ? 'disabled' : ''}>
              ${aiReplyLoading ? '<span class="email-spinner"></span> Generating...' : 'AI Reply'}
            </button>
            <button class="email-action-btn" onclick="EmailModule.replyTo()">Reply</button>
            <button class="email-action-btn danger" onclick="EmailModule.trashMessage('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')">Trash</button>
            <button class="email-action-btn" onclick="EmailModule.toggleReadStatus('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')">Mark Unread</button>
          </div>
        </div>
        <div class="inbox-preview-body">${bodyContent}${attachmentsHtml}</div>`;
    } else {
      previewHtml = '<div class="inbox-empty"><div style="font-size:32px;opacity:0.3">&#9993;</div><div>Select a message to read</div></div>';
    }

    return `<div class="inbox-container">
      ${toolbarHtml}
      <div class="inbox-split">
        <div class="inbox-list">${listHtml}</div>
        <div class="inbox-preview">${previewHtml}</div>
      </div>
      ${composeHtml}
    </div>`;
  }

  // --- Inbox UI actions ---

  function openCompose() {
    showCompose = true;
    composeData = { to: '', subject: '', body: '' };
    rerender();
  }

  function closeCompose() {
    showCompose = false;
    rerender();
  }

  function updateCompose(field, value) {
    composeData[field] = value;
  }

  function replyTo() {
    if (!selectedMessage) return;
    const sm = selectedMessage;
    const reSubject = (sm.subject || '').startsWith('Re: ') ? sm.subject : 'Re: ' + (sm.subject || '');
    const quotedBody = '\n\n--- Original Message ---\nFrom: ' + (sm.from || '') + '\nDate: ' + (sm.date || '') + '\n\n' + (sm.body || '');
    composeData = { to: sm.from || '', subject: reSubject, body: quotedBody };
    showCompose = true;
    rerender();
  }

  async function aiReply() {
    if (!selectedMessage || aiReplyLoading) return;
    const sm = selectedMessage;
    aiReplyLoading = true;
    rerender();
    const resp = await apiFetch('/api/drafter/ai-reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from: sm.from || '',
        subject: sm.subject || '',
        body: sm.body || '',
        message_id: sm.message_id || '',
      }),
    }, 120000);
    aiReplyLoading = false;
    if (resp.error) {
      alert('AI Reply error: ' + resp.error);
      rerender();
      return;
    }
    // Open compose overlay pre-filled with the AI-generated reply
    const reSubject = resp.subject || ('Re: ' + (sm.subject || ''));
    composeData = { to: sm.from || '', subject: reSubject, body: resp.reply_text || '' };
    showCompose = true;
    rerender();
  }

  function selectMessage(folder, uid) {
    fetchMessageDetail(folder, uid);
  }

  function setInboxFolder(folder) {
    inboxFolder = folder;
    selectedMessage = null;
    selectedUid = null;
    fetchInbox();
  }

  function setInboxSearch(query) {
    inboxSearch = query;
    fetchInbox();
  }

  function refreshInbox() {
    fetchInbox(true);  // bypass cache, force fresh IMAP fetch
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
      const cls = action === 'spam_detected' ? 'spam_detected' : action.startsWith('skip') ? 'skipped' : action;
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
    const threadCtx = config.thread_context || 'full_thread';
    const sig = config.signature_html || '';

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
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Thread Context</div>
            <div class="email-setting-desc">How much conversation history the AI sees when drafting a reply</div>
          </div>
          <select class="email-model-select" style="width:auto;min-width:140px" onchange="EmailModule.saveConfig({thread_context: this.value})">
            <option value="full_thread" ${threadCtx === 'full_thread' ? 'selected' : ''}>Full thread</option>
            <option value="latest_only" ${threadCtx === 'latest_only' ? 'selected' : ''}>Latest only</option>
          </select>
        </div>
      </div>

      <div class="email-settings-group">
        <h3>Inbox Sync</h3>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Sync Interval</div>
            <div class="email-setting-desc">Seconds between background email syncs (30-600). Lower = fresher inbox, more IMAP usage.</div>
          </div>
          <input type="number" class="email-setting-input" value="${config.sync_interval_sec || 180}" min="30" max="600" step="10"
            onchange="EmailModule.saveConfig({sync_interval_sec: parseInt(this.value)})" />
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
        <h3>Filters</h3>
        <div style="font-size:12px;color:var(--muted);margin-bottom:10px">
          Control which emails get auto-drafted replies. Disable a filter to stop it from skipping emails.
        </div>
        <div class="email-filter-list">
          ${(config.filters || []).filter(f => f.id !== 'spam_detection').map(f => {
            const isExclusion = f.type === 'exclusion';
            const patterns = (f.patterns || []).join(', ');
            return `<div class="email-filter-item${f.enabled ? '' : ' disabled'}">
              <div class="email-filter-row">
                <label class="email-toggle" style="flex-shrink:0">
                  <input type="checkbox" ${f.enabled ? 'checked' : ''} onchange="EmailModule.toggleFilter('${escHtml(f.id)}', this.checked)">
                  <span class="email-toggle-slider"></span>
                </label>
                <div class="email-filter-info">
                  <div class="email-filter-name">${escHtml(f.name)}</div>
                  <div class="email-filter-desc">${escHtml(f.description)}</div>
                </div>
              </div>
              ${isExclusion ? `<div class="email-filter-patterns" style="${f.enabled ? '' : 'opacity:0.5'}">
                <input type="text" class="email-setting-input" style="width:100%;text-align:left;font-size:12px"
                  value="${escHtml(patterns)}"
                  placeholder="${f.id === 'exclude_senders' ? 'e.g. *@noreply.github.com, alerts@*' : 'e.g. *newsletter*, *unsubscribe*'}"
                  onchange="EmailModule.saveFilterPatterns('${escHtml(f.id)}', this.value)" />
              </div>` : ''}
            </div>`;
          }).join('')}
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

  // --- Spam config helpers ---

  function getSpamFilter() {
    return (config.filters || []).find(f => f.id === 'spam_detection') || {};
  }

  function saveSpamSetting(key, value) {
    const filters = (config.filters || []).map(f => {
      if (f.id === 'spam_detection') return { ...f, [key]: value };
      return f;
    });
    saveConfig({ filters });
  }

  // --- Render: Spam Tab ---

  function renderSpamTab() {
    const sf = getSpamFilter();
    const enabled = sf.enabled !== false;
    const action = sf.spam_action || 'label';
    const notify = sf.notify !== false;
    const threshold = sf.confidence_threshold != null ? sf.confidence_threshold : 0.6;

    // Count spam from history
    const spamCount = history.filter(h => h.action === 'spam_detected').length;

    return `<div class="email-settings">
      <div class="email-settings-group">
        <h3>&#128737; Spam &amp; Phishing Protection</h3>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Enable AI Detection</div>
            <div class="email-setting-desc">Use AI to classify incoming emails as spam or phishing</div>
          </div>
          <label class="email-toggle">
            <input type="checkbox" ${enabled ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('enabled', this.checked)">
            <span class="email-toggle-slider"></span>
          </label>
        </div>
      </div>

      <div class="email-settings-group${enabled ? '' : ' disabled-section'}">
        <h3>Action on Detection</h3>
        <div style="font-size:12px;color:var(--muted);margin-bottom:10px">
          What to do when spam or phishing is detected.
        </div>
        <div class="spam-action-options">
          <label class="spam-action-option${action === 'label' ? ' active' : ''}">
            <input type="radio" name="spam_action" value="label" ${action === 'label' ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('spam_action', 'label')">
            <div class="spam-action-card">
              <div class="spam-action-icon">&#127991;</div>
              <div class="spam-action-title">Label</div>
              <div class="spam-action-desc">Prepend "SPAM:" to the subject line. Email stays in inbox.</div>
            </div>
          </label>
          <label class="spam-action-option${action === 'spam' ? ' active' : ''}">
            <input type="radio" name="spam_action" value="spam" ${action === 'spam' ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('spam_action', 'spam')">
            <div class="spam-action-card">
              <div class="spam-action-icon">&#128465;</div>
              <div class="spam-action-title">Move to Spam</div>
              <div class="spam-action-desc">Move the email to Gmail's Spam folder.</div>
            </div>
          </label>
          <label class="spam-action-option${action === 'trash' ? ' active' : ''}">
            <input type="radio" name="spam_action" value="trash" ${action === 'trash' ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('spam_action', 'trash')">
            <div class="spam-action-card">
              <div class="spam-action-icon">&#128711;</div>
              <div class="spam-action-title">Move to Trash</div>
              <div class="spam-action-desc">Move the email directly to Trash. Auto-deleted after 30 days.</div>
            </div>
          </label>
        </div>
      </div>

      <div class="email-settings-group${enabled ? '' : ' disabled-section'}">
        <h3>Sensitivity</h3>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Confidence Threshold</div>
            <div class="email-setting-desc">Minimum AI confidence to trigger action (${Math.round(threshold * 100)}%)</div>
          </div>
          <input type="range" class="spam-threshold-slider" min="0.3" max="0.95" step="0.05" value="${threshold}"
            oninput="this.nextElementSibling.textContent = Math.round(this.value * 100) + '%'"
            onchange="EmailModule.saveSpamSetting('confidence_threshold', parseFloat(this.value))">
          <span class="spam-threshold-val">${Math.round(threshold * 100)}%</span>
        </div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px;padding-left:2px">
          Lower = more aggressive (catches more spam, more false positives). Higher = more conservative.
        </div>
      </div>

      <div class="email-settings-group${enabled ? '' : ' disabled-section'}">
        <h3>Notifications</h3>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Show Spam Notifications</div>
            <div class="email-setting-desc">Display a notification when spam is detected during drafter runs</div>
          </div>
          <label class="email-toggle">
            <input type="checkbox" ${notify ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('notify', this.checked)">
            <span class="email-toggle-slider"></span>
          </label>
        </div>
      </div>

      <div class="email-settings-group">
        <h3>Recent Spam Activity</h3>
        ${spamCount > 0 ? `
          <div style="font-size:12px;color:var(--muted);margin-bottom:10px">${spamCount} spam emails detected in recent history</div>
          <div class="email-history-list" style="max-height:200px;overflow-y:auto">
            ${history.filter(h => h.action === 'spam_detected').slice(0, 20).map(h => `
              <div class="email-history-row">
                <span class="email-history-action spam_detected">spam</span>
                <span class="email-history-subject" title="${escHtml(h.subject)}">${escHtml(h.subject)}</span>
                <span class="email-history-from" title="${escHtml(h.from_addr)}">${escHtml(h.from_addr)}</span>
                <span class="email-history-time">${fmtDate(h.created_at)}</span>
              </div>
            `).join('')}
          </div>
        ` : `
          <div style="font-size:13px;color:var(--muted);padding:12px 0">No spam detected yet. Run the drafter to start scanning.</div>
        `}
      </div>
    </div>`;
  }

  // --- Render: Profile Tab ---

  function renderProfileTab() {
    if (runningOp === 'rebuild') {
      return `<div class="email-empty">
        <span class="email-spinner"></span>
        <div>Rebuilding style profile from sent emails...</div>
        <div class="email-empty-desc">This analyzes your last 100 sent emails. May take a minute.</div>
      </div>`;
    }

    const age = status ? status.profile_age_days : null;
    const fresh = status ? status.profile_fresh : false;

    if (!profile) {
      return `<div class="email-empty">
        <div class="email-empty-icon">&#128221;</div>
        <div class="email-empty-title">No style profile yet</div>
        <div class="email-empty-desc">
          Click "Build Now" to analyze your sent emails and create a writing style profile.
          This is required before the drafter can generate replies.
        </div>
        <button class="email-action-btn primary" onclick="EmailModule.rebuildProfile()" style="margin-top:12px">Build Now</button>
      </div>`;
    }

    return `<div class="profile-editor-wrap">
      <div class="profile-editor-toolbar">
        <div class="profile-editor-toolbar-left">
          <span style="font-size:14px;font-weight:600;color:var(--text)">Writing Style Profile</span>
          <span id="profile-dirty-dot" class="editor-dirty-dot" style="display:none" title="Unsaved changes"></span>
          <span id="profile-save-indicator" class="editor-save-indicator"></span>
          ${age !== null && age !== undefined ? `<span style="font-size:12px;color:${fresh ? 'var(--green)' : 'var(--yellow)'}">
            ${fresh ? 'Fresh' : 'Stale'} (${age}d old)
          </span>` : ''}
        </div>
        <div class="profile-editor-toolbar-right">
          <button id="profile-save-btn" class="editor-btn" onclick="EmailModule.saveProfile()" disabled title="Save (Cmd+S)">Save</button>
          <button id="profile-revert-btn" class="editor-btn" onclick="EmailModule.revertProfile()" disabled title="Revert to saved">Revert</button>
          <button class="email-action-btn" onclick="EmailModule.rebuildProfile()">Rebuild</button>
        </div>
      </div>
      <div id="profile-ace-editor" class="profile-ace-editor"></div>
    </div>`;
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
        <button class="email-tab${activeTab === 'inbox' ? ' active' : ''}" onclick="EmailModule.setTab('inbox')">Inbox</button>
        <button class="email-tab${activeTab === 'drafts' ? ' active' : ''}" onclick="EmailModule.setTab('drafts')">Drafts${drafts.length ? ' (' + drafts.length + ')' : ''}</button>
        <button class="email-tab${activeTab === 'history' ? ' active' : ''}" onclick="EmailModule.setTab('history')">History</button>
        <button class="email-tab${activeTab === 'spam' ? ' active' : ''}" onclick="EmailModule.setTab('spam')">Spam</button>
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
      <div class="email-split-top${activeTab === 'inbox' ? '' : ' email-content'}">
        ${activeTab === 'inbox' ? renderInboxTab() : ''}
        ${activeTab === 'drafts' ? renderDraftsTab() : ''}
        ${activeTab === 'history' ? renderHistoryTab() : ''}
        ${activeTab === 'spam' ? renderSpamTab() : ''}
        ${activeTab === 'settings' ? renderSettingsTab() : ''}
        ${activeTab === 'profile' ? renderProfileTab() : ''}
      </div>
      <div class="email-split-bottom">
        ${renderChatPane()}
      </div>
    </div>`;
  }

  // --- Sidebar folder helpers ---

  const SIDEBAR_FOLDERS = [
    { key: 'INBOX',              label: 'Inbox',      icon: '&#128229;' },
    { key: '[Gmail]/Starred',    label: 'Starred',    icon: '&#11088;'  },
    { key: '[Gmail]/Sent Mail',  label: 'Sent',       icon: '&#128228;' },
    { key: '[Gmail]/Drafts',     label: 'Drafts',     icon: '&#128196;' },
    { key: '[Gmail]/Spam',       label: 'Spam',       icon: '&#9888;'   },
    { key: '[Gmail]/Trash',      label: 'Trash',      icon: '&#128465;' },
    { key: '[Gmail]/All Mail',   label: 'All Mail',   icon: '&#128231;' },
  ];

  function sidebarSelectFolder(folderKey) {
    if (activeTab !== 'inbox') {
      activeTab = 'inbox';
    }
    inboxFolder = folderKey;
    selectedMessage = null;
    selectedUid = null;
    rerender();
    fetchInbox();
  }

  function sidebarCompose() {
    if (activeTab !== 'inbox') {
      activeTab = 'inbox';
    }
    showCompose = true;
    composeData = { to: '', subject: '', body: '' };
    rerender();
  }

  function renderSidebar() {
    // Compose button
    let html = `<div class="email-sidebar-compose">
      <button class="email-sidebar-compose-btn" onclick="EmailModule.sidebarCompose()">
        <span style="font-size:16px">&#9998;</span> Compose
      </button>
    </div>`;

    // Folder nav
    html += '<div class="email-sidebar-folders">';
    for (const f of SIDEBAR_FOLDERS) {
      const isActive = activeTab === 'inbox' && inboxFolder === f.key;
      const count = f.key === 'INBOX'
        ? inboxMessages.filter(m => !m.is_read).length
        : f.key === '[Gmail]/Drafts' ? drafts.length : 0;
      html += `<button class="email-sidebar-folder${isActive ? ' active' : ''}" onclick="EmailModule.sidebarSelectFolder('${escHtml(f.key)}')">
        <span class="email-sidebar-folder-icon">${f.icon}</span>
        <span class="email-sidebar-folder-label">${f.label}</span>
        ${count > 0 ? `<span class="email-sidebar-folder-count">${count}</span>` : ''}
      </button>`;
    }
    html += '</div>';

    // Divider + drafter tabs
    html += '<div class="email-sidebar-divider"></div>';
    html += '<div class="email-sidebar-folders">';
    const drafterTabs = [
      { tab: 'drafts',   label: 'Auto Drafts', icon: '&#128221;', count: drafts.length },
      { tab: 'history',  label: 'History',      icon: '&#128203;', count: 0 },
      { tab: 'spam',     label: 'Spam Filter',  icon: '&#128737;', count: 0 },
      { tab: 'profile',  label: 'Style Profile',icon: '&#127912;', count: 0 },
      { tab: 'settings', label: 'Settings',     icon: '&#9881;',   count: 0 },
    ];
    for (const t of drafterTabs) {
      const isActive = activeTab === t.tab;
      html += `<button class="email-sidebar-folder${isActive ? ' active' : ''}" onclick="EmailModule.setTab('${t.tab}')">
        <span class="email-sidebar-folder-icon">${t.icon}</span>
        <span class="email-sidebar-folder-label">${t.label}</span>
        ${t.count > 0 ? `<span class="email-sidebar-folder-count">${t.count}</span>` : ''}
      </button>`;
    }
    html += '</div>';

    return html;
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
    toggleFilter,
    saveFilterPatterns,
    saveSpamSetting,
    toggleDraftExpand,
    refreshAll,
    chatSend,
    chatKeyDown,
    chatAutoResize,
    chatSetModel,
    chatSetWorker,
    chatClear,
    saveProfile,
    revertProfile,
    // Inbox
    fetchInbox,
    fetchMessageDetail,
    fetchFolders,
    trashMessage,
    toggleReadStatus,
    sendCompose,
    saveDraftCompose,
    selectMessage,
    openCompose,
    closeCompose,
    updateCompose,
    replyTo,
    aiReply,
    setInboxFolder,
    setInboxSearch,
    refreshInbox,
    sidebarSelectFolder,
    sidebarCompose,
    fetchSyncStatus,
  };
})();
