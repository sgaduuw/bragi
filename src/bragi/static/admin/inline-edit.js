// Admin inline-edit support. Autofocus an [autofocus] element
// inside any htmx-swapped content (the HTML `autofocus`
// attribute only fires on initial page load, not on dynamic
// insertion). Select the text on focus so the operator can
// start typing immediately to replace.
document.body.addEventListener('htmx:afterSwap', function (evt) {
  var el = evt.detail.target.querySelector('[autofocus]');
  if (el) {
    el.focus();
    if (el.select) el.select();
  }
});
