"""Path containment policy for ``/cd`` (SB2) — pure, dependency-light, testable.

The owner can change the bot's working directory with ``/cd <path>``; that path is
operator-supplied input, so it is a confinement boundary (SB2). This module is the
single place that decides whether a requested directory is *allowed*: it canonicalizes
the request (expanding ``~``, resolving the path against the current working directory,
and following BOTH symlinks AND ``..`` via :meth:`Path.resolve`) and then requires the
canonical target to sit inside at least one canonicalized allowed root — unless the
owner has explicitly opted out with ``ALLOW_ANY_PATH=true``.

It is deliberately **pure**: no telegram, no engine, no I/O beyond the filesystem
canonicalization ``Path.resolve`` performs. ``set_cwd`` (in ``claude_runner``) owns the
*existence* / is-a-directory check; SB2 owns only *is this path permitted*. Keeping the
two concerns separate keeps this resolver unit-testable in isolation and means the
symlink-escape guard is exercised without standing up a bot or an engine.

Design (locked — see the T8 brief / progress.md SB2): confinement is **ON by default**.
When ``ALLOWED_ROOTS`` is unset the single default allowed root is the workdir (itself
defaulting to ``$HOME``); the owner widens via ``ALLOWED_ROOTS`` or disables entirely
via ``ALLOW_ANY_PATH=true``. An empty allowed-roots tuple with ``allow_any=False``
rejects everything — fail-closed (SB6).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


class PathNotAllowed(Exception):
    """Raised when a requested ``/cd`` target falls outside every allowed root (SB2).

    The canonical (symlink- and ``..``-resolved) target was not equal to, nor a
    descendant of, any canonicalized allowed root, and ``ALLOW_ANY_PATH`` was not set.
    The bot turns this into a clear refusal and does NOT touch the runner.
    """


def resolve_within_roots(
    arg: str,
    *,
    cwd: str | Path,
    allowed_roots: Iterable[str | Path],
    allow_any: bool,
) -> Path:
    """Canonicalize ``arg`` and confine it to ``allowed_roots`` (SB2).

    Steps:

    1. **Canonicalize.** Expand a leading ``~``, resolve a (possibly relative) ``arg``
       against ``cwd``, then :meth:`Path.resolve` it. ``resolve`` follows BOTH symlinks
       and ``..`` segments, so this is the step that defeats a ``../../etc`` traversal
       *and* a symlink that points outside a root. ``strict=False`` so a not-yet-existent
       path still canonicalizes — existence/is-dir is the caller's concern (``set_cwd``),
       not SB2's.
    2. **Opt-out.** If ``allow_any`` is True, return the canonical path with NO
       containment check (the explicit ``ALLOW_ANY_PATH=true`` escape hatch).
    3. **Confine.** Otherwise require the canonical target to be equal to, or a
       descendant of, at least one canonicalized root (:meth:`Path.is_relative_to`). If
       it is not — or ``allowed_roots`` is empty — raise :class:`PathNotAllowed`
       (fail-closed: empty roots + ``allow_any=False`` rejects everything, SB6).

    Returns the canonical :class:`Path` (the bot passes ``str(target)`` to the runner so
    the stored cwd is always the resolved, contained path — never the raw argument).
    """
    base = Path(cwd)
    try:
        requested = Path(arg).expanduser()
        if not requested.is_absolute():
            requested = base / requested
        # resolve() canonicalizes symlinks AND ".." (strict=False: a missing path is
        # fine; existence is set_cwd's job, not SB2's). THIS is the load-bearing
        # canonicalization.
        target = requested.resolve(strict=False)
    except (ValueError, OSError) as exc:
        # A path the OS itself rejects as malformed (e.g. an embedded NUL byte raises
        # ValueError from realpath) can never be a permitted directory. Fail CLOSED
        # (SB6) — never crash the handler (RB1). The explicit opt-out below is still
        # honored separately: a malformed arg has no valid canonical form to return.
        raise PathNotAllowed(f"unresolvable path {arg!r}: {exc}") from exc

    if allow_any:
        return target

    # Canonicalize each root the same way so a root given with a symlink/trailing "/.."
    # or as "~" is compared on equal footing with the canonical target.
    for root in allowed_roots:
        canonical_root = Path(root).expanduser().resolve(strict=False)
        if target == canonical_root or target.is_relative_to(canonical_root):
            return target

    raise PathNotAllowed(str(target))


__all__ = ["PathNotAllowed", "resolve_within_roots"]
