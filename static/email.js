/**
 * KukuiBot Email Drafter — frontend module.
 * Split pane: drafts/settings on top, dedicated AI chat on bottom.
 * Loaded lazily when user switches to email mode.
 */

const EmailModule = (function () {
  'use strict';

  // --- State ---
  let activeTab = 'inbox'; // 'inbox' | 'drafts' | 'settings' | 'profile'
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
  let selectedDraftUid = null;
  let selectedDraft = null;
  let draftOriginal = null;
  let draftOriginalLoading = false;
  let editingDraftUid = null;   // non-null when compose overlay is editing an existing draft
  let draftRewriting = false;
  let initialized = false;
  let pollTimer = null;

  // Inbox state
  let inboxMessages = [];       // [{from, to, subject, date, uid, folder, is_read, snippet}]
  let inboxFolder = 'INBOX';    // current folder
  let inboxSearch = '';          // IMAP search query
  let inboxLoading = false;
  let searchLoading = false;     // true while a search query is in-flight
  let inboxLoadingMore = false;  // true while fetching next page (infinite scroll)
  let inboxHasMore = true;       // false when server says no more messages
  let selectedMessage = null;   // full message object from /api/gmail/message
  let selectedUid = null;       // uid of currently selected message
  let messageLoading = false;
  let inboxFolders = [];        // available IMAP folders
  let showCompose = false;      // compose form visible
  let composeData = { to: '', subject: '', body: '', body_html: null }; // compose form state
  let composeSending = false;
  let composeRichMode = true;   // true = Quill rich text, false = plain textarea
  let composeQuill = null;      // Quill editor instance
  let aiReplyLoading = false;
  let syncStatus = null;      // {total_cached, last_sync_ts, last_sync_ago_str, folders}
  let syncTimer = null;
  let showRedirect = false;   // redirect dialog visible
  let redirectData = { to: '', subject: '' };
  let redirectSending = false;

  // Multi-select state
  let selectedUids = new Set();       // UIDs of checked messages (inbox)
  let lastCheckedUid = null;          // for shift-click range select
  let selectedDraftUids = new Set();  // UIDs of checked drafts
  let lastCheckedDraftUid = null;     // for shift-click range select in drafts
  let showShortcutsHelp = false;      // keyboard shortcuts help overlay

  // Chat state
  let chatMessages = []; // [{role, text, ts}]
  let chatStreaming = false;
  let chatStreamText = '';
  let chatModelKey = ''; // populated dynamically from available models
  let chatWorkerKey = 'none';
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

  /**
   * Animate a message row sliding out, then resolve after transition ends.
   */
  function _slideOutRow(uid) {
    return new Promise(resolve => {
      const row = document.querySelector(`.inbox-msg-row[onclick*="'${uid}'"]`);
      if (!row) { resolve(); return; }
      row.addEventListener('transitionend', function handler(e) {
        if (e.propertyName === 'max-height' || e.propertyName === 'transform') {
          row.removeEventListener('transitionend', handler);
          resolve();
        }
      });
      setTimeout(resolve, 400);
      requestAnimationFrame(() => row.classList.add('slide-out'));
    });
  }

  /** Animate multiple rows sliding out in parallel */
  function _slideOutRows(uids) {
    return Promise.all(uids.map(uid => _slideOutRow(String(uid))));
  }

  function buildReplyAllTo(sm) {
    const all = [sm.from || '', sm.to || '', sm.cc || '']
      .join(',')
      .split(',')
      .map(s => s.trim())
      .filter(Boolean);
    const seen = new Set();
    const unique = [];
    for (const addr of all) {
      const em = (addr.match(/<([^>]+)>/) || [, addr])[1].toLowerCase().trim();
      if (!seen.has(em)) {
        seen.add(em);
        unique.push(addr);
      }
    }
    return unique.join(', ');
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
    // Before innerHTML rebuild, save Quill content and null the reference so
    // _initComposeQuill can re-create it on the new DOM.
    if (composeQuill && showCompose && composeRichMode) {
      composeData.body_html = composeQuill.root.innerHTML;
      composeData.body = composeQuill.getText().trim();
      composeQuill = null;
    }
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

  // --- Drafter history label for inbox messages ---
  let _historyMap = null;
  let _historyMapVersion = -1;

  function _buildHistoryMap() {
    if (_historyMapVersion === history.length && _historyMap) return _historyMap;
    _historyMap = new Map();
    for (const h of history) {
      if (h.message_id && !_historyMap.has(h.message_id)) {
        _historyMap.set(h.message_id, h);
      }
    }
    _historyMapVersion = history.length;
    return _historyMap;
  }

  function _historyLabelForMessage(messageId) {
    if (!messageId) return null;
    const map = _buildHistoryMap();
    const h = map.get(messageId);
    if (!h) return null;

    const action = h.action || '';
    const reason = (h.skip_reason || '').toLowerCase();

    if (action === 'drafted') return { label: 'Drafted', cls: 'drafted' };
    if (action === 'error') return { label: 'Error', cls: 'error' };

    if (action === 'spam_detected') {
      const scoreMatch = (h.skip_reason || '').match(/\((\d+)%/);
      const scoreSuffix = scoreMatch ? ' ' + scoreMatch[1] + '%' : '';
      if (reason.includes('phishing')) return { label: 'Phishing' + scoreSuffix, cls: 'phishing' };
      if (reason.includes('fraud')) return { label: 'Fraud' + scoreSuffix, cls: 'phishing' };
      const isSpamFolder = inboxFolder === '[Gmail]/Spam';
      return { label: isSpamFolder ? 'Score' + scoreSuffix : 'Spam' + scoreSuffix, cls: 'spam' };
    }

    if (action === 'skipped_sensitive') {
      if (reason.includes('financial') || reason.includes('banking')) return { label: 'Financial', cls: 'sensitive' };
      if (reason.includes('2fa') || reason.includes('verification')) return { label: 'Security', cls: 'sensitive' };
      return { label: 'Sensitive', cls: 'sensitive' };
    }

    if (action === 'skipped_auto') {
      if (reason.includes('automated')) return { label: 'Automated', cls: 'skipped' };
      if (reason.includes('bulk') || reason.includes('mailing')) return { label: 'Bulk', cls: 'skipped' };
      if (reason.includes('from self')) return { label: 'Self', cls: 'skipped' };
      if (reason.includes('cc only')) return { label: 'CC Only', cls: 'skipped' };
      return { label: 'Skipped', cls: 'skipped' };
    }

    if (action === 'skipped_excluded') return { label: 'Excluded', cls: 'skipped' };
    if (action === 'skipped_empty') return { label: 'Empty', cls: 'skipped' };

    // Fallback for any other skipped action
    if (action.startsWith('skipped')) return { label: 'Skipped', cls: 'skipped' };
    return null;
  }

  // --- Inbox data fetching ---

  async function fetchInbox(skipCache) {
    inboxHasMore = true;
    const hadMessages = inboxMessages.length > 0;
    if (!hadMessages) {
      inboxLoading = true;
      rerender();
    }
    if (inboxSearch) {
      searchLoading = true;
      patchInboxList();
    }
    const payload = { folder: inboxFolder, max_results: 50, search: inboxSearch };
    if (skipCache) payload.cache = false;
    const resp = await apiFetch('/api/gmail/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp.ok && resp.messages) {
      inboxMessages = resp.messages;
      inboxHasMore = resp.has_more !== false && resp.messages.length > 0;
      selectedUids.clear();
    } else if (resp.error) {
      console.error('fetchInbox error:', resp.error);
    }
    inboxLoading = false;
    searchLoading = false;
    patchInboxList();
  }

  async function fetchInboxMore() {
    if (inboxLoadingMore || !inboxHasMore || inboxLoading) return;
    inboxLoadingMore = true;
    patchInboxList(); // show spinner at bottom
    const payload = { folder: inboxFolder, max_results: 50, offset: inboxMessages.length, search: inboxSearch };
    const resp = await apiFetch('/api/gmail/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp.ok && resp.messages) {
      const existingUids = new Set(inboxMessages.map(m => String(m.uid)));
      const newMsgs = resp.messages.filter(m => !existingUids.has(String(m.uid)));
      inboxMessages = [...inboxMessages, ...newMsgs];
      inboxHasMore = resp.has_more !== false && resp.messages.length > 0;
    } else {
      inboxHasMore = false;
    }
    inboxLoadingMore = false;
    patchInboxList();
  }

  async function fetchMessageDetail(folder, uid) {
    messageLoading = true;
    selectedUid = uid;
    selectedMessage = null;
    patchPreview();
    const resp = await apiFetch('/api/gmail/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uid: String(uid) }),
    });
    // Guard against stale responses: if user clicked a different message
    // while this request was in-flight, discard this response.
    if (String(selectedUid) !== String(uid)) return;
    if (resp.ok && resp.message) {
      selectedMessage = resp.message;
      // Mark as read if not already
      const msg = inboxMessages.find(m => String(m.uid) === String(uid));
      if (msg && !msg.is_read) {
        msg.is_read = true;
        patchInboxList(); // update bold/unread styling
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
    patchPreview();
  }

  async function fetchFolders() {
    const resp = await apiFetch('/api/gmail/folders');
    if (resp.ok && resp.folders) {
      inboxFolders = resp.folders;
    }
  }

  async function trashMessage(folder, uid) {
    if (!confirm('Move this message to trash?')) return;
    const slidePromise = _slideOutRow(String(uid));
    const resp = await apiFetch('/api/gmail/trash', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uid: String(uid) }),
    });
    if (resp.ok) {
      await slidePromise;
      inboxMessages = inboxMessages.filter(m => String(m.uid) !== String(uid));
      if (String(selectedUid) === String(uid)) {
        selectedMessage = null;
        selectedUid = null;
      }
      patchInboxList();
      patchPreview();
    } else if (resp.error) {
      const row = document.querySelector(`.inbox-msg-row[onclick*="'${uid}'"]`);
      if (row) row.classList.remove('slide-out');
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
      patchInboxList();
    }
  }

  async function sendCompose() {
    if (!composeData.to || !composeData.subject) {
      alert('To and Subject are required.');
      return;
    }
    // Extract content from Quill if active
    if (composeRichMode && composeQuill) {
      composeData.body = composeQuill.getText().trim();
      composeData.body_html = composeQuill.root.innerHTML;
    } else {
      composeData.body_html = null;
    }
    composeSending = true;
    rerender();

    if (editingDraftUid) {
      // Editing an existing draft — update body then send via draft API
      const html = composeData.body_html || composeData.body.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
      const updateResp = await apiFetch(`/api/drafter/drafts/${encodeURIComponent(editingDraftUid)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body_html: html }),
      });
      const sendUid = (updateResp.new_uid) ? updateResp.new_uid : editingDraftUid;
      const resp = await apiFetch(`/api/drafter/drafts/${encodeURIComponent(sendUid)}/send`, { method: 'POST' });
      composeSending = false;
      if (resp.error) {
        alert('Send error: ' + resp.error);
        rerender();
      } else {
        _destroyComposeQuill();
        composeData = { to: '', subject: '', body: '', body_html: null };
        showCompose = false;
        editingDraftUid = null;
        selectedDraft = null;
        selectedDraftUid = null;
        await refreshAll();
      }
    } else {
      // Normal compose — send directly
      const payload = { to: composeData.to, subject: composeData.subject, body: composeData.body };
      if (composeData.body_html) payload.body_html = composeData.body_html;
      const resp = await apiFetch('/api/gmail/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      composeSending = false;
      if (resp.error) {
        alert('Send error: ' + resp.error);
        rerender();
      } else {
        _destroyComposeQuill();
        composeData = { to: '', subject: '', body: '', body_html: null };
        showCompose = false;
        rerender();
        const el = document.querySelector('.inbox-compose-success');
        if (el) { el.style.display = 'block'; setTimeout(() => el.style.display = 'none', 2000); }
      }
    }
  }

  async function saveDraftCompose() {
    if (!composeData.to && !composeData.subject && !composeData.body) return;
    // Extract content from Quill if active
    if (composeRichMode && composeQuill) {
      composeData.body = composeQuill.getText().trim();
      composeData.body_html = composeQuill.root.innerHTML;
    } else {
      composeData.body_html = null;
    }

    if (editingDraftUid) {
      // Update the existing draft in-place
      const html = composeData.body_html || composeData.body.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
      const resp = await apiFetch(`/api/drafter/drafts/${encodeURIComponent(editingDraftUid)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body_html: html }),
      });
      if (resp.error) {
        alert('Save error: ' + resp.error);
      } else {
        // Update local state
        if (selectedDraft) {
          selectedDraft.body_html = html;
          selectedDraft.body = composeData.body;
          selectedDraft.snippet = composeData.body.replace(/\n/g, ' ').trim().substring(0, 120);
          if (resp.new_uid) {
            const oldUid = selectedDraftUid;
            selectedDraftUid = resp.new_uid;
            selectedDraft.uid = resp.new_uid;
            const idx = drafts.findIndex(d => d.uid === oldUid);
            if (idx >= 0) drafts[idx] = selectedDraft;
          }
        }
        _destroyComposeQuill();
        composeData = { to: '', subject: '', body: '', body_html: null };
        showCompose = false;
        editingDraftUid = null;
        rerender();
      }
    } else {
      // Create a new draft
      const payload = { to: composeData.to, subject: composeData.subject, body: composeData.body };
      if (composeData.body_html) payload.body_html = composeData.body_html;
      const resp = await apiFetch('/api/gmail/draft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (resp.error) {
        alert('Draft error: ' + resp.error);
      } else {
        _destroyComposeQuill();
        composeData = { to: '', subject: '', body: '', body_html: null };
        showCompose = false;
        rerender();
      }
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
    // Update badge in sidebar (works even when full renders are skipped)
    if (typeof updateDraftBadgeDOM === 'function') {
      _emailDraftCount = drafts.length;
      updateDraftBadgeDOM();
    }
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
    if (selectedDraftUid === uid) {
      selectedDraftUid = null;
      selectedDraft = null;
      draftOriginal = null;
    }
    await refreshAll();
  }

  // --- Draft split-pane functions ---

  async function selectDraft(uid) {
    selectedDraftUid = uid;
    selectedDraft = drafts.find(d => d.uid === uid) || null;
    draftOriginal = null;
    draftOriginalLoading = true;
    rerender();
    try {
      const draft = drafts.find(d => d.uid === uid);
      const inReplyTo = (draft && draft.in_reply_to) || '';
      const qs = inReplyTo ? `?message_id=${encodeURIComponent(inReplyTo)}` : '';
      const resp = await apiFetch(`/api/drafter/drafts/${encodeURIComponent(uid)}/original${qs}`);
      draftOriginal = resp.ok ? resp : null;
    } catch (e) {
      draftOriginal = null;
    }
    draftOriginalLoading = false;
    rerender();
  }

  function editDraft() {
    if (!selectedDraft) return;
    const d = selectedDraft;
    _destroyComposeQuill();

    // Build quoted original below the draft body (like replyTo does)
    let bodyHtml = d.body_html || '';
    let bodyPlain = d.body || '';

    if (draftOriginal) {
      const origFrom = draftOriginal.from || '';
      const origDate = draftOriginal.date || '';
      if (draftOriginal.body_html) {
        bodyHtml += '<br><br><div style="border-left:2px solid #ccc;padding-left:12px;margin-left:4px;color:#555;">'
          + '<p><strong>From:</strong> ' + escHtml(origFrom) + '<br><strong>Date:</strong> ' + escHtml(origDate) + '</p>'
          + draftOriginal.body_html
          + '</div>';
      } else if (draftOriginal.body) {
        bodyPlain += '\n\n--- Original Message ---\nFrom: ' + origFrom + '\nDate: ' + origDate + '\n\n' + draftOriginal.body;
      }
    }

    editingDraftUid = d.uid;
    composeData = {
      to: d.to || '',
      subject: d.subject || '',
      body: bodyPlain,
      body_html: bodyHtml || null,
    };
    showCompose = true;
    rerender();
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
  }

  async function aiRewriteDraft() {
    if (!selectedDraft || draftRewriting) return;
    draftRewriting = true;
    rerender();
    const resp = await apiFetch('/api/drafter/ai-reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from: draftOriginal ? draftOriginal.from : selectedDraft.to,
        subject: selectedDraft.subject || '',
        body: draftOriginal ? draftOriginal.body : '',
        message_id: selectedDraft.in_reply_to || '',
      }),
    }, 120000);
    if (resp.error) {
      alert('AI Rewrite error: ' + resp.error);
      draftRewriting = false;
      rerender();
      return;
    }
    // Convert AI reply to HTML
    const newHtml = (resp.reply_text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
    // Auto-save the rewritten draft
    const saveResp = await apiFetch(`/api/drafter/drafts/${encodeURIComponent(selectedDraftUid)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body_html: newHtml }),
    });
    selectedDraft.body_html = newHtml;
    selectedDraft.body = resp.reply_text || '';
    selectedDraft.snippet = selectedDraft.body.replace(/\n/g, ' ').trim().substring(0, 120);
    if (saveResp.new_uid) {
      const oldUid = selectedDraftUid;
      selectedDraftUid = saveResp.new_uid;
      selectedDraft.uid = saveResp.new_uid;
      const idx = drafts.findIndex(d => d.uid === oldUid);
      if (idx >= 0) drafts[idx] = selectedDraft;
    }
    draftRewriting = false;
    rerender();
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
    if (tab === 'profile') {
      await fetchProfile();
      rerender();
      // Init Ace editor after DOM has rendered
      requestAnimationFrame(() => initProfileEditor());
    }
    if (tab === 'settings') {
      if (!config.enabled && config.enabled !== false) {
        await fetchConfig();
      }
      if (history.length === 0) {
        await fetchHistory();
      }
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

  // --- Keyboard Shortcuts ---

  function _buildShortcutsHelp() {
    return `<div class="shortcuts-overlay" onclick="if(event.target===this){EmailModule.closeShortcutsHelp()}">
      <div class="shortcuts-modal">
        <button class="shortcuts-close" onclick="EmailModule.closeShortcutsHelp()">&times;</button>
        <h3>Keyboard Shortcuts</h3>
        <table>
          <tr><td>j</td><td>Next message</td></tr>
          <tr><td>k</td><td>Previous message</td></tr>
          <tr><td>o / Enter</td><td>Open focused message</td></tr>
          <tr><td>r / a</td><td>Reply to All</td></tr>
          <tr><td>f</td><td>Forward</td></tr>
          <tr><td># / Delete</td><td>Trash selected</td></tr>
          <tr><td>x</td><td>Toggle checkbox</td></tr>
          <tr><td>Shift+i</td><td>Mark as read</td></tr>
          <tr><td>Shift+u</td><td>Mark as unread</td></tr>
          <tr><td>s</td><td>Toggle star</td></tr>
          <tr><td>c</td><td>Compose new email</td></tr>
          <tr><td>/</td><td>Focus search bar</td></tr>
          <tr><td>Esc</td><td>Close overlay / Deselect all</td></tr>
          <tr><td>${navigator.platform.includes('Mac') ? 'Cmd' : 'Ctrl'}+Enter</td><td>Send compose</td></tr>
          <tr><td>?</td><td>Show this help</td></tr>
        </table>
      </div>
    </div>`;
  }

  function closeShortcutsHelp() {
    showShortcutsHelp = false;
    const el = document.querySelector('.shortcuts-overlay');
    if (el) el.remove();
  }

  function _handleKeyboardShortcut(e) {
    // Only active on inbox/drafts tabs
    if (activeTab !== 'inbox' && activeTab !== 'drafts') return;

    const tag = e.target.tagName;
    const isEditable = tag === 'INPUT' || tag === 'TEXTAREA' ||
      e.target.contentEditable === 'true' || e.target.closest('.ql-editor');

    // Escape and Ctrl/Cmd+Enter work even in compose
    if (e.key === 'Escape') {
      if (showShortcutsHelp) { closeShortcutsHelp(); return; }
      if (showCompose) { closeCompose(); return; }
      if (showRedirect) { closeRedirect(); return; }
      if (selectedUids.size > 0) { bulkDeselectAll(); return; }
      if (selectedDraftUids.size > 0) { bulkDeselectAllDrafts(); return; }
      return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      if (showCompose) { e.preventDefault(); sendCompose(); }
      return;
    }

    // All other shortcuts disabled when typing
    if (isEditable) return;

    const key = e.key;

    // Prevent default for shortcut keys
    if (['j','k','x','s','c','r','a','f','e','/','?','Delete'].includes(key) || key === '#') {
      e.preventDefault();
    }

    if (key === '?') {
      showShortcutsHelp = !showShortcutsHelp;
      if (showShortcutsHelp) {
        const div = document.createElement('div');
        div.innerHTML = _buildShortcutsHelp();
        document.body.appendChild(div.firstElementChild);
      } else {
        closeShortcutsHelp();
      }
      return;
    }

    if (key === '/') {
      const searchInput = document.querySelector('.inbox-search');
      if (searchInput) searchInput.focus();
      return;
    }

    if (key === 'c') {
      openCompose();
      return;
    }

    if (activeTab === 'inbox') {
      if (key === 'j' || key === 'k') {
        const uids = inboxMessages.map(m => String(m.uid));
        if (!uids.length) return;
        let idx = selectedUid ? uids.indexOf(String(selectedUid)) : -1;
        if (key === 'j') idx = idx < uids.length - 1 ? idx + 1 : idx;
        else idx = idx > 0 ? idx - 1 : (idx === -1 ? uids.length - 1 : idx);
        if (idx === -1) idx = 0;
        const m = inboxMessages[idx];
        selectMessage(m.folder || inboxFolder, String(m.uid));
        return;
      }

      if ((key === 'o' || key === 'Enter') && selectedUid) {
        const m = inboxMessages.find(m => String(m.uid) === String(selectedUid));
        if (m) selectMessage(m.folder || inboxFolder, String(m.uid));
        return;
      }

      if ((key === 'r' || key === 'a') && selectedMessage) {
        replyTo();
        return;
      }

      if (key === 'f' && selectedMessage) {
        forward();
        return;
      }

      if (key === 'x' && selectedUid) {
        toggleCheck(String(selectedUid));
        return;
      }

      if (key === 's' && selectedUid) {
        toggleStar(inboxFolder, String(selectedUid));
        return;
      }

      if (key === '#' || key === 'Delete') {
        if (selectedUids.size > 0) {
          bulkTrash();
        } else if (selectedUid) {
          trashMessage(inboxFolder, String(selectedUid));
        }
        return;
      }

      if (key === 'I' && e.shiftKey) {
        if (selectedUids.size > 0) {
          bulkMarkRead();
        } else if (selectedUid) {
          const msg = inboxMessages.find(m => String(m.uid) === String(selectedUid));
          if (msg && !msg.is_read) toggleReadStatus(inboxFolder, String(selectedUid));
        }
        return;
      }

      if (key === 'U' && e.shiftKey) {
        if (selectedUids.size > 0) {
          bulkMarkUnread();
        } else if (selectedUid) {
          const msg = inboxMessages.find(m => String(m.uid) === String(selectedUid));
          if (msg && msg.is_read) toggleReadStatus(inboxFolder, String(selectedUid));
        }
        return;
      }

      if (key === 'e') {
        // TODO: archive
        return;
      }
    }

    if (activeTab === 'drafts') {
      if (key === 'j' || key === 'k') {
        if (!drafts.length) return;
        const draftUids = drafts.map(d => String(d.uid));
        let idx = selectedDraftUid ? draftUids.indexOf(String(selectedDraftUid)) : -1;
        if (key === 'j') idx = idx < draftUids.length - 1 ? idx + 1 : idx;
        else idx = idx > 0 ? idx - 1 : (idx === -1 ? draftUids.length - 1 : idx);
        if (idx === -1) idx = 0;
        selectDraft(draftUids[idx]);
        return;
      }

      if (key === 'x' && selectedDraftUid) {
        toggleDraftCheck(String(selectedDraftUid));
        return;
      }

      if (key === '#') {
        if (selectedDraftUids.size > 0) {
          bulkDiscardDrafts();
        } else if (selectedDraftUid) {
          discardDraft(selectedDraftUid);
        }
        return;
      }
    }
  }

  // --- Lifecycle ---

  async function init() {
    if (initialized) return;
    initialized = true;
    activeTab = 'inbox';
    selectedDraftUid = null;
    selectedDraft = null;
    draftOriginal = null;
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
    document.addEventListener('keydown', _handleKeyboardShortcut);

    await Promise.all([fetchStatus(), fetchConfig(), fetchDrafts(), fetchHistory()]);
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
        await Promise.all([fetchStatus(), fetchDrafts(), fetchHistory()]);
        // Skip rerender while compose overlay is open — innerHTML rebuild
        // destroys the Quill editor instance and leaves the overlay blank.
        if (activeTab === 'drafts' && !showCompose) rerender();
        if (activeTab === 'inbox' && !showCompose) fetchInbox();
      }
    }, 30000);
  }

  function destroy() {
    initialized = false;
    destroyProfileEditor();
    _destroyComposeQuill();
    document.removeEventListener('keydown', _handleKeyboardShortcut);
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
    if (chatAbortController) { try { chatAbortController.abort(); } catch {} }
  }

  // --- Render: Status Banner ---

  function renderStatusBanner(rightHtml) {
    const right = rightHtml ? `<span class="email-status-actions">${rightHtml}</span>` : '';

    if (status === null) {
      return `<div class="email-status-banner">
        <span class="email-spinner"></span>
        <span class="email-status-text">Loading status...</span>
        ${right}
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
        ${right}
      </div>`;
    }

    if (!connected) {
      return `<div class="email-status-banner">
        <span class="email-status-dot red"></span>
        <span class="email-status-text">Gmail not connected</span>
        <span class="email-status-sub">Configure in <a href="/settings-v2.html#gmail" style="color:var(--accent)">Settings</a></span>
        ${right}
      </div>`;
    }
    if (!hasKey) {
      return `<div class="email-status-banner">
        <span class="email-status-dot red"></span>
        <span class="email-status-text">No AI backend available</span>
        <span class="email-status-sub">Claude Code or an API key is required. Check <a href="/settings-v2.html#anthropic" style="color:var(--accent)">Settings</a></span>
        ${right}
      </div>`;
    }
    if (!hasPerm) {
      return `<div class="email-status-banner">
        <span class="email-status-dot yellow"></span>
        <span class="email-status-text">Auto-draft permission not enabled</span>
        <span class="email-status-sub">Enable in <a href="/settings-v2.html#gmail" style="color:var(--accent)">Settings &gt; Gmail</a></span>
        ${right}
      </div>`;
    }
    if (!enabled) {
      return `<div class="email-status-banner">
        <span class="email-status-dot muted"></span>
        <span class="email-status-text">Drafter disabled</span>
        <span class="email-status-sub">Enable it in the Settings tab</span>
        ${right}
      </div>`;
    }

    const lastRun = status.last_run_at ? timeAgo(status.last_run_at) : 'never';
    return `<div class="email-status-banner">
      <span class="email-status-dot green"></span>
      <span class="email-status-text">Active &middot; Last run: ${escHtml(lastRun)}</span>
      <span class="email-status-sub">${status.last_drafts_count || 0} drafted &middot; Profile ${status.profile_fresh ? 'fresh' : 'stale'}</span>
      ${right}
    </div>`;
  }

  // --- Inbox DOM patching (prevents full-page blink) ---

  function buildListHtml() {
    const clearBtn = inboxSearch ? `<button class="inbox-search-clear" onclick="EmailModule.clearSearch()" title="Clear search">&times;</button>` : '';
    const dotsHtml = searchLoading ? '<span class="inbox-search-dots"><span>.</span><span>.</span><span>.</span></span>' : '';
    const allChecked = inboxMessages.length > 0 && inboxMessages.every(m => selectedUids.has(String(m.uid)));
    const someChecked = inboxMessages.some(m => selectedUids.has(String(m.uid)));
    let html = `<div class="inbox-search-bar">
      <input type="checkbox" class="inbox-select-all" ${allChecked ? 'checked' : ''} onclick="EmailModule.toggleSelectAll()" title="Select all" />
      <div class="inbox-search-wrap">
        <input class="inbox-search" type="text" placeholder="Search Gmail..." value="${escHtml(inboxSearch)}"
          onkeydown="if(event.key==='Enter'){EmailModule.setInboxSearch(this.value)}" />
        ${dotsHtml}${clearBtn}
      </div>
    </div>`;
    if (inboxLoading && inboxMessages.length === 0) {
      html += '<div class="inbox-empty"><span class="email-spinner"></span><div>Loading...</div></div>';
    } else if (inboxMessages.length === 0) {
      html += '<div class="inbox-empty"><div style="font-size:32px;opacity:0.3">&#128235;</div><div>No messages</div></div>';
    } else {
      html += inboxMessages.map(m => {
        const isSelected = String(m.uid) === String(selectedUid);
        const isChecked = selectedUids.has(String(m.uid));
        const unreadCls = m.is_read ? '' : ' unread';
        const selectedCls = isSelected ? ' selected' : '';
        const checkedCls = isChecked ? ' checked' : '';
        const starCls = m.is_starred ? ' starred' : '';
        const starChar = m.is_starred ? '\u2605' : '\u2606';
        return `<div class="inbox-msg-row${unreadCls}${selectedCls}${checkedCls}" onclick="EmailModule.selectMessage('${escHtml(m.folder || inboxFolder)}', '${escHtml(String(m.uid))}')">
          <input type="checkbox" class="inbox-msg-check" ${isChecked ? 'checked' : ''} onclick="event.stopPropagation(); EmailModule.toggleCheck('${escHtml(String(m.uid))}', event)" />
          <span class="inbox-msg-star${starCls}" onclick="EmailModule.toggleStar('${escHtml(m.folder || inboxFolder)}', '${escHtml(String(m.uid))}', event)" title="${m.is_starred ? 'Unstar' : 'Star'}">${starChar}</span>
          <div class="inbox-msg-content">
            <div class="inbox-msg-sender">${escHtml(parseSender(m.from))}</div>
            <div class="inbox-msg-subject">${escHtml(m.subject || '(no subject)')}</div>
            <div class="inbox-msg-snippet">${escHtml(m.snippet || '')}</div>
          </div>
          <div class="inbox-msg-meta">
            <div class="inbox-msg-date">${fmtInboxDate(m.date)}</div>
            ${(() => { const hl = _historyLabelForMessage(m.message_id); return hl ? '<span class="inbox-msg-label ' + hl.cls + '">' + escHtml(hl.label) + '</span>' : ''; })()}
          </div>
        </div>`;
      }).join('');
      if (inboxLoadingMore) {
        html += '<div class="inbox-load-more"><span class="email-spinner"></span></div>';
      } else if (!inboxHasMore && inboxMessages.length > 0) {
        html += '<div class="inbox-load-more" style="opacity:0.4;font-size:12px">No more messages</div>';
      }
    }
    return html;
  }

  function patchInboxList() {
    if (activeTab !== 'inbox') return rerender();
    const el = document.querySelector('.inbox-list');
    if (!el) return rerender();
    el.innerHTML = buildListHtml();
    // Set indeterminate state on select-all checkbox (can't be set via HTML attribute)
    const selAllEl = el.querySelector('.inbox-select-all');
    if (selAllEl) {
      const someChecked = inboxMessages.some(m => selectedUids.has(String(m.uid)));
      const allChecked = inboxMessages.length > 0 && inboxMessages.every(m => selectedUids.has(String(m.uid)));
      selAllEl.indeterminate = someChecked && !allChecked;
    }
    // Attach scroll listener for infinite scroll
    const listEl = document.querySelector('.inbox-list');
    if (listEl && !listEl._scrollAttached) {
      listEl._scrollAttached = true;
      listEl.addEventListener('scroll', () => {
        if (listEl.scrollTop + listEl.clientHeight >= listEl.scrollHeight - 80) {
          fetchInboxMore();
        }
      });
    }
  }

  function patchPreview() {
    if (activeTab !== 'inbox') return rerender();
    const el = document.querySelector('.inbox-preview');
    if (!el) return rerender();
    // Bulk action bar replaces preview when items are selected
    let html = '';
    if (selectedUids.size > 0) {
      html = `<div class="inbox-bulk-bar">
        <span class="bulk-count">${selectedUids.size} selected</span>
        <button onclick="EmailModule.bulkTrash()">Trash</button>
        <button onclick="EmailModule.bulkMarkRead()">Mark Read</button>
        <button onclick="EmailModule.bulkMarkUnread()">Mark Unread</button>
        <button onclick="EmailModule.bulkDeselectAll()">Deselect All</button>
      </div>`;
      el.innerHTML = html;
      return;
    }
    if (messageLoading) {
      html = '<div class="inbox-empty"><span class="email-spinner"></span><div>Loading message...</div></div>';
    } else if (selectedMessage) {
      const sm = selectedMessage;
      let bodyContent = '';
      if (sm.body_html) {
        const cspMeta = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src * data: blob:; style-src * \'unsafe-inline\'; font-src *;">';
        const wrappedHtml = '<!DOCTYPE html><html><head>' + cspMeta + '<style>body{margin:0;padding:8px;font-family:sans-serif;color:#333;}</style></head><body>' + sm.body_html + '</body></html>';
        const safeHtml = wrappedHtml.replace(/"/g, '&quot;');
        bodyContent = `<iframe class="inbox-preview-iframe" srcdoc="${safeHtml}" onload="this.style.height=this.contentDocument.body.scrollHeight+'px'"></iframe>`;
      } else {
        bodyContent = `<pre class="inbox-preview-text">${escHtml(sm.body || '(empty)')}</pre>`;
      }
      const attachmentsHtml = (sm.attachments && sm.attachments.length) ? renderAttachments(sm.attachments) : '';
      const listMsg = inboxMessages.find(m => String(m.uid) === String(sm.uid || selectedUid));
      const isStarred = listMsg ? listMsg.is_starred : false;
      const previewStarCls = isStarred ? ' starred' : '';
      const previewStarChar = isStarred ? '\u2605' : '\u2606';
      html = `<div class="inbox-preview-header">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span class="inbox-preview-star${previewStarCls}" onclick="EmailModule.toggleStar('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')" title="${isStarred ? 'Unstar' : 'Star'}">${previewStarChar}</span>
            <div style="font-size:15px;font-weight:600;color:var(--text)">${escHtml(sm.subject || '(no subject)')}</div>
          </div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:2px">From: ${escHtml(sm.from || '')}</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:2px">To: ${escHtml(sm.to || '')}</div>
          ${sm.cc ? `<div style="font-size:12px;color:var(--muted);margin-bottom:2px">CC: ${escHtml(sm.cc)}</div>` : ''}
          <div style="font-size:12px;color:var(--muted)">Date: ${escHtml(sm.date || '')}</div>
          <div class="inbox-preview-actions">
            <button class="email-action-btn primary" onclick="EmailModule.aiReply()" ${aiReplyLoading ? 'disabled' : ''}>
              ${aiReplyLoading ? '<span class="email-spinner"></span> Generating...' : 'AI Reply'}
            </button>
            <button class="email-action-btn" onclick="EmailModule.replyTo()">Reply to All</button>
            <button class="email-action-btn" onclick="EmailModule.forward()">Forward</button>
            <button class="email-action-btn" onclick="EmailModule.openRedirect()">Redirect</button>
            <button class="email-action-btn danger" onclick="EmailModule.trashMessage('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')">Trash</button>
            <button class="email-action-btn" onclick="EmailModule.toggleReadStatus('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')">Mark Unread</button>
          </div>
          ${attachmentsHtml}
        </div>
        <div class="inbox-preview-body">${bodyContent}</div>`;
    } else {
      html = '<div class="inbox-empty"><div style="font-size:32px;opacity:0.3">&#9993;</div><div>Select a message to read</div></div>';
    }
    el.innerHTML = html;
  }

  // --- Quill Rich Text Editor ---

  function _initComposeQuill() {
    if (composeQuill) return;
    if (typeof Quill === 'undefined') {
      // Load Quill from CDN if not yet loaded
      if (!document.getElementById('quill-css')) {
        const link = document.createElement('link');
        link.id = 'quill-css';
        link.rel = 'stylesheet';
        link.href = 'https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.snow.css';
        document.head.appendChild(link);
      }
      if (!document.getElementById('quill-js')) {
        const script = document.createElement('script');
        script.id = 'quill-js';
        script.src = 'https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.js';
        script.onload = () => requestAnimationFrame(() => _initComposeQuill());
        document.head.appendChild(script);
      }
      return;
    }
    const container = document.getElementById('compose-editor');
    if (!container) return;
    composeQuill = new Quill('#compose-editor', {
      theme: 'snow',
      placeholder: 'Compose your message...',
      modules: {
        toolbar: [
          ['bold', 'italic', 'underline', 'strike'],
          [{ header: [1, 2, 3, false] }],
          [{ list: 'ordered' }, { list: 'bullet' }],
          ['link', 'clean'],
        ],
      },
    });
    // Set initial content: prefer HTML for rich formatting, fall back to plain text
    if (composeData.body_html) {
      composeQuill.clipboard.dangerouslyPasteHTML(composeData.body_html);
    } else if (composeData.body) {
      composeQuill.setText(composeData.body);
    }
  }

  function _destroyComposeQuill() {
    if (composeQuill) {
      composeQuill = null;
    }
  }

  function toggleComposeMode() {
    if (composeRichMode && composeQuill) {
      // Switching to plain text: extract text from Quill
      composeData.body = composeQuill.getText().trim();
      _destroyComposeQuill();
    }
    composeRichMode = !composeRichMode;
    rerender();
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
  }

  // --- Compose Overlay (shared by inbox + drafts tabs) ---

  function buildComposeOverlay() {
    if (!showCompose) return '';
    const title = editingDraftUid ? 'Edit Draft'
      : (composeData.subject && composeData.subject.startsWith('Re: ')) ? 'Reply' : 'New Message';
    const bodyEditor = composeRichMode
      ? `<div id="compose-editor" class="inbox-compose-quill"></div>`
      : `<textarea class="inbox-compose-field inbox-compose-body" rows="8" placeholder="Message body..."
          oninput="EmailModule.updateCompose('body', this.value)">${escHtml(composeData.body)}</textarea>`;
    const modeLabel = composeRichMode ? 'Rich Text' : 'Plain Text';
    const toggleLabel = composeRichMode ? 'Switch to Plain Text' : 'Switch to Rich Text';
    const saveLabel = editingDraftUid ? 'Save' : 'Save Draft';
    const html = `<div class="inbox-compose-overlay">
      <div class="inbox-compose-titlebar">
        <span class="inbox-compose-title">${title}</span>
        <span class="inbox-compose-mode-label" style="font-size:11px;color:var(--muted);margin-left:8px">${modeLabel}</span>
        <button class="inbox-compose-close" onclick="EmailModule.closeCompose()" title="Close">&times;</button>
      </div>
      <div class="inbox-compose-form">
        <input class="inbox-compose-field" type="text" placeholder="To" value="${escHtml(composeData.to)}"
          oninput="EmailModule.updateCompose('to', this.value)" />
        <input class="inbox-compose-field" type="text" placeholder="Subject" value="${escHtml(composeData.subject)}"
          oninput="EmailModule.updateCompose('subject', this.value)" />
        ${bodyEditor}
      </div>
      <div class="inbox-compose-footer">
        <button class="email-action-btn primary" onclick="EmailModule.sendCompose()" ${composeSending ? 'disabled' : ''}>
          ${composeSending ? '<span class="email-spinner"></span> Sending...' : 'Send'}
        </button>
        <button class="email-action-btn" onclick="EmailModule.saveDraftCompose()">${saveLabel}</button>
        <button class="email-action-btn" onclick="EmailModule.toggleComposeMode()" style="font-size:11px">${toggleLabel}</button>
        <button class="email-action-btn danger" onclick="EmailModule.closeCompose()" style="margin-left:auto">Discard</button>
      </div>
    </div>`;
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
    return html;
  }

  // --- Render: Inbox Tab ---

  function renderInboxTab() {
    const composeHtml = buildComposeOverlay();

    // Message list (with search bar at top)
    const listHtml = buildListHtml();

    // Preview pane — bulk bar when items selected
    let previewHtml = '';
    if (selectedUids.size > 0) {
      previewHtml = `<div class="inbox-bulk-bar">
        <span class="bulk-count">${selectedUids.size} selected</span>
        <button onclick="EmailModule.bulkTrash()">Trash</button>
        <button onclick="EmailModule.bulkMarkRead()">Mark Read</button>
        <button onclick="EmailModule.bulkMarkUnread()">Mark Unread</button>
        <button onclick="EmailModule.bulkDeselectAll()">Deselect All</button>
      </div>`;
    } else if (messageLoading) {
      previewHtml = '<div class="inbox-empty"><span class="email-spinner"></span><div>Loading message...</div></div>';
    } else if (selectedMessage) {
      const sm = selectedMessage;
      let bodyContent = '';
      if (sm.body_html) {
        // Render HTML email in sandboxed iframe
        const cspMeta = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src * data: blob:; style-src * \'unsafe-inline\'; font-src *;">';
        const wrappedHtml = '<!DOCTYPE html><html><head>' + cspMeta + '<style>body{margin:0;padding:8px;font-family:sans-serif;color:#333;}</style></head><body>' + sm.body_html + '</body></html>';
        const safeHtml = wrappedHtml.replace(/"/g, '&quot;');
        bodyContent = `<iframe class="inbox-preview-iframe" srcdoc="${safeHtml}" onload="this.style.height=this.contentDocument.body.scrollHeight+'px'"></iframe>`;
      } else {
        bodyContent = `<pre class="inbox-preview-text">${escHtml(sm.body || '(empty)')}</pre>`;
      }

      const attachmentsHtml = (sm.attachments && sm.attachments.length) ? renderAttachments(sm.attachments) : '';
      const listMsg2 = inboxMessages.find(m => String(m.uid) === String(sm.uid || selectedUid));
      const isStarred2 = listMsg2 ? listMsg2.is_starred : false;
      const previewStarCls2 = isStarred2 ? ' starred' : '';
      const previewStarChar2 = isStarred2 ? '\u2605' : '\u2606';

      previewHtml = `<div class="inbox-preview-header">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span class="inbox-preview-star${previewStarCls2}" onclick="EmailModule.toggleStar('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')" title="${isStarred2 ? 'Unstar' : 'Star'}">${previewStarChar2}</span>
            <div style="font-size:15px;font-weight:600;color:var(--text)">${escHtml(sm.subject || '(no subject)')}</div>
          </div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:2px">From: ${escHtml(sm.from || '')}</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:2px">To: ${escHtml(sm.to || '')}</div>
          ${sm.cc ? `<div style="font-size:12px;color:var(--muted);margin-bottom:2px">CC: ${escHtml(sm.cc)}</div>` : ''}
          <div style="font-size:12px;color:var(--muted)">Date: ${escHtml(sm.date || '')}</div>
          <div class="inbox-preview-actions">
            <button class="email-action-btn primary" onclick="EmailModule.aiReply()" ${aiReplyLoading ? 'disabled' : ''}>
              ${aiReplyLoading ? '<span class="email-spinner"></span> Generating...' : 'AI Reply'}
            </button>
            <button class="email-action-btn" onclick="EmailModule.replyTo()">Reply to All</button>
            <button class="email-action-btn" onclick="EmailModule.forward()">Forward</button>
            <button class="email-action-btn" onclick="EmailModule.openRedirect()">Redirect</button>
            <button class="email-action-btn danger" onclick="EmailModule.trashMessage('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')">Trash</button>
            <button class="email-action-btn" onclick="EmailModule.toggleReadStatus('${escHtml(sm.folder || inboxFolder)}', '${escHtml(String(sm.uid || selectedUid))}')">Mark Unread</button>
          </div>
          ${attachmentsHtml}
        </div>
        <div class="inbox-preview-body">${bodyContent}</div>`;
    } else {
      previewHtml = '<div class="inbox-empty"><div style="font-size:32px;opacity:0.3">&#9993;</div><div>Select a message to read</div></div>';
    }

    // Redirect dialog (similar to compose overlay but simpler)
    let redirectHtml = '';
    if (showRedirect) {
      redirectHtml = `<div class="inbox-compose-overlay" style="max-height:200px">
        <div class="inbox-compose-titlebar">
          <span class="inbox-compose-title">Redirect Email</span>
          <button class="inbox-compose-close" onclick="EmailModule.closeRedirect()" title="Close">&times;</button>
        </div>
        <div class="inbox-compose-form">
          <input id="redirect-to-input" class="inbox-compose-field" type="email" placeholder="Recipient email address"
            value="${escHtml(redirectData.to)}"
            oninput="EmailModule.updateRedirect('to', this.value)"
            onkeydown="if(event.key==='Enter'){event.preventDefault();EmailModule.sendRedirect()}" />
          <input class="inbox-compose-field" type="text" placeholder="Subject (leave as-is or edit)"
            value="${escHtml(redirectData.subject)}"
            oninput="EmailModule.updateRedirect('subject', this.value)"
            onkeydown="if(event.key==='Enter'){event.preventDefault();EmailModule.sendRedirect()}" />
        </div>
        <div class="inbox-compose-footer">
          <button class="email-action-btn primary" onclick="EmailModule.sendRedirect()" ${redirectSending ? 'disabled' : ''}>
            ${redirectSending ? '<span class="email-spinner"></span> Sending...' : 'Redirect'}
          </button>
          <button class="email-action-btn danger" onclick="EmailModule.closeRedirect()" style="margin-left:auto">Cancel</button>
        </div>
      </div>`;
    }

    return `<div class="inbox-container">
      <div class="inbox-split">
        <div class="inbox-list">${listHtml}</div>
        <div class="inbox-preview">${previewHtml}</div>
      </div>
      ${composeHtml}
      ${redirectHtml}
    </div>`;
  }

  // --- Inbox UI actions ---

  function openCompose() {
    _destroyComposeQuill();
    editingDraftUid = null;
    showCompose = true;
    composeData = { to: '', subject: '', body: '', body_html: null };
    rerender();
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
  }

  function closeCompose() {
    _destroyComposeQuill();
    showCompose = false;
    editingDraftUid = null;
    rerender();
  }

  function updateCompose(field, value) {
    composeData[field] = value;
  }

  function replyTo() {
    if (!selectedMessage) return;
    _destroyComposeQuill();
    editingDraftUid = null;
    const sm = selectedMessage;
    const reSubject = (sm.subject || '').startsWith('Re: ') ? sm.subject : 'Re: ' + (sm.subject || '');
    const quotedPlain = '\n\n--- Original Message ---\nFrom: ' + (sm.from || '') + '\nDate: ' + (sm.date || '') + '\n\n' + (sm.body || '');

    if (sm.body_html) {
      const quotedHtml = '<br><br><div style="border-left:2px solid #ccc;padding-left:12px;margin-left:4px;color:#555;">'
        + '<p><strong>From:</strong> ' + escHtml(sm.from || '') + '<br><strong>Date:</strong> ' + escHtml(sm.date || '') + '</p>'
        + sm.body_html
        + '</div>';
      composeData = { to: buildReplyAllTo(sm), subject: reSubject, body: quotedPlain, body_html: quotedHtml };
    } else {
      composeData = { to: buildReplyAllTo(sm), subject: reSubject, body: quotedPlain, body_html: null };
    }
    showCompose = true;
    rerender();
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
  }

  function forward() {
    if (!selectedMessage) return;
    _destroyComposeQuill();
    editingDraftUid = null;
    const sm = selectedMessage;
    const fwdSubject = (sm.subject || '').startsWith('Fwd: ') ? sm.subject : 'Fwd: ' + (sm.subject || '');
    const quotedPlain = '\n\n--- Forwarded Message ---\nFrom: ' + (sm.from || '') + '\nDate: ' + (sm.date || '') + '\nSubject: ' + (sm.subject || '') + '\n\n' + (sm.body || '');
    if (sm.body_html) {
      const quotedHtml = '<br><br><div style="border-left:2px solid #ccc;padding-left:12px;margin-left:4px;color:#555;">'
        + '<p><strong>From:</strong> ' + escHtml(sm.from || '') + '<br><strong>Date:</strong> ' + escHtml(sm.date || '') + '<br><strong>Subject:</strong> ' + escHtml(sm.subject || '') + '</p>'
        + sm.body_html + '</div>';
      composeData = { to: '', subject: fwdSubject, body: quotedPlain, body_html: quotedHtml };
    } else {
      composeData = { to: '', subject: fwdSubject, body: quotedPlain, body_html: null };
    }
    showCompose = true;
    rerender();
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
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
    // Open compose overlay pre-filled as a proper reply: AI text above quoted original
    const reSubject = resp.subject || ('Re: ' + (sm.subject || ''));
    const aiText = (resp.reply_text || '').trimEnd();
    const quotedPlain = '\n\n--- Original Message ---\nFrom: ' + (sm.from || '') + '\nDate: ' + (sm.date || '') + '\n\n' + (sm.body || '');
    _destroyComposeQuill();
    editingDraftUid = null;

    if (sm.body_html) {
      // Convert AI plain text to simple HTML paragraphs
      const aiHtml = aiText.split('\n').map(line => line.trim() ? '<p>' + escHtml(line) + '</p>' : '<p><br></p>').join('');
      const quotedHtml = '<br><div style="border-left:2px solid #ccc;padding-left:12px;margin-left:4px;color:#555;">'
        + '<p><strong>From:</strong> ' + escHtml(sm.from || '') + '<br><strong>Date:</strong> ' + escHtml(sm.date || '') + '</p>'
        + sm.body_html
        + '</div>';
      composeData = { to: buildReplyAllTo(sm), subject: reSubject, body: aiText + quotedPlain, body_html: aiHtml + quotedHtml };
    } else {
      composeData = { to: buildReplyAllTo(sm), subject: reSubject, body: aiText + quotedPlain, body_html: null };
    }
    showCompose = true;
    rerender();
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
  }

  function openRedirect() {
    if (!selectedMessage) return;
    redirectData = { to: '', subject: selectedMessage.subject || '' };
    showRedirect = true;
    redirectSending = false;
    rerender();
    // Focus the To field after render
    requestAnimationFrame(() => {
      const el = document.getElementById('redirect-to-input');
      if (el) el.focus();
    });
  }

  function closeRedirect() {
    showRedirect = false;
    redirectSending = false;
    rerender();
  }

  function updateRedirect(field, value) {
    redirectData[field] = value;
  }

  async function sendRedirect() {
    if (!redirectData.to || !selectedMessage) {
      alert('Recipient address is required.');
      return;
    }
    redirectSending = true;
    rerender();
    const resp = await apiFetch('/api/gmail/redirect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        folder: selectedMessage.folder || inboxFolder,
        uid: String(selectedMessage.uid || selectedUid),
        to: redirectData.to,
        subject: redirectData.subject || undefined,
      }),
    });
    redirectSending = false;
    if (resp.error) {
      alert('Redirect error: ' + resp.error);
      rerender();
    } else {
      showRedirect = false;
      rerender();
      // Toast feedback
      const toast = document.createElement('div');
      toast.className = 'inbox-compose-success';
      toast.textContent = 'Redirected to ' + redirectData.to;
      toast.style.cssText = 'position:fixed;bottom:20px;right:20px;background:var(--green,#22c55e);color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;z-index:9999;opacity:1;transition:opacity 0.5s';
      document.body.appendChild(toast);
      setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 500); }, 2500);
    }
  }

  function selectMessage(folder, uid) {
    fetchMessageDetail(folder, uid);
  }

  // --- Multi-select functions ---

  function toggleCheck(uid, event) {
    uid = String(uid);
    if (event && event.shiftKey && lastCheckedUid) {
      const uids = inboxMessages.map(m => String(m.uid));
      const start = uids.indexOf(lastCheckedUid);
      const end = uids.indexOf(uid);
      if (start !== -1 && end !== -1) {
        const [lo, hi] = start < end ? [start, end] : [end, start];
        for (let i = lo; i <= hi; i++) selectedUids.add(uids[i]);
      }
    } else {
      if (selectedUids.has(uid)) selectedUids.delete(uid);
      else selectedUids.add(uid);
    }
    lastCheckedUid = uid;
    patchInboxList();
  }

  function toggleSelectAll() {
    const allChecked = inboxMessages.length > 0 && inboxMessages.every(m => selectedUids.has(String(m.uid)));
    if (allChecked) {
      selectedUids.clear();
    } else {
      inboxMessages.forEach(m => selectedUids.add(String(m.uid)));
    }
    patchInboxList();
  }

  function toggleDraftCheck(uid, event) {
    uid = String(uid);
    if (event && event.shiftKey && lastCheckedDraftUid) {
      const uids = drafts.map(d => String(d.uid));
      const start = uids.indexOf(lastCheckedDraftUid);
      const end = uids.indexOf(uid);
      if (start !== -1 && end !== -1) {
        const [lo, hi] = start < end ? [start, end] : [end, start];
        for (let i = lo; i <= hi; i++) selectedDraftUids.add(uids[i]);
      }
    } else {
      if (selectedDraftUids.has(uid)) selectedDraftUids.delete(uid);
      else selectedDraftUids.add(uid);
    }
    lastCheckedDraftUid = uid;
    rerender();
  }

  function toggleDraftSelectAll() {
    const allChecked = drafts.length > 0 && drafts.every(d => selectedDraftUids.has(String(d.uid)));
    if (allChecked) {
      selectedDraftUids.clear();
    } else {
      drafts.forEach(d => selectedDraftUids.add(String(d.uid)));
    }
    rerender();
  }

  // --- Bulk action functions ---

  async function bulkTrash() {
    const uids = [...selectedUids];
    if (!uids.length) return;
    if (!confirm('Trash ' + uids.length + ' message(s)?')) return;
    const slidePromise = _slideOutRows(uids);
    for (const uid of uids) {
      const resp = await apiFetch('/api/gmail/trash', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: inboxFolder, uid: String(uid) }),
      });
      if (resp.ok) {
        inboxMessages = inboxMessages.filter(m => String(m.uid) !== String(uid));
        if (String(selectedUid) === String(uid)) {
          selectedMessage = null;
          selectedUid = null;
        }
      }
    }
    await slidePromise;
    selectedUids.clear();
    patchInboxList();
    patchPreview();
  }

  async function bulkMarkRead() {
    for (const uid of [...selectedUids]) {
      const msg = inboxMessages.find(m => String(m.uid) === String(uid));
      if (msg && !msg.is_read) {
        msg.is_read = true;
        apiFetch('/api/gmail/flags', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder: inboxFolder, uid: String(uid), flags: { seen: true } }),
        });
      }
    }
    selectedUids.clear();
    patchInboxList();
    patchPreview();
  }

  async function bulkMarkUnread() {
    for (const uid of [...selectedUids]) {
      const msg = inboxMessages.find(m => String(m.uid) === String(uid));
      if (msg && msg.is_read) {
        msg.is_read = false;
        apiFetch('/api/gmail/flags', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder: inboxFolder, uid: String(uid), flags: { seen: false } }),
        });
      }
    }
    selectedUids.clear();
    patchInboxList();
    patchPreview();
  }

  function bulkDeselectAll() {
    selectedUids.clear();
    patchInboxList();
    patchPreview();
  }

  async function bulkDiscardDrafts() {
    const uids = [...selectedDraftUids];
    if (!uids.length) return;
    if (!confirm('Discard ' + uids.length + ' draft(s)?')) return;
    for (const uid of uids) {
      runningOp = 'discard:' + uid;
      await apiFetch('/api/drafter/drafts/' + encodeURIComponent(uid) + '/discard', { method: 'POST' });
      if (selectedDraftUid === uid) {
        selectedDraftUid = null;
        selectedDraft = null;
        draftOriginal = null;
      }
    }
    runningOp = '';
    selectedDraftUids.clear();
    await refreshAll();
  }

  function bulkDeselectAllDrafts() {
    selectedDraftUids.clear();
    rerender();
  }

  function setInboxFolder(folder) {
    inboxFolder = folder;
    selectedMessage = null;
    selectedUid = null;
    selectedUids.clear();
    fetchInbox();
  }

  function setInboxSearch(query) {
    inboxSearch = query;
    fetchInbox();
  }

  function clearSearch() {
    inboxSearch = '';
    fetchInbox();
  }

  async function toggleStar(folder, uid, event) {
    if (event) event.stopPropagation();
    const msg = inboxMessages.find(m => String(m.uid) === String(uid));
    if (!msg) return;
    const newFlagged = !msg.is_starred;
    // Optimistic UI update
    msg.is_starred = newFlagged;
    patchInboxList();
    const resp = await apiFetch('/api/gmail/flags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, uid: String(uid), flags: { flagged: newFlagged } }),
    });
    if (!resp.ok) {
      // Revert on failure
      msg.is_starred = !newFlagged;
      patchInboxList();
    }
  }

  function refreshInbox() {
    fetchInbox(true);  // bypass cache, force fresh IMAP fetch
  }

  // --- Render: Drafts Tab ---

  function buildDraftListHtml() {
    const allChecked = drafts.length > 0 && drafts.every(d => selectedDraftUids.has(String(d.uid)));
    const someChecked = drafts.some(d => selectedDraftUids.has(String(d.uid)));
    let selectAllHtml = drafts.length > 0 ? `<div style="padding:6px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px">
      <input type="checkbox" class="inbox-select-all draft-select-all" ${allChecked ? 'checked' : ''} onclick="EmailModule.toggleDraftSelectAll()" title="Select all drafts" />
      <span style="font-size:11px;color:var(--muted)">Select all</span>
    </div>` : '';
    return selectAllHtml + drafts.map(d => {
      const isSelected = d.uid === selectedDraftUid;
      const isChecked = selectedDraftUids.has(String(d.uid));
      const aiLabel = d.is_ai_draft ? '<span class="inbox-msg-label ai-draft">AI Draft</span>' : '';
      return `<div class="draft-msg-row${isSelected ? ' selected' : ''}${isChecked ? ' checked' : ''}"
                   onclick="EmailModule.selectDraft('${escHtml(d.uid)}')">
        <input type="checkbox" class="inbox-msg-check" ${isChecked ? 'checked' : ''} onclick="event.stopPropagation(); EmailModule.toggleDraftCheck('${escHtml(String(d.uid))}', event)" />
        <div class="draft-msg-content">
          <div class="inbox-msg-sender">${escHtml(d.from_name || d.to)} ${aiLabel}</div>
          <div class="inbox-msg-subject">${escHtml(d.subject)}</div>
          <div class="inbox-msg-snippet">${escHtml(d.snippet || '')}</div>
        </div>
        <div class="inbox-msg-date">${fmtInboxDate(d.date)}</div>
      </div>`;
    }).join('');
  }

  function buildDraftPreviewHtml() {
    if (selectedDraftUids.size > 0) {
      return `<div class="inbox-bulk-bar">
        <span class="bulk-count">${selectedDraftUids.size} selected</span>
        <button onclick="EmailModule.bulkDiscardDrafts()">Discard</button>
        <button onclick="EmailModule.bulkDeselectAllDrafts()">Deselect All</button>
      </div>`;
    }
    if (!selectedDraft) {
      return '<div class="inbox-empty"><div style="font-size:32px;opacity:0.3">&#128221;</div><div>Select a draft to preview</div></div>';
    }

    const d = selectedDraft;
    const isSending = runningOp === 'send:' + d.uid;
    const isDiscarding = runningOp === 'discard:' + d.uid;
    const busy = isSending || isDiscarding;

    // Header block
    let html = `<div class="inbox-preview-header">
      <div style="font-size:15px;font-weight:600;color:var(--text);margin-bottom:4px">${escHtml(d.subject || '(no subject)')}</div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:2px">To: ${escHtml(d.to || '')}</div>
      <div style="font-size:12px;color:var(--muted)">Date: ${escHtml(d.date || '')}</div>`;

    // Action bar
    html += `<div class="inbox-preview-actions">
      <button class="email-action-btn primary" onclick="EmailModule.sendDraft('${escHtml(d.uid)}')" ${busy ? 'disabled' : ''}>
        ${isSending ? '<span class="email-spinner"></span> Sending...' : 'Send'}
      </button>
      <button class="email-action-btn" onclick="EmailModule.editDraft()">Edit</button>
      <button class="email-action-btn" onclick="EmailModule.aiRewriteDraft()" ${draftRewriting ? 'disabled' : ''}>
        ${draftRewriting ? '<span class="email-spinner"></span> Rewriting...' : 'AI Rewrite'}
      </button>
      <button class="email-action-btn danger" onclick="EmailModule.discardDraft('${escHtml(d.uid)}')" ${busy ? 'disabled' : ''}>
        ${isDiscarding ? '<span class="email-spinner"></span> Discarding...' : 'Discard'}
      </button>
    </div>`;
    html += '</div>';

    // Draft body in sandboxed iframe
    html += '<div class="inbox-preview-body">';
    if (d.body_html) {
      const cspMeta = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src * data: blob:; style-src * \'unsafe-inline\'; font-src *;">';
      const wrappedHtml = '<!DOCTYPE html><html><head>' + cspMeta + '<style>body{margin:0;padding:8px;font-family:tahoma,sans-serif;font-size:14px;color:#333;line-height:1.6;}</style></head><body>' + d.body_html + '</body></html>';
      const safeHtml = wrappedHtml.replace(/"/g, '&quot;');
      html += `<iframe class="inbox-preview-iframe" srcdoc="${safeHtml}" onload="this.style.height=this.contentDocument.body.scrollHeight+'px'"></iframe>`;
    } else {
      html += `<pre class="inbox-preview-text">${escHtml(d.body || '(empty)')}</pre>`;
    }
    html += '</div>';

    // Original message separator + content
    html += '<div class="draft-original-sep">&mdash;&mdash; Original Message &mdash;&mdash;</div>';

    if (draftOriginalLoading) {
      html += '<div class="inbox-empty" style="padding:20px"><span class="email-spinner"></span><div>Loading original...</div></div>';
    } else if (draftOriginal) {
      html += `<div class="inbox-preview-header" style="border-top:none">
        <div style="font-size:12px;color:var(--muted);margin-bottom:2px">From: ${escHtml(draftOriginal.from || '')}</div>
        <div style="font-size:12px;color:var(--muted)">Date: ${escHtml(draftOriginal.date || '')}</div>
      </div>`;
      html += '<div class="inbox-preview-body">';
      if (draftOriginal.body_html) {
        const cspMeta = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src * data: blob:; style-src * \'unsafe-inline\'; font-src *;">';
        const wrappedHtml = '<!DOCTYPE html><html><head>' + cspMeta + '<style>body{margin:0;padding:8px;font-family:sans-serif;color:#333;}</style></head><body>' + draftOriginal.body_html + '</body></html>';
        const safeHtml = wrappedHtml.replace(/"/g, '&quot;');
        html += `<iframe class="inbox-preview-iframe" srcdoc="${safeHtml}" onload="this.style.height=this.contentDocument.body.scrollHeight+'px'"></iframe>`;
      } else {
        html += `<pre class="inbox-preview-text">${escHtml(draftOriginal.body || '(empty)')}</pre>`;
      }
      html += '</div>';
    } else {
      html += '<div style="padding:16px;font-size:12px;color:var(--muted)">Original message not available</div>';
    }

    return html;
  }

  function renderDraftsTab() {
    const isRunning = runningOp === 'run' || runningOp === 'dry-run';
    const canRun = status && status.gmail_connected && status.has_api_key && status.has_auto_draft_perm;

    let html = '<div style="position:relative;display:flex;flex-direction:column;height:100%;min-height:0">';

    // Combined status banner + run controls in one bar
    const runBtns = `<button class="email-action-btn" onclick="EmailModule.runDrafter(true)" ${!canRun || isRunning ? 'disabled' : ''} title="Preview what would be drafted">
        ${runningOp === 'dry-run' ? '<span class="email-spinner"></span> Checking...' : 'Dry Run'}
      </button>
      <button class="email-action-btn primary" onclick="EmailModule.runDrafter(false)" ${!canRun || isRunning ? 'disabled' : ''} title="Check inbox and create drafts">
        ${runningOp === 'run' ? '<span class="email-spinner"></span> Running...' : 'Run Now'}
      </button>`;
    html += renderStatusBanner(runBtns);

    if (loading) {
      html += '<div class="email-empty"><span class="email-spinner"></span><div>Loading drafts...</div></div>';
      html += '</div>';
      return html;
    }

    if (drafts.length === 0) {
      html += `<div class="email-empty">
        <div class="email-empty-icon">&#9993;</div>
        <div class="email-empty-title">No drafts yet</div>
        <div class="email-empty-desc">
          ${status && status.enabled
            ? 'Run Now to generate AI draft replies, or create a draft in Gmail and it will appear here.'
            : 'Enable the drafter in Settings to generate AI draft replies, or create a draft in Gmail.'}
        </div>
      </div>`;
      html += '</div>';
      return html;
    }

    // Split-pane layout
    html += `<div class="draft-split">
      <div class="draft-list">${buildDraftListHtml()}</div>
      <div class="draft-preview">${buildDraftPreviewHtml()}</div>
    </div>`;
    html += buildComposeOverlay();
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

    // Spam filter state
    const sf = getSpamFilter();
    const spamEnabled = sf.enabled !== false;
    const spamAction = sf.spam_action || 'label';
    const spamNotify = sf.notify !== false;
    const spamThreshold = sf.confidence_threshold != null ? sf.confidence_threshold : 0.6;
    const spamCount = history.filter(h => h.action === 'spam_detected').length;

    // Build model/worker options for the chat section
    const modelOpts = availableModels.map(m =>
      `<option value="${escHtml(m.key)}" ${m.key === chatModelKey ? 'selected' : ''}>${escHtml(m.label)}</option>`
    ).join('');
    const workerOpts = [
      `<option value="none" ${chatWorkerKey === 'none' ? 'selected' : ''}>None (style only)</option>`,
      ...availableWorkers.map(w =>
        `<option value="${escHtml(w.key)}" ${w.key === chatWorkerKey ? 'selected' : ''}>${escHtml(w.name)}</option>`
      )
    ].join('');

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
        <h3>&#128737; Spam &amp; Phishing Protection</h3>
        <div class="email-setting-row">
          <div>
            <div class="email-setting-label">Enable AI Detection</div>
            <div class="email-setting-desc">Use AI to classify incoming emails as spam or phishing</div>
          </div>
          <label class="email-toggle">
            <input type="checkbox" ${spamEnabled ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('enabled', this.checked)">
            <span class="email-toggle-slider"></span>
          </label>
        </div>
        <div class="${spamEnabled ? '' : 'disabled-section'}" style="margin-top:10px">
          <div style="font-size:12px;color:var(--muted);margin-bottom:8px">
            What to do when spam or phishing is detected.
          </div>
          <div class="spam-action-options">
            <label class="spam-action-option${spamAction === 'label' ? ' active' : ''}">
              <input type="radio" name="spam_action" value="label" ${spamAction === 'label' ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('spam_action', 'label')">
              <div class="spam-action-card">
                <div class="spam-action-icon">&#127991;</div>
                <div class="spam-action-title">Label</div>
                <div class="spam-action-desc">Prepend "SPAM:" to the subject line. Email stays in inbox.</div>
              </div>
            </label>
            <label class="spam-action-option${spamAction === 'spam' ? ' active' : ''}">
              <input type="radio" name="spam_action" value="spam" ${spamAction === 'spam' ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('spam_action', 'spam')">
              <div class="spam-action-card">
                <div class="spam-action-icon">&#128465;</div>
                <div class="spam-action-title">Move to Spam</div>
                <div class="spam-action-desc">Move the email to Gmail's Spam folder.</div>
              </div>
            </label>
            <label class="spam-action-option${spamAction === 'trash' ? ' active' : ''}">
              <input type="radio" name="spam_action" value="trash" ${spamAction === 'trash' ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('spam_action', 'trash')">
              <div class="spam-action-card">
                <div class="spam-action-icon">&#128711;</div>
                <div class="spam-action-title">Move to Trash</div>
                <div class="spam-action-desc">Move the email directly to Trash. Auto-deleted after 30 days.</div>
              </div>
            </label>
          </div>
          <div class="email-setting-row" style="margin-top:10px">
            <div>
              <div class="email-setting-label">Confidence Threshold</div>
              <div class="email-setting-desc">Minimum AI confidence to trigger action (${Math.round(spamThreshold * 100)}%)</div>
            </div>
            <input type="range" class="spam-threshold-slider" min="0.3" max="0.95" step="0.05" value="${spamThreshold}"
              oninput="this.nextElementSibling.textContent = Math.round(this.value * 100) + '%'"
              onchange="EmailModule.saveSpamSetting('confidence_threshold', parseFloat(this.value))">
            <span class="spam-threshold-val">${Math.round(spamThreshold * 100)}%</span>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:4px;padding-left:2px">
            Lower = more aggressive (catches more spam, more false positives). Higher = more conservative.
          </div>
          <div class="email-setting-row" style="margin-top:10px">
            <div>
              <div class="email-setting-label">Show Spam Notifications</div>
              <div class="email-setting-desc">Display a notification when spam is detected during drafter runs</div>
            </div>
            <label class="email-toggle">
              <input type="checkbox" ${spamNotify ? 'checked' : ''} onchange="EmailModule.saveSpamSetting('notify', this.checked)">
              <span class="email-toggle-slider"></span>
            </label>
          </div>
        </div>
        ${spamCount > 0 ? `
          <div style="margin-top:12px;font-size:12px;color:var(--muted);margin-bottom:6px">${spamCount} spam emails detected in recent history</div>
          <div class="email-history-list" style="max-height:150px;overflow-y:auto">
            ${history.filter(h => h.action === 'spam_detected').slice(0, 10).map(h => `
              <div class="email-history-row">
                <span class="email-history-action spam_detected">spam</span>
                <span class="email-history-subject" title="${escHtml(h.subject)}">${escHtml(h.subject)}</span>
                <span class="email-history-from" title="${escHtml(h.from_addr)}">${escHtml(h.from_addr)}</span>
                <span class="email-history-time">${fmtDate(h.created_at)}</span>
              </div>
            `).join('')}
          </div>
        ` : ''}
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
    const workerLabel = chatWorkerKey === 'none' ? 'Email Assistant' : (currentWorker ? currentWorker.name : (chatWorkerKey || 'Assistant'));

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
    return `<div class="email-split">
      <div class="email-split-top${activeTab === 'inbox' ? '' : ' email-content'}">
        ${activeTab === 'inbox' ? renderInboxTab() : ''}
        ${activeTab === 'drafts' ? renderDraftsTab() : ''}
        ${activeTab === 'settings' ? renderSettingsTab() : ''}
        ${activeTab === 'profile' ? renderProfileTab() : ''}
      </div>
    </div>`;
  }

  // --- Sidebar folder helpers ---

  const SIDEBAR_FOLDERS = [
    { key: 'INBOX',              label: 'Inbox',      icon: '&#128229;' },
    { key: '[Gmail]/Starred',    label: 'Starred',    icon: '&#11088;'  },
    { key: '[Gmail]/Sent Mail',  label: 'Sent',       icon: '&#128228;' },
    { key: '[Gmail]/Spam',       label: 'Spam',       icon: '&#9888;'   },
    { key: '[Gmail]/Trash',      label: 'Trash',      icon: '&#128465;' },
    { key: '[Gmail]/All Mail',   label: 'All Mail',   icon: '&#128231;' },
  ];

  function sidebarSelectFolder(folderKey) {
    if (activeTab !== 'inbox') {
      activeTab = 'inbox';
    }
    inboxFolder = folderKey;
    inboxHasMore = true;
    selectedMessage = null;
    selectedUid = null;
    selectedUids.clear();
    rerender();
    fetchInbox();
  }

  function sidebarCompose() {
    if (activeTab !== 'inbox' && activeTab !== 'drafts') {
      activeTab = 'inbox';
    }
    _destroyComposeQuill();
    editingDraftUid = null;
    showCompose = true;
    composeData = { to: '', subject: '', body: '', body_html: null };
    rerender();
    if (composeRichMode) {
      requestAnimationFrame(() => _initComposeQuill());
    }
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
        : 0;
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
      { tab: 'drafts',   label: 'Drafts', icon: '&#128221;', count: drafts.length },
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

    // Sync status + button
    html += '<div class="email-sidebar-sync">';
    const syncBtnText = inboxLoading ? '<span class="email-sync-spinner"></span> Syncing...' : 'Sync Now';
    html += `<button class="email-sidebar-sync-btn${inboxLoading ? ' syncing' : ''}" onclick="EmailModule.refreshInbox()" title="Force refresh from Gmail" ${inboxLoading ? 'disabled' : ''}>${syncBtnText}</button>`;
    if (syncStatus && syncStatus.last_sync_ago_str && !inboxLoading) {
      html += `<span class="email-sidebar-sync-status">Synced ${escHtml(syncStatus.last_sync_ago_str)}</span>`;
    }
    html += '</div>';

    return html;
  }

  // --- Badge helpers ---

  function getDraftCount() {
    return drafts.length;
  }

  async function pollDraftCount() {
    try {
      const resp = await apiFetch('/api/drafter/drafts');
      const newDrafts = resp.drafts || [];
      const changed = newDrafts.length !== drafts.length;
      drafts = newDrafts;
      return changed;
    } catch {
      return false;
    }
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
    selectDraft,
    editDraft,
    aiRewriteDraft,
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
    forward,
    toggleComposeMode,
    openRedirect,
    closeRedirect,
    updateRedirect,
    sendRedirect,
    setInboxFolder,
    setInboxSearch,
    clearSearch,
    toggleStar,
    refreshInbox,
    sidebarSelectFolder,
    sidebarCompose,
    fetchSyncStatus,
    getDraftCount,
    pollDraftCount,
    // Multi-select
    toggleCheck,
    toggleSelectAll,
    toggleDraftCheck,
    toggleDraftSelectAll,
    bulkTrash,
    bulkMarkRead,
    bulkMarkUnread,
    bulkDeselectAll,
    bulkDiscardDrafts,
    bulkDeselectAllDrafts,
    closeShortcutsHelp,
  };
})();
