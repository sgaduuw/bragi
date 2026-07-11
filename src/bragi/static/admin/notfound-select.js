// 404-list bulk selection: a select-all checkbox, a live selected count,
// and bulk-action buttons that gather the checked ids into the hidden
// bulk form and submit it to the button's declared action.
(function () {
  function refreshCount() {
    var n = document.querySelectorAll('.nf-check:checked').length;
    document.querySelectorAll('.nf-selected-count').forEach(function (el) { el.textContent = n; });
  }
  document.addEventListener('change', function (e) {
    var t = e.target;
    if (t.classList && t.classList.contains('nf-select-all')) {
      var table = t.closest('table');
      if (table) {
        table.querySelectorAll('.nf-check').forEach(function (c) { c.checked = t.checked; });
      }
    }
    if (t.classList && (t.classList.contains('nf-check') || t.classList.contains('nf-select-all'))) {
      refreshCount();
    }
  });
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.nf-bulk');
    if (!btn) return;
    var checked = Array.prototype.slice.call(document.querySelectorAll('.nf-check:checked'));
    if (!checked.length) { window.alert('Select at least one 404 first.'); return; }
    var msg = btn.getAttribute('data-nf-confirm');
    if (msg && !window.confirm(msg)) return;
    var form = document.querySelector('.nf-bulk-form');
    if (!form) return;
    form.querySelectorAll('input[name="ids"]').forEach(function (i) { i.remove(); });
    checked.forEach(function (c) {
      var i = document.createElement('input');
      i.type = 'hidden'; i.name = 'ids'; i.value = c.value;
      form.appendChild(i);
    });
    form.action = btn.getAttribute('data-nf-action');
    form.submit();
  });
})();
