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
  // Delegated, and re-synced by initInspectorToggle(): the button is replaced
  // whenever a document opens in the main panel (see openInMain).
  if (stored('rdw.inspector') === 'hidden') shell.classList.add('inspector-hidden');
  function initInspectorToggle() {
    var btn = document.getElementById('toggle-inspector');
    if (btn) btn.setAttribute('aria-expanded', String(!shell.classList.contains('inspector-hidden')));
  }
  initInspectorToggle();
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('#toggle-inspector');
    if (!btn) return;
    var hidden = shell.classList.toggle('inspector-hidden');
    btn.setAttribute('aria-expanded', String(!hidden));
    stored('rdw.inspector', hidden ? 'hidden' : 'shown');
  });

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
    // Per-extension limits from app/file_types.py, e.g. {".mp4": 524288000}.
    var limits = {};
    try { limits = JSON.parse(uploadInput.getAttribute('data-limits') || '{}'); } catch (e) { /* server still checks */ }
    var extensionOf = function (name) {
      var dot = name.lastIndexOf('.');
      return dot > 0 ? name.slice(dot).toLowerCase() : '';
    };
    // null = allowed; otherwise why not.
    var problemWith = function (file) {
      var limit = limits[extensionOf(file.name)];
      if (!limit) return 'loại tệp không được hỗ trợ';
      if (file.size > limit) return 'vượt giới hạn ' + Math.round(limit / 1048576) + ' MB cho loại tệp này';
      if (file.size === 0) return 'tệp rỗng';
      return null;
    };
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
      var problem = file ? problemWith(file) : null;
      if (fileCard) {
        fileCard.hidden = !file;
        fileCard.classList.toggle('too-big', !!problem);
      }
      if (file && fileName) {
        fileName.textContent = file.name;
        fileName.title = file.name;
        fileMeta.textContent = formatSize(file.size) + (problem ? ' — ' + problem + ', chọn tệp khác' : ' — sẵn sàng tải lên');
      }
      if (pickLabel) pickLabel.textContent = file ? 'Đổi tệp' : 'Chọn tệp';
      if (uploadGo) uploadGo.disabled = !file || !!problem;
    };
    describeFile(uploadInput.files && uploadInput.files[0]);
    uploadInput.addEventListener('change', function () { describeFile(uploadInput.files && uploadInput.files[0]); });
    if (fileClear) fileClear.addEventListener('click', function () {
      uploadInput.value = '';
      describeFile(null);
    });

    // Upload with a progress bar: a large video takes minutes, and a plain form
    // post gives no sign of life. Without XHR/FormData the form posts normally.
    var progress = document.getElementById('upload-progress');
    var uploading = false;
    if (uploadForm && window.XMLHttpRequest && window.FormData && progress) {
      var progressBar = progress.firstElementChild;
      var finishWithError = function (message) {
        uploading = false;
        progress.hidden = true;
        fileCard.classList.add('too-big');
        fileMeta.textContent = message;
        if (uploadGo) uploadGo.disabled = false;
        if (fileClear) fileClear.disabled = false;
      };
      uploadForm.addEventListener('submit', function (e) {
        var file = uploadInput.files && uploadInput.files[0];
        if (!file || uploading) return;
        e.preventDefault();
        // The metadata editor only writes its rows back to the textarea in its
        // own submit handler, which may run after this one — do it now.
        Array.prototype.forEach.call(uploadForm.elements, function (el) {
          if (el.kvValue) el.value = el.kvValue();
        });
        var xhr = new XMLHttpRequest();
        xhr.open('POST', uploadForm.action);
        xhr.upload.addEventListener('progress', function (ev) {
          if (!ev.lengthComputable) return;
          var pct = Math.floor(ev.loaded / ev.total * 100);
          progressBar.style.width = pct + '%';
          fileMeta.textContent = pct < 100
            ? 'Đang tải lên ' + pct + '% (' + formatSize(ev.loaded) + ' / ' + formatSize(ev.total) + ')'
            : 'Đang lưu vào kho…';
        });
        xhr.addEventListener('load', function () {
          if (xhr.status < 400) {
            uploading = false;
            // The server redirected to the new document's page; go there.
            window.location.href = xhr.responseURL || window.location.href;
            return;
          }
          var page = new DOMParser().parseFromString(xhr.responseText, 'text/html');
          var message = page.querySelector('.center-card h1 + p');
          finishWithError('Tải lên thất bại: ' + (message ? message.textContent : 'lỗi ' + xhr.status));
        });
        xhr.addEventListener('error', function () { finishWithError('Mất kết nối khi tải lên, thử lại.'); });
        uploading = true;
        fileCard.classList.remove('too-big');
        progressBar.style.width = '0';
        progress.hidden = false;
        fileMeta.textContent = 'Đang tải lên 0%';
        if (uploadGo) uploadGo.disabled = true;
        if (fileClear) fileClear.disabled = true;
        xhr.send(new FormData(uploadForm));
      });
      window.addEventListener('beforeunload', function (e) {
        if (!uploading) return;
        e.preventDefault();
        e.returnValue = '';  // browser shows its own "leave page?" prompt
      });
    }

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
        if (problemWith(file)) return;  // the card already explains why
        if (uploadForm.requestSubmit) uploadForm.requestSubmit(); else uploadForm.submit();
      });
    }
  }

  // ----- document tabs (all panes stay visible without JS) -----
  // A function, not run-once: a document opened in the main panel brings new tabs.
  function initDocTabs(root) {
    var tabs = root.querySelector('#doc-tabs');
    if (!tabs) return;
    var buttons = Array.prototype.slice.call(tabs.querySelectorAll('[role="tab"]'));
    // The open tab lives in the URL hash (#info / #extract / #json — "tab-" id
    // without the prefix, so no element matches and the page doesn't jump), so a
    // reload or a server redirect lands back on the same tab.
    var tabName = function (btn) { return btn.id.replace(/^tab-/, ''); };
    var select = function (btn, remember) {
      buttons.forEach(function (b) {
        var on = b === btn;
        b.setAttribute('aria-selected', String(on));
        b.tabIndex = on ? 0 : -1;
        document.getElementById(b.getAttribute('aria-controls')).hidden = !on;
      });
      if (remember && window.history && history.replaceState) history.replaceState(history.state, '', '#' + tabName(btn));
    };
    tabs.hidden = false;
    buttons.forEach(function (b, i) {
      b.addEventListener('click', function () { select(b, true); });
      b.addEventListener('keydown', function (e) {
        var next = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : null;
        if (next === null) return;
        var target = buttons[(next + buttons.length) % buttons.length];
        select(target, true); target.focus();
      });
    });
    var fromHash = buttons.filter(function (b) { return '#' + tabName(b) === location.hash; })[0];
    select(fromHash || buttons[0], false);
  }
  initDocTabs(document);

  // "Trích xuất văn bản": run it in place and swap in the refreshed tab
  // content, instead of a full page load. Without fetch, the form posts
  // normally and the redirect's #extract hash still reopens this tab.
  if (window.fetch && window.DOMParser) {
    document.addEventListener('submit', function (e) {
      var form = e.target.closest('form[data-inline-extract]');
      if (!form) return;
      e.preventDefault();
      var button = form.querySelector('button');
      var label = button.querySelector('span');
      var pane = form.closest('[role="tabpanel"]');
      var errorBox = pane.querySelector('.extract-error');
      button.disabled = true;
      label.textContent = form.getAttribute('data-busy-label') || 'Đang trích xuất…';
      if (errorBox) errorBox.hidden = true;
      fetch(form.action, { method: 'POST', headers: { Accept: 'text/html' } })
        .then(function (r) { return r.text().then(function (html) { return { ok: r.ok, html: html }; }); })
        .then(function (result) {
          var page = new DOMParser().parseFromString(result.html, 'text/html');
          if (!result.ok) {
            // Error page: its message is the paragraph right under the heading.
            var message = page.querySelector('.center-card h1 + p');
            throw new Error(message ? message.textContent : 'Trích xuất thất bại, thử lại sau.');
          }
          var freshPane = page.getElementById(pane.id);
          if (freshPane) pane.innerHTML = freshPane.innerHTML;
          // The MongoDB JSON tab shows the same record — keep it in sync too.
          var freshJson = page.getElementById('json-source');
          var json = document.getElementById('json-source');
          if (freshJson && json) json.textContent = freshJson.textContent;
        })
        .catch(function (err) {
          button.disabled = false;
          label.textContent = 'Thử lại';
          if (errorBox) {
            // TypeError = fetch itself failed (network), its message is browser English.
            errorBox.textContent = (err instanceof TypeError || !err.message) ? 'Không kết nối được máy chủ, thử lại sau.' : err.message;
            errorBox.hidden = false;
          }
        });
    });
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
      if (data.unread && data.unread.length) {
        var unreadP = document.createElement('p');
        unreadP.className = 'chat-unread small-text';
        unreadP.textContent = 'AI chưa đọc được ' + data.unread.length + ' tệp đã chọn: ' + data.unread.join(', ') +
          '. Mở tệp, tab "Văn bản trích xuất" để trích xuất hoặc phân tích bằng AI.';
        body.appendChild(unreadP);
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

    // For the draft saver: the value as currently on screen (the rows are only
    // written back to the textarea on submit), and a way to redraw the rows
    // after the textarea is changed from outside (draft restored / discarded).
    metadataField.kvValue = function () {
      return (!showingRaw && rootGroup) ? JSON.stringify(serializeKvGroup(rootGroup)) : metadataField.value;
    };
    metadataField.kvReload = function () { if (!showingRaw) rebuildVisual(); };

    // Whichever mode is on screen, make sure the real field reflects it before submit.
    var ownerForm = metadataField.form;
    if (ownerForm) ownerForm.addEventListener('submit', function () {
      if (!showingRaw && rootGroup) metadataField.value = JSON.stringify(serializeKvGroup(rootGroup));
    });

    setMode(false);
  }
  // ----- unsaved-input drafts: forms marked data-draft-key keep what was typed in
  // localStorage, so closing a dialog by accident (or reloading) loses nothing.
  // Restored on the next open with a "Bỏ nháp" way out; cleared on submit. -----
  var DRAFT_PREFIX = 'rdw-draft:';
  var DRAFT_MAX_AGE_MS = 14 * 24 * 3600 * 1000;
  function storageGet(key) { try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; } }
  function storageSet(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode / full */ } }
  function storageRemove(key) { try { localStorage.removeItem(key); } catch (e) { /* ignore */ } }
  function formatTime(ms) {
    var d = new Date(ms);
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ' ' + pad(d.getDate()) + '/' + pad(d.getMonth() + 1);
  }
  // JSON fields compare by meaning, not formatting (server pretty-prints, the editor doesn't).
  function canonical(value) {
    try { return JSON.stringify(JSON.parse(value)); } catch (e) { return value; }
  }

  function setupDraft(form) {
    if (form.draftAttached) return;
    form.draftAttached = true;
    var key = DRAFT_PREFIX + form.getAttribute('data-draft-key');
    var extraScope = form.getAttribute('data-draft-scope') && document.getElementById(form.getAttribute('data-draft-scope'));
    var fields = Array.prototype.filter.call(form.elements, function (el) {
      return el.name && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && /^(text|search|url|email|number)$/.test(el.type)));
    });
    if (!fields.length) return;
    var isJson = function (el) { return el.hasAttribute('data-kv-metadata'); };
    var read = function (el) { return el.kvValue ? el.kvValue() : el.value; };
    var comparable = function (el, value) { return isJson(el) ? canonical(value) : value; };
    var baseline = {};
    fields.forEach(function (el) { baseline[el.name] = el.value; });

    var note = document.createElement('div');
    note.className = 'draft-note';
    note.hidden = true;
    var noteText = document.createElement('span');
    var discard = document.createElement('button');
    discard.type = 'button';
    discard.className = 'draft-discard';
    discard.textContent = 'Bỏ nháp';
    note.appendChild(noteText);
    note.appendChild(discard);
    var host = extraScope || form;
    host.insertBefore(note, host.firstChild);

    function applyValues(values) {
      fields.forEach(function (el) {
        if (Object.prototype.hasOwnProperty.call(values, el.name)) el.value = values[el.name];
        if (el.kvReload) el.kvReload();
      });
    }
    // `has-draft` on the form lets CSS flag it where the fields are out of sight
    // (upload: the "Tùy chọn" button), since the draft goes out with the next upload.
    function setNote(text) {
      if (text) noteText.textContent = text;
      note.hidden = !text;
      form.classList.toggle('has-draft', !!text);
    }

    function save() {
      var values = {};
      var changed = false;
      fields.forEach(function (el) {
        values[el.name] = read(el);
        if (comparable(el, values[el.name]) !== comparable(el, baseline[el.name])) changed = true;
      });
      if (!changed) { storageRemove(key); setNote(null); return; }
      var savedAt = Date.now();
      storageSet(key, { values: values, savedAt: savedAt });
      setNote('Đã lưu nháp tự động lúc ' + formatTime(savedAt) + '.');
    }
    var timer = null;
    function scheduleSave() { clearTimeout(timer); timer = setTimeout(save, 300); }
    [form, extraScope].forEach(function (scope) {
      if (!scope) return;
      scope.addEventListener('input', scheduleSave);
      scope.addEventListener('change', scheduleSave);
      // Adding/removing metadata rows is a click, not an input event.
      scope.addEventListener('click', function (e) { if (e.target.closest('.kv-wrap')) scheduleSave(); });
    });

    discard.addEventListener('click', function () {
      storageRemove(key);
      applyValues(baseline);
      setNote(null);
    });
    // Edit forms keep the draft through submit: if the server rejects it (422),
    // nothing is lost, and once it IS saved the next open finds draft == stored
    // values and drops it (below). An upload form always starts empty, so it
    // can't tell — it opts in to clearing on submit instead.
    var clearOnSubmit = form.hasAttribute('data-draft-clear-on-submit');
    form.addEventListener('submit', function () {
      clearTimeout(timer);
      if (clearOnSubmit) storageRemove(key); else save();
    });

    var draft = storageGet(key);
    if (!draft || !draft.values || Date.now() - draft.savedAt > DRAFT_MAX_AGE_MS) { storageRemove(key); return; }
    var differs = fields.some(function (el) {
      return el.name in draft.values && comparable(el, draft.values[el.name]) !== comparable(el, baseline[el.name]);
    });
    if (!differs) { storageRemove(key); return; }  // e.g. the draft was saved after all
    applyValues(draft.values);
    setNote('Đã khôi phục bản nháp chưa lưu (' + formatTime(draft.savedAt) + ').');
  }

  // Drafts first: a restored metadata value must be in the textarea before the
  // key/value editor builds its rows from it.
  Array.prototype.forEach.call(document.querySelectorAll('form[data-draft-key]'), setupDraft);
  Array.prototype.forEach.call(document.querySelectorAll('textarea[data-kv-metadata]'), attachMetadataEditor);

  // ----- open documents in the main panel (NotebookLM-style) -----
  // A document picked in the sources sidebar replaces only the middle of the
  // page: the sidebar (search, scroll position, AI checkboxes) and the AI chat
  // stay put. The URL still changes (pushState), so Back/Forward, reload and
  // "open in new tab" all work; without JS the links are ordinary pages.
  var mainEl = document.getElementById('main');
  var sourceFilter = document.getElementById('source-filter');
  var canNavigateInPlace = !!(mainEl && document.getElementById('sources-panel') && window.fetch && window.DOMParser && window.history && history.pushState);

  function markActiveSource(documentId) {
    Array.prototype.forEach.call(document.querySelectorAll('#sources-panel .source-item'), function (item) {
      item.classList.toggle('active', !!documentId && item.getAttribute('data-document-id') === documentId);
    });
  }

  function openInMain(url, push) {
    mainEl.classList.add('loading');
    return fetch(url, { headers: { Accept: 'text/html' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (html) {
        var page = new DOMParser().parseFromString(html, 'text/html');
        var freshMain = page.getElementById('main');
        if (!freshMain) throw new Error('unexpected page');
        document.title = page.title;
        mainEl.innerHTML = freshMain.innerHTML;
        ['.crumbs', '#page-actions'].forEach(function (sel) {
          var fresh = page.querySelector(sel);
          var current = document.querySelector(sel);
          if (fresh && current) current.innerHTML = fresh.innerHTML;
        });
        // The storage inspector only exists on document pages.
        var body = mainEl.parentNode;
        var oldInspector = body.querySelector('.inspector');
        if (oldInspector) oldInspector.remove();
        var freshInspector = page.querySelector('.body > .inspector');
        if (freshInspector) body.appendChild(document.importNode(freshInspector, true));
        var freshActive = page.querySelector('#sources-panel .source-item.active');
        markActiveSource(freshActive ? freshActive.getAttribute('data-document-id') : null);
        if (push) history.pushState({ rdwMain: true }, '', url);
        mainEl.scrollTop = 0;
        initDocTabs(mainEl);
        initInspectorToggle();
      })
      .catch(function () {
        // Anything unexpected (deleted document, server error, logged out):
        // fall back to a normal page load, which shows the proper page.
        window.location.href = url;
      })
      .then(function () { mainEl.classList.remove('loading'); });
  }

  if (canNavigateInPlace) {
    history.replaceState({ rdwMain: true }, '', location.href);
    document.addEventListener('click', function (e) {
      var link = e.target.closest('a[data-open-in-main]');
      if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      shell.classList.remove('sidebar-open');
      openInMain(link.href, true);
    });
    window.addEventListener('popstate', function (e) {
      if (e.state && e.state.rdwMain) openInMain(location.href, false);
    });
  }

  // ----- sources sidebar: search + kind filter, applied as you type -----
  // Server-side (it must cover every page, not just the 20 on screen); the
  // results block is swapped in place and the URL keeps ?q=&kind= so a reload
  // or a document link keeps the same list. Without JS the form just submits.
  if (sourceFilter && window.fetch && window.DOMParser) {
    var results = document.getElementById('source-results');
    var searchInput = sourceFilter.querySelector('input[name="q"]');
    var kindSelect = sourceFilter.querySelector('select[name="kind"]');
    var listRequest = 0;

    var refreshSources = function (offset) {
      var params = new URLSearchParams();
      if (searchInput.value.trim()) params.set('q', searchInput.value.trim());
      if (kindSelect.value) params.set('kind', kindSelect.value);
      if (offset && offset !== '0') params.set('offset', offset);
      var qs = params.toString();
      // Unchecked "use with AI" boxes survive the list being re-rendered.
      var unchecked = Array.prototype.map.call(results.querySelectorAll('.source-check:not(:checked)'), function (c) { return c.value; });
      var activeItem = results.querySelector('.source-item.active');
      var activeId = activeItem && activeItem.getAttribute('data-document-id');
      var mine = ++listRequest;
      results.classList.add('loading');
      // The project page renders the same list and is cheaper than a document page.
      fetch(sourceFilter.getAttribute('data-list-url') + (qs ? '?' + qs : ''), { headers: { Accept: 'text/html' } })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
        .then(function (html) {
          if (mine !== listRequest) return;  // a newer keystroke won
          var fresh = new DOMParser().parseFromString(html, 'text/html').getElementById('source-results');
          if (!fresh) return;
          results.innerHTML = fresh.innerHTML;
          Array.prototype.forEach.call(results.querySelectorAll('.source-check'), function (c) {
            if (unchecked.indexOf(c.value) !== -1) c.checked = false;
          });
          markActiveSource(activeId);
          history.replaceState(history.state, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
          Array.prototype.forEach.call(document.querySelectorAll('[data-kind-filter]'), function (card) {
            card.classList.toggle('selected', card.getAttribute('data-kind-filter') === kindSelect.value);
          });
        })
        .catch(function () { if (mine === listRequest) sourceFilter.submit(); })
        .then(function () { if (mine === listRequest) results.classList.remove('loading'); });
    };

    var typingTimer = null;
    searchInput.addEventListener('input', function () {
      clearTimeout(typingTimer);
      typingTimer = setTimeout(function () { refreshSources(0); }, 250);
    });
    kindSelect.addEventListener('change', function () { refreshSources(0); });
    sourceFilter.addEventListener('submit', function (e) { e.preventDefault(); clearTimeout(typingTimer); refreshSources(0); });
    document.addEventListener('click', function (e) {
      var page = e.target.closest('a[data-source-page]');
      var clear = e.target.closest('a[data-source-clear]');
      var kindCard = e.target.closest('a[data-kind-filter]');
      if (!page && !clear && !kindCard) return;
      e.preventDefault();
      if (page) {
        refreshSources(new URL(page.href).searchParams.get('offset') || 0);
      } else if (clear) {
        searchInput.value = '';
        kindSelect.value = '';
        refreshSources(0);
      } else {
        var kind = kindCard.getAttribute('data-kind-filter');
        kindSelect.value = kindSelect.value === kind ? '' : kind;  // click again to un-filter
        refreshSources(0);
      }
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
        Array.prototype.forEach.call(body.querySelectorAll('form[data-draft-key]'), setupDraft);
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

  // ----- copy JSON (delegated: the button arrives with each opened document) -----
  document.addEventListener('click', function (e) {
    var copy = e.target.closest('#copy-json');
    if (!copy) return;
    var span = copy.querySelector('span');
    var text = document.getElementById(copy.getAttribute('data-target')).textContent;
    if (!navigator.clipboard) { span.textContent = 'Trình duyệt chặn sao chép'; return; }
    navigator.clipboard.writeText(text).then(function () {
      span.textContent = 'Đã sao chép';
      setTimeout(function () { span.textContent = 'Sao chép'; }, 1600);
    });
  });
}());
