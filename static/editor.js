/**
 * KukuiBot File Editor — Ace Editor integration
 * Loaded lazily when user switches to editor mode.
 */

const EditorModule = (function () {
  'use strict';

  // --- State ---
  let aceEditor = null;
  let currentFile = null;    // { path, language, readonly }
  let isDirty = false;
  let originalContent = '';
  let fileTreeRoot = '';     // current root path shown in tree
  let expandedDirs = new Set();
  let filterText = '';
  let editorReady = false;
  let _currentTreePath = '';  // last loaded tree path (for mobile dropdown)

  // Ace theme mapping (matches KukuiBot themes)
  const ACE_THEMES = {
    'default': 'ace/theme/cobalt',
    'blue': 'ace/theme/cobalt',
    'sol-dark': 'ace/theme/tomorrow_night',
    'sol-light': 'ace/theme/tomorrow',
    'claude-theme': 'ace/theme/one_dark',
  };

  // --- Initialization ---

  function init() {
    if (editorReady) return;
    if (typeof ace === 'undefined') {
      console.error('Ace editor not loaded');
      return;
    }

    const container = document.getElementById('ace-editor');
    if (!container) return;

    aceEditor = ace.edit(container);
    aceEditor.setShowPrintMargin(false);
    aceEditor.setFontSize(14);
    aceEditor.session.setUseWrapMode(true);
    aceEditor.session.setTabSize(4);
    aceEditor.setOption('scrollPastEnd', 0.5);

    // Apply theme
    syncTheme();

    // Dirty tracking
    aceEditor.session.on('change', () => {
      if (!currentFile) return;
      const newDirty = aceEditor.getValue() !== originalContent;
      if (newDirty !== isDirty) {
        isDirty = newDirty;
        updateToolbar();
      }
    });

    // Keybindings
    aceEditor.commands.addCommand({
      name: 'save',
      bindKey: { win: 'Ctrl-S', mac: 'Cmd-S' },
      exec: () => save(),
    });

    editorReady = true;

    // Load file tree
    loadTree();
  }

  function destroy() {
    if (aceEditor) {
      aceEditor.destroy();
      aceEditor = null;
    }
    editorReady = false;
    currentFile = null;
    isDirty = false;
    originalContent = '';
    expandedDirs.clear();
    filterText = '';
  }

  // --- Theme sync ---

  function syncTheme() {
    if (!aceEditor) return;
    const theme = localStorage.getItem('kukuibot.theme') || 'default';
    // Check for body class claude-theme
    const bodyClasses = document.body.className;
    let themeKey = theme;
    if (bodyClasses.includes('claude-theme')) themeKey = 'claude-theme';
    const aceTheme = ACE_THEMES[themeKey] || ACE_THEMES['default'];
    aceEditor.setTheme(aceTheme);
  }

  // --- File Tree ---

  async function loadTree(path) {
    if (!path) {
      // Default to workspace root (from first API call)
      path = fileTreeRoot || '';
    }
    try {
      const url = '/api/files/tree' + (path ? '?path=' + encodeURIComponent(path) : '');
      const res = await fetch(url);
      const data = await res.json();
      if (data.error) {
        console.error('File tree error:', data.error);
        return;
      }
      if (!fileTreeRoot) fileTreeRoot = data.path;
      _currentTreePath = data.path;
      renderTree(data.path, data.entries);
      // Also update mobile dropdown if open
      if (_mobileDropdownOpen) _renderMobileDropdownList(data.path, data.entries);
    } catch (err) {
      console.error('Failed to load file tree:', err);
    }
  }

  function renderTree(parentPath, entries) {
    const host = document.getElementById('file-tree');
    if (!host) return;

    // Store entries for this path
    host.dataset.path = parentPath;

    const filter = filterText.toLowerCase();
    const filtered = filter
      ? entries.filter(e => e.name.toLowerCase().includes(filter))
      : entries;

    let html = '';
    // Show parent nav if not at root
    if (parentPath !== fileTreeRoot) {
      const parent = parentPath.split('/').slice(0, -1).join('/') || '/';
      html += `<div class="ft-item ft-parent" onclick="EditorModule.navigateUp('${escAttr(parent)}')">
        <span class="ft-icon">..</span>
        <span class="ft-name">..</span>
      </div>`;
    }

    for (const entry of filtered) {
      if (entry.type === 'dir') {
        const isExpanded = expandedDirs.has(entry.path);
        html += `<div class="ft-item ft-dir${isExpanded ? ' expanded' : ''}" onclick="EditorModule.toggleDir('${escAttr(entry.path)}', this)" data-path="${escAttr(entry.path)}">
          <span class="ft-icon">${isExpanded ? '&#9662;' : '&#9656;'}</span>
          <span class="ft-folder-icon">${isExpanded ? '\u{1F4C2}' : '\u{1F4C1}'}</span>
          <span class="ft-name">${esc(entry.name)}</span>
        </div>`;
        if (isExpanded) {
          html += `<div class="ft-children" id="ft-children-${cssId(entry.path)}"></div>`;
        }
      } else {
        const isActive = currentFile && currentFile.path === entry.path;
        const sizeStr = entry.size != null ? formatSize(entry.size) : '';
        html += `<div class="ft-item ft-file${isActive ? ' active' : ''}" onclick="EditorModule.openFile('${escAttr(entry.path)}')" title="${esc(entry.path)}${sizeStr ? ' (' + sizeStr + ')' : ''}">
          <span class="ft-icon">&nbsp;</span>
          <span class="ft-name">${esc(entry.name)}</span>
        </div>`;
      }
    }

    host.innerHTML = html;

    // Load expanded dirs
    for (const entry of filtered) {
      if (entry.type === 'dir' && expandedDirs.has(entry.path)) {
        loadSubTree(entry.path);
      }
    }
  }

  async function loadSubTree(dirPath) {
    try {
      const res = await fetch('/api/files/tree?path=' + encodeURIComponent(dirPath));
      const data = await res.json();
      if (data.error) return;

      const host = document.getElementById('ft-children-' + cssId(dirPath));
      if (!host) return;

      const filter = filterText.toLowerCase();
      const filtered = filter
        ? data.entries.filter(e => e.name.toLowerCase().includes(filter))
        : data.entries;

      let html = '';
      for (const entry of filtered) {
        if (entry.type === 'dir') {
          const isExpanded = expandedDirs.has(entry.path);
          html += `<div class="ft-item ft-dir${isExpanded ? ' expanded' : ''}" onclick="EditorModule.toggleDir('${escAttr(entry.path)}', this)" data-path="${escAttr(entry.path)}">
            <span class="ft-icon">${isExpanded ? '&#9662;' : '&#9656;'}</span>
            <span class="ft-folder-icon">${isExpanded ? '\u{1F4C2}' : '\u{1F4C1}'}</span>
            <span class="ft-name">${esc(entry.name)}</span>
          </div>`;
          if (isExpanded) {
            html += `<div class="ft-children" id="ft-children-${cssId(entry.path)}"></div>`;
          }
        } else {
          const isActive = currentFile && currentFile.path === entry.path;
          html += `<div class="ft-item ft-file${isActive ? ' active' : ''}" onclick="EditorModule.openFile('${escAttr(entry.path)}')" title="${esc(entry.path)}">
            <span class="ft-icon">&nbsp;</span>
            <span class="ft-name">${esc(entry.name)}</span>
          </div>`;
        }
      }
      host.innerHTML = html;

      // Recurse for expanded subdirs
      for (const entry of filtered) {
        if (entry.type === 'dir' && expandedDirs.has(entry.path)) {
          loadSubTree(entry.path);
        }
      }
    } catch (err) {
      console.error('Failed to load subtree:', err);
    }
  }

  function toggleDir(path, el) {
    if (expandedDirs.has(path)) {
      expandedDirs.delete(path);
    } else {
      expandedDirs.add(path);
    }
    // Re-render from current tree root
    loadTree(document.getElementById('file-tree')?.dataset.path || fileTreeRoot);
  }

  function navigateUp(path) {
    loadTree(path);
  }

  function breadcrumbNavigate(path) {
    loadTree(path);
  }

  function onFilterInput(value) {
    filterText = value;
    loadTree(document.getElementById('file-tree')?.dataset.path || fileTreeRoot);
  }

  // --- File Operations ---

  async function openFile(path) {
    // Check dirty state
    if (isDirty) {
      if (!confirm('You have unsaved changes. Discard them?')) return;
    }

    try {
      const res = await fetch('/api/files/read?path=' + encodeURIComponent(path));
      const data = await res.json();

      if (data.error) {
        alert(data.error);
        return;
      }

      // Warn about large files
      if (data.size > 500 * 1024) {
        if (!confirm(`This file is ${formatSize(data.size)}. Editing large files may be slow. Open anyway?`)) return;
      }

      currentFile = {
        path: data.path,
        language: data.language,
        readonly: data.readonly,
      };
      originalContent = data.content;
      isDirty = false;

      if (aceEditor) {
        aceEditor.session.setValue(data.content);
        aceEditor.session.setMode('ace/mode/' + (data.language || 'text'));
        aceEditor.setReadOnly(!!data.readonly);
        aceEditor.clearSelection();
        aceEditor.moveCursorTo(0, 0);
        aceEditor.focus();
      }

      updateToolbar();
      updateFileTreeSelection();

      // Store last file
      localStorage.setItem('kukuibot.editor.lastFile', path);
    } catch (err) {
      alert('Failed to open file: ' + err.message);
    }
  }

  async function save() {
    if (!currentFile || currentFile.readonly) return;
    if (!aceEditor) return;

    const content = aceEditor.getValue();
    try {
      const res = await fetch('/api/files/write', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: currentFile.path, content }),
      });
      const data = await res.json();
      if (data.error) {
        alert('Save failed: ' + data.error);
        return;
      }

      originalContent = content;
      isDirty = false;
      updateToolbar();

      // Flash save indicator
      const indicator = document.getElementById('editor-save-indicator');
      if (indicator) {
        indicator.textContent = 'Saved';
        indicator.classList.add('show');
        setTimeout(() => indicator.classList.remove('show'), 1500);
      }
    } catch (err) {
      alert('Save failed: ' + err.message);
    }
  }

  function revert() {
    if (!currentFile || !aceEditor) return;
    if (!isDirty) return;
    if (!confirm('Revert to last saved version?')) return;

    aceEditor.session.setValue(originalContent);
    aceEditor.clearSelection();
    isDirty = false;
    updateToolbar();
  }

  // --- UI Updates ---

  function updateToolbar() {
    const pathEl = document.getElementById('editor-file-path');
    const langEl = document.getElementById('editor-lang');
    const saveBtn = document.getElementById('editor-save-btn');
    const revertBtn = document.getElementById('editor-revert-btn');
    const dirtyDot = document.getElementById('editor-dirty-dot');
    const readonlyBadge = document.getElementById('editor-readonly-badge');
    const cursorEl = document.getElementById('editor-cursor-pos');

    if (pathEl) {
      if (!currentFile) {
        pathEl.innerHTML = 'No file open';
        pathEl.title = '';
      } else {
        // Build clickable breadcrumb from path
        const display = currentFile.path.replace(/.*\/\.kukuibot\//, '~/');
        const segments = display.split('/');
        const filename = segments.pop();
        let bcHtml = '';
        // Rebuild absolute path segments for click targets
        const absParts = currentFile.path.split('/');
        const fname = absParts.pop(); // remove filename
        let cumPath = '';
        for (let i = 0; i < absParts.length; i++) {
          cumPath += (i === 0 ? '' : '/') + absParts[i];
          const label = segments[i] != null ? segments[i] : absParts[i];
          if (label === '') continue;
          bcHtml += `<span class="bc-seg" onclick="EditorModule.breadcrumbNavigate('${escAttr(cumPath)}')">${esc(label)}</span><span class="bc-sep">/</span>`;
        }
        bcHtml += `<span class="bc-file">${esc(filename)}</span>`;
        pathEl.innerHTML = bcHtml;
        pathEl.title = currentFile.path;
      }
    }
    if (langEl) langEl.textContent = currentFile ? (currentFile.language || 'text') : '';
    if (saveBtn) {
      saveBtn.disabled = !isDirty || !currentFile || currentFile.readonly;
    }
    if (revertBtn) {
      revertBtn.disabled = !isDirty;
    }
    if (dirtyDot) {
      dirtyDot.style.display = isDirty ? 'inline-block' : 'none';
    }
    if (readonlyBadge) {
      readonlyBadge.style.display = currentFile?.readonly ? 'inline-block' : 'none';
    }
    if (cursorEl && aceEditor) {
      const pos = aceEditor.getCursorPosition();
      cursorEl.textContent = `Ln ${pos.row + 1}, Col ${pos.column + 1}`;
    }
    // Update mobile filter placeholder with current filename
    const mobileFilterEl = document.getElementById('mobile-editor-filter');
    if (mobileFilterEl) {
      mobileFilterEl.placeholder = getCurrentFileName();
    }
    // Update mobile save button
    const mobileSaveBtn = document.getElementById('mobile-editor-save');
    if (mobileSaveBtn) {
      mobileSaveBtn.disabled = !isDirty || !currentFile || (currentFile && currentFile.readonly);
    }
  }

  function updateFileTreeSelection() {
    const tree = document.getElementById('file-tree');
    if (!tree) return;
    tree.querySelectorAll('.ft-file.active').forEach(el => el.classList.remove('active'));
    if (currentFile) {
      tree.querySelectorAll('.ft-file').forEach(el => {
        if (el.getAttribute('onclick')?.includes(currentFile.path)) {
          el.classList.add('active');
        }
      });
    }
  }

  // --- Dirty guard for beforeunload ---

  function getDirty() { return isDirty; }

  // --- Cursor position tracking ---

  function setupCursorTracking() {
    if (!aceEditor) return;
    aceEditor.selection.on('changeCursor', () => {
      const cursorEl = document.getElementById('editor-cursor-pos');
      if (cursorEl) {
        const pos = aceEditor.getCursorPosition();
        cursorEl.textContent = `Ln ${pos.row + 1}, Col ${pos.column + 1}`;
      }
    });
  }

  // Called after init to set up extra hooks
  function postInit() {
    setupCursorTracking();
    // Reopen last file if any
    const lastFile = localStorage.getItem('kukuibot.editor.lastFile');
    if (lastFile) {
      openFile(lastFile);
    }
  }

  // --- Helpers ---

  function esc(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function escAttr(str) {
    return str.replace(/'/g, "\\'").replace(/"/g, '&quot;');
  }

  function cssId(path) {
    return path.replace(/[^a-zA-Z0-9_-]/g, '_');
  }

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  // --- Mobile File Dropdown (inline below mobile bar) ---

  let _mobileDropdownOpen = false;
  let _mobileDropdownFilter = '';

  function openMobileDropdown() {
    if (_mobileDropdownOpen) return;
    _mobileDropdownOpen = true;
    _mobileDropdownFilter = '';

    // Build dropdown if not in DOM
    let dd = document.getElementById('mobile-file-dropdown');
    if (!dd) {
      const backdrop = document.createElement('div');
      backdrop.className = 'mfd-backdrop';
      backdrop.id = 'mfd-backdrop';
      backdrop.onclick = () => closeMobileDropdown();

      dd = document.createElement('div');
      dd.className = 'mfd-dropdown';
      dd.id = 'mobile-file-dropdown';
      dd.innerHTML = `
        <div class="mfd-breadcrumb" id="mfd-breadcrumb"></div>
        <div class="mfd-list" id="mfd-list"></div>
      `;

      document.body.appendChild(backdrop);
      document.body.appendChild(dd);

      requestAnimationFrame(() => {
        backdrop.classList.add('show');
        dd.classList.add('show');
      });
    } else {
      const backdrop = document.getElementById('mfd-backdrop');
      if (backdrop) backdrop.classList.add('show');
      dd.classList.add('show');
    }

    _loadMobileDropdownTree();
  }

  function closeMobileDropdown() {
    _mobileDropdownOpen = false;
    _mobileDropdownFilter = '';
    const dd = document.getElementById('mobile-file-dropdown');
    const backdrop = document.getElementById('mfd-backdrop');
    if (dd) dd.classList.remove('show');
    if (backdrop) backdrop.classList.remove('show');
    setTimeout(() => {
      if (dd) dd.remove();
      if (backdrop) backdrop.remove();
    }, 200);
    // Clear the filter input and reset placeholder
    const filterEl = document.getElementById('mobile-editor-filter');
    if (filterEl) {
      filterEl.value = '';
      filterEl.blur();
    }
  }

  async function _loadMobileDropdownTree() {
    const path = _currentTreePath || fileTreeRoot || '';
    try {
      const url = '/api/files/tree' + (path ? '?path=' + encodeURIComponent(path) : '');
      const res = await fetch(url);
      const data = await res.json();
      if (data.error) return;
      if (!fileTreeRoot) fileTreeRoot = data.path;
      _currentTreePath = data.path;
      _renderMobileDropdownList(data.path, data.entries);
    } catch (err) {
      console.error('Failed to load mobile dropdown tree:', err);
    }
  }

  function _renderMobileDropdownList(parentPath, entries) {
    const host = document.getElementById('mfd-list');
    if (!host) return;

    // Update breadcrumb
    const bcEl = document.getElementById('mfd-breadcrumb');
    if (bcEl) {
      const parts = parentPath.replace(fileTreeRoot, '').split('/').filter(Boolean);
      let bcHtml = `<span class="mfd-bc-item" onclick="EditorModule.mobileDropdownNavigate('${escAttr(fileTreeRoot)}')">/</span>`;
      let cumPath = fileTreeRoot;
      for (const part of parts) {
        cumPath += '/' + part;
        bcHtml += `<span class="mfd-bc-sep">/</span><span class="mfd-bc-item" onclick="EditorModule.mobileDropdownNavigate('${escAttr(cumPath)}')">${esc(part)}</span>`;
      }
      bcEl.innerHTML = bcHtml;
    }

    const filter = _mobileDropdownFilter.toLowerCase();
    const filtered = filter
      ? entries.filter(e => e.name.toLowerCase().includes(filter))
      : entries;

    const dirs = filtered.filter(e => e.type === 'dir').sort((a, b) => a.name.localeCompare(b.name));
    const files = filtered.filter(e => e.type !== 'dir').sort((a, b) => a.name.localeCompare(b.name));

    let html = '';

    // Parent nav
    if (parentPath !== fileTreeRoot) {
      const parent = parentPath.split('/').slice(0, -1).join('/') || '/';
      html += `<div class="mfd-item mfd-parent" onclick="EditorModule.mobileDropdownNavigate('${escAttr(parent)}')">
        <span class="mfd-icon">..</span>
        <span class="mfd-name">..</span>
      </div>`;
    }

    for (const entry of dirs) {
      html += `<div class="mfd-item mfd-dir" onclick="EditorModule.mobileDropdownNavigate('${escAttr(entry.path)}')">
        <span class="mfd-icon">\u{1F4C1}</span>
        <span class="mfd-name">${esc(entry.name)}</span>
      </div>`;
    }

    for (const entry of files) {
      const isActive = currentFile && currentFile.path === entry.path;
      const sizeStr = entry.size != null ? formatSize(entry.size) : '';
      html += `<div class="mfd-item mfd-file${isActive ? ' active' : ''}" onclick="EditorModule.mobileDropdownOpenFile('${escAttr(entry.path)}')">
        <span class="mfd-icon">\u{1F4C4}</span>
        <span class="mfd-name">${esc(entry.name)}</span>
        ${sizeStr ? `<span class="mfd-size">${sizeStr}</span>` : ''}
      </div>`;
    }

    if (!html) {
      html = '<div class="mfd-empty">No files found</div>';
    }

    host.innerHTML = html;
  }

  async function mobileDropdownNavigate(path) {
    _currentTreePath = path;
    try {
      const url = '/api/files/tree?path=' + encodeURIComponent(path);
      const res = await fetch(url);
      const data = await res.json();
      if (data.error) return;
      _renderMobileDropdownList(data.path, data.entries);
    } catch (err) {
      console.error('Failed to navigate:', err);
    }
  }

  function mobileDropdownOpenFile(path) {
    closeMobileDropdown();
    openFile(path);
  }

  function onMobileDropdownFilter(value) {
    _mobileDropdownFilter = value;
    // Ensure dropdown is open when typing
    if (!_mobileDropdownOpen) openMobileDropdown();
    // Also sync desktop sidebar filter
    filterText = value;
    const sidebarFilter = document.querySelector('.ft-filter');
    if (sidebarFilter && sidebarFilter.value !== value) sidebarFilter.value = value;
    _loadMobileDropdownTree();
  }

  // --- Getters for app.js ---

  function getCurrentFileName() {
    if (!currentFile) return 'No file open';
    const parts = currentFile.path.split('/');
    return parts[parts.length - 1];
  }

  function getCurrentFilePath() {
    return currentFile ? currentFile.path : '';
  }

  // --- Render the editor panel HTML (called from app.js) ---

  function renderEditorPanel() {
    return `
      <div class="editor-toolbar">
        <div class="editor-toolbar-left">
          <span id="editor-file-path" class="editor-file-path" title="">No file open</span>
          <span id="editor-dirty-dot" class="editor-dirty-dot" style="display:none" title="Unsaved changes"></span>
          <span id="editor-readonly-badge" class="editor-readonly-badge" style="display:none">READ-ONLY</span>
          <span id="editor-save-indicator" class="editor-save-indicator"></span>
        </div>
        <div class="editor-toolbar-right">
          <button id="editor-save-btn" class="editor-btn" onclick="EditorModule.save()" disabled title="Save (Cmd+S)">Save</button>
          <button id="editor-revert-btn" class="editor-btn" onclick="EditorModule.revert()" disabled title="Revert to saved">Revert</button>
        </div>
      </div>
      <div id="ace-editor" class="ace-editor-container"></div>
      <div class="editor-status-bar">
        <span id="editor-lang" class="editor-lang"></span>
        <span id="editor-cursor-pos" class="editor-cursor-pos"></span>
      </div>
    `;
  }

  function renderFileTreeSidebar() {
    return `
      <div class="ft-filter-wrap">
        <input type="text" class="ft-filter" placeholder="Filter files..." value="${esc(filterText)}" oninput="EditorModule.onFilterInput(this.value)" />
      </div>
      <div id="file-tree" class="file-tree"></div>
    `;
  }

  // --- Public API ---

  return {
    init,
    destroy,
    syncTheme,
    openFile,
    save,
    revert,
    toggleDir,
    navigateUp,
    breadcrumbNavigate,
    onFilterInput,
    loadTree,
    getDirty,
    postInit,
    renderEditorPanel,
    renderFileTreeSidebar,
    // Mobile file dropdown
    openMobileDropdown,
    closeMobileDropdown,
    mobileDropdownNavigate,
    mobileDropdownOpenFile,
    onMobileDropdownFilter,
    getCurrentFileName,
    getCurrentFilePath,
  };
})();
