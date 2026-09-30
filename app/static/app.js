// Progressive enhancement only: every page works without JavaScript.
(function () {
  'use strict';
  var shell = document.getElementById('shell');
  if (!shell) return;
  var mobile = window.matchMedia('(max-width: 900px)');

  function stored(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (e) { /* storage may be blocked */ }
    return null;
  }

  // ----- tiny markdown renderer for AI answers (headings, bold/italic, lists,
  // tables, hr, inline code) — the LLM replies in markdown, and showing it as
  // literal asterisks/hashes is unreadable, so this renders it as real HTML. -----
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function renderInline(text) {
    var s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__(.+?)__/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*\n]+?)\*/g, '<em>$1</em>');
    s = s.replace(/(^|[^\w])_([^_\n]+?)_(?!\w)/g, '$1<em>$2</em>');
    return s;
  }
  function splitTableRow(line) {
    var cells = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
    return cells.map(function (c) { return c.trim(); });
  }
  function renderMarkdown(text) {
    var lines = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
    var out = [];
    var list = null;
    function flushList() {
      if (!list) return;
      out.push('<' + list.tag + '>' + list.items.map(function (it) { return '<li>' + renderInline(it) + '</li>'; }).join('') + '</' + list.tag + '>');
      list = null;
    }
    var i = 0;
    while (i < lines.length) {
      var trimmed = lines[i].trim();

      if (trimmed === '') { flushList(); i++; continue; }

      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) { flushList(); out.push('<hr>'); i++; continue; }

      var heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (heading) {
        flushList();
        var level = Math.min(heading[1].length + 2, 6);
        out.push('<h' + level + '>' + renderInline(heading[2]) + '</h' + level + '>');
        i++; continue;
      }

      if (trimmed.indexOf('|') !== -1 && i + 1 < lines.length && /^[\s|:-]+$/.test(lines[i + 1]) && lines[i + 1].indexOf('-') !== -1) {
        flushList();
        var headCells = splitTableRow(trimmed);
        var rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() !== '' && lines[i].indexOf('|') !== -1) {
          rows.push(splitTableRow(lines[i]));
          i++;
        }
        var thead = '<thead><tr>' + headCells.map(function (c) { return '<th>' + renderInline(c) + '</th>'; }).join('') + '</tr></thead>';
        var tbody = '<tbody>' + rows.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + renderInline(c) + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody>';
        out.push('<div class="md-table-wrap"><table class="md-table">' + thead + tbody + '</table></div>');
        continue;
      }

      var ul = /^[*\-+]\s+(.*)$/.exec(trimmed);
      if (ul) {
        if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; }
        list.items.push(ul[1]);
        i++; continue;
      }
      var ol = /^\d+[.)]\s+(.*)$/.exec(trimmed);
      if (ol) {
        if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; }
        list.items.push(ol[1]);
        i++; continue;
      }

      flushList();
      var para = [];
      while (i < lines.length && lines[i].trim() !== '' &&
        !/^#{1,6}\s+/.test(lines[i].trim()) &&
        !/^(-{3,}|\*{3,}|_{3,})$/.test(lines[i].trim()) &&
        !/^[*\-+]\s+/.test(lines[i].trim()) &&
        !/^\d+[.)]\s+/.test(lines[i].trim())) {
        para.push(lines[i].trim());
        i++;
      }
      out.push('<p>' + renderInline(para.join(' ')) + '</p>');
    }
    flushList();
    return out.join('');
  }

  // ----- sidebar: collapsible on desktop, drawer on mobile -----
  var sidebarBtn = document.getElementById('toggle-sidebar');
  var scrim = document.getElementById('scrim');
  function syncSidebar() {
    var open = mobile.matches ? shell.classList.contains('sidebar-open') : !shell.classList.contains('sidebar-collapsed');
    if (sidebarBtn) sidebarBtn.setAttribute('aria-expanded', String(open));
    if (scrim) scrim.hidden = !(mobile.matches && open);
  }
  if (stored('rdw.sidebar') === 'collapsed') shell.classList.add('sidebar-collapsed');
  if (sidebarBtn) sidebarBtn.addEventListener('click', function () {
    if (mobile.matches) {
      shell.classList.toggle('sidebar-open');
    } else {
      shell.classList.toggle('sidebar-collapsed');
      stored('rdw.sidebar', shell.classList.contains('sidebar-collapsed') ? 'collapsed' : 'open');
    }
    syncSidebar();
  });
  if (scrim) scrim.addEventListener('click', function () { shell.classList.remove('sidebar-open'); syncSidebar(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && shell.classList.contains('sidebar-open')) { shell.classList.remove('sidebar-open'); syncSidebar(); }
  });
  if (mobile.addEventListener) mobile.addEventListener('change', function () { shell.classList.remove('sidebar-open'); syncSidebar(); });
  syncSidebar();

  // ----- inspector toggle -----
  var inspectorBtn = document.getElementById('toggle-inspector');
  if (inspectorBtn) {
    if (stored('rdw.inspector') === 'hidden') { shell.classList.add('inspector-hidden'); inspectorBtn.setAttribute('aria-expanded', 'false'); }
    inspectorBtn.addEventListener('click', function () {
      var hidden = shell.classList.toggle('inspector-hidden');
      inspectorBtn.setAttribute('aria-expanded', String(!hidden));
      stored('rdw.inspector', hidden ? 'hidden' : 'shown');
    });
  }

  // ----- user menu dropdown (topbar) -----
  // CSS :focus-within already opens this without JS (click/Tab into the
  // button or panel keeps focus inside .user-menu); JS just adds click-to-
  // toggle and click-outside-to-close on top of that baseline.
  var userMenuBtn = document.getElementById('user-menu-btn');
  var userMenu = userMenuBtn && userMenuBtn.closest('.user-menu');
  if (userMenu) {
    var closeUserMenu = function () {
      userMenu.classList.remove('open');
      userMenuBtn.setAttribute('aria-expanded', 'false');
      userMenuBtn.blur();
    };
    userMenuBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = !userMenu.classList.contains('open');
      userMenu.classList.toggle('open', open);
      userMenuBtn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', function (e) {
      if (!userMenu.contains(e.target)) closeUserMenu();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeUserMenu();
    });
  }

  // ----- live storage status in the sidebar (same endpoint Docker uses) -----
  var status = document.getElementById('store-status');
  if (status && window.fetch) {
    fetch(status.getAttribute('data-endpoint'), { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        Array.prototype.forEach.call(status.querySelectorAll('li'), function (li) {
          var state = data.services && data.services[li.getAttribute('data-store')];
          if (state) li.setAttribute('data-state', state);
        });
      })
      .catch(function () {
        Array.prototype.forEach.call(status.querySelectorAll('li'), function (li) { li.setAttribute('data-state', 'down'); });
      });
  }

  // ----- admin user list filter -----
  var userFilter = document.getElementById('user-filter');
  if (userFilter) userFilter.addEventListener('input', function () {
    var q = userFilter.value.trim().toLowerCase();
    Array.prototype.forEach.call(document.querySelectorAll('#user-table tbody tr'), function (row) {
      row.hidden = q !== '' && (row.getAttribute('data-username') || '').indexOf(q) === -1;
    });
  });

  // ----- source upload: label-triggered file input + drag-and-drop onto the whole sources panel -----
  var uploadInput = document.getElementById('file-input');
  var uploadForm = uploadInput && uploadInput.closest('form');
  var sourcesPanel = document.getElementById('sources-panel');
  if (uploadInput) {
    var MAX = 10 * 1024 * 1024;
    var uploadGo = document.getElementById('upload-go');
    var pickLabel = document.getElementById('file-pick-label');
    var fileCard = document.getElementById('upload-file');
    var fileName = document.getElementById('upload-file-name');
    var fileMeta = document.getElementById('upload-file-meta');
    var fileClear = document.getElementById('upload-file-clear');
    var formatSize = function (bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / 1048576).toFixed(2) + ' MB';
    };
    // Picked file -> card with its full name and size; the upload button is
    // only enabled when there is something (valid) to upload.
    var describeFile = function (file) {
      var tooBig = !!file && file.size > MAX;
      if (fileCard) {
        fileCard.hidden = !file;
        fileCard.classList.toggle('too-big', tooBig);
      }
      if (file && fileName) {
        fileName.textContent = file.name;
        fileName.title = file.name;
        fileMeta.textContent = formatSize(file.size) + (tooBig ? ' — vượt giới hạn 10 MB, chọn tệp khác' : ' — sẵn sàng tải lên');
      }
      if (pickLabel) pickLabel.textContent = file ? 'Đổi tệp' : 'Chọn tệp';
      if (uploadGo) uploadGo.disabled = !file || tooBig;
    };
    describeFile(uploadInput.files && uploadInput.files[0]);
    uploadInput.addEventListener('change', function () { describeFile(uploadInput.files && uploadInput.files[0]); });
    if (fileClear) fileClear.addEventListener('click', function () {
      uploadInput.value = '';
      describeFile(null);
    });

    if (sourcesPanel && uploadForm) {
      ['dragenter', 'dragover'].forEach(function (t) {
        sourcesPanel.addEventListener(t, function (e) { e.preventDefault(); sourcesPanel.classList.add('drag'); });
      });
      ['dragleave', 'dragend'].forEach(function (t) {
        sourcesPanel.addEventListener(t, function () { sourcesPanel.classList.remove('drag'); });
      });
      sourcesPanel.addEventListener('drop', function (e) {
        e.preventDefault();
        sourcesPanel.classList.remove('drag');
        var file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (!file) return;
        uploadInput.files = e.dataTransfer.files;
        describeFile(file);
        if (file.size > MAX) return;  // the card already explains why
        if (uploadForm.requestSubmit) uploadForm.requestSubmit(); else uploadForm.submit();
      });
    }
  }

  // ----- document tabs (all panes stay visible without JS) -----
  var tabs = document.getElementById('doc-tabs');
  if (tabs) {
    var buttons = Array.prototype.slice.call(tabs.querySelectorAll('[role="tab"]'));
    var select = function (btn) {
      buttons.forEach(function (b) {
        var on = b === btn;
        b.setAttribute('aria-selected', String(on));
        b.tabIndex = on ? 0 : -1;
        document.getElementById(b.getAttribute('aria-controls')).hidden = !on;
      });
    };
    tabs.hidden = false;
    buttons.forEach(function (b, i) {
      b.addEventListener('click', function () { select(b); });
      b.addEventListener('keydown', function (e) {
        var next = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : null;
        if (next === null) return;
        var target = buttons[(next + buttons.length) % buttons.length];
        select(target); target.focus();
      });
    });
    select(buttons[0]);
  }

  // ----- AI chat: floating action button + slide-over panel -----
  var aiPanel = document.getElementById('ai-panel');
  var aiFab = document.getElementById('ai-fab');
  if (aiPanel && aiFab) {
    var aiScrim = document.getElementById('ai-scrim');
    var aiClose = document.getElementById('ai-close');
    var aiThread = document.getElementById('ai-thread');
    var aiForm = document.getElementById('ai-form');

    shell.classList.add('ai-enhanced');
    aiFab.hidden = false;
    if (aiScrim) aiScrim.hidden = false;

    // Server-rendered answers (the stored chat history) are plain escaped
    // text — upgrade each one to markdown the same way live answers are.
    Array.prototype.forEach.call(aiThread.querySelectorAll('.md-source'), function (staticAnswer) {
      var mdWrap = document.createElement('div');
      mdWrap.className = 'md';
      mdWrap.innerHTML = renderMarkdown(staticAnswer.textContent);
      staticAnswer.replaceWith(mdWrap);
    });
    aiThread.scrollTop = aiThread.scrollHeight;

    // "New conversation": clear the stored history without a page reload.
    var clearForm = document.getElementById('ai-clear-form');
    var emptyTpl = document.getElementById('ai-empty-tpl');
    if (clearForm && window.fetch) {
      clearForm.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!aiThread.querySelector('.chat-bubble')) return;
        if (!window.confirm('Bắt đầu cuộc trò chuyện mới? Lịch sử hỏi đáp hiện tại sẽ bị xóa.')) return;
        fetch(clearForm.getAttribute('data-clear-url'), { method: 'DELETE' })
          .then(function (r) {
            if (!r.ok) throw new Error('clear failed');
            aiThread.innerHTML = emptyTpl ? emptyTpl.innerHTML : '';
          })
          .catch(function () {
            window.alert('Không xóa được lịch sử, thử lại sau.');
          });
      });
    }

    function openAi() {
      shell.classList.add('ai-open');
      aiFab.setAttribute('aria-expanded', 'true');
      var textarea = aiForm && aiForm.querySelector('textarea');
      if (textarea) textarea.focus();
    }
    function closeAi() {
      shell.classList.remove('ai-open');
      aiFab.setAttribute('aria-expanded', 'false');
      aiFab.focus();
    }
    aiFab.addEventListener('click', openAi);
    if (aiClose) aiClose.addEventListener('click', closeAi);
    if (aiScrim) aiScrim.addEventListener('click', closeAi);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && shell.classList.contains('ai-open')) closeAi();
    });

    function clearEmptyState() {
      var empty = aiThread.querySelector('.chat-empty');
      if (empty) empty.remove();
    }
    function avatarHtml(templateId) {
      var tpl = document.getElementById(templateId);
      return tpl ? tpl.innerHTML : '';
    }
    function addUserBubble(text) {
      clearEmptyState();
      var el = document.createElement('div');
      el.className = 'chat-bubble chat-user';
      el.innerHTML =
        '<span class="chat-avatar">' + avatarHtml('ai-user-avatar-tpl') + '</span>' +
        '<div class="chat-bubble-body"></div>';
      el.querySelector('.chat-bubble-body').textContent = text;
      aiThread.appendChild(el);
      return el;
    }
    function addThinkingBubble() {
      var el = document.createElement('div');
      el.className = 'chat-bubble chat-ai chat-thinking';
      el.innerHTML =
        '<span class="chat-avatar ai">' + avatarHtml('ai-ai-avatar-tpl') + '</span>' +
        '<div class="chat-bubble-body"><span class="thinking-dot"></span><span class="thinking-dot"></span><span class="thinking-dot"></span></div>';
      aiThread.appendChild(el);
      return el;
    }
    function renderAnswer(bubble, data) {
      bubble.classList.remove('chat-thinking');
      var body = bubble.querySelector('.chat-bubble-body');
      body.innerHTML = '<div class="md">' + renderMarkdown(data.answer) + '</div>';
      if (data.sources && data.sources.length) {
        var sources = document.createElement('div');
        sources.className = 'chat-sources';
        data.sources.forEach(function (s) {
          var chip = document.createElement('span');
          chip.className = 'chip';
          chip.textContent = (s.ref ? '[' + s.ref + '] ' : '') + s.original_name + ' · đoạn ' + (s.chunk_index + 1);
          sources.appendChild(chip);
        });
        body.appendChild(sources);
      }
      if (data.model) {
        var modelP = document.createElement('p');
        modelP.className = 'chat-model muted small-text';
        modelP.textContent = 'Mô hình: ' + data.model;
        body.appendChild(modelP);
      }
    }
    function renderError(bubble, message) {
      bubble.classList.remove('chat-thinking');
      bubble.classList.add('chat-error');
      bubble.querySelector('.chat-bubble-body').innerHTML = '';
      bubble.querySelector('.chat-bubble-body').textContent = message;
    }

    // Auto-grow the composer textarea instead of showing a scrollbar for short questions.
    var aiTextarea = aiForm && aiForm.querySelector('textarea[name="question"]');
    if (aiTextarea) {
      var growTextarea = function () {
        aiTextarea.style.height = 'auto';
        aiTextarea.style.height = Math.min(aiTextarea.scrollHeight, 120) + 'px';
      };
      aiTextarea.addEventListener('input', growTextarea);
    }

    function selectedDocumentIds() {
      var boxes = document.querySelectorAll('.source-check');
      if (!boxes.length) return null;
      return Array.prototype.map.call(document.querySelectorAll('.source-check:checked'), function (c) { return c.value; });
    }

    if (aiForm && window.fetch) {
      aiForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var textarea = aiForm.querySelector('textarea[name="question"]');
        var select = aiForm.querySelector('select[name="model"]');
        var submitBtn = document.getElementById('ai-submit');
        var question = textarea.value.trim();
        if (!question) return;

        addUserBubble(question);
        var thinking = addThinkingBubble();
        aiThread.scrollTop = aiThread.scrollHeight;
        textarea.value = '';
        if (aiTextarea) growTextarea();
        if (submitBtn) submitBtn.disabled = true;

        fetch(aiForm.getAttribute('data-ask-url'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ question: question, model: select ? (select.value || null) : null, document_ids: selectedDocumentIds() }),
        })
          .then(function (r) {
            return r.json().then(function (data) { return { ok: r.ok, data: data }; });
          })
          .then(function (result) {
            if (result.ok) {
              renderAnswer(thinking, result.data);
            } else {
              renderError(thinking, (result.data && result.data.detail) || 'Có lỗi xảy ra, thử lại sau.');
            }
          })
          .catch(function () {
            renderError(thinking, 'Không thể kết nối máy chủ. Kiểm tra mạng và thử lại.');
          })
          .then(function () {
            if (submitBtn) submitBtn.disabled = false;
            aiThread.scrollTop = aiThread.scrollHeight;
          });
      });
    }
  }

  // ----- shared modal plumbing: fetch a page, pull matching elements out of it, show them in a <dialog> -----
  function fetchAndShowModal(dialog, bodyEl, url, selectors, onInjected) {
    bodyEl.innerHTML = '<div class="doc-modal-loading"><span class="thinking-dot"></span><span class="thinking-dot"></span><span class="thinking-dot"></span></div>';
    dialog.showModal();
    fetch(url, { headers: { Accept: 'text/html' } })
      .then(function (r) { return r.text().then(function (html) { return { ok: r.ok, html: html }; }); })
      .then(function (result) {
        if (!dialog.open) return;
        if (!result.ok) { bodyEl.innerHTML = '<p class="notice">Không tải được nội dung.</p>'; return; }
        var parsed = new DOMParser().parseFromString(result.html, 'text/html');
        var nodes = selectors.map(function (sel) { return parsed.querySelector(sel); }).filter(Boolean);
        bodyEl.innerHTML = '';
        if (!nodes.length) { bodyEl.innerHTML = '<p class="notice">Không có nội dung để hiển thị.</p>'; return; }
        nodes.forEach(function (n) { bodyEl.appendChild(n); });
        if (onInjected) onInjected(bodyEl);
      })
      .catch(function () {
        if (dialog.open) bodyEl.innerHTML = '<p class="notice">Không thể kết nối máy chủ.</p>';
      });
  }
  function wireModalClose(dialog, closeSelector) {
    function close() { if (dialog.open) dialog.close(); }
    Array.prototype.forEach.call(dialog.querySelectorAll(closeSelector), function (btn) { btn.addEventListener('click', close); });
    dialog.addEventListener('click', function (e) { if (e.target === dialog) close(); });
    return close;
  }
  function wireCancelLink(bodyEl, closeFn) {
    var cancel = bodyEl.querySelector('.actions a.btn.secondary');
    if (cancel) cancel.addEventListener('click', function (ev) { ev.preventDefault(); closeFn(); });
  }
  var modalsSupported = window.fetch && window.DOMParser;

  // ----- friendlier metadata editor: key/value rows (with nested groups) instead of raw JSON.
  // Reused for both the upload form and the document-edit form (any textarea marked
  // data-kv-metadata), wherever it happens to live at the time it's attached. -----
  var KV_SUGGESTIONS = [
    { key: 'year', hint: 'Năm xuất bản / tạo tài liệu (số)' },
    { key: 'source', hint: 'Nguồn gốc tài liệu (hội nghị, tạp chí, nội bộ…)' },
    { key: 'doi', hint: 'Mã định danh DOI, nếu có' },
    { key: 'keywords', hint: 'Từ khóa nghiên cứu' },
    { key: 'language', hint: 'Ngôn ngữ tài liệu (vi, en, …)' },
    { key: 'publisher', hint: 'Đơn vị / nhà xuất bản' },
    { key: 'version', hint: 'Phiên bản tài liệu' },
    { key: 'note', hint: 'Ghi chú thêm' },
  ];
  var kvHints = {};
  KV_SUGGESTIONS.forEach(function (s) { kvHints[s.key] = s.hint; });

  function ensureKvDatalist() {
    if (document.getElementById('metadata-key-suggestions')) return;
    var datalist = document.createElement('datalist');
    datalist.id = 'metadata-key-suggestions';
    KV_SUGGESTIONS.forEach(function (s) {
      var opt = document.createElement('option');
      opt.value = s.key;
      opt.label = s.hint;
      datalist.appendChild(opt);
    });
    document.body.appendChild(datalist);
  }
  function coerceValue(str) {
    var t = str.trim();
    if (t === '') return '';
    if (t === 'true') return true;
    if (t === 'false') return false;
    if (t === 'null') return null;
    if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
    return str;
  }
  function valueToText(v) {
    if (v === null) return 'null';
    if (typeof v === 'boolean' || typeof v === 'number') return String(v);
    return v;
  }
  function objectToEntries(obj) {
    return Object.keys(obj).map(function (k) { return [k, obj[k]]; });
  }
  function makeKvRow(key, value) {
    var row = document.createElement('div');
    row.className = 'kv-row';
    var main = document.createElement('div');
    main.className = 'kv-row-main';
    var fields = document.createElement('div');
    fields.className = 'kv-fields';

    var keyInput = document.createElement('input');
    keyInput.type = 'text';
    keyInput.className = 'kv-key';
    keyInput.placeholder = 'Tên trường (vd: year)';
    keyInput.setAttribute('list', 'metadata-key-suggestions');
    keyInput.value = key || '';

    var hint = document.createElement('small');
    hint.className = 'kv-hint';
    hint.hidden = true;
    function syncHint() {
      var h = kvHints[keyInput.value.trim()];
      if (h) { hint.textContent = h; hint.hidden = false; } else { hint.hidden = true; }
    }
    keyInput.addEventListener('input', syncHint);

    fields.appendChild(keyInput);
    fields.appendChild(hint);

    var valueInput = null;
    var nestedGroup = null;
    var wasArray = Array.isArray(value);

    function useValueInput(text) {
      if (nestedGroup) { nestedGroup.remove(); nestedGroup = null; }
      valueInput = document.createElement('input');
      valueInput.type = 'text';
      valueInput.className = 'kv-value';
      valueInput.placeholder = 'Giá trị';
      valueInput.value = text || '';
      fields.appendChild(valueInput);
      toggleBtn.textContent = 'Nhóm';
      toggleBtn.title = 'Chuyển thành nhóm con';
    }
    function useNestedGroup(entries) {
      if (valueInput) { valueInput.remove(); valueInput = null; }
      nestedGroup = buildKvGroup(entries || []);
      fields.appendChild(nestedGroup);
      toggleBtn.textContent = 'Giá trị';
      toggleBtn.title = 'Chuyển thành giá trị đơn';
    }

    var actions = document.createElement('div');
    actions.className = 'kv-row-actions';
    var toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'kv-icon-btn kv-toggle';
    toggleBtn.addEventListener('click', function () {
      if (nestedGroup) useValueInput(''); else useNestedGroup([]);
    });
    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'kv-icon-btn danger';
    removeBtn.title = 'Xóa trường';
    removeBtn.textContent = '✕';
    removeBtn.addEventListener('click', function () { row.remove(); });
    actions.appendChild(toggleBtn);
    actions.appendChild(removeBtn);

    if (value !== null && typeof value === 'object' && !wasArray) {
      useNestedGroup(objectToEntries(value));
    } else if (wasArray) {
      useValueInput(value.join(', '));
    } else {
      useValueInput(valueToText(value));
    }
    syncHint();

    main.appendChild(fields);
    main.appendChild(actions);
    row.appendChild(main);

    row.kvGetEntry = function () {
      var k = keyInput.value.trim();
      if (!k) return null;
      if (nestedGroup) return [k, serializeKvGroup(nestedGroup)];
      var raw = valueInput ? valueInput.value : '';
      if (wasArray) {
        var parts = raw.split(',').map(function (s) { return s.trim(); }).filter(function (s) { return s !== ''; });
        return [k, parts];
      }
      return [k, coerceValue(raw)];
    };
    return row;
  }
  function buildKvGroup(entries) {
    var group = document.createElement('div');
    group.className = 'kv-group';
    entries.forEach(function (e) { group.appendChild(makeKvRow(e[0], e[1])); });
    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'kv-add-row-btn';
    addBtn.textContent = '+ Thêm trường';
    addBtn.addEventListener('click', function () { group.insertBefore(makeKvRow('', ''), addBtn); });
    group.appendChild(addBtn);
    return group;
  }
  function serializeKvGroup(group) {
    var result = {};
    Array.prototype.forEach.call(group.children, function (row) {
      if (!row.kvGetEntry) return;
      var entry = row.kvGetEntry();
      if (entry) result[entry[0]] = entry[1];
    });
    return result;
  }
  function attachMetadataEditor(metadataField) {
    var metadataLabel = metadataField.closest('label.field');
    if (!metadataLabel || metadataField.kvAttached) return;
    metadataField.kvAttached = true;
    ensureKvDatalist();

    function parseCurrentJSON() {
      var text = metadataField.value.trim() || '{}';
      try {
        var obj = JSON.parse(text);
        if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) return null;
        return obj;
      } catch (e) {
        return null;
      }
    }

    var wrap = document.createElement('div');
    wrap.className = 'kv-wrap';
    metadataLabel.parentNode.insertBefore(wrap, metadataLabel);
    wrap.appendChild(metadataLabel);

    var editorTitle = document.createElement('div');
    editorTitle.className = 'kv-editor-title';
    editorTitle.textContent = 'Metadata bổ sung';
    var editorRoot = document.createElement('div');
    editorRoot.className = 'kv-editor';
    var kvError = document.createElement('p');
    kvError.className = 'kv-error';
    kvError.hidden = true;
    var toggleModeBtn = document.createElement('button');
    toggleModeBtn.type = 'button';
    toggleModeBtn.className = 'kv-raw-toggle';

    wrap.insertBefore(editorTitle, metadataLabel);
    wrap.insertBefore(editorRoot, metadataLabel);
    wrap.appendChild(kvError);
    wrap.appendChild(toggleModeBtn);

    var rootGroup = null;
    var showingRaw = false;

    function rebuildVisual() {
      var obj = parseCurrentJSON() || {};
      if (rootGroup) rootGroup.remove();
      rootGroup = buildKvGroup(objectToEntries(obj));
      editorRoot.appendChild(rootGroup);
    }
    function setMode(raw) {
      if (raw) {
        if (rootGroup) metadataField.value = JSON.stringify(serializeKvGroup(rootGroup), null, 2);
        editorTitle.hidden = true;
        editorRoot.hidden = true;
        metadataLabel.hidden = false;
        toggleModeBtn.textContent = 'Quay lại dạng biểu mẫu';
      } else {
        var obj = parseCurrentJSON();
        if (obj === null) {
          kvError.hidden = false;
          kvError.textContent = 'JSON không hợp lệ — sửa lại nội dung rồi thử chuyển chế độ.';
          return;
        }
        kvError.hidden = true;
        rebuildVisual();
        editorTitle.hidden = false;
        editorRoot.hidden = false;
        metadataLabel.hidden = true;
        toggleModeBtn.textContent = 'Xem / sửa JSON thô';
      }
      showingRaw = raw;
    }
    toggleModeBtn.addEventListener('click', function () { setMode(!showingRaw); });

    // Whichever mode is on screen, make sure the real field reflects it before submit.
    var ownerForm = metadataField.form;
    if (ownerForm) ownerForm.addEventListener('submit', function () {
      if (!showingRaw && rootGroup) metadataField.value = JSON.stringify(serializeKvGroup(rootGroup));
    });

    setMode(false);
  }
  Array.prototype.forEach.call(document.querySelectorAll('textarea[data-kv-metadata]'), attachMetadataEditor);

  // ----- document quick-look modal: opens a compact <dialog> instead of navigating away -----
  var docModal = document.getElementById('doc-modal');
  if (docModal && modalsSupported && typeof docModal.showModal === 'function') {
    var docModalBody = document.getElementById('doc-modal-body');
    var docModalFullLink = document.getElementById('doc-modal-full-link');
    wireModalClose(docModal, '[data-doc-modal-close]');

    document.addEventListener('click', function (e) {
      var link = e.target.closest('a.source-main[href^="/ui/documents/"]');
      if (!link) return;
      e.preventDefault();
      var url = link.getAttribute('href');
      if (docModalFullLink) docModalFullLink.href = url;
      fetchAndShowModal(docModal, docModalBody, url, ['.doc-head', '#pane-info']);
    });
  }

  // ----- delete confirmation modal (documents AND projects): fetches the existing confirm page and shows it as a dialog -----
  var confirmModal = document.getElementById('confirm-modal');
  if (confirmModal && modalsSupported && typeof confirmModal.showModal === 'function') {
    var confirmModalBody = document.getElementById('confirm-modal-body');
    var closeConfirmModal = wireModalClose(confirmModal, '[data-confirm-modal-close]');

    document.addEventListener('click', function (e) {
      var link = e.target.closest('[data-confirm-delete]');
      if (!link) return;
      e.preventDefault();
      fetchAndShowModal(confirmModal, confirmModalBody, link.getAttribute('href'), ['.center-card'], function (body) {
        wireCancelLink(body, closeConfirmModal);
      });
    });
  }

  // ----- quick-edit modal (project name/description, document tags/authors/metadata) -----
  var quickEditModal = document.getElementById('quick-edit-modal');
  if (quickEditModal && modalsSupported && typeof quickEditModal.showModal === 'function') {
    var quickEditBody = document.getElementById('quick-edit-modal-body');
    var closeQuickEditModal = wireModalClose(quickEditModal, '[data-quick-edit-modal-close]');

    document.addEventListener('click', function (e) {
      var link = e.target.closest('[data-quick-edit]');
      if (!link) return;
      e.preventDefault();
      fetchAndShowModal(quickEditModal, quickEditBody, link.getAttribute('href'), ['.card.narrow'], function (body) {
        wireCancelLink(body, closeQuickEditModal);
        Array.prototype.forEach.call(body.querySelectorAll('textarea[data-kv-metadata]'), attachMetadataEditor);
      });
    });
  }

  // ----- upload options modal: moves the tags/authors/metadata fields out of the
  // cramped sidebar popover into a roomy dialog. The <details> stays in the DOM as
  // the no-JS fallback home for those fields; JS takes over as soon as it runs. -----
  var uploadModal = document.getElementById('upload-modal');
  var uploadMore = document.getElementById('upload-more');
  if (uploadModal && uploadMore && typeof uploadModal.showModal === 'function') {
    var uploadModalBody = document.getElementById('upload-modal-body');
    var advancedFields = document.getElementById('upload-advanced-fields');
    var uploadFormEl = document.getElementById('upload-form');

    if (advancedFields && uploadFormEl) {
      Array.prototype.forEach.call(advancedFields.querySelectorAll('input, textarea'), function (f) {
        f.setAttribute('form', uploadFormEl.id);
      });
      uploadModalBody.appendChild(advancedFields);
    }

    wireModalClose(uploadModal, '[data-upload-modal-close]');

    var uploadSummary = uploadMore.querySelector('summary');
    if (uploadSummary) {
      uploadSummary.addEventListener('click', function (e) {
        e.preventDefault();
        uploadModal.showModal();
      });
    }
  }

  // ----- copy JSON -----
  var copy = document.getElementById('copy-json');
  if (copy && navigator.clipboard) copy.addEventListener('click', function () {
    var text = document.getElementById(copy.getAttribute('data-target')).textContent;
    var span = copy.querySelector('span');
    navigator.clipboard.writeText(text).then(function () {
      span.textContent = 'Đã sao chép';
      setTimeout(function () { span.textContent = 'Sao chép'; }, 1600);
    });
  });
  else if (copy) copy.hidden = true;
}());
