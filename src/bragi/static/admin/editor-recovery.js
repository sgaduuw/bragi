/* Local recovery is not a save. Only a server receipt clears submitted work. */
(() => {
  if (window.bragiEditorRecovery) return;
  window.bragiEditorRecovery = true;
  const prefix = 'bragi:editor-recovery:';
  const retention = 7 * 24 * 60 * 60 * 1000;
  let active;
  const value = el => el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value;
  const assign = (el, v) => {
    if (el.type === 'checkbox' || el.type === 'radio') el.checked = !!v;
    else el.value = v;
  };
  const controls = form => [...form.elements].filter(el => el.name &&
    !el.name.startsWith('_') && !el.name.startsWith('acknowledge_') &&
    !['submit', 'button', 'file', 'password'].includes(el.type) &&
    (el.type !== 'hidden' || el.name === 'featured_image_id'));
  function snapshot(form) {
    const fields = controls(form).map(el => [el.name, value(el)]);
    const resume = form.querySelector('#resume-fieldset');
    return { fields, resume: resume ? {
      single: [...resume.querySelectorAll('input[id], textarea[id]')]
        .filter(el => !el.closest('.repeating-field') && el.type !== 'hidden')
        .map(el => [el.id, value(el)]),
      groups: [...resume.querySelectorAll('.repeating-field')].map(group => [
        group.dataset.fieldName,
        [...group.querySelectorAll('.repeating-field__rows > .repeating-field__row')].map(row => ({
          id: row.dataset.id,
          fields: [...row.querySelectorAll('[data-field]')].map(el => [el.dataset.field, value(el)])
        }))
      ])
    } : null };
  }
  function restore(form, data) {
    const elements = controls(form);
    data.fields.forEach(([name, v]) => {
      const index = elements.findIndex(el => el.name === name);
      if (index !== -1) assign(elements.splice(index, 1)[0], v);
    });
    const resume = form.querySelector('#resume-fieldset');
    if (resume && data.resume) {
      data.resume.single.forEach(([id, v]) => {
        const el = document.getElementById(id);
        if (el && resume.contains(el)) assign(el, v);
      });
      data.resume.groups.forEach(([name, rows]) => {
        const group = [...resume.querySelectorAll('.repeating-field')].find(el => el.dataset.fieldName === name);
        if (!group) return;
        const target = group.querySelector('.repeating-field__rows');
        target.querySelectorAll('input').forEach(el => el._flatpickr?.destroy());
        target.replaceChildren();
        rows.forEach(row => {
          const clone = group.querySelector('template').content.firstElementChild.cloneNode(true);
          clone.dataset.id = row.id;
          row.fields.forEach(([field, v]) => {
            const el = [...clone.querySelectorAll('[data-field]')].find(el => el.dataset.field === field);
            if (el) {
              // Linked-position choices are populated by the resume editor next.
              if (el.matches('select.js-linked-position')) el.dataset.current = v;
              assign(el, v);
            }
          });
          target.append(clone);
        });
      });
      resume.dispatchEvent(new Event('editor:restore'));
    }
    form.querySelector('[name="body_markdown"]')?.dispatchEvent(new Event('editor:restore'));
    form.querySelector('[name="kind"]')?.dispatchEvent(new Event('change', {bubbles: true}));
    form.querySelector('[name="featured_image_id"]')?.dispatchEvent(new Event('editor:restore'));
  }
  function readRecords() {
    const records = [];
    for (const key of Object.keys(localStorage).filter(k => k.startsWith(prefix))) {
      let record;
      try { record = JSON.parse(localStorage.getItem(key)); } catch (_) { continue; }
      if (!record || !Number.isFinite(record.savedAt)) continue;
      if (Date.now() - record.savedAt > retention) { localStorage.removeItem(key); continue; }
      if (Array.isArray(record.data?.fields)) records.push(record);
    }
    return records;
  }
  function initLibrary() {
    const library = document.querySelector('[data-recovery-library]');
    if (!library || library.dataset.wired) return;
    library.dataset.wired = '1';
    library.addEventListener('toggle', () => {
      if (!library.open) return;
      active?.persist();
      const list = library.querySelector('[data-recovery-list]');
      list.replaceChildren();
      try {
        const records = readRecords().filter(record => {
          try {
            const [user, site] = JSON.parse(record.scope);
            return user === library.dataset.recoveryUser && site === library.dataset.recoverySite;
          } catch (_) { return false; }
        });
        records.sort((a, b) => b.savedAt - a.savedAt).forEach(record => {
          const copy = document.createElement('details');
          const summary = document.createElement('summary');
          const title = record.data.fields.find(([name]) => name === 'title')?.[1] || 'Untitled';
          const context = JSON.parse(record.scope)[3] === 'working-copy' ? 'working copy' : 'live editor';
          summary.textContent = `${title} (${context}), ${new Date(record.savedAt).toLocaleString()}`;
          const text = document.createElement('textarea');
          text.readOnly = true; text.rows = 12;
          text.setAttribute('aria-label', 'Recovered writing');
          // Text only: author content must never become executable admin markup.
          const lines = [];
          const append = fields => fields.forEach(([name, value]) => {
            lines.push(name.replace(/_/g, ' ') + ':', String(value), '');
          });
          append(record.data.fields);
          if (record.data.resume) {
            append(record.data.resume.single);
            record.data.resume.groups.forEach(([name, rows]) => {
              rows.forEach((row, index) => {
                lines.push(`${name} ${index + 1}`);
                append(row.fields);
              });
            });
          }
          text.value = lines.join('\n');
          copy.append(summary, text); list.append(copy);
        });
        if (!records.length) list.textContent = 'No browser recovery copies for this account and site.';
      } catch (_) {
        list.textContent = 'Browser recovery is unavailable. Keep any open editors open and copy your work.';
      }
    });
  }
  function init() {
    document.querySelectorAll('[data-editor-saved]').forEach(receipt => {
      try {
        const key = prefix + receipt.dataset.editorSaved;
        const record = JSON.parse(localStorage.getItem(key) || 'null');
        for (const source of record?.sources || []) {
          const sourceKey = prefix + source.id;
          const current = JSON.parse(localStorage.getItem(sourceKey) || 'null');
          if (current?.scope === record.scope && current.savedAt === source.savedAt) localStorage.removeItem(sourceKey);
        }
        localStorage.removeItem(key);
      } catch (_) { /* The editor below reports unavailable storage. */ }
      receipt.remove();
    });
    initLibrary();
    const form = document.querySelector('form[data-editor-recovery]');
    if (!form || active?.form === form) return;
    let id = crypto.randomUUID();
    const scope = JSON.stringify(['user', 'site', 'entity', 'context'].map(k => form.dataset['recovery' + k[0].toUpperCase() + k.slice(1)]));
    let baseline = JSON.stringify(snapshot(form));
    let sources = [], timer, submitting = false, submittedSnapshot;
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    status.textContent = 'Unsaved changes are recoverable in this browser for 7 days. They are not published.';
    const panel = document.createElement('aside');
    panel.setAttribute('aria-label', 'Writing recovery');
    panel.append(status);
    form.prepend(panel);
    const receipt = document.createElement('input');
    receipt.type = 'hidden'; receipt.name = '_recovery_id'; receipt.value = id;
    form.append(receipt);
    const version = document.createElement('input');
    version.type = 'hidden'; version.name = '_recovery_version';
    form.append(version);
    const warn = () => { status.textContent = 'Browser recovery is unavailable. Keep this page open and copy your work before leaving.'; };
    const dirty = () => JSON.stringify(snapshot(form)) !== baseline;
    function persist() {
      clearTimeout(timer);
      if (!form.isConnected) return;
      try {
        // Further typing during a slow POST was not part of that submission.
        // Give it a new receipt id before the server acknowledges the old one.
        if (submittedSnapshot) {
          if (JSON.stringify(snapshot(form)) === submittedSnapshot) return;
          id = crypto.randomUUID(); receipt.value = id;
          baseline = submittedSnapshot;
          sources = []; submittedSnapshot = null; submitting = false;
        }
        if (!dirty() && !sources.length) { localStorage.removeItem(prefix + id); return; }
        const previous = JSON.parse(localStorage.getItem(prefix + id) || 'null');
        version.value = String(Math.max(Date.now(), (previous?.savedAt || 0) + 1));
        localStorage.setItem(prefix + id, JSON.stringify({id, scope, savedAt: Number(version.value),
          data: snapshot(form), baseToken: form.elements._edit_token?.value || '', sources}));
        status.textContent = 'Recovery copy updated at ' + new Date().toLocaleTimeString() + '. Not saved to Bragi.';
      } catch (_) { warn(); }
    }
    active = {form, dirty, persist, isSubmitting: () => submitting};
    try {
      const probe = prefix + id;
      localStorage.setItem(probe, 'null'); localStorage.removeItem(probe);
    } catch (_) { warn(); }
    // Full storage can still be read and cleared, so keep recovery controls available.
    try {
      // Unsaved responses carry only the submitted version, never newer tab work.
      const source = JSON.parse(localStorage.getItem(prefix + form.dataset.recoverySource) || 'null');
      if (source?.scope === scope && source.savedAt === Number(form.dataset.recoverySourceVersion)) {
        sources = [...(source.sources || []), {id: source.id, savedAt: source.savedAt}];
        persist();
      }
      const records = readRecords().filter(record => record.scope === scope && record.id !== id);
      records.sort((a, b) => b.savedAt - a.savedAt).forEach(record => {
        const row = document.createElement('p');
        row.dataset.recoveryId = record.id;
        row.append('Unfinished writing from ' + new Date(record.savedAt).toLocaleString() + ' ');
        const recover = document.createElement('button');
        recover.type = 'button'; recover.textContent = 'Restore';
        recover.addEventListener('click', () => {
          if (dirty() && !confirm('Replace the current form with this recovered writing?')) return;
          try {
            restore(form, record.data);
            if (form.elements._edit_token) form.elements._edit_token.value = record.baseToken;
            sources = [...(record.sources || []), {id: record.id, savedAt: record.savedAt}];
            persist();
            status.textContent = 'Writing restored. Review it before saving. Bragi will check for newer server changes.';
          } catch (_) { status.textContent = 'This recovery record could not be restored. Keep it and copy your current work.'; }
        });
        const discard = document.createElement('button');
        discard.type = 'button'; discard.textContent = 'Discard';
        discard.addEventListener('click', () => {
          if (!confirm('Permanently discard this browser recovery record?')) return;
          try {
            const current = JSON.parse(localStorage.getItem(prefix + record.id) || 'null');
            if (current && JSON.stringify(current) !== JSON.stringify(record)) {
              status.textContent = 'This recovery copy changed in another tab. Reload before discarding it.';
              return;
            }
            localStorage.removeItem(prefix + record.id); row.remove();
          } catch (_) { warn(); }
        });
        row.append(recover, ' ', discard); panel.append(row);
      });
    } catch (_) { warn(); }
    for (const event of ['input', 'change', 'click', 'dragend']) {
      form.addEventListener(event, () => { clearTimeout(timer); timer = setTimeout(persist, 250); });
    }
    form.addEventListener('submit', () => {
      persist(); submitting = true; submittedSnapshot = JSON.stringify(snapshot(form));
      // A failed request that leaves the page open must regain its leave guard.
      setTimeout(() => { submitting = false; }, 2000);
    });
  }
  window.addEventListener('beforeunload', event => {
    if (!active?.form.isConnected) return;
    active.persist();
    if (active.dirty() && !active.isSubmitting()) { event.preventDefault(); event.returnValue = ''; }
  });
  // htmx restores cached history synchronously in its bubbling popstate handler.
  window.addEventListener('popstate', () => active?.persist(), {capture: true});
  window.addEventListener('pagehide', () => active?.persist());
  document.addEventListener('visibilitychange', () => { if (document.hidden) active?.persist(); });
  document.addEventListener('submit', event => {
    if (!active?.form.isConnected || event.target === active.form || !active.dirty()) return;
    active.persist();
    if (!confirm('This action does not save your current edits. Continue?')) event.preventDefault();
  });
  document.addEventListener('htmx:beforeRequest', event => {
    if (!active?.form.isConnected || !event.detail.target?.contains(active.form)) return;
    active.persist();
    if (active.dirty() && !confirm('Leave with unsaved changes? Your browser recovery copy will remain.')) event.preventDefault();
  });
  for (const event of ['htmx:beforeSwap', 'htmx:historyCacheMissLoad']) {
    document.addEventListener(event, () => active?.persist());
  }
  for (const event of ['htmx:afterSwap', 'htmx:historyRestore']) document.addEventListener(event, init);
  init();
})();
