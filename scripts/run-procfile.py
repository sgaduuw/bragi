#!/usr/bin/env python3
"""Minimal in-repo Procfile supervisor.

Replaces honcho for `make dev`. honcho hasn't been touched since
2018 and imports `pkg_resources`, which setuptools 81+ removed and
which Python 3.14 no longer ships by default — so the upstream
package is broken against any reasonably current Python.

Behaviour we mirror:
  - read `name: command` lines from a Procfile (default: Procfile.dev)
  - spawn each as a subprocess, stdout / stderr merged
  - prefix every output line with `[name    ]` plus a per-process
    ANSI colour cycle so the interleaved output is scannable
  - forward SIGINT / SIGTERM to every child, then wait for exit
  - exit with non-zero if any child exited non-zero

What we don't bother with: env-file loading (use `.env` directly
if needed), per-process scaling, dynamic port assignment. Add
those only when a real second caller asks for them.

Usage:
  scripts/run-procfile.py [PROCFILE]
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

# Six ANSI foreground colours, cycled per process. Skips bright
# white / black so the prefix stays readable on both light and
# dark terminals.
_COLOURS = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m", "\033[31m"]
_RESET = "\033[0m"

_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)\s*:\s*(?P<cmd>.+?)\s*$")


def parse_procfile(path: Path) -> list[tuple[str, str]]:
    """Return `[(name, command), ...]` in declaration order.

    Lines starting with `#` and blank lines are skipped, matching
    honcho's parser. Unparseable lines raise so a typo doesn't
    silently drop a process from the dev loop.
    """
    procs: list[tuple[str, str]] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match is None:
            raise SystemExit(f"{path}:{lineno}: cannot parse {raw!r}")
        procs.append((match.group("name"), match.group("cmd")))
    return procs


def stream_output(proc: subprocess.Popen[bytes], prefix: str) -> None:
    """Read `proc.stdout` line by line, write each with `prefix`.

    Runs on a dedicated thread per child so the supervisor's main
    thread is free to wait on signals + child exits.
    """
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode(errors="replace").rstrip("\n")
        # Stdout is line-buffered on terminals but pipe-buffered
        # under capture; flush after every line so the interleave
        # is timely.
        sys.stdout.write(f"{prefix}{line}\n")
        sys.stdout.flush()
    proc.stdout.close()


def main(argv: list[str]) -> int:
    procfile = Path(argv[1] if len(argv) > 1 else "Procfile.dev")
    if not procfile.exists():
        raise SystemExit(f"{procfile}: not found")

    procs = parse_procfile(procfile)
    if not procs:
        raise SystemExit(f"{procfile}: no processes declared")

    name_width = max(len(name) for name, _ in procs)
    children: list[tuple[str, subprocess.Popen[bytes]]] = []
    threads: list[threading.Thread] = []

    # Spawn in declaration order. Each child gets its own session
    # so SIGINT delivered to the supervisor propagates explicitly
    # below (not via terminal group, which would race the child's
    # own SIGINT handler).
    for index, (name, cmd) in enumerate(procs):
        colour = _COLOURS[index % len(_COLOURS)]
        prefix = f"{colour}[{name.ljust(name_width)}]{_RESET} "
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        children.append((name, proc))
        thread = threading.Thread(target=stream_output, args=(proc, prefix), daemon=True)
        thread.start()
        threads.append(thread)

    shutting_down = threading.Event()

    def shutdown(signum: int, _frame: object) -> None:
        if shutting_down.is_set():
            return  # second Ctrl-C: let the default handler kill us
        shutting_down.set()
        sys.stdout.write(f"\nrun-procfile: signal {signum}, terminating children\n")
        sys.stdout.flush()
        for _, proc in children:
            with contextlib.suppress(ProcessLookupError):  # child already exited
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Block until every child exits. A child exiting non-zero
    # *before* we asked it to means the dev loop is broken (e.g.
    # admin crashed on startup); record the unsolicited failure
    # and propagate SIGTERM to the siblings so the operator sees
    # the crash instead of one of three processes silently dead.
    # Exits *after* shutting_down is set are the expected SIGTERM
    # response and don't count as failures.
    exit_codes: dict[str, int] = {}
    unsolicited: dict[str, int] = {}
    for name, proc in children:
        rc = proc.wait()
        exit_codes[name] = rc
        if rc != 0 and not shutting_down.is_set():
            unsolicited[name] = rc
            sys.stdout.write(f"run-procfile: {name} exited {rc}; shutting down siblings\n")
            sys.stdout.flush()
            shutdown(signal.SIGTERM, None)

    # Drain stdout reader threads (each exits when the pipe closes).
    for thread in threads:
        thread.join(timeout=2.0)

    # Return non-zero only for unsolicited failures. Operator-
    # initiated shutdowns (Ctrl-C / external SIGTERM) where every
    # child responded to the forwarded SIGTERM are a clean exit.
    if unsolicited:
        return next(iter(unsolicited.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
