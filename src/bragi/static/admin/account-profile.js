// Account profile form enhancements (JS-required admin).
//
// 1. Repeatable rel="me" link rows: "Add link" clones the last row blank;
//    "Remove" deletes its row, keeping at least one. Blank rows are
//    dropped server-side.
(function () {
  'use strict';
  var tbody = document.querySelector('#profile-links-rows tbody');
  var addBtn = document.getElementById('profile-links-add');
  if (!tbody || !addBtn) return;

  function blankRow() {
    var rows = tbody.querySelectorAll('.profile-links-edit__row');
    var clone = rows[rows.length - 1].cloneNode(true);
    clone.querySelectorAll('input').forEach(function (i) {
      i.value = '';
      i.removeAttribute('aria-invalid');
    });
    var err = clone.querySelector('.inline-edit-error');
    if (err) err.remove();
    return clone;
  }

  addBtn.addEventListener('click', function () {
    tbody.appendChild(blankRow());
  });

  tbody.addEventListener('click', function (ev) {
    if (!ev.target.classList.contains('profile-links-edit__remove')) return;
    var rows = tbody.querySelectorAll('.profile-links-edit__row');
    if (rows.length <= 1) {
      rows[0].querySelectorAll('input').forEach(function (i) { i.value = ''; });
    } else {
      ev.target.closest('.profile-links-edit__row').remove();
    }
  });
})();

// 2. Avatar: keep the preview in sync with the URL field, and let the
//    "Use Gravatar" / "Use GitHub avatar" buttons fill it in one click.
//    The candidate URLs come from data-attributes on the input.
(function () {
  'use strict';
  var input = document.getElementById('avatar_url');
  var preview = document.getElementById('avatar-preview');
  if (!input || !preview) return;

  function sync() {
    if (input.value) { preview.src = input.value; preview.hidden = false; }
    else { preview.hidden = true; }
  }
  function useSource(url) {
    if (!url) return;
    input.value = url;
    sync();
  }

  input.addEventListener('input', sync);
  var grav = document.getElementById('use-gravatar');
  if (grav) grav.addEventListener('click', function () { useSource(input.dataset.gravatar); });
  var gh = document.getElementById('use-github');
  if (gh) gh.addEventListener('click', function () { useSource(input.dataset.github); });
})();
