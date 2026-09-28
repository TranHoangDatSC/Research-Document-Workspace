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

  // ----- upload dropzone -----
  var input = document.getElementById('file-input');
  var zone = document.getElementById('dropzone');
  if (input && zone) {
    var label = document.getElementById('file-label');
    var hint = document.getElementById('file-hint');
    var original = { label: label.textContent, hint: hint.textContent };
    var MAX = 10 * 1024 * 1024;
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file) { label.textContent = original.label; hint.textContent = original.hint; return; }
      label.textContent = file.name;
      var mib = (file.size / 1048576).toFixed(2);
      hint.textContent = file.size > MAX ? mib + ' MiB — vượt giới hạn 10 MiB, máy chủ sẽ từ chối' : mib + ' MiB';
    });
    ['dragenter', 'dragover'].forEach(function (t) { zone.addEventListener(t, function () { zone.classList.add('drag'); }); });
    ['dragleave', 'drop'].forEach(function (t) { zone.addEventListener(t, function () { zone.classList.remove('drag'); }); });
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
