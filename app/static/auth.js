// Account pages: show/hide password. Without JS the eye buttons stay hidden
// (CSS shows them only under html.js) and the fields work as normal.
(function () {
  'use strict';
  document.documentElement.classList.add('js');
  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-reveal]');
    if (!button) return;
    var input = button.parentNode.querySelector('input');
    var show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.classList.toggle('on', show);
    var label = show ? 'Ẩn mật khẩu' : 'Hiện mật khẩu';
    button.setAttribute('aria-label', label);
    button.title = label;
    input.focus();
  });
}());
