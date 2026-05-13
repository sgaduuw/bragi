"""Markdown and HTML rendering subsystem.

Planned modules:
    markdown.py    markdown-it-py setup, body parsing pipeline
    transforms.py  TransformRegistry for plugin-contributed
                   markdown pre/post-parse transforms and post-render
                   HTML transforms (priority-ordered)

The renderer at runtime:
    body_markdown -> register_markdown_transform pipeline ->
    markdown-it-py -> register_html_transform pipeline -> body_html
"""
