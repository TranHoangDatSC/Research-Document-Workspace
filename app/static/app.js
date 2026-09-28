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

  // ----- project filter -----
  var filter = document.getElementById('project-filter');
  if (filter) filter.addEventListener('input', function () {
    var q = filter.value.trim().toLowerCase();
    Array.prototype.forEach.call(document.querySelectorAll('#project-tree .tree-item'), function (a) {
      a.hidden = q !== '' && a.textContent.toLowerCase().indexOf(q) === -1;
    });
  });

  // ----- source upload: label-triggered file input + drag-and-drop onto the whole sources panel -----
  var uploadInput = document.getElementById('file-input');
  var uploadForm = uploadInput && uploadInput.closest('form');
  var sourcesPanel = document.getElementById('sources-panel');
  var fileHint = document.getElementById('file-hint');
  if (uploadInput) {
    var originalHint = fileHint ? fileHint.textContent : '';
    var MAX = 10 * 1024 * 1024;
    var describeFile = function (file) {
      if (!fileHint) return;
      if (!file) { fileHint.textContent = originalHint; return; }
      var mib = (file.size / 1048576).toFixed(2);
      fileHint.textContent = file.name + ' · ' + (file.size > MAX ? mib + ' MiB — vượt giới hạn 10 MiB' : mib + ' MiB');
    };
    uploadInput.addEventListener('change', function () { describeFile(uploadInput.files && uploadInput.files[0]); });

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
      var html = '<p></p>';
      body.innerHTML = html;
      body.querySelector('p').textContent = data.answer;
      if (data.sources && data.sources.length) {
        var sources = document.createElement('div');
        sources.className = 'chat-sources';
        data.sources.forEach(function (s) {
          var chip = document.createElement('span');
          chip.className = 'chip';
          chip.textContent = s.original_name + ' · đoạn ' + (s.chunk_index + 1);
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

  // ----- document quick-look modal: opens a compact <dialog> instead of navigating away -----
  var docModal = document.getElementById('doc-modal');
  if (docModal && window.fetch && window.DOMParser && typeof docModal.showModal === 'function') {
    var docModalBody = document.getElementById('doc-modal-body');
    var docModalFullLink = document.getElementById('doc-modal-full-link');

    function closeDocModal() { if (docModal.open) docModal.close(); }
    Array.prototype.forEach.call(docModal.querySelectorAll('[data-doc-modal-close]'), function (btn) {
      btn.addEventListener('click', closeDocModal);
    });
    docModal.addEventListener('click', function (e) { if (e.target === docModal) closeDocModal(); });

    function openDocModal(url) {
      if (docModalFullLink) docModalFullLink.href = url;
      docModalBody.innerHTML = '<div class="doc-modal-loading"><span class="thinking-dot"></span><span class="thinking-dot"></span><span class="thinking-dot"></span></div>';
      docModal.showModal();
      fetch(url, { headers: { Accept: 'text/html' } })
        .then(function (r) { return r.text().then(function (html) { return { ok: r.ok, html: html }; }); })
        .then(function (result) {
          if (!docModal.open) return;
          if (!result.ok) { docModalBody.innerHTML = '<p class="notice">Không tải được tài liệu.</p>'; return; }
          var parsed = new DOMParser().parseFromString(result.html, 'text/html');
          var head = parsed.querySelector('.doc-head');
          var info = parsed.querySelector('#pane-info');
          docModalBody.innerHTML = '';
          if (head) docModalBody.appendChild(head);
          if (info) docModalBody.appendChild(info);
          if (!head && !info) docModalBody.innerHTML = '<p class="notice">Không có thông tin để hiển thị.</p>';
        })
        .catch(function () {
          if (docModal.open) docModalBody.innerHTML = '<p class="notice">Không thể kết nối máy chủ.</p>';
        });
    }

    document.addEventListener('click', function (e) {
      var link = e.target.closest('a.source-main[href^="/ui/documents/"]');
      if (!link) return;
      e.preventDefault();
      openDocModal(link.getAttribute('href'));
    });
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
