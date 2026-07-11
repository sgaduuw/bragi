// flatpickr in month mode: gives every browser a calendar UI for
// the <input type="month"> fields, since Firefox and Safari ship
// no native month picker (they fall back to plain text inputs).
// Loaded from esm.sh, same CDN pattern as the TipTap editor's
// admin imports. Author-facing dateFormat is "Y-m" (matches the
// YearMonth Pydantic pattern); altFormat shows "Apr 2024".
import flatpickr from 'https://esm.sh/flatpickr@4';
import monthSelectPlugin from 'https://esm.sh/flatpickr@4/dist/plugins/monthSelect/index.js';

// Generates a 12-char hex stable id for new rows. Matches
// bragi.contrib.page.resume._new_id() on the server side.
const newId = () => crypto.randomUUID().replace(/-/g, '').slice(0, 12);

const fieldset = document.getElementById('resume-fieldset');
if (fieldset) {
  const hiddenInput = document.getElementById('resume-data-hidden');

  // Wire flatpickr month picker on any <input type="month"> inside
  // the given root that hasn't been wired yet. The wired marker
  // (`data-flatpickr-wired`) prevents double-init when this is
  // called on a freshly-cloned row that the initial sweep already
  // skipped (it's idempotent either way).
  const wireMonthPickers = (root) => {
    root.querySelectorAll('input[type=month]:not([data-flatpickr-wired])').forEach((input) => {
      flatpickr(input, {
        plugins: [monthSelectPlugin({
          shorthand: true,
          dateFormat: 'Y-m',
          altFormat: 'M Y',
        })],
      });
      input.dataset.flatpickrWired = '1';
    });
  };
  wireMonthPickers(fieldset);

  // ---- Add row: clone template, give it a new id ----
  fieldset.querySelectorAll('.repeating-field__add').forEach((btn) => {
    btn.addEventListener('click', () => {
      const container = btn.closest('.repeating-field');
      const template = container.querySelector('.repeating-field__template');
      const rows = container.querySelector('.repeating-field__rows');
      const clone = template.content.firstElementChild.cloneNode(true);
      clone.dataset.id = newId();
      rows.appendChild(clone);
      // Wire the new row's month inputs (flatpickr only attaches
      // to elements actually in the live DOM; template content
      // is skipped above).
      wireMonthPickers(clone);
      syncLinkedPositionOptions();
    });
  });

  // ---- Remove row (delegated) ----
  fieldset.addEventListener('click', (e) => {
    const btn = e.target.closest('.repeating-field__remove');
    if (!btn) return;
    e.preventDefault();
    btn.closest('.repeating-field__row').remove();
    syncLinkedPositionOptions();
  });

  // ---- Drag-to-reorder via HTML5 DnD ----
  let dragged = null;
  fieldset.addEventListener('dragstart', (e) => {
    const row = e.target.closest('.repeating-field__row');
    if (!row) return;
    dragged = row;
    row.classList.add('repeating-field__row--dragging');
  });
  fieldset.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('repeating-field__row--dragging');
    dragged = null;
  });
  fieldset.addEventListener('dragover', (e) => {
    const row = e.target.closest('.repeating-field__row');
    if (!row || !dragged || row === dragged) return;
    // Only allow reorder within the same field container.
    if (row.parentElement !== dragged.parentElement) return;
    e.preventDefault();
    const rect = row.getBoundingClientRect();
    const after = (e.clientY - rect.top) > rect.height / 2;
    row.parentElement.insertBefore(dragged, after ? row.nextSibling : row);
  });

  // ---- Linked-position dropdown sync ----
  // Watches the Experience section's rows; rebuilds <option>s in
  // every Project row's `select.js-linked-position` to reflect
  // current company/role/start_date labels. Re-runs on add /
  // remove / blur (so edits to company name propagate).
  function syncLinkedPositionOptions() {
    const expRows = fieldset.querySelectorAll(
      '[data-section="experience"] .repeating-field__row'
    );
    const options = [];
    expRows.forEach((row) => {
      const id = row.dataset.id;
      const company = row.querySelector('[data-field="company"]').value || '(unnamed)';
      const role = row.querySelector('[data-field="role"]').value || '';
      const start = row.querySelector('[data-field="start_date"]').value || '';
      options.push({
        id,
        label: `${company} — ${role}${start ? ` (${start})` : ''}`,
      });
    });
    fieldset.querySelectorAll('select.js-linked-position').forEach((sel) => {
      const current = sel.value || sel.dataset.current || '';
      sel.innerHTML =
        '<option value="">— Not linked / personal project —</option>' +
        options
          .map(
            (o) =>
              `<option value="${o.id}"${o.id === current ? ' selected' : ''}>` +
              `${o.label}</option>`
          )
          .join('');
    });
  }
  fieldset.addEventListener('input', (e) => {
    const inExperience = e.target.closest('[data-section="experience"]');
    if (inExperience) syncLinkedPositionOptions();
  });
  syncLinkedPositionOptions();

  // ---- Form submit: serialise sections to JSON, set hidden input ----
  const form = fieldset.closest('form');
  if (form) {
    form.addEventListener('submit', () => {
      const data = serializeResumeData();
      hiddenInput.value = JSON.stringify(data);
    });
  }

  function rowToObject(row) {
    // Reads every input/textarea/select with data-field; returns
    // an object keyed by field name. Includes the row's stable id.
    const obj = { id: row.dataset.id };
    row.querySelectorAll('[data-field]').forEach((el) => {
      const field = el.dataset.field;
      const raw = el.value;
      if (el.dataset.multiline === '1') {
        // Newline-separated array, trimmed, empty lines dropped.
        obj[field] = raw
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean);
      } else if (el.dataset.split === 'comma') {
        obj[field] = raw
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
      } else if (el.type === 'number') {
        obj[field] = raw === '' ? null : Number(raw);
      } else if (el.tagName === 'TEXTAREA') {
        // Textareas back required-string fields in the resume
        // schema (description_markdown on Position / Project /
        // Education, declared as `str = ""`). An empty textarea
        // means "no narrative yet"; we must send "" so pydantic's
        // str validator accepts it. Sending null (the default-
        // input behaviour below) breaks validation on save with
        // "Input should be a valid string", which surfaces in the
        // wild whenever LinkedIn imports a current position that
        // has no Description set on LinkedIn's side. Multiline
        // array textareas (impacts) take the data-multiline
        // branch above and never reach here.
        obj[field] = raw;
      } else {
        obj[field] = raw === '' ? null : raw;
      }
    });
    return obj;
  }

  function serializeResumeData() {
    const data = {
      header: { tagline: null, location: null, profile_links: [] },
      highlights: [],
      experience: [],
      projects: [],
      education: [],
      skills: [],
      certifications: [],
      languages: [],
      lead_with_role: false,
    };

    // Display toggle: checkbox lives at the top of the fieldset,
    // outside any [data-section] so the SECTIONS sweep below
    // does not touch it.
    const leadRole = document.getElementById('resume-lead-with-role');
    data.lead_with_role = !!(leadRole && leadRole.checked);

    // Header singletons
    data.header.tagline =
      document.getElementById('resume-header-tagline').value || null;
    data.header.location =
      document.getElementById('resume-header-location').value || null;

    // Header.profile_links (repeating)
    const hdrLinks = fieldset.querySelectorAll(
      '[data-section="header"] .repeating-field__row'
    );
    hdrLinks.forEach((row) => {
      const item = rowToObject(row);
      if (item.label || item.url) data.header.profile_links.push(item);
    });

    // Highlights — flat strings (the row's only data-field is "text")
    const hlRows = fieldset.querySelectorAll(
      '[data-section="highlights"] .repeating-field__row'
    );
    hlRows.forEach((row) => {
      const text = row.querySelector('[data-field="text"]').value.trim();
      if (text) data.highlights.push(text);
    });

    // Generic per-section serialiser for the structured types
    const SECTIONS = [
      'experience',
      'projects',
      'education',
      'skills',
      'certifications',
      'languages',
    ];
    SECTIONS.forEach((section) => {
      const rows = fieldset.querySelectorAll(
        `[data-section="${section}"] .repeating-field__row`
      );
      rows.forEach((row) => {
        data[section].push(rowToObject(row));
      });
    });

    return data;
  }
}
