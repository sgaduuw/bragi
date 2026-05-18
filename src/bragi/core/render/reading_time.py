"""Reading-time estimate for post chrome.

Word-count divided by an adult silent-reading-speed constant.
220 WPM is the conventional middle-of-the-road figure (Medium,
Ghost, and most blogs use 200-265). Conservative enough that
code-heavy posts read realistically without overshooting prose.

Rounded up so a 30-second snippet still shows "1 min read"
rather than "0 min read", which would be more confusing than
generous.
"""

from __future__ import annotations

import math

WORDS_PER_MINUTE = 220


def reading_time_minutes(text: str) -> int:
    """Return an integer estimated reading time in minutes.

    Empty or whitespace-only input returns 0; callers decide
    whether to render anything for the 0 case (the post template
    omits the badge when the body is empty).

    Word-count is whitespace-split: code blocks, markdown
    delimiters, and inline formatting all contribute. That's
    intentional: a fenced code block IS something the reader
    has to scan, and treating it as zero words would understate
    a code-heavy post.
    """
    if not text or not text.strip():
        return 0
    words = len(text.split())
    return max(1, math.ceil(words / WORDS_PER_MINUTE))
