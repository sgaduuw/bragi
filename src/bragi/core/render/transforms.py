"""Priority-ordered transform pipeline for markdown text and
rendered HTML.

Plugins register transforms via the `register_markdown_transform`
and `register_html_transform` hooks. Each registers one or more
functions with an explicit priority; the renderer applies the
pipeline in ascending priority order at render time.

Explicit priority is load-bearing: pluggy discovers plugins in an
order that is not deterministic across machines, so transform
ordering cannot rely on registration order. The convention is
priority slots 10 / 100 / 1000 for early / default / late, leaving
room to insert between.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

Transform = Callable[[str], str]


@dataclass
class _TransformEntry:
    priority: int
    name: str
    fn: Transform


@dataclass
class TransformRegistry:
    """A priority-ordered list of `(text) -> text` transforms."""

    _entries: list[_TransformEntry] = field(default_factory=list)

    def add(self, fn: Transform, *, name: str, priority: int = 100) -> None:
        """Register a transform.

        `priority` is ascending (lower runs first). Ties break in
        registration order. Use 10 / 100 / 1000 for early / default
        / late slots to leave room for inserts.
        """
        self._entries.append(_TransformEntry(priority=priority, name=name, fn=fn))
        self._entries.sort(key=lambda e: e.priority)

    def apply(self, text: str) -> str:
        """Run the pipeline; return the final transformed text."""
        for entry in self._entries:
            text = entry.fn(text)
        return text

    def names(self) -> list[str]:
        """Names of registered transforms in priority order."""
        return [e.name for e in self._entries]

    def __len__(self) -> int:
        return len(self._entries)
