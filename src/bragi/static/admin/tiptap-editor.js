// TipTap + Markdown via CDN ESM. Self-hosted bundle + Subresource
// Integrity is a follow-up; admin is behind auth so the surface
// is the operator's own browser.
import { Editor, Node } from 'https://esm.sh/@tiptap/core@2.6';
import StarterKit from 'https://esm.sh/@tiptap/starter-kit@2.6';
import Link from 'https://esm.sh/@tiptap/extension-link@2.6';
// Image is not part of StarterKit. Without an Image node in the
// schema, the picker's `insertContent('![alt](url)')` would land
// as a plain text node whose markdown chars get escaped on save,
// and pasted / drag-dropped `<img>` tags coerce to literal text
// that gets HTML-escaped on save. The result either way: stored
// markdown like `\!\[alt\]\(url\)` or `&lt;img ...&gt;`, both
// rendering as visible text instead of images (#270). With this
// extension installed, tiptap-markdown serializes Image nodes
// straight to `![alt](src)`.
import Image from 'https://esm.sh/@tiptap/extension-image@2.6';
import { Markdown } from 'https://esm.sh/tiptap-markdown@0.8';
// markdown-it attrs plugin — same syntax as mdit-py-plugins'
// attrs_plugin on the Python side. Needed in the editor's
// markdown parser so `{.class}` on a reloaded body round-trips
// back into the Image node's `class` attribute. Without this,
// class survives save but is dropped on reload.
import markdownItAttrs from 'https://esm.sh/markdown-it-attrs@4';
// markdown-it-container — the JS mirror of mdit-py-plugins'
// container_plugin used by bragi.contrib.callouts. Registered per
// callout type in the Callout node's parse.setup so a reloaded
// `::: note ... :::` produces the same <aside class="callout"> HTML
// the Python directive emits, which Callout.parseHTML then captures.
import markdownItContainer from 'https://esm.sh/markdown-it-container@4';
// Pipe-table support. StarterKit ships no table node, so these four
// add the schema (table > tableRow > tableHeader|tableCell). Parse
// (markdown -> editor) is free: tiptap-markdown renders markdown to
// HTML and these nodes' parseHTML handles <table>/<tr>/<th>/<td>,
// once markdown-it's table rule is enabled (see TableWithMarkdown
// below). Only serialize (editor -> markdown) needs custom code.
// resizable is left off: column widths are cell attrs with no GFM
// representation, so they would silently vanish on save (deferred,
// see _claude/specs/2026-06-15-tiptap-table-support-design.md).
import Table from 'https://esm.sh/@tiptap/extension-table@2.6';
import TableRow from 'https://esm.sh/@tiptap/extension-table-row@2.6';
import TableHeader from 'https://esm.sh/@tiptap/extension-table-header@2.6';
import TableCell from 'https://esm.sh/@tiptap/extension-table-cell@2.6';

// Config injected by the template as a non-executable JSON island
// (`type="application/json"` is not restricted by CSP `script-src`).
// Values (textarea id, picker URLs, attachment prefix) are computed
// server-side exactly as the former inline module interpolated them.
const cfg = JSON.parse(document.getElementById('tiptap-editor-config').textContent);
// BubbleMenu floats a small toolbar above the focused Image node
// so the operator can change size + alignment without leaving the
// editor. Admin-only; delivery ships none of this.
// markdown-it plugin enabling `{.class}` attribute syntax on
// images. The Python-side renderer wires `mdit-py-plugins`'s
// `attrs_plugin` already; this JS-side counterpart keeps the
// editor's load-then-serialize cycle from silently dropping
// the class. See _claude/specs/2026-05-27-image-size-classes-design.md.

// Image: extend with a `class` HTML attribute so size + align
// classes (`size-medium align-center`, etc.) round-trip through
// markdown via the attrs_plugin syntax. Without `addAttributes`
// overriding the parent Image's schema, TipTap drops unknown
// HTML attributes on the node and the class vanishes on first
// serialize.
//
// Per-size rendition URLs (renditionSmall/Medium/Full) are also
// tracked on the node so the in-editor `<img src>` matches the
// active size class. Without these, the editor renders the
// multi-MB original even when the operator picks `size-small`,
// and the editor becomes unusable on photos. The renditions are
// NOT serialized to markdown — the stored `src` stays the
// original-sha admin URL (which the load transform converts to /
// from the public-prefix path the body persists as), so
// pictureify on delivery still wraps the original in a
// <picture>.
const SIZE_TO_RENDITION_ATTR = {
  'size-small': 'renditionSmall',
  'size-medium': 'renditionMedium',
  'size-full': 'renditionFull',
};

// Server-injected rendition lookup so reloading a post keeps the
// in-editor `<img src>` pointing at the right-sized WebP instead
// of falling back to the multi-MB original. Only the `class` is
// stored in the markdown body — rendition URLs are editor-only
// and would otherwise vanish on round-trip. The map is keyed by
// sha (extracted from the image src); see attachments plugin's
// `editor_image_renditions` Jinja global.
let renditionMap = {};
try {
  const renditionScript = document.getElementById('image-renditions');
  if (renditionScript && renditionScript.textContent.trim()) {
    renditionMap = JSON.parse(renditionScript.textContent);
  }
} catch (e) {
  console.warn('[bragi-editor] failed to parse rendition map', e);
}

// src may be either the public path (`/attachments/<sha>`) or
// the admin-rewritten path (`/admin/sites/<slug>/attachments/file/<sha>`).
// Optional `file/` segment handles both.
const SHA_FROM_SRC_RE = /\/attachments\/(?:file\/)?([a-f0-9]{64})/;
const renditionUrlForSrc = (src, size) => {
  const match = (src || '').match(SHA_FROM_SRC_RE);
  if (!match) return null;
  const tiers = renditionMap[match[1]];
  if (!tiers) return null;
  const key = tiers[size];
  return key ? ADMIN_ATTACHMENT_PREFIX + key : null;
};
const ClassedImage = Image.extend({
  // tiptap-markdown reads `storage.markdown.serialize` off each
  // node's extension and uses it when emitting markdown. Without
  // this, the default Image serializer emits `![alt](src)` with
  // no class, so save → reload drops the class on every cycle.
  // The parse half (markdown → editor) is handled by the
  // Python-side attrs_plugin which converts `{.class}` into a
  // `class="..."` attribute on the <img>; ClassedImage's
  // `parseHTML` for the class attribute then captures it.
  addStorage() {
    return {
      markdown: {
        serialize(state, node) {
          const alt = state.esc(node.attrs.alt || '');
          const src = state.esc(node.attrs.src || '');
          // markdown-it-attrs (and Python's mdit-py-plugins
          // attrs_plugin) only recognises `{.class}` when it
          // directly follows the closing `)` with no whitespace.
          // A single space turns the whole curly block into
          // literal text and silently drops the class on both
          // reload and delivery render.
          const cls = node.attrs.class
            ? `{.${node.attrs.class.split(' ').filter(Boolean).join(' .')}}`
            : '';
          state.write(`![${alt}](${src})${cls}`);
        },
        parse: {
          // setup(md) is invoked by tiptap-markdown's parser with
          // its internal markdown-it instance at parse time. We
          // register the attrs plugin so `{.class}` on a reloaded
          // body round-trips back into the <img>'s class
          // attribute, which ClassedImage.addAttributes.class
          // then captures.
          setup(md) {
            md.use(markdownItAttrs);
          },
        },
      },
    };
  },
  addAttributes() {
    return {
      ...this.parent?.(),
      class: {
        default: null,
        parseHTML: (el) => el.getAttribute('class'),
        renderHTML: (attrs) => {
          if (!attrs.class) return {};
          return { class: attrs.class };
        },
      },
      // Rendition URLs by size class. Editor-only — read by
      // renderHTML below to pick the visible `<img src>`; never
      // serialized to markdown. `renderHTML: () => ({})` is the
      // TipTap idiom for "don't put this in the rendered HTML
      // attributes". parseHTML looks the URL up in the server-
      // injected rendition map (keyed by sha extracted from the
      // image src), so reloading a post hydrates the same
      // rendition URLs the picker set at insert time.
      renditionSmall: {
        default: null,
        parseHTML: (el) => renditionUrlForSrc(el.getAttribute('src'), 'small'),
        renderHTML: () => ({}),
      },
      renditionMedium: {
        default: null,
        parseHTML: (el) => renditionUrlForSrc(el.getAttribute('src'), 'medium'),
        renderHTML: () => ({}),
      },
      renditionFull: {
        default: null,
        parseHTML: (el) => renditionUrlForSrc(el.getAttribute('src'), 'full'),
        renderHTML: () => ({}),
      },
    };
  },
  renderHTML({ node, HTMLAttributes }) {
    // Pick the per-size rendition URL based on the node's
    // active size class. Falls back to the node's `src` (the
    // original) when no matching rendition is set.
    const classes = (node.attrs.class || '').split(/\s+/);
    for (const cls of classes) {
      const attrName = SIZE_TO_RENDITION_ATTR[cls];
      if (attrName && node.attrs[attrName]) {
        return ['img', { ...HTMLAttributes, src: node.attrs[attrName] }];
      }
    }
    return ['img', HTMLAttributes];
  },
});

// GFM table cells are single-line inline text. @tiptap/extension-table
// cells default to `content: 'block+'` (paragraphs, lists), which a
// pipe table cannot represent. Constraining to inline makes the editor
// unable to author what the markdown round-trip would lose. The
// serializer below also flattens any in-cell hard break to a space.
const InlineTableCell = TableCell.extend({ content: 'inline*' });
const InlineTableHeader = TableHeader.extend({ content: 'inline*' });

// Render a single cell's inline content to a markdown string by
// letting tiptap-markdown render it into `state.out`, then slicing
// off and rolling back what it appended (we assemble the table layout
// ourselves). Marks inside the cell (bold, italic, code, links)
// survive because renderInline emits their markdown delimiters.
function serializeTableCell(state, cellNode) {
  const beforeLen = state.out.length;
  const beforeClosed = state.closed;
  const beforeAtBlockStart = state.atBlockStart;
  state.renderInline(cellNode);
  let text = state.out.slice(beforeLen);
  state.out = state.out.slice(0, beforeLen);
  state.closed = beforeClosed;
  state.atBlockStart = beforeAtBlockStart;
  // Flatten hard breaks / newlines (and any escaping backslash a
  // hardBreak emits before its newline) to a single space; collapse
  // runs of whitespace; trim; escape pipes so they do not split the
  // row. Empty cell becomes a single space to keep the row parseable.
  text = text.replace(/\\?\n+/g, ' ').replace(/\s+/g, ' ').trim();
  text = text.replace(/\|/g, '\\|');
  return text === '' ? ' ' : text;
}

const TableWithMarkdown = Table.extend({
  addStorage() {
    return {
      markdown: {
        // Editor -> markdown. Row 0 is always emitted as the GFM
        // header row followed by the `| --- |` delimiter, so every
        // saved table is valid GFM even if the operator toggled the
        // header off. Column count comes from row 0.
        serialize(state, node) {
          // Close any preceding block (e.g. a paragraph) with its
          // blank-line separation BEFORE the cell loop below runs any
          // renderInline. renderInline internally flushes the pending
          // close; if that happens during the rolled-back cell capture
          // the separation is discarded and the table glues onto the
          // previous block (invalid GFM that will not parse back as a
          // table). Flushing here, against committed output, keeps the
          // blank line.
          state.flushClose();
          const rows = [];
          node.forEach((rowNode) => {
            const cells = [];
            rowNode.forEach((cellNode) => {
              cells.push(serializeTableCell(state, cellNode));
            });
            rows.push(cells);
          });
          if (rows.length === 0) return;
          const colCount = rows[0].length;
          const line = (cells) => {
            const c = cells.slice(0, colCount);
            while (c.length < colCount) c.push(' ');
            return '| ' + c.join(' | ') + ' |';
          };
          state.write(line(rows[0]));
          state.ensureNewLine();
          state.write('| ' + Array(colCount).fill('---').join(' | ') + ' |');
          state.ensureNewLine();
          for (let i = 1; i < rows.length; i++) {
            state.write(line(rows[i]));
            state.ensureNewLine();
          }
          state.closeBlock(node);
        },
        // markdown -> editor. Enable markdown-it's table rule on
        // tiptap-markdown's internal parser so a loaded body's pipe
        // table becomes <table> HTML, which the table extensions'
        // parseHTML then turns into editor nodes. Mirrors how
        // ClassedImage registers markdown-it-attrs via this hook.
        parse: {
          setup(md) {
            md.enable('table');
          },
        },
      },
    };
  },
});

// Site-scoped admin URL for attachment bytes. The stored
// markdown uses public `/attachments/<key>` URLs so it round-
// trips cleanly to delivery; in the editor we rewrite to the
// admin-scoped URL on load (so in-editor previews resolve) and
// back to the public URL on save. The delivery `/attachments/`
// route resolves the site from the Host header, which doesn't
// match on the admin host, so the editor can't fetch them
// unrewritten.
// With media enabled the prefix is the site-scoped admin attachment
// URL (needs a `site_slug` from the route). With media OFF (e.g. the
// global account bio, no site in the route) there is no site to scope
// to and no image picker, so use a non-empty sentinel: it must stay
// non-empty so ADMIN_ATTACHMENT_MARKER ("](" + prefix) can't collapse
// to "](" and match every link in adminToPublic.
const ADMIN_ATTACHMENT_PREFIX = cfg.attachmentPrefix;
const PUBLIC_ATTACHMENT_MARKER = "](/attachments/";
const ADMIN_ATTACHMENT_MARKER = "](" + ADMIN_ATTACHMENT_PREFIX;

function publicToAdmin(md) {
  return md.split(PUBLIC_ATTACHMENT_MARKER).join(ADMIN_ATTACHMENT_MARKER);
}
function adminToPublic(md) {
  return md.split(ADMIN_ATTACHMENT_MARKER).join(PUBLIC_ATTACHMENT_MARKER);
}

// Callout / admonition node: `::: <type> ... :::` <-> a styled <aside>.
// Delivery renders these server-side (bragi.contrib.callouts); this node
// gives the editor the same box and round-trips the markdown. Body is
// block content (parseHTML pulls it from `.callout__body`); the title
// stays out of the content. Keep the type list aligned with the Python
// directive's CALLOUT_TYPES.
const CALLOUT_TYPES = ['note', 'tip', 'info', 'warning', 'danger'];
const calloutTitleCase = (t) => t.charAt(0).toUpperCase() + t.slice(1);
const calloutTypeFromEl = (el) => {
  for (const t of CALLOUT_TYPES) {
    if (el.classList.contains('callout--' + t)) return t;
  }
  return 'note';
};

const Callout = Node.create({
  name: 'callout',
  group: 'block',
  content: 'block+',
  defining: true,
  addAttributes() {
    return {
      calloutType: {
        default: 'note',
        parseHTML: (el) => calloutTypeFromEl(el),
        renderHTML: () => ({}),  // carried in the class, not a DOM attr
      },
      title: {
        default: null,
        parseHTML: (el) => {
          const titleEl = el.querySelector('.callout__title');
          if (!titleEl) return null;
          const text = titleEl.textContent.trim();
          // A title equal to the capitalized type is the default; store
          // null so serialize doesn't re-emit a redundant custom title.
          return text === calloutTitleCase(calloutTypeFromEl(el)) ? null : text;
        },
        renderHTML: () => ({}),
      },
    };
  },
  parseHTML() {
    return [{ tag: 'aside.callout', contentElement: '.callout__body' }];
  },
  renderHTML({ node }) {
    const type = node.attrs.calloutType || 'note';
    const title = node.attrs.title || calloutTitleCase(type);
    return [
      'aside',
      { class: 'callout callout--' + type },
      ['p', { class: 'callout__title' }, title],
      ['div', { class: 'callout__body' }, 0],
    ];
  },
  addStorage() {
    return {
      markdown: {
        serialize(state, node) {
          const type = node.attrs.calloutType || 'note';
          const header = node.attrs.title ? `${type} ${node.attrs.title}` : type;
          state.write(`::: ${header}\n`);
          state.renderContent(node);
          state.write(':::');
          state.closeBlock(node);
        },
        parse: {
          setup(md) {
            CALLOUT_TYPES.forEach((type) => {
              md.use(markdownItContainer, type, {
                render(tokens, idx) {
                  const token = tokens[idx];
                  if (token.nesting !== 1) return '</div></aside>\n';
                  const rest = token.info.trim().split(/\s+/).slice(1).join(' ');
                  const title = md.utils.escapeHtml(rest || calloutTitleCase(type));
                  return `<aside class="callout callout--${type}">`
                    + `<p class="callout__title">${title}</p>`
                    + `<div class="callout__body">`;
                },
              });
            });
          },
        },
      },
    };
  },
});

const textarea = document.getElementById(cfg.textareaId);
const mount = document.getElementById('tiptap-editor');
const toolbar = document.getElementById('tiptap-editor-toolbar');
if (!textarea || !mount || !toolbar) {
  // Template shape changed; let the textarea stand alone.
  console.warn('tiptap editor mount missing; falling back to textarea');
} else {
  const editor = new Editor({
    element: mount,
    extensions: [
      StarterKit,
      Link.configure({ openOnClick: false, autolink: false }),
      ClassedImage,
      // resizable: false set explicitly (not relying on the extension
      // default) so a future CDN bump that flips the default cannot
      // silently introduce colwidth cell attrs, which have no GFM
      // representation and would vanish on save (round-trip hazard).
      TableWithMarkdown.configure({ resizable: false }),
      TableRow,
      InlineTableHeader,
      InlineTableCell,
      Callout,
      Markdown.configure({
        html: false,
        linkify: true,
        breaks: false,
        tightLists: true,
        // The save / load class round-trip is plumbed via
        // ClassedImage's `addStorage.markdown.serialize` and
        // `addStorage.markdown.parse.setup` — both are read by
        // tiptap-markdown@0.8.10 (`MarkdownSerializer`'s
        // `getMarkdownSpec(node).serialize`, `MarkdownParser`'s
        // per-parse `parse.setup(md)` walk), so no overrides
        // here.
      }),
    ],
    content: publicToAdmin(textarea.value),
    onUpdate: ({ editor }) => {
      textarea.value = adminToPublic(editor.storage.markdown.getMarkdown());
    },
    onSelectionUpdate: ({ editor }) => updateToolbarActive(editor),
  });

  document.body.classList.add('has-tiptap-editor');
  // Hide the source textarea directly (id-agnostic, since the editor
  // can bind to any caller-provided textarea id).
  textarea.style.display = 'none';
  mount.hidden = false;
  toolbar.hidden = false;
  // Image bubble menu: we manage visibility + positioning
  // ourselves rather than via @tiptap/extension-bubble-menu
  // (Tippy.js). Tippy was clobbering the element in our setup
  // (the DOM was empty after init), so we keep the menu in its
  // template position, hide it by default, and `position:
  // absolute` it above the focused image on selectionUpdate.
  const imageBubbleMenu = document.getElementById('tiptap-image-bubble-menu');
  if (imageBubbleMenu) {
    // Reparent to <body> so the menu is free of any positioned
    // ancestor in the host form scaffolding. Without this the
    // `position: absolute` math is off by the offsetParent's
    // document-relative offset, which is what the "menu lands
    // at the top-left of the editor" bug was.
    document.body.appendChild(imageBubbleMenu);
    imageBubbleMenu.style.position = 'absolute';
    imageBubbleMenu.style.zIndex = '50';
    imageBubbleMenu.hidden = true;
  }

  // Prefixes that map to bragi internal-link content types. v1
  // ships post + page in-tree; this list is the JS-side mirror of
  // the registered `ContentTypeSpec.internal_link_prefix` values.
  // A third-party content type that adopts the picker will need
  // one entry added here (small enough to be acceptable v1 cost;
  // an endpoint emitting the live list is a possible follow-up).
  const INTERNAL_LINK_PREFIXES = ['post', 'page'];

  function currentInternalLinkMarker(editor) {
    if (!editor.isActive('link')) return null;
    const href = editor.getAttributes('link').href || '';
    const colon = href.indexOf(':');
    if (colon < 1) return null;
    const prefix = href.slice(0, colon);
    if (INTERNAL_LINK_PREFIXES.indexOf(prefix) === -1) return null;
    return href;
  }

  function updateToolbarActive(editor) {
    const map = {
      bold: () => editor.isActive('bold'),
      italic: () => editor.isActive('italic'),
      code: () => editor.isActive('code'),
      h1: () => editor.isActive('heading', { level: 1 }),
      h2: () => editor.isActive('heading', { level: 2 }),
      h3: () => editor.isActive('heading', { level: 3 }),
      'bullet-list': () => editor.isActive('bulletList'),
      'ordered-list': () => editor.isActive('orderedList'),
      blockquote: () => editor.isActive('blockquote'),
      'code-block': () => editor.isActive('codeBlock'),
      link: () => editor.isActive('link') && !currentInternalLinkMarker(editor),
      'internal-link': () => !!currentInternalLinkMarker(editor),
      table: () => editor.isActive('table'),
    };
    toolbar.querySelectorAll('button[data-action]').forEach((btn) => {
      const fn = map[btn.dataset.action];
      btn.classList.toggle('is-active', !!(fn && fn()));
    });
  }

  toolbar.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    e.preventDefault();
    // Collapse the Table dropdown after any toolbar action so it does
    // not stay open over the editor.
    const openMenu = btn.closest('details.toolbar-menu');
    if (openMenu) openMenu.open = false;
    const chain = editor.chain().focus();
    switch (btn.dataset.action) {
      case 'bold': chain.toggleBold().run(); break;
      case 'italic': chain.toggleItalic().run(); break;
      case 'code': chain.toggleCode().run(); break;
      case 'h1': chain.toggleHeading({ level: 1 }).run(); break;
      case 'h2': chain.toggleHeading({ level: 2 }).run(); break;
      case 'h3': chain.toggleHeading({ level: 3 }).run(); break;
      case 'bullet-list': chain.toggleBulletList().run(); break;
      case 'ordered-list': chain.toggleOrderedList().run(); break;
      case 'blockquote': chain.toggleBlockquote().run(); break;
      case 'code-block': chain.toggleCodeBlock().run(); break;
      case 'link': {
        const previous = editor.getAttributes('link').href || '';
        const url = window.prompt('Link URL', previous);
        if (url === null) break;
        if (url === '') { chain.unsetLink().run(); break; }
        chain.extendMarkRange('link').setLink({ href: url }).run();
        break;
      }
      case 'unlink': chain.unsetLink().run(); break;
      case 'image': openImagePicker(); break;
      case 'internal-link': openInternalLinkPicker(currentInternalLinkMarker(editor)); break;
      case 'table':
        chain.insertTable({ rows: 2, cols: 2, withHeaderRow: true }).run();
        break;
      case 'table-row-before': chain.addRowBefore().run(); break;
      case 'table-row-after': chain.addRowAfter().run(); break;
      case 'table-col-before': chain.addColumnBefore().run(); break;
      case 'table-col-after': chain.addColumnAfter().run(); break;
      case 'table-row-delete': chain.deleteRow().run(); break;
      case 'table-col-delete': chain.deleteColumn().run(); break;
      case 'table-header-toggle': chain.toggleHeaderRow().run(); break;
      case 'table-delete': chain.deleteTable().run(); break;
      case 'callout-note':
      case 'callout-tip':
      case 'callout-info':
      case 'callout-warning':
      case 'callout-danger':
        chain.insertContent({
          type: 'callout',
          attrs: { calloutType: btn.dataset.action.slice('callout-'.length) },
          content: [{ type: 'paragraph' }],
        }).run();
        break;
    }
  });

  // Image picker: open the `<dialog>`, lazy-load the grid via
  // htmx so the page weight stays light when the operator isn't
  // embedding images. Selecting a card inserts a markdown image
  // link at the cursor and closes the dialog.
  const pickerDialog = document.getElementById('image-picker-dialog');
  const pickerTarget = document.getElementById('image-picker-target');
  const pickerUrl = cfg.pickerUrl;

  function openImagePicker() {
    if (!pickerDialog || !pickerTarget) return;
    // Load the grid into the dialog body. htmx.ajax replaces
    // innerHTML and processes any hx-* attributes in the loaded
    // markup (pagination, site filter).
    if (window.htmx) {
      window.htmx.ajax('GET', pickerUrl, { target: pickerTarget, swap: 'innerHTML' });
    } else {
      pickerTarget.innerHTML = '<p style="padding: 1rem;">htmx unavailable; close and try again.</p>';
    }
    pickerDialog.showModal();
  }

  if (pickerDialog) {
    pickerDialog.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="close-picker"]')) {
        pickerDialog.close();
        return;
      }
      const card = e.target.closest('.picker-card');
      if (!card) return;
      e.preventDefault();
      const key = card.dataset.storageKey || '';
      const filename = card.dataset.filename || '';
      const alt = card.dataset.altText || filename || 'image';
      if (!key) return;
      // setImage inserts a proper Image node into the document so
      // tiptap-markdown serializes it as `![alt](src)`. The
      // previous `insertContent('![alt](url)')` shape inserted the
      // markdown as plain text, which then got its special chars
      // escaped on save (#270). The src uses the admin-scoped URL
      // so the in-editor preview resolves; adminToPublic rewrites
      // it back to the public form on save.
      //
      // The per-size rendition URLs land on the node as
      // editor-only attrs; ClassedImage's renderHTML picks the
      // one matching the active size class so the visible
      // `<img src>` is the right-sized bytes (otherwise size-small
      // CSS-scales a 1.8MB JPEG and the editor lays out badly).
      // Each rendition key is a path-style storage_key
      // (`<sha>/<width>/webp`); empty string means no done WebP
      // at that tier yet, which falls back to the original src
      // via renderHTML.
      const buildRenditionUrl = (k) => k ? ADMIN_ATTACHMENT_PREFIX + k : null;
      editor
        .chain()
        .focus()
        .setImage({
          src: ADMIN_ATTACHMENT_PREFIX + key,
          alt: alt,
          // Default class: medium width, centered. The BubbleMenu
          // on the inserted Image node lets the operator swap to
          // small/full or left/right after insertion.
          class: 'size-medium align-center',
          renditionSmall: buildRenditionUrl(card.dataset.renditionSmall || ''),
          renditionMedium: buildRenditionUrl(card.dataset.renditionMedium || ''),
          renditionFull: buildRenditionUrl(card.dataset.renditionFull || ''),
        })
        .run();
      pickerDialog.close();
    });
    // Click on the backdrop closes; <dialog> doesn't do this by default.
    pickerDialog.addEventListener('click', (e) => {
      if (e.target === pickerDialog) pickerDialog.close();
    });
  }

  // Internal-link picker: same htmx-into-<dialog> pattern as the
  // image picker. Selecting a card either replaces the current
  // link mark's href (cursor in an existing internal link),
  // wraps the current selection with a fresh link mark, or
  // inserts the target's title with the mark applied (no
  // selection). Persisted form for all three cases is the
  // typed-prefix marker `post:<id>` / `page:<id>`, which the
  // tiptap-markdown serializer round-trips to
  // `[text](post:<id>)`.
  const internalDialog = document.getElementById('internal-link-picker-dialog');
  const internalTarget = document.getElementById('internal-link-picker-target');
  const internalPickerUrl = cfg.internalPickerUrl;

  function openInternalLinkPicker(currentMarker) {
    if (!internalDialog || !internalTarget) return;
    if (window.htmx) {
      window.htmx.ajax('GET', internalPickerUrl, {
        target: internalTarget,
        swap: 'innerHTML',
      }).then(() => {
        // After-swap: highlight the currently-targeted card if
        // we're editing an existing internal link. Auto-focus
        // the search input either way.
        if (currentMarker) {
          const card = internalTarget.querySelector(
            '.picker-card[data-internal-link-marker="' + currentMarker + '"]'
          );
          if (card) {
            card.classList.add('is-selected');
            card.scrollIntoView({ block: 'nearest' });
          }
        }
        const input = internalTarget.querySelector('#picker-q');
        if (input) input.focus();
      });
    } else {
      internalTarget.innerHTML =
        '<p style="padding: 1rem;">htmx unavailable; close and try again.</p>';
    }
    internalDialog.showModal();
  }

  function insertInternalLink(marker, displayTitle) {
    // Three cases, in priority order:
    // 1. Cursor is inside an existing internal link -> swap its
    //    href, leave the text alone.
    // 2. Text is selected -> wrap with the link mark.
    // 3. No selection -> insert `displayTitle` as the link text
    //    and apply the mark.
    const { from, to } = editor.state.selection;
    if (currentInternalLinkMarker(editor)) {
      editor.chain().focus().extendMarkRange('link').setLink({ href: marker }).run();
    } else if (from !== to) {
      editor.chain().focus().setLink({ href: marker }).run();
    } else {
      editor
        .chain()
        .focus()
        .insertContent({
          type: 'text',
          text: displayTitle,
          marks: [{ type: 'link', attrs: { href: marker } }],
        })
        .run();
    }
  }

  if (internalDialog) {
    internalDialog.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="close-internal-picker"]')) {
        internalDialog.close();
        return;
      }
      const card = e.target.closest('.picker-card');
      if (!card) return;
      e.preventDefault();
      const marker = card.dataset.internalLinkMarker || '';
      const title = card.dataset.displayTitle || '';
      if (!marker) return;
      insertInternalLink(marker, title);
      internalDialog.close();
    });
    // Backdrop click closes the dialog. Same shape as the image picker.
    internalDialog.addEventListener('click', (e) => {
      if (e.target === internalDialog) internalDialog.close();
    });
  }

  // Image bubble menu — hand-rolled.
  //
  // We tried `@tiptap/extension-bubble-menu` (Tippy.js) first;
  // Tippy yanked the menu element out of the DOM in this setup
  // and never put it back. Rolling our own keeps the element
  // where the template puts it, in our control: hide by default,
  // `position: absolute` it above the focused image on
  // selectionUpdate, listen for clicks directly on the menu.
  if (imageBubbleMenu) {
    const SIZE_CLASSES = new Set(['size-small', 'size-medium', 'size-full']);
    const ALIGN_CLASSES = new Set(['align-left', 'align-center', 'align-right']);
    // Prevent the button from stealing focus on mousedown.
    // Without this, clicking a bubble-menu button blurs the
    // image node selection; `updateAttributes('image', ...)`
    // then operates on the caret's surrounding text node and
    // the image's class doesn't change.
    imageBubbleMenu.addEventListener('mousedown', (e) => {
      if (e.target.closest('button[data-size], button[data-align]')) {
        e.preventDefault();
      }
    });
    imageBubbleMenu.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-size], button[data-align]');
      if (!btn) return;
      e.preventDefault();
      const current = (editor.getAttributes('image').class || '')
        .split(/\s+/).filter(Boolean);
      const isSize = btn.dataset.size != null;
      const incoming = isSize ? btn.dataset.size : btn.dataset.align;
      const group = isSize ? SIZE_CLASSES : ALIGN_CLASSES;
      const next = current.filter((c) => !group.has(c));
      next.push(incoming);
      editor.chain().focus().updateAttributes('image', {
        class: next.join(' '),
      }).run();
    });
    // Position + active-state for the bubble menu. Fires on:
    //   - selectionUpdate (the user moved the caret onto/off an image)
    //   - update          (the active image's attrs or layout changed
    //                      — e.g. clicking S re-renders the <img> at
    //                      a smaller width, so the menu must
    //                      reposition AND the active-state buttons
    //                      must reflect the new class)
    // updateAttributes('image', ...) fires `update` but NOT
    // `selectionUpdate` (selection didn't move, just attrs
    // changed); without the update binding, the "M is still
    // highlighted after clicking S" bug returns.
    const refreshBubbleMenu = () => {
      if (!editor.isActive('image')) {
        imageBubbleMenu.hidden = true;
        return;
      }
      const selectedImg = mount.querySelector('img.ProseMirror-selectednode')
        || mount.querySelector('img');
      if (!selectedImg) {
        imageBubbleMenu.hidden = true;
        return;
      }
      // Show before measuring: a `hidden`/`display: none` element
      // returns zeros from getBoundingClientRect, so on the first
      // show the menuRect was 0×0 and the centering math was off
      // by half the (true) menu width.
      imageBubbleMenu.style.visibility = 'hidden';
      imageBubbleMenu.hidden = false;
      const rect = selectedImg.getBoundingClientRect();
      const menuRect = imageBubbleMenu.getBoundingClientRect();
      // Menu is reparented to <body> at init, so `position:
      // absolute` resolves against the initial containing block.
      // Document coords = viewport coords + scrollX/Y.
      const top = rect.top + window.scrollY - menuRect.height - 8;
      const left = rect.left + window.scrollX + (rect.width - menuRect.width) / 2;
      imageBubbleMenu.style.top = `${Math.max(8, top)}px`;
      imageBubbleMenu.style.left = `${Math.max(8, left)}px`;
      imageBubbleMenu.style.visibility = '';
      // Active-state buttons mirror the node's current class.
      const active = (editor.getAttributes('image').class || '').split(/\s+/);
      imageBubbleMenu.querySelectorAll('button').forEach((b) => {
        const slug = b.dataset.size || b.dataset.align;
        b.classList.toggle('is-active', active.includes(slug));
      });
      // Re-fire once the new src finishes loading. Clicking a
      // size button swaps the `<img src>` to a different
      // rendition URL (per ClassedImage.renderHTML); for the
      // brief window between src-changed and bytes-loaded the
      // <img> has placeholder dimensions, so the rect we
      // measured above can be wildly off (centering math
      // collapses to the Math.max(8, ...) clamp). When load
      // fires, re-measure with the real dimensions. `{once:
      // true}` so we don't stack listeners across clicks.
      if (!selectedImg.complete) {
        selectedImg.addEventListener('load', refreshBubbleMenu, { once: true });
      }
    };
    editor.on('selectionUpdate', refreshBubbleMenu);
    editor.on('update', refreshBubbleMenu);
  }

  // Belt-and-braces: even if onUpdate hasn't fired yet (rare race
  // when submitting immediately after typing), serialize one more
  // time on form submit so the textarea is fresh.
  const form = textarea.closest('form');
  if (form) {
    form.addEventListener('submit', () => {
      textarea.value = adminToPublic(editor.storage.markdown.getMarkdown());
    });
  }
}
