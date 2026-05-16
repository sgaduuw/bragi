"""HTML transforms for the embeds plugin.

Single concern today: ensure each page that contains a
click-to-load YouTube embed carries the small delegated click
handler that swaps the placeholder for the nocookie iframe. The
handler is shared across all embeds on the page; injecting it
once at HTML-transform time avoids duplicating the script per
embed (a post with N embeds otherwise ships N copies).
"""

from __future__ import annotations

# Delegated click handler. Targets any `<button data-embed-action="load">`
# inside a `.bragi-embed--youtube-cto` wrapper; on click, replaces
# the wrapper's contents with the nocookie iframe carrying the
# captured `data-video-id`. The handler is registered once via
# event delegation, so it survives htmx partial swaps too.
_YT_ALLOW = (
    "accelerometer; autoplay; clipboard-write; encrypted-media; "
    "gyroscope; picture-in-picture; web-share"
)
_YOUTUBE_CTO_SCRIPT = (
    "<script>"
    "document.addEventListener('click',function(e){"
    "var b=e.target.closest('.bragi-embed--youtube-cto button[data-embed-action=\"load\"]');"
    "if(!b)return;"
    "e.preventDefault();"
    "var w=b.closest('.bragi-embed--youtube-cto');"
    "var id=w&&w.getAttribute('data-video-id');"
    "if(!id)return;"
    "var f=document.createElement('iframe');"
    "f.src='https://www.youtube-nocookie.com/embed/'+id+'?autoplay=1';"
    "f.setAttribute('title','YouTube video');"
    f"f.setAttribute('allow','{_YT_ALLOW}');"
    "f.setAttribute('allowfullscreen','');"
    "w.innerHTML='';"
    "w.appendChild(f);"
    "});"
    "</script>"
)

_MARKER = "bragi-embed--youtube-cto"


def inject_youtube_cto_script(rendered_html: str) -> str:
    """Append the click-to-load handler iff the page contains a CTO
    wrapper. No-op for pages without YouTube embeds, and harmless
    for pages with only iframe-mode YouTube embeds (no CTO marker
    -> no script)."""
    if _MARKER not in rendered_html:
        return rendered_html
    return rendered_html + _YOUTUBE_CTO_SCRIPT
