"""A conservative **Bash command policy** — the P6/C2-residual guardrail (P13 T-BASH).

A **pure**, import-clean module (no telegram, no SDK, no engine — mirrors
:mod:`claude_tg.permissions`'s isolation) that classifies a raw shell command against a
small, conservative built-in **denylist** of genuinely-dangerous shapes. It is the
guardrail the engine layers ADDITIVELY on top of the existing approval gate so the one
documented-UNCONFINED tool (``Bash`` — see ``docs/features/p6-security-audit/findings.md``
C2: "an arbitrary shell command has no reliable static target, so ``Bash`` is deliberately
NOT path-parsed") cannot silently ``rm -rf /`` / force-push / ``curl … | sh`` under a
prior session-grant or ``/yolo``.

**What this is — and is NOT.** This is a **pattern guardrail on the approval UX**, not a
sandbox. It is substring/regex matching on the **RAW** command string; it explicitly does
NOT parse the shell and does NOT attempt to defeat obfuscation (base64-decode-then-eval,
``$IFS`` tricks, variable indirection). It raises the floor against *accidental* and
*obvious* destruction; it is **not** a defense against a determined adversarial Claude. The
honest C2 boundary (Bash is not statically confinable) **still stands**.

**Conservative = few false positives (the load-bearing design bar).** A heuristic denylist
that fires on a benign ``rm -rf ./build`` would train the operator to disable the policy
wholesale, which is *worse* for safety than a loud prompt. So every pattern is written to
match only genuinely-dangerous shapes: ``rm -rf /`` matches, ``rm -rf ./build`` does NOT;
``curl … | sh`` matches, a bare ``curl -O url`` does NOT. The match/no-match table in the
tests pins each lookalike.

**Fail-closed contract (enforced by the CALLER, documented here).** :func:`classify_bash`
is written never to raise on a ``str`` input, but the engine treats *any* exception from it
as a **hit** (escalate in flag mode / deny in strict mode) — never a silent allow. A policy
bug must err toward friction, never toward exposure (the ADR-003 / SB6 invariant). This
module's job is only to return the match (or ``None``); the escalate/deny decision is the
engine's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

#: Severity of a denylist match. ``high`` = destructive / remote-code-exec / irreversible
#: (``rm -rf /``, ``curl|sh``, ``mkfs``, fork bomb); ``medium`` = dangerous-but-recoverable
#: or a read of a secret store (``chmod -R 777``, force-push, ``cat ~/.ssh/id_*``). Severity
#: is carried for the render/audit label only — BOTH severities flag/deny identically (the
#: policy does not currently branch on it; it is informational, body-free metadata).
BashSeverity = Literal["high", "medium"]


@dataclass(frozen=True)
class BashPolicyMatch:
    """One denylist match — a body-free label + severity, NEVER the raw command.

    :attr:`pattern` is the stable rule id (e.g. ``"root-rm"``); :attr:`label` is the short
    human line the prompt/audit shows (e.g. ``"recursive delete of a root path"``);
    :attr:`severity` is :data:`BashSeverity`. None of these fields carry the command body —
    the command text the operator sees comes from the gate's existing
    :func:`~claude_tg.engine.types.safe_input_summary` (truncated to 160 chars), not from
    here — so a :class:`BashPolicyMatch` is body-free by construction (SB3).
    """

    pattern: str
    label: str
    severity: BashSeverity


# ---------------------------------------------------------------------------
# The built-in denylist — conservative, high-signal destructive shapes.
# ---------------------------------------------------------------------------
#
# Each rule is (pattern_id, label, severity, compiled_regex). The regexes are written to
# be CONSERVATIVE: they target genuinely-dangerous shapes and are guarded against the
# common benign lookalike (a relative ./path, a bare download, --force-with-lease, …).
# Matching is on the RAW command string (the caller passes tool_input["command"] verbatim,
# NOT the 160-char-truncated summary), case-insensitive where that doesn't widen the match
# unsafely. Comments cite the benign command each rule must NOT catch.

# An `rm` with BOTH a recursive flag (-r / -R / --recursive, incl. bundled -rf / -fr / -rfv)
# AND a force flag (-f / --force) somewhere in the command, AND a dangerous TARGET. The two
# flag groups + the target are asserted by independent ANCHORED-AT-rm lookaheads (zero-width,
# so order between flags and target doesn't matter), each scanning the rest of the command
# (`[^\n|;&]*` = the rm's own argument list, not a later piped/chained command). Reused notion
# of "recursive force rm" so the rule reads as one intent.
#
# A dangerous rm TARGET is: filesystem root `/` (alone / `/ ` / `/*`), the home dir `~`
# (alone / `~/` / `~/*`), `$HOME` (alone / `$HOME/` / `$HOME/*`), a root glob `/*`, or the
# --no-preserve-root override. A RELATIVE target (`./build`, `build/`, `/tmp/foo`,
# `~/proj/node_modules`) must NOT match — only a WHOLE root / home / root-glob is dangerous.
_RM_RECURSIVE = r"(?=[^\n|;&]*?\s-{1,2}[A-Za-z-]*(?:r|-recursive)[A-Za-z-]*\b)"
_RM_FORCE = r"(?=[^\n|;&]*?\s-{1,2}[A-Za-z-]*(?:f|-force)[A-Za-z-]*\b)"
_RM_DANGEROUS_TARGET = (
    r"(?=[^\n|;&]*?(?:"
    r"--no-preserve-root"  # explicit "yes, delete /" override
    r"|\s/(?:\s|$|\*)"  # a bare `/` arg: `rm -rf /`, `rm -rf / `, `rm -rf /*`
    r"|\s~(?:/\*?)?(?:\s|$)"  # `rm -rf ~`, `rm -rf ~/`, `rm -rf ~/*` (whole-home)
    r"|\s\$HOME(?:/\*?)?(?:\s|$)"  # `rm -rf $HOME`, `$HOME/`, `$HOME/*`
    r"))"
)

_BUILTIN_RULES: list[tuple[str, str, BashSeverity, re.Pattern[str]]] = [
    (
        "root-rm",
        "recursive force-delete of / , ~ , or $HOME",
        "high",
        re.compile(
            r"\brm\b" + _RM_RECURSIVE + _RM_FORCE + _RM_DANGEROUS_TARGET, re.IGNORECASE
        ),
    ),
    (
        "pipe-to-shell",
        "download piped straight into a shell (remote code execution)",
        "high",
        # curl/wget/fetch … whose output is piped into sh/bash/zsh/dash (with optional
        # flags like `| sudo bash -s`). A bare `curl -O url` (no pipe-to-shell) does NOT
        # match. Requires a downloader on the left of the pipe so `echo hi | sh` of a
        # local string is out of scope (we target REMOTE code-exec specifically).
        re.compile(
            r"\b(?:curl|wget|fetch)\b[^|]*\|\s*(?:sudo\s+)?(?:[A-Za-z0-9_/]*/)?"
            r"(?:sh|bash|zsh|dash|ksh)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "force-push",
        "git force-push (can overwrite remote history)",
        "medium",
        # `git push … --force` or `… -f`. CONSERVATIVE NOTE: we deliberately flag BOTH
        # --force and --force-with-lease (the design's stated conservative default —
        # re-confirm both; a lease push is safer but still rewrites a remote ref). The
        # `(?!-with-lease)` is NOT applied — a lease push still matches and re-prompts.
        re.compile(
            r"\bgit\b[^\n]*\bpush\b[^\n]*(?:--force\b|--force-with-lease\b|\s-f\b|\s-[A-Za-z]*f[A-Za-z]*\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "mkfs",
        "filesystem format (mkfs — destroys a device)",
        "high",
        re.compile(r"\bmkfs(?:\.[A-Za-z0-9]+)?\b", re.IGNORECASE),
    ),
    (
        "dd-to-device",
        "dd writing to a raw device (overwrites a disk)",
        "high",
        # `dd … of=/dev/sda` — a dd whose OUTPUT is a /dev node. `dd of=./out.img` (a file)
        # does NOT match — only of=/dev/* is the disk-destroying shape.
        re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.IGNORECASE),
    ),
    (
        "redirect-to-device",
        "redirect overwriting a raw disk device (> /dev/sd…)",
        "high",
        # `> /dev/sda` / `> /dev/nvme0n1` — a redirect onto a block device. Excludes the
        # benign char devices a script legitimately writes to: /dev/null, /dev/stdout,
        # /dev/stderr, /dev/tty, /dev/fd/*, /dev/zero, /dev/random, /dev/urandom.
        re.compile(
            r">\s*/dev/(?!null|stdout|stderr|tty|fd/|zero|random|urandom)\w",
            re.IGNORECASE,
        ),
    ),
    (
        "fork-bomb",
        "fork bomb (exhausts process table)",
        "high",
        # The classic `:(){ :|:& };:` and its whitespace variants. Match the function
        # definition that recursively pipes itself into the background — the load-bearing
        # `:|:&` (or `:| :&`) shape. Conservative: a normal pipeline never looks like this.
        re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:\s*&[^}]*\}\s*;\s*:"),
    ),
    (
        "chmod-777-recursive",
        "world-writable recursive chmod (chmod -R 777)",
        "medium",
        # `chmod -R 777` / `chmod -R a+rwx` / `chmod 777 /` (recursive world-writable, or
        # 777 on root). A plain `chmod 755 file` or `chmod +x script.sh` does NOT match.
        re.compile(
            r"\bchmod\b(?:"
            r"[^\n]*-{1,2}[A-Za-z-]*R[A-Za-z-]*\b[^\n]*\b(?:777|a=?\+?rwx)\b"  # recursive 777
            r"|[^\n]*\b777\b\s+/(?:\s|$)"  # 777 on the bare root
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "chown-recursive-root",
        "recursive chown on a system root (chown -R … /)",
        "medium",
        # `chown -R user /` / `chown -R user:grp /etc` — a recursive ownership change rooted
        # at a SYSTEM path. A `chown -R me ./project` (relative) does NOT match.
        re.compile(
            r"\bchown\b[^\n]*-{1,2}[A-Za-z-]*R[A-Za-z-]*\b[^\n]*\s/(?:etc|usr|bin|var|boot|lib|sys|opt|sbin|System|Library)?\b(?:/|\s|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "secret-read",
        "read of a secret store (ssh key / cloud creds / .env)",
        "medium",
        # A read (cat/less/more/head/tail/xxd/od/strings/cp/scp/base64) of an obvious secret
        # store: an ssh PRIVATE key (~/.ssh/id_*), cloud credentials (~/.aws/credentials,
        # gcloud, ~/.kube/config), or a .env file. Lower severity (reading is less
        # destructive than rm) but still flagged so an exfil-shaped read re-confirms. A read
        # of ~/.ssh/known_hosts or id_rsa.pub is NOT a private key — id_*.pub is excluded.
        re.compile(
            r"\b(?:cat|less|more|head|tail|xxd|od|strings|cp|scp|base64|nl|tac)\b[^\n]*"
            r"(?:"
            r"(?:\.ssh/|/\.ssh/)id_(?!\w*\.pub\b)\w+"  # ssh private key (not .pub)
            r"|\.aws/credentials\b"  # AWS creds
            r"|\.config/gcloud/[^\n]*credential"  # gcloud creds
            r"|\.kube/config\b"  # kube config
            r"|(?:^|[\s/])\.env(?:\.[\w.-]+)?\b"  # a .env / .env.local file
            r")",
            re.IGNORECASE,
        ),
    ),
]


def _compile_extra(extra_patterns: Sequence[str]) -> list[tuple[str, str, BashSeverity, re.Pattern[str]]]:
    """Compile owner-supplied extra denylist patterns into rules (best-effort, additive).

    Each non-empty, stripped string in ``extra_patterns`` becomes an additional rule with
    a generic ``"custom"`` pattern id + label and ``medium`` severity, ADDITIVE to the
    built-ins (the built-ins cannot be removed via config — removing a safety pattern must
    be a code change, fail-safe). A pattern that fails to compile is **skipped** (logged by
    the caller via the value error it would otherwise raise — here we simply drop it so one
    bad env entry can't wedge the whole policy); an empty sequence yields no extra rules.
    The pattern is matched case-insensitively, consistent with the built-ins.
    """
    rules: list[tuple[str, str, BashSeverity, re.Pattern[str]]] = []
    for raw in extra_patterns:
        pat = raw.strip()
        if not pat:
            continue
        try:
            compiled = re.compile(pat, re.IGNORECASE)
        except re.error:
            # A malformed custom pattern is dropped (fail-safe — never crash the gate). The
            # built-in rules still apply; the owner sees nothing run for this bad entry.
            continue
        rules.append(("custom", "matched a custom denylist pattern", "medium", compiled))
    return rules


def classify_bash(
    command: str,
    *,
    extra_patterns: Sequence[str] = (),
) -> BashPolicyMatch | None:
    """Classify a raw Bash ``command`` against the denylist — return the match or ``None``.

    **Pure.** Scans the RAW command string (the caller MUST pass ``tool_input["command"]``
    verbatim — NOT the 160-char-truncated summary, or a long dangerous command could slip
    past the truncation). Returns the FIRST matching :class:`BashPolicyMatch` (built-ins are
    checked in declaration order — most-destructive first — then any ``extra_patterns``), or
    ``None`` when nothing matches. Conservative by design: a benign ``rm -rf ./build`` /
    ``curl -O url`` / ``chmod +x f`` returns ``None`` (the match/no-match table pins this).

    A non-``str`` / empty / whitespace-only ``command`` returns ``None`` (nothing to match —
    a Bash request with no command can't be dangerous, and there's nothing to scan). This
    function is written never to raise on a ``str``; the **caller treats any exception as a
    hit** (fail-closed — see the module docstring + :meth:`Engine.on_tool_request`).
    """
    if not isinstance(command, str):
        return None
    if not command.strip():
        return None
    for pattern_id, label, severity, regex in _BUILTIN_RULES:
        if regex.search(command):
            return BashPolicyMatch(pattern=pattern_id, label=label, severity=severity)
    for pattern_id, label, severity, regex in _compile_extra(extra_patterns):
        if regex.search(command):
            return BashPolicyMatch(pattern=pattern_id, label=label, severity=severity)
    return None


def builtin_pattern_ids() -> tuple[str, ...]:
    """Return the built-in rule ids (for tests / introspection) — stable, ordered."""
    return tuple(rule[0] for rule in _BUILTIN_RULES)


__all__ = [
    "BashSeverity",
    "BashPolicyMatch",
    "classify_bash",
    "builtin_pattern_ids",
]
