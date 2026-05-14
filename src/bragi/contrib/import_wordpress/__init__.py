"""WordPress eXtended RSS (WXR) XML importer.

Reads a WordPress export file (`Tools > Export` in WP admin)
and lands the posts, pages, tags, and category metadata in
bragi. HTML bodies are converted to markdown via `markdownify`;
shortcodes are stripped with a warning per unique shortcode
name. Attachments and comments are out of scope for v1 and are
counted-and-warned rather than imported.

See GitHub issue #39 for design rationale and the answers to
the open questions enumerated there.
"""
