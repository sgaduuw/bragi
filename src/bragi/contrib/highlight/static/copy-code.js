// Copy-to-clipboard for highlighted code blocks. Progressive
// enhancement: the button is inert without JS, the code still renders.
// Delegated so it covers blocks added after load.
(function () {
  function copy(btn) {
    var block = btn.closest('.code-block');
    if (!block) return;
    // Prefer the code cell (linenos table layout) so line numbers are
    // not copied; fall back to the plain <pre>.
    var pre = block.querySelector('td.code pre') || block.querySelector('.highlight pre');
    var text = (pre ? pre.innerText : block.innerText).replace(/\n+$/, '');
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(function () {
      var original = btn.textContent;
      btn.textContent = 'Copied';
      btn.classList.add('copied');
      setTimeout(function () {
        btn.textContent = original;
        btn.classList.remove('copied');
      }, 1500);
    }).catch(function () {});
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.code-copy');
    if (btn) copy(btn);
  });
})();
