// Slug live-suggest: as the operator types the title, write a slugified
// version into the slug field, until they touch it. The server-side
// handler also fills an empty slug at POST time; this is the live-UX
// layer. Safe on edit forms too: a pre-filled slug marks `touched`, so an
// existing slug is never overwritten.
(function () {
  const titleEl = document.getElementById('title');
  const slugEl = document.getElementById('slug');
  if (!titleEl || !slugEl) return;
  let touched = slugEl.value.length > 0;
  slugEl.addEventListener('input', function () { touched = true; });
  titleEl.addEventListener('input', function () {
    if (touched) return;
    slugEl.value = slugify(titleEl.value);
  });
  function slugify(text) {
    return text
      .normalize('NFKD')
      .replace(/\p{M}/gu, '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }
})();
