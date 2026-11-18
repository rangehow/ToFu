"""Raw dangerous-command patterns, Unix and Windows.

Responsibility: the fail-closed fallback of
``command_analysis.dangerous_command_rule`` for text whose shell structure is
unknown (the parser cannot delimit it, or a wrapper's options are not
understood). Parsed commands are judged by their program words and operands
instead, so a word inside data never matches. A new rule there needs its
pattern here; keep each pattern anchored on a literal word so the scan stays
linear on long text.

Entry: DANGEROUS_PATTERNS (re-exported by ``lib.project_mod.config``).
Dependencies: none. ``command_analysis`` imports this at module level, so it
must stay a leaf: a module-level import of ``config`` there let a thread that
first-imported ``config`` (holding its import lock while the package
``__init__`` reaches ``run_command``) deadlock with a thread executing
``command_analysis``, and CPython handed ``run_command`` a partially
initialized ``command_analysis``.

NOTE: there is deliberately NO ``rm -rf /`` pattern here. Delete commands
are guarded by the argument-PARSING ``_is_catastrophic_delete`` (depth
rule: ``/`` / ``/mnt`` / any <2-component target refused, scoped deep
paths allowed — plus workspace containment for restricted principals),
which runs in every enforcement point (run_command, desktop agent,
safety pre-hook). This list used to carry the blunt regex
``\\brm\\s+-rf\\s+/``, which fired on EVERY absolute-path delete: it
hard-blocked legitimate scoped cleanup such as ``rm -rf /tmp/wt_fill``
(a temp git-worktree path an agent is expected to manage) with the
generic "matches dangerous pattern" error BEFORE the parser could allow
it — while catching nothing the parser doesn't catch better (flag-order
smuggles like ``rm /mnt -r -f``, ``sudo rm -rf /``). Do not re-add a
substring/regex delete rule here; extend the parser instead.
"""

DANGEROUS_PATTERNS = [
    # ── Unix ──
    r'\bmkfs\b',               # format disk
    r'\bdd\s+.*of=/',          # dd overwrite
    r'>\s*/dev/sd',            # overwrite block device
    r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:',  # fork bomb
    r'\bshutdown\b',
    r'\breboot\b',
    r'\binit\s+0\b',
    r'\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\s+(?:\S+\s+)?/[*.]?(?:\s|$)',  # chmod -R … /
    # ── Windows ──
    r'\bformat\s+[a-zA-Z]:',  # format C:
    r'\brd\s+/s\s+/q\b',      # rd /s /q (recursive delete)
    r'\bdel\s+/s\s+/q\b',     # del /s /q (recursive delete)
    r'\bdiskpart\b',           # disk partitioning
]
