/**
 * KukuiBot — Vanilla JS frontend
 * Features: multi-tab, sidebar, SSE streaming, work log,
 *           elevation, auto-approve, reasoning, settings menu.
 */

const API = '';
let APP_NAME = 'KukuiBot'; // Updated from /api/status on init

// --- Auth 401 guard ---
// Redirect to login on 401 from any API call (session expired / not authenticated).
let _redirectingToLogin = false;
function handleAuthExpired() {
  if (_redirectingToLogin) return;
  _redirectingToLogin = true;
  window.location.href = '/login.html';
}
function check401(res) {
  if (res && res.status === 401) { handleAuthExpired(); return true; }
  return false;
}

// --- Tab / Session State ---
let tabs = [];
let activeTabId = null;
let tabCounter = 0;
let userName = '';
let editingTabId = null;  // Tab currently being renamed
let _tabClickTimer = null; // Debounce single-click so dblclick can cancel it
let editingTabRendered = false;  // Set after the initial rename render completes

const LS_TABS_KEY = 'kukuibot.tabs.v1';
const LS_ACTIVE_KEY = 'kukuibot.activeTab.v1';
const LS_AUTONAME_ENABLED_KEY = 'kukuibot.autoNameEnabled.v1';

let tabsSyncTimer = null;
let tabsSyncInFlight = false;

// --- Per-tab working state ---
let statusBanner = null;
let statusBannerTabId = null;
let safetyCapMessage = null;
let safetyCapTabId = null;
let firstToolJumpDone = false;
let firstToolJumpTabId = null;
let autoNaming = false;
let autoNameEnabled = false;
let autoNameTimer = null;
let autoNamedSessions = new Set();
const AUTO_NAME_INTERVAL_MS = 60 * 60 * 1000;

// --- Theme ---
const THEMES = ['blue', 'claudia', 'sol-dark', 'sol-light'];
const THEME_LABELS = { 'blue': 'Blue', 'claudia': 'Claudia', 'sol-dark': 'Solarized Dark', 'sol-light': 'Solarized Light' };
const LS_THEME_KEY = 'kukuibot.theme';
const THEME_CLASS = { 'blue': null, 'claudia': 'claude-theme', 'sol-dark': 'sol-dark', 'sol-light': 'sol-light' };
function applyTheme(theme) {
  document.body.classList.remove('sol-dark', 'sol-light', 'claude-theme');
  const cls = THEME_CLASS[theme];
  if (cls) document.body.classList.add(cls);
  localStorage.setItem(LS_THEME_KEY, theme);
  // Sync Ace editor theme if active
  if (typeof EditorModule !== 'undefined' && editorInitialized) {
    try { EditorModule.syncTheme(); } catch {}
  }
}
function cycleTheme() {
  const current = localStorage.getItem(LS_THEME_KEY) || 'blue';
  const idx = THEMES.indexOf(current);
  const next = THEMES[(idx + 1) % THEMES.length];
  applyTheme(next);
  showSettings = false;
  requestRender({ preserveScroll: true });
}
// Migrate old theme keys
(function() {
  const saved = localStorage.getItem(LS_THEME_KEY);
  if (saved === 'claude' || saved === 'default') localStorage.setItem(LS_THEME_KEY, 'claudia');
})();
applyTheme(localStorage.getItem(LS_THEME_KEY) || 'blue');

// --- App Mode (chat vs editor vs email) ---
let appMode = 'chat'; // 'chat' | 'editor' | 'email'
let editorInitialized = false;
let emailInitialized = false;
let _emailDraftCount = 0;
let _draftBadgeTimer = null;
let _editorModeSwitch = false; // true during the render that switches modes
let _emailRenderRequested = false; // set by EmailModule to allow one render through

function setAppMode(mode, initialPath) {
  if (mode === appMode && !initialPath) return;
  // Guard dirty editor state when leaving editor
  if (appMode === 'editor' && typeof EditorModule !== 'undefined' && EditorModule.getDirty()) {
    if (!confirm('You have unsaved changes in the editor. Switch anyway?')) return;
  }
  // Close mobile file dropdown if leaving editor
  if (appMode === 'editor' && typeof EditorModule !== 'undefined') {
    EditorModule.closeMobileDropdown();
    EditorModule.destroy();
    editorInitialized = false;
  }
  // Destroy email module when leaving email mode
  if (appMode === 'email' && typeof EmailModule !== 'undefined') {
    EmailModule.destroy();
    emailInitialized = false;
  }
  appMode = mode;
  showSettings = false;
  _editorModeSwitch = true; // allow this one render through
  requestRender({});
  // Init editor after render if switching to editor mode
  if (mode === 'editor') {
    _waitForEditorDOM(() => {
      if (typeof EditorModule !== 'undefined') {
        // Pre-set root before init so init's loadTree uses the correct path
        if (initialPath) EditorModule.presetRoot(initialPath);
        EditorModule.init();
        if (!editorInitialized) {
          EditorModule.postInit();
          editorInitialized = true;
        } else {
          EditorModule.syncTheme();
          // Reload tree for already-initialized editor (setRoot if path, else refresh)
          if (initialPath) {
            EditorModule.setRoot(initialPath);
          } else {
            EditorModule.loadTree();
          }
        }
        editorInitialized = true;
      }
    });
  }
  // Init email module after render
  if (mode === 'email') {
    requestAnimationFrame(() => {
      if (typeof EmailModule !== 'undefined') {
        EmailModule.init();
        emailInitialized = true;
      }
    });
  }
}

// Poll for #ace-editor container to appear in DOM before calling callback.
// Replaces the fragile setTimeout(50) which sometimes fires before the DOM is ready.
function _waitForEditorDOM(cb) {
  let attempts = 0;
  const maxAttempts = 40; // 40 × 50ms = 2s max wait
  function check() {
    if (document.getElementById('ace-editor')) {
      cb();
    } else if (++attempts < maxAttempts) {
      requestAnimationFrame(check);
    } else {
      console.error('Editor container #ace-editor not found after polling');
    }
  }
  // First attempt on next animation frame (after render flush)
  requestAnimationFrame(check);
}

// Dirty guard — warn before closing with unsaved editor changes
window.addEventListener('beforeunload', (e) => {
  if (appMode === 'editor' && typeof EditorModule !== 'undefined' && EditorModule.getDirty()) {
    e.preventDefault();
    e.returnValue = '';
  }
});

// --- Global state ---
let authenticated = false;
let _openaiConnected = false;
let elevatedSession = { enabled: false, remaining_seconds: 0 };
let reasoningEffort = 'high';
let showSettings = false;
let showRootWarning = false;
let showWorkLog = false;
let showNewWorkerModal = false;
let newWorkerModelKey = 'codex';
let newWorkerIdentityKey = '';
let newWorkerError = '';
let _availableWorkers = [];
let _workersDir = '';
let _updateAvailable = false;
let _updateBehindCount = 0;
let showDeleteTabModal = false;
let showCompactModal = false;
let _pendingSkillAfterCompact = null;
let showMobileWorkerMenu = false;
let deleteTabTargetId = null;
let planUsage = null;
let runtimeStarted = 0;
let _claudeBridgeUp = false; // health status for sidebar dot
let _delegatedTaskCache = { outgoing: [], outgoing_session: '', incoming: [], ts: 0 }; // delegation status cache
let _delegActivityCache = []; // real-time delegation activity from /api/delegate/activity
let _delegActivityDismissed = new Set(); // task_ids user dismissed
let _delegActivityTimer = null; // fast poll interval handle
let _delegActivityDoneTimers = {}; // task_id -> timeout for auto-removing "done" bars
let isLocalClient = false;
let lastStatusEmit = { sig: '', at: 0 };

// --- Voice input state ---
let voiceActive = false;
let voiceTranscript = '';
let speechRecognition = null;
let silenceTimer = null;
let lastTranscriptTime = 0;
const SILENCE_TIMEOUT_MS = 15000;
const MIC_IDLE_TIMEOUT_MS = 15000;
const hasSpeechAPI = (typeof webkitSpeechRecognition !== 'undefined') || (typeof SpeechRecognition !== 'undefined');
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

// --- Input button state (mic vs send) ---
let inputHasText = false;
let _restoreFocusAfterRender = false;

// Auto-scroll behavior (temporarily disabled for stability)
const AUTO_SCROLL_ENABLED = true;
let autoStickBottom = true;
let manualScrollTop = 0;
let lastScrollContentKey = '';
let restoreScrollTopOnce = null;
let streamDomInitialized = false;

// Streaming recovery loop (mobile/background resilience)
let streamRecoveryTimer = null;
let streamRecoveryBusy = false;

// --- Message Queue (type while model is busy) ---
const _messageQueue = [];  // Array of { tabId, text } — queued when tab is loading
let _lastSseEventTime = {};  // tabId → timestamp of last SSE event received
const INPUT_SAFETY_TIMEOUT_MS = 60000;  // Force-unlock after 60s of no SSE events

// --- Claude Bridge Pool — Per-Worker EventSource Connections ---
// Each Claude tab maps to a worker identity (developer, it-admin, etc.).
// Each worker gets its own bridge process and EventSource connection.
// Only ONE tab per worker identity is allowed.
const CLAUDE_API = '/api/claude';
const _claudeEvtSources = {};       // sessionId → EventSource
const _claudeEvtConnecting = {};    // sessionId → true
const _claudeIAmSending = {};       // sessionId → true while send() is consuming inline SSE
let _localCompactInProgress = false;

// --- SSE Connection TTL ---
// Keep recently-active tabs connected for 30 minutes instead of disconnecting
// immediately on tab switch. This lets background tabs receive live streaming
// events (tool progress, thinking, text deltas) so messages don't vanish.
// Cap at 4 concurrent SSE connections (browser limit is 6 for HTTP/1.1;
// 1 for global events + 1 headroom for fetch requests = 4 available).
const _SSE_TTL_MS = 30 * 60 * 1000;  // 30 minutes
const _SSE_MAX_CONNECTIONS = 4;
const _sseLastActiveAt = {};     // sessionId → timestamp (when tab was last the active tab)
let _sseTtlTimer = null;         // periodic cleanup timer

// Must match server.py / claude_bridge.py; separates prepended delegation
// notifications from the user's actual message content.
const DELEGATION_PREPEND_BOUNDARY = '[[KUKUIBOT_DELEGATION_BOUNDARY_V1]]';

function _isClaudeModel(mk) { return mk === 'claude_opus' || mk === 'claude_sonnet'; }

function _claudeWorker(tab) {
  return (tab && tab.workerIdentity) || 'developer';
}

function _claudeUrl(tab, path) {
  const sid = tab?.sessionId || '';
  return CLAUDE_API + path + (path.includes('?') ? '&' : '?') + 'session_id=' + encodeURIComponent(sid);
}

function _claudeTabBySid(sid) {
  return tabs.find(t => _isClaudeModel(t.modelKey) && t.sessionId === sid);
}

function connectClaudeEventsForTab(tab) {
  if (!tab || !_isClaudeModel(tab.modelKey)) return;
  const sid = tab.sessionId;
  if (_claudeEvtSources[sid] || _claudeEvtConnecting[sid]) return;
  _claudeEvtConnecting[sid] = true;
  const es = new EventSource(CLAUDE_API + '/events?session_id=' + encodeURIComponent(sid));
  es.onopen = () => {
    delete _claudeEvtConnecting[sid];
    _claudeEvtSources[sid] = es;
  };
  es.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      const claudeTab = _claudeTabBySid(sid);
      if (!claudeTab) return;
      // --- Delegation Wake Notification (SSE) ---
      // When the server's proactive wake system delivers a delegation status change
      // (task completed, failed, etc.), it broadcasts a "delegation_notification" SSE
      // event to the browser. This renders immediately as a system card in the chat,
      // giving visual feedback before the model generates its response.
      // This is Step 3 of the delivery pipeline in _deliver_or_queue_parent_notification.
      if (evt.type === 'delegation_notification') {
        // Dedup: skip if we already rendered this task+status via SSE or message text
        const dedupeKey = (evt.task_id || '') + ':' + (evt.status || '');
        claudeTab._seenDelegNotifs = claudeTab._seenDelegNotifs || new Set();
        if (claudeTab._seenDelegNotifs.has(dedupeKey)) return;
        claudeTab._seenDelegNotifs.add(dedupeKey);
        // Cap at 200 entries to prevent memory leaks
        if (claudeTab._seenDelegNotifs.size > 200) {
          const first = claudeTab._seenDelegNotifs.values().next().value;
          claudeTab._seenDelegNotifs.delete(first);
        }
        const statusLabel = (evt.status || 'update').replace(/_/g, ' ');
        const icon = evt.status === 'completed' ? '✅' : evt.status === 'running' ? '⚙️' : evt.status === 'failed' || evt.status === 'dispatch_failed' ? '❌' : '📋';
        claudeTab.messages.push({
          role: 'system', ts: Date.now(), text: evt.message || `Task ${evt.task_id} ${statusLabel}`,
          _card: { icon, title: `Delegation: ${statusLabel}`, stats: [evt.task_id || ''], files: [], _showBody: true }
        });
        persistTabs(); requestRender({ forceStickBottom: true });
        return;
      }
      // Skip other events while send() is consuming inline SSE
      if (_claudeIAmSending[sid]) return;
      if (!claudeTab.wasLoading) {
        if (evt.type === 'user_message' && evt.text) {
          claudeTab.messages.push({ id: evt.ts || Date.now(), role: 'user', text: evt.text, timestamp: evt.ts ? new Date(evt.ts) : new Date() });
          persistTabs(); requestRender({ forceStickBottom: true });
          return;
        }
        if (evt.type === 'context_loaded' || evt.type === 'compaction' || evt.type === 'compaction_done') {
          // Fall through
        } else if (evt.type === 'text' || evt.type === 'chunk' || evt.type === 'tool_use' || evt.type === 'thinking_start') {
          claudeTab.wasLoading = true;
          claudeTab.loadingSinceMs = Date.now();
          claudeTab.streamingText = '';
          claudeTab.thinkingText = '';
          claudeTab.thinkingExpanded = true;
          claudeTab.workLog = [];
          claudeTab.toolCallCount = 0;
          claudeTab.activeToolLabel = null; claudeTab.activeToolDetail = null;
        } else {
          return;
        }
      }
      if (evt.type === 'user_message') return;
      // --- Context Loaded (Wake/Fresh Start) ---
      // Fired by claude_bridge.py after identity files (SOUL.md, USER.md, TOOLS.md, etc.)
      // are injected into a fresh Claude subprocess. This happens on:
      //   - First message after server restart (subprocess freshly spawned)
      //   - Proactive wake of an idle session (delegation notification triggers context load)
      // Shows a system card listing which identity files were loaded.
      if (evt.type === 'context_loaded') {
        const loadedFiles = evt.loaded_files || [];
        claudeTab.messages.push({
          role: 'system', ts: Date.now(), text: 'Context loaded',
          _card: { icon: '🔄', title: 'Context Loaded', files: loadedFiles, stats: [`${loadedFiles.length} files loaded`] }
        });
        persistTabs(); requestRender({ forceStickBottom: autoStickBottom });
        return;
      }
      if (evt.type === 'compaction') {
        return;
      }
      if (evt.type === 'compaction_done') {
        if (_localCompactInProgress) return;
        const loadedFiles = evt.loaded_files || [];
        const summaryK = evt.summary_length ? Math.round(evt.summary_length / 1000) + 'k' : '?';
        const count = evt.compaction_count || '?';
        claudeTab.messages.push({
          role: 'system', ts: Date.now(), text: `Smart compact #${count} complete`,
          _card: { icon: '✅', title: 'Context Reloaded', files: loadedFiles, stats: [`Compact #${count}`, `${summaryK} chars`, `${loadedFiles.length} files loaded`] }
        });
        persistTabs(); requestRender({ forceStickBottom: autoStickBottom });
        return;
      }
      const fullTextRef = { text: claudeTab.streamingText || '' };
      handleEvent(evt, fullTextRef, claudeTab.id);
      claudeTab.streamingText = fullTextRef.text;
      if (evt.type === 'done') {
        const doneText = evt.text || '';
        const streamedText = fullTextRef.text || '';
        const finalText = doneText.length >= streamedText.length ? doneText : streamedText;
        const def = MODELS[claudeTab.modelKey] || MODELS.codex;
        finalizeTurn(claudeTab, finalText, def, { tokens: evt.tokens, runId: evt.run_id || claudeTab.currentRunId || '' });
      }
    } catch (e) { console.warn('Claude EventSource parse error:', e); }
  };
  es.onerror = () => {
    delete _claudeEvtSources[sid];
    delete _claudeEvtConnecting[sid];
    try { es.close(); } catch {}
    // Check auth before reconnecting — prevents infinite 401 reconnect loop
    fetch(API + '/auth/status', { cache: 'no-store' }).then(r => {
      if (r.status === 401 || !r.ok) { handleAuthExpired(); return; }
      return r.json().then(d => {
        if (!d.logged_in) { handleAuthExpired(); return; }
        setTimeout(() => {
          const t = _claudeTabBySid(sid);
          if (t) connectClaudeEventsForTab(t);
        }, 5000);
      });
    }).catch(() => {
      setTimeout(() => {
        const t = _claudeTabBySid(sid);
        if (t) connectClaudeEventsForTab(t);
      }, 5000);
    });
  };
}

function disconnectClaudeEventsForTab(tab) {
  if (!tab) return;
  const sid = tab.sessionId;
  const es = _claudeEvtSources[sid];
  if (es) {
    try { es.close(); } catch {}
    delete _claudeEvtSources[sid];
  }
  delete _claudeEvtConnecting[sid];
  delete _sseLastActiveAt[sid];
}

// --- SSE TTL: connection lifecycle management ---
// Count active SSE connections (Claude + Anthropic, excludes global)
function _countSseConnections() {
  let n = 0;
  for (const sid in _claudeEvtSources) if (_claudeEvtSources[sid]) n++;
  for (const sid in _claudeEvtConnecting) if (_claudeEvtConnecting[sid]) n++;
  for (const sid in _anthropicEvtSources) if (_anthropicEvtSources[sid]) n++;
  for (const sid in _anthropicEvtConnecting) if (_anthropicEvtConnecting[sid]) n++;
  return n;
}

// Evict the oldest inactive SSE connection to make room for a new one.
// Never evicts the active tab or tabs that are currently loading.
function _evictOldestSse() {
  let oldest = null;
  let oldestTime = Infinity;
  for (const sid in _sseLastActiveAt) {
    const tab = tabs.find(t => t.sessionId === sid);
    if (!tab) { // orphan — disconnect immediately
      if (_claudeEvtSources[sid]) { try { _claudeEvtSources[sid].close(); } catch {} delete _claudeEvtSources[sid]; }
      if (_anthropicEvtSources[sid]) { try { _anthropicEvtSources[sid].close(); } catch {} delete _anthropicEvtSources[sid]; }
      delete _sseLastActiveAt[sid];
      return true;
    }
    if (tab.id === activeTabId) continue;     // never evict active tab
    if (tab.wasLoading) continue;              // never evict loading tab
    if (_sseLastActiveAt[sid] < oldestTime) {
      oldestTime = _sseLastActiveAt[sid];
      oldest = tab;
    }
  }
  if (oldest) {
    if (_isClaudeModel(oldest.modelKey)) disconnectClaudeEventsForTab(oldest);
    if (_isAnthropicModel(oldest.modelKey)) disconnectAnthropicEventsForTab(oldest);
    return true;
  }
  return false;
}

// Periodic cleanup: disconnect tabs that exceeded TTL
function _sseTtlCleanup() {
  const now = Date.now();
  for (const sid in _sseLastActiveAt) {
    if (now - _sseLastActiveAt[sid] > _SSE_TTL_MS) {
      const tab = tabs.find(t => t.sessionId === sid);
      if (!tab) { delete _sseLastActiveAt[sid]; continue; }
      if (tab.id === activeTabId) continue;     // active tab stays
      if (tab.wasLoading) continue;              // loading tab stays
      if (_isClaudeModel(tab.modelKey)) disconnectClaudeEventsForTab(tab);
      if (_isAnthropicModel(tab.modelKey)) disconnectAnthropicEventsForTab(tab);
    }
  }
}

function _startSseTtlTimer() {
  if (_sseTtlTimer) return;
  _sseTtlTimer = setInterval(_sseTtlCleanup, 60000); // check every minute
}

// Ensure the given tab can get an SSE connection, evicting if at cap
function _ensureSseSlot(tab) {
  if (!tab) return;
  const sid = tab.sessionId;
  // Already connected — just update timestamp
  if (_claudeEvtSources[sid] || _claudeEvtConnecting[sid] ||
      _anthropicEvtSources[sid] || _anthropicEvtConnecting[sid]) {
    _sseLastActiveAt[sid] = Date.now();
    return;
  }
  // Need a new connection — check capacity
  while (_countSseConnections() >= _SSE_MAX_CONNECTIONS) {
    if (!_evictOldestSse()) break; // can't evict anyone — proceed anyway
  }
  _sseLastActiveAt[sid] = Date.now();
}

function connectClaudeEvents() {
  // SSE TTL model: connect the active tab + keep recently-active tabs connected
  // for up to 30 minutes. Capped at 4 concurrent connections to stay within
  // the browser's 6-connection limit (1 global SSE + 1 headroom = 4 available).
  const act = activeTab();
  if (act && _isClaudeModel(act.modelKey)) {
    _ensureSseSlot(act);
    connectClaudeEventsForTab(act);
  }
  _startSseTtlTimer();
}

// --- Anthropic Persistent EventSource Connections ---
// Mirrors the Claude EventSource pattern for Anthropic Direct tabs.
const _anthropicEvtSources = {};      // sessionId → EventSource
const _anthropicEvtConnecting = {};   // sessionId → true
const _anthropicIAmSending = {};      // sessionId → true while send() is consuming inline SSE

function _isAnthropicModel(mk) { return mk === 'anthropic'; }

function _anthropicTabBySid(sid) {
  return tabs.find(t => _isAnthropicModel(t.modelKey) && t.sessionId === sid);
}

function connectAnthropicEventsForTab(tab) {
  if (!tab || !_isAnthropicModel(tab.modelKey)) return;
  const sid = tab.sessionId;
  if (_anthropicEvtSources[sid] || _anthropicEvtConnecting[sid]) return;
  _anthropicEvtConnecting[sid] = true;
  const es = new EventSource(API + '/api/anthropic/events?session_id=' + encodeURIComponent(sid));
  es.onopen = () => {
    delete _anthropicEvtConnecting[sid];
    _anthropicEvtSources[sid] = es;
  };
  es.onmessage = (e) => {
    if (_anthropicIAmSending[sid]) return;  // inline SSE is handling this turn
    try {
      const evt = JSON.parse(e.data);
      const aTab = _anthropicTabBySid(sid);
      if (!aTab) return;

      // If not currently loading, check if this is start of a new turn
      if (!aTab.wasLoading) {
        if (evt.type === 'user_message' && evt.text) {
          aTab.messages.push({ id: evt.ts || Date.now(), role: 'user', text: evt.text, timestamp: evt.ts ? new Date(evt.ts) : new Date() });
          persistTabs(); requestRender({ forceStickBottom: true });
          return;
        }
        if (evt.type === 'text' || evt.type === 'chunk' || evt.type === 'tool_use' || evt.type === 'thinking_start') {
          aTab.wasLoading = true;
          aTab.loadingSinceMs = Date.now();
          aTab.streamingText = '';
          aTab.thinkingText = '';
          aTab.thinkingExpanded = true;
          aTab.workLog = [];
          aTab.toolCallCount = 0;
          aTab.activeToolLabel = null; aTab.activeToolDetail = null;
        } else {
          return;
        }
      }
      if (evt.type === 'user_message') return;

      const fullTextRef = { text: aTab.streamingText || '' };
      handleEvent(evt, fullTextRef, aTab.id);
      aTab.streamingText = fullTextRef.text;
      if (evt.type === 'done') {
        const doneText = evt.text || '';
        const streamedText = fullTextRef.text || '';
        const finalText = doneText.length >= streamedText.length ? doneText : streamedText;
        const def = MODELS[aTab.modelKey] || MODELS.anthropic;
        finalizeTurn(aTab, finalText, def, { tokens: evt.tokens, runId: evt.run_id || aTab.currentRunId || '' });
      }
    } catch (err) { console.warn('Anthropic EventSource parse error:', err); }
  };
  es.onerror = () => {
    delete _anthropicEvtSources[sid];
    delete _anthropicEvtConnecting[sid];
    try { es.close(); } catch {}
    // Check auth before reconnecting — prevents infinite 401 reconnect loop
    fetch(API + '/auth/status', { cache: 'no-store' }).then(r => {
      if (r.status === 401 || !r.ok) { handleAuthExpired(); return; }
      return r.json().then(d => {
        if (!d.logged_in) { handleAuthExpired(); return; }
        setTimeout(() => {
          const t = _anthropicTabBySid(sid);
          if (t) connectAnthropicEventsForTab(t);
        }, 5000);
      });
    }).catch(() => {
      setTimeout(() => {
        const t = _anthropicTabBySid(sid);
        if (t) connectAnthropicEventsForTab(t);
      }, 5000);
    });
  };
}

function disconnectAnthropicEventsForTab(tab) {
  if (!tab) return;
  const sid = tab.sessionId;
  const es = _anthropicEvtSources[sid];
  if (es) {
    try { es.close(); } catch {}
    delete _anthropicEvtSources[sid];
  }
  delete _anthropicEvtConnecting[sid];
  delete _sseLastActiveAt[sid];
}

function connectAnthropicEvents() {
  // SSE TTL model: same as connectClaudeEvents — keep recently-active connected.
  const act = activeTab();
  if (act && _isAnthropicModel(act.modelKey)) {
    _ensureSseSlot(act);
    connectAnthropicEventsForTab(act);
  }
  _startSseTtlTimer();
}

// --- Global Broadcast SSE (Cross-Device Sync) ---
// One connection per browser — receives tab changes, new chat messages, etc.
// so all devices stay in sync with server state.
let _globalEvtSource = null;
let _globalEvtReconnectTimer = null;
let _globalTabsSyncDebounce = null;

function connectGlobalEvents() {
  if (_globalEvtSource) return;
  const es = new EventSource(API + '/api/events/global');
  es.onopen = () => { _globalEvtSource = es; };
  es.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      if (evt.type === 'chatlog_append') {
        // A new message was persisted to chatlog for a session — append if we have that tab loaded
        const tab = tabs.find(t => t.sessionId === evt.session_id);
        if (tab && tab.messages) {
          // Dedup: skip if we already have this message (by text + role + close timestamp)
          const isDupe = tab.messages.some(m =>
            m.role === evt.role && m.text === evt.text && Math.abs((m.timestamp?.getTime?.() || 0) - evt.ts) < 3000
          );
          if (!isDupe) {
            tab.messages.push({
              id: evt.ts || Date.now(),
              role: evt.role,
              text: evt.text,
              timestamp: new Date(evt.ts || Date.now()),
              modelLabel: evt.role === 'assistant' ? (MODELS[tab.modelKey]?.shortName || 'Assistant') : undefined,
            });
            if (tab.id !== activeTabId) { tab.unread = true; }
            persistTabs();
            requestRender({ preserveScroll: tab.id !== activeTabId });
          }
        }
      } else if (evt.type === 'tabs_updated') {
        // Another device synced tabs — debounce a re-fetch from server
        if (_globalTabsSyncDebounce) clearTimeout(_globalTabsSyncDebounce);
        _globalTabsSyncDebounce = setTimeout(async () => {
          const changed = await syncTabsFromServerSessions(80, { forceServerLabels: true, serverAuthoritative: true });
          if (changed) requestRender({ preserveScroll: true });
        }, 1000);
      }
    } catch (err) { console.warn('Global SSE parse error:', err); }
  };
  es.onerror = () => {
    _globalEvtSource = null;
    try { es.close(); } catch {}
    if (_globalEvtReconnectTimer) clearTimeout(_globalEvtReconnectTimer);
    _globalEvtReconnectTimer = setTimeout(connectGlobalEvents, 5000);
  };
}

function disconnectGlobalEvents() {
  if (_globalEvtSource) {
    try { _globalEvtSource.close(); } catch {}
    _globalEvtSource = null;
  }
  if (_globalEvtReconnectTimer) {
    clearTimeout(_globalEvtReconnectTimer);
    _globalEvtReconnectTimer = null;
  }
}

// --- Connection status indicator for work log toolbar ---
// Returns 'connected' | 'connecting' | 'disconnected' for the given tab
function getTabConnectionStatus(tab) {
  if (!tab) return 'disconnected';
  const mk = tab.modelKey;
  const sid = tab.sessionId;
  if (_isClaudeModel(mk)) {
    if (_claudeEvtSources[sid]) return 'connected';
    if (_claudeEvtConnecting[sid]) return 'connecting';
    return 'disconnected';
  }
  if (_isAnthropicModel(mk)) {
    if (_anthropicEvtSources[sid]) return 'connected';
    if (_anthropicEvtConnecting[sid]) return 'connecting';
    return 'disconnected';
  }
  // Codex/Spark/OpenRouter don't use persistent EventSource — always "connected" (uses fetch)
  return 'connected';
}

// --- Paginated chat loading (per-tab) ---
// Each tab tracks: _chatlogTotal, _chatlogLoaded, _allLoaded
const CHATLOG_PAGE_SIZE = 10;

function supportsNoReasoningForTab(tab = activeTab()) {
  return (tab?.modelKey || '') === 'spark';
}

function enforceReasoningForActiveTab() {
  if (!supportsNoReasoningForTab() && reasoningEffort === 'none') {
    reasoningEffort = 'high';
    // Keep backend runtime config aligned with UI when switching away from Spark.
    fetch(API + '/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reasoning_effort: 'high' }),
    }).catch(() => {});
  }
}

function renderReasoningPickerButtons() {
  const allowNone = supportsNoReasoningForTab();
  return `
    ${allowNone ? `<button class="reason-none ${reasoningEffort==='none'?'active':''}" onclick="setReasoning('none')" title="No reasoning — fastest responses for Spark">⚡</button>` : ''}
    <button class="${reasoningEffort==='low'?'active':''}" onclick="setReasoning('low')" title="Low — Light reasoning for routine tasks">L</button>
    <button class="${reasoningEffort==='medium'?'active':''}" onclick="setReasoning('medium')" title="Medium — Balanced reasoning for complex tasks">M</button>
    <button class="${reasoningEffort==='high'?'active':''}" onclick="setReasoning('high')" title="High — Deep reasoning for hard problems">H</button>
  `;
}

// --- Model Definitions ---
const MODELS = {
  claude_opus: {
    name: 'Claude Code - Opus 4.6',
    shortName: 'Opus 4.6',
    dot: 'claude',
    emoji: '<img src="/claude-opus-icon.png" style="width:1em;height:1em;vertical-align:-0.1em">',
    model: 'claude-code (opus 4.6)',
    group: 'Claude Code Workers',
    menuIcon: '<img src="/claude-opus-icon.png" style="width:1em;height:1em;vertical-align:-0.1em">',
    menuLabel: 'Claude Code - Opus 4.6',
    menuHover: 'Claude Code - Opus 4.6',
  },
  claude_sonnet: {
    name: 'Claude Code - Sonnet 4.6',
    shortName: 'Sonnet 4.6',
    dot: 'claude',
    emoji: '<img src="/claude-icon.svg" style="width:1em;height:1em;vertical-align:-0.1em">',
    model: 'claude-code (sonnet 4.6)',
    group: 'Claude Code Workers',
    menuIcon: '<img src="/claude-icon.svg" style="width:1em;height:1em;vertical-align:-0.1em">',
    menuLabel: 'Claude Code - Sonnet 4.6',
    menuHover: 'Claude Code - Sonnet 4.6',
  },
  codex: {
    name: 'GPT Codex',
    shortName: 'Codex',
    dot: 'codex',
    emoji: '<img src="https://static.vecteezy.com/system/resources/previews/022/227/364/non_2x/openai-chatgpt-logo-icon-free-png.png" style="width:1em;height:1em;vertical-align:-0.1em">',
    model: 'gpt-5.3-codex',
    group: 'GPT Codex Workers',
    menuIcon: '<img src="https://static.vecteezy.com/system/resources/previews/022/227/364/non_2x/openai-chatgpt-logo-icon-free-png.png" style="width:1em;height:1em;vertical-align:-0.1em">',
    menuLabel: 'GPT Codex',
    menuHover: 'gpt-5.3-codex',
  },
  spark: {
    name: 'Spark',
    shortName: 'Spark',
    dot: 'spark',
    emoji: '⚡',
    model: 'spark',
    group: 'Spark Workers',
    menuIcon: '⚡',
    menuLabel: 'Spark',
    menuHover: 'spark',
  },
};

// Dynamically populated from /api/openrouter/config at startup.
// Each entry maps a key like "openrouter_gemini_3_1_pro_preview" to a MODELS-compatible definition.
let _orModelsLoaded = false;
async function _loadOpenRouterModels(force) {
  if (_orModelsLoaded && !force) return;
  try {
    const r = await fetch(API + '/api/openrouter/config', { headers: { 'Accept': 'application/json' } });
    if (!r.ok) return;
    const d = await r.json();
    if (!d.has_api_key) { _orModelsLoaded = false; return; }
    const models = d.models || [];
    for (const m of models) {
      // Build a safe key from model ID: "google/gemini-3.1-pro-preview" → "openrouter_gemini_3_1_pro_preview"
      const safeKey = 'openrouter_' + m.id.replace(/[^a-zA-Z0-9]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');
      if (MODELS[safeKey]) continue; // don't overwrite if already exists
      const reasoning = m.reasoning ? ` · reasoning: ${m.reasoning}` : '';
      
      // Detect provider-specific models and use custom icons
      const isAnthropic = m.id.toLowerCase().includes('anthropic') || m.id.toLowerCase().includes('claude');
      const isGoogle = m.id.toLowerCase().includes('google') || m.id.toLowerCase().includes('gemini');
      const icon = isAnthropic
        ? '<img src="/claude-icon.svg" style="width:1em;height:1em;vertical-align:-0.1em">'
        : isGoogle
        ? '<img src="https://brandlogos.net/wp-content/uploads/2025/03/gemini_icon-logo_brandlogos.net_aacx5.png" style="width:1em;height:1em;vertical-align:-0.1em">'
        : '🌐';
      
      MODELS[safeKey] = {
        name: `${m.label} (OpenRouter)`,
        shortName: m.label,
        dot: 'openrouter',
        emoji: icon,
        model: 'openrouter',
        group: 'OpenRouter Workers',
        menuIcon: icon,
        menuLabel: m.label,
        menuHover: `OpenRouter · ${m.id}${reasoning}`,
        openrouterModel: m.id,
      };
    }
    _orModelsLoaded = true;
  } catch (e) {
    console.warn('Failed to load OpenRouter models:', e);
  }
}

// Dynamically populated from /api/anthropic/config at startup.
let _anthropicModelsLoaded = false;
let _anthropicApiConnected = false; // true only if a dedicated API key is configured
async function _loadAnthropicModels(force) {
  if (_anthropicModelsLoaded && !force) return;
  try {
    const r = await fetch(API + '/api/anthropic/config', { headers: { 'Accept': 'application/json' } });
    if (!r.ok) { _anthropicApiConnected = false; return; }
    const d = await r.json();
    _anthropicApiConnected = !!d.has_dedicated_key;
    if (!d.has_api_key) return; // Don't show if no key configured
    const models = d.models || [];
    const anthropicIcon = '<img src="/claude-icon.svg" style="width:1em;height:1em;vertical-align:-0.1em">';
    for (const m of models) {
      // Build a safe key: "claude-sonnet-4-6" → "anthropic_sonnet_4_6"
      const safeKey = 'anthropic_' + m.id.replace(/^claude-/, '').replace(/[^a-zA-Z0-9]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');
      if (MODELS[safeKey]) continue;
      const ctx = (m.context_window / 1000) + 'K';
      MODELS[safeKey] = {
        name: `${m.label} (API)`,
        shortName: m.label,
        dot: 'anthropic',
        emoji: anthropicIcon,
        model: 'anthropic',
        group: 'Anthropic API',
        menuIcon: anthropicIcon,
        menuLabel: m.label,
        menuHover: `Anthropic API \u00b7 ${m.id} \u00b7 ${ctx} context`,
      };
    }
    _anthropicModelsLoaded = true;
  } catch (e) {
    console.warn('Failed to load Anthropic models:', e);
  }
}

// --- Helpers ---
const fmtTime = ts => new Date(ts || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const fmtM = n => n == null ? '' : (n / 1000000).toFixed(1) + 'M';

function fmtBytes(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v) || v < 0) return 'n/a';
  if (v === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let x = v;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  return `${x >= 10 ? x.toFixed(0) : x.toFixed(1)} ${units[i]}`;
}

function serviceDot(state) {
  if (state === 'ok') return '🟢';
  if (state === 'warn') return '🟡';
  if (state === 'unknown') return '🟡';
  return '🔴';
}

async function runSlashStatus() {
  const tab = activeTab();
  if (!tab) return;

  // Immediate UX feedback: inject the trigger phrase into chat right away.
  tab.messages.push({ id: Date.now(), role: 'user', text: 'Health Update', timestamp: new Date() });
  tab.workLog = []; tab.toolCallCount = 0;
  tab.activeToolLabel = 'Health checks'; tab.activeToolDetail = null;
  tab.wasLoading = true;
  tab.loadingSinceMs = Date.now();
  streamTabId = tab.id;
  showWorkLog = true;
  addWork(tab.id, { type: 'status', text: 'Running health checks…' });
  persistTabs();
  requestRender({ forceStickBottom: true });

  await refreshMeta();

  let sys = null;
  let guard = null;
  let auth = null;
  let backup = null;
  let secquick = null;
  addWork(tab.id, { type: 'status', text: 'Checking host stats and services…' });
  try {
    const [sysRes, guardRes, authRes, backupRes, secRes] = await Promise.all([
      fetch(API + '/api/system-stats').catch(() => null),
      fetch(API + '/api/content-guard/health').catch(() => null),
      fetch(API + '/auth/status').catch(() => null),
      fetch(API + '/api/backup/status').catch(() => null),
      fetch(API + '/api/security-quick-check').catch(() => null),
    ]);
    if (sysRes && sysRes.ok) sys = await sysRes.json();
    if (guardRes && guardRes.ok) guard = await guardRes.json();
    if (authRes && authRes.ok) auth = await authRes.json();
    if (backupRes && backupRes.ok) backup = await backupRes.json();
    if (secRes && secRes.ok) secquick = await secRes.json();
  } catch {
    addWork(tab.id, { type: 'status', text: 'Some checks failed; using best-effort data…' });
  }

  // Fallback: if endpoint blocked by auth/session edge cases, gather minimal local stats
  // from current tab/runtime metadata so report still has useful server-ish info.
  if (!sys) {
    sys = {
      hostname: (location && location.hostname) ? location.hostname : 'server',
      platform: (navigator && navigator.platform) ? navigator.platform : 'n/a',
    };
  }

  const d = tab.tokenDebug || {};
  const ctx = d.effective_tokens != null
    ? `${Math.round((d.effective_tokens || 0) / 1000)}K / ${Math.round((d.context_window || 0) / 1000)}K`
    : 'n/a';
  const modelName = MODELS[tab.modelKey]?.name || tab.model || 'Unknown';
  const reasoning = String(reasoningEffort || 'medium').toUpperCase();

  const cpu = (sys?.cpu && Number.isFinite(Number(sys.cpu.used_percent)))
    ? `${sys.cpu.used_percent}% · load ${sys.cpu.load1} · ${sys.cpu.cores} cores`
    : 'n/a';
  const mem = (sys?.memory && Number.isFinite(Number(sys.memory.total_bytes)) && Number(sys.memory.total_bytes) > 0)
    ? `${sys.memory.used_percent}% · ${fmtBytes(sys.memory.used_bytes)} / ${fmtBytes(sys.memory.total_bytes)}`
    : 'n/a';
  const disk = (sys?.disk && Number.isFinite(Number(sys.disk.total_bytes)) && Number(sys.disk.total_bytes) > 0)
    ? `${sys.disk.used_percent}% · ${fmtBytes(sys.disk.used_bytes)} / ${fmtBytes(sys.disk.total_bytes)}`
    : 'n/a';
  const host = sys?.hostname || 'server';
  const platformStr = sys?.platform || 'n/a';

  const stage1State = guard?.stage1?.deberta === 'ok' ? 'ok' : (guard?.stage1?.deberta === 'unavailable' ? 'warn' : 'down');
  const stage2State = guard?.stage2_spark?.available ? 'ok' : 'warn';
  const toolState = 'ok'; // core tools are built-in on backend
  const searchState = guard?.stage2_spark?.available ? 'ok' : 'warn';

  const sudoState = secquick?.checks?.sudo?.state || 'warn';
  const fwState = secquick?.checks?.firewall?.state || 'warn';
  const sshState = secquick?.checks?.remote_login?.state || 'warn';
  const sudoersModeState = secquick?.checks?.sudoers_include_mode?.state || 'warn';
  const filevaultState = secquick?.checks?.filevault?.state || 'warn';
  const sipState = secquick?.checks?.sip?.state || 'warn';
  const secFindings = Array.isArray(secquick?.findings) ? secquick.findings : [];
  const critCount = secFindings.filter(f => String(f?.severity) === 'critical').length;
  const warnCount = secFindings.filter(f => String(f?.severity) === 'warn').length;

  const oaiConnected = Boolean(auth?.openai_connected);
  const oaiProvider = auth?.provider_type || 'none';
  const oaiState = oaiConnected ? 'ok' : 'down';

  const githubConfigured = Boolean(backup?.configured);
  const githubState = githubConfigured ? 'ok' : 'warn';
  const githubRepo = backup?.repo_url || 'not configured';

  const sshDetail = secquick?.checks?.remote_login?.detail || 'unavailable; may require sudo password';
  const hideSshLine = /check requires sudo password/i.test(String(sshDetail));

  const lines = [
    '## 🩺 Health Check',
    '',
    `- **Model:** ${modelName}`,
    `- **Reasoning:** ${reasoning}`,
    `- **Context Window:** ${ctx}`,
    '',
    '### 🖥️ PC',
    `- **Host:** ${host}`,
    `- **Platform:** ${platformStr}`,
    `- **CPU:** ${cpu}`,
    `- **Memory:** ${mem}`,
    `- **Disk:** ${disk}`,
    '',
    '### 🧩 Services',
    `- ${serviceDot(stage1State)} **Security Filters (Stage 1 / DeBERTa):** ${guard?.stage1?.deberta || 'n/a'}`,
    `- ${serviceDot(stage2State)} **Security Filters (Stage 2 / Spark):** ${guard?.stage2_spark?.available ? 'online' : 'offline / no token'}`,
    `- ${serviceDot(toolState)} **Core Tools:** bash · read_file · write_file · edit_file · spawn_agent`,
    `- ${serviceDot(searchState)} **Search Tool Connections:** DuckDuckGo + Spark sanitizer`,
    '',
    '### 🛡️ Quick Security Checks',
    `- ${serviceDot(sudoState === 'critical' ? 'down' : sudoState)} **Sudo posture:** ${secquick?.checks?.sudo?.detail || 'unavailable (permission/session issue)'}`,
    `- ${serviceDot(fwState)} **Firewall:** ${secquick?.checks?.firewall?.detail || 'unavailable'} _(warn-only if disabled)_`,
    ...(!hideSshLine ? [`- ${serviceDot(sshState)} **Remote Login (SSH):** ${sshDetail}`] : []),
    `- ${serviceDot(sudoersModeState)} **sudoers.d file modes:** ${secquick?.checks?.sudoers_include_mode?.detail || 'unavailable'}`,
    `- ${serviceDot(filevaultState)} **FileVault:** ${secquick?.checks?.filevault?.detail || 'unavailable'}`,
    `- ${serviceDot(sipState)} **System Integrity Protection (SIP):** ${secquick?.checks?.sip?.detail || 'unavailable'}`,
    `- **Security findings:** ${critCount} critical · ${warnCount} warnings${secquick?.overall ? ` · overall=${secquick.overall}` : ''}`,
    `${secFindings.length ? `- **Top findings:** ${secFindings.slice(0,3).map(f => `${f.title}: ${f.detail}`).join(' | ')}` : '- **Top findings:** none'}`,
    '',
    '### 🔌 Connected APIs / Models',
    `- ${serviceDot(oaiState)} **OpenAI:** ${oaiConnected ? 'connected' : 'not connected'} · provider=${oaiProvider}`,
    `- ${serviceDot(githubState)} **GitHub Backup:** ${githubConfigured ? 'configured' : 'not configured'} · ${githubRepo}`,
    `- ${serviceDot('ok')} **Active Model Profile:** ${modelName} · reasoning=${reasoning}`,
  ];

  addWork(tab.id, { type: 'status', text: 'Compiling health report…' });

  const msg = lines.join('\n');
  const sig = `${tab.id}|${msg}`;
  const now = Date.now();
  if (lastStatusEmit.sig === sig && (now - lastStatusEmit.at) < 800) return;
  lastStatusEmit = { sig, at: now };

  tab.messages.push(_sysCard(msg, '📊', 'Status Update'));
  tab.activeToolLabel = null; tab.activeToolDetail = null;
  addWork(tab.id, { type: 'status', text: 'Done' });
  tab.wasLoading = false;
  tab.loadingSinceMs = 0;
  if (streamTabId === tab.id) streamTabId = null;
  showWorkLog = false;
  persistTabs();
  requestRender({ forceStickBottom: activeTabId === tab.id });
}

function transformSearchMarkdownToHtml(text) {
  const src = text || '';
  const blockRe = /^### \[([^\]]+)\]\((https?:\/\/[^\s)]+)\)\n(https?:\/\/[^\s]+)(?:\n([\s\S]*?))?(?=\n### \[|\n*$)/gm;
  let out = '';
  let lastIdx = 0;
  let matched = false;
  let m;

  while ((m = blockRe.exec(src)) !== null) {
    matched = true;
    const [full, title, href, urlLine, snippetRaw] = m;
    out += src.slice(lastIdx, m.index);

    const rawHref = href || urlLine || '';
    let host = rawHref;
    let displayUrl = rawHref;
    try {
      const u = new URL(rawHref);
      host = (u.hostname || rawHref).toLowerCase();
      displayUrl = `${u.hostname}${u.pathname && u.pathname !== '/' ? u.pathname : ''}`;
      if (displayUrl.length > 90) displayUrl = `${displayUrl.slice(0, 87).replace(/\/+$/, '')}…`;
    } catch {}

    const safeTitle = escText(title || 'Result');
    const safeHref = escText(rawHref);
    const safeHost = escText(host);
    const safeDisplayUrl = escText(displayUrl);
    const favicon = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=32`;
    const snippet = (snippetRaw || '').trim();
    const safeSnippet = escText(snippet);

    out += `<article class="search-result"><div class="search-meta"><img class="search-favicon" src="${favicon}" alt="" loading="lazy" referrerpolicy="no-referrer" /><div class="search-site-wrap"><div class="search-site">${safeHost}</div><div class="search-url">${safeDisplayUrl}</div></div></div><a class="search-title" href="${safeHref}" target="_blank" rel="noopener noreferrer">${safeTitle}</a>${safeSnippet ? `<div class="search-snippet">${safeSnippet}</div>` : ''}</article>`;
    lastIdx = m.index + full.length;
  }

  if (!matched) return src;
  out += src.slice(lastIdx);
  return `<section class="search-results">${out}</section>`;
}

function sanitizeAndExternalizeLinks(html) {
  const clean = DOMPurify.sanitize(html || '');
  const doc = new DOMParser().parseFromString(`<div>${clean}</div>`, 'text/html');
  doc.querySelectorAll('a[href]').forEach((a) => {
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  });

  // Add copy buttons to fenced code blocks.
  doc.querySelectorAll('pre').forEach((pre) => {
    const wrap = doc.createElement('div');
    wrap.className = 'code-block-wrap';

    const btn = doc.createElement('button');
    btn.className = 'code-copy-btn';
    btn.type = 'button';
    btn.textContent = 'Copy';
    btn.setAttribute('aria-label', 'Copy code block');
    btn.setAttribute('title', 'Copy code');
    btn.setAttribute('onclick', 'copyCodeBlock(this)');

    const parent = pre.parentNode;
    if (!parent) return;
    parent.insertBefore(wrap, pre);
    wrap.appendChild(btn);
    wrap.appendChild(pre);
  });

  return doc.body.firstElementChild ? doc.body.firstElementChild.innerHTML : clean;
}

const esc = s => sanitizeAndExternalizeLinks(marked.parse(transformSearchMarkdownToHtml(s || ''), { breaks: true, gfm: true }));
const escText = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// --- Choice Button Detection ---
function detectChoiceButtons(text) {
  if (!text || typeof text !== 'string') return null;
  // Strip fenced code blocks before analysis
  const stripped = text.replace(/```[\s\S]*?```/g, '');
  let result = null;

  // --- "Option A/B/C" detection (independent of ? gate) ---
  // Matches: Option A, **Option B**, **Option B (Simplest)**, etc.
  // Must be at start of line (with optional bold markers) to avoid false positives
  // from numbered list items like "1. Option A".
  const optionRe = /(?:^|\n)\s*\*{0,2}(Option\s+[A-Z](?:\s*[\(\[][^)\]]*[\)\]])?)\*{0,2}/g;
  const optionMatches = [];
  let optMatch;
  while ((optMatch = optionRe.exec(stripped)) !== null) {
    // Extract the clean label: "Option X" plus any parenthetical
    const raw = optMatch[1].trim();
    // Dedupe by the option letter
    const letter = raw.match(/Option\s+([A-Z])/);
    if (letter && !optionMatches.some(m => m.letter === letter[1])) {
      optionMatches.push({ label: raw, value: raw, letter: letter[1] });
    }
  }
  if (optionMatches.length >= 2 && optionMatches.length <= 8) {
    result = { options: optionMatches.map(m => ({ label: m.label, value: m.value })) };
  }

  // --- "Restart Server" detection (independent of ? gate) ---
  // Only triggers if the restart mention is in the LAST sentence of the message.
  // Split on sentence boundaries (.!?\n), take the last non-empty segment.
  const _sentences = stripped.split(/(?<=[.!?\n])\s*/);
  const lastSentence = (_sentences.filter(s => s.trim()).pop() || stripped).trim();
  if (!result && (/restart\s+server/i.test(lastSentence) || /server\s+restart/i.test(lastSentence) || (/\brestart\b/i.test(lastSentence) && /\?/.test(lastSentence)))) {
    result = { options: [{ label: 'Restart Server', value: 'Restart Server' }] };
  }

  // --- Numbered list detection (only if no prior detection matched) ---
  // Suppressed if there are multiple separate numbered lists in the message.
  // Buttons show just the number (e.g. "1", "2") and send just the number.
  if (!result) {
  const numPatterns = [
    /^\s*\*\*(\d+)[\.\)]\*\*\s+(.+)$/gm,  // **1.** Option
    /^\s*(\d+)\)\s+(.+)$/gm,                // 1) Option
    /^\s*(\d+)\.\s+(.+)$/gm,                // 1. Option
  ];

  // Multi-list suppression: count separate numbered list groups
  const allLines = stripped.split('\n');
  let listGroupCount = 0;
  let inList = false;
  for (const line of allLines) {
    const isListItem = /^\s*(\*\*\d+[\.\)]\*\*\s+|\d+[\.\)]\s+)/.test(line);
    if (isListItem && !inList) {
      listGroupCount++;
      inList = true;
    } else if (!isListItem && line.trim() !== '') {
      inList = false;
    }
  }
  if (listGroupCount > 1) {
    // Multiple separate numbered lists — suppress number buttons
    // Fall through to Yes detection only
  } else {
    for (const re of numPatterns) {
      re.lastIndex = 0;
      const matches = [];
      let match;
      while ((match = re.exec(stripped)) !== null) {
        matches.push({ marker: match[1], value: match[2].trim() });
      }
      if (matches.length < 2 || matches.length > 8) continue;

      // Build options — label and value are just the number
      result = { options: matches.map(m => ({
        label: m.marker,
        value: m.marker,
      })) };
      break;
    }
  }
  } // end if (!result) numbered list guard

  // --- Final pass: append Yes button if ? found in last two sentences ---
  // This runs after all other detection, so it appends to any existing result.
  // Protect numbered markers (e.g. "1. ", "2. ") from creating false sentence boundaries.
  const sentences = stripped.replace(/\n+/g, ' ')
    .replace(/(\d+)\.\s/g, '$1\u2E31 ')
    .split(/(?<=[.!?])\s+/)
    .map(s => s.replace(/\u2E31/g, '.'))
    .filter(s => s.trim());
  const lastTwo = sentences.slice(-2);
  const endsWithQuestion = /[?？]\s*$/.test(stripped.trim());
  const hasQuestion = endsWithQuestion || lastTwo.some(s => s.includes('?'));
  if (hasQuestion) {
    if (result) {
      if (!result.options.some(o => /^yes/i.test(o.value))) {
        result.options.push({ label: 'Yes, Proceed', value: 'Yes, Proceed' });
      }
    } else {
      result = { options: [{ label: 'Yes, Proceed', value: 'Yes, Proceed' }] };
    }
  }

  return result;
}


function choiceSelect(text, tabId, msgIdx) {
  const tab = tabId ? getTab(tabId) : activeTab();
  if (tab && tab.messages && msgIdx >= 0 && tab.messages[msgIdx]) {
    delete tab.messages[msgIdx]._choiceButtons;
  }
  quickSend(text);
}

// Delegated click handler for choice buttons (avoids inline onclick quoting issues)
document.addEventListener('click', function(e) {
  const btn = e.target.closest('.choice-btn');
  if (!btn) return;
  const tabId = btn.dataset.choiceTab;
  const msgIdx = parseInt(btn.dataset.choiceMsg, 10);
  const tab = tabId ? getTab(tabId) : activeTab();
  if (!tab || !tab.messages || isNaN(msgIdx)) return;
  const msg = tab.messages[msgIdx];
  if (!msg || !msg._choiceButtons) return;
  const btnIdx = parseInt(btn.dataset.choiceIdx, 10);
  const opt = msg._choiceButtons[btnIdx];
  if (!opt) return;
  choiceSelect(opt.value, tabId, msgIdx);
});

async function copyCodeBlock(btn) {
  try {
    const wrap = btn && btn.closest ? btn.closest('.code-block-wrap') : null;
    if (!wrap) return;
    const pre = wrap.querySelector('pre');
    const code = pre ? (pre.innerText || pre.textContent || '') : '';
    if (!code) return;
    await navigator.clipboard.writeText(code);
    const original = btn.textContent;
    btn.textContent = 'Copied';
    btn.classList.add('copied');
    setTimeout(() => {
      btn.textContent = original || 'Copy';
      btn.classList.remove('copied');
    }, 1200);
  } catch {
    // no-op
  }
}

function linkifyText(s) {
  const escaped = escText(s || '');
  const urlRegex = /\b(https?:\/\/[^\s<>"]+|www\.[^\s<>"]+)/gi;
  return escaped.replace(urlRegex, (match) => {
    let core = match;
    let suffix = '';
    while (/[.,!?;:]$/.test(core)) {
      suffix = core.slice(-1) + suffix;
      core = core.slice(0, -1);
    }
    const href = /^https?:\/\//i.test(core) ? core : `https://${core}`;
    return `<a href="${href}" target="_blank" rel="noopener noreferrer">${core}</a>${suffix}`;
  });
}

function getTab(id) { return tabs.find(t => t.id === id); }
function activeTab() { return getTab(activeTabId); }
function isTabLoading(tab) { return !!tab?.wasLoading; }
function tabLoadingSince(tab) { return Number(tab?.loadingSinceMs || 0); }
function tabStreamingText(tab) { return String(tab?.streamingText || ''); }
function tabIsStreamOwner(tab) { return !!tab?.id && !!tab?.wasLoading; }
function tabElevation(tab) { return tab?.elevation || null; }
function tabApproveAll(tab) { return !!tab?.approveAll; }
function tabApproveAllVisible(tab) { return !!tab?.approveAllVisible; }

function forceRefreshApp() {
  // Force a full app refresh (not just a soft re-render).
  // Add a cache-buster and clear local tab cache so names rehydrate from server.
  try {
    localStorage.removeItem(LS_TABS_KEY);
    localStorage.removeItem(LS_ACTIVE_KEY);
  } catch {}
  const u = new URL(window.location.href);
  u.searchParams.set('refresh', String(Date.now()));
  window.location.assign(u.toString());
}

function scheduleTabsSync() {
  if (!authenticated) return;
  if (tabsSyncTimer) clearTimeout(tabsSyncTimer);
  tabsSyncTimer = setTimeout(() => {
    pushTabsToServer();
  }, 400);
}

async function pushTabsToServer() {
  if (!authenticated || tabsSyncInFlight) return;
  tabsSyncInFlight = true;
  try {
    const payload = {
      tabs: tabs.map((t, idx) => ({
        id: t.id,
        session_id: t.sessionId,
        label: t.label,
        model_key: t.modelKey,
        label_updated_at: Number(t.labelUpdatedAt || 0),
        created_explicitly: !!t.createdExplicitly,
        sort_order: idx,
        worker_identity: t.workerIdentity || '',
      })),
    };
    const res = await fetch(API + '/api/tabs/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (data?.rejected_session_ids && Array.isArray(data.rejected_session_ids) && data.rejected_session_ids.length) {
      const rejected = new Set(data.rejected_session_ids.map(String));
      const before = tabs.length;
      tabs = tabs.filter(t => !rejected.has(String(t.sessionId)));
      const after = tabs.length;
      if (before !== after) {
        // If active tab was rejected, switch to first remaining.
        if (activeTabId && !tabs.some(t => t.id === activeTabId)) {
          activeTabId = tabs[0]?.id || null;
        }
        // Show a user-visible banner (reuse status banner surface).
        const tab = activeTab();
        if (tab) {
          statusBanner = `⚠️ Max sessions limit reached. ${before - after} worker(s) were not created.`;
          statusBannerTabId = tab.id;
        }
        persistTabs();
        requestRender({ preserveScroll: true });
      }
    }
  } catch {}
  finally {
    tabsSyncInFlight = false;
  }
}

function persistTabs() {
  try {
    const payload = tabs.map(t => ({
      id: t.id,
      modelKey: t.modelKey,
      label: t.label,
      sessionId: t.sessionId,
      createdExplicitly: !!t.createdExplicitly,
      draft: t.draft || '',
      activeToolLabel: t.activeToolLabel || null,
      activeToolDetail: t.activeToolDetail || null,
      unread: !!t.unread,
      needsAttention: !!t.needsAttention,
      labelUpdatedAt: Number(t.labelUpdatedAt || 0),
      lastSeq: Number(t.lastSeq || 0),
      wasLoading: !!t.wasLoading,
      loadingSinceMs: Number(t.loadingSinceMs || 0),
      streamingText: String(t.streamingText || ''),
      elevation: t.elevation || null,
      approveAll: !!t.approveAll,
      approveAllVisible: !!t.approveAllVisible,
      tps: Number.isFinite(Number(t.tps)) ? Number(t.tps) : null,
      tpsStateChars: Number(t.tpsStateChars || 0),
      tpsStateStartTime: Number(t.tpsStateStartTime || 0),
      reconnecting: !!t.reconnecting,
      workerIdentity: t.workerIdentity || '',
      streamEpoch: Number(t.streamEpoch || 0),
      currentRunId: String(t.currentRunId || ''),
      lastFinalizedRunId: String(t.lastFinalizedRunId || ''),
    }));
    localStorage.setItem(LS_TABS_KEY, JSON.stringify(payload));
    if (activeTabId) localStorage.setItem(LS_ACTIVE_KEY, activeTabId);
  } catch {}
  scheduleTabsSync();
}

function restoreTabsFromStorage() {
  try {
    const raw = localStorage.getItem(LS_TABS_KEY);
    const saved = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(saved) || saved.length === 0) return false;

    tabs = [];
    tabCounter = 0;
    for (const st of saved) {
      if (!st || !MODELS[st.modelKey]) continue;
      createTab(st.modelKey, {
        id: st.id,
        label: st.label,
        labelUpdatedAt: Number(st.labelUpdatedAt || 0),
        sessionId: st.sessionId,
        createdExplicitly: !!st.createdExplicitly,
        draft: st.draft || '',
        activeToolLabel: st.activeToolLabel || null,
        activeToolDetail: st.activeToolDetail || null,
        unread: !!st.unread,
        needsAttention: !!st.needsAttention,
        lastSeq: Number(st.lastSeq || 0),
        wasLoading: false,       // Never persist across reloads — prevents send() from silently blocking
        loadingSinceMs: 0,
        streamingText: String(st.streamingText || ''),
        elevation: st.elevation || null,
        approveAll: !!st.approveAll,
        approveAllVisible: !!st.approveAllVisible,
        tps: Number.isFinite(Number(st.tps)) ? Number(st.tps) : null,
        tpsStateChars: Number(st.tpsStateChars || 0),
        tpsStateStartTime: Number(st.tpsStateStartTime || 0),
        reconnecting: !!st.reconnecting,
        workerIdentity: st.workerIdentity || '',
        streamEpoch: Number(st.streamEpoch || 0),
        currentRunId: String(st.currentRunId || ''),
        lastFinalizedRunId: String(st.lastFinalizedRunId || ''),
        silent: true,
      });
      const n = Number(String(st.id || '').split('-').pop());
      if (Number.isFinite(n)) tabCounter = Math.max(tabCounter, n);
    }

    if (tabs.length === 0) return false;

    const preferred = localStorage.getItem(LS_ACTIVE_KEY);
    activeTabId = tabs.some(t => t.id === preferred) ? preferred : tabs[0].id;
    return true;
  } catch {
    return false;
  }
}

function parseTabSessionId(sessionId) {
  const raw = String(sessionId || '');
  // Supports both legacy numeric ids (tab-codex-3) and stable ids (tab-codex-<token>).
  const m = raw.match(/^tab-(codex|spark)-([a-zA-Z0-9_-]+)$/);
  if (!m) return null;
  const suffix = String(m[2] || '').trim();
  const n = /^\d+$/.test(suffix) ? (Number(suffix) || 0) : 0;
  return {
    modelKey: m[1],
    id: `${m[1]}-${suffix}`,
    num: n,
  };
}

async function syncTabsFromServerSessions(limit = 60, opts = {}) {
  try {
    const forceServerLabels = !!opts.forceServerLabels;
    const serverAuthoritative = !!opts.serverAuthoritative;
    const bust = Date.now();
    const url = API + '/api/history/sessions?limit=' + Math.max(1, Math.min(limit, 100)) + '&_=' + bust;
    const res = await fetch(url, {
      cache: 'no-store',
      headers: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' },
    });
    if (!res.ok) return false;
    const data = await res.json();
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    const deletedSids = Array.isArray(data.deleted) ? data.deleted : [];

    let changed = false;

    // Remove locally-cached tabs that were deleted on another device
    if (deletedSids.length > 0) {
      const deletedSet = new Set(deletedSids);
      const before = tabs.length;
      tabs = tabs.filter(t => !deletedSet.has(t.sessionId));
      if (tabs.length < before) {
        changed = true;
        if (!tabs.some(t => t.id === activeTabId)) {
          activeTabId = tabs[0]?.id || null;
        }
      }
    }

    // Track which session IDs the server knows about for authoritative pruning
    const serverSids = new Set();

    const tabNum = (id) => {
      const m = String(id || '').match(/-(\d+)$/);
      return m ? Number(m[1]) || 0 : 0;
    };
    for (const s of sessions) {
      const sid = String(s?.session_id || '').trim();
      if (!sid) continue;

      const parsed = parseTabSessionId(sid);
      const fromMetaModel = String(s?.model_key || '').trim();
      const fromMetaId = String(s?.tab_id || '').trim();

      const modelKey = MODELS[fromMetaModel] ? fromMetaModel : (parsed?.modelKey || '');
      const tabId = fromMetaId || (parsed?.id || '');
      if (!modelKey || !tabId || !MODELS[modelKey]) continue;

      serverSids.add(sid);

      const savedLabel = String(s?.label || '').trim();
      const savedLabelUpdatedAt = Number(s?.label_updated_at || 0);
      const savedCreatedExplicitly = Boolean(s?.created_explicitly);
      const fallbackNum = tabNum(tabId) || parsed?.num || (tabs.filter(t => t.modelKey === modelKey).length + 1);
      const fallbackLabel = `${MODELS[modelKey].shortName} ${fallbackNum}`;
      const desiredLabel = savedLabel || fallbackLabel;

      const savedWorkerIdentity = String(s?.worker_identity || '').trim();

      let existing = tabs.find(t => t.sessionId === sid || t.id === tabId);
      if (!existing) {
        // Only create new tabs from sync if the session has a worker identity or was
        // explicitly created. Old orphaned sessions should not spawn phantom tabs.
        if (!savedWorkerIdentity && !savedCreatedExplicitly) continue;
        existing = createTab(modelKey, {
          id: tabId,
          sessionId: sid,
          label: desiredLabel,
          labelUpdatedAt: savedLabelUpdatedAt,
          createdExplicitly: savedCreatedExplicitly,
          workerIdentity: savedWorkerIdentity,
          silent: true,
        });
        changed = true;
      } else {
        if (existing.modelKey !== modelKey) { existing.modelKey = modelKey; existing.model = MODELS[modelKey].model; changed = true; }
        if (existing.id !== tabId) { existing.id = tabId; changed = true; }
        if (Boolean(existing.createdExplicitly) !== savedCreatedExplicitly) { existing.createdExplicitly = savedCreatedExplicitly; changed = true; }
        const localTs = Number(existing.labelUpdatedAt || 0);
        if (savedLabel && (forceServerLabels || savedLabelUpdatedAt >= localTs) && existing.label !== savedLabel) {
          existing.label = savedLabel;
          existing.labelUpdatedAt = Math.max(savedLabelUpdatedAt, localTs);
          changed = true;
        } else if (!savedLabel && !existing.label) {
          existing.label = fallbackLabel;
          changed = true;
        }
        if (savedWorkerIdentity && existing.workerIdentity !== savedWorkerIdentity) {
          existing.workerIdentity = savedWorkerIdentity;
          changed = true;
        }
      }

      const n = tabNum(tabId);
      if (Number.isFinite(n) && n > 0) tabCounter = Math.max(tabCounter, n);
    }

    // Server-authoritative mode: remove local tabs that the server doesn't know about.
    // This ensures all devices show the same tabs. Tabs that were just created locally
    // but haven't synced yet will be re-pushed by pushTabsToServer() after boot.
    if (serverAuthoritative && serverSids.size > 0) {
      const before = tabs.length;
      tabs = tabs.filter(t => serverSids.has(t.sessionId));
      if (tabs.length < before) {
        changed = true;
      }
    }

    // Apply server sort_order to reorder tabs for cross-device consistency
    const orderMap = {};
    for (const s of sessions) {
      const sid = String(s?.session_id || '').trim();
      if (sid && typeof s.sort_order === 'number') orderMap[sid] = s.sort_order;
    }
    if (Object.keys(orderMap).length > 0) {
      const hasMeaningfulOrder = Object.values(orderMap).some(v => v !== 0);
      if (hasMeaningfulOrder) {
        const before = tabs.map(t => t.id).join(',');
        tabs.sort((a, b) => {
          const oa = typeof orderMap[a.sessionId] === 'number' ? orderMap[a.sessionId] : 9999;
          const ob = typeof orderMap[b.sessionId] === 'number' ? orderMap[b.sessionId] : 9999;
          return oa - ob;
        });
        if (tabs.map(t => t.id).join(',') !== before) changed = true;
      }
    }

    if (changed && !tabs.some(t => t.id === activeTabId)) {
      activeTabId = tabs[0]?.id || null;
    }
    if (changed) persistTabs();
    return changed;
  } catch {
    return false;
  }
}


// Coalesce frequent UI updates (SSE/tool events) to avoid jank
let renderQueued = false;
let queuedRenderOpts = { forceStickBottom: false, preserveScroll: false, keepTop: null };
function requestRender(opts = {}) {
  queuedRenderOpts.forceStickBottom = queuedRenderOpts.forceStickBottom || !!opts.forceStickBottom;
  queuedRenderOpts.preserveScroll = queuedRenderOpts.preserveScroll || !!opts.preserveScroll;
  // Explicit scroll-to-bottom intent always wins over preserve
  if (queuedRenderOpts.forceStickBottom) queuedRenderOpts.preserveScroll = false;
  if (typeof opts.keepTop === 'number') queuedRenderOpts.keepTop = opts.keepTop;
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    const o = queuedRenderOpts;
    queuedRenderOpts = { forceStickBottom: false, preserveScroll: false, keepTop: null };
    // Skip background re-renders while tab rename input is active.
    // startRenameTab() injects the input via direct DOM manipulation and sets
    // editingTabRendered=true immediately, so ALL renders are blocked until editing is done.
    if (editingTabId && editingTabRendered) return;
    if (editingTabId) editingTabRendered = true;
    // Skip background re-renders while in editor mode.
    // render() does root.innerHTML=... which destroys the Ace editor instance.
    // Only allow the mode-switch render through (when _editorModeSwitch is set).
    if (appMode === 'editor' && !_editorModeSwitch) return;
    // Skip background re-renders while in email mode.
    // render() does root.innerHTML=... which destroys the email panel DOM
    // (Ace profile editor, chat scroll position, form state).
    // Only allow the mode-switch render or explicit EmailModule render through.
    if (appMode === 'email' && !_editorModeSwitch && !_emailRenderRequested) return;
    if (_emailRenderRequested) _emailRenderRequested = false;
    const inputEl = document.getElementById('input');
    const inputFocused = inputEl && document.activeElement === inputEl;
    if (inputFocused && !o.forceStickBottom && o.preserveScroll && !_editorModeSwitch) return;
    if (_editorModeSwitch) _editorModeSwitch = false;
    render(o);
  });
}

function onMessagesScroll(e) {
  const el = e?.target || document.getElementById('messages');
  if (!el) return;
  const dist = el.scrollHeight - (el.scrollTop + el.clientHeight);
  autoStickBottom = dist <= 24;
  if (!autoStickBottom) manualScrollTop = el.scrollTop;
  // Auto-load older messages when scrolled near the top
  if (el.scrollTop < 50) {
    const tab = activeTab();
    if (tab && !tab._allLoaded && !tab._loadingOlder) {
      loadOlderMessages(tab);
    }
  }
}

// --- Tab Management ---
function isNarrowScreen() {
  try { return window.matchMedia && window.matchMedia('(max-width: 820px)').matches; } catch { return window.innerWidth <= 820; }
}

function _sortManagerTabsFirst() {
  // Sort tabs by WORKER_GROUPS order. Within the same group, preserve existing order.
  const groupIndex = (identity) => {
    for (let i = 0; i < WORKER_GROUPS.length; i++) {
      if (WORKER_GROUPS[i].keys.includes(identity)) return i;
    }
    return WORKER_GROUPS.length - 1; // "Other" catch-all
  };
  tabs.sort((a, b) => groupIndex(a.workerIdentity || '') - groupIndex(b.workerIdentity || ''));
}

function createTab(modelKey, opts = {}) {
  tabCounter++;
  const explicitCreate = opts.explicit === true;
  const def = MODELS[modelKey] || MODELS.codex;
  const makeStableSuffix = () => {
    const rand = Math.random().toString(36).slice(2, 8);
    const ts = Date.now().toString(36).slice(-4);
    return `${ts}${rand}`;
  };
  const id = opts.id || `${modelKey}-${makeStableSuffix()}`;
  const sessionId = opts.sessionId || `tab-${id}`;
  const num = tabs.filter(t => t.modelKey === modelKey).length + 1;
  const tab = {
    id,
    modelKey,
    model: def.model,
    label: opts.label || `${def.shortName} ${num}`,
    labelUpdatedAt: Number(opts.labelUpdatedAt || 0),
    sessionId,
    createdExplicitly: (typeof opts.createdExplicitly === 'boolean') ? !!opts.createdExplicitly : explicitCreate,
    messages: [],
    workLog: [],
    toolCallCount: Number(opts.toolCallCount || 0),
    contextInfo: null,
    tokenDebug: null,
    draft: opts.draft || '',
    activeToolLabel: opts.activeToolLabel || null,
    activeToolDetail: opts.activeToolDetail || null,
    unread: !!opts.unread,
    needsAttention: !!opts.needsAttention,
    lastSeq: Number(opts.lastSeq || 0),
    wasLoading: !!opts.wasLoading,
    loadingSinceMs: Number(opts.loadingSinceMs || 0),
    streamingText: String(opts.streamingText || ''),
    thinkingText: '',
    thinkingExpanded: false,
    elevation: opts.elevation || null,
    approveAll: !!opts.approveAll,
    approveAllVisible: !!opts.approveAllVisible,
    tps: Number.isFinite(Number(opts.tps)) ? Number(opts.tps) : null,
    tpsStateChars: Number(opts.tpsStateChars || 0),
    tpsStateStartTime: Number(opts.tpsStateStartTime || 0),
    reconnecting: !!opts.reconnecting,
    workerIdentity: opts.workerIdentity || '',
    streamAbortController: null,
    streamEpoch: Number(opts.streamEpoch || 0),
    currentRunId: String(opts.currentRunId || ''),
    lastFinalizedRunId: String(opts.lastFinalizedRunId || ''),
  };
  tabs.push(tab);
  // Keep manager tabs at the top of the array so sidebar + drag-drop indexes stay consistent
  _sortManagerTabsFirst();
  // ⚠️ WARNING: DO NOT add connectClaudeEventsForTab() or connectAnthropicEventsForTab() here.
  // Lazy SSE: switchTab() handles connect/disconnect exclusively.
  // Adding eager connections here will exhaust the browser's 6-connection HTTP/1.1 limit
  // and freeze the UI when many worker tabs exist.
  if (!opts.silent) {
    switchTab(id);  // switchTab will connect SSE for this tab
    requestRender({ preserveScroll: true });
  }
  persistTabs();
  return tab;
}

function closeTab(id) {
  const idx = tabs.findIndex(t => t.id === id);
  if (idx === -1) return;
  const tab = tabs[idx];

  // Disconnect Claude EventSource for this tab's worker
  if (_isClaudeModel(tab.modelKey)) {
    disconnectClaudeEventsForTab(tab);
  }
  // Disconnect Anthropic EventSource for this tab
  if (_isAnthropicModel(tab.modelKey)) {
    disconnectAnthropicEventsForTab(tab);
  }

  // Server-side background cleanup: history + tab_meta + runtime + security state.
  fetch(API + '/api/tab-delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: tab.sessionId, tab_id: tab.id }),
  }).catch(() => {
    // Fallback for older servers: at least clear chat history.
    fetch(API + '/api/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: tab.sessionId }),
    }).catch(() => {});
  });

  tabs.splice(idx, 1);
  if (tabs.length === 0) {
    createTab(_defaultModel());
  } else if (activeTabId === id) {
    switchTab(tabs[Math.min(idx, tabs.length - 1)].id);
  } else {
    persistTabs();
    requestRender();
  }
}

function switchTab(id) {
  // --- Lazy SSE: disconnect previous tab's EventSource, connect new one ---
  const prevTab = activeTab();
  activeTabId = id;
  const switched = activeTab();

  // SSE TTL model: instead of disconnecting the previous tab immediately,
  // mark it with a timestamp so it stays connected for up to 30 minutes.
  // This lets background tabs receive live events (tool progress, streaming text).
  // The TTL cleanup timer and _ensureSseSlot() handle eviction when the cap is hit.
  // Tabs with wasLoading=true are never evicted (even after TTL).
  if (prevTab && prevTab.id !== id) {
    const prevSid = prevTab.sessionId;
    if (_claudeEvtSources[prevSid] || _anthropicEvtSources[prevSid]) {
      // Already connected — just stamp the TTL (don't disconnect)
      _sseLastActiveAt[prevSid] = Date.now();
    }
  }

  if (switched) {
    switched.unread = false;
    switched.needsAttention = false;
  }
  enforceReasoningForActiveTab();
  autoStickBottom = true;
  if (editingTabId && editingTabId !== id) { editingTabId = null; editingTabRendered = false; }
  persistTabs();

  if (streamDomInitialized) {
    renderMessageStreamOnly({ forcePaneRefresh: true });
    updateModelSelectorsUI();
    updateReasoningPickerUI();
    const input = document.getElementById('input');
    const tab = activeTab();
    if (input && tab) {
      input.value = tab.draft || '';
      autoResize(input);
      updateInputBtn();
    }
  } else {
    requestRender();
  }

  refreshMeta();
  const tab = activeTab();
  // Sequence SSE attach after history load to avoid race where SSE events
  // arrive before history is loaded, causing duplicate or out-of-order rendering
  const connectSSE = () => {
    if (tab) {
      _ensureSseSlot(tab);
      if (_isClaudeModel(tab.modelKey)) connectClaudeEventsForTab(tab);
      if (_isAnthropicModel(tab.modelKey)) connectAnthropicEventsForTab(tab);
    }
  };
  if (tab && tab.messages.length === 0) {
    loadTabHistory(tab, 120).then(connectSSE, connectSSE);
  } else {
    connectSSE();
  }
}

function renameTab(id, newName) {
  if (editingTabId !== id) return;  // Already completed (e.g. Enter triggers blur too)
  const tab = getTab(id);
  if (tab && newName.trim()) {
    tab.label = newName.trim();
    tab.labelUpdatedAt = Date.now();
  }
  editingTabId = null;
  editingTabRendered = false;
  persistTabs();
  // Push immediately so user-edited names propagate cross-device right away.
  pushTabsToServer();
  // Need a full render so the inline rename input is replaced by the label
  // (stream-only updates don't re-render the sidebar structure).
  requestRender({ preserveScroll: true });
}

function startRenameTab(id) {
  editingTabId = id;
  editingTabRendered = true;  // Block background renders immediately

  // Direct DOM manipulation — bypass the render cycle entirely to avoid
  // race conditions with coalesced rAFs, async refreshMeta, SSE updates, etc.
  const tabEl = document.querySelector(`.tab-item[data-tab-id="${id}"]`);
  const labelSpan = tabEl && tabEl.querySelector('.tab-label');
  if (labelSpan) {
    const tab = getTab(id);
    const inp = document.createElement('input');
    inp.className = 'tab-name-input';
    inp.id = 'tab-name-' + id;
    inp.value = tab ? tab.label : labelSpan.textContent;
    inp.onclick = function(e) { e.stopPropagation(); };
    inp.onblur = function() { renameTab(id, inp.value); };
    inp.onkeydown = function(e) {
      if (e.key === 'Enter') { renameTab(id, inp.value); e.preventDefault(); }
      if (e.key === 'Escape') { editingTabId = null; editingTabRendered = false; requestRender({ preserveScroll: true }); }
    };
    labelSpan.replaceWith(inp);
    // Also disable onclick on the tab-item so clicks don't trigger switchTab
    tabEl.removeAttribute('onclick');
    inp.focus();
    inp.select();
  } else {
    // Fallback: tab not in DOM yet (shouldn't happen), use render path
    editingTabRendered = false;
    requestRender({ preserveScroll: true });
    setTimeout(() => {
      const inp = document.getElementById('tab-name-' + id);
      if (inp) { inp.focus(); inp.select(); }
    }, 30);
  }
}

function loadAutoNamePrefs() {
  // Fast cache from localStorage, then override from server
  try {
    autoNameEnabled = localStorage.getItem(LS_AUTONAME_ENABLED_KEY) === '1';
  } catch {
    autoNameEnabled = false;
  }
  // Load server-side setting (source of truth)
  fetch(API + '/api/config', { credentials: 'include' })
    .then(r => r.ok ? r.json() : null)
    .then(cfg => {
      if (cfg && typeof cfg.auto_name_enabled === 'boolean') {
        const serverVal = cfg.auto_name_enabled;
        if (serverVal !== autoNameEnabled) {
          autoNameEnabled = serverVal;
          try { localStorage.setItem(LS_AUTONAME_ENABLED_KEY, autoNameEnabled ? '1' : '0'); } catch {}
          if (autoNameEnabled) startAutoNameScheduler(); else stopAutoNameScheduler();
          requestRender({ preserveScroll: true });
        }
      }
    })
    .catch(() => {});
}

function persistAutoNamePrefs() {
  try { localStorage.setItem(LS_AUTONAME_ENABLED_KEY, autoNameEnabled ? '1' : '0'); } catch {}
  // Persist to server DB
  fetch(API + '/api/config', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ auto_name_enabled: autoNameEnabled }),
  }).catch(() => {});
}

function startAutoNameScheduler() {
  if (autoNameTimer) return;
  autoNameTimer = setInterval(() => {
    if (!autoNameEnabled) return;
    autoNameTabs({ source: 'hourly', silent: true });
  }, AUTO_NAME_INTERVAL_MS);
}

function stopAutoNameScheduler() {
  if (!autoNameTimer) return;
  clearInterval(autoNameTimer);
  autoNameTimer = null;
}

function setAutoNameEnabled(enabled) {
  autoNameEnabled = !!enabled;
  persistAutoNamePrefs();
  if (autoNameEnabled) startAutoNameScheduler();
  else stopAutoNameScheduler();
  requestRender({ preserveScroll: true });
}

function toggleAutoNameEnabled() {
  setAutoNameEnabled(!autoNameEnabled);
  const current = activeTab();
  if (current) addWork(current.id, { type: 'status', text: `Auto-name ${autoNameEnabled ? 'enabled' : 'disabled'}` });
}

function maybeAutoNameNewTab(tab) {
  // Mark tab for quick-naming after first assistant response completes.
  // We save the user's first message text so quickNameTab can use it.
  if (!autoNameEnabled || !tab) return;
  if (tab.workerIdentity === 'dev-manager') return;
  const sid = String(tab.sessionId || '');
  if (!sid || autoNamedSessions.has(sid)) return;
  const userMsgs = (tab.messages || []).filter(m => m.role === 'user');
  if (userMsgs.length !== 1) return;
  tab._pendingQuickName = userMsgs[0].text;
}

async function quickNameTab(tab) {
  // Fire after first assistant response — names tab from user's first message.
  if (!tab || !tab._pendingQuickName) return;
  const text = tab._pendingQuickName;
  delete tab._pendingQuickName;
  const sid = String(tab.sessionId || '');
  if (autoNamedSessions.has(sid)) return;
  autoNamedSessions.add(sid);
  try {
    const res = await fetch(API + '/api/auto-name-single', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tab_id: tab.id, session_id: sid, model_key: tab.modelKey, text }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data?.ok && data.name) {
      const nm = String(data.name).trim();
      if (nm && nm !== tab.label) {
        tab.label = nm;
        tab.labelUpdatedAt = Date.now();
        persistTabs();
        requestRender({ preserveScroll: true });
      }
    }
  } catch {}
}

async function autoNameTabs(opts = {}) {
  const source = opts?.source || 'manual';
  const silent = !!opts?.silent;
  if (!tabs.length || autoNaming) return;
  if (source !== 'manual' && !autoNameEnabled) return;

  autoNaming = true;
  requestRender({ preserveScroll: true });
  const current = activeTab();
  if (!silent && current) addWork(current.id, { type: 'status', text: 'Auto-naming workers…' });
  try {
    const nameable = tabs.filter(t => t.workerIdentity !== 'dev-manager');
    const payload = {
      tabs: nameable.map(t => ({
        id: t.id,
        session_id: t.sessionId,
        label: t.label,
        model_key: t.modelKey,
        label_updated_at: Number(t.labelUpdatedAt || 0),
      })),
    };
    const res = await fetch(API + '/api/auto-name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data?.ok) {
      const msg = data?.error || `HTTP ${res.status}`;
      if (!silent && current) addWork(current.id, { type: 'status', text: `Auto-name failed: ${msg}` });
      return;
    }

    const updates = Array.isArray(data.renamed) ? data.renamed : [];
    let changed = 0;
    for (const row of updates) {
      const t = getTab(row.id);
      if (!t) continue;
      const nm = String(row.name || '').trim();
      if (!nm || t.label === nm) continue;
      t.label = nm;
      t.labelUpdatedAt = Date.now();
      changed++;
      // Fast visual feedback: repaint each rename immediately.
      persistTabs();
      requestRender({ preserveScroll: true });
      await new Promise(r => setTimeout(r, 0));
    }
    persistTabs();
    if (!silent && current) addWork(current.id, { type: 'status', text: `Auto-named ${changed} worker${changed === 1 ? '' : 's'}` });
  } catch (e) {
    if (!silent && current) addWork(current.id, { type: 'status', text: `Auto-name error: ${e.message || e}` });
  } finally {
    autoNaming = false;
    requestRender({ preserveScroll: true });
  }
}

// --- Render ---
function fmtCtx(ci) {
  const t = ci.tokens, m = ci.max, pct = ci.pct || 0;
  const fmtMax = m >= 500000 ? `${(m/1e6).toFixed(0)}M` : `${Math.round(m/1000)}K`;
  const fmtTok = t < 100000 ? `${Math.round(t/1000)}K` : `${(t/1e6).toFixed(1)}M`;
  return `${fmtTok} / ${fmtMax} (${pct}%)`;
}

function render(opts = {}) {
  const root = document.getElementById('root');
  if (!authenticated) { renderLogin(root); return; }

  // Track if textarea was focused so we can restore after innerHTML replacement
  const prevInput = document.getElementById('input');
  if (prevInput && document.activeElement === prevInput) _restoreFocusAfterRender = true;

  const tab = activeTab();
  const messages = tab ? tab.messages : [];
  const workLog = tab ? tab.workLog : [];
  const pct = tab?.contextInfo?.pct || 0;
  const ctxColor = pct > 90 ? 'var(--red)' : pct > 70 ? 'var(--yellow)' : 'var(--green)';
  const ctxStr = tab?.contextInfo ? fmtCtx(tab.contextInfo) : '';
  const activeIsStreamOwner = tabIsStreamOwner(tab);
  const tpsStr = (activeIsStreamOwner && tab?.tps != null) ? `<span style="color:var(--accent);margin-left:6px">${tab.tps} t/s</span>` : '';
  const elevMin = Math.max(0, Math.ceil((elevatedSession.remaining_seconds || 0) / 60));
  const def = tab ? MODELS[tab.modelKey] : MODELS.codex;
  const activeModelKey = tab?.modelKey || 'codex';
  const initials = userName ? userName.split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2) : '?';

  // Preserve scroll position to prevent full-window jump on streamed updates
  const prevMessagesEl = document.getElementById('messages');
  const prevScrollTop = prevMessagesEl ? prevMessagesEl.scrollTop : 0;
  const prevClientHeight = prevMessagesEl ? prevMessagesEl.clientHeight : 0;
  const prevScrollHeight = prevMessagesEl ? prevMessagesEl.scrollHeight : 0;
  const prevBottomOffset = prevMessagesEl
    ? Math.max(0, prevScrollHeight - (prevScrollTop + prevClientHeight))
    : 0;
  const wasNearBottom = prevMessagesEl
    ? (prevBottomOffset < 24)
    : true;
  const contentKey = `${messages.length}:${workLog.length}:${tabStreamingText(tab).length}:${isTabLoading(tab) ? 1 : 0}`;
  const contentChanged = contentKey !== lastScrollContentKey;

  root.innerHTML = `
    <!-- SIDEBAR -->
    <div class="sidebar">
      <div class="sidebar-header-wrap">
          <button class="sidebar-header" onclick="toggleSettings()">
            <img class="logo" src="/favicon-64.png?v=20260215-2240" alt="${APP_NAME}" />
            <span>${APP_NAME}</span>
            <span class="header-chevron">${showSettings ? '▴' : '▾'}</span>
          </button>
        </div>
      ${showSettings ? `<div class="sidebar-nav">
        <div class="sidebar-submenu-wrap">
          <button class="sidebar-nav-item" onclick="event.stopPropagation();this.nextElementSibling.classList.toggle('show')"><span class="sidebar-nav-icon">🎨</span>Theme ▸</button>
          <div class="sidebar-submenu">
            ${THEMES.map(t => {
              const active = (localStorage.getItem(LS_THEME_KEY) || 'blue') === t;
              return `<button class="sidebar-theme-item${active ? ' active-theme' : ''}" onclick="applyTheme('${t}');updateSettingsUI();">${active ? '● ' : '○ '}${THEME_LABELS[t]}</button>`;
            }).join('\n            ')}
          </div>
        </div>
        <button class="sidebar-nav-item" onclick="forceRefreshApp()"><span class="sidebar-nav-icon">&#128260;</span>Reload App</button>
        <a href="/settings-v2.html" class="sidebar-nav-item"><span class="sidebar-nav-icon">&#9881;</span>Settings</a>
        <div class="sidebar-nav-item auto-name-nav ${autoNaming ? 'busy' : ''}" title="Click to rename tabs. Toggle enables auto-naming.">
          <span class="sidebar-nav-icon">&#129668;</span>
          <button class="auto-name-run" ${autoNaming ? 'disabled' : ''} onclick="${autoNaming ? '' : 'autoNameTabs()'}">Auto Name</button>
          ${autoNaming ? '<span class="auto-name-spinner" aria-hidden="true"></span>' : ''}
          <label class="auto-name-switch" onclick="event.stopPropagation()" title="Auto-name hourly + on new tabs">
            <input type="checkbox" ${autoNameEnabled ? 'checked' : ''} onchange="toggleAutoNameEnabled()" />
            <span class="auto-name-slider"></span>
          </label>
        </div>
        <button class="sidebar-nav-item" onclick="doLogout()"><span class="sidebar-nav-icon">&#128682;</span>Logout</button>
      </div>` : ''}
      <div class="sidebar-mode-strip">
        <button class="sidebar-mode-btn${appMode === 'chat' ? ' active' : ''}" onclick="setAppMode('chat')"><span class="sidebar-nav-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><circle cx="9" cy="10" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="10" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="10" r="1" fill="currentColor" stroke="none"/></svg></span><span class="sidebar-mode-label">AI</span></button>
        <button class="sidebar-mode-btn${appMode === 'editor' ? ' active' : ''}" onclick="setAppMode('editor')"><span class="sidebar-nav-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></span><span class="sidebar-mode-label">Files</span></button>
        <button class="sidebar-mode-btn${appMode === 'email' ? ' active' : ''}" onclick="setAppMode('email')" style="position:relative"><span class="sidebar-nav-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg></span>${_emailDraftCount > 0 ? `<span class="email-draft-badge" id="email-draft-badge">${_emailDraftCount}</span>` : ''}<span class="sidebar-mode-label">Email</span></button>
      </div>
      ${appMode === 'editor' ? `
      <div class="sidebar-section editor-sidebar-section">
        ${typeof EditorModule !== 'undefined' ? EditorModule.renderFileTreeSidebar() : '<div class="ft-loading">Loading...</div>'}
      </div>
      ` : appMode === 'email' ? `
      <div class="sidebar-section email-sidebar-section">
        ${typeof EmailModule !== 'undefined' ? EmailModule.renderSidebar() : '<div class="ft-loading">Loading...</div>'}
      </div>
      ` : `
      <div class="sidebar-section">
        ${_renderGroupedTabs(tabs)}
        <button class="new-worker-btn" onclick="openNewWorkerModal()" title="Create a new worker">+ New Worker</button>
        ${_updateAvailable ? `<button class="update-available-btn" onclick="_updateAvailable=false;localStorage.setItem('kukuibot.updateAvailable','0');requestRender();window.location.href='/settings-v2.html#updates'" title="${_updateBehindCount} update${_updateBehindCount === 1 ? '' : 's'} available">&#8593; Update Available</button>` : ''}
      </div>
      ${planUsage?.ok ? `
      <div class="sidebar-usage">
        <div class="usage-label">Codex Usage</div>
        <div class="usage-split">
          <div class="usage-col">
            <div class="usage-bar-wrap">
              <div class="usage-bar-fill" style="width:${planUsage.codex?.hourlyUsedPct || 0}%;background:${(planUsage.codex?.hourlyUsedPct||0) > 80 ? 'var(--red)' : (planUsage.codex?.hourlyUsedPct||0) > 60 ? 'var(--yellow)' : 'var(--green)'}"></div>
            </div>
            <div class="usage-text">5 Hr · ${planUsage.codex?.hourlyUsedPct || 0}%</div>
          </div>
          <div class="usage-col">
            <div class="usage-bar-wrap">
              <div class="usage-bar-fill" style="width:${planUsage.codex?.weeklyUsedPct || 0}%;background:${(planUsage.codex?.weeklyUsedPct||0) > 80 ? 'var(--red)' : (planUsage.codex?.weeklyUsedPct||0) > 60 ? 'var(--yellow)' : 'var(--green)'}"></div>
            </div>
            <div class="usage-text">Week · ${planUsage.codex?.weeklyUsedPct || 0}%</div>
          </div>
        </div>

      </div>` : ''}
      `}

    </div>

    <!-- MAIN PANEL -->
    <div class="main-panel">
      <!-- Mobile top bar (hidden on desktop) -->
      <div class="mobile-bar">
        ${appMode === 'editor' ? `
        <div class="mobile-bar-left">
          <button class="mobile-menu-btn" onclick="toggleSettings()" title="Menu"><img class="mobile-logo" src="/favicon-64.png?v=20260215-2240" alt="${APP_NAME}" /></button>
          <input type="text" class="mobile-editor-filter" id="mobile-editor-filter"
            placeholder="${typeof EditorModule !== 'undefined' ? escText(EditorModule.getCurrentFileName()).replace(/"/g,'&quot;') : 'No file open'}"
            value=""
            onfocus="if(typeof EditorModule!=='undefined')EditorModule.openMobileDropdown()"
            oninput="if(typeof EditorModule!=='undefined')EditorModule.onMobileDropdownFilter(this.value)"
          />
          <button id="mobile-editor-save" class="editor-btn mobile-editor-save" onclick="if(typeof EditorModule!=='undefined')EditorModule.save()" disabled title="Save">Save</button>
        </div>
        ` : appMode === 'email' ? `
        <div class="mobile-bar-left">
          <button class="mobile-menu-btn" onclick="toggleSettings()" title="Menu"><img class="mobile-logo" src="/favicon-64.png?v=20260215-2240" alt="${APP_NAME}" /></button>
          <span class="mobile-email-title">Email Drafter</span>
        </div>
        ` : `
        <div class="mobile-bar-left">
          <button class="mobile-menu-btn" onclick="toggleSettings()" title="Menu"><img class="mobile-logo" src="/favicon-64.png?v=20260215-2240" alt="${APP_NAME}" /></button>
          <div class="mobile-worker-picker model-${activeModelKey}" onclick="toggleMobileWorkerMenu(event)">
            <span class="mwp-icon">${_getWorkerIcon(tab)}</span>
            <span class="mwp-model-logo">${def.emoji || ''}</span>
            <span class="mwp-label">${escText(tab?.label || def.name)}</span>
            <span class="mwp-chevron">▾</span>
          </div>
          <button class="mobile-add-btn" onclick="openNewWorkerModal()" title="New Worker">+</button>
        </div>
        `}
        <div id="mobile-worker-menu-host">${showMobileWorkerMenu ? renderMobileWorkerMenu() : ''}</div>
        ${showSettings ? `<div class="mobile-settings-nav">
          <div class="sidebar-submenu-wrap">
            <button class="sidebar-nav-item" onclick="event.stopPropagation();this.nextElementSibling.classList.toggle('show')"><span class="sidebar-nav-icon">🎨</span>Theme ▸</button>
            <div class="sidebar-submenu">
              ${THEMES.map(t => {
                const active = (localStorage.getItem(LS_THEME_KEY) || 'blue') === t;
                return `<button class="sidebar-theme-item${active ? ' active-theme' : ''}" onclick="applyTheme('${t}');updateSettingsUI();">${active ? '● ' : '○ '}${THEME_LABELS[t]}</button>`;
              }).join('\n              ')}
            </div>
          </div>
          <button class="sidebar-nav-item" onclick="forceRefreshApp()"><span class="sidebar-nav-icon">&#128260;</span>Reload App</button>
          <a href="/settings-v2.html" class="sidebar-nav-item"><span class="sidebar-nav-icon">&#9881;</span>Settings</a>
            <div class="sidebar-nav-item auto-name-nav ${autoNaming ? 'busy' : ''}">
            <span class="sidebar-nav-icon">&#129668;</span>
            <button class="auto-name-run" ${autoNaming ? 'disabled' : ''} onclick="${autoNaming ? '' : 'autoNameTabs()'}">Auto Name</button>
            ${autoNaming ? '<span class="auto-name-spinner" aria-hidden="true"></span>' : ''}
            <label class="auto-name-switch" onclick="event.stopPropagation()">
              <input type="checkbox" ${autoNameEnabled ? 'checked' : ''} onchange="toggleAutoNameEnabled()" />
              <span class="auto-name-slider"></span>
            </label>
          </div>
          <button class="sidebar-nav-item" onclick="doLogout()"><span class="sidebar-nav-icon">&#128682;</span>Logout</button>
        </div>` : ''}
      </div>

      ${appMode === 'editor' ? `
      <div class="editor-panel">
        ${typeof EditorModule !== 'undefined' ? EditorModule.renderEditorPanel() : '<div class="editor-loading">Loading editor...</div>'}
      </div>
      ` : appMode === 'email' ? `
      <div class="email-panel">
        ${typeof EmailModule !== 'undefined' ? EmailModule.renderPanel() : '<div class="email-loading">Loading email module...</div>'}
      </div>
      ` : `
      <div class="messages" id="messages" onscroll="onMessagesScroll(event)">${renderMessagesInner(tab, def)}</div>

      <div class="bottom-chrome">
      <div id="deleg-bars-host">${renderDelegationBars()}</div>
      <div id="work-log-host">${renderWorkLogFixed(tab)}</div>

      ${tabElevation(tab) ? renderElevation(tab) : ''}

      <div class="status-bar">
        <span>
          Context:${ctxStr ? ` <span style="color:${ctxColor}">${ctxStr}</span>` : ' --'}${tpsStr}
        </span>
        <div class="controls">
          <button class="btn-sm ${elevatedSession.enabled ? 'root-active' : 'inactive'}" onclick="promptRoot()" title="${elevatedSession.enabled ? `Root active — ${elevMin}m remaining. Click to revoke.` : 'Enable 30-min privileged root mode'}">${elevatedSession.enabled ? `🔓 Root ${elevMin}m` : '🔐 Root'}</button>
          ${tabApproveAllVisible(tab) ? `<button class="btn-sm ${tabApproveAll(tab) ? 'active' : 'inactive'}" onclick="toggleApproveAll()" title="${tabApproveAll(tab) ? 'Auto Allow is ON — click to turn off' : 'Auto Allow is OFF — click to turn on'}">⚡ Auto Allow${tabApproveAll(tab) ? ' ✓' : ''}</button>` : ''}
          ${_isClaudeModel(tab.modelKey) ? '' : `<div class="reasoning-wrap">
            <span class="reasoning-label">Reasoning:</span>
            <div class="reasoning-picker" id="reasoning-picker-host">${renderReasoningPickerButtons()}</div>
          </div>`}
          <span class="worker-name-badge" id="worker-name-badge" onclick="_toggleSkillsPopup(event)">${_getWorkerDisplayName(tab)}</span>
        </div>
      </div>

      <div class="input-area">
        <div class="drag-overlay"><div class="drag-overlay-label"><span class="drag-icon">📂</span>Drop files here</div></div>
        ${renderAttachmentBar(tab)}
        <input type="file" id="file-picker" multiple accept="image/*,.txt,.md,.py,.js,.ts,.json,.csv,.html,.css,.pdf,.log,.xml,.yaml,.yml,.sh,.toml,.env,.sql,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp" style="display:none" onchange="if(this.files.length){_processFiles(this.files);this.value=''}">
        <div class="input-row" style="position:relative;">
          <div class="textarea-wrap">
            <textarea id="input" rows="2" autocomplete="off" placeholder="Message ${tab ? escText(tab.label) : def.name}..." onkeydown="handleKey(event)" oninput="onInputChange(this);autoResize(this);updateInputBtn()"></textarea>
            <button type="button" class="attach-btn" onclick="document.getElementById('file-picker').click()" title="Attach file" aria-label="Attach file">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            </button>
          </div>
          <button id="action-btn" type="button" class="send-btn" onclick="handleActionBtn()" title="Send or dictate" aria-label="Start voice input">
            <svg class="icon-send" fill="currentColor" viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            <svg class="icon-mic" fill="currentColor" viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm-1-9c0-.55.45-1 1-1s1 .45 1 1v6c0 .55-.45 1-1 1s-1-.45-1-1V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>
            ${_queueCountForTab(tab) > 0 ? `<span class="queue-badge">${_queueCountForTab(tab)}</span>` : ''}
          </button>
        </div>
        ${_queueCountForTab(tab) > 0 ? `<div class="queue-hint">${_queueCountForTab(tab)} message${_queueCountForTab(tab) > 1 ? 's' : ''} queued — will send when model is ready</div>` : ''}
      </div>
      </div><!-- .bottom-chrome -->
      `}
    </div>

    ${showRootWarning ? renderRootWarning() : ''}
    ${showNewWorkerModal ? renderNewWorkerModal() : ''}
    ${showDeleteTabModal ? renderDeleteTabModal() : ''}
    ${showPasswordModal ? renderPasswordModal() : ''}
    ${showBackupModal ? renderBackupModal() : ''}
    ${showRestartModal ? renderRestartModal() : ''}
    ${showCompactModal ? renderCompactModal() : ''}
  `;

  const el = document.getElementById('messages');
  if (el) {
    streamDomInitialized = true;
    if (typeof opts.keepTop === 'number') {
      // Hard anchor requested by caller (used for UI-only toggles like settings).
      el.scrollTop = Math.max(0, opts.keepTop);
      manualScrollTop = el.scrollTop;
    } else {
      // Follow newest lines unless user intentionally scrolled up.
      const shouldStick = AUTO_SCROLL_ENABLED && (!opts.preserveScroll) && (opts.forceStickBottom || autoStickBottom || (!prevMessagesEl && wasNearBottom));
      if (shouldStick) {
        el.scrollTop = el.scrollHeight;
        autoStickBottom = true;
      } else {
        // Keep exact viewport top when user is not pinned to bottom.
        // This avoids jumps to top when content height changes (e.g. stream -> final message).
        const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
        const nextTop = Math.max(0, Math.min(prevScrollTop, maxTop));
        el.scrollTop = nextTop;
        manualScrollTop = nextTop;
      }
    }
  }
  lastScrollContentKey = contentKey;

  // Restore draft text and re-apply composer height after full render.
  // Draft is set via JS (not innerHTML) to prevent stale text from reappearing
  // when background renders fire after send() has already cleared the draft.
  const inputEl = document.getElementById('input');
  if (inputEl) {
    const draftTab = activeTab();
    inputEl.value = draftTab?.draft || '';
    autoResize(inputEl);
    // Restore focus if textarea was focused before render (prevents mobile keyboard collapse)
    // Skip on iOS during voice input — focusing textarea triggers keyboard popup
    if (_restoreFocusAfterRender && !(isIOS && voiceActive)) {
      inputEl.focus();
      // Restore cursor to end of text
      const len = inputEl.value.length;
      inputEl.setSelectionRange(len, len);
    }
  }
  _restoreFocusAfterRender = false;

  // Re-attach file paste/drop listeners and re-render attachment bar after DOM rebuild
  attachFileListeners();

  // Update mic/send button state after render
  setTimeout(() => updateInputBtn(), 0);
}

function renderSettingsMenu(modelKey = (activeTab()?.modelKey || 'codex')) {
  return `<div class="settings-menu model-${modelKey}">
    <div class="menu-section">
      <button class="menu-item" onclick="runSlashStatus();requestRender({ preserveScroll: true })">🩺 Health Update</button>
      <button class="menu-item" onclick="showCompactConfirm()">🗜 Compact Now</button>
      <button class="menu-item danger" onclick="promptDeleteCurrentTab()">🧨 Delete Current Tab</button>
    </div>
    <div class="menu-section">
      <div class="theme-submenu-wrap">
        <button class="menu-item has-submenu" onclick="event.stopPropagation();this.nextElementSibling.classList.toggle('show')">🎨 Theme ▸</button>
        <div class="theme-submenu">
          ${THEMES.map(t => {
            const active = (localStorage.getItem(LS_THEME_KEY) || 'blue') === t;
            return `<button class="menu-item${active ? ' active-theme' : ''}" onclick="applyTheme('${t}');updateSettingsUI();">${active ? '● ' : '○ '}${THEME_LABELS[t]}</button>`;
          }).join('\n          ')}
        </div>
      </div>
      <button class="menu-item" onclick="window.location.href='/settings-v2.html'">⚙️ Settings</button>
      <button class="menu-item" onclick="checkForUpdates()">📦 Check for Updates</button>
      <button class="menu-item" onclick="forceRefreshApp()">🔄 Reload App</button>
    </div>
    <div class="menu-section">
      <button class="menu-item logout" onclick="doLogout()">🚪 Logout</button>
    </div>
  </div>`;
}

function renderRootWarning() {
  return `<div class="root-warning-overlay" onclick="if(event.target===this){showRootWarning=false;requestRender({ preserveScroll: true });}">
    <div class="root-warning">
      <h3>⚠️ Enable Root Mode (30 min)</h3>
      <p>This grants passwordless sudo access for 30 minutes by adding a temporary sudoers rule. The rule is automatically removed when the timer expires or you revoke it.</p>
      <p><strong style="color:var(--red)">Only enable this if you trust the current task.</strong></p>
      <div class="warn-actions">
        <button class="btn-cancel" onclick="showRootWarning=false;requestRender({ preserveScroll: true })">Cancel</button>
        <button class="btn-confirm" onclick="confirmRoot()">Enable Root Mode</button>
      </div>
    </div>
  </div>`;
}

// Worker-type icons for sidebar tabs
const WORKER_ICONS = {
  'developer':    '&lt;/&gt;',
  'dev-manager':  '🤵',
  'code-analyst': '📐',
  'it-admin':     '🛡️',
  'seo':          '🚀',
  'assistant':    '📝',
  'planner':      '📋',
};
const DEFAULT_WORKER_ICON = '🤖';

// Sidebar group definitions — tabs are grouped and ordered by these categories.
// Within each group, tab order is the delegation priority (top = preferred).
const WORKER_GROUPS = [
  { label: '',               keys: ['dev-manager', 'planner'] },  // top-level, no header
  { label: 'Code Planning',  keys: ['code-analyst'] },
  { label: 'Developers',     keys: ['developer'] },
  { label: 'Assistants',  keys: ['assistant'] },
  { label: 'IT Admins',   keys: ['it-admin'] },
  { label: 'Marketing',   keys: ['seo'] },
  { label: 'Other',       keys: [] },  // catch-all for unmatched worker types
];

function _getWorkerIcon(tab) {
  const key = tab?.workerIdentity;
  return key && WORKER_ICONS[key] ? WORKER_ICONS[key] : DEFAULT_WORKER_ICON;
}

// Model preference order per worker type (first available wins)
const WORKER_MODEL_PREFS = {
  'developer':     ['claude_opus', 'claude_sonnet', 'anthropic_sonnet_4_6', 'codex', 'spark'],
  'code-analyst':  ['claude_opus', 'claude_sonnet', 'anthropic_sonnet_4_6', 'codex', 'spark'],
  'dev-manager':   ['claude_opus', 'codex', 'claude_sonnet', 'anthropic_sonnet_4_6', 'spark'],
  'it-admin':      ['anthropic_sonnet_4_6', 'claude_sonnet', 'claude_opus', 'codex', 'spark'],
  'seo':           ['spark', 'codex', 'anthropic_sonnet_4_6', 'claude_sonnet', 'claude_opus'],
  'assistant':     ['anthropic_sonnet_4_6', 'spark', 'codex', 'claude_sonnet', 'claude_opus'],
  'planner':       ['claude_opus', 'claude_sonnet', 'anthropic_sonnet_4_6', 'codex', 'spark'],
};
const DEFAULT_MODEL_PREFS = ['claude_opus', 'claude_sonnet', 'anthropic_sonnet_4_6', 'codex', 'spark'];

function _getWorkerDisplayNameByKey(key) {
  const workerKey = String(key || '').trim();
  if (!workerKey) return '';
  const w = _availableWorkers.find(w => w.key === workerKey);
  if (w?.name) return w.name;
  return workerKey
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function _getWorkerDisplayName(tab) {
  return _getWorkerDisplayNameByKey(tab?.workerIdentity);
}

// --- Skills popup on worker badge (click-only) ---
const _skillsCache = {};  // workerKey -> {skills:[], ts:number}

// Display overrides for skills in the popup menu
const _skillDisplayNames = {
  'network-audit-execution': 'Network Audit',
};
// Display order override (lower = higher in list). Skills not listed keep their API order.
const _skillDisplayOrder = {
  'using-skills': 0,
  'network-audit-execution': 1,
};

let _skillsDir = '';  // populated from API response
let _workerSkillsDir = '';  // per-worker skills folder path

async function _fetchSkillsForWorker(workerKey) {
  const cached = _skillsCache[workerKey];
  if (cached && Date.now() - cached.ts < 60000) { _skillsDir = cached.skillsDir || ''; _workerSkillsDir = cached.workerSkillsDir || ''; return cached.skills; }
  try {
    const res = await fetch(API + '/api/skills/' + encodeURIComponent(workerKey));
    const data = await res.json();
    const skills = data.skills || [];
    _skillsDir = data.skills_dir || '';
    _workerSkillsDir = data.worker_skills_dir || '';
    _skillsCache[workerKey] = { skills, skillsDir: _skillsDir, workerSkillsDir: _workerSkillsDir, ts: Date.now() };
    return skills;
  } catch { return []; }
}

async function _toggleSkillsPopup(ev) {
  ev.stopPropagation();
  const existing = document.getElementById('skills-popup');
  if (existing) { existing.remove(); return; }
  const tab = tabs.find(t => t.id === activeTabId);
  const workerKey = tab?.workerIdentity;
  if (!workerKey) return;
  const [skills] = await Promise.all([_fetchSkillsForWorker(workerKey), _loadAvailableWorkers()]);
  if (!skills.length) return;
  // Apply display order overrides (skills not in map keep original position)
  skills.sort((a, b) => (_skillDisplayOrder[a.id] ?? 50) - (_skillDisplayOrder[b.id] ?? 50));
  const badge = document.getElementById('worker-name-badge');
  if (!badge) return;
  const rect = badge.getBoundingClientRect();
  const popup = document.createElement('div');
  popup.id = 'skills-popup';
  popup.className = 'skills-popup';
  // Position fixed relative to viewport so re-renders don't destroy it
  popup.style.position = 'fixed';
  popup.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
  popup.style.right = (window.innerWidth - rect.right) + 'px';
  const titleRow = document.createElement('div');
  titleRow.className = 'skills-popup-title';
  const titleLabel = document.createElement('span');
  titleLabel.textContent = 'Skills';
  titleRow.appendChild(titleLabel);
  if (_skillsDir || _workersDir) {
    const editGroup = document.createElement('span');
    editGroup.className = 'skills-edit-group';
    const editLabel = document.createElement('span');
    editLabel.className = 'skills-edit-label';
    editLabel.textContent = 'Edit:';
    editGroup.appendChild(editLabel);
    if (_workersDir) {
      const workerBtn = document.createElement('button');
      workerBtn.className = 'skills-edit-btn';
      workerBtn.textContent = 'Worker';
      workerBtn.onclick = (e) => { e.stopPropagation(); document.getElementById('skills-popup')?.remove(); setAppMode('editor', _workersDir); setTimeout(() => { if (typeof EditorModule !== 'undefined') EditorModule.openFile(_workersDir.replace(/\/$/, '') + '/' + workerKey + '.md'); }, 300); };
      editGroup.appendChild(workerBtn);
    }
    if (_skillsDir) {
      const sep = document.createElement('span');
      sep.className = 'skills-edit-sep';
      sep.textContent = '|';
      editGroup.appendChild(sep);
      const skillsBtn = document.createElement('button');
      skillsBtn.className = 'skills-edit-btn';
      skillsBtn.textContent = 'Skills';
      skillsBtn.onclick = (e) => {
        e.stopPropagation(); document.getElementById('skills-popup')?.remove();
        setAppMode('editor', _workerSkillsDir || _skillsDir);
      };
      editGroup.appendChild(skillsBtn);
    }
    titleRow.appendChild(editGroup);
  }
  popup.appendChild(titleRow);
  skills.forEach(s => {
    const row = document.createElement('div');
    row.className = 'skills-popup-item';
    row.onclick = (e) => { e.stopPropagation(); document.getElementById('skills-popup')?.remove(); _insertSkillPrompt(s); };
    const name = document.createElement('span');
    name.className = 'skills-popup-name';
    name.textContent = _skillDisplayNames[s.id] || s.id.replace(/-/g, ' ');
    const desc = document.createElement('span');
    desc.className = 'skills-popup-desc';
    desc.textContent = s.description;
    row.appendChild(name);
    row.appendChild(desc);
    popup.appendChild(row);
  });
  document.body.appendChild(popup);
}

// Dismiss skills popup on click-outside
document.addEventListener('click', (e) => {
  const popup = document.getElementById('skills-popup');
  if (!popup) return;
  if (popup.contains(e.target)) return;
  const badge = document.getElementById('worker-name-badge');
  if (badge && badge.contains(e.target)) return;
  popup.remove();
});

async function _insertSkillPrompt(skill) {
  const starters = {
    'using-skills': 'Check which skills apply to this task: ',
    'verification-before-completion': 'Verify the following is actually complete with evidence: ',
    'scope-assessment': 'Assess the scope and complexity of this project: ',
    'delegation-dispatch': 'Delegate this task with a thorough prompt: ',
    'quality-gating': 'Run a quality gate on the current phase output: ',
    'agent-output-consolidation': 'Consolidate the following agent outputs and resolve conflicts: ',
    'multi-phase-orchestration': 'Plan and orchestrate a multi-phase pipeline for: ',
    'brainstorming': 'Let\'s brainstorm the design before implementing: ',
    'network-audit-execution': 'Run a comprehensive network audit on the current connected network',
    'diagnose-before-fix': 'Diagnose the root cause before making changes: ',
    'audit-phase-discipline': 'Execute the next audit phase with proper gates: ',
    'finding-documentation': 'Document all findings with structured FINDING_CARDs: ',
    'non-destructive-operations': 'Plan this change with backup and rollback steps: ',
    'post-change-verification': 'Verify all services are healthy after the change: ',
    'read-before-modify': 'Read and trace the code before making changes to: ',
    'test-after-change': 'Run tests to verify the change works correctly: ',
    'phase-discipline': 'Plan this as a phased project with verification at each step: ',
    'small-focused-changes': 'Make small, focused changes — one concern at a time: ',
    'read-before-opine': 'Analyze the code thoroughly before recommending: ',
    'plan-specificity': 'Create a specific plan with file paths and line numbers: ',
    'tradeoff-analysis': 'Analyze the tradeoffs between approaches for: ',
    'evidence-anchoring': 'Provide evidence-anchored analysis of: ',
  };
  const starter = starters[skill.id] || `Use the ${skill.id.replace(/-/g, ' ')} skill for: `;
  // Store the skill so confirmCompact can insert the prompt after compacting
  _pendingSkillAfterCompact = { starter };
  // Show the compact confirmation modal instead of compacting immediately
  showCompactConfirm();
}

function _getModelLogoForKey(modelKey) {
  const key = String(modelKey || '').trim();
  if (!key) return '🤖';
  const direct = MODELS[key];
  if (direct?.emoji) return direct.emoji;
  const fallback = Object.values(MODELS).find(m => (m?.model || '') === key && m?.emoji);
  return fallback?.emoji || '🤖';
}

function _getWorkerTabLabel(workerKey, modelKey = '') {
  const wk = String(workerKey || '').trim();
  if (!wk) return 'Worker';
  const mk = String(modelKey || '').trim();
  const exact = tabs.find(t => (t.workerIdentity || '') === wk && (!mk || t.modelKey === mk));
  if (exact?.label) return exact.label;
  const any = tabs.find(t => (t.workerIdentity || '') === wk);
  if (any?.label) return any.label;
  return _getWorkerDisplayNameByKey(wk) || 'Worker';
}

function _isComboInUse(modelKey, workerKey) {
  return tabs.some(t => t.modelKey === modelKey && (t.workerIdentity || '') === (workerKey || ''));
}

// Default model for new tabs: claude_opus if connected, then codex
function _defaultModel() {
  if (MODELS['claude_opus'] && _isModelAvailable('claude_opus', MODELS['claude_opus'])) return 'claude_opus';
  if (MODELS['codex'] && _isModelAvailable('codex', MODELS['codex'])) return 'codex';
  // Fall back to first available model
  for (const [key, m] of Object.entries(MODELS)) {
    if (_isModelAvailable(key, m)) return key;
  }
  // Nothing connected — return codex as placeholder (callers should check availability)
  return 'codex';
}

function _bestModelForWorker(workerKey) {
  const prefs = WORKER_MODEL_PREFS[workerKey] || DEFAULT_MODEL_PREFS;
  for (const mk of prefs) {
    if (MODELS[mk] && _isModelAvailable(mk, MODELS[mk])) return mk;
  }
  return _defaultModel();
}

function _autoSelectModelForWorker() {
  const identSel = document.getElementById('new-worker-identity-select');
  if (identSel) newWorkerIdentityKey = identSel.value;
  newWorkerModelKey = _bestModelForWorker(newWorkerIdentityKey);
  newWorkerError = '';
  // Re-render so model dropdown options update their "in use" states
  requestRender({ preserveScroll: true });
}

function _updateNewWorkerModal() {
  // Re-render the modal to update active counts — does NOT override model selection
  requestRender({ preserveScroll: true });
}

function _deferredTabClick(id) {
  clearTimeout(_tabClickTimer);
  _tabClickTimer = setTimeout(() => switchTab(id), 250);
}

function _renderTabItem(t, idx) {
  const m = MODELS[t.modelKey] || MODELS.codex;
  const ci = t.contextInfo;
  const tipCtx = ci ? fmtCtx(ci) : '--';
  const tipPct = ci ? ci.pct + '%' : '';
  const workerName = _getWorkerDisplayName(t);
  const tipModel = (workerName ? workerName + ' · ' : '') + (m.menuHover || m.model || m.name);
  const showWorkerIcon = (t.workerIdentity || '') === 'dev-manager';
  return `
    <div class="tab-item ${t.id === activeTabId ? 'active' : ''} model-${t.modelKey}" data-tab-id="${t.id}" data-tab-idx="${idx}" draggable="true"
         onclick="${editingTabId === t.id ? '' : `event.stopPropagation();_deferredTabClick('${t.id}')`}"
         ondblclick="clearTimeout(_tabClickTimer);_tabClickTimer=null;event.stopPropagation();startRenameTab('${t.id}')"
         ondragstart="onTabDragStart(event,'${t.id}')"
         ondragover="onTabDragOver(event,${idx})"
         ondrop="onTabDrop(event,${idx})"
         ondragend="onTabDragEnd(event)"
         onmouseenter="showTabTip(event)"
         onmouseleave="hideTabTip(event)">
      ${showWorkerIcon ? `<span class="tab-worker-icon">${_getWorkerIcon(t)}</span>` : ''}
      <span class="tab-model-logo">${m.emoji || ''}</span>
      ${editingTabId === t.id
        ? `<input class="tab-name-input" id="tab-name-${t.id}" value="${escText(t.label)}"
            onblur="renameTab('${t.id}', this.value)"
            onkeydown="if(event.key==='Enter'){renameTab('${t.id}',this.value);event.preventDefault();}if(event.key==='Escape'){editingTabId=null;editingTabRendered=false;requestRender({ preserveScroll: true });}"
            onclick="event.stopPropagation()">`
        : `<span class="tab-label">${escText(t.label)}</span>`}
      ${editingTabId !== t.id && t.needsAttention
        ? `<span class="tab-badge alert" title="Needs attention">⚠️</span>`
        : (editingTabId !== t.id && t.unread ? `<span class="tab-badge unread" title="Unread reply"></span>` : '')}
      ${(() => {
        if (editingTabId === t.id) return '';
        const inc = _delegIncomingForTab(t);
        const out = _delegOutgoingCount(t);
        if (inc.length > 0) return '<span class="tab-badge delegated" title="Delegated task running"></span>';
        if (out > 0) return '<span class="tab-badge delegated" title="Delegated task running"></span>';
        return '';
      })()}
      <button class="tab-close" onclick="event.stopPropagation();promptDeleteTab('${t.id}')" title="Delete">×</button>
      <div class="tab-hover-tip">
        <div class="tab-hover-model">${escText(tipModel)}</div>
        <div class="tab-hover-ctx">${tipCtx}${tipPct ? ` · ${tipPct}` : ''}</div>
      </div>
    </div>`;
}

function _renderGroupedTabs(allTabs) {
  // Bucket tabs into groups by workerIdentity
  const grouped = WORKER_GROUPS.map(g => ({ ...g, tabs: [] }));
  const knownKeys = new Set(WORKER_GROUPS.flatMap(g => g.keys));
  for (const t of allTabs) {
    const id = t.workerIdentity || '';
    let placed = false;
    for (const g of grouped) {
      if (g.keys.includes(id)) { g.tabs.push(t); placed = true; break; }
    }
    if (!placed) grouped[grouped.length - 1].tabs.push(t); // "Other"
  }
  // Render only groups that have tabs
  let html = '';
  for (const g of grouped) {
    if (g.tabs.length === 0) continue;
    if (g.label) html += `<div class="sidebar-group-header">${escText(g.label)}</div>`;
    for (const t of g.tabs) {
      const idx = allTabs.indexOf(t);
      html += _renderTabItem(t, idx);
    }
  }
  return html;
}

function _isModelAvailable(key, m) {
  // Claude Code models require the bridge to be up
  if (_isClaudeModel(key)) return _claudeBridgeUp;
  // Anthropic API models require the API key to be validated (only loaded if has_api_key)
  if (m.model === 'anthropic') return _anthropicApiConnected;
  // OpenRouter models require the API key (only loaded if config returned models)
  if (m.model === 'openrouter') return _orModelsLoaded;
  // Codex/Spark — available only when OpenAI is connected
  if (key === 'codex' || key === 'spark') return _openaiConnected;
  return true;
}

function renderNewWorkerModal() {
  const allAvailable = Object.entries(MODELS).filter(([k, m]) => _isModelAvailable(k, m));
  const coreModels = allAvailable.filter(([, m]) => m.model !== 'openrouter' && m.model !== 'anthropic');
  const anthropicModels = allAvailable.filter(([, m]) => m.model === 'anthropic');
  const orModels = allAvailable.filter(([, m]) => m.model === 'openrouter');
  const wk = newWorkerIdentityKey || '';
  const _modelOpt = (key, m, nameField) => {
    const count = tabs.filter(t => t.modelKey === key && (t.workerIdentity || '') === wk).length;
    const suffix = count > 0 ? ` (${count} active)` : '';
    const label = (m[nameField] || m.name) + suffix;
    return `<option value="${key}" ${key === newWorkerModelKey ? 'selected' : ''}>${escText(label)}</option>`;
  };
  const coreOpts = coreModels.map(([key, m]) => _modelOpt(key, m, 'name')).join('');
  const anthropicOpts = anthropicModels.length ? `<optgroup label="Anthropic API">${anthropicModels.map(([key, m]) => _modelOpt(key, m, 'shortName')).join('')}</optgroup>` : '';
  const orOpts = orModels.length ? `<optgroup label="OpenRouter">${orModels.map(([key, m]) => _modelOpt(key, m, 'shortName')).join('')}</optgroup>` : '';
  const workerOpts = _availableWorkers.map(w =>
    `<option value="${escText(w.key)}" ${w.key === newWorkerIdentityKey ? 'selected' : ''}>${escText(w.name)}</option>`
  ).join('');
  return `<div class="new-worker-overlay" onclick="if(event.target===this){cancelNewWorkerModal();}">
    <div class="new-worker-dialog">
      <h3>New Worker</h3>
      <label class="new-worker-label">Role</label>
      <select class="new-worker-select" id="new-worker-identity-select"
        onchange="newWorkerIdentityKey=this.value;newWorkerError='';_updateNewWorkerModal();">
        <option value="">No role (general)</option>
        ${workerOpts}
      </select>
      <label class="new-worker-label">Model</label>
      <select class="new-worker-select" id="new-worker-model-select"
        onchange="newWorkerModelKey=this.value;newWorkerError='';">
        ${coreOpts}
        ${anthropicOpts}
        ${orOpts}
      </select>
      ${newWorkerError ? `<p class="new-worker-error">${escText(newWorkerError)}</p>` : ''}
      <div class="new-worker-actions">
        <button class="btn-cancel" onclick="cancelNewWorkerModal()">Cancel</button>
        <button class="btn-create" onclick="confirmNewWorkerModal()">Create</button>
      </div>
    </div>
  </div>`;
}

function renderDeleteTabModal() {
  const tab = deleteTabTargetId ? tabs.find(t => t.id === deleteTabTargetId) : activeTab();
  const label = tab?.label || 'this tab';
  return `<div class="root-warning-overlay" onclick="if(event.target===this){cancelDeleteCurrentTab();}">
    <div class="root-warning">
      <h3>🧨 Delete Tab</h3>
      <p>Delete <strong>${escText(label)}</strong>?</p>
      <p>This will permanently clear the conversation history and remove the worker.</p>
      <div class="warn-actions">
        <button class="btn-cancel" onclick="cancelDeleteCurrentTab()">Cancel</button>
        <button class="btn-confirm" onclick="confirmDeleteCurrentTab()">Delete</button>
      </div>
    </div>
  </div>`;
}

function _shortPath(p) {
  return (p || '').replace('~/.kukuibot/', '').replace('~/', '~/');
}

/** Detect and parse a [DELEGATION UPDATE] message. Returns parsed info or null. */
function _parseDelegationUpdate(text) {
  if (!text || !text.startsWith('[DELEGATION UPDATE]')) return null;
  const lines = text.split('\n');
  let taskId = '', worker = '', status = '', elapsed = '', summary = '', action = '';
  // Only parse header fields from lines BEFORE the Summary section.
  // The summary body can contain lines like "Task: ..." or "Status: ..." that
  // would corrupt metadata if parsed as header fields.
  let inSummary = false;
  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith('Summary:')) { inSummary = true; continue; }
    if (inSummary) {
      // Inside summary body — only extract trailing Next:/Action: lines
      // that appear AFTER the summary content (they signal end of summary)
      if (t.startsWith('Next:')) { action = t.slice(5).trim(); inSummary = false; }
      else if (t.startsWith('Action:')) { action = t.slice(7).trim(); inSummary = false; }
      continue;
    }
    if (t.startsWith('Task:')) taskId = t.slice(5).trim();
    else if (t.startsWith('Worker:')) worker = t.slice(7).trim();
    else if (t.startsWith('Status:')) status = t.slice(7).trim();
    else if (t.startsWith('Elapsed:')) elapsed = t.slice(8).trim();
    else if (t.startsWith('Action:')) action = t.slice(7).trim();
    else if (t.startsWith('Next:')) action = t.slice(5).trim();
  }
  // Extract summary block (everything after "Summary:\n")
  const sumIdx = text.indexOf('Summary:\n');
  if (sumIdx !== -1) {
    const afterSum = text.slice(sumIdx + 9);
    // Summary ends before a root-level "Next:" or "Action:" line, or goes to end
    const endMatch = afterSum.match(/\n(?:Next:|Action:)/);
    summary = endMatch ? afterSum.slice(0, endMatch.index).trim() : afterSum.trim();
  }
  // Extract final status from "Status: x → y"
  const arrow = status.indexOf('\u2192');
  const toStatus = arrow !== -1 ? status.slice(arrow + 1).trim() : status;
  const icon = toStatus === 'completed' ? '\u2705' : toStatus === 'running' ? '\u2699\uFE0F' : (toStatus === 'failed' || toStatus === 'dispatch_failed' || toStatus === 'timed_out') ? '\u274C' : '\uD83D\uDCCB';
  return { taskId, worker, status, toStatus, elapsed, summary, action, icon };
}

/** Parse a [DELEGATED TASK ...] summary logged to the worker's base tab. */
function _parseDelegatedTaskSummary(text) {
  if (!text) return null;
  const headerMatch = text.match(/^\[DELEGATED TASK (\w+)\]/);
  if (!headerMatch) return null;
  const toStatus = headerMatch[1].toLowerCase();
  const lines = text.split('\n');
  let taskId = '', worker = '', elapsed = '', summary = '';
  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith('Task:')) taskId = t.slice(5).trim();
    else if (t.startsWith('Worker:')) worker = t.slice(7).trim();
    else if (t.startsWith('Elapsed:')) elapsed = t.slice(8).trim();
  }
  // Extract result block (everything after "Result:\n")
  const resIdx = text.indexOf('Result:\n');
  if (resIdx !== -1) {
    summary = text.slice(resIdx + 8).trim();
  }
  const icon = toStatus === 'completed' ? '\u2705' : toStatus === 'failed' ? '\u274C' : '\uD83D\uDCCB';
  return { taskId, worker, status: toStatus, toStatus, elapsed, summary, action: '', icon };
}

/** Render a delegation update as a collapsible system card. */
function renderDelegationCard(m, parsed) {
  const { taskId, worker, status, toStatus, elapsed, summary, action, icon } = parsed;
  const time = m.timestamp ? fmtTime(m.timestamp) : (m.ts ? fmtTime(m.ts) : '');
  const statusLabel = toStatus.replace(/_/g, ' ');
  const shortTask = taskId.length > 16 ? taskId.slice(0, 16) + '\u2026' : taskId;
  const summaryLine = `${icon} Delegation: ${statusLabel}` + (shortTask ? ` \u2014 ${shortTask}` : '');

  let bodyHtml = '';
  if (taskId) bodyHtml += `<div class="deleg-field"><span class="deleg-label">Task</span> ${escText(taskId)}</div>`;
  if (worker) bodyHtml += `<div class="deleg-field"><span class="deleg-label">Worker</span> ${escText(worker)}</div>`;
  if (status) bodyHtml += `<div class="deleg-field"><span class="deleg-label">Status</span> ${escText(status)}</div>`;
  if (elapsed) bodyHtml += `<div class="deleg-field"><span class="deleg-label">Elapsed</span> ${escText(elapsed)}</div>`;
  if (summary) bodyHtml += `<div class="deleg-summary"><div class="md-content">${esc(summary)}</div></div>`;
  if (action) bodyHtml += `<div class="deleg-field deleg-action"><span class="deleg-label">Action</span> ${escText(action)}</div>`;

  let html = `<div class="msg assistant delegation"><div class="bubble" style="padding:0;background:transparent"><div class="system-card deleg-card">`;
  html += `<details><summary class="deleg-toggle">${escText(summaryLine)}</summary>`;
  html += `<div class="deleg-body">${bodyHtml}</div>`;
  html += `</details>`;
  html += `</div></div>`;
  if (time) html += `<div class="time">${time}</div>`;
  html += `</div>`;
  return html;
}

/** Build a system card message object. All system messages should use this. */
function _sysCard(text, icon = 'ℹ️', title = 'System') {
  return {
    id: Date.now() + Math.floor(Math.random() * 1000),
    role: 'system', ts: Date.now(), text,
    timestamp: new Date(),
    _card: { icon, title, stats: [], files: [], _showBody: true }
  };
}

function renderSystemCard(m) {
  const d = m._card || {};
  const icon = d.icon || '🔄';
  const title = d.title || 'System';
  const files = (d.files || []).map(f => _shortPath(f));
  const stats = d.stats || [];
  const time = m.timestamp ? fmtTime(m.timestamp) : (m.ts ? fmtTime(m.ts) : '');
  // For auto-wrapped plain system messages, show the text as body content
  const bodyText = (m.text || '').trim();
  // Show body text when explicitly requested, or for auto-wrapped plain messages
  const showBody = bodyText && bodyText.toLowerCase() !== title.toLowerCase() && d._showBody;
  const looksMarkdown = showBody && /(^\s*#{1,6}\s)|(^\s*[-*]\s)|(```)|(^\s*\d+\.\s)/m.test(bodyText);

  let html = `<div class="msg system"><div class="bubble" style="padding:0;background:transparent"><div class="system-card">`;
  html += `<div class="sc-header"><span class="sc-icon">${icon}</span>${escText(title)}</div>`;
  if (stats.length) {
    html += `<div class="sc-stats">${stats.map(s => `<span>${escText(s)}</span>`).join('')}</div>`;
  }
  if (showBody) {
    html += looksMarkdown
      ? `<div class="sc-body"><div class="md-content">${esc(bodyText)}</div></div>`
      : `<div class="sc-body">${escText(bodyText)}</div>`;
  }
  if (files.length) {
    const allTags = files.map(f => `<span class="sc-file">${escText(f)}</span>`);
    html += `<div class="sc-files">${allTags.join('')}</div>`;
  }
  html += `</div></div>`;
  if (time) html += `<div class="time">${time}</div>`;
  html += `</div>`;
  return html;
}

function pushAssistantMessage(tab, text, def, opts = {}) {
  if (!tab) return false;
  const raw = String(text || '');
  if (!raw.trim()) return false;

  const trimmed = raw.trim();
  const last = (tab.messages || [])[tab.messages.length - 1];
  if (last && last.role === 'assistant' && String(last.text || '').trim() === trimmed) {
    if (opts.tokens && !last.tokens) last.tokens = opts.tokens;
    if (opts.thinking && !last.thinking) last.thinking = opts.thinking;
    if (opts.thinkingExpanded) last.thinkingExpanded = true;
    return false;
  }

  const msg = {
    id: Date.now() + Math.floor(Math.random() * 1000),
    role: 'assistant',
    text: raw,
    timestamp: new Date(),
    modelLabel: def.shortName || def.name,
    tokens: opts.tokens || null,
  };
  if (opts.thinking) {
    msg.thinking = opts.thinking;
    if (opts.thinkingExpanded) msg.thinkingExpanded = true;
  }
  tab.messages.push(msg);
  return true;
}

/**
 * finalizeTurn — single place that transitions a tab from streaming to done.
 *
 * Sequence: snapshot reasoning → push message (with reasoning expanded) → clear live fields → render once.
 * This prevents the race where clearing thinkingText before pushing the message causes reasoning to vanish.
 *
 * @param {object} tab        - The tab object
 * @param {string} finalText  - The final assistant text
 * @param {object} def        - Model definition (from MODELS[])
 * @param {object} opts       - { tokens, skipRender, skipQuickName }
 */
function finalizeTurn(tab, finalText, def, opts = {}) {
  if (!tab) return;
  const runId = String(opts.runId || tab.currentRunId || '').trim();
  if (runId && runId === String(tab.lastFinalizedRunId || '')) return;
  // 1. Snapshot reasoning before clearing anything
  const savedThinking = String(tab._doneThinking || tab.thinkingText || '').trim();

  // 2. Push assistant message with reasoning attached (collapsed by default after completion)
  const text = String(finalText || '').trim();
  if (text) {
    pushAssistantMessage(tab, finalText, def, {
      tokens: tab._doneTokens || opts.tokens || null,
      thinking: savedThinking || null,
      thinkingExpanded: false,
    });
  } else if (savedThinking) {
    // No final text but reasoning exists — push a shell message so reasoning isn't lost
    pushAssistantMessage(tab, '*(No text output — see thought process below)*', def, {
      tokens: tab._doneTokens || opts.tokens || null,
      thinking: savedThinking,
      thinkingExpanded: true,
    });
  }

  // 2b. Detect choice buttons on the just-pushed assistant message
  if (text) {
    const detect = detectChoiceButtons(text);
    if (detect) {
      const lastMsg = tab.messages[tab.messages.length - 1];
      if (lastMsg && lastMsg.role === 'assistant') {
        lastMsg._choiceButtons = detect.options;
      }
    }
  }

  // 3. Now safe to clear all live streaming fields
  tab._doneThinking = '';
  tab._doneTokens = null;
  tab.streamingText = '';
  tab.thinkingText = '';
  tab.thinkingExpanded = false;
  tab.wasLoading = false;
  tab.loadingSinceMs = 0;
  tab.elevation = null;
  tab.activeToolLabel = null;
  tab.activeToolDetail = null;

  // 4. Fire the deferred 'Done' work log entry now that the message is persisted
  addWork(tab.id, { type: 'status', text: 'Done' });

  // 5. Persist and render once
  if (runId) tab.lastFinalizedRunId = runId;
  persistTabs();
  if (!opts.skipRender) requestRender();
  refreshMeta();
  if (!opts.skipQuickName) quickNameTab(tab);

  // 6. Drain message queue — send next queued message for this tab
  _drainMessageQueue(tab);
}

function renderMessage(m, def, tabId = null, msgIdx = -1) {
  // Delegation task summaries (logged to worker base tab on completion)
  const rawText = m.text || '';
  if (rawText.startsWith('[DELEGATED TASK ')) {
    const parsed = _parseDelegatedTaskSummary(rawText);
    if (parsed) return renderDelegationCard(m, parsed);
  }
  // Delegation updates render as collapsible cards regardless of stored role
  if (rawText.startsWith('[DELEGATION UPDATE]')) {
    // Check if delegation notification was prepended to a user message.
    // Prefer explicit boundary token; fall back to legacy \n\n---\n\n separator
    // for backward compatibility with older persisted messages.
    const boundary = `\n\n${DELEGATION_PREPEND_BOUNDARY}\n\n`;
    let sepIdx = rawText.indexOf(boundary);
    let sepLen = boundary.length;
    if (sepIdx === -1) {
      sepIdx = rawText.indexOf('\n\n---\n\n');
      sepLen = 7;
    }
    const delegBlock = sepIdx !== -1 ? rawText.slice(0, sepIdx) : rawText;
    const userText = sepIdx !== -1 ? rawText.slice(sepIdx + sepLen).trim() : '';
    // Split multiple notifications (joined with \n\n[DELEGATION UPDATE])
    const parts = delegBlock.split(/\n\n(?=\[DELEGATION UPDATE\])/);
    let html = '';
    const ownerTab = tabId ? getTab(tabId) : activeTab();
    for (const part of parts) {
      const parsed = _parseDelegationUpdate(part.trim());
      if (parsed) {
        // Dedup: skip if SSE already rendered this task+status
        if (ownerTab) {
          const dk = (parsed.taskId || '') + ':' + (parsed.toStatus || parsed.status || '');
          ownerTab._seenDelegNotifs = ownerTab._seenDelegNotifs || new Set();
          if (ownerTab._seenDelegNotifs.has(dk)) continue;
          ownerTab._seenDelegNotifs.add(dk);
          if (ownerTab._seenDelegNotifs.size > 200) {
            const first = ownerTab._seenDelegNotifs.values().next().value;
            ownerTab._seenDelegNotifs.delete(first);
          }
        }
        html += renderDelegationCard(m, parsed);
      }
    }
    if (html) {
      // If there's a trailing user message after the separator, render it too
      if (userText) {
        const userMsg = Object.assign({}, m, { text: userText, role: 'user' });
        html += renderMessage(userMsg, def, tabId, msgIdx);
      }
      return html;
    }
  }

  // System wake notifications — render as system cards regardless of stored role
  if (rawText.startsWith('[SYSTEM WAKE]')) {
    if (!m._card) {
      m._card = { icon: '🔄', title: 'System Wake', stats: [], files: [], _showBody: true };
    }
    return renderSystemCard(Object.assign({}, m, { role: 'system' }));
  }

  // ALL system messages render as cards — auto-wrap plain ones
  if (m.role === 'system') {
    if (!m._card) {
      // Auto-wrap legacy plain system messages into card format
      m._card = { icon: 'ℹ️', title: 'System', stats: [], files: [], _showBody: true };
    }
    return renderSystemCard(m);
  }

  const isUser = m.role === 'user';
  const modelIcon = !isUser && def?.emoji ? `<span class="bubble-model-icon">${def.emoji}</span> ` : '';
  const label = isUser ? 'You' : `${modelIcon}${m.modelLabel || def.shortName || def.name}`;
  const cls = isUser ? 'user' : 'assistant';
  // Build inline attachment previews for user messages
  const attachHtml = (isUser && m._attachments && m._attachments.length) ? _renderMsgAttachments(m._attachments) : '';
  const hasText = m.text && m.text !== '[attached files]';
  const mdRendered = hasText ? `<div class="md-content">${esc(m.text)}</div>` : '';
  const content = isUser ? (mdRendered + attachHtml) : mdRendered;
  const time = m.timestamp ? fmtTime(m.timestamp) : '';
  const tokenStr = m.tokens ? ` · ${fmtM(m.tokens)}` : '';
  const streamingAttr = m._streaming ? ' id="streaming-msg"' : '';
  const canToggleThinking = !!tabId && Number.isInteger(msgIdx) && msgIdx >= 0;
  const tabForThinking = tabId ? getTab(tabId) : null;
  const isCodexThinking = (tabForThinking?.modelKey || '') === 'codex';
  const thinkingBodyClass = isCodexThinking ? 'thinking-content thinking-plain' : 'thinking-content';
  const thinkingBody = isCodexThinking
    ? `<div class="${thinkingBodyClass}">${esc(m.thinking)}</div>`
    : `<div class="${thinkingBodyClass}"><div class="md-content">${esc(m.thinking)}</div></div>`;
  const thinkingHtml = m.thinking
    ? `<div class="thinking-block thinking-saved${m.thinkingExpanded ? ' expanded' : ''}"><button class="thinking-toggle"${canToggleThinking ? ` onclick="toggleMessageThinking('${tabId}',${msgIdx})"` : ''}>${m.thinkingExpanded ? '▼' : '▶'} 🧠 Thought process</button>${m.thinkingExpanded ? thinkingBody : ''}</div>`
    : '';
  const escAttr = s => escText(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const hasChoiceBtns = m._choiceButtons && m._choiceButtons.length > 0;
  const choiceBtnsHtml = hasChoiceBtns
    ? `<div class="choice-buttons">${m._choiceButtons.map((opt, bi) =>
        `<button class="choice-btn" title="${escAttr(opt.value)}" data-choice-idx="${bi}" data-choice-tab="${tabId}" data-choice-msg="${msgIdx}">${escText(opt.label)}</button>`
      ).join('')}</div>`
    : '';
  const pendingCls = m._pending ? ' pending' : '';
  const bubbleCls = hasChoiceBtns ? 'bubble has-choice-buttons' : 'bubble';
  const pendingBadge = m._pending ? '<div class="pending-badge">Pending — will send when ready</div>' : '';
  const pendingDismiss = m._pending ? `<button class="pending-dismiss" onclick="_cancelQueuedMessage(${m.id})" title="Remove from queue">&times;</button>` : '';
  return `<div class="msg ${cls}${pendingCls}"${streamingAttr}>${pendingDismiss}<div class="label">${label}</div><div class="${bubbleCls}">${content}${choiceBtnsHtml}</div>${thinkingHtml}${time ? `<div class="time">${time}${tokenStr}</div>` : ''}${pendingBadge}</div>`;
}

function renderMessagesInner(tab, def) {
  const messages = tab ? tab.messages : [];
  const workLog = tab ? tab.workLog : [];
  const tabId = tab?.id || null;
  const isStreamOwner = tabIsStreamOwner(tab);
  const isElevationOwner = !!tabId && !!tabElevation(tab);
  const isStatusOwner = !!tabId && statusBannerTabId === tabId;
  const isSafetyCapOwner = !!tabId && safetyCapTabId === tabId;
  const liveText = tabStreamingText(tab);
  const isLoading = isTabLoading(tab);
  return `
    ${messages.length === 0 && !(liveText && isStreamOwner) && !(isLoading && isStreamOwner) ? `
      <div class="empty-state">
        <div class="emoji">${def.emoji}</div>
        <h2>${tab ? escText(tab.label) : def.name}</h2>
        <p>${def.model} · Standalone Agent<br>Full tool access · Unified work log</p>
      </div>` : ''}
    ${statusBanner && isStatusOwner ? `<div class="status-banner info">${escText(statusBanner)}</div>` : ''}
    ${safetyCapMessage && isSafetyCapOwner ? `
      <div class="status-banner warn">
        <div><strong>⚠️ Safety cap reached</strong></div>
        <div style="margin-top:4px;opacity:0.9">${escText(safetyCapMessage)}</div>
        <div class="cap-actions">
          <button onclick="quickSend('continue and summarize progress')" style="background:rgba(245,158,11,0.2);color:#fcd34d">Continue</button>
          <button onclick="quickSend('summarize progress and stop')" style="background:rgba(255,255,255,0.08);color:#fcd34d">Summarize</button>
          <button onclick="dismissCap()" style="background:rgba(255,255,255,0.04);color:#fcd34d">Dismiss</button>
        </div>
      </div>` : ''}
    ${tab && !tab._allLoaded && messages.length > 0 ? `<div data-load-older style="text-align:center;padding:12px;color:var(--accent);font-size:13px;cursor:pointer;user-select:none" onclick="loadOlderMessages()">↑ Load older messages (${Math.max(0, (tab._chatlogTotal || 0) - (tab._chatlogLoaded || 0))} remaining)</div>` : ''}
    ${messages.map((m, i) => renderMessage(m, def, tabId, i)).join('')}
    ${tab && isLoading && isStreamOwner ? `<div class="thinking-block" id="live-thinking-${tabId}"${tab.thinkingText ? '' : ' style="display:none"'}><button class="thinking-toggle" onclick="toggleThinking('${tabId}')">${tab.thinkingExpanded ? '▼' : '▶'} 🧠 Thinking…</button>${tab.thinkingExpanded ? ((tab.modelKey === 'codex') ? `<div class="thinking-content thinking-plain" id="live-thinking-content-${tabId}">${esc(tab.thinkingText || '')}</div>` : `<div class="thinking-content" id="live-thinking-content-${tabId}"><div class="md-content">${esc(tab.thinkingText || '')}</div></div>`) : ''}</div>` : ''}
    ${liveText && isStreamOwner ? renderMessage({ role: 'assistant', text: liveText, timestamp: new Date(), _streaming: true }, def, tabId) : ''}
    ${''}
    <div id="bottom"></div>
  `;
}

function renderMessageStreamOnly(opts = {}) {
  if (!streamDomInitialized) return;
  const forcePaneRefresh = !!opts.forcePaneRefresh;
  const forceStickBottom = !!opts.forceStickBottom;
  const lockScroll = !!opts.lockScroll;
  const tab = activeTab();
  if (!tab) return;
  const def = MODELS[tab.modelKey] || MODELS.codex;
  const el = document.getElementById('messages');
  if (!el) return;

  const prevTop = el.scrollTop;
  const prevBottomOffset = Math.max(0, el.scrollHeight - (el.scrollTop + el.clientHeight));
  const wasNearBottom = prevBottomOffset < 24;

  // Always refresh the work-log host (it's outside .messages now)
  const wlHost = document.getElementById('work-log-host');
  if (wlHost) wlHost.innerHTML = renderWorkLogFixed(tab);

  const streamEl = document.getElementById('streaming-msg');
  const liveThinkingHost = document.getElementById(`live-thinking-${tab.id}`);
  const liveThinkingBody = document.getElementById(`live-thinking-content-${tab.id}`);
  if (!forcePaneRefresh && (streamEl || liveThinkingHost)) {
    // Fast path: patch streaming bubble + live-thinking block without full pane re-render.
    const bubble = streamEl && streamEl.querySelector('.bubble');
    if (bubble) bubble.innerHTML = `<div class="md-content">${esc(tabStreamingText(tab))}</div>`;

    if (liveThinkingHost) {
      const hasThinking = !!tab.thinkingText;
      liveThinkingHost.style.display = hasThinking ? '' : 'none';

      const tBtn = liveThinkingHost.querySelector('.thinking-toggle');
      if (tBtn) tBtn.textContent = `${tab.thinkingExpanded ? '▼' : '▶'} 🧠 Thinking…`;

      if (tab.thinkingExpanded && hasThinking) {
        if (liveThinkingBody) {
          liveThinkingBody.innerHTML = (tab.modelKey === 'codex')
            ? `${esc(tab.thinkingText || '')}`
            : `<div class="md-content">${esc(tab.thinkingText || '')}</div>`;
        } else {
          // Expand created after prior collapsed render; refresh pane once to add content node.
          el.innerHTML = renderMessagesInner(tab, def);
        }
      }
      if (!tab.thinkingExpanded && liveThinkingBody) {
        liveThinkingBody.remove();
      }
    }
  } else {
    // Refresh only the messages pane (not full app root).
    el.innerHTML = renderMessagesInner(tab, def);
  }

  if (lockScroll) {
    const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = Math.max(0, Math.min(prevTop, maxTop));
  } else if (forceStickBottom) {
    el.scrollTop = el.scrollHeight;
    autoStickBottom = true;
  } else if (AUTO_SCROLL_ENABLED && (autoStickBottom || wasNearBottom)) {
    el.scrollTop = el.scrollHeight;
  } else {
    const nextTop = Math.max(0, el.scrollHeight - el.clientHeight - prevBottomOffset);
    el.scrollTop = Number.isFinite(nextTop) ? nextTop : prevTop;
  }
}

function workLogText(e) {
  if (!e) return '';
  if (e.type === 'reasoning') return e.text || '';
  if (e.type === 'tool_call') return `${e.label} → ${e.detail || ''}`;
  if (e.type === 'tool_result') return `${e.label} ✓ ${e.detail || ''}`;
  if (e.type === 'status') return e.text || '';
  return '';
}


function changeWorkerType(roleKey) {
  const tab = activeTab();
  if (!tab) return;
  tab.workerIdentity = roleKey;
  persistTabs();
  requestRender({ preserveScroll: true });
}

function renderWorkLog(workLog, isWorking = false, activeToolLabel = null, toolCallCount = 0, activeToolDetail = null, connStatus = 'connected') {
  const list = workLog || [];
  // Use the tracked total, not the capped array
  const toolCalls = toolCallCount || list.filter(e => e.type === 'tool_call').length;

  // Display last 100 items only
  const displayList = list.slice(-100);
  const items = displayList.map(e => {
    const text = workLogText(e);
    return `<li><span class="dot">•</span><span>${escText(text)}</span><span class="time">${fmtTime(e.ts)}</span></li>`;
  }).join('');

  const toolSummary = `(${toolCalls} tool${toolCalls === 1 ? '' : 's'})`;
  const leftText = (!showWorkLog && isWorking)
    ? `${toolSummary}${activeToolLabel ? ` ${activeToolLabel}` : ''}${activeToolDetail ? ` ${activeToolDetail}` : ''}`
    : toolSummary;

  const rightBusy = isWorking
    ? `<span class="work-head-right"><span class="typing-dots mini"><div class="dot"></div><div class="dot"></div><div class="dot"></div></span><button class="wl-inline-stop" onclick="cancelGeneration(event)" title="Stop generation">Stop</button></span>`
    : '';

  // Connection status indicator bar (left border on toggle row, mirrors deleg-bar pattern)
  const connTitle = connStatus === 'connected' ? 'Connected' : connStatus === 'connecting' ? 'Reconnecting…' : 'Disconnected';

  return `<div class="work-log">
    <div class="conn-bar conn-${connStatus}" title="${connTitle}" style="display:flex;align-items:center;gap:0;">
      <button class="wl-compact-btn" onclick="showCompactConfirm()" title="Smart Compact">🗜</button>
      <button class="work-log-toggle" onclick="toggleWorkLog()" style="flex:1;">
        <span class="work-head-left">${showWorkLog ? '▼' : '▶'} ${escText(leftText)}</span>
        ${rightBusy}
      </button>
    </div>
    ${showWorkLog ? `<ul class="work-log-list">${items}</ul>` : ''}
  </div>`;
}

function renderWorkLogFixed(tab) {
  if (!tab) return '';
  const workLog = tab.workLog || [];
  const isStreamOwner = tabIsStreamOwner(tab);
  const isLoading = isTabLoading(tab);
  const isWorking = isLoading && isStreamOwner;
  const connStatus = getTabConnectionStatus(tab);
  return renderWorkLog(workLog, isWorking, tab.activeToolLabel || null, tab.toolCallCount || 0, tab.activeToolDetail || null, connStatus);
}

function renderElevation(tab) {
  const elev = tabElevation(tab);
  if (!elev) return '';
  return `<div class="elev-modal-overlay" onclick="if(event.target===this)handleElevation('deny')">
    <div class="elev-modal">
      <div class="elev-modal-icon">🔐</div>
      <div class="elev-modal-title">Approval Required</div>
      <div class="elev-modal-detail">${APP_NAME} wants to run a restricted action:</div>
      <div class="elev-modal-cmd"><strong>${escText(elev.tool_name)}</strong> — ${escText(elev.reason)}</div>
      <div class="elev-modal-actions">
        <button class="elev-btn-allow" onclick="handleElevation('approve')">✓ Allow</button>
        <button class="elev-btn-deny"  onclick="handleElevation('deny')">✗ Deny</button>
        <button class="elev-btn-always" onclick="handleElevationAllowAll()" title="Allow this and all future actions automatically">⚡ Always Allow</button>
      </div>
    </div>
  </div>`;
}

function renderLogin(root) {
  root.innerHTML = `<div class="login-screen">
    <h2>🔒 Logged out</h2>
    <p class="subtitle">Your ${APP_NAME} session ended</p>
    <p style="font-size:12px;color:var(--muted)">Sign in again to continue.</p>
    <button class="login-btn" onclick="window.location.href='/login.html'">Go to Login</button>
  </div>`;
}

// --- Input ---
function handleKey(e) {
  if (e?.isComposing) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (voiceActive) { stopVoice(false); voiceTranscript = ''; }
    send();
  }
  if (e.key === 'ArrowRight') {
    const inp = document.getElementById('input');
    const atEnd = inp && inp.selectionStart === inp.value.length;
    if (voiceActive || atEnd) {
      e.preventDefault();
      if (voiceActive) { cancelVoice(); } else { startVoice(); }
    }
  }
  if (e.key === 'Escape' && voiceActive) { e.preventDefault(); cancelVoice(); }
}
function autoResize(el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 200) + 'px'; }

// --- File Paste & Drag-Drop ---
const FILE_MAX_IMAGE_BYTES = 10 * 1024 * 1024;  // 10MB
const FILE_MAX_TEXT_BYTES = 500 * 1024;           // 500KB
const FILE_MAX_COUNT = 5;
let _fileReadersLoading = 0;  // Track in-flight FileReader ops to prevent send() race

const FILE_TEXT_EXTENSIONS = new Set([
  'py','js','json','md','txt','csv','log','yaml','yml','toml','html','css',
  'sh','sql','xml','ini','cfg','conf','rb','go','rs','java','c','cpp','h','ts'
]);
const FILE_IMAGE_MIMES = new Set(['image/png','image/jpeg','image/gif','image/webp','image/svg+xml']);

function _fileIsImage(file) {
  return FILE_IMAGE_MIMES.has(file.type);
}
function _fileIsText(file) {
  if (file.type && file.type.startsWith('text/')) return true;
  const ext = (file.name || '').split('.').pop().toLowerCase();
  return FILE_TEXT_EXTENSIONS.has(ext);
}

function _showFileToast(msg) {
  const area = document.querySelector('.input-area');
  if (!area) return;
  // Remove any existing toast
  const old = area.querySelector('.file-toast');
  if (old) old.remove();
  const toast = document.createElement('div');
  toast.className = 'file-toast';
  toast.textContent = msg;
  area.style.position = 'relative';
  area.appendChild(toast);
  setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
}

function _processFiles(fileList) {
  const tab = activeTab();
  if (!tab) return;
  if (!tab.pendingFiles) tab.pendingFiles = [];

  for (const file of fileList) {
    if (tab.pendingFiles.length >= FILE_MAX_COUNT) {
      _showFileToast(`Maximum ${FILE_MAX_COUNT} files per message`);
      break;
    }
    const isImage = _fileIsImage(file);
    const isText = _fileIsText(file);
    if (!isImage && !isText) {
      _showFileToast(`Unsupported file type: ${file.name}`);
      continue;
    }
    if (isImage && file.size > FILE_MAX_IMAGE_BYTES) {
      _showFileToast(`Image too large (max 10MB): ${file.name}`);
      continue;
    }
    if (isText && file.size > FILE_MAX_TEXT_BYTES) {
      _showFileToast(`Text file too large (max 500KB): ${file.name}`);
      continue;
    }

    const entry = {
      id: Date.now() + Math.floor(Math.random() * 10000),
      name: file.name,
      type: file.type || 'application/octet-stream',
      size: file.size,
      dataUrl: null,
      textContent: null,
      isImage
    };

    _fileReadersLoading++;
    if (isImage) {
      const reader = new FileReader();
      reader.onload = () => {
        entry.dataUrl = reader.result;
        _fileReadersLoading = Math.max(0, _fileReadersLoading - 1);
        _rerenderAttachmentBar();
        updateInputBtn();
      };
      reader.onerror = () => { _fileReadersLoading = Math.max(0, _fileReadersLoading - 1); };
      reader.readAsDataURL(file);
    } else {
      const reader = new FileReader();
      reader.onload = () => {
        entry.textContent = reader.result;
        _fileReadersLoading = Math.max(0, _fileReadersLoading - 1);
        _rerenderAttachmentBar();
        updateInputBtn();
      };
      reader.onerror = () => { _fileReadersLoading = Math.max(0, _fileReadersLoading - 1); };
      reader.readAsText(file);
    }
    tab.pendingFiles.push(entry);
  }
  _rerenderAttachmentBar();
  updateInputBtn();
}

function _removeAttachment(fileId) {
  const tab = activeTab();
  if (!tab || !tab.pendingFiles) return;
  tab.pendingFiles = tab.pendingFiles.filter(f => f.id !== fileId);
  _rerenderAttachmentBar();
  updateInputBtn();
}

function renderAttachmentBar(tab) {
  if (!tab || !tab.pendingFiles || !tab.pendingFiles.length) return '';
  const chips = tab.pendingFiles.map(f => {
    const thumb = (f.isImage && f.dataUrl)
      ? `<img class="chip-thumb" src="${f.dataUrl}" alt="">`
      : `<span class="chip-icon">${f.isImage ? '🖼️' : '📎'}</span>`;
    return `<span class="attachment-chip">
      ${thumb}
      <span class="chip-name" title="${escText(f.name)}">${escText(f.name)}</span>
      <button class="chip-remove" onclick="_removeAttachment(${f.id})" title="Remove">✕</button>
    </span>`;
  }).join('');
  return `<div class="attachment-bar">${chips}</div>`;
}

function _rerenderAttachmentBar() {
  const area = document.querySelector('.input-area');
  if (!area) return;
  const existing = area.querySelector('.attachment-bar');
  if (existing) existing.remove();
  const tab = activeTab();
  const html = renderAttachmentBar(tab);
  if (html) {
    const inputRow = area.querySelector('.input-row');
    if (inputRow) inputRow.insertAdjacentHTML('beforebegin', html);
  }
}

function _onPasteFile(e) {
  const items = e.clipboardData?.items;
  const files = e.clipboardData?.files;
  const collected = [];
  // Check items first (for screenshot pastes)
  if (items) {
    for (const item of items) {
      if (item.kind === 'file') {
        const f = item.getAsFile();
        if (f) collected.push(f);
      }
    }
  }
  // Also check files list
  if (files && files.length && !collected.length) {
    for (const f of files) collected.push(f);
  }
  if (collected.length > 0) {
    e.preventDefault();
    _processFiles(collected);
  }
  // If no files, let default paste behavior happen (plain text)
}
function _onDragOver(e) { e.preventDefault(); e.currentTarget.classList.add('drag-over'); }
function _onDragLeave(e) { e.currentTarget.classList.remove('drag-over'); }
function _onDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  if (e.dataTransfer?.files?.length) _processFiles(e.dataTransfer.files);
}

function attachFileListeners() {
  const input = document.getElementById('input');
  const area = document.querySelector('.input-area');
  // Use named handlers so re-attaching after innerHTML rebuild is safe
  // (old DOM is gone, new DOM needs fresh listeners)
  if (input && !input._fileListenerAttached) {
    input.addEventListener('paste', _onPasteFile);
    input._fileListenerAttached = true;
  }
  if (area && !area._fileListenerAttached) {
    area.addEventListener('dragover', _onDragOver);
    area.addEventListener('dragleave', _onDragLeave);
    area.addEventListener('drop', _onDrop);
    area._fileListenerAttached = true;
  }
  // Re-render attachment bar if tab has pending files
  _rerenderAttachmentBar();
}

// --- Inline attachment rendering in chat messages ---

function _renderMsgAttachments(attachments) {
  if (!attachments || !attachments.length) return '';
  const images = attachments.filter(a => a.isImage && a.dataUrl);
  const textFiles = attachments.filter(a => !a.isImage && a.textContent !== null);
  let html = '<div class="msg-attachments">';
  if (images.length) {
    html += '<div class="msg-attach-images">';
    for (const img of images) {
      html += `<img class="msg-attach-img" src="${img.dataUrl}" alt="${escText(img.name)}" title="${escText(img.name)}" onclick="_openLightbox(this.src)">`;
    }
    html += '</div>';
  }
  for (const tf of textFiles) {
    const uid = 'tfa-' + Math.floor(Math.random() * 1e9);
    html += `<div class="msg-attach-file">
      <button class="msg-attach-file-header" onclick="_toggleFileContent('${uid}',this)">
        <span class="file-chevron">▶</span>
        <span class="file-icon">📄</span>
        <span class="file-label">${escText(tf.name)}</span>
      </button>
      <div class="msg-attach-file-content" id="${uid}">${escText(tf.textContent)}</div>
    </div>`;
  }
  html += '</div>';
  return html;
}

function _toggleFileContent(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  const isVisible = el.classList.toggle('show');
  if (btn) btn.classList.toggle('expanded', isVisible);
}

function _openLightbox(src) {
  const existing = document.querySelector('.lightbox-overlay');
  if (existing) existing.remove();
  const overlay = document.createElement('div');
  overlay.className = 'lightbox-overlay';
  overlay.onclick = (e) => { if (e.target === overlay || e.target.classList.contains('lightbox-close')) overlay.remove(); };
  overlay.innerHTML = `<button class="lightbox-close" title="Close">✕</button><img src="${src}" alt="Preview">`;
  document.body.appendChild(overlay);
  // Close on Escape key
  const onKey = (e) => { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', onKey); } };
  document.addEventListener('keydown', onKey);
}

// --- Voice Input (Web Speech API — works in Safari via on-device Siri STT) ---
function cleanupVoice() {
  if (silenceTimer) { clearInterval(silenceTimer); silenceTimer = null; }
  if (speechRecognition) {
    try { speechRecognition.abort(); } catch {}
    speechRecognition = null;
  }
}

function voiceAutoSend() {
  const text = voiceTranscript.trim();
  cleanupVoice();
  voiceActive = false;
  voiceTranscript = '';
  if (text) {
    const input = document.getElementById('input');
    if (input) input.value = text;
    const tab = activeTab();
    if (tab) { tab.draft = text; persistTabs(); }
    requestRender();
    setTimeout(() => send(), 50);
  } else {
    // Preserve any existing text in the input box
    const input = document.getElementById('input');
    const tab = activeTab();
    if (input && tab) { tab.draft = input.value || ''; persistTabs(); }
    requestRender();
  }
}

function startSilenceDetection() {
  if (silenceTimer) clearInterval(silenceTimer);
  silenceTimer = setInterval(() => {
    const elapsed = Date.now() - lastTranscriptTime;
    if (elapsed >= SILENCE_TIMEOUT_MS && voiceTranscript.trim()) {
      console.log(`[Voice] Auto-sending after ${SILENCE_TIMEOUT_MS}ms silence`);
      voiceAutoSend();
    } else if (elapsed >= MIC_IDLE_TIMEOUT_MS && !voiceTranscript.trim()) {
      console.log("[Voice] No speech detected — mic off after 15s");
      // Preserve any existing text in the input box before render
      const input = document.getElementById('input');
      const tab = activeTab();
      if (input && tab) { tab.draft = input.value || ''; persistTabs(); }
      cleanupVoice();
      voiceActive = false;
      requestRender();
    }
  }, 500);
}

function startVoice() {
  const tab = activeTab();
  if (!hasSpeechAPI) {
    const tab = activeTab();
    if (tab) tab.messages.push(_sysCard('Speech recognition is not available in this browser. Try Safari on iPhone/macOS or Chrome.', '🎙️', 'Voice'));
    requestRender({ preserveScroll: true });
    return;
  }
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  const recognition = new SpeechRec();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = 'en-US';

  voiceActive = true;
  voiceTranscript = '';
  lastTranscriptTime = Date.now();
  requestRender({ preserveScroll: true });
  // Focus textarea so voice transcript appears in the input box.
  // On iOS, set inputMode='none' before focus to prevent the keyboard from appearing.
  const inp = document.getElementById('input');
  if (inp) {
    if (isIOS) inp.inputMode = 'none';
    inp.focus();
  }

  recognition.onresult = (event) => {
    let final = '';
    let interim = '';
    for (let i = 0; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        final += event.results[i][0].transcript;
      } else {
        interim += event.results[i][0].transcript;
      }
    }
    const text = (final + interim).trim();
    if (text !== voiceTranscript) {
      lastTranscriptTime = Date.now();
    }
    voiceTranscript = text;
    const input = document.getElementById('input');
    if (input) input.value = voiceTranscript;
    // Sync to tab.draft so transcript survives any full re-render
    const _tab = activeTab();
    if (_tab) _tab.draft = voiceTranscript;
    // Use preserveScroll to avoid destroying textarea focus during voice input
    requestRender({ preserveScroll: true });
  };

  recognition.onerror = (event) => {
    console.warn('[Voice] Error:', event.error);
    if (event.error === 'no-speech' || event.error === 'aborted') return;
    cleanupVoice();
    voiceActive = false;
    const _errInp = document.getElementById('input');
    if (_errInp && isIOS) _errInp.inputMode = '';
    requestRender();
  };

  recognition.onend = () => {
    // Safari stops recognition after ~60s — restart if still recording
    if (voiceActive && speechRecognition === recognition) {
      try { recognition.start(); } catch {}
    }
  };

  recognition.start();
  speechRecognition = recognition;
  startSilenceDetection();
  beep();
  console.log('[Voice] Started (Web Speech API)');
}

function stopVoice(shouldSend) {
  const text = voiceTranscript.trim();
  cleanupVoice();
  voiceActive = false;
  voiceTranscript = '';
  // Restore normal inputMode so keyboard works for text editing
  const _inp = document.getElementById('input');
  if (_inp && isIOS) _inp.inputMode = '';
  if (shouldSend && text) {
    const input = document.getElementById('input');
    const tab = activeTab();
    if (input) input.value = text;
    if (tab) { tab.draft = text; persistTabs(); }
    requestRender({ preserveScroll: true });
    setTimeout(() => send(), 50);
  } else {
    requestRender({ preserveScroll: true });
  }
}

function cancelVoice() {
  // User clicked into textarea — cancel voice, keep transcript for editing
  const text = voiceTranscript;
  cleanupVoice();
  voiceActive = false;
  voiceTranscript = '';
  // Restore normal inputMode so keyboard works for text editing
  const _inp = document.getElementById('input');
  if (_inp && isIOS) _inp.inputMode = '';
  requestRender({ preserveScroll: true });
  setTimeout(() => {
    const input = document.getElementById('input');
    const tab = activeTab();
    if (input) { input.value = text; input.focus(); }
    if (tab) { tab.draft = text; persistTabs(); }
  }, 30);
}

function handleMicClick() {
  // Focus textarea so macOS dictation has an active target field.
  // On iOS, use inputMode='none' to get focus without keyboard popup.
  const inp = document.getElementById('input');
  if (inp) {
    if (isIOS) inp.inputMode = 'none';
    inp.focus();
  }
  if (!voiceActive) {
    startVoice();
  } else {
    stopVoice(true);
  }
}

// --- Global keyboard shortcuts ---
document.addEventListener('keydown', (e) => {
  const tag = (e.target.tagName || '').toLowerCase();
  const isTyping = tag === 'input' || tag === 'textarea';

  // Right arrow → start voice (when not typing)
  if (e.key === 'ArrowRight' && !isTyping && !voiceActive && !isTabLoading(activeTab()) && hasSpeechAPI) {
    e.preventDefault();
    startVoice();
    return;
  }
  // Enter → send voice transcript (when voice active and not in textarea)
  if (e.key === 'Enter' && voiceActive && !isTyping) {
    e.preventDefault();
    stopVoice(true);
    return;
  }
});

function handleActionBtn() {
  const input = document.getElementById('input');

  if (voiceActive) {
    stopVoice(true);
    return;
  }

  const tab = activeTab();
  if (input && input.value.trim()) {
    if (input) input.focus();
    send();
  } else if (!isTabLoading(tab)) {
    handleMicClick();
  }
}

function cancelGeneration(e) {
  if (e) e.stopPropagation();
  const tab = activeTab();
  if (!tab || !isTabLoading(tab)) return;
  const sid = tab.sessionId;
  // Mark as user-cancelled so the finally block in send() won't treat abort as reconnect
  tab._userCancelled = true;
  // Abort the client-side fetch stream
  if (tab.streamAbortController) {
    try { tab.streamAbortController.abort(); } catch {}
  }
  // Tell the server to cancel
  fetch(API + '/api/chat/cancel?session_id=' + encodeURIComponent(sid), { method: 'POST' }).catch(() => {});
}

function onInputChange(el) {
  const tab = activeTab();
  if (!tab || !el) return;
  tab.draft = el.value || '';
  persistTabs();
}

function updateInputBtn() {
  const input = document.getElementById('input');
  const btn = document.getElementById('action-btn');
  if (!btn || !input) return;

  const tab = activeTab();
  const hasFiles = !!(tab && tab.pendingFiles && tab.pendingFiles.length);
  const hasText = input.value.trim().length > 0 || hasFiles;
  const listening = !!voiceActive;

  btn.classList.toggle('has-text', hasText);
  btn.classList.toggle('voice-active', listening);

  if (listening) {
    btn.title = 'Send voice message';
    btn.setAttribute('aria-label', 'Send voice message');
  } else if (hasText) {
    btn.title = 'Send message';
    btn.setAttribute('aria-label', 'Send message');
  } else {
    btn.title = 'Start voice input';
    btn.setAttribute('aria-label', 'Start voice input');
  }
}

// --- SSE ---
const toolLabels = {
  bash:'⚙️ bash', bash_background:'⚙️ bash_bg', bash_check:'⚙️ bash_check',
  read_file:'📄 read', write_file:'✏️ write', edit_file:'✏️ edit',
  memory_search:'🔍 memory', memory_read:'📄 memory', spawn_agent:'🤖 agent', _thinking:'🧠',
  // Claude CLI tool names (capitalized)
  Bash:'⚙️ bash', Read:'📄 read', Write:'✏️ write', Edit:'✏️ edit',
  Grep:'🔍 grep', Glob:'🔍 glob', WebSearch:'🌐 search', WebFetch:'🌐 fetch',
  TodoRead:'📋 todo', TodoWrite:'📋 todo',
};

function handleEvent(evt, fullTextRef, tabId) {
  // Track last sequence per-tab only to avoid cross-tab resume bleed.
  const tab = getTab(tabId);
  if (!tab) return;
  // Track last SSE event time for safety timeout
  _lastSseEventTime[tabId] = Date.now();
  if (evt.seq) tab.lastSeq = Math.max(Number(tab.lastSeq || 0), Number(evt.seq || 0));

  const evtRunId = String(evt?.run_id || '');
  if (evtRunId) {
    if (!tab.currentRunId) tab.currentRunId = evtRunId;
    if (tab.currentRunId && evtRunId !== tab.currentRunId) return;
  }

  const isVisibleTab = activeTabId === tabId;

  // Background tab badges:
  // - ⚠️ when assistance/elevation is required OR tool errors happen
  // - blue unread dot when a new reply completes (unless alert is active)
  if (!isVisibleTab) {
    if (evt.type === 'elevation_required') {
      tab.needsAttention = true;
      tab.unread = false;
      persistTabs();
      requestRender({ preserveScroll: true });
    } else if (evt.type === 'error') {
      tab.needsAttention = true;
      tab.unread = false;
      persistTabs();
      requestRender({ preserveScroll: true });
    } else if (evt.type === 'done' && !tab.needsAttention) {
      tab.unread = true;
      persistTabs();
      requestRender({ preserveScroll: true });
    }
  }

  if (evt.type === 'text' || evt.type === 'chunk') {
    fullTextRef.text += evt.text || '';
    tab.streamingText = fullTextRef.text;
    if (!tab.tpsStateStartTime) tab.tpsStateStartTime = Date.now();
    tab.tpsStateChars = Number(tab.tpsStateChars || 0) + (evt.text || '').length;
    const elapsed = (Date.now() - Number(tab.tpsStateStartTime || 0)) / 1000;
    if (elapsed > 0.5) tab.tps = Math.round((Number(tab.tpsStateChars || 0) / 4) / elapsed);

    if (isVisibleTab) {
      // Ensure first token creates streaming bubble once; then patch bubble only.
      if (fullTextRef.text.length <= (evt.text || '').length + 2) {
        // First token — full render to create streaming bubble.
        // If user is at the bottom, stick there; otherwise preserve their scroll position.
        if (autoStickBottom) {
          requestRender({ forceStickBottom: true });
        } else {
          requestRender({ preserveScroll: true });
        }
      } else {
        renderMessageStreamOnly();
      }
    }
  } else if (evt.type === 'thinking' || evt.type === 'thinking_summary') {
    const chunk = String(evt.text || '');
    tab.thinkingText = (tab.thinkingText || '') + chunk;
    if (isVisibleTab) renderMessageStreamOnly();
  } else if (evt.type === 'thinking_start') {
    addWork(tabId, { type: 'status', text: 'Thinking…' });
  } else if (evt.type === 'ping' && evt.tool) {
    // Claude SSE: tool calls come as ping events with tool name
    const label = toolLabels[evt.tool] || toolLabels[evt.tool?.toLowerCase()] || `🔧 ${evt.tool}`;
    tab.activeToolLabel = label;
    tab.activeToolDetail = (evt.detail||'').slice(0,180) || null;
    addWork(tabId, { type: 'tool_call', label, detail: (evt.detail||'').slice(0,180) });
  } else if (evt.type === 'tool_use') {
    const label = toolLabels[evt.name] || `🔧 ${evt.name}`;
    tab.activeToolLabel = label;
    tab.activeToolDetail = (evt.input||'').slice(0,180) || null;
    addWork(tabId, { type: 'tool_call', label, detail: (evt.input||'').slice(0,180) });
  } else if (evt.type === 'tool_result') {
    addWork(tabId, { type: 'tool_result', label: toolLabels[evt.name] || `🔧 ${evt.name}`, detail: (evt.output||'').slice(0,180) });
  } else if (evt.type === 'subagent_tool_use') {
    const label = `🤖 ${toolLabels[evt.name]||evt.name}`;
    tab.activeToolLabel = label;
    tab.activeToolDetail = (evt.input||'').slice(0,180) || null;
    addWork(tabId, { type: 'tool_call', label, detail: (evt.input||'').slice(0,180) });
  } else if (evt.type === 'subagent_tool_result') {
    addWork(tabId, { type: 'tool_result', label: `🤖 ${toolLabels[evt.name]||evt.name}`, detail: (evt.output||'').slice(0,180) });
  } else if (evt.type === 'elevation_required') {
    tab.elevation = evt;
    tab.approveAllVisible = true;  // Show auto-approve button for this tab from now on
    addWork(tabId, { type: 'status', text: 'Elevation required' });
    beep();
    if (isVisibleTab) requestRender();
  } else if (evt.type === 'elevation_approved') {
    tab.elevation = null;
    tab.needsAttention = false;
    addWork(tabId, { type: 'status', text: 'Approved' });
    persistTabs();
    if (isVisibleTab) requestRender();
  } else if (evt.type === 'elevation_denied') {
    tab.elevation = null;
    tab.needsAttention = false;
    addWork(tabId, { type: 'status', text: 'Denied' });
    persistTabs();
    if (isVisibleTab) requestRender();
  } else if (evt.type === 'context') {
    tab.contextInfo = { tokens: evt.tokens, max: evt.max, pct: Math.round((evt.pct||0)*100), source: evt.source || tab?.contextInfo?.source || '' };
  } else if (evt.type === 'error') {
    if (isVisibleTab) {
      tab.needsAttention = true;
      tab.unread = false;
      persistTabs();
    }
    const msg = evt.message || evt.error || 'Unknown error';
    fullTextRef.text += `\n\n⚠️ ${msg}`;
    tab.streamingText = fullTextRef.text;
    addWork(tabId, { type: 'status', text: `Error: ${msg}` });
    if (msg.toLowerCase().includes('safety cap')) {
      safetyCapMessage = msg;
      safetyCapTabId = tabId;
    }
    if (isVisibleTab) requestRender();
  } else if (evt.type === 'progress') {
    addWork(tabId, { type: 'status', text: evt.message || 'In progress…' });
  } else if (evt.type === 'done') {
    // Snapshot reasoning + tokens — do NOT clear thinkingText or trigger renders yet.
    // The caller (finalizeTurn / EventSource done handler) will do the cleanup after
    // persisting the final message, preventing the reasoning-disappears race.
    tab._doneThinking = tab.thinkingText || '';
    const dt = evt.text || '';
    if (dt && dt.length > (fullTextRef.text||'').length) { fullTextRef.text = dt; tab.streamingText = dt; }
    tab.activeToolLabel = null; tab.activeToolDetail = null;
    if (evt.tokens) tab._doneTokens = evt.tokens;
    // NOTE: addWork('Done') is deferred — called by finalizeTurn after message is pushed,
    // so the pane refresh sees the saved message with reasoning attached.
  } else if (evt.type === 'user_message') {
    // Multi-browser sync: passive browser receives user message from active browser
    if (!tab._iAmSending && evt.text) {
      tab.messages.push({ id: evt.ts || Date.now(), role: 'user', text: evt.text, timestamp: evt.ts ? new Date(evt.ts) : new Date() });
      if (isVisibleTab) requestRender({ forceStickBottom: true });
    }
  }
}

function addWork(tabId, entry) {
  const tab = getTab(tabId);
  if (!tab) return;

  const newItem = { id: Date.now() + '-' + Math.random().toString(16).slice(2,8), ts: Date.now(), ...entry };
  const last = tab.workLog[tab.workLog.length - 1];

  const refreshWorkPane = () => {
    // Only repaint immediately if this is the visible tab.
    if (activeTabId !== tabId) return;

    const shouldJumpToNewest = (
      showWorkLog &&
      !firstToolJumpDone &&
      firstToolJumpTabId === tabId &&
      newItem.type === 'tool_call'
    );

    if (shouldJumpToNewest) firstToolJumpDone = true;

    // Update ONLY the work log host — do NOT rebuild the entire messages pane.
    // Full pane re-renders (forcePaneRefresh) during streaming destroy and recreate
    // every message bubble via innerHTML, causing visible blank flashes especially
    // in long conversations. The work log is a separate DOM element that can be
    // patched independently.
    if (streamDomInitialized) {
      const wlHost = document.getElementById('work-log-host');
      if (wlHost) wlHost.innerHTML = renderWorkLogFixed(tab);
      // If the work log just expanded, scroll it into view
      if (shouldJumpToNewest) {
        const el = document.getElementById('messages');
        if (el) el.scrollTop = el.scrollHeight;
      }
    } else {
      const lockWhileCollapsed = !showWorkLog && !shouldJumpToNewest;
      requestRender({
        preserveScroll: lockWhileCollapsed || !shouldJumpToNewest,
        forceStickBottom: shouldJumpToNewest,
      });
    }
  };

  // Collapse duplicate consecutive status/tool lines to avoid UI churn
  if (last && last.type === newItem.type) {
    const sameText = workLogText(last) === workLogText(newItem);
    const withinMs = (newItem.ts - (last.ts || 0)) < 1200;
    if (sameText && withinMs) {
      last.ts = newItem.ts;
      refreshWorkPane();
      return;
    }
  }

  // Track total tool calls (survives workLog trimming)
  if (newItem.type === 'tool_call') {
    tab.toolCallCount = (tab.toolCallCount || 0) + 1;
  }

  tab.workLog.push(newItem);

  // Cap display log at 100 entries to keep memory clean
  if (tab.workLog.length > 100) {
    tab.workLog = tab.workLog.slice(-100);
  }

  refreshWorkPane();
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function isServerRestartError(err) {
  const msg = String(err?.message || err || '').toLowerCase();
  return msg.includes('load failed') || msg.includes('failed to fetch') ||
    msg.includes('networkerror') || msg.includes('network request failed') ||
    msg.includes('connection refused');
}

async function waitForServerReconnect(tab, sentText) {
  // --- Server Restart Detection & Session Restoration ---
  // Called when a network error (connection refused, load failed) is detected during
  // send() — indicating the server has gone down (intentional restart or crash).
  //
  // Flow:
  //   1. Show "Server restarting — reconnecting..." system card
  //   2. Preserve the user's sent text in the draft field (so they can resend)
  //   3. Poll /api/status every 2s for up to 20s waiting for the server to come back
  //   4. Once back: show "Server back online" card, re-establish SSE EventSource
  //      connections so the tab receives proactive wake broadcasts and model responses
  //   5. If server doesn't come back: show error card
  //
  // After reconnection, the server-side _system_wake() will send [SYSTEM WAKE]
  // notifications to coordinator sessions, which triggers context injection and
  // resume of delegation monitoring.
  tab.messages.push(_sysCard('Server restarting — reconnecting...', '♻️', 'Connection'));
  tab.wasLoading = false;
  tab.loadingSinceMs = 0;
  tab._iAmSending = false;
  persistTabs();
  requestRender({ preserveScroll: true });

  // Preserve sent text so user can resend
  if (sentText) {
    tab.draft = sentText;
    const input = document.getElementById('input');
    if (input) input.value = sentText;
  }

  // Poll /api/status until server is back (up to ~20s)
  let connected = false;
  for (let i = 0; i < 10; i++) {
    await sleep(2000);
    try {
      const res = await fetch(API + '/api/status?session_id=' + encodeURIComponent(tab.sessionId || 'default'), {
        cache: 'no-store', signal: AbortSignal.timeout(3000),
      });
      if (res.ok) { connected = true; break; }
    } catch {}
  }

  if (connected) {
    tab.messages.push(_sysCard('Server back online', '✅', 'Connection'));
    // Re-establish EventSource connections
    if (_isClaudeModel(tab.modelKey)) {
      disconnectClaudeEventsForTab(tab);
      connectClaudeEventsForTab(tab);
    } else if (_isAnthropicModel(tab.modelKey)) {
      disconnectAnthropicEventsForTab(tab);
      connectAnthropicEventsForTab(tab);
    }
    // Re-establish global broadcast SSE
    disconnectGlobalEvents();
    connectGlobalEvents();
  } else {
    tab.messages.push(_sysCard('Could not reconnect — check if the server is running', '❌', 'Connection'));
  }

  persistTabs();
  requestRender({ preserveScroll: true });
  return connected;
}

async function fetchTaskStatus(sessionId) {
  try {
    const res = await fetch(API + '/api/status?session_id=' + encodeURIComponent(String(sessionId || 'default')), {
      cache: 'no-store',
      headers: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' },
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function fetchLatestAssistantFromHistory(tab, limit = 40) {
  if (!tab) return '';
  try {
    const res = await fetch(API + '/api/history?session_id=' + encodeURIComponent(tab.sessionId) + '&limit=' + Math.max(10, Math.min(Number(limit || 40), 200)), {
      cache: 'no-store',
      headers: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' },
    });
    if (!res.ok) return '';
    const data = await res.json().catch(() => ({}));
    const msgs = Array.isArray(data?.messages) ? data.messages : [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m && m.role === 'assistant' && String(m.content || '').trim()) return String(m.content || '');
    }
  } catch {}
  return '';
}

async function recoverEventSourceFinalFromHistory(tab) {
  // Claude/Anthropic EventSource streams do not replay missed events after reconnect.
  // If the final `done` event is lost, the UI can show a transient streaming bubble
  // that later gets cleared by the watchdog. Recover persisted final text from history
  // before clearing local loading state.
  if (!tab) return false;
  const streamedText = String(tab.streamingText || '').trim();
  if (!streamedText) return false; // avoid pulling stale older turns when no live stream exists

  const recoveredText = String(await fetchLatestAssistantFromHistory(tab, 50) || '').trim();
  if (!recoveredText) return false;
  if (recoveredText.length < streamedText.length) return false;

  const def = MODELS[tab.modelKey] || MODELS.codex;
  finalizeTurn(tab, recoveredText, def, { runId: tab.currentRunId || '' });
  return true;
}

async function resumeStreamForTab(tab, fullTextRef, opts = {}) {
  // --- Stream Resume After Brief Disconnection ---
  // When an SSE stream drops mid-response (brief network hiccup, not full restart),
  // this function attempts to reconnect using /api/resume?after_seq=N to replay
  // missed events from the server's in-memory event ring buffer.
  // If the run is no longer active, falls back to fetching the latest assistant
  // response from chat history to recover the final text.
  // This is distinct from waitForServerReconnect which handles full server restarts.
  if (!tab || tab.reconnecting) return false;
  tab.reconnecting = true;
  const attempts = Math.max(1, Number(opts.attempts || 3));
  const pauseMs = Math.max(250, Number(opts.pauseMs || 900));
  try {
    for (let i = 0; i < attempts; i++) {
      const afterSeq = Number(tab.lastSeq || 0);
      const url = API + '/api/resume?session_id=' + encodeURIComponent(tab.sessionId) + '&after_seq=' + encodeURIComponent(String(afterSeq));
      const res = await fetch(url, {
        cache: 'no-store',
        headers: { 'Accept': 'text/event-stream', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' },
      });
      if (res.ok && res.body) {
        const done = await processSseStream(res, fullTextRef, tab.id);
        if (done) return true;
      }

      const st = await fetchTaskStatus(tab.sessionId);
      if (st && st.status === 'running') {
        await sleep(pauseMs);
        continue;
      }

      // Not running anymore: if stream replay missed final chunk, recover from history.
      const recoveredText = await fetchLatestAssistantFromHistory(tab, 50);
      if (recoveredText && recoveredText.length > (fullTextRef?.text || '').length) {
        fullTextRef.text = recoveredText;
        tab.streamingText = recoveredText;
      }
      return (fullTextRef?.text || '').trim().length > 0;
    }
  } catch {}
  finally {
    tab.reconnecting = false;
  }
  return false;
}

async function processSseStream(res, fullTextRef, tabId) {
  let sawDone = false;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // Normalize CRLF to LF, then split on double-newline for proper SSE frame parsing
    buf = buf.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const frames = buf.split('\n\n');
    buf = frames.pop() || ''; // last element is incomplete frame
    for (const frame of frames) {
      // Collect all data: lines in this frame (SSE spec allows multi-line data)
      const dataLines = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('data: ')) dataLines.push(line.slice(6));
        else if (line.startsWith('data:')) dataLines.push(line.slice(5));
      }
      if (!dataLines.length) continue;
      try {
        const evt = JSON.parse(dataLines.join('\n'));
        if (evt.type === 'resume_end') {
          if (evt.reason === 'done') { sawDone = true; return true; }
          continue;
        }
        handleEvent(evt, fullTextRef, tabId);
        if (evt.type === 'done') { sawDone = true; return true; }
      } catch (e) { console.warn('SSE parse error:', e); }
    }
  }
  return sawDone;
}

// --- Friendly error messages ---
function friendlyError(errMsg, tab) {
  if (!errMsg) return errMsg;
  if (errMsg === 'run_in_progress') return 'Still processing your last message — please wait a moment.';
  if (/abort/i.test(errMsg)) return tab?._userCancelled ? null : 'Response cancelled.';
  return errMsg;
}

// --- Send ---
async function send() {
  const input = document.getElementById('input');
  const tab = activeTab();
  if (!input || !tab) return;
  // Clear any pending choice buttons when user sends
  const _lastAsst = (tab.messages || []).slice(-1)[0];
  if (_lastAsst && _lastAsst._choiceButtons) delete _lastAsst._choiceButtons;
  const text = (input.value || '').trim();
  const hasFiles = !!(tab.pendingFiles && tab.pendingFiles.length);
  if (!text && !hasFiles) return;
  // Wait for any in-flight FileReader operations to complete before sending
  if (hasFiles && _fileReadersLoading > 0) {
    const waitStart = Date.now();
    await new Promise(resolve => {
      const check = () => {
        if (_fileReadersLoading <= 0 || Date.now() - waitStart > 5000) resolve();
        else setTimeout(check, 50);
      };
      check();
    });
  }
  // Build attachments array from pending files — must match server's _validate_attachments() field names
  const attachments = hasFiles ? tab.pendingFiles.map(f => ({
    name: f.name,
    type: f.type,
    isImage: f.isImage,
    dataUrl: f.isImage ? (f.dataUrl || null) : null,
    textContent: !f.isImage ? (f.textContent ?? null) : null,
  })).filter(a => a.dataUrl || a.textContent !== null) : undefined;
  // Use placeholder text if only files attached
  const sendText = text || (hasFiles ? '[attached files]' : '');
  // Snapshot attachment display data for inline rendering in chat (ephemeral, not persisted)
  const _msgAttachments = hasFiles ? tab.pendingFiles.map(f => ({
    name: f.name, type: f.type, isImage: f.isImage,
    dataUrl: f.isImage ? (f.dataUrl || null) : null,
    textContent: !f.isImage ? (f.textContent ?? null) : null,
  })).filter(a => a.dataUrl || a.textContent !== null) : null;
  if (isTabLoading(tab)) {
    // Queue the message — show it immediately with a pending badge
    const pendingMsg = { id: Date.now() + Math.floor(Math.random() * 1000), role: 'user', text: sendText, timestamp: new Date(), _pending: true, _attachments: _msgAttachments };
    tab.messages.push(pendingMsg);
    _messageQueue.push({ tabId: tab.id, text: sendText, pendingMsgId: pendingMsg.id, pendingFiles: hasFiles ? [...tab.pendingFiles] : [] });
    input.value = '';
    tab.draft = '';
    tab.pendingFiles = [];
    _rerenderAttachmentBar();
    persistTabs();
    requestRender({ forceStickBottom: true });
    return;
  }

  if (tab.streamAbortController) {
    try { tab.streamAbortController.abort(); } catch {}
  }
  tab.streamAbortController = new AbortController();
  tab.streamEpoch = Number(tab.streamEpoch || 0) + 1;
  const myEpoch = tab.streamEpoch;
  tab.currentRunId = '';

  if (sendText.startsWith('/')) {
    const cmd = sendText.split(/\s+/)[0].slice(1).toLowerCase();
    if (cmd === 'help') { tab.messages.push(_sysCard('Use Settings → Health Update / Compact / Reset / Clear', '❓', 'Help')); tab.draft = ''; persistTabs(); input.value = ''; requestRender({ preserveScroll: true }); return; }
  }

  // Skip pushing a user message if this was drained from the queue (already shown)
  const wasDrained = tab._drainedText === sendText;
  const drainRetries = wasDrained ? (tab._drainRetries || 0) : 0;
  if (wasDrained) {
    delete tab._drainedText;
    delete tab._drainRetries;
  } else {
    tab.messages.push({ id: Date.now() + 1, role: 'user', text: sendText, timestamp: new Date(), _attachments: _msgAttachments });
  }
  tab._iAmSending = true;
  maybeAutoNameNewTab(tab);
  tab.wasLoading = true;
  tab.loadingSinceMs = Date.now();
  _lastSseEventTime[tab.id] = Date.now();  // Initialize for safety timeout
  tab.streamingText = '';
  tab.thinkingText = '';
  tab.thinkingExpanded = true;
  tab.workLog = []; tab.toolCallCount = 0;
  tab.activeToolLabel = null; tab.activeToolDetail = null;
  tab.elevation = null;
  statusBanner = null;
  statusBannerTabId = null;
  safetyCapMessage = null;
  safetyCapTabId = null;
  tab.tps = null;
  tab.tpsStateChars = 0;
  tab.tpsStateStartTime = 0;
  input.value = '';
  tab.draft = '';
  tab.pendingFiles = [];
  _rerenderAttachmentBar();
  persistTabs();
  requestRender({ forceStickBottom: true });

  const fullTextRef = { text: '' };
  const def = MODELS[tab.modelKey];
  let streamEnded = false;
  let handledRestart = false;  // set in catch to skip finalizeTurn in finally

  // Claude tabs: POST to bridge with inline SSE streaming.
  const isClaudeTab = _isClaudeModel(tab.modelKey);
  if (isClaudeTab) {
    const sid = tab.sessionId;
    _claudeIAmSending[sid] = true;
    try {
      const res = await fetch(API + '/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: sendText, session_id: sid, stream: true, ...(attachments ? { attachments } : {}) }),
        signal: tab.streamAbortController?.signal,
      });
      if (!res.ok) {
        if (check401(res)) return;
        const e = await res.json().catch(() => ({}));
        const errMsg = e.error || 'Request failed: ' + res.status;
        // If this was a drained (queued) message that hit a 409 race, retry (up to 3x)
        if (wasDrained && drainRetries < 3 && res.status === 409 && errMsg === 'run_in_progress') {
          tab._drainedText = sendText;
          tab._drainRetries = drainRetries + 1;
          tab.wasLoading = false;
          tab.loadingSinceMs = 0;
          setTimeout(() => _drainMessageQueue(tab), 500);
          return;
        }
        const displayErr = friendlyError(errMsg, tab) || errMsg;
        tab.messages.push({ id: Date.now() + 1, role: 'assistant', text: `⚠️ ${displayErr}`, timestamp: new Date(), modelLabel: def.shortName || def.name });
        tab.wasLoading = false;
        tab.loadingSinceMs = 0;
        persistTabs();
        requestRender();
        _drainMessageQueue(tab);
        return;
      }
      // Consume inline SSE via ReadableStream (proper frame parsing)
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done: rDone, value } = await reader.read();
        if (rDone) break;
        buf += dec.decode(value, { stream: true });
        buf = buf.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        const frames = buf.split('\n\n'); buf = frames.pop() || '';
        for (const frame of frames) {
          const dataLines = [];
          for (const line of frame.split('\n')) {
            if (line.startsWith('data: ')) dataLines.push(line.slice(6));
            else if (line.startsWith('data:')) dataLines.push(line.slice(5));
          }
          if (!dataLines.length) continue;
          try {
            const evt = JSON.parse(dataLines.join('\n'));
            if (myEpoch !== tab.streamEpoch) continue;
            handleEvent(evt, fullTextRef, tab.id);
            tab.streamingText = fullTextRef.text;
            if (evt.type === 'done') { streamEnded = true; tab._doneTokens = evt.tokens || null; }
          } catch (e) { console.warn('Claude inline SSE parse error:', e); }
        }
      }
    } catch (err) {
      if (isServerRestartError(err)) {
        handledRestart = true;
        const reconnected = await waitForServerReconnect(tab, sendText);
        _claudeIAmSending[sid] = false;
        if (tab.streamAbortController && myEpoch === tab.streamEpoch) tab.streamAbortController = null;
        // Reload history from server so the persisted response appears in tab.messages
        if (reconnected) {
          await loadTabHistory(tab);
          const lastUserMsg = [...(tab.messages || [])].reverse().find(m => m.role === 'user');
          if (lastUserMsg && lastUserMsg.text === sendText) {
            tab.draft = '';
            const inp = document.getElementById('input');
            if (inp && activeTab()?.id === tab.id) inp.value = '';
            persistTabs();
          }
        }
        return;
      }
      if (fullTextRef.text === '') {
        console.warn('Claude send error:', err.message || err);
        const friendly = friendlyError(err.message || String(err), tab);
        if (friendly) fullTextRef.text = `⚠️ ${friendly}`;
      }
    } finally {
      delete tab._userCancelled;
      tab._iAmSending = false;
      if (firstToolJumpTabId === tab.id) { firstToolJumpDone = false; firstToolJumpTabId = null; }
      showWorkLog = false;
      if (!handledRestart) {
        finalizeTurn(tab, fullTextRef.text, def, { runId: tab.currentRunId || '' });
      }
      // Delay clearing _claudeIAmSending so EventSource done handler doesn't race
      setTimeout(() => { _claudeIAmSending[sid] = false; }, 50);
      if (tab.streamAbortController && myEpoch === tab.streamEpoch) tab.streamAbortController = null;
    }
    return;
  }

  // Anthropic tabs: POST to /api/chat with inline SSE, EventSource catches background events
  const isAnthropicTab = _isAnthropicModel(tab.modelKey);
  if (isAnthropicTab) {
    const sid = tab.sessionId;
    _anthropicIAmSending[sid] = true;
    try {
      const res = await fetch(API + '/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: sendText, session_id: sid, ...(attachments ? { attachments } : {}) }),
        signal: tab.streamAbortController?.signal,
      });
      if (!res.ok) {
        if (check401(res)) return;
        const e = await res.json().catch(() => ({}));
        const errMsg = e.error || 'Request failed: ' + res.status;
        // If this was a drained (queued) message that hit a 409 race, retry (up to 3x)
        if (wasDrained && drainRetries < 3 && res.status === 409 && errMsg === 'run_in_progress') {
          tab._drainedText = sendText;
          tab._drainRetries = drainRetries + 1;
          tab.wasLoading = false;
          tab.loadingSinceMs = 0;
          _anthropicIAmSending[sid] = false;
          setTimeout(() => _drainMessageQueue(tab), 500);
          handledRestart = true;
          return;
        }
        throw new Error(errMsg);
      }
      const done = await processSseStream(res, fullTextRef, tab.id);
      if (done) streamEnded = true;
    } catch (err) {
      if (isServerRestartError(err)) {
        handledRestart = true;
        const reconnected = await waitForServerReconnect(tab, sendText);
        _anthropicIAmSending[sid] = false;
        if (tab.streamAbortController && myEpoch === tab.streamEpoch) tab.streamAbortController = null;
        if (reconnected) {
          await loadTabHistory(tab);
          const lastUserMsg = [...(tab.messages || [])].reverse().find(m => m.role === 'user');
          if (lastUserMsg && lastUserMsg.text === sendText) {
            tab.draft = '';
            const inp = document.getElementById('input');
            if (inp && activeTab()?.id === tab.id) inp.value = '';
            persistTabs();
          }
        }
        return;
      }
      // If inline stream dropped, EventSource will pick up the rest
      if (fullTextRef.text === '') {
        console.warn('Anthropic send error:', err.message || err);
        const friendly = friendlyError(err.message || String(err), tab);
        if (friendly) fullTextRef.text = `⚠️ ${friendly}`;
      }
    } finally {
      delete tab._userCancelled;
      tab._iAmSending = false;
      if (firstToolJumpTabId === tab.id) { firstToolJumpDone = false; firstToolJumpTabId = null; }
      showWorkLog = false;
      if (!handledRestart) {
        finalizeTurn(tab, fullTextRef.text, def, { runId: tab.currentRunId || '' });
      }
      // Delay clearing _anthropicIAmSending so EventSource done handler doesn't race
      setTimeout(() => { _anthropicIAmSending[sid] = false; }, 50);
      if (tab.streamAbortController && myEpoch === tab.streamEpoch) tab.streamAbortController = null;
    }
    return;
  }

  // Non-Claude/non-Anthropic tabs (Codex, OpenRouter): original request-scoped streaming.
  // These don't use persistent EventSource, so stream drops require explicit recovery.
  try {
    const res = await fetch(API + '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: sendText, session_id: tab.sessionId, ...(attachments ? { attachments } : {}) }),
      signal: tab.streamAbortController?.signal,
    });
    if (!res.ok) {
      if (check401(res)) return;
      const e = await res.json().catch(() => ({}));
      const errMsg = e.error || 'Request failed: ' + res.status;
      // If this was a drained (queued) message that hit a 409 race, retry (up to 3x)
      if (wasDrained && drainRetries < 3 && res.status === 409 && errMsg === 'run_in_progress') {
        tab._drainedText = sendText;
        tab._drainRetries = drainRetries + 1;
        tab.wasLoading = false;
        tab.loadingSinceMs = 0;
        setTimeout(() => _drainMessageQueue(tab), 500);
        handledRestart = true; // skip finalizeTurn in finally
        return;
      }
      throw new Error(errMsg);
    }
    const done = await processSseStream(res, fullTextRef, tab.id);
    if (done) streamEnded = true;
  } catch (err) {
    if (isServerRestartError(err)) {
      // Two-tier recovery for network errors:
      //   1. resumeStreamForTab — tries /api/resume to replay missed SSE events
      //      from the server's in-memory ring buffer (handles brief disconnects)
      //   2. waitForServerReconnect — polls /api/status until server is back
      //      (handles full server restarts where ring buffer is gone)
      const recovered = await resumeStreamForTab(tab, fullTextRef, { attempts: 4, pauseMs: 900 });
      if (!recovered) {
        const st = await fetchTaskStatus(tab.sessionId);
        if (st && st.status === 'running') {
          // Server is up but stream replay didn't get everything — leave loading
          // state active so the periodic watchdog keeps attempting resumes.
          tab.activeToolLabel = 'Reconnecting stream…'; tab.activeToolDetail = null;
          persistTabs();
          requestRender({ preserveScroll: true });
          handledRestart = true;
          return;
        }
        // Resume failed AND server unreachable — likely a full restart
        if (!st) {
          handledRestart = true;
          const reconnected = await waitForServerReconnect(tab, sendText);
          if (tab.streamAbortController && myEpoch === tab.streamEpoch) tab.streamAbortController = null;
          if (reconnected) {
            await loadTabHistory(tab);
            const lastUserMsg = [...(tab.messages || [])].reverse().find(m => m.role === 'user');
            if (lastUserMsg && lastUserMsg.text === sendText) {
              tab.draft = '';
              const inp = document.getElementById('input');
              if (inp && activeTab()?.id === tab.id) inp.value = '';
              persistTabs();
            }
          }
          return;
        }
      }
    } else if (fullTextRef.text === '') {
      console.warn('Send error:', err.message || err);
      const friendly = friendlyError(err.message || String(err), tab);
      if (friendly) fullTextRef.text = `⚠️ ${friendly}`;
    }
  } finally {
    if (handledRestart) return;
    const cancelled = !!tab._userCancelled;
    if (cancelled) delete tab._userCancelled;
    const st = (streamEnded || cancelled) ? { status: 'done' } : await fetchTaskStatus(tab.sessionId);
    if (st && st.status === 'running') {
      tab.wasLoading = true;
      if (!tab.loadingSinceMs) tab.loadingSinceMs = Date.now();
      if (!tab.streamingText) tab.streamingText = fullTextRef.text || '';
      tab.activeToolLabel = tab.activeToolLabel || 'Reconnecting stream…';
      persistTabs();
      requestRender({ preserveScroll: true });
      return;
    }

    tab._iAmSending = false;
    if (firstToolJumpTabId === tab.id) {
      firstToolJumpDone = false;
      firstToolJumpTabId = null;
    }
    showWorkLog = false;
    finalizeTurn(tab, fullTextRef.text, def, { runId: tab.currentRunId || '' });
    if (tab.streamAbortController && myEpoch === tab.streamEpoch) tab.streamAbortController = null;
  }
}

function quickSend(text) {
  const tab = activeTab();
  if (!tab) return;
  const input = document.getElementById('input');
  if (input) input.value = text;
  tab.draft = text;
  persistTabs();
  send();
}
// --- Message Queue Helpers ---
function _cancelQueuedMessage(msgId) {
  const tab = activeTab();
  if (!tab) return;
  // Remove from queue
  const qIdx = _messageQueue.findIndex(q => q.pendingMsgId === msgId);
  if (qIdx !== -1) _messageQueue.splice(qIdx, 1);
  // Remove from chat messages
  const mIdx = (tab.messages || []).findIndex(m => m.id === msgId);
  if (mIdx !== -1) tab.messages.splice(mIdx, 1);
  persistTabs();
  requestRender({ preserveScroll: true });
}

function _queueCountForTab(tab) {
  if (!tab) return 0;
  return _messageQueue.filter(q => q.tabId === tab.id).length;
}

function _drainMessageQueue(tab) {
  if (!tab) return;
  const idx = _messageQueue.findIndex(q => q.tabId === tab.id);
  if (idx === -1) return;
  // Only auto-send if this is the active tab (send() operates on activeTab)
  if (activeTabId !== tab.id) return;
  const queued = _messageQueue.splice(idx, 1)[0];
  // Clear pending badge on the existing message
  if (queued.pendingMsgId) {
    const pm = (tab.messages || []).find(m => m.id === queued.pendingMsgId);
    if (pm) delete pm._pending;
  }
  // Mark tab so send() skips pushing a duplicate user message
  tab._drainedText = queued.text;
  // Restore queued file attachments so send() picks them up
  if (queued.pendingFiles && queued.pendingFiles.length) {
    tab.pendingFiles = queued.pendingFiles;
  }
  // Put the queued message into the input and fire send()
  const input = document.getElementById('input');
  if (input) input.value = queued.text;
  tab.draft = queued.text;
  persistTabs();
  // Use setTimeout to let finalizeTurn's render complete before sending
  setTimeout(() => send(), 100);
}

function dismissCap() {
  safetyCapMessage = null;
  safetyCapTabId = null;
  requestRender({ preserveScroll: true });
}

// --- Elevation ---
async function handleElevation(action) {
  const tab = activeTab();
  const elev = tabElevation(tab);
  if (!tab || !elev) return;
  try { await fetch(API + '/api/elevate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: elev.request_id, action }) }); } catch {}
  tab.elevation = null;
  persistTabs();
  requestRender();
}

async function handleElevationAllowAll() {
  // Approve this one + turn on auto-approve for this tab only
  const tab = activeTab();
  const elev = tabElevation(tab);
  if (!tab) return;
  if (elev) {
    try { await fetch(API + '/api/elevate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: elev.request_id, action: 'approve' }) }); } catch {}
  }
  tab.approveAll = true;
  tab.approveAllVisible = true;
  try { await fetch(API + '/api/approve-all', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: tab.sessionId || 'default', enabled: true }) }); } catch {}
  tab.elevation = null;
  persistTabs();
  requestRender();
}

// --- Controls ---
async function toggleApproveAll() {
  const tab = activeTab();
  if (!tab) return;
  const next = !tab.approveAll;
  tab.approveAll = next;
  // Hide the button entirely when turned off — it reappears on the next elevation event
  tab.approveAllVisible = next;
  requestRender({ preserveScroll: true });
  try {
    await fetch(API + '/api/approve-all', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: tab.sessionId || 'default', enabled: next }) });
  } catch {
    tab.approveAll = !next;
    tab.approveAllVisible = !next;
    requestRender({ preserveScroll: true });
  }
  persistTabs();
}

function promptRoot() {
  showSettings = false;
  if (elevatedSession.enabled) {
    // Already active — disable it
    disableRoot();
  } else {
    showRootWarning = true;
  }
  requestRender();
}

async function confirmRoot() {
  showRootWarning = false;
  requestRender({ preserveScroll: true });
  const tab = activeTab();
  const sid = tab?.sessionId || 'default';

  // Keep app-level elevated session for existing guardrail bypass behavior.
  try {
    const res = await fetch(API + '/api/elevated-session', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid, enabled: true, ttl_seconds: 1800 })
    });
    const data = await res.json();
    elevatedSession = { enabled: Boolean(data.enabled), remaining_seconds: Number(data.remaining_seconds || 0) };
  } catch {
    elevatedSession = { enabled: false, remaining_seconds: 0 };
  }

  // Request OS-privileged helper to write temporary sudoers rule.
  try {
    const pres = await fetch(API + '/api/privileged/elevate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid, ttl_seconds: 1800 })
    });
    const pdata = await pres.json().catch(() => ({}));
    if (!pres.ok || !pdata.ok) {
      if (tab) {
        tab.messages.push(_sysCard(`Root elevation failed: ${pdata.error || ('HTTP ' + pres.status)}`, '⚠️', 'Root'));
      }
    } else if (tab) {
      tab.messages.push(_sysCard('Root mode enabled — passwordless sudo for 30 minutes.', '🔓', 'Root'));
    }
  } catch (e) {
    if (tab) {
      tab.messages.push(_sysCard(`Privileged helper unavailable: ${e?.message || e}. Is the helper daemon running?`, '⚠️', 'Root'));
    }
  }

  persistTabs();
  requestRender({ preserveScroll: true });
}

async function disableRoot() {
  elevatedSession = { enabled: false, remaining_seconds: 0 };
  const tab = activeTab();
  const sid = tab?.sessionId || 'default';
  try {
    await fetch(API + '/api/elevated-session', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid, enabled: false })
    });
  } catch {}
  try {
    await fetch(API + '/api/privileged/revoke', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid })
    });
  } catch {}
  requestRender();
}

function updateReasoningPickerUI() {
  const host = document.getElementById('reasoning-picker-host');
  if (!host) return;
  host.innerHTML = renderReasoningPickerButtons();
}

async function setReasoning(level) {
  const allowNone = supportsNoReasoningForTab();
  const allowed = allowNone ? ['none', 'low', 'medium', 'high'] : ['low', 'medium', 'high'];
  if (!allowed.includes(level)) return;
  reasoningEffort = level;
  updateReasoningPickerUI();
  try { await fetch(API + '/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reasoning_effort: level }) }); } catch {}
}

function updateSettingsUI() {
  requestRender({ preserveScroll: true });
  if (showSettings) setTimeout(() => document.addEventListener('click', closeSettingsOnClickOutside, { once: true }), 0);
}

function updateModelSelectorsUI() {
  const tab = activeTab();
  if (!tab) return;

  document.querySelectorAll('.tab-item').forEach(el => {
    if (!el) return;
    if (el.getAttribute('data-tab-id') === tab.id) el.classList.add('active');
    else el.classList.remove('active');
  });

  // Update mobile worker picker label
  const mwp = document.querySelector('.mobile-worker-picker');
  if (mwp) {
    const def = MODELS[tab.modelKey] || MODELS.codex;
    const iconEl = mwp.querySelector('.mwp-icon');
    const logoEl = mwp.querySelector('.mwp-model-logo');
    const labelEl = mwp.querySelector('.mwp-label');
    if (iconEl) iconEl.innerHTML = _getWorkerIcon(tab);
    if (logoEl) logoEl.innerHTML = def.emoji || '';
    if (labelEl) labelEl.textContent = tab.label;
    // Update model class
    Object.keys(MODELS).forEach(k => mwp.classList.remove('model-' + k));
    mwp.classList.add('model-' + tab.modelKey);
  }

  const statusLabel = document.querySelector('.status-bar span');
  if (statusLabel) {
    const pct = tab?.contextInfo?.pct || 0;
    const ctxColor = pct > 90 ? 'var(--red)' : pct > 70 ? 'var(--yellow)' : 'var(--green)';
    const ctxStr = tab?.contextInfo ? fmtCtx(tab.contextInfo) : '';
    const tpsStr = (tabIsStreamOwner(tab) && tab?.tps != null) ? ` · ${tab.tps} t/s` : '';
    const elevMin = Math.max(0, Math.ceil((elevatedSession.remaining_seconds || 0) / 60));
    statusLabel.innerHTML = `Context:${ctxStr ? ` <span style="color:${ctxColor}">${ctxStr}</span>` : ' --'}${tpsStr}`;
  }
  const workerBadge = document.getElementById('worker-name-badge');
  if (workerBadge) workerBadge.textContent = _getWorkerDisplayName(tab);
}

function toggleSettings() {
  showSettings = !showSettings;
  // In editor mode, toggle the dropdown via direct DOM manipulation
  // to avoid a full render() that would destroy the Ace editor instance.
  if (appMode === 'editor') {
    _toggleEditorSettingsDOM();
    return;
  }
  updateSettingsUI();
}

function _toggleEditorSettingsDOM() {
  // Desktop sidebar dropdown
  const navEl = document.querySelector('.sidebar-nav');
  const chevron = document.querySelector('.header-chevron');
  if (showSettings && !navEl) {
    const headerWrap = document.querySelector('.sidebar-header-wrap');
    if (headerWrap) {
      const nav = document.createElement('div');
      nav.className = 'sidebar-nav';
      nav.innerHTML = `
        <div class="sidebar-submenu-wrap">
          <button class="sidebar-nav-item" onclick="event.stopPropagation();this.nextElementSibling.classList.toggle('show')"><span class="sidebar-nav-icon">&#127912;</span>Theme &#9656;</button>
          <div class="sidebar-submenu">
            ${THEMES.map(t => {
              const active = (localStorage.getItem(LS_THEME_KEY) || 'blue') === t;
              return `<button class="sidebar-theme-item${active ? ' active-theme' : ''}" onclick="applyTheme('${t}');_toggleEditorSettingsDOM();showSettings=false;">${active ? '\u25cf ' : '\u25cb '}${THEME_LABELS[t]}</button>`;
            }).join('')}
          </div>
        </div>
        <button class="sidebar-nav-item" onclick="forceRefreshApp()"><span class="sidebar-nav-icon">&#128260;</span>Reload App</button>
        <a href="/settings-v2.html" class="sidebar-nav-item"><span class="sidebar-nav-icon">&#9881;</span>Settings</a>
      `;
      headerWrap.after(nav);
    }
  } else if (!showSettings && navEl) {
    navEl.remove();
  }
  if (chevron) chevron.textContent = showSettings ? '\u25B4' : '\u25BE';
  // Mobile dropdown
  const mobileNav = document.querySelector('.mobile-settings-nav');
  if (showSettings && !mobileNav) {
    const mobileBar = document.querySelector('.mobile-bar');
    if (mobileBar) {
      const nav = document.createElement('div');
      nav.className = 'mobile-settings-nav';
      nav.innerHTML = `
        <div class="sidebar-submenu-wrap">
          <button class="sidebar-nav-item" onclick="event.stopPropagation();this.nextElementSibling.classList.toggle('show')"><span class="sidebar-nav-icon">&#127912;</span>Theme &#9656;</button>
          <div class="sidebar-submenu">
            ${THEMES.map(t => {
              const active = (localStorage.getItem(LS_THEME_KEY) || 'blue') === t;
              return `<button class="sidebar-theme-item${active ? ' active-theme' : ''}" onclick="applyTheme('${t}');_toggleEditorSettingsDOM();showSettings=false;">${active ? '\u25cf ' : '\u25cb '}${THEME_LABELS[t]}</button>`;
            }).join('')}
          </div>
        </div>
        <button class="sidebar-nav-item" onclick="forceRefreshApp()"><span class="sidebar-nav-icon">&#128260;</span>Reload App</button>
        <a href="/settings-v2.html" class="sidebar-nav-item"><span class="sidebar-nav-icon">&#9881;</span>Settings</a>
      `;
      mobileBar.appendChild(nav);
    }
  } else if (!showSettings && mobileNav) {
    mobileNav.remove();
  }
  if (showSettings) setTimeout(() => document.addEventListener('click', closeSettingsOnClickOutside, { once: true }), 0);
}
async function _loadAvailableWorkers() {
  if (_availableWorkers.length > 0) return;
  try {
    const res = await fetch(API + '/api/workers');
    const data = await res.json();
    _availableWorkers = data.workers || [];
    if (data.workers_dir) _workersDir = data.workers_dir;
  } catch {}
}

async function openNewWorkerModal(modelKey) {
  await Promise.all([
    _loadOpenRouterModels(),
    _loadAnthropicModels(),
    _loadAvailableWorkers(),
    pollClaudeBridgeHealth(),
  ]);
  // Default to Developer worker type, auto-select best model for it
  newWorkerIdentityKey = 'developer';
  newWorkerModelKey = _bestModelForWorker(newWorkerIdentityKey);
  newWorkerError = '';
  showNewWorkerModal = true;
  requestRender({ preserveScroll: true });
  setTimeout(() => {
    const sel = document.getElementById('new-worker-identity-select');
    if (sel) sel.focus();
  }, 30);
}

function cancelNewWorkerModal() {
  showNewWorkerModal = false;
  newWorkerError = '';
  requestRender({ preserveScroll: true });
}

// --- Drag-to-reorder tab handlers ---
let dragTabId = null;
function onTabDragStart(e, tabId) {
  dragTabId = tabId;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', tabId);
  requestAnimationFrame(() => {
    const el = document.querySelector(`.tab-item[data-tab-id="${tabId}"]`);
    if (el) el.classList.add('dragging');
  });
}
function onTabDragOver(e, idx) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.tab-item').forEach(el => el.classList.remove('drag-over'));
  e.currentTarget.classList.add('drag-over');
}
function onTabDrop(e, targetIdx) {
  e.preventDefault();
  document.querySelectorAll('.tab-item').forEach(el => el.classList.remove('drag-over', 'dragging'));
  if (!dragTabId) return;
  const srcIdx = tabs.findIndex(t => t.id === dragTabId);
  if (srcIdx === -1 || srcIdx === targetIdx) { dragTabId = null; return; }
  const [moved] = tabs.splice(srcIdx, 1);
  tabs.splice(targetIdx, 0, moved);
  _sortManagerTabsFirst();
  dragTabId = null;
  persistTabs();
  requestRender({ preserveScroll: true });
}
function onTabDragEnd(e) {
  dragTabId = null;
  document.querySelectorAll('.tab-item').forEach(el => el.classList.remove('dragging', 'drag-over'));
}

// --- Worker card hover tooltip ---
function showTabTip(e) {
  if (dragTabId) return;
  const card = e.currentTarget;
  const tip = card.querySelector('.tab-hover-tip');
  if (!tip) return;
  const rect = card.getBoundingClientRect();
  tip.style.display = 'block';
  tip.style.left = (rect.right + 8) + 'px';
  tip.style.top = (rect.top + rect.height / 2 - tip.offsetHeight / 2) + 'px';
  tip.style.opacity = '1';
}
function hideTabTip(e) {
  const tip = e.currentTarget.querySelector('.tab-hover-tip');
  if (tip) { tip.style.opacity = '0'; setTimeout(() => { tip.style.display = 'none'; }, 120); }
}

async function confirmNewWorkerModal() {
  // Read values from dropdowns in case onchange didn't fire (e.g. default selected)
  const sel = document.getElementById('new-worker-model-select');
  if (sel) newWorkerModelKey = sel.value;
  const identSel = document.getElementById('new-worker-identity-select');
  if (identSel) newWorkerIdentityKey = identSel.value;
  const def = MODELS[newWorkerModelKey] || MODELS.codex;
  // Use worker identity name as label prefix if set; number by worker type
  const workerDef = _availableWorkers.find(w => w.key === newWorkerIdentityKey);
  const sameWorkerCount = tabs.filter(t => (t.workerIdentity || '') === (newWorkerIdentityKey || '')).length;
  const nextNum = sameWorkerCount + 1;
  const name = workerDef ? `${workerDef.name} ${nextNum}` : `${def.shortName} ${nextNum}`;

  // Phase C: fast UX enforcement — check max-session status before creating.
  try {
    const st = await fetch(API + '/api/max/status', { headers: { 'Accept': 'application/json' } })
      .then(r => r.json()).catch(() => null);
    const limits = st?.limits || {};
    const atTotal = !!st?.at_total_limit;
    const atCodex = !!st?.at_codex_limit;
    const atSpark = !!st?.at_spark_limit;

    if (atTotal || (newWorkerModelKey === 'codex' && atCodex) || (newWorkerModelKey === 'spark' && atSpark)) {
      const lt = Number(limits.max_total_sessions || 0);
      const lc = Number(limits.max_codex_sessions || 0);
      const ls = Number(limits.max_spark_sessions || 0);
      const msg = (atTotal && lt > 0)
        ? `Max sessions reached (${lt}). Close a worker to continue.`
        : (newWorkerModelKey === 'codex' && lc > 0)
          ? `Max Codex sessions reached (${lc}). Close a worker to continue.`
          : (newWorkerModelKey === 'spark' && ls > 0)
            ? `Max Spark sessions reached (${ls}). Close a worker to continue.`
            : `Max sessions limit reached. Close a worker to continue.`;
      newWorkerError = msg;
      requestRender({ preserveScroll: true });
      return;
    }
  } catch {}

  const t = createTab(newWorkerModelKey, { label: name, explicit: true, workerIdentity: newWorkerIdentityKey });
  if (t?.id) switchTab(t.id);

  // Save worker identity to server (await to ensure tab_meta row exists before first chat)
  if (t?.sessionId && newWorkerIdentityKey) {
    try {
      await fetch(API + '/api/tab/worker-identity', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: t.sessionId, worker_identity: newWorkerIdentityKey, model_key: newWorkerModelKey }),
      });
    } catch {}
  }

  // Per-worker OpenRouter model pinning (for specialized OpenRouter worker types).
  try {
    const tabModel = MODELS[newWorkerModelKey] || null;
    const openrouterModel = String(tabModel?.openrouterModel || '').trim();
    if (t?.sessionId && openrouterModel) {
      await fetch(API + '/api/openrouter/session-model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: t.sessionId, model: openrouterModel }),
      });
    }
  } catch {}

  showNewWorkerModal = false;
  newWorkerError = '';
  requestRender({ preserveScroll: true });
}

function createTabFromUI(modelKey) {
  openNewWorkerModal(modelKey);
}

function showMobileAddMenu() {
  // Keep behavior consistent with mobile '+' buttons.
  const last = tabs.length > 0 ? tabs[tabs.length - 1].modelKey : null;
  const next = last === 'codex' ? 'spark' : 'codex';
  createTabFromUI(next);
}

function handleMobileTabSelect(sel, modelKey) {
  if (!sel) return;
  const v = sel.value || '';
  if (v.startsWith('switch-')) {
    return;
  }
  if (v.startsWith('create-')) {
    if (isNarrowScreen()) {
      openNewWorkerModal(modelKey);
      return;
    }
    const t = createTab(modelKey, { explicit: true });
    if (t?.id) switchTab(t.id);
    return;
  }
  if (v) switchTab(v);
}

function toggleMobileWorkerMenu(e) {
  e.stopPropagation();
  showMobileWorkerMenu = !showMobileWorkerMenu;
  if (showSettings) showSettings = false;
  requestRender({ preserveScroll: true });
}

function renderMobileWorkerMenu() {
  // Group tabs same as sidebar
  const grouped = WORKER_GROUPS.map(g => ({ ...g, tabs: [] }));
  const knownKeys = new Set(WORKER_GROUPS.flatMap(g => g.keys));
  for (const t of tabs) {
    let placed = false;
    for (const g of grouped) {
      if (g.keys.includes(t.workerIdentity || '')) { g.tabs.push(t); placed = true; break; }
    }
    if (!placed) grouped[grouped.length - 1].tabs.push(t);
  }
  let itemsHtml = '';
  for (const g of grouped) {
    if (g.tabs.length === 0) continue;
    if (g.label) itemsHtml += `<div class="mwm-group-header">${escText(g.label)}</div>`;
    for (const t of g.tabs) {
      const m = MODELS[t.modelKey] || MODELS.codex;
      const isActive = t.id === activeTabId;
      itemsHtml += `<button class="mwm-item ${isActive ? 'active' : ''} model-${t.modelKey}" onclick="selectMobileWorker('${t.id}')">
        <span class="mwm-item-icon">${_getWorkerIcon(t)}</span>
        <span class="mwm-item-model-logo">${m.emoji || ''}</span>
        <span class="mwm-item-label">${escText(t.label)}</span>
        ${isActive ? '<span class="mwm-item-check">✓</span>' : ''}
      </button>`;
    }
  }
  return `<div class="mwm-overlay" onclick="closeMobileWorkerMenu()"></div>
    <div class="mwm-dropdown">
    ${itemsHtml}
    <button class="mwm-item mwm-new" onclick="closeMobileWorkerMenu();openNewWorkerModal()">
      <span class="mwm-item-icon">+</span>
      <span class="mwm-item-label">New Worker</span>
    </button>
  </div>`;
}

function selectMobileWorker(tabId) {
  showMobileWorkerMenu = false;
  switchTab(tabId);
  requestRender({ preserveScroll: true });
}

function closeMobileWorkerMenu() {
  showMobileWorkerMenu = false;
  requestRender({ preserveScroll: true });
}
function closeSettingsOnClickOutside(e) {
  if (!e.target.closest('.sidebar-header-wrap') && !e.target.closest('.sidebar-nav') && !e.target.closest('.mobile-settings-nav') && !e.target.closest('.mobile-menu-btn')) {
    showSettings = false;
    if (appMode === 'editor') {
      _toggleEditorSettingsDOM(); // removes dropdowns via direct DOM
    } else {
      updateSettingsUI();
    }
  }
  if (showMobileWorkerMenu && !e.target.closest('.mobile-worker-picker') && !e.target.closest('.mwm-dropdown')) {
    showMobileWorkerMenu = false;
    requestRender({ preserveScroll: true });
  }
}
function toggleWorkLog() {
  const msgEl = document.getElementById('messages');
  const top = msgEl ? msgEl.scrollTop : 0;
  showWorkLog = !showWorkLog;
  requestRender({ preserveScroll: true, keepTop: top });
}

function toggleThinking(tabId) {
  const tab = getTab(tabId);
  if (tab) {
    tab.thinkingExpanded = !tab.thinkingExpanded;
    renderMessageStreamOnly({ forcePaneRefresh: true });
  }
}

function toggleMessageThinking(tabId, idx) {
  const tab = getTab(tabId);
  if (!tab || !Array.isArray(tab.messages)) return;
  const i = Number(idx);
  if (!Number.isInteger(i) || i < 0 || i >= tab.messages.length) return;
  const msg = tab.messages[i];
  if (!msg || !msg.thinking) return;
  msg.thinkingExpanded = !msg.thinkingExpanded;
  persistTabs();
  if (activeTabId === tabId) renderMessageStreamOnly({ forcePaneRefresh: true });
}

function promptDeleteTab(id) {
  showSettings = false;
  deleteTabTargetId = id || (activeTab()?.id) || null;
  if (!deleteTabTargetId) return;
  showDeleteTabModal = true;
  requestRender({ preserveScroll: true });
}

function promptDeleteCurrentTab() {
  promptDeleteTab(activeTab()?.id);
}

function cancelDeleteCurrentTab() {
  showDeleteTabModal = false;
  deleteTabTargetId = null;
  requestRender({ preserveScroll: true });
}

function confirmDeleteCurrentTab() {
  const id = deleteTabTargetId || activeTab()?.id;
  showDeleteTabModal = false;
  deleteTabTargetId = null;
  if (!id) {
    requestRender({ preserveScroll: true });
    return;
  }
  closeTab(id);
}

function showCompactConfirm() {
  showSettings = false;
  showCompactModal = true;
  requestRender({ preserveScroll: true });
}

function cancelCompactModal() {
  _pendingSkillAfterCompact = null;
  showCompactModal = false;
  requestRender({ preserveScroll: true });
}

async function confirmCompact() {
  const pendingSkill = _pendingSkillAfterCompact;
  _pendingSkillAfterCompact = null;
  showCompactModal = false;
  requestRender({ preserveScroll: true });
  await doCompact();
  // If this compact was triggered by a skill click, insert the prompt now
  if (pendingSkill) {
    const input = document.getElementById('input');
    if (!input) return;
    input.value = pendingSkill.starter;
    input.focus();
    input.setSelectionRange(pendingSkill.starter.length, pendingSkill.starter.length);
    autoResize(input);
    updateInputBtn();
  }
}

function renderCompactModal() {
  const tab = activeTab();
  const model = tab ? (tab.modelKey || 'unknown').replace(/_/g, ' ') : 'this worker';
  return `<div class="compact-modal-overlay" onclick="if(event.target===this)cancelCompactModal()">
    <div class="compact-modal">
      <div class="compact-modal-icon">🗜</div>
      <div class="compact-modal-title">Smart Compact</div>
      <div class="compact-modal-detail">
        This will compress the conversation history for <strong>${escText(model)}</strong> to free up context space.
        <ul class="compact-modal-list">
          <li>Recent messages are summarized into a compact digest</li>
          <li>Identity files and key context are reloaded fresh</li>
          <li>The worker continues with full awareness of prior work</li>
        </ul>
        <span class="compact-modal-note">Best used when context is getting large or between project phases.</span>
      </div>
      <div class="compact-modal-actions">
        <button class="compact-btn-ok" onclick="confirmCompact()">Compact Now</button>
        <button class="compact-btn-cancel" onclick="cancelCompactModal()">Cancel</button>
      </div>
    </div>
  </div>`;
}

async function doCompact() {
  showSettings = false;
  const tab = activeTab();
  if (!tab) return;
  addWork(tab.id, { type: 'status', text: 'Compacting session…' });

  const isClaude = _isClaudeModel(tab.modelKey);
  if (isClaude) _localCompactInProgress = true;

  // Show pre-compact system card
  {
    let preFiles = [];
    if (isClaude) {
      try {
        const st = await fetch(_claudeUrl(tab, '/status')).then(r => r.json()).catch(() => null);
        preFiles = st?.active_docs || [];
      } catch (_) {}
    }
    tab.messages.push({
      role: 'system', ts: Date.now(), text: 'Smart Compact — compacting…',
      _card: { icon: '🗜', title: 'Smart Compact', files: preFiles, stats: ['Compacting…'] }
    });
    requestRender({ preserveScroll: true });
  }

  try {
    const before = isClaude ? null : await fetch(API + '/api/tokens?session_id=' + tab.sessionId).then(r => r.json()).catch(() => null);
    const endpoint = isClaude ? _claudeUrl(tab, '/smart-compact') : API + '/api/compact';
    const body = isClaude ? { n_messages: 100, session_id: tab.sessionId } : { session_id: tab.sessionId };
    const res = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    const after = isClaude
      ? await fetch(_claudeUrl(tab, '/status')).then(r => r.json()).catch(() => null)
      : await fetch(API + '/api/tokens?session_id=' + tab.sessionId).then(r => r.json()).catch(() => null);
    if (data?.ok || data?.status === 'ok') {
      if (isClaude) {
        const summaryK = data.summary_length ? Math.round(data.summary_length / 1000) + 'k' : '?';
        const loadedFiles = data.loaded_files || [];
        const count = data.compaction_count || '?';
        statusBanner = `✅ Compact #${count}: ${summaryK} chars, ${loadedFiles.length} files loaded`;
        tab.messages.push({
          role: 'system', ts: Date.now(), text: `Smart compact #${count} complete`,
          _card: {
            icon: '✅', title: 'Context Reloaded',
            files: loadedFiles,
            stats: [`Compact #${count}`, `${summaryK} chars`, `${loadedFiles.length} files loaded`]
          }
        });
      } else {
        const bk = data.before?.tokens != null ? Math.round(data.before.tokens / 1000) + 'k' : (before?.estimated_tokens != null ? Math.round(before.estimated_tokens/1000) + 'k' : '?');
        const ak = data.after?.tokens != null ? Math.round(data.after.tokens / 1000) + 'k' : (after?.estimated_tokens != null ? Math.round(after.estimated_tokens/1000) + 'k' : '?');
        const kept = data.kept_messages || '?';
        statusBanner = `✅ Compacted: ${bk} → ${ak}`;
        tab.messages.push({
          role: 'system', ts: Date.now(), text: `Smart compact complete — ${bk} → ${ak}`,
          _card: {
            icon: '✅', title: 'Context Reloaded',
            files: [],
            stats: [`${bk} → ${ak} tokens`, `${kept} messages kept`]
          }
        });
      }
      statusBannerTabId = tab.id;
    } else {
      statusBanner = `⚠️ ${data?.reason || data?.error || 'Compaction failed'}`;
      statusBannerTabId = tab.id;
    }
    setTimeout(() => {
      if (statusBannerTabId === tab.id) {
        statusBanner = null;
        statusBannerTabId = null;
      }
      requestRender({ preserveScroll: true });
    }, 4000);
  } catch (e) {
    statusBanner = `⚠️ Error: ${e.message}`;
    statusBannerTabId = tab.id;
    setTimeout(() => {
      if (statusBannerTabId === tab.id) {
        statusBanner = null;
        statusBannerTabId = null;
      }
      requestRender({ preserveScroll: true });
    }, 5000);
  }
  _localCompactInProgress = false;
  refreshMeta(); requestRender({ preserveScroll: true });
}

async function doReset() {
  showSettings = false;
  const tab = activeTab();
  if (!tab) return;
  tab.messages = []; tab.workLog = []; tab.toolCallCount = 0; tab.activeToolLabel = null; tab.activeToolDetail = null;
  tab.streamingText = '';
  tab.wasLoading = false;
  tab.loadingSinceMs = 0;
  tab.elevation = null;
  if (firstToolJumpTabId === tab.id) { firstToolJumpDone = false; firstToolJumpTabId = null; }
  if (statusBannerTabId === tab.id) { statusBanner = null; statusBannerTabId = null; }
  if (safetyCapTabId === tab.id) { safetyCapMessage = null; safetyCapTabId = null; }
  if (_isClaudeModel(tab.modelKey)) {
    try { await fetch(API + '/api/claude/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: tab.sessionId }) }); } catch {}
  } else {
    try { await fetch(API + '/api/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: tab.sessionId }) }); } catch {}
  }
  requestRender();
}

async function doClear() {
  showSettings = false;
  const tab = activeTab();
  if (!tab) return;
  tab.messages = []; tab.workLog = []; tab.toolCallCount = 0;
  tab.streamingText = '';
  tab.wasLoading = false;
  tab.loadingSinceMs = 0;
  tab.elevation = null;
  if (statusBannerTabId === tab.id) { statusBanner = null; statusBannerTabId = null; }
  if (safetyCapTabId === tab.id) { safetyCapMessage = null; safetyCapTabId = null; }
  if (_isClaudeModel(tab.modelKey)) {
    try { await fetch(API + '/api/claude/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: tab.sessionId }) }); } catch {}
  } else {
    try { await fetch(API + '/api/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: tab.sessionId }) }); } catch {}
  }
  requestRender({ forceStickBottom: true });
}

async function doBackup() {
  showSettings = false;
  const tab = activeTab();
  if (tab) tab.messages.push(_sysCard('Backing up to GitHub...', '☁️', 'Backup'));
  requestRender();
  try {
    const res = await fetch(API + '/api/backup', { method: 'POST' });
    const data = await res.json().catch(() => ({}));

    if (res.status === 400 && data.needs_setup) {
      if (tab) tab.messages.push(_sysCard('GitHub backup is not configured yet. Please add repo URL + branch.', '⚙️', 'Backup'));
      await openBackupSetupModal();
      requestRender();
      return;
    }

    if (res.ok && data.ok) {
      if (tab) tab.messages.push(_sysCard('Backup complete' + (data.output ? '\n' + data.output : ''), '✅', 'Backup'));
    } else {
      if (tab) tab.messages.push(_sysCard('Backup failed: ' + (data.error || 'Unknown error'), '❌', 'Backup'));
    }
  } catch (e) {
    if (tab) tab.messages.push(_sysCard('Backup error: ' + e.message, '❌', 'Backup'));
  }
  requestRender();
}

function openAISetup() { showSettings = false; window.location.href = '/setup.html#step2'; }

function promptRestartServer() {
  showSettings = false;
  showRestartModal = true;
  requestRender({ preserveScroll: true });
}

function cancelRestartServer() {
  showRestartModal = false;
  requestRender({ preserveScroll: true });
}

async function confirmRestartServer() {
  showRestartModal = false;
  requestRender({ preserveScroll: true });
  try {
    await fetch(API + '/api/restart', { method: 'POST' });
  } catch {}
  const tab = activeTab();
  if (tab) {
    tab.messages.push(_sysCard('Restart requested. Reconnecting…', '♻️', 'Server'));
    persistTabs();
    requestRender({ preserveScroll: true });
  }
  // Give the backend a moment to restart, then force fresh load.
  setTimeout(() => {
    forceRefreshApp();
  }, 1600);
}

function renderRestartModal() {
  return `<div class="root-warning-overlay" onclick="if(event.target===this){cancelRestartServer();}">
    <div class="root-warning">
      <h3>♻️ Restart Server</h3>
      <p>Are you sure you want to restart ${APP_NAME} now?</p>
      <p>Active streams will disconnect briefly and reconnect after restart.</p>
      <div class="warn-actions">
        <button class="btn-cancel" onclick="cancelRestartServer()">Cancel</button>
        <button class="btn-confirm" onclick="confirmRestartServer()">OK</button>
      </div>
    </div>
  </div>`;
}

let showPasswordModal = false;
let passwordModalUser = '';
let passwordModalError = '';
let passwordModalSuccess = '';

let showBackupModal = false;
let backupRepoUrl = '';
let backupBranch = 'main';
let backupModalError = '';
let backupModalSuccess = '';

let showRestartModal = false;

async function resetPassword() {
  showSettings = false;
  passwordModalError = '';
  passwordModalSuccess = '';
  // Fetch current user info
  try {
    const res = await fetch(API + '/auth/me');
    const data = await res.json();
    passwordModalUser = data.display_name || data.username || 'User';
  } catch { passwordModalUser = 'User'; }
  showPasswordModal = true;
  requestRender({ preserveScroll: true });
  setTimeout(() => { const el = document.getElementById('pw-new'); if (el) el.focus(); }, 50);
}

function closePasswordModal() {
  showPasswordModal = false;
  passwordModalError = '';
  passwordModalSuccess = '';
  requestRender();
}

async function submitPasswordChange() {
  const currentPw = document.getElementById('pw-current')?.value || '';
  const newPw = document.getElementById('pw-new')?.value || '';
  const confirm = document.getElementById('pw-confirm')?.value || '';
  passwordModalError = '';
  passwordModalSuccess = '';

  if (!isLocalClient && !currentPw) { passwordModalError = 'Current password is required'; requestRender({ preserveScroll: true }); return; }
  if (!newPw || newPw.length < 6) { passwordModalError = 'New password must be at least 6 characters'; requestRender({ preserveScroll: true }); return; }
  if (newPw !== confirm) { passwordModalError = 'Passwords don\'t match'; requestRender({ preserveScroll: true }); return; }

  const payload = { new_password: newPw };
  if (currentPw) payload.current_password = currentPw;
  try {
    const res = await fetch(API + '/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.error) { passwordModalError = data.error; requestRender({ preserveScroll: true }); return; }
    passwordModalSuccess = 'Password updated ✓';
    requestRender();
    setTimeout(() => closePasswordModal(), 1500);
  } catch (e) {
    passwordModalError = e.message;
    requestRender();
  }
}

function renderPasswordModal() {
  const needsCurrent = !isLocalClient;
  return `<div class="root-warning-overlay" onclick="if(event.target===this)closePasswordModal()">
    <div class="root-warning" style="border-color:var(--border-light)">
      <h3 style="color:var(--accent)">🔑 Change Password</h3>
      <p style="margin-bottom:14px">Signed in as <strong style="color:var(--text)">${escText(passwordModalUser)}</strong></p>
      <div style="display:flex;flex-direction:column;gap:10px">
        ${needsCurrent ? `<input type="password" id="pw-current" placeholder="Current password" autocomplete="current-password"
          style="padding:10px 12px;background:var(--bg);border:1px solid var(--border-light);border-radius:8px;color:var(--text);font-size:14px;outline:none;-webkit-text-security:disc"
          onkeydown="if(event.key==='Enter'){document.getElementById('pw-new').focus();event.preventDefault();}">` : ''}
        <input type="password" id="pw-new" placeholder="New password" autocomplete="off"
          style="padding:10px 12px;background:var(--bg);border:1px solid var(--border-light);border-radius:8px;color:var(--text);font-size:14px;outline:none;-webkit-text-security:disc"
          onkeydown="if(event.key==='Enter'){document.getElementById('pw-confirm').focus();event.preventDefault();}">
        <input type="password" id="pw-confirm" placeholder="Confirm new password" autocomplete="off"
          style="padding:10px 12px;background:var(--bg);border:1px solid var(--border-light);border-radius:8px;color:var(--text);font-size:14px;outline:none;-webkit-text-security:disc"
          onkeydown="if(event.key==='Enter'){submitPasswordChange();event.preventDefault();}">
      </div>
      ${passwordModalError ? `<p style="color:var(--red);font-size:12px;margin-top:8px">${escText(passwordModalError)}</p>` : ''}
      ${passwordModalSuccess ? `<p style="color:var(--green);font-size:12px;margin-top:8px">${passwordModalSuccess}</p>` : ''}
      <div class="warn-actions" style="margin-top:14px">
        <button class="btn-cancel" onclick="closePasswordModal()">Cancel</button>
        <button class="btn-confirm" style="background:rgba(94,181,247,0.2);color:var(--accent)" onclick="submitPasswordChange()">Update Password</button>
      </div>
    </div>
  </div>`;
}

async function openBackupSetupModal() {
  backupModalError = '';
  backupModalSuccess = '';
  backupRepoUrl = backupRepoUrl || '';
  backupBranch = backupBranch || 'main';

  try {
    const res = await fetch(API + '/api/backup/status');
    const data = await res.json().catch(() => ({}));
    if (data.repo_url) backupRepoUrl = data.repo_url;
    if (data.branch) backupBranch = data.branch;
  } catch {}

  showBackupModal = true;
  requestRender();
  setTimeout(() => { const el = document.getElementById('backup-repo-url'); if (el) el.focus(); }, 50);
}

function closeBackupModal() {
  showBackupModal = false;
  backupModalError = '';
  backupModalSuccess = '';
  requestRender();
}

async function submitBackupConfig() {
  backupModalError = '';
  backupModalSuccess = '';

  const repoUrl = (document.getElementById('backup-repo-url')?.value || '').trim();
  const branch = (document.getElementById('backup-branch')?.value || '').trim() || 'main';

  if (!repoUrl) { backupModalError = 'Repository URL is required'; requestRender(); return; }

  try {
    const res = await fetch(API + '/api/backup/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_url: repoUrl, branch, prefer_ssh: true }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
      backupModalError = data.error || 'Failed to save backup configuration';
      requestRender();
      return;
    }
    backupModalSuccess = 'Backup configuration saved ✓';
    backupRepoUrl = repoUrl;
    backupBranch = branch;
    requestRender();
    setTimeout(() => closeBackupModal(), 900);
  } catch (e) {
    backupModalError = e.message || 'Failed to save backup configuration';
    requestRender();
  }
}

function renderBackupModal() {
  return `<div class="root-warning-overlay" onclick="if(event.target===this)closeBackupModal()">
    <div class="root-warning" style="border-color:var(--border-light);max-width:640px">
      <h3 style="color:var(--accent)">☁️ Configure GitHub Backup</h3>
      <p style="margin-bottom:10px">Enter your repo details once, then <strong>Backup Now</strong> will commit + push automatically.</p>

      <div style="font-size:12px;line-height:1.45;color:var(--muted);background:rgba(255,255,255,0.03);border:1px solid var(--border-light);border-radius:8px;padding:10px 12px;margin-bottom:10px">
        <div style="font-weight:600;color:var(--text);margin-bottom:6px">Quick setup (2 steps):</div>
        <ol style="margin:0 0 0 18px;padding:0;display:flex;flex-direction:column;gap:4px">
          <li>Create a repo on GitHub (empty is fine).</li>
          <li>Copy the repo URL from the green <strong>Code</strong> button and paste it below.</li>
        </ol>
        <div style="margin-top:8px">
          Need help? <a href="https://github.com/new" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">Create a new repository</a>
          · <a href="https://docs.github.com/en/repositories/creating-and-managing-repositories/quickstart-for-repositories" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">GitHub quickstart</a>
          · <a href="https://docs.github.com/en/get-started/getting-started-with-git/about-remote-repositories" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">About repo URLs</a>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:10px">
        <input id="backup-repo-url" value="${escText(backupRepoUrl)}" placeholder="Repository URL (e.g. git@github.com:user/repo.git or https://github.com/user/repo.git)"
          style="padding:10px 12px;background:var(--bg);border:1px solid var(--border-light);border-radius:8px;color:var(--text);font-size:14px;outline:none">
        <input id="backup-branch" value="${escText(backupBranch || 'main')}" placeholder="Branch (usually main)"
          style="padding:10px 12px;background:var(--bg);border:1px solid var(--border-light);border-radius:8px;color:var(--text);font-size:14px;outline:none"
          onkeydown="if(event.key==='Enter'){submitBackupConfig();event.preventDefault();}">
      </div>

      <p style="font-size:11px;color:var(--muted);margin-top:8px">Tip: HTTPS URLs may require a Personal Access Token for push. SSH URLs require your SSH key added in GitHub.</p>

      ${backupModalError ? `<p style="color:var(--red);font-size:12px;margin-top:8px">${escText(backupModalError)}</p>` : ''}
      ${backupModalSuccess ? `<p style="color:var(--green);font-size:12px;margin-top:8px">${backupModalSuccess}</p>` : ''}
      <div class="warn-actions" style="margin-top:14px">
        <button class="btn-cancel" onclick="closeBackupModal()">Cancel</button>
        <button class="btn-confirm" style="background:rgba(94,181,247,0.2);color:var(--accent)" onclick="submitBackupConfig()">Save & Use for Backups</button>
      </div>
    </div>
  </div>`;
}

async function checkForUpdates() {
  showSettings = false;
  requestRender({ preserveScroll: true });
  const tab = activeTab();

  if (tab) tab.messages.push(_sysCard('Checking for updates...', '📦', 'Update'));
  requestRender({ forceStickBottom: true });

  try {
    const r = await fetch(API + '/api/update/check', { method: 'POST' });
    const d = await r.json();
    if (tab) tab.messages.pop();

    if (d.error) {
      if (tab) tab.messages.push(_sysCard('Update check failed: ' + d.error, '❌', 'Update'));
    } else if (d.up_to_date) {
      // Show rollback button if a previous update can be rolled back
      let msg = APP_NAME + ' is up to date. (' + (d.commit || '').substring(0, 7) + ')';
      if (d.rollback_available) {
        msg += '\n\n<button onclick="rollbackUpdate()" style="margin-top:8px;padding:6px 16px;border-radius:8px;border:1px solid var(--border);background:var(--surface);cursor:pointer;color:var(--text)">↩ Rollback Last Update</button>';
      }
      if (tab) tab.messages.push(_sysCard(msg, '✅', 'Update'));
    } else {
      // Fetch changelog before showing apply prompt
      const count = d.behind || 0;
      let changelogText = count + ' update' + (count === 1 ? '' : 's') + ' available.\n\n';

      try {
        const clr = await fetch(API + '/api/update/changelog');
        const cld = await clr.json();
        if (cld.commits && cld.commits.length > 0) {
          changelogText += '**Changelog** (' + cld.from_commit + ' → ' + cld.to_commit + '):\n';
          for (const c of cld.commits.slice(0, 20)) {
            changelogText += '- `' + c.hash + '` ' + c.message + '\n';
          }
          if (cld.commits.length > 20) {
            changelogText += '- ... and ' + (cld.commits.length - 20) + ' more\n';
          }
          changelogText += '\n';
        }
      } catch {}

      changelogText += '<div style="display:flex;gap:8px;margin-top:8px">';
      changelogText += '<button onclick="applyUpdate()" style="padding:6px 16px;border-radius:8px;border:none;background:var(--accent, #007AFF);color:white;cursor:pointer;font-weight:600">Apply Update</button>';
      changelogText += '<button onclick="skipUpdate()" style="padding:6px 16px;border-radius:8px;border:1px solid var(--border);background:var(--surface);cursor:pointer;color:var(--text)">Skip</button>';
      changelogText += '</div>';

      if (tab) tab.messages.push(_sysCard(changelogText, '📦', 'Update Available'));
    }
  } catch (e) {
    if (tab) tab.messages.pop();
    if (tab) tab.messages.push(_sysCard('Network error: ' + e.message, '❌', 'Update'));
  }
  requestRender({ forceStickBottom: true });
}

function skipUpdate() {
  const tab = activeTab();
  if (tab && tab.messages.length) {
    // Remove the changelog card (last system message)
    for (let i = tab.messages.length - 1; i >= 0; i--) {
      if (tab.messages[i]._card && tab.messages[i]._card.title === 'Update Available') {
        tab.messages.splice(i, 1);
        break;
      }
    }
  }
  requestRender({ preserveScroll: true });
}

async function applyUpdate() {
  const tab = activeTab();

  // Replace changelog card with applying message
  if (tab) {
    for (let i = tab.messages.length - 1; i >= 0; i--) {
      if (tab.messages[i]._card && tab.messages[i]._card.title === 'Update Available') {
        tab.messages.splice(i, 1);
        break;
      }
    }
    tab.messages.push(_sysCard('Applying update...', '📦', 'Update'));
    requestRender({ forceStickBottom: true });
  }

  try {
    const ur = await fetch(API + '/api/update/apply', { method: 'POST' });
    const ud = await ur.json();
    if (tab) tab.messages.pop();

    if (ud.error) {
      if (tab) tab.messages.push(_sysCard('Update failed: ' + ud.error, '❌', 'Update'));
    } else {
      _updateAvailable = false;
      _updateBehindCount = 0;
      localStorage.setItem('kukuibot.updateAvailable', '0');
      localStorage.setItem('kukuibot.updateBehind', '0');
      localStorage.removeItem('kukuibot.lastUpdateCheck');

      let msg = 'Updated! ' + (ud.pre_update_commit || '') + ' → ' + (ud.new_commit || '');
      if (ud.nuclear) {
        msg += '\n\n⚠️ **Update required a full reset.** Local changes may have been lost.';
      }
      msg += '\n\n↩ <button onclick="rollbackUpdate()" style="padding:6px 16px;border-radius:8px;border:1px solid var(--border);background:var(--surface);cursor:pointer;color:var(--text)">Rollback</button>';
      msg += '\n\nReloading in 5 seconds...';

      if (tab) tab.messages.push(_sysCard(msg, '✅', 'Update'));
      requestRender({ forceStickBottom: true });
      setTimeout(() => forceRefreshApp(), 5000);
    }
  } catch (e) {
    if (tab) tab.messages.pop();
    if (tab) tab.messages.push(_sysCard('Network error: ' + e.message, '❌', 'Update'));
  }
  requestRender({ forceStickBottom: true });
}

async function rollbackUpdate() {
  const tab = activeTab();
  if (tab) tab.messages.push(_sysCard('Rolling back update...', '↩', 'Rollback'));
  requestRender({ forceStickBottom: true });

  try {
    const r = await fetch(API + '/api/update/rollback', { method: 'POST' });
    const d = await r.json();
    if (tab) tab.messages.pop();

    if (d.error) {
      if (tab) tab.messages.push(_sysCard('Rollback failed: ' + d.error, '❌', 'Rollback'));
    } else {
      localStorage.removeItem('kukuibot.lastUpdateCheck');
      if (tab) tab.messages.push(_sysCard('Rolled back to ' + (d.rolled_back_to || 'previous version') + '.\n\nReloading in 3 seconds...', '✅', 'Rollback'));
      requestRender({ forceStickBottom: true });
      setTimeout(() => forceRefreshApp(), 3000);
    }
  } catch (e) {
    if (tab) tab.messages.pop();
    if (tab) tab.messages.push(_sysCard('Network error: ' + e.message, '❌', 'Rollback'));
  }
  requestRender({ forceStickBottom: true });
}

async function doLogout() {
  showSettings = false;
  stopAutoNameScheduler();
  try { await fetch(API + '/auth/logout', { method: 'POST' }); } catch {}
  authenticated = false;
  _openaiConnected = false;
  requestRender({ preserveScroll: true });
  window.location.href = '/login.html';
}

// --- Data Loading ---
async function refreshMeta() {
  const tab = activeTab();
  if (!tab) return;
  try {
    const isClaude = _isClaudeModel(tab.modelKey);
    const [tokRes, rtRes, aaRes, esRes, psRes, dbgRes, claudeRes] = await Promise.all([
      isClaude ? Promise.resolve(null) : fetch(API + '/api/tokens?session_id=' + tab.sessionId).then(r => r.json()).catch(() => null),
      fetch(API + '/api/runtime').then(r => r.json()).catch(() => null),
      fetch(API + '/api/approve-all?session_id=' + tab.sessionId).then(r => r.json()).catch(() => null),
      fetch(API + '/api/elevated-session?session_id=' + tab.sessionId).then(r => r.json()).catch(() => null),
      fetch(API + '/api/privileged/status?session_id=' + tab.sessionId).then(r => r.json()).catch(() => null),
      fetch(API + '/api/token-debug?session_id=' + tab.sessionId).then(r => r.json()).catch(() => null),
      isClaude ? fetch(_claudeUrl(tab, '/status')).then(r => r.json()).catch(() => null) : Promise.resolve(null),
    ]);
    // Defer usage stats fetch — it hits an external API (chatgpt.com) and shouldn't block local metadata
    fetch(API + '/api/usage').then(r => r.json()).then(u => { if (u?.ok) { planUsage = u; requestRender({ preserveScroll: true }); } }).catch(() => {});
    if (isClaude && claudeRes?.ok) {
      const ct = claudeRes.last_input_tokens || 0;
      const cw = claudeRes.compaction?.context_window || 1000000;
      tab.contextInfo = { tokens: ct, max: cw, pct: Math.round(ct / cw * 100), source: 'api' };
    } else if (tokRes) {
      tab.contextInfo = { tokens: tokRes.estimated_tokens, max: tokRes.context_window, pct: tokRes.usage_percent, source: tokRes.source || '' };
    }
    if (dbgRes) tab.tokenDebug = dbgRes;
    if (rtRes) runtimeStarted = Number(rtRes.runtime_started || 0);
    if (aaRes) { tab.approveAll = Boolean(aaRes.enabled); if (tab.approveAll) tab.approveAllVisible = true; }
    if (esRes) elevatedSession = { enabled: Boolean(esRes.enabled), remaining_seconds: Number(esRes.remaining_seconds || 0) };
    if (psRes && psRes.ok === true && typeof psRes.elevated !== 'undefined') {
      // UI root indicator reflects real privileged helper capability when available.
      elevatedSession = { enabled: Boolean(psRes.elevated), remaining_seconds: Number(psRes.remaining_seconds || 0) };
    }
  } catch {}
  try {
    const cfg = await fetch(API + '/api/config').then(r => r.json());
    if (cfg.reasoning_effort) reasoningEffort = cfg.reasoning_effort;
  } catch {}
  requestRender({ preserveScroll: true });
}

async function loadTabHistory(tab, limit = 120) {
  if (!tab) return;
  const isClaude = _isClaudeModel(tab.modelKey);
  const chatlogUrl = API + '/api/chatlog?session_id=' + encodeURIComponent(tab.sessionId) + '&n=' + CHATLOG_PAGE_SIZE + '&offset=0';
  try {
    const res = await fetch(chatlogUrl);
    if (!res.ok) {
      if (isClaude) return; // no legacy fallback for Claude
      return await _loadTabHistoryLegacy(tab, limit);
    }
    const data = await res.json();
    const msgs = (data.messages || []).filter(m => m.role && m.text);
    tab._chatlogTotal = data.total || 0;
    tab._chatlogLoaded = msgs.length;
    tab._allLoaded = !data.has_more;
    // Build seenLogIds set for deduplication on merge
    tab._seenLogIds = new Set();
    tab.messages = msgs.map((m, i) => {
      const logId = m.id || 0;
      if (logId) tab._seenLogIds.add(logId);
      return {
        id: logId || ((m.ts || Date.now()) + i),
        logId,
        role: m.role,
        text: m.text,
        timestamp: m.ts ? new Date(m.ts) : new Date(),
        modelLabel: m.role === 'assistant' ? (MODELS[tab.modelKey]?.shortName || MODELS[tab.modelKey]?.name || 'Assistant') : undefined,
      };
    });
    // Sort by logId ascending for deterministic ordering
    tab.messages.sort((a, b) => (a.logId || 0) - (b.logId || 0));
    requestRender({ forceStickBottom: true });
  } catch {
    return await _loadTabHistoryLegacy(tab, limit);
  }
}

async function _loadTabHistoryLegacy(tab, limit = 120) {
  try {
    const res = await fetch(API + '/api/history?session_id=' + encodeURIComponent(tab.sessionId) + '&limit=' + limit);
    if (!res.ok) return;
    const data = await res.json();
    const msgs = Array.isArray(data.messages) ? data.messages : [];
    tab.messages = msgs.map((m, i) => ({
      id: Date.now() + i,
      role: m.role === 'user' ? 'user' : 'assistant',
      text: m.content || '',
      timestamp: new Date(),
      modelLabel: m.role === 'assistant' ? (MODELS[tab.modelKey]?.shortName || MODELS[tab.modelKey]?.name || 'Assistant') : undefined,
    }));
    tab._allLoaded = true;
    requestRender({ forceStickBottom: true });
  } catch {}
}

async function loadOlderMessages(tab) {
  if (!tab) tab = activeTab();
  if (!tab || tab._allLoaded || tab._loadingOlder) return;
  tab._loadingOlder = true;
  const def = MODELS[tab.modelKey] || MODELS.codex;
  // Show loading indicator while fetching
  const el = document.getElementById('messages');
  const indicator = el && el.querySelector('[data-load-older]');
  const prevIndicatorHtml = indicator ? indicator.innerHTML : '';
  if (indicator) indicator.innerHTML = '<span style="opacity:0.6">Loading\u2026</span>';
  try {
    // Use cursor-based pagination (before_id) if we have a known oldest logId
    const seenIds = tab._seenLogIds || new Set();
    const existingMsgs = tab.messages || [];
    const oldestLogId = existingMsgs.reduce((min, m) => (m.logId && (min === 0 || m.logId < min)) ? m.logId : min, 0);
    let olderUrl;
    if (oldestLogId > 0) {
      olderUrl = API + '/api/chatlog?session_id=' + encodeURIComponent(tab.sessionId) + '&n=' + CHATLOG_PAGE_SIZE + '&before_id=' + oldestLogId;
    } else {
      const offset = tab._chatlogLoaded || 0;
      olderUrl = API + '/api/chatlog?session_id=' + encodeURIComponent(tab.sessionId) + '&n=' + CHATLOG_PAGE_SIZE + '&offset=' + offset;
    }
    const res = await fetch(olderUrl);
    if (!res.ok) { if (indicator) indicator.innerHTML = prevIndicatorHtml; return; }
    const data = await res.json();
    const older = (data.messages || []).filter(m => m.role && m.text);
    tab._allLoaded = !data.has_more;
    if (older.length === 0) { tab._allLoaded = true; if (indicator) indicator.remove(); return; }
    // Deduplicate using seenLogIds
    const newOlder = older.filter(m => {
      const logId = m.id || 0;
      if (logId && seenIds.has(logId)) return false;
      if (logId) seenIds.add(logId);
      return true;
    });
    tab._seenLogIds = seenIds;
    tab._chatlogLoaded = (tab._chatlogLoaded || 0) + newOlder.length;
    if (newOlder.length === 0) { if (indicator) indicator.remove(); return; }
    const olderMapped = newOlder.map((m, i) => {
      const logId = m.id || 0;
      return {
        id: logId || ((m.ts || Date.now()) + i),
        logId,
        role: m.role,
        text: m.text,
        timestamp: m.ts ? new Date(m.ts) : new Date(),
        modelLabel: m.role === 'assistant' ? def.name : undefined,
      };
    });
    tab.messages = [...olderMapped, ...tab.messages];
    // Sort all messages by logId ascending for deterministic ordering
    tab.messages.sort((a, b) => (a.logId || 0) - (b.logId || 0));
    // Surgical DOM insertion with scroll position preservation
    if (el) {
      const prevScrollHeight = el.scrollHeight;
      let olderHtml = '';
      for (const m of olderMapped) {
        olderHtml += renderMessage(m, def);
      }
      const firstMsg = el.querySelector('.msg');
      if (indicator) {
        indicator.insertAdjacentHTML('afterend', olderHtml);
        if (tab._allLoaded) {
          indicator.remove();
        } else {
          const remaining = (tab._chatlogTotal || 0) - (tab._chatlogLoaded || 0);
          indicator.innerHTML = '\u2191 Load older messages (' + remaining + ' remaining)';
        }
      } else if (firstMsg) {
        firstMsg.insertAdjacentHTML('beforebegin', olderHtml);
      } else {
        el.insertAdjacentHTML('afterbegin', olderHtml);
      }
      // Preserve scroll position: offset by the height of newly inserted content
      const addedHeight = el.scrollHeight - prevScrollHeight;
      el.scrollTop += addedHeight;
    }
  } catch { if (indicator) indicator.innerHTML = prevIndicatorHtml; }
  finally { tab._loadingOlder = false; }
}

// --- Beep ---
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator(); const gain = ctx.createGain();
    osc.type = 'sine'; osc.frequency.value = 880; gain.gain.value = 0.06;
    osc.connect(gain); gain.connect(ctx.destination);
    osc.start(); setTimeout(() => { osc.stop(); ctx.close(); }, 120);
  } catch {}
}

// --- Countdown ---
let streamWatchdogBusy = false;
setInterval(async () => {
  if (elevatedSession.enabled) {
    elevatedSession.remaining_seconds = Math.max(0, (elevatedSession.remaining_seconds || 0) - 1);
    if (elevatedSession.remaining_seconds <= 0) elevatedSession.enabled = false;
    requestRender({ preserveScroll: true });
  }

  if (streamWatchdogBusy) return;
  streamWatchdogBusy = true;
  try {
    // Stream watchdog: do NOT clear loading blindly. Confirm server state first.
    const now = Date.now();
    let changed = false;
    for (const t of tabs) {
      if (!(t?.wasLoading) || Number(t.loadingSinceMs || 0) <= 0) continue;

      // Safety timeout: if no SSE events for 60s on ANY tab type, force-unlock
      const lastEvt = _lastSseEventTime[t.id] || t.loadingSinceMs || 0;
      const sseIdleMs = now - lastEvt;
      if (sseIdleMs >= INPUT_SAFETY_TIMEOUT_MS) {
        console.warn(`[watchdog] Force-unlocking tab ${t.id} — ${Math.round(sseIdleMs / 1000)}s since last SSE event`);
        t.wasLoading = false;
        t.loadingSinceMs = 0;
        t.streamingText = '';
        t.activeToolLabel = null; t.activeToolDetail = null;
        t.elevation = null;
        delete _lastSseEventTime[t.id];
        changed = true;
        // Drain any queued messages for this tab
        _drainMessageQueue(t);
        continue;
      }

      // Claude/Anthropic tabs with EventSource: check SSE idle time instead of skipping
      const hasEvtSource = (_isClaudeModel(t.modelKey) && _claudeEvtSources[t.sessionId])
        || (_isAnthropicModel(t.modelKey) && _anthropicEvtSources[t.sessionId]);
      if (hasEvtSource) {
        const sseLastEvt = _lastSseEventTime[t.id] || t.loadingSinceMs || 0;
        const sseIdle = now - sseLastEvt;
        // If SSE events are still flowing (within 15s), trust EventSource for cleanup
        if (sseIdle < 15000) continue;
        // SSE idle >15s while wasLoading — verify with server
        const sseStatus = await fetchTaskStatus(t.sessionId);
        if (sseStatus && sseStatus.status === 'running') {
          if (!t.activeToolLabel) {
            t.activeToolLabel = 'Reconnecting stream…';
            changed = true;
          }
          continue;
        }

        // EventSource tabs can miss a final done event during restart/reconnect.
        // Before clearing the live bubble, recover persisted final assistant text.
        let recovered = false;
        if (_isClaudeModel(t.modelKey) || _isAnthropicModel(t.modelKey)) {
          recovered = await recoverEventSourceFinalFromHistory(t);
        }
        if (recovered) {
          delete _lastSseEventTime[t.id];
          changed = true;
          continue;
        }

        // Server says idle and no recoverable final text — clear stuck loading state
        console.warn(`[watchdog] Clearing stuck loading on tab ${t.id} — SSE idle ${Math.round(sseIdle / 1000)}s, server says idle`);
        t.wasLoading = false;
        t.loadingSinceMs = 0;
        t.streamingText = '';
        t.activeToolLabel = null; t.activeToolDetail = null;
        t.elevation = null;
        delete _lastSseEventTime[t.id];
        changed = true;
        _drainMessageQueue(t);
        continue;
      }
      // Non-EventSource tabs (Codex, Spark, etc.): use original 45s threshold
      const ageMs = now - Number(t.loadingSinceMs || 0);
      if (ageMs < 45000) continue;
      const st = await fetchTaskStatus(t.sessionId);
      if (st && st.status === 'running') {
        if (!t.activeToolLabel) {
          t.activeToolLabel = 'Reconnecting stream…';
          changed = true;
        }
        continue;
      }
      t.wasLoading = false;
      t.loadingSinceMs = 0;
      t.streamingText = '';
      t.activeToolLabel = null; t.activeToolDetail = null;
      t.elevation = null;
      changed = true;
      // Drain any queued messages for this tab
      _drainMessageQueue(t);
    }
    if (changed) {
      persistTabs();
      requestRender({ preserveScroll: true });
    }
  } finally {
    streamWatchdogBusy = false;
  }
}, 1000);

// --- Background Stream Recovery ---
// When the browser tab loses focus or the page is hidden, streaming responses
// from non-EventSource tabs (Codex, OpenRouter) may be dropped because the
// fetch body reader stops consuming. This function is called by the periodic
// stream watchdog to detect and recover from incomplete streams.
// Claude/Anthropic tabs use persistent EventSource connections which survive
// background transitions, so they skip this recovery path.
async function recoverActiveStreamFromBackground() {
  const tab = activeTab();
  if (!tab || !tab.wasLoading) return;
  // Claude tabs with EventSource don't need legacy resume recovery
  if (_isClaudeModel(tab.modelKey) && _claudeEvtSources[tab.sessionId]) return;
  // Anthropic tabs with EventSource don't need legacy resume recovery
  if (_isAnthropicModel(tab.modelKey) && _anthropicEvtSources[tab.sessionId]) return;

  tab.wasLoading = true;
  if (!tab.loadingSinceMs) tab.loadingSinceMs = Date.now();
  tab.activeToolLabel = tab.activeToolLabel || 'Reconnecting stream…';
  if (!tab.streamingText) tab.streamingText = '';
  persistTabs();
  requestRender({ preserveScroll: true });

  const fullTextRef = { text: tab.streamingText || '' };
  const recovered = await resumeStreamForTab(tab, fullTextRef, { attempts: 4, pauseMs: 900 });

  if (!recovered) {
    const st = await fetchTaskStatus(tab.sessionId);
    if (st && st.status === 'running') {
      // Leave loading state active; periodic loop will keep attempting resumes.
      tab.wasLoading = true;
      tab.activeToolLabel = 'Reconnecting stream…'; tab.activeToolDetail = null;
      persistTabs();
      requestRender({ preserveScroll: true });
      return;
    }
  }

  const def = MODELS[tab.modelKey] || MODELS.codex;
  const lastMsg = (tab.messages || [])[tab.messages.length - 1];
  const lastText = String(lastMsg?.text || '').trim();
  const nextText = String(fullTextRef.text || '').trim();
  // Only finalize if we have new content that wasn't already pushed
  if (nextText && !(lastMsg && lastMsg.role === 'assistant' && lastText === nextText)) {
    finalizeTurn(tab, fullTextRef.text, def, { runId: tab.currentRunId || '' });
  } else {
    // No new text — still clean up loading state
    tab._doneThinking = '';
    tab._doneTokens = null;
    tab.streamingText = '';
    tab.thinkingText = '';
    tab.wasLoading = false;
    tab.loadingSinceMs = 0;
    tab.activeToolLabel = null; tab.activeToolDetail = null;
    persistTabs();
    requestRender({ preserveScroll: true });
    refreshMeta();
    _drainMessageQueue(tab);
  }
}

function startStreamRecoveryLoop() {
  if (streamRecoveryTimer) return;
  streamRecoveryTimer = setInterval(async () => {
    if (streamRecoveryBusy) return;
    if (document.hidden) return;
    const tab = activeTab();
    if (!tab || !tab.wasLoading) return;
    streamRecoveryBusy = true;
    try {
      await recoverActiveStreamFromBackground();
    } finally {
      streamRecoveryBusy = false;
    }
  }, 3000);
}

function stopStreamRecoveryLoop() {
  if (!streamRecoveryTimer) return;
  clearInterval(streamRecoveryTimer);
  streamRecoveryTimer = null;
}

window.addEventListener('pageshow', () => {
  recoverActiveStreamFromBackground();
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) recoverActiveStreamFromBackground();
});

// --- Boot ---
async function boot() {
  // Fetch app name from server config (allows branding via env var)
  try {
    const _st = await fetch(API + '/api/status', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    const _sd = await _st.json();
    if (_sd.app_name) APP_NAME = _sd.app_name;
  } catch (_) {}
  document.title = APP_NAME;
  loadAutoNamePrefs();
  if (autoNameEnabled) startAutoNameScheduler();
  startStreamRecoveryLoop();
  await _loadOpenRouterModels();
  await _loadAnthropicModels();
  try {
    const res = await fetch(API + '/auth/status', {
      cache: 'no-store',
      headers: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' },
    });
    const data = await res.json();
    if (!data.setup_complete) { window.location.href = '/setup.html'; return; }
    if (!data.logged_in) { window.location.href = '/login.html'; return; }
    authenticated = data.authenticated;
    _openaiConnected = Boolean(data.openai_connected);
    userName = data.user || '';
    isLocalClient = Boolean(data.is_localhost);
    if (authenticated) {
      // Fast-paint: restore localStorage cache for instant UI, but server will overwrite.
      // This prevents a blank screen while the server fetch completes.
      restoreTabsFromStorage();
      requestRender();

      // Server is the sole source of truth for tabs. Overwrite localStorage state.
      // forceServerLabels + serverAuthoritative ensures server wins on all fields.
      await syncTabsFromServerSessions(80, { forceServerLabels: true, serverAuthoritative: true });

      // Only create a default tab if the server returned nothing.
      if (tabs.length === 0) {
        const dm = _defaultModel();
        if (_isModelAvailable(dm, MODELS[dm])) {
          const tab = createTab(dm, { silent: true, explicit: false });
          editingTabId = null;
          editingTabRendered = false;
          activeTabId = tab.id;
        }
      }

      // Ensure a manager tab always exists (recreated on every boot if closed)
      // But only if at least one model is actually connected
      if (!tabs.some(t => t.workerIdentity === 'dev-manager')) {
        const mk = _bestModelForWorker('dev-manager');
        if (_isModelAvailable(mk, MODELS[mk])) {
          createTab(mk, { silent: true, explicit: false, workerIdentity: 'dev-manager', label: 'Manager' });
        }
      }
      _sortManagerTabsFirst();

      enforceReasoningForActiveTab();

      // Load all tab histories in parallel instead of sequentially
      await Promise.all(tabs.map(t => loadTabHistory(t, 120)));
      persistTabs();
      pushTabsToServer();
      // Persistent Claude EventSource (streaming)
      connectClaudeEvents();
      // Persistent Anthropic EventSource (mirrors Claude pattern)
      connectAnthropicEvents();
      // Global broadcast SSE (cross-device sync: tab changes, new chat messages)
      connectGlobalEvents();
      // If iOS suspended the tab mid-stream and we reloaded, attempt recovery now.
      recoverActiveStreamFromBackground();
    }
    requestRender();
    // Defer non-critical metadata (usage stats, token counts) until after UI is rendered
    refreshMeta();
    // Check for updates once per calendar day (non-blocking)
    checkForUpdatesDaily();
    // Start polling for email draft badge count
    startDraftBadgePoll();

    // Check for ?editor= query param (e.g. from settings "Open in File Editor" button)
    const urlParams = new URLSearchParams(window.location.search);
    const editorPath = urlParams.get('editor');
    if (editorPath) {
      // Clean the URL so a refresh doesn't re-trigger editor mode
      history.replaceState(null, '', '/');
      setAppMode('editor', editorPath);
    }
  } catch { requestRender({ preserveScroll: true }); }
}

// --- Email draft badge poll ---
function updateDraftBadgeDOM() {
  // Direct DOM update — works even when full renders are skipped (email/editor mode)
  const existing = document.getElementById('email-draft-badge');
  if (_emailDraftCount > 0) {
    if (existing) {
      existing.textContent = _emailDraftCount;
    } else {
      // Badge doesn't exist in DOM yet — find the email button and inject it
      const emailBtn = document.querySelector('.sidebar-mode-btn[onclick*="email"]');
      if (emailBtn) {
        emailBtn.style.position = 'relative';
        const badge = document.createElement('span');
        badge.className = 'email-draft-badge';
        badge.id = 'email-draft-badge';
        badge.textContent = _emailDraftCount;
        // Insert before the label span
        const label = emailBtn.querySelector('.sidebar-mode-label');
        if (label) emailBtn.insertBefore(badge, label);
        else emailBtn.appendChild(badge);
      }
    }
  } else if (existing) {
    existing.remove();
  }
}

async function pollDraftBadge() {
  if (typeof EmailModule === 'undefined') return;
  const changed = await EmailModule.pollDraftCount();
  const newCount = EmailModule.getDraftCount();
  if (newCount !== _emailDraftCount) {
    _emailDraftCount = newCount;
    updateDraftBadgeDOM();
  }
}

function startDraftBadgePoll() {
  if (_draftBadgeTimer) return;
  // Initial fetch
  pollDraftBadge();
  // Poll every 60s
  _draftBadgeTimer = setInterval(pollDraftBadge, 60000);
}

// --- Daily update check (once per calendar day) ---
async function checkForUpdatesDaily() {
  const today = new Date().toISOString().slice(0, 10);
  const lastCheck = localStorage.getItem('kukuibot.lastUpdateCheck');
  if (lastCheck === today) {
    // Already checked today — restore cached result
    _updateAvailable = localStorage.getItem('kukuibot.updateAvailable') === '1';
    _updateBehindCount = parseInt(localStorage.getItem('kukuibot.updateBehind') || '0', 10);
    if (_updateAvailable) requestRender({ preserveScroll: true });
    return;
  }
  try {
    const r = await fetch(API + '/api/update/check', { method: 'POST', credentials: 'same-origin', signal: AbortSignal.timeout(20000) });
    if (!r.ok) return;
    const d = await r.json();
    _updateAvailable = !d.up_to_date && d.behind > 0;
    _updateBehindCount = d.behind || 0;
    localStorage.setItem('kukuibot.lastUpdateCheck', today);
    localStorage.setItem('kukuibot.updateAvailable', _updateAvailable ? '1' : '0');
    localStorage.setItem('kukuibot.updateBehind', String(_updateBehindCount));
    if (_updateAvailable) requestRender({ preserveScroll: true });
  } catch {}
}

// --- Claude Bridge health poll (for sidebar status dot) ---
async function pollClaudeBridgeHealth() {
  try {
    const r = await fetch('/claude/api-pool/status', { credentials: 'same-origin' });
    if (!r.ok) { _claudeBridgeUp = false; return; }
    const d = await r.json();
    // Mark as up if bridges are running OR Claude auth is configured (bridges spawn on demand)
    const bridgesRunning = d.active_bridges && d.active_bridges.some(b => b.running);
    _claudeBridgeUp = bridgesRunning || !!d.configured;
  } catch { _claudeBridgeUp = false; }
}
pollClaudeBridgeHealth();
setInterval(pollClaudeBridgeHealth, 30000);

// --- Delegation task polling (for sidebar badges) ---
async function pollDelegatedTasks() {
  try {
    const tab = activeTab();
    const sid = tab ? tab.sessionId : '';
    const r = await fetch(`/api/delegated-tasks?session_id=${encodeURIComponent(sid)}&compact=1`, { credentials: 'same-origin' });
    if (!r.ok) return;
    const data = await r.json();
    if (!data.ok) return;
    _delegatedTaskCache = { outgoing: data.outgoing || [], outgoing_session: sid, incoming: data.incoming || [], ts: Date.now() };
    _checkDelegActivityPollNeeded();
    requestRender({ preserveScroll: true });
  } catch {}
}
pollDelegatedTasks();
setInterval(pollDelegatedTasks, 10000);

function _delegIncomingForTab(tab) {
  if (!tab) return [];
  return _delegatedTaskCache.incoming.filter(t =>
    t.target_session_id === tab.sessionId || t.target_base_session_id === tab.sessionId
  );
}
function _delegOutgoingCount(tab) {
  if (!tab) return 0;
  if (tab.sessionId !== _delegatedTaskCache.outgoing_session) return 0;
  return _delegatedTaskCache.outgoing.filter(t => t.status === 'running' || t.status === 'dispatched').length;
}

// --- Delegation Activity Bars (real-time tool status for delegated workers) ---

const _delegModelColors = {
  codex: '#10b981',        // green
  claude_opus: '#a78bfa',  // purple
  claude_sonnet: '#818cf8', // indigo
  anthropic: '#f97316',    // orange
  openrouter: '#06b6d4',   // cyan
};

const _delegToolLabels = {
  bash:'⚙️ bash', bash_background:'⚙️ bash_bg', bash_check:'⚙️ bash_check',
  read_file:'📄 read', write_file:'✏️ write', edit_file:'✏️ edit',
  memory_search:'🔍 memory', memory_read:'📄 memory', spawn_agent:'🤖 agent',
  Bash:'⚙️ bash', Read:'📄 read', Write:'✏️ write', Edit:'✏️ edit',
  Grep:'🔍 grep', Glob:'🔍 glob', WebSearch:'🌐 search', WebFetch:'🌐 fetch',
  _thinking:'🧠 thinking', _generating:'💬 generating',
};

async function pollDelegActivity() {
  try {
    const r = await fetch('/api/delegate/activity', { credentials: 'same-origin' });
    if (!r.ok) return;
    const data = await r.json();
    if (!data.ok) return;
    const prev = _delegActivityCache;
    _delegActivityCache = data.activities || [];

    // Detect tasks that were active before but are now gone (completed)
    const currentIds = new Set(_delegActivityCache.map(a => a.task_id));
    for (const p of prev) {
      if (!currentIds.has(p.task_id) && !_delegActivityDismissed.has(p.task_id)) {
        // Task completed — show brief "Done" state then auto-remove
        if (!_delegActivityDoneTimers[p.task_id]) {
          _delegActivityCache.push({ ...p, status: 'completed', tool_name: null, tool_detail: null });
          _delegActivityDoneTimers[p.task_id] = setTimeout(() => {
            _delegActivityDismissed.add(p.task_id);
            delete _delegActivityDoneTimers[p.task_id];
            _updateDelegBarsDOM();
          }, 3000);
        }
      }
    }

    _updateDelegBarsDOM();
  } catch {}
}

function _updateDelegBarsDOM() {
  const host = document.getElementById('deleg-bars-host');
  if (host) host.innerHTML = renderDelegationBars();
}

function _startDelegActivityPoll() {
  if (_delegActivityTimer) return;
  _delegActivityTimer = setInterval(pollDelegActivity, 2500);
  pollDelegActivity(); // immediate first poll
}

function _stopDelegActivityPoll() {
  if (_delegActivityTimer) {
    clearInterval(_delegActivityTimer);
    _delegActivityTimer = null;
  }
}

function dismissDelegBar(taskId) {
  _delegActivityDismissed.add(taskId);
  if (_delegActivityDoneTimers[taskId]) {
    clearTimeout(_delegActivityDoneTimers[taskId]);
    delete _delegActivityDoneTimers[taskId];
  }
  _updateDelegBarsDOM();
  // Persist dismiss to DB so it survives page refresh
  fetch(API + '/api/delegate/dismiss', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id: taskId }),
  }).catch(() => {});
  // Cancel the sub-worker's active generation to stop burning tokens
  const task = _delegActivityCache.find(a => a.task_id === taskId);
  if (task && task.target_session_id) {
    fetch(API + '/api/chat/cancel?session_id=' + encodeURIComponent(task.target_session_id), {
      method: 'POST',
    }).catch(() => {});
  }
}

function renderDelegationBars() {
  const visible = _delegActivityCache.filter(a => !_delegActivityDismissed.has(a.task_id));
  if (visible.length === 0) {
    _stopDelegActivityPoll();
    return '';
  }

  return visible.map(a => {
    const color = _delegModelColors[a.model] || '#5eb5f7';
    const shortId = (a.task_id || '').slice(-8);
    const workerKey = String(a.worker || '').trim();
    const modelKey = String(a.model || '').trim();
    const showWorkerIcon = workerKey === 'dev-manager';
    const workerName = _getWorkerTabLabel(workerKey, modelKey);
    const modelLogo = _getModelLogoForKey(modelKey);
    const isDone = a.status === 'completed';

    let toolText = '';
    if (isDone) {
      toolText = '<span class="deleg-done-label">Done</span>';
    } else if (a.tool_name) {
      const label = _delegToolLabels[a.tool_name] || `🔧 ${a.tool_name}`;
      const detail = a.tool_detail ? ` <span class="deleg-tool-detail">${escText(a.tool_detail.slice(0, 80))}</span>` : '';
      toolText = `${escText(label)}${detail}`;
    } else if (a.is_active) {
      toolText = '<span class="typing-dots mini"><div class="dot"></div><div class="dot"></div><div class="dot"></div></span>';
    } else if (a.status === 'dispatched') {
      toolText = '<span class="deleg-status-text">Dispatched, waiting…</span>';
    }

    const dots = (a.is_active && !isDone) ? '<span class="typing-dots mini"><div class="dot"></div><div class="dot"></div><div class="dot"></div></span>' : '';

    return `<div class="deleg-bar${isDone ? ' done' : ''}" style="border-left-color:${color}">
      <span class="deleg-bar-left">
        <span class="deleg-worker-row model-${escText(modelKey)}">
          ${showWorkerIcon ? `<span class="deleg-worker-icon">${_getWorkerIcon({ workerIdentity: workerKey })}</span>` : ''}
          <span class="deleg-worker-model-logo">${modelLogo}</span>
          <span class="deleg-worker">${escText(workerName)}</span>
        </span>
        <span class="deleg-tool">${toolText}</span>
      </span>
      <span class="deleg-bar-right">
        ${dots}
        <span class="deleg-task-id">${escText(shortId)}</span>
        <button class="deleg-dismiss" data-task-id="${escText(a.task_id)}" title="Dismiss">×</button>
      </span>
    </div>`;
  }).join('');
}

// Delegated click handler for dismiss buttons
document.addEventListener('click', function(e) {
  const btn = e.target.closest('.deleg-dismiss');
  if (!btn) return;
  const taskId = btn.dataset.taskId;
  if (taskId) dismissDelegBar(taskId);
});

// Auto-start/stop activity polling based on delegation cache
function _checkDelegActivityPollNeeded() {
  const hasActive = _delegatedTaskCache.incoming.some(t => t.status === 'dispatched' || t.status === 'running') ||
                    _delegatedTaskCache.outgoing.some(t => t.status === 'dispatched' || t.status === 'running');
  if (hasActive) {
    _startDelegActivityPoll();
  } else if (_delegActivityCache.filter(a => !_delegActivityDismissed.has(a.task_id)).length === 0) {
    _stopDelegActivityPoll();
  }
}

// --- Periodic context refresh (keeps token count current in status bar) ---
setInterval(() => {
  const tab = activeTab();
  if (tab && !isTabLoading(tab)) refreshMeta();
}, 30000);

boot();
