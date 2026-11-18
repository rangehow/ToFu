"""Pure command-string / command-output analysis.

Extracted from ``lib/project_mod/run_command.py`` (2026-07-11) to isolate the
side-effect-free analysis layer from the subprocess-execution machinery. Every
function here is PURE: it inspects a command string or captured output text and
returns a verdict (dangerous? destructive? which files does it write? which
top-level delete is catastrophic?) or a cleaned/compressed copy of output. None
of them spawn a process, read process state, or mutate the filesystem — the
filesystem touches are read-only: the ``os.path.realpath`` workspace-containment
check and the ``.git`` marker discovery of the repository-metadata anchor inside
:func:`_is_catastrophic_delete`.

``run_command.py`` re-imports every public symbol from here, so both existing
import paths keep working unchanged::

    from lib.project_mod.run_command import _is_destructive_command  # still works
    from lib.project_mod.tools import _is_catastrophic_delete        # still works

There is no dependency back on ``run_command.py`` — the execution layer depends
on this module, not the reverse.
"""

import fnmatch
import os
import re
import shlex
from collections import Counter, namedtuple
from glob import glob as _glob

from lib.log import get_logger
from lib.project_mod.command_structure import (
    Compound,
    ItemRun,
    ShellStructureUnknown,
    SimpleCommand,
    Subshell,
    control_structure,
    parse_shell_structure,
)
from lib.project_mod.dangerous_patterns import DANGEROUS_PATTERNS

logger = get_logger(__name__)

# Alias kept for parity with run_command.py's historical ``import re as _re``
# usage inside the analysis functions moved here.
_re = re


# ═══════════════════════════════════════════════════════
#  Command output cleanup for LLM consumption
# ═══════════════════════════════════════════════════════

# ANSI escape codes: SGR (colors), cursor movement, OSC (window titles)
_ANSI_ESC_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-B012]')

# tqdm-style progress bar: "Label: NN%|bar_chars| X/Y [timing, rate]"
# The content inside [...] varies widely (ETA, rate, ?it/s) — match any non-]
_PROGRESS_RE = re.compile(
    r'^(.*?)\s*\d+%\|[^|]*\|\s*\d+/\d+\s*\[[^\]]*\](.*)$'
)

# Pre-compiled regex for run_command
_FS_HEAVY_RE = re.compile(r'\b(du|find|locate|tree|wc\s+-|cloc|sloccount|ncdu|fd)\b')


def _mask_quoted_literals(command):
    """Return *command* with the CONTENTS of quoted spans blanked to spaces.

    Delimiters are blanked too, so token boundaries and the length survive;
    unquoted text, backslash escapes included, is kept verbatim. Shape checks
    use it to find unquoted operators (``<``, ``(``, ``;`` …) without tripping
    over quoted arguments. It is not a command parser: it knows nothing of
    heredoc bodies, substitutions inside double quotes or text a shell
    executes (``sh -c '…'``); :mod:`lib.project_mod.command_structure` owns
    that structure.
    """
    out = []
    in_single = in_double = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == '\\' and not in_single and i + 1 < n:
            # Escape: keep both chars when unquoted (preserve structure),
            # blank them when inside a double-quoted span.
            out.append('  ' if in_double else command[i:i + 2])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(' ')
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(' ')
        elif in_single or in_double:
            out.append(' ')
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def _extract_progress_label(line):
    """Extract the label prefix from a tqdm-style progress bar line.

    Returns the stripped label string if this is a *pure* progress bar line
    (no significant content after the bar), or None otherwise.
    Lines with substantial trailing content (e.g. "[Worker 3] Starting …")
    are NOT treated as progress bars — they go through Phase 4 dedup instead.
    """
    m = _PROGRESS_RE.match(line)
    if not m:
        return None
    label = m.group(1).strip()
    trailing = m.group(2).strip()
    # If there's significant content after the progress bar, this is an
    # "announcement" line (e.g. worker startup) — don't treat as progress bar
    if len(trailing) > 20:
        return None
    return label


# ── Device / worker detection for multi-GPU annotation ──────────
# ⚠️ NO PRODUCTION CALLER. This permissive union (accelerator words AND plain
# numbered workers in one class) is retained only for the exported-symbol
# contract asserted by tests/project_mod/test_command_analysis_extraction.py. Because it
# cannot distinguish "cuda:0" from "io worker 0", using it to label output is
# exactly how the folder came to report three postgres processes as three CUDA
# devices. NEW CODE MUST USE _extract_accelerator_ids (hardware claims) or
# _extract_ordinal_ids (index-only claims) — never this regex.
_DEVICE_RE = re.compile(
    r'(?:cuda|gpu|device|rank|worker)[\s:_]*(\d+)', re.IGNORECASE
)

# Only these words are EVIDENCE of a real compute accelerator, and only
# they may licence a ``cuda:`` prefix in the fold marker. ``worker``/``rank``
# deliberately excluded: real ``ps aux`` output contains lines like
# "postgres: io worker 0/1/2", which the old union regex turned into
# "×3 devices on cuda:0-2" — three database processes reported as three
# GPUs. A fold marker that invents hardware is worse than no marker at all,
# so accelerator attribution now requires an accelerator word.
_ACCEL_RE = re.compile(r'(?:cuda|gpu|nvidia|hip|rocm|xpu)[\s:_]*(\d+)',
                       re.IGNORECASE)

# Numbered-variant words: enough to say "these lines differ by an index",
# NOT enough to name the hardware.
_ORDINAL_RE = re.compile(r'(?:device|rank|worker|shard|replica)[\s:_]*(\d+)',
                         re.IGNORECASE)


def _extract_progress_pct(line):
    """Extract percentage from a tqdm-style progress bar line.

    Returns an integer 0-100, or None if not a progress bar.
    """
    m = re.search(r'(\d+)%\|', line)
    return int(m.group(1)) if m else None


def _extract_device_ids(lines):
    """Extract unique device/worker IDs from a group of lines.

    Looks for patterns like cuda:0, GPU 3, Worker 5, rank 2.
    Returns sorted list of unique integer IDs, or empty list.

    ⚠️ NO PRODUCTION CALLER — kept for the exported-symbol contract only.
    This is the permissive union (accelerators AND plain numbered workers), so
    it does NOT establish that a GPU is involved. New code must use
    :func:`_extract_accelerator_ids` for anything that renders a ``cuda:``
    label, or :func:`_extract_ordinal_ids` for index-only claims; reusing this
    one is how the fold marker started claiming hardware that isn't there.
    """
    ids = set()
    for ln in lines:
        for m in _DEVICE_RE.finditer(ln):
            ids.add(int(m.group(1)))
    return sorted(ids)


def _extract_accelerator_ids(lines):
    """Extract IDs backed by EXPLICIT accelerator evidence (cuda/gpu/…).

    Returns sorted unique ints, or [] when the lines carry no accelerator
    word — in which case the caller must not emit a ``cuda:`` label.
    """
    ids = set()
    for ln in lines:
        for m in _ACCEL_RE.finditer(ln):
            ids.add(int(m.group(1)))
    return sorted(ids)


def _extract_ordinal_ids(lines):
    """Extract IDs from plain numbered-variant words (worker/rank/shard/…).

    Used to say "N numbered variants" WITHOUT naming any hardware.
    """
    ids = set()
    for ln in lines:
        for m in _ORDINAL_RE.finditer(ln):
            ids.add(int(m.group(1)))
    return sorted(ids)


def _format_cuda_device_range(ids):
    """Format CUDA device IDs as a compact range string with ``cuda:`` prefix.

    Examples:
        [0,1,2,3,4,5,6,7] → 'cuda:0-7'
        [0,2,5] → 'cuda:0,2,5'
        [3] → 'cuda:3'

    See ``lib/log_clean.py::_format_device_range`` for the un-prefixed
    variant used by the log-clean banner.
    """
    if not ids:
        return ''
    if len(ids) == 1:
        return f'cuda:{ids[0]}'
    # Contiguous range?
    if ids[-1] - ids[0] + 1 == len(ids):
        return f'cuda:{ids[0]}-{ids[-1]}'
    return 'cuda:' + ','.join(str(i) for i in ids)


def _line_fingerprint(line):
    """Structural fingerprint: replace digit sequences with '#'.

    Lines that differ only in numeric values (device IDs, counts, timings)
    produce the same fingerprint and can be collapsed.
    Returns None for short/empty lines to prevent false grouping.
    """
    s = line.strip()
    if len(s) < 20:
        return None
    return re.sub(r'\d+', '#', s)


def _clean_command_output(output):
    """Clean command output for efficient LLM consumption.

    Phases:
      1. Strip ANSI escape codes (colors, cursor control)
      2. Resolve ``\\r`` carriage-return overwrites (keep final visible text)
      3. Compress tqdm-style progress bar groups → first + ~50% + last
      4. Collapse multi-device / repeated lines → first + count

    This drastically reduces token waste from training logs, data pipelines,
    and multi-GPU workloads without losing meaningful information.
    """
    if not output or len(output) < 200:
        return output

    original_len = len(output)

    # ── Phase 1: Strip ANSI escape codes ──────────────────────────────
    output = _ANSI_ESC_RE.sub('', output)

    # ── Phase 2: Resolve \\r carriage-return overwrites ───────────────
    # tqdm and similar tools write "\\r" to rewind the cursor and overwrite
    # the line.  In captured (non-TTY) output, all intermediate states are
    # visible — keep only the final non-empty segment per line.
    raw_lines = output.split('\n')
    lines = []
    for raw in raw_lines:
        if '\r' in raw:
            parts = raw.split('\r')
            visible = ''
            for p in parts:
                if p.strip():
                    visible = p
            lines.append(visible if visible else '')
        else:
            lines.append(raw)

    # ── Phase 3 & 4: Group and compress consecutive similar lines ─────
    # Helper: collect a run of lines matching a predicate, skipping blanks
    def _collect_group(start, match_fn):
        """Collect consecutive lines matching match_fn, skipping blank separators.

        Returns (group_of_content_lines, next_index_after_group).
        """
        grp = [lines[start]]
        j = start + 1
        while j < len(lines):
            if not lines[j].strip():
                # Blank line — peek ahead to see if next content line
                # still belongs to the group
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and match_fn(lines[k]):
                    # The blank(s) separate members of the same group —
                    # skip them and continue collecting
                    j = k
                    grp.append(lines[j])
                    j += 1
                else:
                    break
            elif match_fn(lines[j]):
                grp.append(lines[j])
                j += 1
            else:
                break
        return grp, j

    result = []
    i = 0
    total_compressed = 0

    while i < len(lines):
        line = lines[i]

        # Skip blank lines that separate groups (will be re-added as needed)
        if not line.strip():
            result.append(line)
            i += 1
            continue

        # ── Phase 3: Progress bar compression ─────────────────────────
        pb_label = _extract_progress_label(line)
        if pb_label is not None:
            group, j = _collect_group(
                i, lambda ln: _extract_progress_label(ln) == pb_label)

            n = len(group)
            if n <= 3:
                result.extend(group)
            else:
                # ── Percentage-aware sampling + concurrency detection ──
                pcts = [(_extract_progress_pct(g), g) for g in group]
                valid = [(p, g) for p, g in pcts if p is not None]

                # How many lines share a percentage tells us the output is
                # CONCURRENT — it does NOT tell us what the workers are. The
                # old code called every such group "×N devices", so four tqdm
                # bars from a single-process data loader were reported as four
                # devices. Same rule as the Phase 4 marker: name hardware only
                # with an explicit accelerator word, otherwise describe the
                # shape ('parallel streams') and claim nothing about what runs
                # them.
                stream_count = 1
                if valid:
                    pct_freq = Counter(p for p, _ in valid)
                    stream_count = max(pct_freq.values())
                accel_ids = _extract_accelerator_ids(group)
                if len(accel_ids) > 1:
                    device_note = (f', ×{len(accel_ids)} devices on '
                                   f'{_format_cuda_device_range(accel_ids)}')
                elif stream_count > 1:
                    device_note = f', ×{stream_count} parallel streams'
                else:
                    device_note = ''

                if valid:
                    # Pick lines by percentage: start / mid / end
                    by_pct = sorted(valid, key=lambda x: x[0])
                    first_line = by_pct[0][1]
                    last_line  = by_pct[-1][1]
                    pct_mid = (by_pct[0][0] + by_pct[-1][0]) // 2
                    mid_entry = min(valid,
                                    key=lambda x: abs(x[0] - pct_mid))
                    mid_line = mid_entry[1]

                    result.append(first_line)
                    has_mid = (mid_line != first_line
                               and mid_line != last_line)
                    skipped = n - (3 if has_mid else 2)
                    result.append(
                        f'  … ({skipped} more progress updates'
                        f'{device_note}) …')
                    if has_mid:
                        result.append(mid_line)
                    result.append(last_line)
                    total_compressed += skipped
                else:
                    # Fallback: positional sampling
                    result.append(group[0])
                    result.append(
                        f'  … ({n - 2} more progress updates'
                        f'{device_note}) …')
                    result.append(group[-1])
                    total_compressed += n - 2
            i = j
            continue

        # ── Phase 4: Multi-device / repeated line collapse ────────────
        fp = _line_fingerprint(line)
        if fp is not None:
            group, j = _collect_group(
                i, lambda ln: _line_fingerprint(ln) == fp)

            n = len(group)
            if n <= 2:
                result.extend(group)
            else:
                result.append(group[0])
                # Attribution is gated on EVIDENCE, not on "some word had a
                # number after it". Three tiers, narrowest first:
                #   1. explicit accelerator word → may say cuda:N-M
                #   2. plain numbered variant (worker/rank/…) → count only
                #   3. neither → state the grouping rule, claim nothing
                # Tier 1 vs 2 matters: real `ps aux` has "postgres: io
                # worker 0/1/2", and calling that three CUDA devices is an
                # invented fact, not a lossy summary.
                accel_ids = _extract_accelerator_ids(group)
                ord_ids = _extract_ordinal_ids(group)
                if len(accel_ids) > 1:
                    dev_range = _format_cuda_device_range(accel_ids)
                    result.append(
                        f'  … (+{n - 1} lines folded, '
                        f'×{len(accel_ids)} devices on {dev_range}) …')
                elif len(ord_ids) > 1:
                    result.append(
                        f'  … (+{n - 1} lines folded, '
                        f'{len(ord_ids)} numbered variants) …')
                else:
                    # NOT "similar lines": these lines share a structural
                    # fingerprint (digit runs normalised to #) and their
                    # non-numeric text may differ completely.
                    result.append(
                        f'  … (+{n - 1} lines folded: same structure, '
                        f'differing values) …')
                total_compressed += n - 1
            i = j
            continue

        result.append(line)
        i += 1

    cleaned = '\n'.join(result)
    # The fold total must reach the MODEL, not just the log. Without it a
    # reader cannot tell whether they are looking at the whole output or a
    # fraction of it — each marker states its own group, but nothing states
    # the aggregate. Only emitted when folding actually happened, so
    # untouched output stays byte-identical.
    if total_compressed > 0:
        kept = cleaned.count('\n') + 1
        cleaned += (
            f'\n[output folded: {total_compressed} of '
            f'{len(lines)} lines omitted, {kept} shown — consecutive lines '
            f'sharing a structural fingerprint were grouped]'
        )
    if total_compressed > 5:
        logger.debug('_clean_command_output: compressed %d repetitive lines '
                     '(%d → %d chars)', total_compressed, original_len,
                     len(cleaned))
    return cleaned


def _independent_runtime_command(command):
    """Plain runtime commands need no checkout snapshot or writer exclusion.

    This is scheduling policy, not proof that an interpreter cannot write.
    Explicit write targets are checked separately before applying this policy.
    Complex shell grammar and embedded filter/Git programs keep the existing
    conservative lease path; ordinary test/build jobs run on live files.
    """
    if (re.search(r'[$`]', command)
            or re.search(r'[\r\n#<>(){}]', _mask_quoted_literals(command))):
        return False
    independent = {
        'pytest', 'py.test', 'python', 'python3', 'npm', 'npx', 'pnpm', 'yarn',
        'make', 'cmake', 'ninja', 'cargo', 'rustc', 'node', 'sleep',
        'echo', 'printf', 'true', 'false', 'pwd',
    }
    segments = _split_pipeline(command)
    for segment in segments:
        try:
            words = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            return False
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        if not words:
            return False
        executable = os.path.basename(words[0])
        if executable not in independent and not re.fullmatch(r'python3\.[0-9]+', executable):
            return False
    return bool(segments)


# GNU sed's own script grammar (sed/compile.c: snarf_char_class, match_slash,
# compile_address, mark_subst_opts, compile_program), restricted to the
# commands admission proves.
_SED_SAFE_SUBSTITUTION_FLAGS = frozenset('gpiImM0123456789')
# GNU sed writes the first two names to its own streams; none is a workspace file.
_SED_NON_FILE_OUTPUTS = frozenset({'/dev/stdout', '/dev/stderr', '/dev/null'})


def _sed_bracket_end(program, index):
    """Index after the bracket expression whose ``[`` precedes ``index``.

    Mirrors GNU sed's ``snarf_char_class``: inside a bracket the delimiter and
    backslash are literal, a leading ``]`` (after an optional ``^``) is a member
    and ``[:...:]``, ``[.....]`` and ``[=...=]`` may contain ``]``. None when
    the bracket is unterminated.
    """
    end = len(program)
    if index < end and program[index] == '^':
        index += 1
    if index < end and program[index] == ']':
        index += 1
    state, opener = 0, None
    while index < end:
        char = program[index]
        index += 1
        if char in '.:=':
            if state == 1:
                state, opener = 2, char
                continue
            if state == 2 and char == opener:
                state = 3
                continue
        elif char == '[':
            if state == 0:
                state = 1
            continue
        elif char == ']':
            if state < 2:
                return index
            if state == 3:
                state = 0
        state &= ~1
    return None


def _sed_field_end(program, index, delimiter, *, regex):
    """Index after the unescaped ``delimiter`` closing the field at ``index``.

    Mirrors GNU sed's ``match_slash``: a backslash escapes the next character
    and, in a regex, ``[`` opens a bracket expression that may hold the
    delimiter. A replacement has no brackets. None when unterminated.
    """
    end = len(program)
    while index < end:
        char = program[index]
        index += 1
        if char == delimiter:
            return index
        if char == '\\':
            index += 1
        elif char == '[' and regex:
            index = _sed_bracket_end(program, index)
            if index is None:
                return None
    return None


def _sed_address_end(program, index):
    """Index after a line number, ``$`` or ``/regex/[IM]`` address at ``index``.

    ``index`` itself when none starts there (other GNU address forms are outside
    the proof); None for an unterminated regex.
    """
    end = len(program)
    if index < end and program[index] == '$':
        return index + 1
    if index < end and program[index] == '/':
        index = _sed_field_end(program, index + 1, '/', regex=True)
        while index is not None and index < end and program[index] in 'IM':
            index += 1
        return index
    while index < end and program[index] in '0123456789':
        index += 1
    return index


def _sed_script_commands(program):
    """``(command, output file or None)`` for each command of a one-line script.

    The grammar is the subset admission proves: optional data addresses (line
    numbers, ``$``, ``/regex/``, one range, ``!``) on ``s`` (punctuation
    delimiter; flags ``gpiImM`` and digits, or a final ``w FILE``), ``p``,
    ``d``, ``q [EXIT]`` and a final ``w``/``W FILE``, separated by ``;`` and
    blanks. As in GNU sed a filename runs to the end of the script, and fields
    split exactly where sed splits them, so a bracketed ``;`` or delimiter stays
    data. Anything else (newlines, blocks, labels, branches, comments, ``e``,
    ``r``, ``R``, other commands or address forms) returns None.

    Non-ASCII text is data in UTF-8 and single-byte locales, where no ASCII
    byte belongs to a multibyte character; a legacy double-byte locale (GBK,
    Big5) is outside this proof.
    """
    if not 1 <= len(program) <= 4096 or any(char in program for char in '\n\r\v\f\0'):
        return None
    end, index, commands = len(program), 0, []

    def blanks(position):
        while position < end and program[position] in ' \t':
            position += 1
        return position

    while True:
        while index < end and program[index] in '; \t':
            index += 1
        if index == end:
            return tuple(commands) or None
        after = _sed_address_end(program, index)
        if after is None:
            return None
        if after > index:
            index = blanks(after)
            if index < end and program[index] == ',':
                start = blanks(index + 1)
                after = _sed_address_end(program, start)
                if after is None or after == start:
                    return None
                index = blanks(after)
        if index < end and program[index] == '!':
            index = blanks(index + 1)
        if index == end:
            return None
        command, output = program[index], None
        index += 1
        if command == 's':
            delimiter = program[index:index + 1]
            if not '!' <= delimiter <= '~' or delimiter.isalnum() or delimiter == '\\':
                return None
            index = _sed_field_end(program, index + 1, delimiter, regex=True)
            if index is not None:
                index = _sed_field_end(program, index, delimiter, regex=False)
            if index is None:
                return None
            while True:
                index = blanks(index)
                if index == end or program[index] == ';':
                    break
                if program[index] == 'w':
                    output, index = program[blanks(index + 1):], end
                    break
                if program[index] not in _SED_SAFE_SUBSTITUTION_FLAGS:
                    return None
                index += 1
        elif command in 'wW':
            output, index = program[blanks(index):], end
        elif command in 'pdq':
            index = blanks(index)
            if command == 'q':
                while index < end and program[index] in '0123456789':
                    index += 1
                index = blanks(index)
            if index < end and program[index] != ';':
                return None
        else:
            return None
        if output == '':
            return None
        commands.append((command, output))


def _literal_sed_script(program):
    """Prove a script only substitutes, prints, deletes or quits."""
    commands = _sed_script_commands(program)
    return commands is not None and all(output is None for _command, output in commands)


_SedInvocation = namedtuple('_SedInvocation', (
    'programs program_words script_files operands inplace backup follows_links scanned'))
# GNU sed options that change neither the script, the operands nor the files written.
_SED_INERT_LONG_OPTIONS = frozenset({
    '--quiet', '--silent', '--regexp-extended', '--separate', '--unbuffered',
    '--null-data', '--zero-terminated', '--posix', '--debug', '--sandbox', '--binary',
})


def _sed_invocation(arguments):
    """Split GNU sed argv into its roles; None when a word's role is unknown.

    Options precede operands and ``--`` ends them. ``-e``/``--expression`` add
    programs and ``-f``/``--file`` script files, attached or separate; without
    either the first operand is the program. ``program_words`` indexes the
    argument each program came from. A first ``--help``/``--version`` prints and
    exits before sed reads another word, so it has no program or operand; a
    later one does not, since ``-e``/``-f`` compile their program (opening its
    w files) as they are read. Unknown or abbreviated long options, a missing
    value and an option-like word after the first operand (GNU option
    permutation would re-read it) return None. Permutation also means getopt
    reads every word before ``--`` as a possible option, operands included;
    ``scanned`` counts those leading words. A ``--`` after the first operand
    ends GNU's scan but is a file under POSIXLY_CORRECT, which stops at the
    first operand, so it stays an operand.
    """
    if arguments and arguments[0] in {'--help', '--version'}:
        return _SedInvocation((), (), (), (), False, False, False, 0)
    programs, program_words, script_files = [], [], []
    inplace = backup = follows_links = terminated = False
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg == '-' or not arg.startswith('-'):
            break
        index += 1
        if arg == '--':
            terminated = True
            break
        if arg in _SED_INERT_LONG_OPTIONS:
            continue
        if arg == '--follow-symlinks':
            follows_links = True
            continue
        name, separator, value = arg.partition('=')
        if name == '--in-place':
            inplace, backup = True, backup or bool(value)
            continue
        if name in {'--expression', '--file', '--line-length'}:
            word = index - 1
            if not separator:
                if index == len(arguments):
                    return None
                value, word = arguments[index], index
                index += 1
            if name == '--expression':
                programs.append(value)
                program_words.append(word)
            elif name == '--file':
                script_files.append(value)
            continue
        if arg.startswith('--'):
            return None
        position = 1
        while position < len(arg):
            flag = arg[position]
            position += 1
            if flag in 'efl':
                value, word = arg[position:], index - 1
                if not value:
                    if index == len(arguments):
                        return None
                    value, word = arguments[index], index
                    index += 1
                if flag == 'e':
                    programs.append(value)
                    program_words.append(word)
                elif flag == 'f':
                    script_files.append(value)
                break
            if flag == 'i':
                inplace, backup = True, backup or position < len(arg)  # -iSUFFIX, -iE
                break
            if flag not in 'nrsuzEb':
                return None
    scanned = index - 1 if terminated else len(arguments)
    operands = list(arguments[index:])
    if not terminated:
        end = operands.index('--', 1) if '--' in operands[1:] else len(operands)
        if any(word.startswith('-') and word != '-' for word in operands[1:end]):
            return None
        scanned = index + end
    if not programs and not script_files:
        if not operands:
            return None
        programs.append(operands.pop(0))
        program_words.append(index)
    return _SedInvocation(tuple(programs), tuple(program_words), tuple(script_files),
                          tuple(operands), inplace, backup, follows_links, scanned)


def literal_sed_effects(arguments, *, shapes=()):
    """Return (inplace, input paths) only for proven bounded sed programs.

    Every program must pass ``_literal_sed_script``. This is the shared evidence
    for filters, target discovery and FILE leases. Script files, suffix
    backups, ``--follow-symlinks``, an in-place edit without operands and a
    program the shell computes stay unresolved. ``shapes`` holds the
    ``_WordShape`` of each argument: a computed program is not the text proven
    here, and a word sed may read as an option its text does not show,
    anywhere before option scanning ends, voids the proof (``*.py`` matching
    ``-i.py`` makes ``sed -n 1p *.py`` an in-place edit). Resolution still
    verifies each leaf.
    """
    invocation = _sed_invocation(arguments)
    if (invocation is None or invocation.script_files or invocation.backup
            or invocation.follows_links or (invocation.inplace and not invocation.operands)
            or any(word < len(shapes) and shapes[word].computed
                   for word in invocation.program_words)
            or any(shape.hidden for shape in shapes[:invocation.scanned])
            or not all(_literal_sed_script(program) for program in invocation.programs)):
        return None
    return invocation.inplace, invocation.operands


def _sed_written_paths(invocation):
    """In-place operands and the files w commands and ``s///w`` flags name.

    The program text itself is never a path. A program outside the script
    grammar, or one in a script file, names no literal output here.
    """
    paths = list(invocation.operands) if invocation.inplace else []
    for program in invocation.programs:
        paths.extend(output for _command, output in _sed_script_commands(program) or ()
                     if output is not None and output not in _SED_NON_FILE_OUTPUTS)
    return paths


# perl switches that take no value.
_PERL_FLAG_SWITCHES = frozenset('acfnpsStTuUwWX')


def _perl_one_liner(args):
    """``(in-place, operands, code)`` for perl argv whose switches are all known.

    ``-e``/``-E`` code is program text, never a path. ``-i[EXT]``, ``-M``,
    ``-m``, ``-F`` and ``-0xHEX`` take the rest of their cluster, so ``-pie``
    edits in place with backup extension ``e``; ``-I`` takes the rest or the
    next word, and ``-l``/``-0`` octal digits. Without code the first operand
    is a program file. Any other switch returns None.
    """
    inplace, code, index = False, [], 0
    while index < len(args) and args[index].startswith('-') and args[index] != '-':
        arg = args[index]
        index += 1
        if arg == '--':
            break
        position = 1
        while position < len(arg):
            switch = arg[position]
            position += 1
            if switch in 'eE':
                if position < len(arg):
                    code.append(arg[position:])
                elif index < len(args):
                    code.append(args[index])
                    index += 1
                else:
                    return None
                break
            if switch in 'MmFi' or (switch == '0' and arg[position:position + 1] == 'x'
                                    and position + 1 < len(arg)):
                if (switch in 'MmF' and position == len(arg)
                        or switch in 'Fi' and any(char.isspace() for char in arg[position:])):
                    return None
                inplace = inplace or switch == 'i'
                break
            if switch == 'I':
                if position == len(arg):
                    if index == len(args):
                        return None
                    index += 1
                break
            if switch in 'l0':
                while position < len(arg) and arg[position] in '01234567':
                    position += 1
            elif switch not in _PERL_FLAG_SWITCHES:
                return None
    operands = args[index:]
    if not code:
        if not operands:
            return None
        operands = operands[1:]
    return inplace, tuple(operands), tuple(code)


def command_segment_formatter_writes(source):
    """Return (writes, literal targets, complete, owned) for Ruff, or None.

    This proves maintained argv shapes, not permission. Unknown options,
    wrappers and expansions retain writer admission without owned evidence.
    """
    try:
        words = list(command_segment_words(source))
    except ValueError:
        return None
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words.pop(0)
    unwrapped = _unwrap_command_parts(words)
    if not unwrapped or os.path.basename(unwrapped[0]) != 'ruff':
        return None
    if len(unwrapped) < 2 or unwrapped[1] not in {'format', 'check'}:
        return True, (), False, False
    verb, args = unwrapped[1], unwrapped[2:]
    flags = {'--no-cache', '--isolated', '--force-exclude', '-q', '--quiet',
             '-v', '--verbose', '--respect-gitignore', '--no-respect-gitignore'}
    flags |= ({'--check', '--diff'} if verb == 'format' else
              {'--fix', '--no-fix', '--fix-only', '--unsafe-fixes', '--no-unsafe-fixes', '--diff'})
    targets = _static_writer_operands(args, flags)
    complete = (words == unwrapped and targets is not None
                and not shell_source_has_expansion(source)
                and not _has_unquoted_shell_metachars(source))
    if not complete:
        return True, (), False, False
    options = args[:args.index('--')] if '--' in args else args
    if verb == 'format':
        writes = not any(arg in {'--check', '--diff'} for arg in options)
        owned = writes
    else:
        # fix=true in discovered Ruff configuration can make a plain check
        # write. Only explicit no-fix/report mode proves source-read-only.
        writes = '--no-fix' not in options and '--diff' not in options
        owned = writes and any(arg in {'--fix', '--fix-only'} for arg in options)
    return writes, tuple(targets), bool(targets), owned


def _command_checkout_mutation(command, cwd=''):
    """Best-effort checkout mutation signal for undo/snapshot analysis.

    Resolved write targets, in-place utilities and mutating Git count as known
    mutations. Opaque interpreters/test/build commands do not prove mutation,
    but False is not a readonly or stable-checkout guarantee. Commands take no
    workspace lease either way (docs/runbooks/GIT_WORKSPACE_GUARD.md).
    """
    if not command or not command.strip():
        return False
    if _extract_write_targets(command, cwd):
        return True
    if _SED_INPLACE.search(command) or _AWK_INPLACE.search(command):
        return True
    for segment in _split_pipeline(command):
        segment = segment.strip()
        if not segment:
            continue
        words = [word.text for word in _shell_words(segment)]
        index = 0
        while index < len(words) and _ENV_ASSIGNMENT_GUARD_RE.match(words[index]):
            index += 1
        words = git_command_words(words[index:]) or unwrap_process_wrappers(words[index:])
        if not words:
            continue
        base = os.path.basename(words[0])
        if base == 'git':
            if _classify_git_invocation(words[1:]) in ('safe_write', 'tree_destroying'):
                return True
            continue  # readonly or unknown-but-non-mutating Git
        formatter = command_segment_formatter_writes(segment)
        if formatter is not None:
            if formatter[0]:
                return True
            continue
        if base == 'sort':
            parsed = command_segment_targets(segment)
            if parsed.targets or not parsed.complete:
                return True
        if base in _READONLY_COMMANDS:
            continue
        # Opaque non-Git command (interpreter, build/test runner, …): not a
        # proven checkout mutation; no automatic reader lease is inferred.
    return False


# ``-c`` settings that only pin presentation or switch behaviour off. The last
# two disable post-command housekeeping, which no inspection triggers and which
# agents pass on every git invocation.
_HARMLESS_GIT_CONFIG = frozenset({
    'core.fsmonitor=false', 'log.showSignature=false', 'format.pretty=medium',
    'gc.auto=0', 'maintenance.auto=false',
})


def _lease_free_git_segment(command):
    """Return a hardened, fully parsed readonly Git command, else ``None``.

    Writer admission must NOT use the best-effort undo/snapshot classifier
    below: e.g. sort -o, find -delete, and sed's w program all write. Unknown
    commands, options, wrappers and shell grammar require a lease. This small
    literal grammar preserves supported Git inspections, not every
    command that might happen to be readonly. Reject expansions even inside
    quotes rather than interpreting shell syntax with a token-only parser.

    Git can run config-driven helpers or refresh its index even for these
    verbs. Disable those effects as part of the exemption. The canonical
    form is itself accepted so nested outer/worker guards remain lease-free.
    Like the lease itself, this assumes cooperating executables, not a sandbox
    against a substituted git binary or hostile inherited environment.
    """
    if (not command or shell_source_has_expansion(command)
            or re.search(r'[\r\n#;&|<>(){}*?\[\]\\]', _mask_quoted_literals(command))):
        return None
    try:
        words = list(command_segment_words(command))
    except ValueError:
        return None
    if not words or words[0] not in {'git', '/usr/bin/git', '/bin/git'}:
        return None

    directory_args = []
    index = 1
    while index < len(words):
        word = words[index]
        if word in {'--no-pager', '--no-optional-locks'}:
            index += 1
        elif word == '-c' and index + 1 < len(words) and words[index + 1] in _HARMLESS_GIT_CONFIG:
            index += 2
        elif word == '-C' and index + 1 < len(words):
            directory_args.extend(words[index:index + 2])
            index += 2
        else:
            break
    if index >= len(words):
        return None
    verb = words[index]
    args = words[index + 1:]
    original_args = list(args)
    if _classify_git_invocation(words[1:], require_known_options=True) != 'readonly':
        return None
    if verb in {'--version', 'version'}:
        # The version builtin discovers no repository, reads no pager or
        # helper configuration, and no alias can shadow a builtin.
        if args not in ([], ['--build-options']):
            return None
        return shlex.join([words[0], '--no-pager', '--no-optional-locks',
                           *directory_args, verb, *args])

    if verb == 'clean':
        # Reuse the SAME value/bundle parser: proven noninteractive preview.
        # Native clean dry-run has no write hook; disable pager/fsmonitor.
        if _classify_git_clean(args) != 'readonly':
            return None
        return shlex.join([words[0], '--no-pager', '--no-optional-locks',
                           '-c', 'core.fsmonitor=false', '-c', 'log.showSignature=false',
                           '-c', 'format.pretty=medium', *directory_args, verb, *args])
    if verb == 'grep':
        # git grep has no pager/diff-helper hardening flags; its own grammar is
        # the complete proof, including bundled short flags and attached values.
        if _classify_git_grep(args) != 'readonly':
            return None
        return shlex.join([words[0], '--no-pager', '--no-optional-locks',
                           *directory_args, verb, *args])
    if verb == 'hash-object':
        # A path input runs configured clean/process filter drivers (git-lfs
        # clean stores the object it hashes); no hardening flag disables them.
        if _git_hash_object_runs_filters(args) is not False:
            return None
        return shlex.join([words[0], '--no-pager', '--no-optional-locks',
                           *directory_args, verb, *args])
    flags = {
        'status': {'-s', '--short', '-b', '--branch', '-z', '--null', '--porcelain',
                   '--porcelain=1', '--porcelain=2', '--porcelain=v1', '--porcelain=v2',
                   '--long', '--show-stash', '--ahead-behind', '--no-ahead-behind',
                   '-u', '-uno', '-unormal', '-uall', '--untracked-files=no',
                   '--untracked-files=normal', '--untracked-files=all'},
        'branch': {'--show-current', '--list', '-l', '-a', '--all', '-r', '--remotes', '-v', '-vv',
                   '--verbose'},
        'tag': {'--list', '-l', '-n'},
        'worktree': {'list', '--porcelain', '-z'},

        'check-ignore': {'-q', '--quiet', '-v', '--verbose', '-n', '--non-matching',
                         '-z', '--stdin', '--no-index'},
        'remote': {'-v', '--verbose'},
        'rev-parse': {'--show-toplevel', '--git-dir', '--git-common-dir', '--show-prefix',
                      '--is-inside-work-tree', '--is-bare-repository', '--verify', '--short',
                      '--abbrev-ref', '--symbolic', '--symbolic-full-name', '--git-path'},
        'ls-files': {'-z', '-s', '--stage', '-c', '--cached', '-d', '--deleted', '-m',
                     '--modified', '-o', '--others', '-i', '--ignored', '--exclude-standard',
                     '--eol', '--full-name', '--error-unmatch'},
        'ls-tree': {'-r', '-t', '-d', '-l', '-z', '--long', '--name-only', '--name-status',
                    '--full-name', '--full-tree'},
        'merge-base': {'-a', '--all', '--is-ancestor', '--octopus', '--independent', '--fork-point'},
        'symbolic-ref': {'-q', '--quiet', '--short'},
        'cherry': {'-v', '--verbose', '--abbrev'},
        'cat-file': {'-p', '-t', '-s', '-e', '--batch', '--batch-check', '--batch-all-objects', '--unordered'},
        'for-each-ref': set(),
        'config': {'--get', '--get-all', '--get-regexp', '--get-urlmatch', '--list', '-l',
                   '--show-origin', '--show-scope', '--local', '--global', '--system',
                   '--includes', '--null', '-z', 'get', 'list'},
        'describe': {'--always', '--tags', '--all', '--long', '--dirty', '--broken'},
        'name-rev': {'--name-only', '--tags', '--all', '--no-undefined', '--always'},
        'blame': {'--no-textconv', '-n', '-s', '-p', '--porcelain', '--line-porcelain'},
        'shortlog': {'-n', '-s', '-e', '--numbered', '--summary', '--email', '--all'},
        'count-objects': {'-v', '--verbose', '-H', '--human-readable', '-vH', '-Hv'},
        # git grep has no mutable output; its pager option is refused above.
        'grep': _GIT_GREP_FLAGS,
    }
    diff_flags = {'--stat', '--numstat', '--shortstat', '--name-only', '--name-status',
                  '--check', '--raw', '--summary', '--patch', '-p', '--no-patch', '-s',
                  '--no-ext-diff', '--no-textconv', '--no-color', '--color=never',
                  '--color=auto', '--color=always', '--exit-code', '--quiet',
                  '--cached', '--staged', '--no-index', '--ignore-space-change',
                  '--ignore-all-space', '--ignore-space-at-eol', '--binary', '--full-index'}
    log_flags = {'--oneline', '--graph', '--all', '--decorate', '--decorate=short',
                 '--decorate=full', '--decorate=auto', '--decorate=no', '--no-decorate',
                 '--no-show-signature', '--reverse', '--date-order', '--topo-order',
                 '--first-parent', '--no-merges', '--merges',
                 '-i', '--regexp-ignore-case', '--all-match', '--invert-grep',
                 '--basic-regexp', '--extended-regexp', '--fixed-strings',
                 '--perl-regexp', '-E', '-F', '-P'} | diff_flags
    flags.update({'log': log_flags, 'show': log_flags, 'diff': diff_flags,
                  'rev-list': {'--count', '--left-right', '--boundary', '--objects'} | log_flags,
                  'reflog': log_flags, 'stash': log_flags})
    values = {
        'log': {'-n', '--max-count', '--format', '--pretty', '--date', '--since', '--until', '--author', '--grep'},
        'show': {'--format', '--pretty', '--date'},
        'diff': {'--diff-filter', '--relative'},
        'ls-files': {'--exclude', '--exclude-from'},
        'for-each-ref': {'--format', '--sort', '--count', '--points-at', '--contains', '--merged'},
        'branch': {'--format', '--sort', '--contains', '--no-contains', '--merged', '--no-merged',
                   '--points-at'},
        'tag': {'--format', '--sort', '--contains', '--merged', '--no-merged', '--points-at'},
        'describe': {'--abbrev', '--match', '--exclude', '--dirty', '--broken'},
        'blame': {'-L', '--contents'},
        'shortlog': {'--format', '--group'},
        'cherry': {'--abbrev'},
    }
    values['rev-list'] = values['log']
    values['reflog'] = values['log']
    values['stash'] = values['log']

    # Pickaxe searches the same diffs as -p; log/show run them under the
    # --no-ext-diff/--no-textconv hardening below. rev-list gets no hardening.
    values['log'] = values['log'] | {'-S', '-G'}
    flags['log'] = flags['log'] | {'--pickaxe-regex', '--pickaxe-all'}
    if verb not in flags:
        return None
    if verb == 'worktree' and args[:1] != ['list']:
        return None
    if verb == 'reflog' and args[:1] == ['show']:
        args = args[1:]
    if verb == 'stash':
        if args[:1] not in (['list'], ['show']):
            return None
        args = args[1:]
    index = 0
    while index < len(args):
        word = args[index]
        if word == '--':
            break  # Remaining tokens are literal pathspecs, not options.
        option = word.split('=', 1)[0]
        if word in flags[verb] or not word.startswith('-'):
            index += 1
        elif option in values.get(verb, set()):
            if '=' in word:
                value = word.split('=', 1)[1]
                index += 1
            elif index + 1 < len(args):
                value = args[index + 1]
                index += 2
            elif verb == 'branch' and option in _GIT_OPTIONAL_COMMIT_OPTIONS:
                index += 1  # Last on the command line, the commit defaults to HEAD.
                continue
            else:
                return None
            if option in {'--format', '--pretty', '--group'}:  # --group=format:<format>
                if '%G' in value or re.search(r'%\(\*?signature(?::|\))', value):
                    return None  # Verification atoms can run signature helpers.
        elif verb in {'log', 'show', 'rev-list', 'reflog', 'stash'} and re.fullmatch(
                r'(?:-[0-9]+|-n[0-9]+|-U[0-9]+)', word):
            index += 1
        elif verb == 'branch' and re.fullmatch(r'-[arv]+', word):
            index += 1  # Bundled listing flags: -av, -rv.
        elif verb == 'diff' and re.fullmatch(r'-U[0-9]+', word):
            index += 1
        elif verb == 'log' and word[:2] in ('-S', '-G') and len(word) > 2:
            index += 1  # attached pickaxe value: -S'needle'
        else:
            return None  # Unknown/helper/output flags retain writer admission.
    # Keep parameter-sensitive listing commands from borrowing operand support
    # to create refs; classification above owns this distinction.
    hardening = ([flag for flag in ('--no-ext-diff', '--no-textconv') if flag not in original_args]
                 if verb in {'log', 'show', 'diff', 'reflog', 'stash', 'blame'} else [])
    if verb == 'blame':
        hardening = [flag for flag in hardening if flag != '--no-ext-diff']
    if verb == 'stash' or (verb == 'reflog' and original_args[:1] == ['show']):
        hardened_args = original_args[:1] + hardening + original_args[1:]
    else:
        hardened_args = hardening + original_args
    configuration = ([] if verb == 'config' else [
        '-c', 'core.fsmonitor=false', '-c', 'log.showSignature=false',
        '-c', 'format.pretty=medium'])
    return shlex.join([
        words[0], '--no-pager', '--no-optional-locks',
        *configuration, *directory_args, verb, *hardened_args,
    ])


# Read verbs whose operands only select revisions, paths or patterns: no
# operand, whatever it expands to, switches them into a writing mode.
_EXPANSION_INERT_GIT_VERBS = frozenset({
    'log', 'show', 'diff', 'rev-list', 'ls-files', 'ls-tree', 'grep', 'blame',
    'shortlog', 'status', 'cat-file', 'rev-parse', 'merge-base', 'name-rev',
    'cherry', 'check-ignore', 'describe', 'for-each-ref',
})
_SIMPLE_PARAMETER = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})')
# A plain value, and a signature-verification atom that a format value may not hold.
_EXPANSION_PROBES = ('x', '%G?')


def git_read_expansion_inert(source):
    """Whether a Git read stays inside the hardened grammar whatever it expands to.

    Every expanded word must follow a literal read verb whose operands cannot
    select a mode, start with a literal character so that no word it produces
    is an option (``lib/*.py``, ``HEAD.."$b"``, ``--grep="$s"``), and hold
    only plain ``$NAME`` parameters or pathname patterns. The grammar must
    accept the command with each parameter replaced by a plain value and by a
    signature atom, which keeps an expanded ``--format`` value conservative.
    Global options, the verb, redirects, command substitutions and words
    that may split or begin with ``-`` (``$x``, ``"$x"``, ``*.py``) are never
    proven.
    """
    if re.search(r'[<>`]', _mask_quoted_literals(source)) or '$(' in source:
        return False
    try:
        tokens = _shell_words(source, strict=True, redirects=True)
        shapes = command_segment_word_shapes(source)
    except ValueError:
        return False
    texts = [token.text for token in tokens]
    index = 1
    while index < len(texts) and texts[index] in {'--no-pager', '--no-optional-locks', '-c', '-C'}:
        index += 2 if texts[index] in {'-c', '-C'} else 1
    if (not texts or os.path.basename(texts[0]) != 'git' or index >= len(texts)
            or texts[index] not in _EXPANSION_INERT_GIT_VERBS
            or any(shape.computed for shape in shapes[:index + 1])):
        return False
    expanded = [position for position, shape in enumerate(shapes) if shape.computed]
    if not expanded or any(shapes[position].hidden for position in expanded):
        return False
    for probe in _EXPANSION_PROBES:
        words = list(texts)
        for position in expanded:
            words[position] = _SIMPLE_PARAMETER.sub(probe, texts[position])
            if '$' in words[position]:
                return False  # positional, special or operator expansions
        if _lease_free_git_segment(shlex.join(words)) is None:
            return False
    return True


def _lease_free_git_command(command):
    """Harden every Git inspection in a literal chain, preserving separators.

    Literal opaque Python/runtime consumers keep their existing lease-free
    scheduling policy; this grants no read authorization to embedded code.
    Writes, wrappers, redirections and unknown Git shapes never borrow the
    exemption from a neighbouring inspection.
    """
    if not command or shell_source_has_expansion(command):
        return None
    masked = _mask_quoted_literals(command)
    if re.search(r'[\r\n#<>(){}*?\[\]~\\]', masked):
        return None
    spans = _split_pipeline_spans(command)
    if not spans or command[:spans[0][0]].strip() or command[spans[-1][1]:].strip():
        return None
    replacements = []
    saw_git = False
    for index, (start, end) in enumerate(spans):
        if index and command[spans[index - 1][1]:start].strip() not in {';', '&&', '||', '|'}:
            return None
        segment = command[start:end].strip()
        try:
            words = list(command_segment_words(segment))
        except ValueError:
            return None
        if not words:
            return None
        if os.path.basename(words[0]) == 'git':
            hardened = _lease_free_git_segment(segment)
            if hardened is None:
                return None
            replacements.append((start, end, hardened))
            saw_git = True
        elif not (_independent_runtime_command(segment)
                  or (words[0] in {'head', 'tail', 'wc', 'cat', 'cut', 'tr'}
                      and not _command_checkout_mutation(segment))):
            return None
    if not saw_git:
        return None
    for start, end, hardened in reversed(replacements):
        command = command[:start] + ' ' + hardened + ' ' + command[end:]
    return command.strip()


def command_requires_workspace_runtime(command):
    """Only proven Git inspection chains can skip dependency preparation.

    Lease-free scheduling alone includes interpreters/builds. Require the
    shared guard's complete readonly classification as well, so an adjacent
    Python/Node consumer still gets its workspace dependencies.
    """
    return not (_git_guard_classify(command, require_known_segments=True) == 'readonly'
                and _lease_free_git_command(command) is not None)


def _harden_substitution_bodies(command, view):
    """Harden Git reads inside ``$(…)``/backtick bodies, which the shell runs too.

    Without this a ``echo "HEAD $(git rev-parse HEAD)"`` prefix kept the whole
    command (often a long test run) under a checkout fence, because the body
    ran unhardened and an index read may invoke a configured fsmonitor hook.
    Admission judges each body by its executed text, so both sides agree.
    Commands with heredocs keep exact bytes, as do backtick bodies containing
    backslashes (re-escaping a rewritten body is not attempted). *view* is the
    command's :func:`command_rewrite_text` view: a substitution-shaped text in
    a comment is blanked there and stays as written.
    """
    if 'heredoc' in command_segment_dynamic_kinds(command):
        return command
    bodies = command_substitution_bodies(command)
    for start, end, body in reversed(bodies or ()):
        if view[start] != command[start]:
            continue
        raw = command[start:end]
        if raw.startswith('`'):
            if '\\' not in raw:
                command = f'{command[:start]}`{harden_git_inspections(body)}`{command[end:]}'
        else:
            command = f'{command[:start]}$({harden_git_inspections(body)}){command[end:]}'
    return command


def harden_git_inspections(command):
    """Harden literal read segments even when another segment needs a lease.

    Authorization remains bound to the original tool arguments. A recorded
    background wrapper is never rewritten after binding (its typed binding
    preserves exact bytes); the adapter binds this function's output instead
    (``run_command.command_execution_source``). Rewriting changes no shell
    separators/cwd.
    Only command text is hardened (:func:`command_rewrite_text`): heredoc
    bodies, comments and quoted strings keep their bytes. Command-substitution
    bodies are hardened first, as commands of their own.
    """
    text = command_rewrite_text(command)
    if text is None:
        return command
    command = _harden_substitution_bodies(command, text.view)
    text = command_rewrite_text(command)
    if text is None:
        return command
    replacements = []
    for start, end in text.ranges:
        segment = command[start:end].strip()
        # A shell wrapper keeps its own admission: the hardened read grammar
        # owns a literal `git` head only, and an `env git …` query still
        # refreshes the index.
        hardened = _lease_free_git_segment(segment)
        if hardened is None:
            # A trailing ``2>/dev/null`` is not shell write syntax; harden the
            # residual inspection and keep the original redirect bytes.
            split = command_segment_discardable_redirect_split(segment)
            if split is not None:
                core, suffix = split
                core_hardened = _lease_free_git_segment(core)
                hardened = (None if core_hardened is None
                            else core_hardened + ' ' + suffix)
        if hardened is not None and hardened != segment:
            replacements.append((start, end, hardened))
    for start, end, hardened in reversed(replacements):
        # Pad only against neighbouring bytes: the command's own edge blanks
        # can be syntax (`$( (` is a subshell, `$((` arithmetic; `a\ `).
        command = (command[:start] + (' ' if start else '') + hardened
                   + (' ' if end < len(command) else '') + command[end:])
    return command


# Best-effort undo/snapshot fast path, NOT proof of read-only execution.
# Several utilities have write-capable flags or embedded programs. Lease
# admission must use the separate fail-closed parser above, never this set.
_READONLY_COMMANDS = frozenset({
    # ── Search / match ──
    'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack',
    # ── View / page ──
    'cat', 'head', 'tail', 'less', 'more', 'bat',
    # ── List / stat ──
    'ls', 'dir', 'tree', 'stat', 'file', 'du', 'df',
    # ── Find / locate ──
    'find', 'fd', 'fdfind', 'locate', 'which', 'whereis', 'type',
    # ── Text processing (pure filters — no in-place flag) ──
    # Note: sed/awk are here because plain invocations are stdout filters;
    # sed -i and gawk's `-i inplace` extension are caught separately by
    # _SED_INPLACE / _AWK_INPLACE before the whitelist check.  The awk
    # variants share awk's semantics — including its pre-existing blind
    # spot for writes from inside the program text (print > "file" is
    # caught by the redirect probe; system("...") is not, same as awk).
    'wc', 'sort', 'uniq', 'cut', 'tr', 'sed', 'column',
    'awk', 'gawk', 'mawk', 'nawk', 'goawk',
    # ── Compare / hash ──
    'diff', 'cmp', 'comm', 'md5sum', 'sha256sum', 'sha1sum',
    # ── Shell builtins / info ──
    'echo', 'printf', 'true', 'false', 'test', '[',
    'env', 'printenv', 'whoami', 'id', 'hostname',
    'date', 'cal', 'uptime', 'uname',
    'pwd', 'basename', 'dirname', 'realpath', 'readlink',
    # ── Process / resource inspection ──
    'ps', 'top', 'htop', 'free', 'vmstat', 'lsof', 'pgrep',
    # ── Network (query-only forms) ──
    'ping', 'dig', 'nslookup', 'host', 'traceroute',
    # ── Data format ──
    'jq', 'yq',
    # ── Git read-only sub-commands are handled specially below ──
})

# Git sub-commands that are purely read-only.
_GIT_READONLY_SUBCOMMANDS = frozenset({
    'status', 'log', 'diff', 'show', 'branch', 'tag',
    'remote', 'describe', 'rev-parse', 'rev-list',
    'ls-files', 'ls-tree', 'ls-remote', 'merge-base', 'cat-file', 'for-each-ref', 'name-rev',
    'check-ignore', 'version', 'cherry',
    'blame', 'shortlog', 'reflog',
    'config', 'stash',  # Parameter-sensitive forms are checked before this set.
    'count-objects',
})

# ``git branch``/``git tag`` filters whose commit defaults to HEAD when last.
_GIT_OPTIONAL_COMMIT_OPTIONS = frozenset({'--contains', '--no-contains', '--merged', '--no-merged'})
# ``git config`` subcommands (Git 2.46+); a lone one is never a key read.
_GIT_CONFIG_ACTIONS = frozenset({
    'get', 'list', 'set', 'unset', 'rename-section', 'remove-section', 'edit',
})

_GIT_DESTRUCTIVE_SUBCOMMANDS = frozenset({
    'checkout', 'switch', 'reset', 'clean', 'rm', 'mv',
    'stash', 'rebase', 'merge', 'cherry-pick', 'revert',
    'apply', 'am', 'pull', 'fetch', 'push', 'clone', 'init',
    'add', 'commit', 'restore',
})

# Shell output redirection operators that write to files.
# Excludes harmless >/dev/null and 2>/dev/null (stderr suppression).
_REDIRECT_TO_DEV_NULL = _re.compile(r'[12]?>+\s*/dev/null\b')
_REDIRECT_PATTERN = _re.compile(r'[12]?>>?(?!&)')

# sed with in-place flag
_SED_INPLACE = _re.compile(r'\bsed\b.*\s-i')

# awk-family in-place editing via gawk's bundled ``inplace`` extension:
# ``gawk -i inplace '{...}' file`` (also ``-iinplace``, ``--include inplace``
# and ``--include=inplace``; goawk accepts the same flags).  Like
# _SED_INPLACE this is a loose whole-command probe: it may cross pipeline
# separators and match ``-i inplace`` inside a quoted program — a false
# positive only costs one extra snapshot (~5 ms), while a false negative
# silently loses undo coverage.  Exact operand parsing happens per segment
# in _extract_write_targets via _awk_inplace_operands.
_AWK_INPLACE = _re.compile(r'\b\w*awk\b.*?\s(?:-i|--include)[\s=]*\S*inplace')


_ShellWord = namedtuple(
    '_ShellWord', ['text', 'start', 'end', 'squote', 'dquote'])


def _removed_quotes(raw):
    """The text POSIX quote removal makes of one unsplit shell word.

    Inside double quotes a backslash quotes only ``$``, a backtick, ``"``,
    a backslash or a newline; ``shlex`` kept it before ``$`` and the backtick,
    so ``sh -c "touch \\$x"`` read as a write of a file named ``$x`` while the
    shell runs ``touch $x``. A backslash-newline is a line continuation.
    """
    out, index, quote = [], 0, None
    while index < len(raw):
        char = raw[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                out.append(char)
        elif char == '\\':
            if index + 1 == len(raw):
                raise ValueError('No escaped character')
            following = raw[index + 1]
            if quote == '"' and following not in '$`"\\\n':
                out.append(char)
            elif following != '\n':
                out.append(following)
                index += 1
            else:
                index += 1
        elif char == '"':
            quote = None if quote else '"'
        elif char == "'" and quote is None:
            quote = "'"
        else:
            out.append(char)
        index += 1
    if quote is not None:
        raise ValueError('No closing quotation')
    return ''.join(out)


def _shell_words(s, *, strict=False, redirects=False):
    """Split one shell segment into unquoted words with source offsets.

    Guards and rewriters share this parser so quoted patterns such as
    ``grep "not found"`` cannot be interpreted differently by a fallback
    path. Shell separators are handled by :func:`_split_pipeline_spans`;
    callers pass one resulting segment here.
    """
    out = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        if redirects and s[i] in '<>':
            while i < n and s[i] in '<>':
                i += 1
            out.append(_ShellWord(s[start:i], start, i, False, False))
            continue
        quote = None
        squote = dquote = False
        while i < n:
            char = s[i]
            if quote is None and (char.isspace() or (redirects and char in '<>')):
                break
            if char == '\\' and quote != "'":
                if quote != '"' or (i + 1 < n and s[i + 1] in '$`"\\\n'):
                    i += 2
                    continue
            if char in "'\"":
                if quote is None:
                    quote = char
                    squote = squote or char == "'"
                    dquote = dquote or char == '"'
                elif quote == char:
                    quote = None
            i += 1
        raw = s[start:i]
        try:
            text = _removed_quotes(raw)
        except ValueError:
            if strict:
                raise
            text = raw  # Best-effort legacy analysis; admission uses strict mode.
        out.append(_ShellWord(text, start, min(i, n), squote, dquote))
    return out


def _split_pipeline(cmd):
    """Split a shell command into pipeline/chain segments, respecting quotes.

    Splits on |, ;, &&, ||, newlines, and subshell parentheses but NOT
    inside single or double quotes. This prevents splitting patterns like
    ``grep -i "foo|bar"`` on the pipe.

    Newlines and bare ``(`` / ``)`` are ALWAYS command structure in shell
    grammar (a newline is a command separator; an unquoted paren is a
    subshell / function / case / expansion delimiter, never part of a
    word — escaped ``\\(`` stays glued via the backslash branch). Leaving
    them attached glues phantom tokens onto segments: the subshell closer
    in ``( producer | grep -E 'pat' )`` tokenizes as a fake grep OPERAND
    (a stream filter misread as a filesystem read), and the opener in
    ``( grep -rn x lib/ )`` masks the command word entirely (evasion).
    """
    return [cmd[s:e].strip() for s, e in _split_pipeline_spans(cmd)]


def _split_pipeline_spans(cmd, *, keep_substitutions=False):
    """Span-returning twin of :func:`_split_pipeline`: list of (start, end)
    offsets into *cmd* for each non-empty segment (separators excluded,
    surrounding whitespace included). The grep-redirect splicer rewrites
    segments in place through these spans."""
    spans = []
    # Admission recursively judges each substitution in its actual cwd. Keep
    # its enclosing word intact instead of inventing an executable `n=$`.
    # Existing source-splicing consumers retain their historical span view.
    substitutions = (dict((start, end) for start, end, _ in
                          command_substitution_bodies(cmd) or ())
                     if keep_substitutions else {})
    start = 0
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        if i in substitutions:
            i = substitutions[i]
            continue
        c = cmd[i]
        if c == "'" and not in_double:
            in_single = not in_single
            i += 1
        elif c == '"' and not in_single:
            in_double = not in_double
            i += 1
        elif c == '\\' and i + 1 < n and (in_double or not in_single):
            i += 2
        elif not in_single and not in_double:
            if c in ';\n()':
                spans.append((start, i))
                i += 1
                start = i
            elif c == '&' and i + 1 < n and cmd[i + 1] == '&':
                spans.append((start, i))
                i += 2
                start = i
            elif c == '|' and i + 1 < n and cmd[i + 1] == '|':
                spans.append((start, i))
                i += 2
                start = i
            elif c == '|':
                spans.append((start, i))
                i += 1
                start = i
            else:
                i += 1
        else:
            i += 1
    spans.append((start, n))
    return [(s, e) for s, e in spans if cmd[s:e].strip()]


def pipeline_span_separator(raw):
    """Control meaning of the raw text between two :func:`_split_pipeline_spans` spans.

    Returns ``(operator, closes, opens)``. ``operator`` joins the two
    commands, parentheses aside: ``'&&'``, ``'||'``, ``'|'``, ``';'`` (a
    newline separates exactly like ``;``), ``';;'`` in a case clause, or ``''``.
    ``closes``: a subshell or substitution ended there, so any cwd it entered
    is gone and the next command may run in every earlier cwd. ``opens``: the
    next command starts a new command list (a subshell or substitution), so
    it runs first there whatever ``operator`` the group itself follows.
    """
    operator = raw.replace('(', ' ').replace(')', ' ').strip()
    if not operator and '\n' in raw:
        operator = ';'
    return operator, ')' in raw, raw.rfind('(') > raw.rfind(')')


def _literal_heredoc_body_view(command):
    """Return *command* with literal heredoc body lines blanked in place.

    Complete quoted ``cat``/``python``/``node`` heredoc bodies are file content
    or interpreter source, never shell segments: span rewriters analyse this
    offset-preserving view and splice the ORIGINAL command at the same offsets.
    ``command_heredoc`` owns the grammar. Any shape its admission view does not
    accept, and any body its stdin-intent view reports as executable or
    expanded (``cat <<'EOF' | sh``), keeps the whole command visible. The
    admission view keeps header newlines and blanks body/delimiter newlines, so
    exactly those lines are hidden; header bytes (marker, descriptor
    duplications) stay as written.
    """
    from lib.project_mod.command_heredoc import (  # it imports this module
        command_admission_view, command_stdin_intent_view)
    view = command_admission_view(command)
    if view == command or len(view) != len(command):
        return command
    intent_view, executable_bodies = command_stdin_intent_view(command)
    if executable_bodies or intent_view != view:
        return command
    lines = []
    start = 0
    while start < len(command):
        newline = command.find('\n', start)
        end = len(command) if newline < 0 else newline + 1
        hidden = (view[newline] != '\n' if newline >= 0
                  else view[start:end] != command[start:end])
        lines.append(' ' * (end - start) if hidden else command[start:end])
        start = end
    return ''.join(lines)


def command_rewrite_text(command):
    """The command text every run_command rewrite may edit, or None for none.

    The one rule of each in-place rewrite (grep splice, find budget, cp splice,
    grep -r flags, Git read hardening and preparation, curl/wget hardening) and
    of the guards that mirror them: ``command_structure.shell_rewrite_text``
    with :func:`command_heredoc.shell_script_heredocs` naming the heredoc
    bodies a local shell runs. Heredoc bodies, comments, quoted strings,
    here-strings and backtick bodies are never edited. A rewriter analyses
    ``.view`` (same offsets as *command*) or ``.ranges`` and splices the
    original; None means the structure cannot be delimited, so the executed
    bytes stay exactly as written. Decision: "Command rewrites edit only
    command text" in docs/modules/tools_execution.md.
    """
    from lib.project_mod.command_heredoc import shell_script_heredocs  # it imports this module
    from lib.project_mod.command_structure import shell_rewrite_text
    try:
        return shell_rewrite_text(command, scripts=shell_script_heredocs)
    except ShellStructureUnknown:
        return None


# ── Commands whose file arguments are WRITE targets ──────────────────
# Maps command name → set of arg-index semantics for extracting write targets.
# 'all_args'       — every non-flag argument is a write target (rm, touch, chmod)
# 'last_arg'       — last non-flag argument is the write target (cp, install)
# 'all_args_files' — like all_args but only existing files (mkdir targets are dirs)
_WRITE_TARGET_COMMANDS = {
    # Delete
    'rm':     'all_args',
    'rmdir':  'all_args',
    'unlink': 'all_args',
    # Create / modify metadata
    'touch':  'all_args',
    'chmod':  'all_args',
    'chown':  'all_args',
    'chgrp':  'all_args',
    # Copy / move — destination is the write target; source is read-only
    'cp':     'last_arg',
    'mv':     'last_arg',
    'install':'last_arg',
    # In-place editors
    'patch':  'all_args',
    # Archive extraction
    'tar':    'opaque',   # too complex to parse; fall back to full snapshot
    'unzip':  'opaque',
    'gunzip': 'all_args',
    # In-place edit (sed -i handled specially in _extract_write_targets)
    'sed':    'sed_special',
}


# awk option letters that consume a value (glued or separate): -F sep,
# -v var=val, -f file, -i ext, -e text, -E file, -l ext, -W compat.
# NOTE: gawk's -o is deliberately absent — modern gawk treats it as
# argless, so consuming a following word here would misalign operands.
_AWK_VALUE_OPTS = frozenset('FvfieElW')

# awk options whose presence means the PROGRAM came from an option, so
# every operand is a VAR=val assignment or a file (never program text).
_AWK_PROGRAM_OPTS = frozenset('feE')


_SdInvocation = namedtuple('_SdInvocation', ['program', 'files', 'writes'])


def _sd_invocation(args):
    """Parse ``sd [OPTIONS] FIND REPLACE [FILES]...`` (sd 1.x, clap grammar).

    sd rewrites every FILES operand in place unless ``-p/--preview`` (or
    ``--help``/``--version``) is given; with no FILES it is a stdin filter.
    Options may follow positionals until ``--``. Returns None for an option
    outside the documented grammar so the caller keeps every word a candidate.
    """
    positionals, writes, options, index = [], True, True, 0
    while index < len(args):
        arg = args[index]
        index += 1
        if options and arg == '--':
            options = False
        elif options and arg.startswith('--'):
            option, separator, _value = arg.partition('=')
            if option in {'--max-replacements', '--flags'}:
                if not separator:
                    if index >= len(args):
                        return None
                    index += 1
            elif option in {'--preview', '--help', '--version'} and not separator:
                writes = False
            elif option != '--fixed-strings' or separator:
                return None
        elif options and arg.startswith('-') and arg != '-':
            position = 1
            while position < len(arg):
                flag = arg[position]
                position += 1
                if flag in 'nf':
                    if position == len(arg):
                        if index >= len(args):
                            return None
                        index += 1
                    break
                if flag in 'phV':
                    writes = False
                elif flag != 'F':
                    return None
        else:
            positionals.append(arg)
    if len(positionals) < 2:
        writes = False
    return _SdInvocation(tuple(positionals[:2]), tuple(positionals[2:]), writes)


def _awk_inplace_operands(args):
    """File operands of an awk argv that loads the ``inplace`` extension.

    ``args`` are the words after the awk command word (naive whitespace
    split, mirroring the sed -i block's trade-off: a quoted multi-token
    program leaves junk tokens in the operand list — the safe
    over-inclusion direction, since a junk target only widens the diff
    filter).  Returns a list of file targets when ``-i inplace`` /
    ``--include[=] inplace`` is present, else None.  Under-inclusion is
    not possible: without ``-e``/``-f``/``-E`` the FIRST operand is always
    the program text, and real file operands can never precede it.
    """
    inplace = False
    program_via_option = False
    operands = []
    i, n = 0, len(args)
    while i < n:
        word = args[i]
        if word == '--':
            operands.extend(args[i + 1:])
            break
        if word.startswith('--'):
            name, eq, val = word[2:].partition('=')
            if name in ('include', 'load', 'assign', 'field-separator',
                        'file', 'exec', 'source'):
                if not eq:
                    i += 1
                    val = args[i] if i < n else ''
                if name == 'include' and 'inplace' in val.split('/')[-1]:
                    inplace = True
                if name in ('file', 'exec', 'source'):
                    program_via_option = True
            i += 1
            continue
        if word.startswith('-') and word != '-':
            letter, val = word[1:2], word[2:]
            if letter in _AWK_VALUE_OPTS:
                if not val:
                    i += 1
                    val = args[i] if i < n else ''
                if letter == 'i' and 'inplace' in val.split('/')[-1]:
                    inplace = True
                if letter in _AWK_PROGRAM_OPTS:
                    program_via_option = True
            i += 1
            continue
        operands.append(word)
        i += 1
    if not inplace:
        return None
    if operands and not program_via_option:
        operands = operands[1:]  # the first operand is the program text
    # VAR=val operands are assignments, not files.  A real file whose name
    # contains '=' must be addressed as ./name (awk's own rule too), which
    # does not match this pattern.
    return [w for w in operands if not _re.match(r'^\w+=', w)]


# After ``$`` these start a parameter, arithmetic or command expansion; before
# anything else (``/``, a blank, the end of the word) the shell keeps ``$``.
_SHELL_EXPANSION_STARTS = frozenset(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_@*#?$!-{([')


def _shell_dollar_is_literal(source, index, quote):
    following = source[index + 1:index + 2]
    return following not in _SHELL_EXPANSION_STARTS and not (
        quote is None and following in ('"', "'"))  # bash $'...' and $"..."


def _shell_expansion_prefix(source, *, pathname=True, literal_dollars=False):
    """Decode the literal prefix before a real shell expansion, if any.

    ``literal_dollars`` keeps a ``$`` that starts no expansion as text instead
    of conservatively counting every ``$`` as one.
    """
    quote = None
    prefix = []
    index = 0
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'" and index + 1 < len(source):
            following = source[index + 1]
            if quote != '"' or following in '$`"\\\n':
                prefix.append(following)
                index += 2
                continue
        if char in "'\"":
            if quote is None:
                quote = char
                index += 1
                continue
            if quote == char:
                quote = None
                index += 1
                continue
        # Tilde in HEAD~1 is literal, unlike a token/assignment prefix ~.
        # Keep assignment/colon cases conservative across supported shells.
        tilde_prefix = char == '~' and (index == 0 or source[index - 1] in ' \t\r\n=:')
        dollar = char == '$' and not (
            literal_dollars and _shell_dollar_is_literal(source, index, quote))
        if (quote != "'" and (char == '`' or dollar)
                or pathname and quote is None and (char in '*?[]{}' or tilde_prefix)):
            return ''.join(prefix), True
        prefix.append(char)
        index += 1
    return ''.join(prefix), quote is not None


def shell_source_has_expansion(source, *, pathname=True):
    """Detect expansion, not literal dollars/globs inside single quotes."""
    return _shell_expansion_prefix(source, pathname=pathname)[1]


def command_segment_words(source):
    """One shell-token authority for utility targets and admission.

    Redirection operators are separate tokens even without whitespace. Quoted
    operands remain one token; no command text or embedded program is executed.
    """
    return tuple(word.text for word in _shell_words(source, strict=True, redirects=True))


# What the shell may make of one source word before its program sees it.
# computed: the text is unknown before execution. hidden: some resulting word
# may begin with ``-``, so a program can read it as an option the source does
# not show. multiple: it may become zero or several words.
_WordShape = namedtuple('_WordShape', 'computed hidden multiple')


def _brace_expands(raw, start):
    """Whether the unquoted ``{`` at *start* opens a brace expansion.

    Only a comma or a ``..`` sequence before the closing brace expands;
    ``{}`` and ``{x}`` stay literal words.
    """
    quote, index, depth = None, start + 1, 1
    while index < len(raw):
        char = raw[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if char in "'\"" and quote in (None, char):
            quote = None if quote else char
        elif quote is None:
            if char == ',' or raw.startswith('..', index):
                return True
            depth += char == '{'
            depth -= char == '}'
            if not depth:
                return False
        index += 1
    return False


def _expansion_end(raw, index):
    """Index after the ``$(…)``, ``$((…))``, ``${…}`` or backtick body at *index*.

    None when the body is not tracked (unterminated, or a construct the
    substitution scanner declines); the caller then assumes the worst.
    """
    if raw[index] == '`':
        close = index + 1
        while close < len(raw) and raw[close] != '`':
            close += 2 if raw[close] == '\\' else 1
        return close + 1 if close < len(raw) else None
    if raw.startswith('$((', index):
        return _arithmetic_end(raw, index + 3)
    if raw.startswith('$(', index):
        close = _substitution_end(raw, index + 2)
        return None if close is None else close + 1
    close = raw.find('}', index)
    return None if close < 0 else close + 1


def _shell_word_shape(raw, *, ends_segment=False):
    """``_WordShape`` of one raw shell word.

    Every word that a pathname or brace pattern, a quoted expansion or a
    tilde prefix produces starts with the source word's first produced
    character, so ``src/*.py``, ``"logs/$f"`` and ``~/x`` never become an
    option while ``*.py`` (``-i.py``) and ``"$f"`` may. An unquoted parameter,
    command or arithmetic expansion is field-split, and ``"$@"`` yields one
    word per element: a new word may begin inside either. At the end of a
    segment an unquoted ``$`` is the remnant of a ``$(…)`` that the pipeline
    splitter cut at its parenthesis.
    """
    computed = _shell_expansion_prefix(raw, literal_dollars=True)[1]
    quote, index, first, splits, multiple = None, 0, None, False, False
    while index < len(raw):
        char, end, expansion = raw[index], index + 1, False
        following = raw[index + 1:index + 2]
        if char == '\\' and quote != "'":
            end = index + 2
        elif char in "'\"" and quote in (None, char):
            quote = None if quote else char
            index = end
            continue
        elif quote != "'" and (char == '`' or char == '$' and following in '({'
                               and following):
            expansion, close = True, _expansion_end(raw, index)
            splits = splits or quote is None or (
                following == '{' and '@' in raw[index:close or len(raw)])  # "${a[@]}"
            if close is None:
                multiple = splits = True
            end = close or len(raw)
        elif quote != "'" and char == '$':
            if quote is None and following in ('"', "'"):
                expansion = True  # $'…' and $"…" quote one word
            elif following in _SHELL_EXPANSION_STARTS:
                expansion = True
                splits = splits or quote is None or following == '@'
            elif quote is None and ends_segment and not following:
                expansion = splits = True
        elif quote is None and (char in '*?[' or char == '{' and _brace_expands(raw, index)):
            expansion = multiple = True
        if first is None:
            first = expansion
        index = end
    return _WordShape(computed or splits, bool(first) or splits, multiple or splits)


def command_segment_word_shapes(source):
    """``_WordShape`` of each ``command_segment_words`` word.

    Redirect operators and their targets never reach the program, so they
    are neither hidden options nor extra operands.
    """
    words = _shell_words(source, strict=True, redirects=True)
    end, shapes, target = len(source.rstrip()), [], False
    for word in words:
        raw = source[word.start:word.end]
        shape = _shell_word_shape(raw, ends_segment=word.end >= end)
        operator = raw == word.text and raw.startswith(('<', '>'))
        if operator or target:
            shape = shape._replace(hidden=False, multiple=False)
        shapes.append(shape)
        target = operator
    return tuple(shapes)


# Transparent program wrappers. They change the child's environment or its
# argv[0]; they never change which files the child may write, so admission has
# to judge the wrapped program. Treating the wrapper as an opaque runtime made
# `env -u VAR python3 check.py` a whole-checkout writer for its entire
# supervised lifetime: measured 2026-10-22, one such background command held
# the canonical `tree:` root for 479 s and 2852 of 3948 recorded waits were
# 60 s or longer, blocking every other conversation's Edit and write_file in
# the same checkout.
# `env` options that consume the next word as their value.
_ENV_VALUE_OPTIONS = frozenset({'-u', '--unset', '-C', '--chdir'})


def _env_operand(words, index):
    """Index of the program ``env`` at ``words[index]`` runs, else None."""
    index += 1
    while index < len(words):
        word = words[index]
        if word == '--':
            index += 1
            break
        if word in ('-i', '--ignore-environment', '-0', '--null'):
            index += 1
            continue
        if word in _ENV_VALUE_OPTIONS:
            index += 2
            continue
        if word.startswith('--unset=') or word.startswith('--chdir='):
            index += 1
            continue
        if word in ('-S', '--split-string'):
            return None  # the program is inside a quoted string
        if word.startswith('-'):
            return None  # unknown env option: keep the wrapper opaque
        if _ENV_ASSIGNMENT_GUARD_RE.match(word):
            index += 1
            continue
        break
    return index if index < len(words) else None  # `env` alone runs nothing


# Reserved words that introduce a command: the command after one decides the
# segment's effect, the keyword itself has none. Closing words have no effect
# alone or with input redirects.
_PREFIX_KEYWORDS = frozenset({'do', 'then', 'else', 'elif', 'if', 'while', 'until', '!', '{'})
_CLOSING_KEYWORDS = frozenset({'done', 'fi', 'esac', '}'})
_SHELL_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def control_flow_view(segment):
    """Peel shell control syntax so admission judges the commands that run.

    Returns None for an ordinary command, '' for an effect-free header
    (``for NAME [in WORDS]``, ``select``, ``case WORD in``, or ``done``/``fi``/
    ``esac``/``}`` with at most input redirects), or the source of the command
    a prefix keyword introduces (``do wc -l "$f"`` -> ``wc -l "$f"``).
    Exclusion follows effects, not syntax: fencing a whole checkout because a
    reader sat inside a loop serialized every other conversation behind it. A
    header that runs a substitution or writes output keeps the ordinary verdict.
    """
    try:
        tokens = _shell_words(segment, strict=True, redirects=True)
    except ValueError:
        return None
    if not tokens:
        return None
    keyword = tokens[0].text
    if segment[tokens[0].start:tokens[0].end] != keyword:
        return None  # a quoted word is an ordinary command name
    if keyword in _PREFIX_KEYWORDS:
        return segment[tokens[1].start:].strip() if len(tokens) > 1 else ''
    if keyword not in _CLOSING_KEYWORDS | {'for', 'select', 'case'}:
        return None
    if command_segment_executes_substitution(segment):
        return None
    outputs = command_segment_output_targets(segment)
    words = list(outputs.words)
    if not outputs.complete or outputs.targets:
        return None
    if keyword in _CLOSING_KEYWORDS:
        return '' if words == [keyword] else None
    if keyword == 'case':
        return '' if len(words) >= 3 and words[-1] == 'in' else None
    if (len(words) >= 2 and _SHELL_NAME.match(words[1])
            and (len(words) == 2 or words[2] == 'in')):
        return ''
    return None


def peeled_command(segment):
    """The command that control syntax introduces; '' for an effect-free header.

    Every judge of a segment reads this view, so a reserved word in front
    (``then git reset --hard``, ``{ cd wt``) cannot hide what runs.
    """
    for _ in range(16):
        body = control_flow_view(segment)
        if body is None:
            return segment
        if not body:
            return ''
        segment = body
    return segment


def command_segment_wrapper_view(source):
    """Return the segment text with its transparent program wrappers removed.

    ``env`` and the process wrappers of :func:`process_wrapper_operand` only
    adjust the child's environment, deadline, priority or invocation, so the
    workspace effect of ``env -u VAR timeout 60 prog args`` is the effect of
    ``prog args``. The
    returned view keeps every other byte of the segment, so quoting, operands
    and redirects stay intact.

    Returns *source* unchanged when no wrapper applies, when the wrapper's
    own options are not fully understood (``env -S``), or when the wrapper
    executes nothing (``env`` alone, ``command -v``). An unproven wrapper
    stays visible to the caller, which keeps the conservative verdict.
    """
    if not source:
        return source
    try:
        words = _shell_words(source, strict=True, redirects=True)
    except ValueError:
        return source
    texts = [word.text for word in words]
    index = 0
    while index < len(texts) and _ENV_ASSIGNMENT_GUARD_RE.match(texts[index]):
        index += 1
    unwrapped = False
    for _ in range(_MAX_WRAPPER_DEPTH):
        if index >= len(texts):
            break
        # Privilege wrappers stay visible: `sudo -i` changes the program's cwd.
        if os.path.basename(texts[index]) == 'env':
            operand = _env_operand(texts, index)
        else:
            operand = process_wrapper_operand(texts, index)
        if operand is None:
            break
        index, unwrapped = operand, True
    if not unwrapped or index >= len(words):
        return source
    return source[words[index].start:]


def command_segment_dynamic_kinds(source):
    """Classify the shell-dynamic tokens of one segment.

    The coarse "dynamic syntax" signal cannot tell a command substitution
    (which can execute anything) from an input redirect (which only reads)
    or an unquoted glob (which only produces more words). Admission needs
    that difference: an input redirect and a glob cannot create a file, so
    they must not escalate a proven reader into a whole-checkout writer.

    Returns a frozenset drawn from ``substitution``, ``control``,
    ``redirect_in``, ``redirect_out``, ``heredoc``, ``glob``, ``brace`` and
    ``comment``.
    """
    kinds = set()
    in_single = False
    in_double = False
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == '\\' and not in_single and index + 1 < length:
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if not in_single:
            if char in '$`':
                # The shell expands ``$VAR`` and ``$(…)`` inside double quotes
                # too; only single quotes make them literal.
                kinds.add('substitution')
            elif not in_double:
                if char in '*?[':
                    kinds.add('glob')
                elif char in '{}':
                    kinds.add('brace')
                elif char == '<':
                    # ``<<``/``<<<`` carry an inline body, not a file to read.
                    if source[index + 1:index + 2] == '<':
                        kinds.add('heredoc')
                        index += 2
                        continue
                    kinds.add('redirect_in')
                elif char == '>':
                    kinds.add('redirect_out')
                elif char in ';&|()\n\r':
                    kinds.add('control')
                elif char == '#':
                    kinds.add('comment')
        index += 1
    return frozenset(kinds)


_SUBSTITUTION_UNTRACKED = re.compile(r'(?:^|[\s;&|(])(?:case|esac)(?=[\s;&|)]|$)|<<|(?:^|\s)#')


def _substitution_end(source, start):
    """Index of the ``)`` closing a ``$(`` whose body starts at ``start``."""
    depth, in_single, in_double, index = 1, False, False, start
    while index < len(source):
        char = source[index]
        if char == '\\' and not in_single:
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == '`':
                close = source.find('`', index + 1)
                if close < 0:
                    return None
                index = close
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    body = source[start:index]
                    return None if _SUBSTITUTION_UNTRACKED.search(body) else index
        index += 1
    return None


def _arithmetic_end(source, start):
    """Index after the ``))`` closing a ``$((`` whose expression starts at ``start``."""
    depth, index = 2, start
    while index < len(source):
        char = source[index]
        if char in '`\'"\\' or source.startswith('$(', index):
            return None  # a quoted or nested construct inside arithmetic
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


# What an arithmetic expression may hold without hiding a command: names,
# numbers, operators and blanks; `$` must start a plain variable reference.
_ARITHMETIC_OPERATOR_CHARACTERS = frozenset(' \t+-*/%<>=!~^&|?:,')
_ARITHMETIC_WORD = re.compile(r'[A-Za-z0-9_]+')
_ARITHMETIC_REFERENCE = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]|\{[A-Za-z_][A-Za-z0-9_]*\})')


def shell_arithmetic_is_flat(interpreter='/bin/sh'):
    """Whether *interpreter* reads a variable in arithmetic only as a number.

    bash (also as ``sh`` and with ``--posix``), ksh and zsh evaluate a
    variable's value as an arithmetic expression, and a subscript there runs
    command substitutions: with ``x='a[$(touch r)]'``, ``: $((x))`` creates
    ``r``. dash rejects a value that is not a number ("Illegal number").
    """
    import shutil
    path = interpreter if os.path.isabs(interpreter) else shutil.which(interpreter)
    return bool(path) and os.path.basename(os.path.realpath(path)) == 'dash'


def _inert_arithmetic_end(source, index):
    """Index after the arithmetic expansion ``$((`` at *index* opens, or None.

    Numbers, operators and balanced parentheses yield a number and run
    nothing, and so do variables (bare names, ``$name``, ``${name}``, ``$1``)
    in a shell that reads them as numbers (``shell_arithmetic_is_flat``).
    ``$((cmd) )`` closes with ``) )``, which bash reads as a command
    substitution of a subshell, so it does not qualify; neither do quotes,
    subscripts, ``#`` bases, special parameters or nested expansions.
    """
    index, depth, variables = index + 3, 0, False
    while index < len(source):
        char = source[index]
        word = _ARITHMETIC_WORD.match(source, index)
        if char == '$' or (word and not char.isdigit()):
            reference = word or _ARITHMETIC_REFERENCE.match(source, index)
            if reference is None:
                return None
            index, variables = reference.end(), True
            continue
        if word:
            index = word.end()  # a number; `0x1f` is not a name
            continue
        if char == '(':
            depth += 1
        elif char == ')':
            if not depth:
                if not source.startswith('))', index):
                    return None
                return None if variables and not shell_arithmetic_is_flat() else index + 2
            depth -= 1
        elif char not in _ARITHMETIC_OPERATOR_CHARACTERS:
            return None
        index += 1
    return None


def command_substitution_bodies(source, *, quoted_only=False):
    """``[(start, end, body)]`` for each top-level command substitution, or None.

    ``source[start:end]`` spans the whole substitution, delimiters included, and
    ``body`` is the command text the substituting shell runs (backtick escapes
    removed). With ``quoted_only`` only the bodies the pipeline splitter keeps
    inside one segment are returned: ``$(…)`` inside double quotes and every
    backtick body (an unquoted ``$(`` is already split at its parentheses).
    None means a construct this scanner does not track (an unterminated body,
    ``case``/heredoc/comment syntax inside one, a substitution inside
    arithmetic); callers keep their conservative verdict.
    """
    bodies, in_single, in_double, index, length = [], False, False, 0, len(source)
    while index < length:
        char = source[index]
        if char == '\\' and not in_single:
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and char == '`':
            end = index + 1
            while end < length and source[end] != '`':
                end += 2 if source[end] == '\\' else 1
            raw = source[index + 1:end]
            if end >= length or (in_double and '\\"' in raw) or _SUBSTITUTION_UNTRACKED.search(raw):
                return None
            bodies.append((index, end + 1, re.sub(r'\\([\\`$])', r'\1', raw)))
            index = end + 1
            continue
        elif not in_single and source.startswith('$(', index):
            if source.startswith('$((', index):
                end = _arithmetic_end(source, index + 3)
                if end is None:
                    return None
                index = end
                continue
            end = _substitution_end(source, index + 2)
            if end is None:
                return None
            if in_double or not quoted_only:
                bodies.append((index, end + 1, source[index + 2:end]))
            index = end + 1
            continue
        index += 1
    return None if in_single or in_double else bodies


def command_segment_executes_substitution(source):
    """Whether the segment runs a command while the shell expands it.

    ``command_segment_dynamic_kinds`` reports every unquoted ``$`` as a
    substitution. Only ``$(…)``, backticks, process substitution
    ``<(…)``/``>(…)`` and an arithmetic expansion that may hide one execute
    anything; ``$VAR``/``${VAR}`` and an inert ``$((…))`` only produce words,
    exactly like a glob. Single quotes make all of them literal.
    """
    in_single = in_double = False
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        if char == '\\' and not in_single and index + 1 < length:
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single:
            following = source[index + 1:index + 2]
            if char == '$' and source.startswith('$((', index):
                end = _inert_arithmetic_end(source, index)
                if end is None:
                    return True
                index = end
                continue
            if char == '`' or (char == '$' and following == '('):
                return True
            if not in_double and char in '<>' and following == '(':
                return True
        index += 1
    return False


# ``program_text`` holds the words that are inline program source (a sed script,
# perl -e code): never a path, though a sed script may name its own output
# files as a suffix (``w FILE`` runs to the end of the program).
# ``word_indices`` (set by command_segment_output_targets only) gives the
# position of each word among the segment's shell words, redirects included,
# so word shapes can be aligned with ``words``.
_CommandTargets = namedtuple('_CommandTargets', 'words targets complete program_text word_indices',
                             defaults=((), ()))


def _static_writer_operands(args, flags):
    """Known non-value flags only; never guess unknown option arity."""
    operands = []
    options = True
    for word in args:
        if options and word == '--':
            if operands:
                return None
            options = False
        elif options and word.startswith('-'):
            if operands or word not in flags:
                return None
        else:
            operands.append(word)
    return operands


def _single_writer_option(args, short, long, flags):
    """Parse one value option plus literal operands; duplicates stay opaque."""
    value = None
    operands = []
    index = 0
    while index < len(args):
        word = args[index]
        if operands and word.startswith('-'):
            return None  # option permutation can depend on POSIXLY_CORRECT
        if word == '--':
            operands.extend(args[index + 1:])
            break
        if word == short or word == long:
            index += 1
            if value is not None or index >= len(args):
                return None
            value = args[index]
        elif word.startswith(long + '=') or (word.startswith(short) and len(word) > len(short)):
            if value is not None:
                return None
            value = word.split('=', 1)[1] if word.startswith(long + '=') else word[len(short):]
        elif word.startswith('-'):
            if word not in flags:
                return None
        else:
            operands.append(word)
        index += 1
    return (value, operands) if value is not None else None


def _find_parts(args):
    """One bounded parser for search roots, link policy and the expression."""
    index, follows_links = 0, False
    while index < len(args):
        arg = args[index]
        if arg in {'-H', '-L', '-P'}:
            follows_links = arg == '-L'
            index += 1
        elif re.fullmatch(r'-O[0-3]', arg):
            index += 1
        elif arg == '-D' and index + 1 < len(args):
            index += 2
        elif arg == '--':
            index += 1
            break
        else:
            break
    roots = []
    while index < len(args) and not args[index].startswith('-') and args[index] not in {'!', '('}:
        roots.append(args[index])
        index += 1
    if not roots and index < len(args) and args[index].startswith('-O'):
        return None
    return roots or ['.'], args[index:], follows_links


def _find_write_rows(expression):
    """Identify action boundaries without interpreting predicate data as flags."""
    values = {'-name', '-iname', '-path', '-ipath', '-wholename', '-iwholename',
              '-lname', '-ilname', '-regex', '-iregex', '-type', '-xtype', '-user',
              '-uid', '-group', '-gid', '-perm', '-size', '-links', '-inum',
              '-samefile', '-newer', '-anewer', '-cnewer', '-atime', '-mtime',
              '-ctime', '-amin', '-mmin', '-cmin', '-maxdepth', '-mindepth',
              '-regextype', '-fstype', '-printf', '-used', '-context'}
    rows, index, follows_links = [], 0, False
    while index < len(expression):
        arg = expression[index]
        if arg == '-follow':
            follows_links = True
            index += 1
        elif arg in {'-exec', '-execdir', '-ok', '-okdir'}:
            end = index + 1
            while end < len(expression) and expression[end] not in {';', '+'}:
                end += 1
            rows.append((index, end))
            index = end + 1
        elif arg in {'-fprint', '-fprint0', '-fls', '-fprintf'}:
            rows.append((index, min(len(expression), index + (3 if arg == '-fprintf' else 2))))
            index += 3 if arg == '-fprintf' else 2
        elif arg == '-delete':
            rows.append((index, index + 1))
            index += 1
        else:
            index += 2 if arg in values or re.fullmatch(r'-newer[a-zA-Z]{2}', arg) else 1
    return rows, follows_links


def command_segment_output_targets(source):
    """Shell output effects independent of the invoked utility's own write set."""
    try:
        tokens = _shell_words(source, strict=True, redirects=True)
    except ValueError:
        return _CommandTargets((), (), False)
    words, redirects, kept = [], [], []
    complete, index = True, 0
    while index < len(tokens):
        word = tokens[index]
        token = word.text
        raw = source[word.start:word.end]
        if raw == token and token in {'>', '>>', '<', '<<', '<<<', '<>'}:
            if (index and words and words[-1].isdigit()
                    and tokens[index - 1].end == word.start
                    and source[tokens[index - 1].start:word.start].isdigit()):
                words.pop()  # Only an adjacent, unquoted number is an IO descriptor.
                kept.pop()
            if index + 1 >= len(tokens):
                return _CommandTargets(tuple(words), tuple(redirects), False, (), tuple(kept))
            value_word = tokens[index + 1]
            value = value_word.text
            value_source = source[value_word.start:value_word.end]
            if token in {'>', '>>', '<>'}:
                if value_source.startswith('&'):
                    # Known stdout/stderr duplication creates no named file.
                    # Other inherited descriptors or legacy >&file are opaque.
                    if token != '>' or value_source not in {'&1', '&2', '&-'}:
                        complete = False
                elif value != '/dev/null':
                    redirects.append(value)
                    if shell_source_has_expansion(value_source):
                        complete = False
            index += 2
        elif raw == token and token.startswith(('>', '<')):
            return _CommandTargets(tuple(words), tuple(redirects), False, (), tuple(kept))
        else:
            words.append(token)
            kept.append(index)
            index += 1
    return _CommandTargets(tuple(words), tuple(redirects), complete, (), tuple(kept))


def command_segment_discardable_redirect_split(source):
    """Split trailing output redirects that open no file from the command.

    Returns ``(core, suffix)`` when every redirect is ``/dev/null`` or a
    descriptor dup AND all of them trail the last word. A redirect in the
    middle of a command, or any named output file, stays unresolved (None) so
    callers keep ordinary writer admission.
    """
    if not source or not source.strip() or '\n' in source or '\r' in source:
        return None  # A heredoc body is executable sh; it is never a redirect.
    outputs = command_segment_output_targets(source)
    if not outputs.complete or outputs.targets or not outputs.words:
        return None
    try:
        tokens = _shell_words(source, strict=True, redirects=True)
    except ValueError:
        return None
    # Input redirects and heredocs stay in the command: a here-document body is
    # shell source, not a discarded descriptor.
    first = next((index for index, token in enumerate(tokens)
                  if token.text in {'>', '>>', '<>'}
                  and source[token.start:token.end] == token.text), None)
    if first is None:
        return None
    split_at = tokens[first].start
    if (first and tokens[first - 1].text.isdigit()
            and tokens[first - 1].end == split_at):
        split_at = tokens[first - 1].start  # Adjacent IO descriptor digit.
    core, suffix = source[:split_at].strip(), source[split_at:].strip()
    if not core or not suffix or '\n' in core or '<' in suffix:
        return None
    tail = command_segment_output_targets(suffix)
    if not tail.complete or tail.targets or tail.words:
        return None  # Not redirect-only: a word follows the redirect.
    return core, suffix

def command_segment_targets(source):
    """Bounded literal utility writes; incomplete parses retain candidate roots.

    `complete` is snapshot precision, not authorization or lease exemption.
    Unknown mutating options retain every literal candidate rather than guessing
    an option's arity. Opaque runtimes remain the caller's scheduling policy.
    Inline program text (``program_text``) is never a candidate path.
    """
    outputs = command_segment_output_targets(source)
    words, redirects = list(outputs.words), list(outputs.targets)
    while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
        words.pop(0)
    words = _unwrap_command_parts(words)
    if not words:
        return _CommandTargets((), tuple(redirects), True)
    base, args = os.path.basename(words[0]), words[1:]
    targets = list(redirects)
    complete = outputs.complete and not shell_source_has_expansion(source)

    def uncertain():
        # Long option values may themselves select an external destination.
        candidates = [arg.split('=', 1)[1] if arg.startswith('--') and '=' in arg
                      else arg for arg in args if not arg.startswith('-')
                      or (arg.startswith('--') and '=' in arg)]
        return _CommandTargets(tuple(words), tuple(dict.fromkeys(targets + candidates)), False)

    if base in {'touch', 'tee', 'mkdir'}:
        flags = ({'-a', '-c', '-m', '--no-create'} if base == 'touch' else
                 {'-a', '-i', '--append', '--ignore-interrupts'} if base == 'tee' else
                 {'-p', '--parents', '-v', '--verbose'})
        operands = _static_writer_operands(args, flags)
        if operands is None:
            return uncertain()
        targets.extend(operands)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'truncate':
        option = _single_writer_option(args, '-s', '--size', {'-c', '--no-create'})
        if option is None:
            return uncertain()
        targets.extend(option[1])
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'sort':
        short_values = set('okStT')
        short_flags = set('bdfgiMhnRrsuVzcmC')
        long_values = {'--output', '--key', '--field-separator', '--buffer-size',
                       '--temporary-directory', '--batch-size', '--parallel',
                       '--compress-program', '--random-source', '--files0-from', '--sort'}
        long_flags = {'--reverse', '--numeric-sort', '--general-numeric-sort',
                      '--human-numeric-sort', '--month-sort', '--version-sort',
                      '--random-sort', '--ignore-leading-blanks', '--dictionary-order',
                      '--ignore-case', '--ignore-nonprinting', '--stable', '--unique',
                      '--zero-terminated', '--merge', '--check', '--help', '--version', '--debug'}
        index = 0
        while index < len(args):
            arg = args[index]
            index += 1
            if arg == '--':
                break
            if arg.startswith('--'):
                option, separator, value = arg.partition('=')
                if option in long_values:
                    if not separator:
                        if index >= len(args):
                            return uncertain()
                        value = args[index]
                        index += 1
                    if option in {'--output', '--temporary-directory'}:
                        targets.append(value)
                    # Helper programs cannot borrow a harmless read exemption.
                    if option in {'--compress-program', '--random-source', '--files0-from'}:
                        return uncertain()
                elif option not in long_flags or separator:
                    return uncertain()
            elif arg.startswith('-') and arg != '-':
                position = 1
                while position < len(arg):
                    flag = arg[position]
                    position += 1
                    if flag in short_values:
                        value = arg[position:]
                        if not value:
                            if index >= len(args):
                                return uncertain()
                            value = args[index]
                            index += 1
                        if flag in {'o', 'T'}:
                            targets.append(value)
                        break
                    if flag not in short_flags:
                        return uncertain()
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base in {'cp', 'mv'}:
        operands, destination = [], None
        index, options = 0, True
        flags = (set('abdfHilLnprRsTuvxP') if base == 'cp' else set('bfinTuv'))
        long_flags = {'--force', '--interactive', '--no-clobber', '--verbose',
                      '--no-target-directory', '--backup', '--update', '--help', '--version'}
        if base == 'cp':
            long_flags |= {'--archive', '--recursive', '--dereference', '--no-dereference',
                           '--preserve', '--parents', '--link', '--symbolic-link',
                           '--one-file-system', '--remove-destination', '--copy-contents'}
        while index < len(args):
            arg = args[index]
            index += 1
            if options and arg == '--':
                options = False
            elif options and arg.startswith('--'):
                option, separator, value = arg.partition('=')
                if option in {'--target-directory', '--suffix'}:
                    if not separator:
                        if index >= len(args):
                            return uncertain()
                        value = args[index]
                        index += 1
                    if option == '--target-directory':
                        destination = value
                elif option not in long_flags and option not in {'--reflink', '--sparse', '--no-preserve'}:
                    return uncertain()
            elif options and arg.startswith('-') and arg != '-':
                position = 1
                while position < len(arg):
                    flag = arg[position]
                    position += 1
                    if flag in {'t', 'S'}:
                        value = arg[position:]
                        if not value:
                            if index >= len(args):
                                return uncertain()
                            value = args[index]
                            index += 1
                        if flag == 't':
                            destination = value
                        break
                    if flag not in flags:
                        return uncertain()
            else:
                operands.append(arg)
        if destination is None:
            if len(operands) < 2:
                return uncertain()
            destination = operands.pop()
        targets.append(destination)
        if base == 'mv':
            targets.extend(operands)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'sed':
        invocation = _sed_invocation(args)
        if invocation is None:
            return uncertain()
        # Program text is never a path; the files its w commands name are.
        targets.extend(_sed_written_paths(invocation))
        # A hidden option may switch on an in-place edit of every operand.
        shapes = command_segment_word_shapes(source)
        shapes = [shapes[index] for index in outputs.word_indices][len(outputs.words) - len(args):]
        proven = literal_sed_effects(args, shapes=shapes) is not None
        if not proven:
            # As for any unproven utility, every file the invocation names stays a candidate.
            targets.extend((*invocation.operands, *invocation.script_files))
        return _CommandTargets(tuple(words), tuple(dict.fromkeys(targets)), complete and proven,
                               invocation.programs)
    if base == 'perl':
        one_liner = _perl_one_liner(args)
        if one_liner is not None:
            inplace, operands, code = one_liner
            if inplace:
                targets.extend(operands)
            return _CommandTargets(tuple(words), tuple(targets), False, code)
    if base == 'sd':
        invocation = _sd_invocation(args)
        if invocation is None:
            return uncertain()
        if invocation.writes:
            targets.extend(invocation.files)
        return _CommandTargets(tuple(words), tuple(targets), complete, invocation.program)
    if base.endswith('awk'):
        files = _awk_inplace_operands(args)
        if files is not None:
            targets.extend(files)
            return _CommandTargets(tuple(words), tuple(targets), complete)
        return _CommandTargets(tuple(words), tuple(targets), complete)
    if base == 'env' and args:
        return _CommandTargets(tuple(words), tuple(targets), False)
    if base == 'find':
        parsed = _find_parts(args)
        if parsed is None:
            return uncertain()  # Unknown optimizer grammar cannot erase peer operands.
        roots, expression, _follows_links = parsed
        rows, _expression_follows_links = _find_write_rows(expression)
        if rows:
            targets.extend(roots)
            program_text = []
            for index, end in rows:
                arg = expression[index]
                if arg in {'-fprint', '-fprint0', '-fprintf', '-fls'} and index + 1 < len(expression):
                    targets.append(expression[index + 1])
                if arg in {'-exec', '-execdir', '-ok', '-okdir'}:
                    nested = command_segment_targets(shlex.join(expression[index + 1:end]))
                    targets.extend(path for path in nested.targets if '{}' not in path)
                    program_text.extend(nested.program_text)
                    if not nested.complete:
                        targets.extend(path for path in expression[index + 2:end]
                                       if not path.startswith('-') and '{}' not in path
                                       and path not in nested.program_text)
            return _CommandTargets(tuple(words), tuple(dict.fromkeys(targets)), complete,
                                   tuple(program_text))
        return _CommandTargets(tuple(words), tuple(targets), complete)
    mode = _WRITE_TARGET_COMMANDS.get(base)
    if mode == 'all_args':
        targets.extend(arg for arg in args if not arg.startswith('-'))
    elif mode == 'last_arg':
        operands = [arg for arg in args if not arg.startswith('-')]
        targets.extend(operands[-1:])
    elif mode == 'opaque' or base not in _READONLY_COMMANDS:
        return _CommandTargets(tuple(words), tuple(targets), False)
    return _CommandTargets(tuple(words), tuple(targets), complete)


def command_segment_literal_targets(source, *, shell_outputs_only=False):
    """Derive literal targets or bounded expansion parents from source words.

    A dynamic source operand must not erase a literal destination in another
    checkout. Single-quoted/escaped dollar signs are filenames, not expansion.
    Fully dynamic names remain covered by the caller's conservative cwd claim;
    this cooperative parser is not a sandbox for arbitrary runtime expansion.

    ``shell_outputs_only`` keeps a path only when every word producing it is
    the operand of an output redirect (``>``, ``>>``, ``<>``). The shell opens
    one file there and never removes or replaces a directory, so a TREE claim
    on such a path is scheduling scope. A path any other word also produces
    (a utility operand, even with the same text) is excluded.
    """
    parsed = command_segment_targets(source)
    try:
        words = _shell_words(source, strict=True, redirects=True)
    except ValueError:
        return ()
    redirected = {index + 1 for index, word in enumerate(words)
                  if word.text in {'>', '>>', '<>'} and source[word.start:word.end] == word.text}
    # A sed ``w FILE`` runs to the end of its program, so it is a suffix of the
    # program's word, exactly as an attached option value (``-ofile``) is.
    embedding = set(parsed.program_text)
    result, operand_paths = set(), set()
    for target in parsed.targets:
        origins = [index for index, word in enumerate(words) if word.text == target
                   or ((word.text.startswith('-') or word.text in embedding)
                       and word.text.endswith(target))]
        produced = set()
        if not origins:
            if target == '.':
                produced.add(target)  # find's implicit search root
        elif any(not shell_source_has_expansion(source[words[index].start:words[index].end])
                 for index in origins):
            produced.add(target)
        else:
            # Use source provenance, not the decoded target's first dollar: a
            # quoted checkout name may itself contain literal expansion markers.
            for index in origins:
                word = words[index]
                prefix, expanded = _shell_expansion_prefix(source[word.start:word.end])
                if expanded:
                    target_prefix = prefix[len(word.text) - len(target):]
                    parent = os.path.dirname(target_prefix)
                    if parent:
                        produced.add(parent)
        result |= produced
        if not origins or not redirected.issuperset(origins):
            operand_paths |= produced
    return tuple(sorted(result - operand_paths if shell_outputs_only else result))


def _extract_write_targets(command, cwd=''):
    """Parse a shell command and return the set of file paths it WRITES to.

    Returns:
        set[str] | None
        - set of relative paths that the command writes to (may be empty
          if the command is read-only)
        - None if the command is opaque (interpreter, build tool, etc.)
          and we cannot determine specific targets — meaning ANY file
          in the project could be modified.

    This is used to filter snapshot diffs: only files in the returned set
    (or all files if None) are reported as changed. None does not mean that
    known writes are absent: admission must use command_segment_literal_targets
    to retain explicit destinations even when other effects are opaque.
    """
    if not command or not command.strip():
        return set()

    targets = set()
    for segment in _split_pipeline(command):
        parsed = command_segment_targets(segment)
        if parsed.words and os.path.basename(parsed.words[0]) == 'git':
            if (_classify_git_invocation(list(parsed.words[1:])) != 'readonly'
                    or shell_source_has_expansion(segment)):
                return None
        elif not parsed.complete:
            return None
        targets.update(parsed.targets)
    return targets


def _filter_changes_by_targets(changes, write_targets, cwd):
    """Filter snapshot-diff changes to only include plausible write targets.

    Args:
        changes: list of {rel_path, change_type} from _diff_snapshots
        write_targets: set of paths from _extract_write_targets, or None
            (None = opaque command, keep all changes)

    Returns:
        Filtered list of changes.
    """
    if write_targets is None:
        # Opaque command — keep all changes (can't filter)
        return changes

    if not write_targets:
        # Command is purely read-only but was snapshot'd anyway (edge case)
        return []

    # Normalize targets: resolve to relative paths from project root,
    # expand directories (a target of 'src/' should match 'src/foo.py')
    normalized = set()
    dir_prefixes = []
    for t in write_targets:
        # Strip quotes that might be in the command
        t = t.strip('"\'')
        # Resolve relative to cwd within the project
        if os.path.isabs(t):
            normalized.add(t)
        else:
            normalized.add(os.path.normpath(t))
        # If target looks like a directory (ends with / or is a known dir),
        # also match children
        if t.endswith('/'):
            dir_prefixes.append(os.path.normpath(t) + '/')
        # Also try it as a dir prefix (for 'rm -rf dir' where dir has no /)
        dir_prefixes.append(os.path.normpath(t) + '/')

    filtered = []
    for ch in changes:
        rel = ch['rel_path']
        norm_rel = os.path.normpath(rel)
        # Direct match
        if norm_rel in normalized:
            filtered.append(ch)
            continue
        # Directory prefix match (target is a parent dir)
        if any(norm_rel.startswith(dp) for dp in dir_prefixes):
            filtered.append(ch)
            continue
        # Glob match for patterns like *.pyc
        if any('*' in t and __import__('fnmatch').fnmatch(norm_rel, t)
               for t in write_targets):
            filtered.append(ch)
            continue
    return filtered


def _is_destructive_command(command):
    """Analyze whether a shell command could modify the filesystem.

    Returns True if the command is potentially destructive and file
    change tracking (snapshot/diff) should be performed.

    This is a best-effort snapshot heuristic, not a safety/admission proof.
    Common inspection/filter binaries skip tracking even though some options
    or embedded programs can write. Unknown binaries, interpreters and build
    tools are assumed destructive. Writer exclusion uses the separate
    fail-closed lease parser, never this heuristic.
    """
    if not command or not command.strip():
        return False

    cmd = command.strip()

    # Shell expansion can execute writes before any nominally readonly verb.
    # Conservatively reject even quoted expansion markers; token-only parsing
    # is not evidence that a command substitution or variable is harmless.
    if shell_source_has_expansion(cmd, pathname=False):
        return True

    # Output redirection → always destructive (writes to file).
    # First strip harmless redirects to /dev/null (e.g. 2>/dev/null),
    # then check if any real file-writing redirects remain.
    cmd_no_devnull = _REDIRECT_TO_DEV_NULL.sub('', cmd)
    if _REDIRECT_PATTERN.search(cmd_no_devnull):
        return True

    # sed -i (in-place edit) → destructive even though sed itself is a filter
    if _SED_INPLACE.search(cmd):
        return True

    # gawk/goawk -i inplace → destructive for the same reason as sed -i
    if _AWK_INPLACE.search(cmd):
        return True

    # Split pipeline into individual commands and check each segment
    # e.g. "grep foo | sort | wc -l" → ['grep', 'sort', 'wc']
    segments = _split_pipeline(cmd)
    for seg in segments:
        if not seg:
            continue
        # Strip leading env vars (FOO=bar cmd ...)
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        # Get base command name
        try:
            parts = command_segment_words(seg)
        except ValueError:
            return True
        if not parts:
            continue
        base = parts[0].split('/')[-1]  # handle /usr/bin/rm → rm

        # env with arguments may execute a writer (env git stash apply).
        if base == 'env' and len(parts) > 1:
            return True

        # ── Special-case: git ──
        if base == 'git':
            words = [word.text for word in _shell_words(seg)]
            if (_classify_git_invocation(words[1:]) == 'readonly'
                    and not _re.search(r'[$`]', seg)):
                continue
            # Any other git sub-command (including destructive ones
            # and unknown future ones) → destructive
            return True

        # sd is a stdin filter unless it names FILES, which it rewrites in place.
        if base == 'sd':
            parsed = command_segment_targets(seg)
            if parsed.targets or not parsed.complete:
                return True
            continue

        # cd/pushd/popd only move the shell (expansions and redirects were
        # judged above); the commands that follow decide. Kept out of
        # _READONLY_COMMANDS so write-target derivation still treats a cd as
        # moving its relative targets.
        if base in ('cd', 'pushd', 'popd'):
            continue

        # ── Check readonly whitelist ──
        if base in _READONLY_COMMANDS:
            continue  # this segment is safe

        # ── Everything else → destructive ──
        # This includes: interpreters (python, node, ruby, perl, bash),
        # build tools (make, cmake, cargo, go), package managers (npm, pip),
        # file ops (rm, mv, cp, touch, chmod, tar, …), and any unknown binary.
        return True


# ── Catastrophic top-level deletion guard ───────────────────────────
# Deleting a first-level directory (``/mnt``, ``/home``, ``/data`` …) or
# the filesystem root itself is never a legitimate agent action — it is
# the failure mode that wiped other teams' paths during a benchmark run.
# We refuse it OUTRIGHT (no trash rewrite, no approval bypass), parsing
# the actual ``rm``/``rmdir`` arguments so flag order can't smuggle it
# past us (``rm -fr /mnt`` == ``rm /mnt -r -f``).
_DELETE_COMMANDS = frozenset({'rm', 'rmdir', 'unlink'})

# Number of leading path components a delete target must have to be
# considered "deep enough" to allow. ``/`` → 0, ``/mnt`` → 1 (blocked),
# ``/mnt/foo`` → 2 (allowed). Tunable via env for stricter sites.
_MIN_DELETE_DEPTH = max(1, int(os.environ.get('TOFU_MIN_DELETE_DEPTH', '2')))

# Privilege wrappers that can precede the real command word. A delete behind
# one (``sudo rm -rf /``) must be judged by the ``rm``, not by the ``sudo`` —
# otherwise a one-word prefix smuggles a catastrophic delete past every
# argument-level guard. (The retired substring guard ``\brm\s+-rf\s+/``
# caught this shape precisely because it never parsed; when the regex was
# removed in favour of this parser, seeing through sudo/doas is what keeps
# coverage neutral-or-better.) Their options follow each program's documented
# grammar (``_PRIVILEGE_GRAMMARS``), so an option value (``sudo -D /tmp rm …``,
# ``sudo --user root rm …``) is never mistaken for the command word.
_PRIV_WRAPPERS = frozenset({'sudo', 'doas'})


def _unwrap_command_parts(parts):
    """Strip a leading privilege wrapper (``sudo``/``doas`` + its options).

    Returns the token list of the wrapped command, or *parts* unchanged when
    no wrapper is present. sudo's shell-assignment ``VAR=value`` words
    directly before the command stay in front of it. An option outside the
    wrapper's grammar counts as a flag in this best-effort view; the guards'
    :func:`_program_views` treats it as not understood instead. Only ONE
    wrapper level is unwrapped; anything more exotic (``xargs rm``,
    ``find -exec rm``) is out of scope for this best-effort net. Effect
    analysis that must also see through process wrappers (``timeout 5 rm``)
    uses :func:`unwrap_process_wrappers`.
    """
    if not parts or parts[0].split('/')[-1] not in _PRIV_WRAPPERS:
        return parts
    return parts[_privilege_wrapper_parse(parts, strict=False)[0]:]


# Process wrappers run their operand with the caller's cwd, environment and
# descriptors, adding only a deadline (timeout), a scheduling priority (nice),
# a hang-up disposition (nohup), stream buffering (stdbuf), a resource report
# on stderr (time) or a lookup rule (command, exec). The operand decides the
# workspace effect: `timeout 900 git switch` transforms the checkout like
# `git switch`, and `nice git reset --hard` destroys what `git reset --hard`
# destroys. Judging the wrapper word instead admitted both with no lease and
# past the shared-checkout guard (observed 2026-10-25).
_PROCESS_WRAPPERS = frozenset({'command', 'exec', 'nice', 'nohup', 'stdbuf', 'time', 'timeout'})
_WRAPPER_DURATION_RE = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[smhd]?\Z|inf(?:inity)?\Z')
_NICE_ADJUSTMENT_RE = re.compile(r'-n-?[0-9]+\Z|--adjustment=-?[0-9]+\Z|--?[0-9]+\Z')
_STDBUF_MODE_RE = re.compile(r'-[ioe].+\Z|--(?:input|output|error)=.+\Z')
_TIMEOUT_OPTION_RE = re.compile(r'-[sk].+\Z|--(?:signal|kill-after)=.+\Z')
_MAX_WRAPPER_DEPTH = 8


def process_wrapper_operand(words, index=0):
    """Index of the program run by the process wrapper at ``words[index]``.

    ``None`` when that word is no process wrapper, when its options are not
    fully understood, or when it runs nothing (``command -v``, ``nice``
    alone, ``timeout`` without a duration); the caller then keeps judging the
    wrapper word. ``exec -c`` (empty environment) and GNU ``time -o FILE``
    (writes a report) are never transparent.
    """
    if index >= len(words):
        return None
    name = os.path.basename(words[index])
    if name not in _PROCESS_WRAPPERS:
        return None
    position = index + 1

    def options():
        return position < len(words) and words[position].startswith('-') and words[position] != '-'

    if name == 'timeout':
        while options():
            word = words[position]
            if word == '--':
                position += 1
                break
            if word in ('-s', '--signal', '-k', '--kill-after'):
                position += 2
            elif (word in ('--preserve-status', '--foreground', '-v', '--verbose')
                  or _TIMEOUT_OPTION_RE.match(word)):
                position += 1
            else:
                return None
        if position >= len(words) or not _WRAPPER_DURATION_RE.match(words[position]):
            return None
        position += 1
    elif name == 'nice':
        while options():
            word = words[position]
            if word == '--':
                position += 1
                break
            if word in ('-n', '--adjustment'):
                position += 2
            elif _NICE_ADJUSTMENT_RE.match(word):
                position += 1
            else:
                return None
    elif name == 'stdbuf':
        modes = 0
        while options():
            word = words[position]
            if word == '--':
                position += 1
                break
            if word in ('-i', '-o', '-e', '--input', '--output', '--error'):
                position += 2
            elif _STDBUF_MODE_RE.match(word):
                position += 1
            else:
                return None
            modes += 1
        if not modes:
            return None  # stdbuf refuses to run without a buffering mode
    elif name == 'time':
        while options():
            word = words[position]
            if word == '--':
                position += 1
                break
            if word in ('-p', '--portability', '-v', '--verbose', '-q', '--quiet'):
                position += 1
            elif word in ('-f', '--format'):
                position += 2
            elif word.startswith('--format='):
                position += 1
            else:
                return None
    elif name == 'nohup':
        if options():
            if words[position] != '--':
                return None
            position += 1
    elif name == 'command':
        while options():
            word = words[position]
            if word == '--':
                position += 1
                break
            if set(word[1:]) - {'p'}:
                return None  # -v/-V describe the command instead of running it
            position += 1
    else:  # exec
        while options():
            word = words[position]
            if word == '--':
                position += 1
                break
            if word == '-a':
                position += 2
            elif word == '-l':
                position += 1
            else:
                return None
    return position if position < len(words) else None


def unwrap_process_wrappers(words):
    """Words of the program that actually runs, privilege and process
    wrappers removed (at most ``_MAX_WRAPPER_DEPTH`` levels).

    ``env`` stays: callers that track the child's environment interpret it.
    """
    words = list(words)
    for _ in range(_MAX_WRAPPER_DEPTH):
        words = _unwrap_command_parts(words)
        operand = process_wrapper_operand(words)
        if operand is None:
            break
        words = words[operand:]
    return words


# ── Literal shell-state tracking for delete-target resolution ──────
#
# The second-generation guard expanded ``$VAR`` with ``os.path.expandvars``
# against the SERVER process environment, so a variable assigned earlier in
# the same command (``D=/tmp/x; rm -rf $D``) was invisible, stayed a literal
# ``$D`` and tripped the "unresolved -> root-ish" refusal — a false positive
# measured in conv mvl6adj1bcd1r1 (2026-10-24). The same naive tokenization
# (``str.split``, no dequoting) let a quoted target (``rm -rf "/mnt"``) or a
# ``..`` hop (``rm -rf /tmp/../mnt``) skip the rule entirely.
#
# The rules below track what the shell will ACTUALLY expand:
#
#   * a word expands the way the shell expands it (``_expand_word``): quotes
#     and escapes, ``$NAME``, the positional parameters (``$1``, ``"$@"``),
#     the ``${…}`` operators (``${D:-x}``, ``${D:?}``, ``${D%/*}`` …), command
#     substitutions and a leading ``~``, then field splitting and pathname
#     expansion of unquoted results. A substitution whose output is known
#     prints it (``pwd``, ``mktemp``, ``echo``/``printf``/``dirname``/
#     ``basename`` of known words, ``cd DIR && pwd``, ``git rev-parse
#     --show-toplevel``); any other one is DYNAMIC;
#   * a dynamic/unset variable is judged as possibly EMPTY (the Steam
#     ``$STEAMROOT/`` bug class): ``rm -rf "$D/"`` with ``D`` empty deletes
#     ``/``; a suffix deep enough to survive the collapse (``$D/build/out``)
#     is allowed, as is a bare ``"$D"`` (an empty operand deletes nothing).
#     A value built from one keeps that shape: after ``E="$D/x"``, ``"$E"``
#     is judged as ``/x`` if ``$D`` expands empty;
#   * literal ``cd`` hops update the tracked cwd set (with the failure
#     fallback kept across ``;``/``||``) so ``cd / && rm -rf mnt`` is judged
#     as a delete of ``/mnt``;
#   * relative operands are judged against the tracked cwd (``rm -rf ../../..``
#     from a deep workspace climbs to a shallow path and is refused);
#   * deleting the home directory, the tracked cwd, or an ancestor of it is
#     refused even when the numeric depth rule would pass;
#   * the state follows the shell's control flow (see "Delete operands come
#     only from commands a shell would run" below): an assignment that may
#     not run never replaces the value the name held before;
#   * a name the command never bound holds its value in the environment the
#     command's shell starts with, which the caller passes (run_command: the
#     child environment it built); a caller that does not know it (the
#     cwd-less pre-hook) leaves every operand reading it to the executor;
#   * builtins change exactly what they change: assignments and declarations
#     bind (``export``/``declare -x`` also export), ``unset`` empties a name,
#     ``read``/``getopts``/``printf -v``/``mapfile`` make it dynamic,
#     ``let``/``((…))``/``source`` may change it, ``set --``/``shift`` and a
#     function call set the positional parameters, ``cd``/``pushd``/``popd``
#     move, and ``eval`` runs the text its operands expand to;
#   * a child shell (``sh -c``, ``bash <<EOF``) sees the exported names, the
#     functions of ``export -f`` and its own positional parameters; a
#     ``trap`` action runs later in this shell, with any state reached after
#     the trap was set.
#
# A value set is a tuple of candidate strings in which ``_DYNAMIC`` stands
# for "or no provable literal value" (a path left the name dynamic or
# unset), ``_TRUSTED`` for "or an unknown value that is never empty", and a
# ``_Collapsed`` for the empty collapse of a value built from unproven
# names; None is the set holding only ``_DYNAMIC``. ``_DEFERRED`` is a
# value read from an environment the caller does not know. Sets stay
# bounded; when they overflow the name simply loses its proven value: it
# holds ``_OVERFLOW``, judged like ``_DYNAMIC`` but kept by every join, so a
# loop that keeps growing a value (``n=x$n``) reaches its fixpoint.
_DELETE_BINDING_CAP = 8
_CWD_CAP = 8
_MKSTEMP_VALUE = '/tmp/.tofu-delete-guard-mktemp'  # depth-2 placeholder
_DECLARATION_BUILTINS = frozenset({'export', 'readonly', 'declare', 'typeset', 'local'})
# An assignment in front of one of these may outlive the command (POSIX).
_SPECIAL_BUILTINS = frozenset({
    ':', '.', 'break', 'continue', 'eval', 'exec', 'exit', 'export', 'readonly',
    'return', 'set', 'shift', 'times', 'trap', 'unset'})
_ASSIGNMENT_WORD = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)(\+?)=')
_SHELL_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


class _ValueMarker:
    """A value-set element that is no literal string."""

    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


_DYNAMIC = _ValueMarker('_DYNAMIC')
_DEFERRED = _ValueMarker('_DEFERRED')
_OVERFLOW = _ValueMarker('_OVERFLOW')
# A value this guard cannot know but the shell guarantees is not empty (the
# non-empty case of ``${D:-x}``, ``$(dirname …)``): no collapse to judge.
_TRUSTED = _ValueMarker('_TRUSTED')


class _Collapsed:
    """The value a binding holds when the unproven *names* it was built from
    expand empty (``E="$D/x"`` with ``D`` dynamic holds ``/x``)."""

    __slots__ = ('text', 'names')

    def __init__(self, text, names):
        self.text = text
        self.names = frozenset(names)

    def __eq__(self, other):
        return (isinstance(other, _Collapsed) and self.text == other.text
                and self.names == other.names)

    def __hash__(self):
        return hash((self.text, self.names))

    def __repr__(self):
        return f'_Collapsed({self.text!r}, {sorted(self.names)})'


class _EnvironmentDeferred(Exception):
    """An expansion read the environment or cwd of a caller that does not
    know them; the executor's pass judges that operand."""


def _mkstemp_value(source):
    """A bounded proof that ``$(mktemp …)`` yields a fresh deep path, or None."""
    body = source.strip()
    if body.startswith('mktemp'):
        body = body[len('mktemp'):]
    elif body.startswith('command mktemp'):
        body = body[len('command mktemp'):]
    else:
        return None
    try:
        words = [w.text for w in _shell_words(body, strict=True)]
    except ValueError:
        return None
    tmpdir = None
    index = 0
    while index < len(words):
        word = words[index]
        if word in ('-d', '-u', '-t', '-q', '--directory', '--dry-run',
                    '--quiet', '--suffix'):
            index += 1
            continue
        if word in ('-p', '--tmpdir'):
            if index + 1 >= len(words):
                return None
            tmpdir = words[index + 1]
            index += 2
            continue
        if word.startswith('--tmpdir='):
            tmpdir = word.split('=', 1)[1]
            index += 1
            continue
        if word.startswith('-'):
            return None  # unknown option: stay conservative
        index += 1  # a TEMPLATE operand; its exact text never shallowens
    if tmpdir and tmpdir.count('/') < 1:
        return None
    if tmpdir and not tmpdir.startswith('/'):
        return None
    return os.path.join(tmpdir.rstrip('/') if tmpdir else '/tmp',
                        '.tofu-delete-guard-mktemp')


def _resolve_delete_value(raw, state, home):
    """Resolve one assignment VALUE text to its value set.

    ``raw`` is the RAW text after ``NAME=`` (quotes intact — an assignment
    word is never field-split or globbed, so ``D="a b"`` binds ``a b``). The
    set holds literal candidates and, for a candidate built from a name or a
    command substitution without a provable value, its empty collapse
    (``_Collapsed``, or ``_DYNAMIC`` when that collapse is empty itself);
    None when only ``_DYNAMIC`` remains. Returns ``_OVERFLOW`` past the
    candidate bound and ``_DEFERRED`` when it reads an environment the
    caller does not know. Values may reference earlier bindings
    (``BAD=$R/frontend``).
    """
    try:
        values = _expand_word(raw, state, home, split=False).values()
    except _EnvironmentDeferred:
        return _DEFERRED
    except _Overflowed:
        return _OVERFLOW
    except _Unenumerable:
        return None
    return _value_set(values)


_Expansion = namedtuple('_Expansion', ['candidates', 'unproven', 'collapsed'])
_Field = namedtuple('_Field', ['text', 'names', 'glob', 'trusted'])
_Alternative = namedtuple(
    '_Alternative',
    ['closed', 'text', 'names', 'glob', 'present', 'uncertain', 'trusted'])
_START = _Alternative((), '', frozenset(), False, False, frozenset(), False)
_REFERENCED_NAME = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)')
_GLOB_MARK = '\x00glob'
_GLOB_CHARACTERS = frozenset('*?[')
_DEFAULT_IFS = ' \t\n'
_PARAMETER_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!0-]')
# Longest first, so ``:-`` wins over ``:``, ``##`` over ``#``, ``//`` over ``/``.
_PARAMETER_OPERATORS = (
    ':-', ':=', ':?', ':+', '-', '=', '?', '+', '##', '#', '%%', '%', '//',
    '/#', '/%', '/', '^^', '^', ',,', ',', '@', ':')
_DEFAULT_ASSIGNMENT = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*):?=')
_OUTPUT_REDIRECT = re.compile(r'(?:([0-9]+)|&)?(?:>>?|>\||<>|>&)\Z')


class _Unenumerable(Exception):
    """A word whose expansions this guard cannot enumerate."""


class _Overflowed(Exception):
    """A word with more candidate expansions than ``_DELETE_BINDING_CAP``."""


def _expand_delete_operand(raw, state, home):
    """Expand one command operand like the shell would, without executing.

    Returns an ``_Expansion``: ``candidates`` are the fields the operand may
    become (a field pathname expansion applies to carries a trailing
    ``_GLOB_MARK`` the depth judgement consumes; a field holding a value
    that is unknown but never empty is not among them); ``collapsed`` holds, per
    candidate, the names (variables, or a command substitution's text)
    without a provable value that were substituted EMPTY on its path — the
    unset-collapse case (``rm -rf "$D/"`` with ``D`` unset or dynamic
    deletes ``/``); ``unproven`` is their union. Returns None when the
    operand cannot be enumerated or has more candidates than the bound.
    Raises ``_EnvironmentDeferred`` when it reads the environment or the cwd
    of a caller that does not know them.
    """
    try:
        # Each word a brace expression makes (bash) is an operand of its own;
        # dash keeps the braces.
        alternatives = [alternative for word in [raw, *(_brace_variants(raw) or ())]
                        for alternative in _scan_word(word, state, home, True, False).fields()]
    except (_Unenumerable, _Overflowed):
        return None
    fields = [field for fields, _uncertain in alternatives for field in fields
              if not field.trusted]
    collapsed = tuple(field.names for field in fields)
    return _Expansion(
        tuple(field.text + (_GLOB_MARK if field.glob else '') for field in fields),
        set().union(*collapsed), collapsed)


def _expand_word(raw, state, home, *, split=True, heredoc=False):
    """Scan shell word *raw* into its possible expansions, a
    :class:`_WordExpansion`. *split*: a command operand, which brace
    expansion (bash), field splitting and pathname expansion apply to; off
    for an assignment value or a script's text. *heredoc*: the body of an
    unquoted heredoc, where quotes are plain characters."""
    if split and not heredoc:
        variants = _brace_variants(raw)
        if variants is not None:
            # dash keeps the braces; bash makes a word of each variant.
            fields = _scan_word(raw, state, home, split, heredoc).fields()
            products = [((), frozenset())]
            for variant in variants:
                alternatives = _scan_word(variant, state, home, split, heredoc).fields()
                products = [(fields_so_far + more, uncertain | more_uncertain)
                            for fields_so_far, uncertain in products
                            for more, more_uncertain in alternatives]
                if len(products) > _DELETE_BINDING_CAP:
                    raise _Overflowed()
            return _FieldAlternatives(fields + products)
    return _scan_word(raw, state, home, split, heredoc)


class _FieldAlternatives:
    """Field lists a word may make: the expansion of a brace expression."""

    def __init__(self, alternatives):
        self._alternatives = alternatives

    def fields(self):
        return self._alternatives


def _brace_variants(raw):
    """The words bash brace expansion makes of *raw* (``/tmp/{a,b}`` makes
    ``/tmp/a`` and ``/tmp/b``, ``{1..3}`` three words), or None when it has
    no unquoted brace expression."""
    found = _first_brace(raw)
    if found is None:
        return None
    start, end, parts = found
    variants = []
    for part in parts:
        word = raw[:start] + part + raw[end + 1:]
        variants.extend(_brace_variants(word) or [word])
        if len(variants) > _DELETE_BINDING_CAP:
            raise _Overflowed()
    return variants


_BRACE_SEQUENCE = re.compile(
    r'(-?[0-9]+)\.\.(-?[0-9]+)(?:\.\.(-?[0-9]+))?\Z|([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?[0-9]+))?\Z')


def _first_brace(raw):
    """``(start, end, parts)`` of the first unquoted brace expression in
    *raw*: a ``{a,b}`` list or a ``{x..y[..step]}`` sequence; None when it
    has none."""
    quote, index = None, 0
    while index < len(raw):
        char = raw[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif char == '{' and quote is None and not raw.startswith('${', index - 1 if index else 0):
            found = _brace_parts(raw, index)
            if found is not None:
                return (index,) + found
        index += 1
    return None


def _brace_parts(raw, start):
    """``(end, parts)`` of the brace expression opening at ``raw[start]``,
    or None when those braces stay literal."""
    depth, quote, index, cut = 0, None, start, [start]
    while index < len(raw):
        char = raw[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif quote is None and char == '{' and not (index and raw[index - 1] == '$'):
            depth += 1
        elif quote is None and char == '}':
            depth -= 1
            if depth == 0:
                cut.append(index)
                break
        elif quote is None and char == ',' and depth == 1:
            cut.append(index)
        index += 1
    else:
        return None
    end = cut[-1]
    if len(cut) > 2:
        return end, [raw[left + 1:right] for left, right in zip(cut, cut[1:])]
    sequence = _BRACE_SEQUENCE.match(raw[start + 1:end])
    if sequence is None:
        return None
    numbers = sequence.group(1) is not None
    first, last = ((int(sequence.group(1)), int(sequence.group(2))) if numbers
                   else (ord(sequence.group(4)), ord(sequence.group(5))))
    step = abs(int(sequence.group(3) or sequence.group(6) or 1)) or 1
    count = abs(last - first) // step + 1
    if count > _DELETE_BINDING_CAP:
        raise _Overflowed()
    direction = step if last >= first else -step
    values = [first + position * direction for position in range(count)]
    return end, [str(value) if numbers else chr(value) for value in values]


def _scan_word(raw, state, home, split, heredoc):
    """Scan *raw* left to right into a :class:`_WordExpansion`."""
    expansion = _WordExpansion(state, home, split)
    quote, index, length = None, 0, len(raw)
    while index < length:
        char = raw[index]
        if quote == "'":
            end = raw.find("'", index)
            if end < 0:
                raise _Unenumerable()
            expansion.literal(raw[index:end], quoted=True)
            quote, index = None, end + 1
        elif char == '\\':
            index = expansion.escape(raw, index, quote == '"', heredoc)
        elif char == "'" and quote is None and not heredoc:
            expansion.quote_seen()
            quote, index = "'", index + 1
        elif char == '"' and not heredoc:
            if not quote and not raw.startswith(('"$@"', '"${@}"'), index):
                expansion.quote_seen()  # "$@" alone makes no field without arguments
            quote, index = (None if quote else '"'), index + 1
        elif char == '$':
            index = expansion.dollar(raw, index, quote == '"' or heredoc)
        elif char == '`':
            index = expansion.backtick(raw, index, quote == '"' or heredoc)
        elif char == '~' and index == 0 and quote is None and not heredoc:
            index = expansion.tilde(raw)
        else:
            end = index + 1
            while end < length and raw[end] not in '\\\'"$`':
                end += 1
            expansion.literal(raw[index:end], quoted=quote is not None or heredoc)
            index = end
    if quote is not None:
        raise _Unenumerable()  # an unterminated quote
    return expansion


class _WordExpansion:
    """The possible expansions of one shell word as it is scanned.

    Each ``_Alternative`` holds the finished fields and the field being
    built; *present* marks a field that exists although empty (a quoted
    one), *trusted* a field holding a value that is unknown but never empty,
    and *uncertain* the unquoted unproven values that may have split the
    alternative into any number of fields.
    """

    def __init__(self, state, home, split):
        self.state, self.home, self.split = state, home, split
        self.alternatives = [_START]

    def fields(self):
        """``(fields, uncertain)`` per alternative of an operand."""
        return [(_close_field(alternative).closed, alternative.uncertain)
                for alternative in self.alternatives]

    def values(self):
        """The value set of an unsplit word: one value per alternative."""
        return _dedupe(_field_value(alternative.text, alternative.names,
                                    alternative.glob, alternative.trusted)
                       for alternative in self.alternatives)

    def literal(self, chunk, quoted):
        glob = self.split and not quoted and _has_glob(chunk)
        self.alternatives = [
            alternative._replace(
                text=alternative.text + chunk, glob=alternative.glob or glob,
                present=alternative.present or quoted or bool(chunk))
            for alternative in self.alternatives]

    def quote_seen(self):
        self.alternatives = [alternative._replace(present=True)
                             for alternative in self.alternatives]

    def splice(self, values, name=None, quoted=False):
        """Splice in each value of the set *values*, the value of *name*."""
        unquoted = self.split and not quoted
        ifs = self._ifs() if unquoted else ''
        self._keep([_splice(alternative, value, name, quoted, unquoted, ifs)
                    for alternative in self.alternatives for value in values])

    def _keep(self, alternatives):
        if len(alternatives) > _DELETE_BINDING_CAP:
            raise _Overflowed()
        self.alternatives = alternatives

    def _ifs(self):
        value = self.state.bindings.get('IFS', (_DEFAULT_IFS,))
        if not isinstance(value, tuple) or len(value) != 1 or not isinstance(value[0], str):
            raise _Unenumerable()  # field splitting on an IFS this guard cannot know
        return value[0]

    def escape(self, raw, index, in_double, heredoc):
        """A backslash quotes the next character; in double quotes and heredoc
        bodies only ``$``, a backtick, a backslash and a newline (and ``"`` in
        double quotes), and stays itself before any other."""
        following = raw[index + 1:index + 2]
        if following and (not (in_double or heredoc) or following in '$`\\\n'
                          or (in_double and following == '"')):
            if following != '\n':
                self.literal(following, quoted=True)
            return index + 2
        self.literal('\\', quoted=True)
        return index + 1

    def dollar(self, raw, index, quoted):
        following = raw[index + 1:index + 2]
        if following == "'" and not quoted:
            return self._ansi_c(raw, index)
        if following == '"' and not quoted:
            return index + 1  # bash $"…": the double-quoted string that follows
        if raw.startswith('$((', index):
            end = _arithmetic_end(raw, index + 3)
            if end is not None:
                self.literal('0', quoted=True)  # a number: never empty
                return end
        if following == '(':
            end = _substitution_end(raw, index + 2)
            if end is None:
                raise _Unenumerable()
            body = raw[index + 2:end].strip()
            self.splice(_substitution_values(body, self.state, self.home),
                        f'$({_brief(body)})', quoted)
            return end + 1
        if following == '{':
            end = _parameter_end(raw, index + 2)
            if end is None:
                raise _Unenumerable()
            self.parameter(raw[index + 2:end], quoted)
            return end + 1
        match = _PARAMETER_NAME.match(raw, index + 1)
        if match is None:
            self.literal('$', quoted=True)
            return index + 1
        name = match.group(0)[0] if match.group(0)[0].isdigit() else match.group(0)
        self.parameter(name, quoted)  # `$10` is `${1}0`
        return index + 1 + len(name)

    def backtick(self, raw, index, quoted):
        body, position = [], index + 1
        while position < len(raw) and raw[position] != '`':
            if raw[position] == '\\' and raw[position + 1:position + 2] in ('$', '`', '\\'):
                position += 1
            body.append(raw[position])
            position += 1
        if position >= len(raw):
            raise _Unenumerable()  # an unterminated backtick
        text = ''.join(body).strip()
        self.splice(_substitution_values(text, self.state, self.home),
                    f'`{_brief(text)}`', quoted)
        return position + 1

    def tilde(self, raw):
        end = raw.find('/')
        if raw[1:end] if end >= 0 else raw[1:]:
            raise _Unenumerable()  # ~user, ~+, ~-: the shell resolves them
        self.splice(self._parameter_values('HOME'), 'HOME', quoted=True)
        return 1

    def _ansi_c(self, raw, index):
        decoded = []
        end = _ansi_c_span(raw, index, decoded)
        if end is None:
            raise _Unenumerable()
        # bash decodes the escapes; a POSIX sh reads `$` and a quoted string.
        self.splice((''.join(decoded), '$' + raw[index + 2:end - 1]), None, quoted=True)
        return end

    def parameter(self, inner, quoted):
        """Splice parameter expansion ``${inner}``."""
        if inner in ('@', '*'):
            self._arguments(joined=inner == '*' and quoted, quoted=quoted)
            return
        if inner in ('#', '?', '$') or (inner.startswith('#') and len(inner) > 1):
            self.literal('0', quoted=True)  # a count, a status, a pid, a length
            return
        if inner == '-':
            self.literal('h', quoted=True)  # the option letters
            return
        if inner == '0':
            self.splice(_value_tuple(self.state.zero), '$0', quoted)
            return
        if inner.startswith('!'):
            self.splice((_DYNAMIC,), '$' + inner, quoted)
            return
        match = _PARAMETER_NAME.match(inner)
        if match is None:
            raise _Unenumerable()
        name, rest = match.group(0), inner[match.end():]
        if rest.startswith('['):
            self.splice((_DYNAMIC,), name, quoted)  # arrays are not tracked
            return
        if name in ('@', '*'):
            if not rest.startswith(':') or rest.startswith((':-', ':=', ':?', ':+')):
                raise _Unenumerable()  # an operator on each parameter
            # ${@:offset[:length]}: some of the parameters; judging all is a superset.
            self._arguments(joined=name == '*' and quoted, quoted=quoted)
            return
        values = self._parameter_values(name)
        if rest:
            operator = next((op for op in _PARAMETER_OPERATORS if rest.startswith(op)), None)
            if operator is None:
                raise _Unenumerable()
            values = self._operate(values, operator, rest[len(operator):])
        self.splice(values, name, quoted)

    def _parameter_values(self, name):
        if name.isdigit():
            return _value_tuple(self.state.positional_value(int(name)))
        if name in ('@', '*'):
            return (_DYNAMIC,)
        if name in ('#', '?', '$'):
            return ('0',)
        if name == 'PWD' and 'PWD' not in self.state.bindings:
            values = self.state.cwd_values()
            if values is None:
                raise _Unenumerable()
            return values
        if name == 'HOME' and 'HOME' not in self.state.bindings and (
                self.state.environment is None or not self.state.environment.get('HOME')):
            return (self.home,) if self.home else (_DYNAMIC,)
        value = self.state.get_value(name)
        if value is _OVERFLOW and not self.split:
            raise _Overflowed()  # a value built from it overflows too
        return _value_tuple(value)

    def _word_values(self, word):
        return _expand_word(word, self.state, self.home, split=False).values()

    def _operate(self, values, operator, word):
        """Apply ``${NAME<operator><word>}`` to NAME's value set."""
        colon = operator.startswith(':')
        if operator in (':-', '-', ':=', '='):
            # An empty value takes the default; without `:` only an unset one
            # does, and set-but-empty is not told apart from unset here.
            default = self._word_values(word)
            return _dedupe(item for value in values for item in (
                default + (() if colon else ('',)) if value == ''
                else default + ((_TRUSTED,) if colon else (_DYNAMIC,)) if value is _DYNAMIC
                else (value,)))
        if operator in (':?', '?'):
            # The shell stops when the value is empty (unset, without `:`).
            return _dedupe(
                item for value in values for item in (
                    () if value == '' and colon
                    else ((_TRUSTED,) if colon else (_DYNAMIC,)) if value is _DYNAMIC
                    else (value,)))
        if operator in (':+', '+'):
            alternate = self._word_values(word)
            return _dedupe(item for value in values for item in (
                ('',) + alternate if value is _DYNAMIC or (value == '' and not colon)
                else ('',) if value == '' else alternate))
        if operator in ('#', '##', '%', '%%'):
            patterns = self._word_values(word)
            return _dedupe(_trim(value, pattern, operator)
                           for value in values for pattern in patterns)
        if operator in ('/', '//', '/#', '/%'):
            pattern, _separator, replacement = word.partition('/')
            patterns, replacements = self._word_values(pattern), self._word_values(replacement)
            return _dedupe(_replace_pattern(value, operator, found, put)
                           for value in values for found in patterns for put in replacements)
        if operator == ':':
            return _dedupe(_substring(value, word) for value in values)
        if operator in ('^', '^^', ',', ',,') and not word:
            return _dedupe(_convert_case(value, operator) for value in values)
        return (_DYNAMIC,)

    def _arguments(self, joined, quoted):
        """Splice ``$@``/``$*``: each positional parameter a field of its own,
        or (``"$*"``) all of them joined by a space."""
        prefix, tail = self.state.positional
        if joined:
            for position, value in enumerate(prefix):
                if position:
                    self.literal(' ', quoted=True)
                self.splice(_value_tuple(value), f'${position + 1}', quoted=True)
            if tail != ():
                without = self.alternatives
                if prefix:
                    self.literal(' ', quoted=True)
                self.splice(_value_tuple(tail), '$*', quoted=True)
                self._keep(without + self.alternatives)
            return
        unquoted = self.split and not quoted
        ifs = self._ifs() if unquoted else ''
        alternatives = self.alternatives
        for position, value in enumerate(prefix):
            if position:
                alternatives = [_close_field(alternative) for alternative in alternatives]
            alternatives = [_splice(alternative, item, f'${position + 1}', quoted, unquoted, ifs)
                            for alternative in alternatives for item in _value_tuple(value)]
            self._keep(alternatives)
        if tail != ():
            more = [_splice(_close_field(alternative) if prefix else alternative, item, '$@',
                            quoted, unquoted, ifs)
                    for alternative in alternatives for item in _value_tuple(tail)]
            alternatives = [alternative._replace(uncertain=alternative.uncertain | {'$@'})
                            for alternative in alternatives + more]
        self._keep(alternatives)


def _splice(alternative, value, name, quoted, unquoted, ifs):
    """*alternative* with *value*, one element of a value set, spliced in;
    *unquoted* in an operand, it is split into fields at *ifs* characters and
    its glob characters apply."""
    if value is _DYNAMIC or value is _TRUSTED:
        label = name or 'an expansion'
        return alternative._replace(
            names=alternative.names | {label} if value is _DYNAMIC else alternative.names,
            present=alternative.present or quoted or value is _TRUSTED,
            trusted=alternative.trusted or value is _TRUSTED,
            uncertain=alternative.uncertain | {label} if unquoted else alternative.uncertain)
    text, names = ((value.text, value.names) if isinstance(value, _Collapsed)
                   else (value, frozenset()))
    marked = text.endswith(_GLOB_MARK)
    if marked:
        text = text[:-len(_GLOB_MARK)]
    segments = re.split('[' + re.escape(ifs) + ']+', text) if ifs and text else [text]
    for position, segment in enumerate(segments):
        if position:
            alternative = _close_field(alternative)
        alternative = alternative._replace(
            text=alternative.text + segment, names=alternative.names | names,
            glob=(alternative.glob or (unquoted and _has_glob(segment))
                  or (marked and position == len(segments) - 1)),
            present=alternative.present or quoted or bool(segment))
    if ifs and any(char not in _DEFAULT_IFS and char in text for char in ifs):
        # Splitting at a non-blank IFS character keeps empty fields.
        alternative = alternative._replace(uncertain=alternative.uncertain | {'IFS'})
    return alternative


def _close_field(alternative):
    """*alternative* with the field being built finished."""
    if alternative.present or alternative.text:
        alternative = alternative._replace(closed=alternative.closed + (
            _Field(alternative.text, alternative.names, alternative.glob,
                   alternative.trusted),))
    return alternative._replace(text='', names=frozenset(), glob=False, present=False,
                                trusted=False)


def _field_value(text, names, glob, trusted=False):
    """The value-set element of an expanded text: the literal, its empty
    collapse when it rests on unproven *names*, ``_TRUSTED`` when it holds
    a value that is unknown but never empty."""
    if trusted:
        return _TRUSTED
    text += _GLOB_MARK if glob else ''
    if not names:
        return text
    return _Collapsed(text, names) if text else _DYNAMIC


def _value_set(values):
    """A binding's value from a value set: None for ``_DYNAMIC`` alone."""
    if values == (_DYNAMIC,):
        return None
    return _OVERFLOW if len(values) > _DELETE_BINDING_CAP else values


def _value_tuple(value):
    """The elements of a binding's value."""
    if value is _DEFERRED:
        raise _EnvironmentDeferred('a parameter')
    if value is None or value is _OVERFLOW:
        return (_DYNAMIC,)
    return value


def _dedupe(items):
    return tuple(dict.fromkeys(items))


def _may_be_empty(value):
    return value is _DYNAMIC or value == ''


def _has_glob(text):
    return any(char in _GLOB_CHARACTERS for char in text)


def _brief(text):
    return ' '.join(text.split())[:40]


def _parameter_end(raw, start):
    """Index of the ``}`` closing a ``${`` whose text starts at *start*."""
    depth, index, quote = 1, start, None
    while index < len(raw):
        char = raw[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if quote == "'":
            quote = None if char == "'" else quote
        elif char == "'" and quote is None:
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif raw.startswith('$(', index) and not raw.startswith('$((', index):
            end = _substitution_end(raw, index + 2)
            if end is None:
                return None
            index = end
        elif raw.startswith('${', index):
            depth += 1
            index += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _text_of(value):
    """The text a value set element stands for: an unknown one collapses."""
    if isinstance(value, str):
        return value[:-len(_GLOB_MARK)] if value.endswith(_GLOB_MARK) else value
    return _split_value(value)[0] if isinstance(value, _Collapsed) else ''


def _split_value(value):
    """``(text, names, glob)`` of a literal or collapsed value."""
    text, names = ((value.text, value.names) if isinstance(value, _Collapsed)
                   else (value, frozenset()))
    if text.endswith(_GLOB_MARK):
        return text[:-len(_GLOB_MARK)], names, True
    return text, names, False


def _trim(value, pattern, operator):
    """``${NAME#pattern}`` (``##``, ``%``, ``%%``) on one value."""
    if not isinstance(value, (str, _Collapsed)) or not isinstance(pattern, str):
        return _DYNAMIC
    text, names, glob = _split_value(value)
    if glob:
        return _DYNAMIC
    if operator in ('#', '##'):
        ends = range(len(text) + 1) if operator == '#' else range(len(text), -1, -1)
        text = text[next((end for end in ends
                          if fnmatch.fnmatchcase(text[:end], pattern)), 0):]
    else:
        starts = range(len(text), -1, -1) if operator == '%' else range(len(text) + 1)
        text = text[:next((start for start in starts
                           if fnmatch.fnmatchcase(text[start:], pattern)), len(text))]
    return _field_value(text, names, False)


def _replace_pattern(value, operator, pattern, replacement):
    """``${NAME/pattern/replacement}`` (``//``, ``/#``, ``/%``) on one
    value; a pattern with glob characters is not followed."""
    if (not isinstance(value, str) or not isinstance(pattern, str)
            or not isinstance(replacement, str) or _has_glob(pattern)
            or value.endswith(_GLOB_MARK)):
        return _DYNAMIC
    if not pattern:
        return value
    if operator == '/':
        return value.replace(pattern, replacement, 1)
    if operator == '//':
        return value.replace(pattern, replacement)
    if operator == '/#':
        return replacement + value[len(pattern):] if value.startswith(pattern) else value
    return value[:-len(pattern)] + replacement if value.endswith(pattern) else value


def _substring(value, specification):
    """``${NAME:offset[:length]}`` on one value, for integer literals."""
    if not isinstance(value, str) or value.endswith(_GLOB_MARK):
        return _DYNAMIC
    try:
        numbers = [int(part) for part in specification.split(':')]
    except ValueError:
        return _DYNAMIC
    if len(numbers) not in (1, 2):
        return _DYNAMIC
    start = numbers[0] if numbers[0] >= 0 else max(len(value) + numbers[0], 0)
    if len(numbers) == 1:
        return value[start:]
    end = start + numbers[1] if numbers[1] >= 0 else len(value) + numbers[1]
    return value[start:end]


def _convert_case(value, operator):
    """``${NAME^}`` (``^^``, ``,``, ``,,``) on one value."""
    if not isinstance(value, (str, _Collapsed)):
        return value
    text, names, glob = _split_value(value)
    if operator == '^^':
        text = text.upper()
    elif operator == ',,':
        text = text.lower()
    elif text:
        text = (text[0].upper() if operator == '^' else text[0].lower()) + text[1:]
    return _field_value(text, names, glob)


def _substitution_values(body, state, home):
    """The value set command substitution *body* prints: a deep placeholder
    for ``mktemp``, the cwd for ``pwd``, and what ``echo``, ``printf``,
    ``dirname``, ``basename``, ``cd DIR && pwd`` and ``git rev-parse
    --show-toplevel`` print for words this guard can expand; ``_DYNAMIC``
    for anything else."""
    mkstemp = _mkstemp_value(body)
    if mkstemp is not None:
        return (mkstemp,)
    try:
        items = parse_shell_structure(body)
    except ShellStructureUnknown:
        return (_DYNAMIC,)
    if not items or not all(isinstance(item, SimpleCommand) and not item.heredocs
                            and not _redirects_output(item) for item in items):
        return (_DYNAMIC,)
    first = [_literal_command_word(word) for word in items[0].words]
    if (len(items) == 2 and first[:1] == ['cd']
            and items[1].separator in ('&&', ';', '\n')
            and [_literal_command_word(word) for word in items[1].words]
            in (['pwd'], ['pwd', '-L'], ['pwd', '-P'])):
        return _directory_output(state, items[0].words, home, items[1].separator == '&&')
    if len(items) != 1:
        return (_DYNAMIC,)
    views = _program_views(items[0].words)
    if views is _UNKNOWN_PROGRAM or len(views) != 1:
        return (_DYNAMIC,)
    literal, raw = views[0]
    producer = _PRODUCERS.get(os.path.basename(literal[0]))
    return (_DYNAMIC,) if producer is None else producer(literal, raw, state, home)


def _redirects_output(item):
    """Whether *item* sends its standard output somewhere other than its
    command substitution."""
    for operator, _target in item.redirects:
        match = _OUTPUT_REDIRECT.match(operator)
        if match and match.group(1) in (None, '1'):
            return True
    return False


def _pwd_output(literal, raw, state, home):
    values = state.cwd_values()
    if values is None:
        raise _Unenumerable()
    return values


def _echo_output(literal, raw, state, home):
    operands = list(raw[1:])
    while operands and re.fullmatch(r'-[neE]+', _literal_command_word(operands[0])):
        if set(_literal_command_word(operands.pop(0))[1:]) - {'n'}:
            return (_DYNAMIC,)  # dash prints -e and -E as operands
    output = _joined_output(operands, state, home, ' ')
    # dash's echo interprets backslash escapes; bash's does not.
    return (_DYNAMIC,) if any('\\' in _text_of(value) for value in output) else output


def _printf_output(literal, raw, state, home):
    if len(raw) < 2:
        return (_DYNAMIC,)
    formats = _expand_word(raw[1], state, home, split=False).values()
    if len(formats) != 1 or not isinstance(formats[0], str):
        return (_DYNAMIC,)
    form = formats[0][:-2] if formats[0].endswith('\\n') else formats[0]
    if form == '%s':
        output = _joined_output(raw[2:], state, home, '')
        return (_DYNAMIC,) if any('\n' in _text_of(value) for value in output) else output
    if '%' not in form and '\\' not in form and len(raw) == 2:
        return (form,)
    return (_DYNAMIC,)


def _dirname_output(literal, raw, state, home):
    if len(raw) != 2:
        return (_DYNAMIC,)
    if _makes_no_field(raw[1], state, home):
        return ('',)  # no operand: dirname fails and prints nothing
    return _mapped_output(raw[1], state, home, _posix_dirname, _TRUSTED)


def _makes_no_field(raw, state, home):
    """Whether word *raw* expands to no field on every path (an unquoted
    empty value)."""
    return all(not fields and not uncertain
               for fields, uncertain in _expand_word(raw, state, home).fields())


def _basename_output(literal, raw, state, home):
    if len(raw) not in (2, 3):
        return (_DYNAMIC,)
    if len(raw) == 2 and _makes_no_field(raw[1], state, home):
        return ('',)  # no operand: basename fails and prints nothing
    suffix = literal[2] if len(raw) == 3 else ''
    return _mapped_output(raw[1], state, home,
                          lambda path: _posix_basename(path, suffix), _DYNAMIC)


def _git_output(literal, raw, state, home):
    operands, bases = list(literal[1:]), None
    if operands[:1] == ['-C'] and len(operands) > 1:
        bases, stays = _cd_destinations(state, list(raw[2:3]), home)
        if stays:
            return (_DYNAMIC,)
        operands = operands[2:]
    if operands != ['rev-parse', '--show-toplevel']:
        return (_DYNAMIC,)
    if state.environment is None:
        raise _EnvironmentDeferred('GIT_DIR')
    if any(name in state.bindings or state.environment.get(name)
           for name in ('GIT_DIR', 'GIT_WORK_TREE')):
        return (_DYNAMIC,)
    if bases is None:
        bases = state.cwd_values()
        if bases is None:
            raise _Unenumerable()
    return _dedupe(_git_toplevel(base) for base in bases)


_PRODUCERS = {
    'pwd': _pwd_output, 'echo': _echo_output, 'printf': _printf_output,
    'dirname': _dirname_output, 'basename': _basename_output, 'git': _git_output,
}


def _joined_output(raws, state, home, separator):
    """What the fields of words *raws* print joined by *separator* (``echo``:
    a space, ``printf %s``: nothing); each is expanded like an operand. A
    field pathname expansion applies to prints names this guard cannot list;
    an unknown field may print anything."""
    outputs = [((), frozenset(), False)]
    for raw in raws:
        alternatives = _expand_word(raw, state, home).fields()
        if any(field.glob for fields, _uncertain in alternatives for field in fields):
            return (_DYNAMIC,)
        outputs = [(texts + tuple(field.text for field in fields),
                    names.union(uncertain, *(field.names for field in fields)),
                    trusted or any(field.trusted for field in fields))
                   for texts, names, trusted in outputs
                   for fields, uncertain in alternatives]
        if len(outputs) > _DELETE_BINDING_CAP:
            raise _Overflowed()
    return _dedupe(_field_value(separator.join(texts), names, False, trusted)
                   for texts, names, trusted in outputs)


def _mapped_output(raw, state, home, function, of_unknown):
    """*function* of each value of word *raw* (``dirname``, ``basename``),
    of the empty collapse for a value built from unproven names; *of_unknown*
    stands for its result on a value this guard cannot know at all."""
    output = []
    for value in _expand_word(raw, state, home, split=False).values():
        if value is _DYNAMIC or value is _TRUSTED:
            output.append(of_unknown)
            continue
        text, names, glob = _split_value(value)
        output.append(of_unknown if glob else _field_value(function(text), names, False))
    return _dedupe(output)


def _posix_dirname(path):
    stripped = path.rstrip('/')
    if not stripped:
        return '/' if path else '.'
    if '/' not in stripped:
        return '.'
    return stripped.rsplit('/', 1)[0].rstrip('/') or '/'


def _posix_basename(path, suffix=''):
    stripped = path.rstrip('/')
    if not stripped:
        return '/' if path else ''
    base = stripped.rsplit('/', 1)[-1]
    return base[:-len(suffix)] if suffix and base != suffix and base.endswith(suffix) else base


def _git_toplevel(directory):
    """What ``git rev-parse --show-toplevel`` prints in *directory*: the
    closest directory holding ``.git``; nothing outside a repository."""
    current = os.path.normpath(directory)
    while True:
        if os.path.lexists(os.path.join(current, '.git')):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return ''
        current = parent


def _directory_output(state, words, home, after_success):
    """What ``cd DIR && pwd`` prints (nothing when cd fails); after ``;``
    the old cwd instead."""
    literal = [_literal_command_word(word) for word in words]
    try:
        destinations, stays = _cd_destinations(
            state, _directory_operands(literal, words), home)
    except _Unenumerable:
        return (_DYNAMIC,)  # a directory this guard cannot know, or nothing
    printed = list(destinations)
    if stays or not after_success:
        cwds = state.cwd_values()
        if cwds is None:
            raise _Unenumerable()
        printed += list(cwds)
    return _dedupe(printed + [''] if after_success else printed)  # cd may fail


def _script_texts(raws, state, home, *, heredoc=False):
    """The texts a shell may run for words *raws* (``eval`` joins its
    operands with spaces), or None when they cannot be enumerated."""
    expansion = _WordExpansion(state, home, split=False)
    try:
        for position, raw in enumerate(raws):
            if position:
                expansion.literal(' ', quoted=True)
            expansion.splice(_expand_word(raw, state, home, split=False,
                                          heredoc=heredoc).values(), None, quoted=True)
    except (_Unenumerable, _Overflowed):
        return None
    return list(_dedupe(_text_of(value) for value in expansion.values()))


def _could_run_delete(text):
    """Whether shell text names rm/rmdir/unlink at all. The delete guard does
    not read a command without one of those words, and the resolver reports
    text it cannot read as unresolvable only when it has one."""
    for word in ('rm', 'unlink'):  # rmdir starts with rm
        start = text.find(word) if text else -1
        while start >= 0:
            end = start + len(word)
            if word == 'rm' and text.startswith('dir', end):
                end += 3
            if not ((start and _joins_word(text[start - 1]))
                    or (end < len(text) and _joins_word(text[end]))):
                return True
            start = text.find(word, start + 1)
    return False


def _joins_word(char):
    return char.isalnum() or char in '_.-'


_ANSI_C_ESCAPES = {'a': '\a', 'b': '\b', 'e': '\x1b', 'E': '\x1b', 'f': '\f',
                   'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\\': '\\',
                   "'": "'", '"': '"', '?': '?'}
_ANSI_C_NUMERIC = re.compile(
    r'x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|([0-7]{1,3})|c(.)')


def _ansi_c_span(raw, index, out):
    """Decode the bash ``$'…'`` span opening at ``raw[index]`` into *out*;
    return the index after it, or None when it does not close."""
    index += 2
    while index < len(raw):
        char = raw[index]
        if char == "'":
            return index + 1
        if char != '\\' or index + 1 >= len(raw):
            out.append(char)
            index += 1
            continue
        escape = raw[index + 1]
        if escape in _ANSI_C_ESCAPES:
            out.append(_ANSI_C_ESCAPES[escape])
            index += 2
            continue
        match = _ANSI_C_NUMERIC.match(raw, index + 1)
        if match is None:
            out.append('\\' + escape)
            index += 2
            continue
        hexa, short, long, octal, control = match.groups()
        if control is not None:
            out.append(chr(ord(control) & 0x1f))
        else:
            code = int(octal, 8) if octal else int(hexa or short or long, 16)
            if code > 0x10FFFF:
                return None
            out.append(chr(code & 0xff if octal else code))
        index = match.end()
    return None


def _ansi_c_word(raw):
    """Text bash makes of a word holding ``$'…'`` spans and plain quoting,
    or None when another expansion (``$x``, ``$(…)``, backticks) is present."""
    out = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if raw.startswith("$'", index):
            index = _ansi_c_span(raw, index, out)
            if index is None:
                return None
        elif char == "'":
            end = raw.find("'", index + 1)
            if end < 0:
                return None
            out.append(raw[index + 1:end])
            index = end + 1
        elif char == '"':
            end = raw.find('"', index + 1)
            body = raw[index + 1:end]
            if end < 0 or any(c in body for c in '$`\\'):
                return None
            out.append(body)
            index = end + 1
        elif char == '\\' and index + 1 < len(raw):
            out.append(raw[index + 1])
            index += 2
        elif char in '$`':
            return None
        else:
            out.append(char)
            index += 1
    return ''.join(out)


def _literal_command_word(raw):
    """Dequote a word that has no expansion (``\\rm``, ``"rm"`` -> ``rm``).

    The shell removes quotes before it looks a command up or an option is
    parsed, so command words and options are compared dequoted. A word with
    ``$``/backtick expansion keeps its raw text.
    """
    if "$'" in raw:
        decoded = _ansi_c_word(raw)
        return raw if decoded is None else decoded
    if '$' in raw or '`' in raw or not any(char in raw for char in '\'"\\'):
        return raw
    inner = raw[1:-1]
    if len(raw) > 1 and raw[0] == raw[-1] and (
            (raw[0] == "'" and "'" not in inner)
            or (raw[0] == '"' and '"' not in inner and '\\' not in inner)):
        return inner
    try:
        words = shlex.split(raw, comments=False, posix=True)
    except ValueError:
        return raw
    return words[0] if len(words) == 1 else raw


def _candidate_depth(path):
    """Components of *path* after normpath; a pure-glob tail counts as its parent.

    ``/mnt/*`` deletes everything /mnt holds, so it is judged as ``/mnt``
    (depth 1). An anchored pattern keeps its literal anchor:
    ``/tmp/build-*`` stays depth 2.
    """
    glob = path.endswith('\x00glob')
    if glob:
        path = path[:-len('\x00glob')]
    normalized = os.path.normpath(path)
    if glob:
        components = [c for c in normalized.split('/') if c]
        while components and all(ch in '*?' for ch in components[-1]):
            components.pop()  # `*`, `?*`: every name; `[ab]` names one character
        normalized = '/' + '/'.join(components) if components else '/'
    return normalized, len([c for c in normalized.split('/') if c])


def _loop_variable(header, state, home):
    """``(name, values, runs at least once)`` of a ``for``/``select`` header.

    The listed words expand like operands, a field each (field splitting and
    pathname expansion apply; a glob field keeps its pattern shape); a word
    that may make no field (``$(ls)``, ``"$@"``, an unquoted unproven value)
    may leave the list empty. Without ``in`` the loop walks the positional
    parameters. An arithmetic ``for ((…))`` binds nothing, and ``select``
    may run its body any number of times. An empty list yields ``()``.
    """
    words = header[1:]
    if not words or words[0].startswith('(('):
        return None, None, False
    values, runs = [], False
    try:
        if words[1:2] != ('in',):
            prefix, tail = state.positional
            for value in prefix + ((tail,) if tail != () else ()):
                values.extend(_value_tuple(value))
            runs = bool(prefix)
        else:
            for raw in words[2:]:
                try:
                    alternatives = _expand_word(raw, state, home).fields()
                except (_Unenumerable, _Overflowed):
                    values.append(_DYNAMIC)
                    continue
                for fields, uncertain in alternatives:
                    values.extend(_field_value(*field) for field in fields)
                    if uncertain:
                        values.append(_DYNAMIC)
                runs = runs or all(fields and not uncertain
                                   for fields, uncertain in alternatives)
    except _EnvironmentDeferred:
        return words[0], _DEFERRED, False
    values = _dedupe(values)
    return (words[0], None if values == (_DYNAMIC,) else values,
            runs and header[0] == 'for')


def _join_values(mine, theirs):
    """The value set a name holds where two paths meet."""
    for absorbing in (_DEFERRED, _OVERFLOW):
        if mine is absorbing or theirs is absorbing:
            return absorbing
    merged = tuple(dict.fromkeys(
        ((_DYNAMIC,) if mine is None else mine)
        + ((_DYNAMIC,) if theirs is None else theirs)))
    if merged == (_DYNAMIC,):
        return None
    return _OVERFLOW if len(merged) > _DELETE_BINDING_CAP else merged


class _Exits:
    """States that leave a loop (``breaks``, ``continues``) or a function
    call (``returns``) early, joined; ``saved`` holds, joined, what the names
    a call made ``local`` held before ``local`` ran."""

    __slots__ = ('breaks', 'continues', 'returns', 'saved')

    def __init__(self):
        self.breaks = self.continues = self.returns = None
        self.saved = {}

    def reach(self, kind, state):
        reached = getattr(self, kind)
        if reached is None:
            setattr(self, kind, state.branch())
        else:
            reached.join(state)

    def join_into(self, state, *kinds):
        for kind in kinds:
            reached = getattr(self, kind)
            if reached is not None:
                state.join(reached)


class _Jumps:
    """Where ``break``/``continue`` (the enclosing ``loops``, innermost
    last) and ``return`` (the innermost function ``call``) lead."""

    __slots__ = ('loops', 'call')

    def __init__(self, loops=(), call=None):
        self.loops = loops
        self.call = call


_NO_JUMPS = _Jumps()


class _Definition:
    """One definition of a shell function: its body node and script items."""

    __slots__ = ('body', 'items')

    def __init__(self, body, items):
        self.body = body
        self.items = items

    def __eq__(self, other):
        return (isinstance(other, _Definition) and self.body is other.body
                and self.items is other.items)

    def __hash__(self):
        return hash((id(self.body), id(self.items)))


class _DeleteGuardState:
    """Literal shell state at one point of a command, as this guard tracks it.

    ``environment`` is the mapping the command's shell starts with (None when
    the caller does not know it). ``cwds`` are the possible working
    directories (none for a caller that gave no cwd); ``cwd_lost`` marks a
    path where a ``cd`` target could not be followed. ``functions`` maps a
    name to its possible definitions (None
    among them where the name may be undefined). ``positional`` is
    ``(prefix, tail)``: a value per known positional parameter, then the
    values of any number of further ones (``()``: none); ``zero`` is the
    value of ``$0``. ``exports`` maps a name to whether a process this shell
    starts sees it (True, False, 'maybe'; a name absent from it is exported
    when the starting environment holds it), ``allexport`` is the ``set -a``
    mode ('on', 'off', 'maybe') and ``exported_functions`` the functions of
    ``export -f``; ``aliases`` maps an alias to the texts it may hold;
    ``traps`` are the trap actions set on this path. ``jumps``, ``calls`` and
    ``alias_active`` describe the position rather than the state: where
    ``break``/``continue``/``return`` lead, the functions being run and the
    aliases whose text is being read; ``elsewhere`` whether the command being
    run executes on another host, container or root (``ssh``, ``docker
    exec``, ``chroot``); ``cd_outcome`` what the last ``cd`` that may fail
    left on success and on failure (see :func:`_outcomes`). ``home`` is this
    process's home directory. ``memo``
    keeps, for the whole judgement, one parse and structure per script, the
    program views of each command and the trap actions set.
    """

    __slots__ = ('bindings', 'cwds', 'cwd_given', 'cwd_lost',
                 'environment', 'functions', 'positional', 'zero', 'exports',
                 'allexport', 'exported_functions', 'aliases', 'traps', 'jumps',
                 'calls', 'alias_active', 'elsewhere', 'cd_outcome', 'home', 'memo')
    _POSITION = frozenset({'jumps', 'calls', 'alias_active', 'elsewhere', 'cd_outcome',
                           'home', 'memo'})

    def __init__(self, cwd, environment):
        start = os.path.normpath(cwd) if cwd and os.path.isabs(cwd) else None
        self.bindings = {}
        self.cwds = [start] if start else []
        self.cwd_given = start is not None
        self.cwd_lost = False
        self.environment = environment
        self.functions = {}
        self.positional = ((), ())
        self.zero = (_TRUSTED,)  # the shell's name
        self.exports = {}
        self.exported_functions = frozenset()
        self.allexport = 'off'
        self.aliases = {}
        self.traps = ()
        self.jumps = _NO_JUMPS
        self.calls = frozenset()
        self.alias_active = frozenset()
        self.elsewhere = False
        self.cd_outcome = None
        self.home = os.path.expanduser('~')
        self.memo = {}

    def branch(self):
        """This shell on another path through the same commands."""
        clone = object.__new__(_DeleteGuardState)
        for slot in self.__slots__:
            setattr(clone, slot, getattr(self, slot))
        clone.bindings = dict(self.bindings)
        clone.cwds = list(self.cwds)
        clone.functions = dict(self.functions)
        clone.exports = dict(self.exports)
        clone.aliases = dict(self.aliases)
        return clone

    def fork(self):
        """State of a subshell (``( … )``, a pipeline stage, a substitution): it
        cannot jump to this shell's loops or calls and runs none of its
        traps."""
        clone = self.branch()
        clone.jumps = _NO_JUMPS
        clone.traps = ()
        return clone

    def child(self, positional=((), ()), zero=(_TRUSTED,)):
        """State of a shell process this one starts (``sh -c``, ``bash
        <<'EOF'``): the cwd, the environment with every exported name, the
        functions of ``export -f``, its own *positional* parameters and
        ``$0``."""
        clone = self.fork()
        clone.zero = zero
        clone.bindings, clone.exports = {}, {}
        for name in dict.fromkeys([*self.bindings, *self.exports]):
            exported = self._exported(name)
            if exported == 'maybe':
                clone.bindings[name] = _join_values(
                    self._held(name), _DEFERRED if self.environment is None else ('',))
            elif exported:
                clone.bindings[name] = self._held(name)
            elif self.environment is None or name in self.environment:
                clone.bindings[name] = ('',)  # withdrawn from the environment
                continue
            else:
                continue
            clone.exports[name] = True  # the child exports what it inherits
        clone.functions = {name: self.functions[name]
                           for name in self.exported_functions if name in self.functions}
        clone.positional = positional
        clone.exported_functions = frozenset(clone.functions)
        clone.allexport = 'off'
        clone.aliases = {}
        clone.calls = clone.alias_active = frozenset()
        return clone

    def _exported(self, name):
        """True, False or 'maybe': whether a process this shell starts sees its
        value of *name*."""
        if name in self.exports:
            return self.exports[name]
        if self.environment is None:
            return 'maybe'  # it may come from the unknown environment
        return name in self.environment

    def assign(self, other):
        """Take the state *other* reached at this same point."""
        for slot in self.__slots__:
            if slot not in self._POSITION:
                setattr(self, slot, getattr(other, slot))

    def join(self, other):
        """Add the state *other* reached at this same point on another path."""
        for name in dict.fromkeys([*self.bindings, *other.bindings]):
            self.bindings[name] = _join_values(self._held(name), other._held(name))
        for cwd in other.cwds:
            if cwd not in self.cwds:
                self.cwds.append(cwd)
        del self.cwds[_CWD_CAP:]
        self.cwd_lost = self.cwd_lost or other.cwd_lost
        for name in dict.fromkeys([*self.functions, *other.functions]):
            self.functions[name] = _dedupe(self.functions.get(name, (None,))
                                           + other.functions.get(name, (None,)))
        self.positional = _join_positional(self.positional, other.positional)
        for name in dict.fromkeys([*self.exports, *other.exports]):
            mine, theirs = self._exported(name), other._exported(name)
            self.exports[name] = mine if mine == theirs else 'maybe'
        self.exported_functions |= other.exported_functions
        for name in dict.fromkeys([*self.aliases, *other.aliases]):
            self.aliases[name] = _dedupe(self.aliases.get(name, ())
                                         + other.aliases.get(name, ()))
        if self.allexport != other.allexport:
            self.allexport = 'maybe'
        self.traps += tuple(watch for watch in other.traps if watch not in self.traps)

    def signature(self):
        """What a join can add to: values, places, definitions, exports and
        traps."""
        return ({name: _comparable(value) for name, value in self.bindings.items()},
                frozenset(self.cwds), self.cwd_lost,
                {name: frozenset(found) for name, found in self.functions.items()},
                (tuple(_comparable(value) for value in self.positional[0]),
                 _comparable(self.positional[1])), _comparable(self.zero),
                frozenset(self.exports.items()),
                self.exported_functions, self.allexport,
                {name: frozenset(texts) for name, texts in self.aliases.items()},
                frozenset(self.traps))

    def _held(self, name):
        """The value *name* holds here: its binding, else the environment's
        (empty when the environment lacks it)."""
        if name in self.bindings:
            return self.bindings[name]
        if self.environment is None:
            return _DEFERRED
        return (self.environment.get(name, ''),)

    def get_value(self, name):
        value = self._held(name)
        if value is _DEFERRED:
            raise _EnvironmentDeferred(name)
        return value

    def positional_value(self, number):
        """The value of positional parameter *number*."""
        prefix, tail = self.positional
        if number <= len(prefix):
            return prefix[number - 1]
        return ('',) if tail == () else _join_values(tail, ('',))

    def cwd_values(self):
        """Candidates of ``$PWD`` and ``$(pwd)``: None after a ``cd`` this
        guard could not follow; unknown to a caller that gave no cwd."""
        if not self.cwd_given and (self.cwd_lost or not self.cwds):
            raise _EnvironmentDeferred('PWD')
        if self.cwd_lost or not self.cwds:
            return None
        return tuple(self.cwds)


def _comparable(value):
    return frozenset(value) if isinstance(value, tuple) else value


def _join_positional(mine, theirs):
    """The positional parameters two paths reach the same point with."""
    (mine_prefix, mine_tail), (their_prefix, their_tail) = mine, theirs
    shared = min(len(mine_prefix), len(their_prefix))
    prefix = tuple(_join_values(left, right)
                   for left, right in zip(mine_prefix, their_prefix))
    rest = mine_prefix[shared:] + their_prefix[shared:]
    tail = ('',) if rest else ()  # a parameter one path lacks is unset there
    for value in rest + tuple(item for item in (mine_tail, their_tail) if item != ()):
        tail = _join_values(tail, value)
    return prefix, tail


def _positional_from(raws, state, home):
    """``(prefix, tail)`` positional parameters argument words *raws* set:
    the value of each argument whose word always makes exactly one field,
    then the values of any number of further ones."""
    prefix, tail = [], ()
    for raw in raws:
        try:
            alternatives = _expand_word(raw, state, home).fields()
        except _EnvironmentDeferred:
            tail = _join_values(tail, _DEFERRED)
            continue
        except (_Unenumerable, _Overflowed):
            tail = _join_values(tail, None)
            continue
        if tail == () and all(len(fields) == 1 and not uncertain and not fields[0].glob
                              for fields, uncertain in alternatives):
            prefix.append(_value_set(_dedupe(_field_value(*fields[0])
                                             for fields, _uncertain in alternatives)))
            continue
        values = [_field_value(*field) for fields, _uncertain in alternatives
                  for field in fields]
        if any(uncertain for _fields, uncertain in alternatives):
            values.append(_DYNAMIC)
        if values:
            tail = _join_values(tail, _value_set(_dedupe(values)))
    return tuple(prefix), tail


def _shift(state, count):
    """``shift [count]``: the positional parameters move down; with *count*
    None (not a literal number) any of them may come first."""
    prefix, tail = state.positional
    if count is None:
        rest = ()
        for value in prefix + ((tail,) if tail != () else ()):
            rest = _join_values(rest, value)
        state.positional = ((), _join_values(rest, ('',)) if rest != () else ())
    else:
        state.positional = (prefix[count:], tail) if count <= len(prefix) else ((), tail)


_TRAP_ACTIONS = object()  # the memo key of the trap actions set in a command


class _TrapWatch:
    """A trap action and the join of every state reached since it was set: a
    signal, or ``exit`` for an EXIT trap, may run it at any of them."""

    __slots__ = ('text', 'seen')

    def __init__(self, text):
        self.text, self.seen = text, None

    def observe(self, state):
        if self.seen is None:
            self.seen = state.branch()
        else:
            self.seen.join(state)


def _directory_operands(literal, raw):
    """The operands of ``cd``/``pushd`` after their options (``-L``, ``-P``,
    ``-e``, ``-@``)."""
    for position in range(1, len(literal)):
        word = literal[position]
        if word == '--':
            return list(raw[position + 1:])
        if not (len(word) > 1 and word[0] == '-' and set(word[1:]) <= set('LPe@')):
            return list(raw[position:])
    return []


def _cd_destinations(state, operands, home):
    """``(directories, may stay)`` ``cd OPERANDS`` may enter; ``cd -`` is
    ``cd "$OLDPWD"`` and ``cd`` alone ``cd "$HOME"``. Raises
    ``_Unenumerable`` for a target this guard cannot follow (an unknown
    value, a pattern, an unknown ``$CDPATH``) and ``_EnvironmentDeferred`` for
    one only a caller with the environment and cwd can follow."""
    if len(operands) > 1:
        return [], True  # too many arguments: cd fails
    raw = operands[0] if operands else '"$HOME"'
    if _literal_command_word(raw) == '-':
        raw = '"$OLDPWD"'
    destinations, stays = [], False
    for fields, uncertain in _expand_word(raw, state, home).fields():
        if uncertain or len(fields) > 1:
            raise _Unenumerable()
        if not fields:
            stays = True
            continue
        text, names, glob, trusted = fields[0]
        if trusted or glob or names:
            raise _Unenumerable()  # an unknown value may name any directory
        if not text:
            stays = True  # `cd ""` stays where it is; an unset OLDPWD fails
        elif os.path.isabs(text):
            destinations.append(os.path.normpath(text))
        elif _searches_cdpath(state, text):
            destinations.extend(_cdpath_destinations(state, text))
        elif state.cwd_lost or not state.cwds:
            state.cwd_values()  # defers when the caller gave no cwd
            raise _Unenumerable()
        else:
            destinations.extend(os.path.normpath(os.path.join(base, text))
                                for base in state.cwds)
    return destinations, stays


def _cdpath_destinations(state, text):
    """Where ``cd TEXT`` goes when it searches ``$CDPATH``: for each value it
    may hold, the first entry under which TEXT is a directory now, else TEXT
    under the cwd. Raises ``_Unenumerable`` for a ``$CDPATH`` or cwd this
    guard cannot know."""
    value = state._held('CDPATH')
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise _Unenumerable()
    if state.cwd_lost or not state.cwds:
        state.cwd_values()  # defers when the caller gave no cwd
        raise _Unenumerable()
    destinations = []
    for cwd in state.cwds:
        for search in value:
            for entry in search.split(':'):
                candidate = os.path.normpath(os.path.join(cwd, entry, text))
                if os.path.isdir(candidate):
                    break
            else:
                candidate = os.path.normpath(os.path.join(cwd, text))
            destinations.append(candidate)
    return destinations


def _searches_cdpath(state, text):
    """Whether ``cd TEXT`` may search ``$CDPATH`` instead of the cwd."""
    if text.split('/', 1)[0] in ('.', '..'):
        return False
    value = state._held('CDPATH')
    return not isinstance(value, tuple) or any(item != '' for item in value)


def _change_directory(state, operands, home):
    """``cd OPERANDS``: the new cwd candidates; a successful cd sets
    ``$OLDPWD`` to the old ``$PWD`` and ``$PWD`` to the new directory. A cd
    into directories that all exist now succeeds. Any other may fail, so the
    old state stays possible; ``cd_outcome`` keeps both outcomes for the
    ``&&``/``||`` list the cd may lead."""
    try:
        destinations, _stays = _cd_destinations(state, operands, home)
    except (_Unenumerable, _EnvironmentDeferred, _Overflowed):
        destinations = None
    if destinations == []:
        return  # cd stays where it is, or fails
    old_pwd = state._held('PWD') if 'PWD' in state.bindings else (
        None if state.cwd_lost or not state.cwds else tuple(state.cwds))
    entered = None if destinations is None else list(_dedupe(destinations))[:_CWD_CAP]
    if entered is not None and all(os.path.isdir(path) for path in entered):
        state.bindings['OLDPWD'] = old_pwd
        if 'PWD' in state.bindings:
            state.bindings['PWD'] = tuple(entered)
        state.cwds, state.cwd_lost = entered, False
        return
    state.cd_outcome = (entered, (list(state.cwds), state.cwd_lost,
                                  state.bindings.get('PWD', _ABSENT),
                                  state.bindings.get('OLDPWD', _ABSENT)))
    state.bindings['OLDPWD'] = _join_values(state._held('OLDPWD'), old_pwd)
    if 'PWD' in state.bindings:
        state.bindings['PWD'] = _join_values(
            state.bindings['PWD'], None if destinations is None else _dedupe(destinations))
    if destinations is None:
        state.cwd_lost = True
    else:
        state.cwds = list(_dedupe(destinations + state.cwds))[:_CWD_CAP]


# ── Delete operands come only from commands a shell would run ──────
#
# ``command_structure.parse_shell_structure`` delimits the command the way a
# shell does and ``control_structure`` pairs its reserved words into the
# compound commands a shell executes. The walk below yields the operands of
# every rm/rmdir/unlink simple command that may run: in the command itself,
# in each ( … ) group (judged with the group's own bindings and cd hops), in
# command and process substitution bodies, in every branch, loop body and
# function body, and in text a shell executes — a heredoc or here-string
# read by a stdin shell (directly or later in its pipeline) and a ``-c``
# string. Heredoc bodies, quoted strings and Git pathspecs are data. Text the
# parser cannot delimit refuses only when it could run a delete.
#
# The state follows the shell's control flow. An ``&&``/``||`` operand, an
# ``if``/``case`` branch and a loop body run on their own copies of the
# state, and the state after the construct joins every path that reaches
# its end: an assignment that may not run never replaces the value the name
# held before. A loop's head is the fixpoint of its iterations; ``break``,
# ``continue`` and ``return`` carry their state to where they lead; a
# function body runs on the caller's state where the function is called.
# Decision: "Catastrophic-delete guard control flow" in
# docs/modules/tools_execution.md.

def _delete_events(items, state):
    """Yield ``('operand', raw word, state)`` for each rm/unlink operand the
    parsed script may run, in execution order, ``('empty-directory', raw
    word, state)`` for each rmdir operand, ``('opaque', text, error)`` for
    shell-executed text that cannot be parsed and ``('recursion', name,
    state)`` for a recursive call that is not followed. *state* follows
    the script's literal bindings, ``cd`` hops and function definitions
    along its control flow; groups and substitution bodies run in a fork."""
    known = state.memo.get(id(items))
    if known is None:
        known = state.memo[id(items)] = (items, control_structure(items))
    if known[1] is None:
        yield from _unstructured_events(items, state)
    else:
        yield from _list_events(known[1], items, state)


def _list_events(script, items, state):
    for and_or in script:
        yield from _and_or_events(
            and_or, items, state.fork() if and_or.background else state)


def _and_or_events(and_or, items, state):
    """``a && b || c``: an operand runs once the list so far succeeded
    (``&&``) or failed (``||``); the state after it joins both outcomes. An
    operand that leaves (:func:`_leaves`) ends the path that runs it."""
    (_, first), *rest = and_or.pipelines
    if not rest:
        yield from _pipeline_events(first, items, state)
        return
    state.cd_outcome = None
    yield from _pipeline_events(first, items, state)
    succeeded, failed = _outcomes(first, items, state)
    for operator, pipeline in rest:
        runs = succeeded if operator == '&&' else failed
        if runs is None:
            continue  # no path reaches it
        leaves = _leaves(pipeline, runs)
        runs.cd_outcome = None
        yield from _pipeline_events(pipeline, items, runs)
        ran_ok, ran_failed = (None, None) if leaves else _outcomes(pipeline, items, runs)
        if operator == '&&':
            succeeded, failed = ran_ok, _union(failed, ran_failed)
        else:
            succeeded, failed = _union(succeeded, ran_ok), ran_failed
    final = _union(succeeded, failed)
    if final is not None and final is not state:
        state.assign(final)


_ABSENT = object()  # a name that has no binding


def _outcomes(pipeline, items, state):
    """``(success, failure)`` states after *pipeline* ran on *state*: a
    single ``cd`` that may fail enters its directories on success and stays
    where it was on failure, the other way round under ``!``; any other
    pipeline leaves both the state it reached. The state the cd entered is
    *state* itself."""
    outcome, state.cd_outcome = state.cd_outcome, None
    failed = state.branch()
    if (outcome is not None and len(pipeline.commands) == 1
            and isinstance(pipeline.commands[0], ItemRun)):
        entered, (cwds, cwd_lost, pwd, oldpwd) = outcome
        if entered is not None:
            state.cwds, state.cwd_lost = entered, False
        failed.cwds, failed.cwd_lost = cwds, cwd_lost
        for name, value in (('PWD', pwd), ('OLDPWD', oldpwd)):
            if value is _ABSENT:
                failed.bindings.pop(name, None)
            else:
                failed.bindings[name] = value
        if _negated(pipeline.commands[0], items):
            return failed, state
    return state, failed


def _negated(run, items):
    """Whether ``!`` inverts the status of the pipeline *run* starts."""
    words = items[run.index].words
    return words[:len(words) - len(run.words)].count('!') % 2 == 1


def _union(mine, theirs):
    """Join two states that may reach the same point, None for no path."""
    if mine is None:
        return theirs
    if theirs is not None:
        mine.join(theirs)
    return mine


def _leaves(pipeline, state):
    """Whether *pipeline* always ends the path that runs it: ``exit``, a
    ``return`` in a function or ``break``/``continue`` in a loop (whose
    state :func:`_jump` already carried where it leads), alone or last in a
    ``{ … }`` group."""
    if len(pipeline.commands) != 1:
        return False  # each stage runs in a subshell
    node = pipeline.commands[0]
    if isinstance(node, Compound):
        if node.kind != 'group' or not node.parts[0]:
            return False
        last = node.parts[0][-1]
        return (len(last.pipelines) == 1 and not last.background
                and _leaves(last.pipelines[0][1], state))
    words = [word for word in node.words or () if not _ASSIGNMENT_WORD.match(word)]
    head = _literal_command_word(words[0]) if words else None
    if head == 'exit':
        return True
    if head == 'return':
        return state.jumps.call is not None
    return head in ('break', 'continue') and bool(state.jumps.loops)


def _pipeline_events(pipeline, items, state):
    if len(pipeline.commands) == 1:
        yield from _node_events(pipeline.commands[0], items, state)
        return
    for node in pipeline.commands:
        # Every stage of a pipeline runs in a subshell of its own.
        yield from _node_events(node, items, state.fork())


def _node_events(node, items, state):
    if isinstance(node, ItemRun):
        yield from _item_events(node, items, state)
        return
    for index in node.items:
        yield from _nested_events(items[index], state)
    yield from _COMPOUND_EVENTS[node.kind](node.parts, items, state)


def _nested_events(item, state):
    """Delete events of the substitutions an item's words and redirects run."""
    for script in item.nested:
        yield from _delete_events(script, state.fork())
    for heredoc in item.heredocs:
        for script in heredoc.nested:
            yield from _delete_events(script, state.fork())


def _item_events(run, items, state):
    item = items[run.index]
    yield from _nested_events(item, state)
    if isinstance(item, Subshell):
        yield from _delete_events(item.body, state.fork())
    else:
        yield from _command_run_events(items, run.index, run.words, state)
    for watch in state.traps:
        watch.observe(state)


def _command_run_events(items, index, words, state):
    """Judge item *index* run as the simple command *words* (its words after
    any reserved words): the scripts it hands a shell, then the command.
    Where its command word may be an alias, the alias text runs too."""
    aliased = _alias_expansions(words, state)
    if aliased is not None and not aliased:
        yield from _unaliased_run_events(items, index, words, state)
        return
    if aliased is None:
        text = '\n'.join([' '.join(words)] + [
            value for values in state.aliases.values() for value in values])
        if _could_run_delete(text):
            yield 'opaque', text, ShellStructureUnknown(
                'more alias texts than this guard follows', 0)
        aliased = ()
    outcomes = []
    for text, names in aliased:
        path = state.branch()
        path.alias_active = state.alias_active | names
        yield from _alias_text_events(items, index, text, path)
        path.alias_active = state.alias_active
        outcomes.append(path)
    plain = state.branch()
    yield from _unaliased_run_events(items, index, words, plain)
    outcomes.append(plain)
    state.assign(_joined(outcomes))


def _alias_text_events(items, index, text, state):
    """Run *text*, the command an alias made of item *index*. One simple
    command keeps the item's heredocs and redirects; other text runs as a
    script, and the item's stdin may be read by a shell it names."""
    parsed = _parsed_script(text, state)
    if (not isinstance(parsed, ShellStructureUnknown) and len(parsed) == 1
            and isinstance(parsed[0], SimpleCommand)
            and not (parsed[0].redirects or parsed[0].heredocs or parsed[0].nested)):
        alternative_items = list(items)
        alternative_items[index] = items[index]._replace(words=parsed[0].words)
        yield from _command_run_events(alternative_items, index, parsed[0].words, state)
        return
    yield from _delete_text_events(text, state)
    for body in _stdin_texts(items[index]):
        if _could_run_delete(body):
            yield 'opaque', body, ShellStructureUnknown(
                'the input of a command an alias names', 0)


def _alias_expansions(words, state):
    """``(text, names)`` for each command *words* may run where its command
    word, and after an alias value ending in a blank the next word, is an
    alias: the command with the aliases *names* expanded. [] when no alias
    applies; None past ``_DELETE_BINDING_CAP`` texts. An alias is never
    expanded inside its own text."""
    if not state.aliases:
        return []
    count = 0
    while count < len(words) and _ASSIGNMENT_WORD.match(words[count]):
        count += 1
    pending, expansions = [(list(words[:count]), count, frozenset())], []
    while pending:
        head, position, used = pending.pop()
        word = words[position] if position < len(words) else None
        values = (() if word is None or word in used or word in state.alias_active
                  else state.aliases.get(word, ()))
        if used:  # the next word itself: it may be no alias
            expansions.append((' '.join(head + list(words[position:])), used))
        for value in values:
            if value[-1:] in (' ', '\t'):
                pending.append((head + [value], position + 1, used | {word}))
            else:
                expansions.append(
                    (' '.join(head + [value] + list(words[position + 1:])), used | {word}))
        if len(expansions) + len(pending) > _DELETE_BINDING_CAP:
            return None
    return expansions


def _unaliased_run_events(items, index, words, state):
    elsewhere = state.elsewhere
    if not elsewhere and not _FOREIGN_FILESYSTEM_RUNNERS.isdisjoint(words):
        state.elsewhere = True
    position = _expanding_program_word(words, state)
    if position is not None:
        yield from _expanded_command_events(items, index, words, position, state)
    else:
        yield from _shell_input_events(items, index, words, state)
        yield from _simple_command_events(words, state)
    state.elsewhere = elsewhere


# Runners whose program sees the files of another host, container or root:
# a top-level path missing here may exist there.
_FOREIGN_FILESYSTEM_RUNNERS = frozenset({
    'ssh', 'chroot', 'nsenter', 'unshare', 'docker', 'podman', 'kubectl'})


def _expanding_program_word(words, state):
    """Index in *words* of the first program word the shell may make another
    program (a parameter or command expansion, a bash brace expression), the
    command word or the program a wrapper runs (``sudo $RM``), else None. A
    pattern keeps its basename (``/*/rm`` is ``rm``)."""
    if not any('$' in word or '`' in word or '{' in word for word in words):
        return None
    views = _views(words, state)
    if views is _UNKNOWN_PROGRAM:
        return None
    for _literal, raw in views:
        if not raw or not _program_word_expands(raw[0]):
            continue
        for start in range(len(words) - len(raw), -1, -1):
            if list(words[start:start + len(raw)]) == list(raw):
                return start
    return None


def _program_word_expands(raw):
    if shell_source_has_expansion(raw, pathname=False):
        return True
    try:
        return '{' in raw and _brace_variants(raw) is not None
    except _Overflowed:
        return True


def _views(words, state):
    """The :func:`_program_views` of *words*, computed once per judgement."""
    key = ('views', tuple(words))
    views = state.memo.get(key)
    if views is None:
        views = state.memo[key] = _program_views(list(words))
    return views


def _expanded_command_events(items, index, words, position, state):
    """``words[position]``, a program word the shell expands (``$RM -rf /``,
    ``"$@"`` in a wrapper function, ``sudo $RM``, ``$EMPTY rm …``,
    ``/bin/r? …``), runs the program its first field names with its other
    fields as the first arguments: each field list it may make is judged as
    the command it makes, with the item's heredocs and redirects. Where the
    program may be one this guard cannot name, the resolver reports it
    unresolvable when its text could run a delete."""
    home = state.home
    _default_assignments(words[:position + 1], state, home)
    try:
        alternatives = _expand_word(words[position], state, home).fields()
    except _EnvironmentDeferred:
        return  # the executor's pass knows the environment
    except (_Unenumerable, _Overflowed):
        alternatives = None
    unknown = alternatives is None
    choices = []
    for fields, uncertain in alternatives or ():
        made, may_be_unknown = _program_word_choices(fields, uncertain)
        choices.extend(made)
        unknown = unknown or may_be_unknown
    if unknown:
        text = ' '.join(words)
        if _could_run_delete(text):
            yield 'opaque', text, ShellStructureUnknown(
                'a program word this guard cannot expand', 0)
    choices = _dedupe(choices)
    if len(choices) == 1 and not unknown:
        yield from _program_choice_events(items, index, words, position, choices[0], state)
        return
    outcomes = [state.branch()] if unknown else []
    for choice in choices:
        path = state.branch()
        yield from _program_choice_events(items, index, words, position, choice, path)
        outcomes.append(path)
    if outcomes:
        state.assign(_joined(outcomes))


def _program_choice_events(items, index, words, position, choice, state):
    """Judge item *index* with its program word ``words[position]`` made the
    words *choice*."""
    expanded = tuple(words[:position]) + choice + tuple(words[position + 1:])
    alternative_items = list(items)
    alternative_items[index] = items[index]._replace(words=expanded)
    yield from _unaliased_run_events(alternative_items, index, expanded, state)


def _program_word_choices(fields, uncertain):
    """The words a program word making *fields* may start its command with,
    requoted, and whether its program may be one this guard cannot name: a
    value it cannot know names no program (owner decision 2026-11-17), and a
    pattern names the first file it matches."""
    rest = tuple(_requote(field) for field in fields[1:] if not field.trusted)
    if not fields:
        return ([] if uncertain else [rest]), bool(uncertain)
    program = fields[0]
    if program.trusted or program.names:
        return [], True
    if program.glob and _has_glob(os.path.basename(program.text)):
        matches = sorted(_glob(program.text))
        return ([tuple(_shell_quoted(match) for match in matches) + rest]
                if matches else []), False
    return [(_shell_quoted(program.text),) + rest], bool(uncertain)


def _shell_quoted(text):
    return "'" + text.replace("'", "'\\''") + "'"


def _requote(field):
    """A raw word that expands back to *field*: its text quoted, the pattern
    characters of a field pathname expansion applies to left bare."""
    if not field.glob:
        return _shell_quoted(field.text)
    return ''.join(part if part in _GLOB_CHARACTERS or part == ']' else _shell_quoted(part)
                   for part in re.split(r'([*?[\]])', field.text) if part)


def _group_events(parts, items, state):
    yield from _list_events(parts[0], items, state)


def _if_events(parts, items, state):
    """Each condition runs once those before it failed; at most one body
    runs, none when every condition fails and there is no ``else``."""
    clauses, else_body = parts
    outcomes = []
    for condition, body in clauses:
        yield from _list_events(condition, items, state)
        taken = state.branch()
        yield from _list_events(body, items, taken)
        outcomes.append(taken)
    rest = state.branch()
    if else_body is not None:
        yield from _list_events(else_body, items, rest)
    outcomes.append(rest)
    state.assign(_joined(outcomes))


def _while_events(parts, items, state):
    """``while``/``until``: the condition runs before every iteration and
    once more before the loop ends; the body runs any number of times."""
    condition, body = parts
    exits = _Exits()

    def iteration(path):
        yield from _list_events(condition, items, path)
        yield from _list_events(body, items, path)

    path = _in_loop(_loop_head(state, exits, iteration), state, exits)
    yield from _list_events(condition, items, path)
    leaving = path.branch()
    yield from _list_events(body, items, path)
    exits.join_into(leaving, 'breaks')
    state.assign(leaving)


def _for_events(parts, items, state):
    """``for``/``select``: the body runs once per listed word, any number of
    times for a computed list, ``select`` or an arithmetic ``for ((…))``."""
    header, body = parts
    name, values, runs = _loop_variable(header, state, state.home)
    if values == ():
        return  # an empty list never runs the body
    exits = _Exits()

    def iteration(path):
        if name is not None:
            path.bindings[name] = values
        yield from _list_events(body, items, path)

    head = _loop_head(state, exits, iteration)
    path = _in_loop(head, state, exits)
    yield from iteration(path)
    exits.join_into(path, 'continues', 'breaks')
    if not runs:
        path.join(head)
    state.assign(path)


def _case_events(parts, items, state):
    """At most one clause runs, unless ``;&`` falls through or ``;;&`` tests
    the patterns after it; none when no pattern matches (``*`` always does)."""
    outcomes, carried, matches_all = [], None, False
    for marker, body, terminator in parts[0]:
        path = state.branch()
        if carried is not None:
            path.join(carried)
        yield from _nested_events(items[marker], path)
        yield from _list_events(body, items, path)
        outcomes.append(path)
        matches_all = matches_all or '*' in items[marker].patterns
        if terminator in (';&', ';;&'):
            carried = path if carried is None else _joined([carried, path])
    if not matches_all:
        outcomes.append(state.branch())
    state.assign(_joined(outcomes))


# Functions a shell calls without the script naming them: bash runs
# command_not_found_handle (zsh command_not_found_handler) in place of any
# command it cannot find.
_SHELL_CALLED_FUNCTIONS = frozenset({'command_not_found_handle', 'command_not_found_handler'})


def _function_events(parts, items, state):
    """A definition runs nothing: its body runs on the caller's state wherever
    the name is called. The state here is not the one it runs with (``$1``,
    names assigned before the call), so the body is not judged here unless
    the shell itself may call it (:data:`_SHELL_CALLED_FUNCTIONS`)."""
    name, body = parts
    if name in _SHELL_CALLED_FUNCTIONS:
        yield from _node_events(body, items, state.fork())
    state.functions[name] = (_Definition(body, items),)


_COMPOUND_EVENTS = {
    'group': _group_events, 'if': _if_events, 'while': _while_events,
    'until': _while_events, 'for': _for_events, 'case': _case_events,
    'function': _function_events,
}


def _in_loop(head, state, exits):
    """A path into a loop from *head* whose ``break``/``continue`` lead to
    *exits*."""
    path = head.branch()
    path.jumps = _Jumps(state.jumps.loops + (exits,), state.jumps.call)
    return path


def _loop_head(state, exits, iteration):
    """The state at a loop's head: its entry joined with the end of every
    iteration and every ``continue`` until no path adds anything. The events
    of *iteration(path)* are judged later, once, from the returned head."""
    head = state.branch()
    while True:
        path = _in_loop(head, state, exits)
        for _event in iteration(path):
            pass
        merged = head.branch()
        merged.join(path)
        exits.join_into(merged, 'continues')
        if merged.signature() == head.signature():
            return head
        head = merged


def _joined(states):
    joined = states[0].branch()
    for state in states[1:]:
        joined.join(state)
    return joined


# Words that open, separate or close compound syntax, skipped in front of a
# command of a script whose reserved words do not pair up.
_UNPAIRED_SYNTAX = frozenset({
    '{', '}', '!', 'if', 'then', 'elif', 'else', 'fi', 'while', 'until', 'do',
    'done', 'time'})


def _unstructured_events(items, state):
    """A script whose reserved words do not pair up (a shell rejects it, but
    may run the lines before the error): every item may run or not, any
    number of times, in order."""
    def iteration(path):
        for index in range(len(items)):
            step = path.branch()
            yield from _unstructured_item_events(items, index, step)
            path.join(step)

    path = _loop_head(state, _Exits(), iteration).branch()
    yield from iteration(path)
    state.assign(path)


def _unstructured_item_events(items, index, state):
    item = items[index]
    yield from _nested_events(item, state)
    if isinstance(item, Subshell):
        yield from _delete_events(item.body, state.fork())
        return
    if not isinstance(item, SimpleCommand):
        return  # a case clause marker
    words = list(item.words)
    while words and words[0] in _UNPAIRED_SYNTAX:
        del words[0]
    if words[:1] in (['for'], ['select']):
        yield from _shell_input_events(items, index, words, state)
        name, values, _runs = _loop_variable(
            tuple(words), state, state.home)
        if name is not None and values != ():
            state.bindings[name] = values
        return
    yield from _command_run_events(items, index, words, state)


def _reads_shell_stdin(item):
    if not isinstance(item, SimpleCommand):
        return False
    from lib.project_mod.command_heredoc import shell_input_source
    views = _program_views(item.words)
    return (views is not _UNKNOWN_PROGRAM
            and any(shell_input_source(view[0]) == ('stdin', None) for view in views))


def _shell_string_script(raw):
    """(script text, literal) of a word a shell runs as source.

    A *literal* word (single-quoted, ANSI-C or expansion-free) reaches the
    shell that runs it as written; any other is expanded by this shell first
    (:func:`_script_texts`). None when the word is not one shell word.
    """
    if "$'" in raw:
        decoded = _ansi_c_word(raw)
        if decoded is not None:
            return decoded, True
    try:
        words = shlex.split(raw, comments=False, posix=True)
    except ValueError:
        return None
    if len(words) != 1:
        return None
    return words[0], raw.startswith("'") or not any(c in raw for c in '$`')


_ShellSource = namedtuple('_ShellSource', ['raw', 'script', 'arguments', 'heredoc', 'reader'])


def _shell_source_scripts(items, index, views):
    """Text a shell executes as a script of its own for ``items[index]``: the
    ``-c`` operand of a shell, and the heredocs/here-strings of a command whose
    stdin a shell reads (``bash <<'EOF'``, ``cat <<'EOF' | sh``).

    *views* are the item's ``(literal, raw)`` program views. Returns
    ``_ShellSource`` tuples in execution order: the raw text; *script*, the
    ``(text, literal)`` of :func:`_shell_string_script` (None when the raw
    word is not one shell word; for a heredoc, whether its delimiter was
    quoted); the raw words after a ``-c`` string (``$0``, then the
    positional parameters); whether the text is a heredoc body; the index of
    the item whose shell reads it, whose words give that shell's environment.
    """
    from lib.project_mod.command_heredoc import shell_command_operand, shell_input_source
    item = items[index]
    scripts = []
    reads_stdin = False
    for view in views:
        source = shell_input_source(view[0])
        if source is None:
            continue
        reads_stdin = reads_stdin or source == ('stdin', None)
        # The shell dequotes its program word and options (`"bash" -c …`); the
        # command string's own quoting decides whether this shell expands it.
        position = (shell_command_operand(view[0]) if source[0] == 'command' and source[1]
                    else None)
        if position is not None:
            raw_text = view[1][position]
            scripts.append(_ShellSource(raw_text, _shell_string_script(raw_text),
                                        tuple(view[1][position + 1:]), False, index))
    stdin_inputs = [_ShellSource(heredoc.body, (heredoc.body, heredoc.quoted), (), True, None)
                    for heredoc in item.heredocs]
    stdin_inputs += [_ShellSource(target, _shell_string_script(target), (), False, None)
                     for operator, target in item.redirects
                     if operator.endswith('<<<')]
    if stdin_inputs:
        reader = index if reads_stdin else _stdin_shell_stage(items, index)
        if reader is not None:
            scripts.extend(source._replace(reader=reader) for source in stdin_inputs)
    return scripts


def _stdin_shell_stage(items, index):
    """Index of a later stage of the pipeline of ``items[index]`` that may be a
    shell reading what the earlier stages print, else None."""
    for position in range(index + 1, len(items)):
        if items[position].separator != '|':
            return None
        if _reads_shell_stdin(items[position]):
            return position
    return None


def _stdin_texts(item):
    """Heredoc bodies and here-strings *item* feeds its program's stdin."""
    return ([heredoc.body for heredoc in item.heredocs]
            + [target for operator, target in item.redirects if operator.endswith('<<<')])


def _shell_input_events(items, index, words, state):
    """Judge text that a shell executes as a script of its own (see
    :func:`_shell_source_scripts`); *words* are the item's words after any
    reserved words."""
    views = _views(words, state)
    if views is _UNKNOWN_PROGRAM:
        # The hidden program may be a shell reading this stdin.
        for text in _stdin_texts(items[index]):
            if _could_run_delete(text):
                yield 'opaque', text, ShellStructureUnknown(
                    'a wrapper or runner option is not understood', 0)
        return
    home = state.home
    for source in _shell_source_scripts(items, index, views):
        if source.script is None:
            if _could_run_delete(source.raw):
                yield 'opaque', source.raw, ShellStructureUnknown(
                    'a shell script word this guard cannot read', 0)
            continue
        if not source.script[0].strip():
            continue
        text, literal = source.script
        texts = None
        if not literal:  # this shell expands it before the child runs it
            try:
                texts = _script_texts([source.raw], state, home, heredoc=source.heredoc)
            except _EnvironmentDeferred:
                texts = None
        positional = _positional_from(source.arguments[1:], state, home)
        zero = (_resolve_delete_value(source.arguments[0], state, home)
                if source.arguments else (_TRUSTED,))
        for script in texts or [text]:
            child = state.child(positional, zero)
            _command_environment(items[source.reader].words, state, child, home)
            yield from _delete_text_events(script, child)
    yield from _printed_script_events(items, index, views, state, home)


def _printed_script_events(items, index, views, state, home):
    """A shell later in the pipeline reads what ``items[index]`` prints
    (``echo 'rm -rf /' | sh``): judge the output this guard can tell (see
    :func:`_substitution_values`) as a child shell's script."""
    if len(views) != 1:
        return
    literal, raw = views[0]
    producer = _PRODUCERS.get(os.path.basename(literal[0]))
    if producer is None or _redirects_output(items[index]):
        return
    reader = _stdin_shell_stage(items, index)
    if reader is None:
        return
    try:
        printed = producer(literal, raw, state, home)
    except _EnvironmentDeferred:
        return  # the executor's pass knows the environment
    except (_Unenumerable, _Overflowed):
        text = ' '.join(raw)
        if _could_run_delete(text):
            yield 'opaque', text, ShellStructureUnknown(
                'output a shell reads that this guard cannot expand', 0)
        return
    for value in printed:
        text = _text_of(value)
        if text.strip():
            child = state.child()
            _command_environment(items[reader].words, state, child, home)
            yield from _delete_text_events(text, child)


def _command_environment(words, state, child, home):
    """Give *child*, the shell a command starts, the variables the command
    puts in its environment: ``NAME=value`` words in front of it or of
    ``env``, and ``env -i`` (an empty environment) or ``env -u NAME``."""
    position = 0
    while position < len(words):
        word = words[position]
        match = _ASSIGNMENT_WORD.match(word)
        if match and not match.group(2):
            child.bindings[match.group(1)] = _resolve_delete_value(
                word[match.end():], state, home)
        elif _literal_command_word(word) == 'env':
            position += 1
            while position < len(words) and _literal_command_word(words[position]).startswith('-'):
                option = _literal_command_word(words[position])
                if option in ('-i', '--ignore-environment', '-'):
                    child.bindings, child.environment = {}, {}
                elif option in ('-u', '--unset') and position + 1 < len(words):
                    position += 1
                    child.bindings[_literal_command_word(words[position])] = ('',)
                position += 1
            continue
        else:
            return
        position += 1


def _parsed_script(text, state):
    """The parse of shell source *text*, kept for the whole judgement; the
    ``ShellStructureUnknown`` when it cannot be parsed."""
    parsed = state.memo.get(text)
    if parsed is None:
        try:
            parsed = parse_shell_structure(text)
        except ShellStructureUnknown as error:
            parsed = error
        state.memo[text] = parsed
    return parsed


def _delete_text_events(text, state):
    """Delete events of shell source *text* run with *state*."""
    parsed = _parsed_script(text, state)
    if isinstance(parsed, ShellStructureUnknown):
        if _could_run_delete(text):
            yield 'opaque', text, parsed
        return
    yield from _delete_events(parsed, state)


def _simple_command_events(words, state):
    """Apply one simple command (its *words* after any reserved words) to
    *state*; yield its delete operands."""
    texts = list(words)
    if not texts:
        return
    home = state.home
    _default_assignments(texts, state, home)
    count = 0
    while count < len(texts) and _ASSIGNMENT_WORD.match(texts[count]):
        count += 1
    prefix, rest = texts[:count], texts[count:]
    if not rest:
        for token in prefix:
            _assign(state, token, home)
        return
    head = _literal_command_word(rest[0])
    if head.startswith('(('):
        _may_change(state, _SHELL_IDENTIFIER.findall(head))  # arithmetic may assign
        return
    if prefix and (head in _SPECIAL_BUILTINS or head in state.functions):
        _persist(prefix, state, home)
    if head in _DECLARATION_BUILTINS:
        _declaration(head, rest[1:], state, home)
        return
    if head in ('break', 'continue', 'return'):
        _jump(head, rest[1:], state)
        return
    views = _views(texts, state)
    if views is _UNKNOWN_PROGRAM:
        # A wrapper or runner whose options are not understood (`timeout $T`)
        # hides its program: its first delete word is taken as the program.
        text = ' '.join(texts)
        if _could_run_delete(text):
            yield 'opaque', text, ShellStructureUnknown(
                'a wrapper or runner option is not understood', 0)
            literal = [_literal_command_word(word) for word in texts]
            for start, word in enumerate(literal):
                if os.path.basename(word) in _DELETE_COMMANDS:
                    yield from _program_delete_events(
                        literal[start:], texts[start:], state, home)
                    break
        return
    for literal, raw in views:
        yield from _program_delete_events(literal, raw, state, home)
    if head in state.functions:
        yield from _function_call_events(head, rest[1:], prefix, state, home)


def _assign(state, token, home):
    """``NAME=value`` (``NAME+=value``) binds NAME; exported under ``set -a``."""
    match = _ASSIGNMENT_WORD.match(token)
    name, value = match.group(1), token[match.end():]
    if match.group(2):
        value = '${%s}%s' % (name, value)  # appending
    state.bindings[name] = _resolve_delete_value(value, state, home)
    if state.allexport == 'on':
        state.exports[name] = True
    elif state.allexport == 'maybe' and state._exported(name) is not True:
        state.exports[name] = 'maybe'


def _persist(prefix, state, home):
    """Assignments in front of a special builtin or a function call may outlive
    it (POSIX) or not (bash): either value stays possible."""
    for token in prefix:
        name = _ASSIGNMENT_WORD.match(token).group(1)
        before = state._held(name)
        _assign(state, token, home)
        state.bindings[name] = _join_values(before, state.bindings[name])


def _may_change(state, names):
    """Names a command may have assigned: their old values stay possible."""
    for name in names:
        state.bindings[name] = _join_values(state._held(name), None)


def _set_dynamic(state, names):
    """Names a command assigns values this guard cannot know."""
    for name in names:
        if _SHELL_IDENTIFIER.fullmatch(name):
            state.bindings[name] = None


def _default_assignments(texts, state, home):
    """``${NAME:=word}`` (``${NAME=word}``) in any word assigns NAME its
    default when it is unset or empty."""
    for raw in texts:
        for match in _DEFAULT_ASSIGNMENT.finditer(raw):
            end = _parameter_end(raw, match.start() + 2)
            if end is None or _in_single_quotes(raw, match.start()):
                continue
            name = match.group(1)
            try:
                default = _expand_word(raw[match.end():end], state, home,
                                       split=False).values()
                values = _value_tuple(state.get_value(name))
            except (_EnvironmentDeferred, _Unenumerable, _Overflowed):
                _may_change(state, (name,))
                continue
            state.bindings[name] = _value_set(_dedupe(
                item for value in values
                for item in (default if _may_be_empty(value) else (value,))))


def _in_single_quotes(raw, index):
    """Whether *index* of shell word *raw* lies inside single quotes."""
    quote, position = None, 0
    while position < index:
        char = raw[position]
        if char == '\\' and quote != "'":
            position += 2
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        position += 1
    return quote == "'"


def _declaration(head, raws, state, home):
    """``export``/``readonly``/``declare``/``typeset``/``local``: options,
    then ``NAME[=value]`` operands."""
    flags, operands = set(), []
    for position, raw in enumerate(raws):
        word = _literal_command_word(raw)
        if word == '--':
            operands = list(raws[position + 1:])
            break
        if len(word) > 1 and word[0] in '-+' and word[1:].isalpha():
            flags.update(word[0] + letter for letter in word[1:])
            continue
        operands = list(raws[position:])
        break
    exporting = (head == 'export' and '-n' not in flags) or '-x' in flags
    withdrawing = (head == 'export' and '-n' in flags) or '+x' in flags
    untracked = head != 'export' and bool(flags & {'-a', '-A', '-n'})  # arrays, namerefs
    local = state.jumps.call if head == 'local' or (
        head in ('declare', 'typeset') and '-g' not in flags) else None
    for raw in operands:
        match = _ASSIGNMENT_WORD.match(raw)
        name = match.group(1) if match else _literal_command_word(raw)
        if not _SHELL_IDENTIFIER.fullmatch(name):
            continue
        if '-f' in flags:
            if exporting:
                state.exported_functions |= {name}
            continue
        if local is not None:
            held = state._held(name)
            local.saved[name] = (_join_values(local.saved[name], held)
                                 if name in local.saved else held)
        if match and untracked:
            state.bindings[name] = None
        elif match:
            _assign(state, raw, home)
        elif local is not None:
            # bash unsets a new local; dash keeps the value it had.
            state.bindings[name] = _join_values(state._held(name), ('',))
        if exporting or withdrawing:
            state.exports[name] = exporting


def _jump(head, arguments, state):
    """Carry *state* to where ``break``/``continue`` [N] or ``return`` lead."""
    if head == 'return':
        if state.jumps.call is not None:
            state.jumps.call.reach('returns', state)
        return
    loops = state.jumps.loops
    count = arguments[0] if arguments else '1'
    if count.isdigit() and int(count) > 0:
        loops = loops[max(len(loops) - int(count), 0):][:1]
    for exits in loops:  # an unknown count may leave any of them
        exits.reach('breaks' if head == 'break' else 'continues', state)


def _function_call_events(name, arguments, prefix, state, home):
    """Run the body of shell function *name* on the caller's *state*, with
    the call's *arguments* as its positional parameters and its *prefix*
    assignments in force."""
    positional = _positional_from(arguments, state, home)
    outcomes = []
    for definition in state.functions[name]:
        path = state.branch()
        if definition in state.calls:
            yield 'recursion', name, path  # a recursive call is not followed
        elif definition is not None:
            exits = _Exits()
            path.jumps = _Jumps(state.jumps.loops, exits)
            path.calls = state.calls | {definition}
            path.positional = positional
            for token in prefix:
                _assign(path, token, home)
            yield from _node_events(definition.body, definition.items, path)
            exits.join_into(path, 'returns')
            for local, saved in exits.saved.items():
                # A path that made the name local gets its old value back; a
                # path that never ran `local` keeps what it assigned.
                path.bindings[local] = _join_values(saved, path._held(local))
            path.jumps, path.calls, path.positional = state.jumps, state.calls, state.positional
        outcomes.append(path)
    state.assign(_joined(outcomes))


def _program_delete_events(literal, raw, state, home):
    """Apply one program view to *state*; yield its delete operands."""
    head = os.path.basename(literal[0])
    effect = _BUILTIN_EFFECTS.get(head)
    if effect is not None:
        yield from effect(literal, raw, state, home) or ()
        return
    if head not in _DELETE_COMMANDS:
        return
    kind = 'empty-directory' if head == 'rmdir' else 'operand'
    end_of_options = False
    for literal_word, raw_word in zip(literal[1:], raw[1:]):
        if not end_of_options:
            if literal_word == '--':
                end_of_options = True
                continue
            if literal_word.startswith('-'):
                continue
        yield kind, raw_word, state


def _cd_effect(literal, raw, state, home):
    _change_directory(state, _directory_operands(literal, raw), home)


def _pushd_effect(literal, raw, state, home):
    """``pushd DIR`` enters DIR like ``cd``; the directory stack is not
    tracked, so ``pushd`` alone or ``pushd +N`` goes somewhere unknown."""
    operands = _directory_operands(literal, raw)
    if len(operands) == 1 and not re.fullmatch(r'[+-][0-9]+',
                                                _literal_command_word(operands[0])):
        _change_directory(state, operands, home)
    else:
        _popd_effect(literal, raw, state, home)


_ALIAS_NAME = re.compile(r"[^\s'\"\\$`=<>|&;()]+")


def _alias_effect(literal, raw, state, home):
    """``alias NAME=VALUE …``: a command read later whose word is NAME may
    run VALUE (dash, and bash in POSIX mode, expand aliases in scripts).
    Every value a name held stays possible: a function body keeps the
    aliases of its definition, and ``unalias`` does not undo a command
    already read."""
    for word in raw[1:]:
        try:
            texts = _script_texts([word], state, home)
        except _EnvironmentDeferred:
            continue  # the executor's pass knows the environment
        if texts is None:
            if _could_run_delete(word):
                yield 'opaque', word, ShellStructureUnknown(
                    'an alias value this guard cannot expand', 0)
            continue
        for text in texts:
            name, separator, value = text.partition('=')
            if separator and _ALIAS_NAME.fullmatch(name):
                state.aliases[name] = _dedupe(state.aliases.get(name, ()) + (value,))


def _popd_effect(literal, raw, state, home):
    """``popd`` returns to a directory of the stack, which is not tracked."""
    state.cwd_lost = True
    state.bindings['OLDPWD'] = _join_values(state._held('OLDPWD'), tuple(state.cwds) or None)
    if 'PWD' in state.bindings:
        state.bindings['PWD'] = None


def _eval_events(literal, raw, state, home):
    """``eval`` runs, in this shell, the text its operands expand to joined by
    spaces."""
    try:
        texts = _script_texts(raw[1:], state, home)
    except _EnvironmentDeferred:
        return  # the executor's pass knows the environment
    if texts is None:
        text = ' '.join(raw[1:])
        if _could_run_delete(text):
            yield 'opaque', text, ShellStructureUnknown(
                'eval operands this guard cannot expand', 0)
        return
    outcomes = []
    for text in texts:
        path = state.branch()
        yield from _delete_text_events(text, path)
        outcomes.append(path)
    state.assign(_joined(outcomes))


def _source_effect(literal, raw, state, home):
    """``source FILE`` (``. FILE``) runs a file this guard does not read: any
    name the command bound may have changed; inherited names are trusted."""
    _may_change(state, list(state.bindings))


def _unset_effect(literal, raw, state, home):
    functions = '-f' in literal[1:]
    for name in literal[1:]:
        if not _SHELL_IDENTIFIER.fullmatch(name):
            continue
        if functions:
            state.functions.pop(name, None)
        else:
            state.bindings[name] = ('',)


def _read_effect(literal, raw, state, home):
    values, operands = _builtin_options(literal, 'adinNptu')
    _set_dynamic(state, operands + values.get('a', []) or ['REPLY'])


def _getopts_effect(literal, raw, state, home):
    _set_dynamic(state, list(literal[2:3]) + ['OPTARG', 'OPTIND'])


def _printf_effect(literal, raw, state, home):
    values, _operands = _builtin_options(literal, 'v')
    _set_dynamic(state, values.get('v', []))


def _mapfile_effect(literal, raw, state, home):
    _values, operands = _builtin_options(literal, 'dnOsuCc')
    _set_dynamic(state, operands[:1] or ['MAPFILE'])


def _let_effect(literal, raw, state, home):
    _may_change(state, [name for word in literal[1:]
                        for name in _SHELL_IDENTIFIER.findall(word)])


def _set_effect(literal, raw, state, home):
    """``set``: ``-a``/``-o allexport`` switch exporting; ``--`` (or the first
    operand) sets the positional parameters; other options change no name."""
    position = 1
    while position < len(literal):
        word = literal[position]
        if word in ('--', '-'):
            state.positional = _positional_from(raw[position + 1:], state, home)
            return
        if word in ('-o', '+o'):
            if literal[position + 1:position + 2] == ['allexport']:
                state.allexport = 'on' if word == '-o' else 'off'
            position += 2
            continue
        if len(word) > 1 and word[0] in '-+':
            if 'a' in word[1:]:
                state.allexport = 'on' if word[0] == '-' else 'off'
            position += 1
            continue
        state.positional = _positional_from(raw[position:], state, home)
        return


def _shift_effect(literal, raw, state, home):
    count = literal[1] if len(literal) > 1 else '1'
    _shift(state, int(count) if count.isdigit() else None)


def _trap_action(raw):
    """The action word of ``trap ACTION SIGNAL…`` (*raw*: the trap's words),
    or None when the command sets no action (it prints, resets or ignores)."""
    operands = list(raw[1:])
    if operands and _literal_command_word(operands[0]) == '--':
        operands.pop(0)
    if len(operands) < 2:
        return None
    action = _literal_command_word(operands[0])
    if action in ('-', '') or action.isdigit() or action.startswith('-'):
        return None
    return operands[0]


def _trap_effect(literal, raw, state, home):
    """``trap ACTION SIGNAL…`` sets an action that runs later in this shell; it
    is judged after the command with every state reached once it was set. A
    double-quoted action expands now, a literal one when it runs."""
    action = _trap_action(raw)
    if action is None:
        return
    script = _shell_string_script(action)
    if script is None:
        texts = None
    elif script[1]:
        texts = [script[0]]
    else:
        try:
            texts = _script_texts([action], state, home)
        except _EnvironmentDeferred:
            return  # the executor's pass knows the environment
    if texts is None:
        if _could_run_delete(action):
            yield 'opaque', action, ShellStructureUnknown(
                'a trap action this guard cannot expand', 0)
        return
    watches = state.memo.setdefault(_TRAP_ACTIONS, {})
    for text in texts:
        if text.strip():
            watch = watches.setdefault(text, _TrapWatch(text))
            watch.observe(state)
            if watch not in state.traps:
                state.traps += (watch,)


def _builtin_options(literal, value_letters):
    """``(option values by letter, operands)`` of a builtin with getopt-style
    options; the letters in *value_letters* take a value."""
    values, position = {}, 1
    while position < len(literal):
        word = literal[position]
        if word == '--':
            position += 1
            break
        if len(word) < 2 or word[0] != '-':
            break
        for index in range(1, len(word)):
            if word[index] in value_letters:
                value = word[index + 1:]
                if not value and position + 1 < len(literal):
                    position += 1
                    value = literal[position]
                values.setdefault(word[index], []).append(value)
                break
        position += 1
    return values, list(literal[position:])


_BUILTIN_EFFECTS = {
    'cd': _cd_effect, 'pushd': _pushd_effect, 'popd': _popd_effect,
    'eval': _eval_events, 'source': _source_effect, '.': _source_effect,
    'unset': _unset_effect, 'read': _read_effect, 'getopts': _getopts_effect,
    'printf': _printf_effect, 'mapfile': _mapfile_effect,
    'readarray': _mapfile_effect, 'let': _let_effect, 'set': _set_effect,
    'shift': _shift_effect, 'trap': _trap_effect, 'alias': _alias_effect,
}


def _command_events(items, state):
    """Delete events of a whole command: its script, then each trap action set
    in it, with every state reached after the trap was set."""
    yield from _delete_events(items, state)
    judged = set()
    watches = state.memo.get(_TRAP_ACTIONS, {})
    while len(judged) < len(watches):
        for text, watch in list(watches.items()):
            if text not in judged:
                judged.add(text)
                yield from _delete_text_events(text, watch.seen.fork())


def _repository_metadata_anchors(cwd):
    """Git metadata of every checkout holding *cwd*: marker, git dir, common dir.

    Deleting one of these (or an ancestor) destroys the history and refs of
    the checkout the command runs in. Discovery reads ``.git`` markers only
    (``workspace_lease`` owns it); unreadable metadata adds no anchor, and
    the depth/home/workspace rules still apply.
    """
    if not cwd:
        return ()
    try:
        from lib.project_mod.workspace_lease import _checkout_chain
        chain = _checkout_chain(cwd)
    except Exception as error:
        logger.debug('[RunCommand] repository metadata anchors unavailable for %s: %s',
                     cwd, error)
        return ()
    anchors = []
    for binding in chain:
        root, common, git_dir = binding[0], str(binding[1]), binding[2]
        for path in (os.path.join(root, '.git'), git_dir, common):
            path = os.path.normpath(path)
            if path not in anchors:
                anchors.append(path)
    return tuple(anchors)


def _names_something(path):
    """Whether *path* names an existing file, directory or link; a pattern,
    whether it matches one."""
    return bool(_glob(path)) if _has_glob(path) else os.path.lexists(path)


class _DeleteJudge:
    """Depth, anchor and containment rules for the paths one command's delete
    operands name."""

    def __init__(self, cwd, ws_real):
        self.home = os.path.expanduser('~')
        self.cwd = os.path.normpath(cwd) if cwd and os.path.isabs(cwd) else None
        self.ws_real = ws_real
        self.protected = []
        if self.home and os.path.isabs(self.home):
            self.protected.append(('home', os.path.normpath(self.home)))
        if self.cwd:
            # The directory the command starts in, not one a cd inside it reaches.
            self.protected.append(('workspace', self.cwd))
        self._repository = None

    def _repository_anchors(self):
        if self._repository is None:
            self._repository = _repository_metadata_anchors(self.cwd)
        return self._repository

    def _anchor_reason(self, normalized):
        for label, anchor in self.protected:
            if normalized == anchor or anchor.startswith(normalized + os.sep):
                return (label, f'target is the {label} directory '
                               f'({normalized}) itself or an ancestor of it')
        if (self.cwd and normalized.startswith(self.cwd.rstrip(os.sep) + os.sep)
                and '.git' not in normalized.split(os.sep)):
            return None  # below the working directory: no metadata of its checkout
        if _has_glob(normalized) or not os.path.lexists(normalized):
            return None  # no Git metadata at or below it; a pattern never equals an anchor
        repository = self._repository_anchors()
        if repository:
            real = os.path.realpath(normalized)
            for anchor in repository:
                for target in (normalized, real):
                    if target == anchor or anchor.startswith(target.rstrip(os.sep) + os.sep):
                        return ('repository',
                                f'target {normalized} is the Git metadata ({anchor}) of '
                                'the checkout holding the working directory or an '
                                'ancestor of it')
        return None

    def __call__(self, raw_operand, state):
        try:
            expanded = _expand_delete_operand(raw_operand, state, self.home)
        except _EnvironmentDeferred:
            return None  # the executor's pass knows the environment and cwd
        if expanded is None:
            return None  # more expansions than this guard follows: no path to name
        for candidate, unproven_names in zip(expanded.candidates, expanded.collapsed):
            if unproven_names:
                continue  # it rests on a value this guard cannot know being empty
            glob_marked = candidate.endswith(_GLOB_MARK)
            text = candidate[:-len(_GLOB_MARK)] if glob_marked else candidate
            if not text:
                continue  # an empty operand deletes nothing
            if not os.path.isabs(text) and (state.cwd_lost or not state.cwds):
                continue  # relative to a directory this guard cannot name
            # The glob marker rides on the joined path so _candidate_depth
            # can still collapse a pure-glob tail to its parent.
            marker = _GLOB_MARK if glob_marked else ''
            paths = [text + marker] if os.path.isabs(text) else [
                os.path.join(base, text) + marker for base in state.cwds]
            for path in paths:
                normalized, depth = _candidate_depth(path)
                verdict_reason = None
                if depth < _MIN_DELETE_DEPTH and (
                        state.elsewhere or _names_something(normalized)):
                    # A shallow path that names nothing here holds nothing the
                    # command did not create itself; another host's may.
                    verdict_reason = (
                        'shallow',
                        f'resolves to {normalized} (<{_MIN_DELETE_DEPTH} '
                        'path components)')
                if verdict_reason is None:
                    verdict_reason = self._anchor_reason(normalized)
                if verdict_reason is None and self.ws_real:
                    tgt_real = os.path.realpath(normalized)
                    if not (tgt_real == self.ws_real
                            or tgt_real.startswith(self.ws_real + os.sep)):
                        verdict_reason = ('containment',
                                          'target resolves outside the workspace')
                if verdict_reason is not None:
                    reason, detail = verdict_reason
                    empty = _empty_names(raw_operand, state)
                    if empty and reason in ('shallow', 'home'):
                        detail += f' when {", ".join(empty)} is empty'
                    return (raw_operand, reason, detail)
        return None


def _empty_names(raw_operand, state):
    """The variables *raw_operand* reads that are empty on some path here
    (unset, assigned empty), named in a refusal."""
    names = []
    for name in dict.fromkeys(_REFERENCED_NAME.findall(raw_operand)):
        value = None if name in ('PWD', 'HOME') else state._held(name)
        if isinstance(value, tuple) and '' in value:
            names.append(f'${name}')
    return names


def _unparsed_excerpt(text, error):
    start = max(0, min(error.offset, len(text)) - 20)
    return text[start:start + 40]


def _catastrophic_delete_detail(command, cwd=None, *, environment=os.environ):
    """Judge *command* for catastrophic deletes; return (target, reason, detail).

    *target* is the operand text as written, *reason* is one of ``shallow``,
    ``home``, ``workspace``, ``repository`` or ``containment``, and *detail*
    is a short human-readable explanation for the refusal message. Returns
    None unless a delete the command may run names a protected path this
    guard can resolve: an unknown value, program, directory or shell
    structure passes (owner decision 2026-11-17, "Catastrophic-delete guard
    precision" in docs/modules/tools_execution.md).

    *environment* is the mapping the command's shell starts with: by default
    this process's, which the children it starts inherit; run_command passes
    the child environment it built. None means the caller does not know it
    (the cwd-less safety pre-hook): an operand that reads a name the command
    never bound, or ``$PWD`` without a cwd, is left to the executor's pass.
    """
    if not command or not _could_run_delete(command):
        return None
    # Workspace containment only engages for restricted principals AND when a
    # workspace cwd is known. Import locally to avoid a module import cycle.
    ws_real = None
    try:
        from lib.project_mod.abs_path_guard import is_restricted
        if cwd and is_restricted():
            ws_real = os.path.realpath(cwd)
    except Exception as e:
        logger.debug('[RunCommand] workspace containment resolve failed: %s', e)
        ws_real = None
    try:
        items = parse_shell_structure(command)
    except ShellStructureUnknown:
        return None
    judge = _DeleteJudge(cwd, ws_real)
    for kind, payload, context in _command_events(items, _DeleteGuardState(cwd, environment)):
        if kind == 'operand':
            verdict = judge(payload, context)
            if verdict is not None:
                return verdict
    return None


def resolve_delete_targets(command, cwd=None, *, environment=os.environ):
    """Resolve every rm/rmdir/unlink operand to absolute candidate paths.

    Returns a list of ``(operand_text, [absolute candidates], unproven_names)``
    triples — one per delete operand the command may run, from the same
    parse, in-command bindings and ``cd`` tracking as
    :func:`_catastrophic_delete_detail`. ``unproven_names`` is non-empty when
    a variable had no provable literal value (candidates include the empty
    collapse); candidates are None (``{'<opaque>'}``) for an operand or
    shell text that cannot be resolved. This is the shared operand resolver:
    the catastrophic-depth guard and the desktop agent's share-root
    containment judge the SAME resolved targets instead of re-parsing.
    *environment* is the one :func:`_catastrophic_delete_detail` takes.
    """
    if not command or not command.strip():
        return []
    try:
        items = parse_shell_structure(command)
    except ShellStructureUnknown as error:
        if not _could_run_delete(command):
            return []
        return [(_unparsed_excerpt(command, error), None, {'<opaque>'})]
    home = os.path.expanduser('~')
    resolved_targets = []
    for kind, payload, context in _command_events(items, _DeleteGuardState(cwd, environment)):
        if kind in ('operand', 'empty-directory'):
            resolved_targets.append(_resolved_target(payload, context, home))
        elif kind == 'opaque':
            resolved_targets.append((_unparsed_excerpt(payload, context), None, {'<opaque>'}))
        elif _could_run_delete(command):  # a recursive call, not followed
            resolved_targets.append((payload, None, {'<opaque>'}))
    return resolved_targets


def _resolved_target(operand, state, home):
    """``(operand, absolute candidates, unproven names)`` of one operand."""
    try:
        expanded = _expand_delete_operand(operand, state, home)
    except _EnvironmentDeferred:
        expanded = None
    if expanded is None:
        return operand, None, {'<opaque>'}
    paths = []
    for candidate in expanded.candidates:
        text = candidate[:-len(_GLOB_MARK)] if candidate.endswith(_GLOB_MARK) else candidate
        if not text:
            continue
        if os.path.isabs(text):
            paths.append(os.path.normpath(text))
        elif state.cwd_lost:
            return operand, None, {'<opaque>'}  # relative to a cd not followed
        else:
            paths.extend(os.path.normpath(os.path.join(base, text))
                         for base in state.cwds)
    return operand, paths, expanded.unproven


_DELETE_REFUSAL_MESSAGES = {
    'shallow': 'refusing to delete top-level path',
    'home': 'refusing to delete the home directory',
    'workspace': 'refusing to delete the workspace directory',
    'repository': 'refusing to delete the repository metadata',
    'containment': 'refusing to delete outside the workspace',
}


def delete_refusal_message(detail):
    """One model-facing sentence for a ``_catastrophic_delete_detail`` verdict.

    Every enforcement point (run_command, the safety pre-hook, the desktop
    agent) shares this so the refusal names the REAL reason — a shallow path,
    the home/workspace anchor or the repository metadata — never the generic
    "scope the deletion" advice that misdiagnosed the shell-local ``$BAD``
    case in conv mvl6adj1bcd1r1.
    """
    target, reason, why = detail
    lead = _DELETE_REFUSAL_MESSAGES.get(reason, 'refusing to delete')
    return f'Command blocked for safety: {lead} {target!r} — {why}.'


def _is_catastrophic_delete(command, cwd=None, *, environment=os.environ):
    """Return the offending path if *command* deletes a forbidden target.

    Three independent rules; any one rejects:

      1. **Depth rule (always on).** A delete (``rm``/``rmdir``/``unlink``)
         whose resolved absolute target has fewer than ``_MIN_DELETE_DEPTH``
         path components (``/``, ``/mnt``, ``/home``) is catastrophic and
         refused for every caller — this is never legitimate.

      2. **Anchor rule (always on).** Deleting the home directory, the
         command's own cwd, the Git metadata (``.git`` marker, git dir,
         common dir) of a checkout holding the cwd, or an ancestor of any of
         them is refused even when deep enough to pass rule 1.

      3. **Workspace-containment rule (restricted callers only).** When the
         current task runs in a *restricted* context — i.e. a remote /
         ``agent/run`` principal — any absolute delete whose realpath falls
         OUTSIDE the active workspace ``cwd`` is refused, even if it is deep
         enough to pass rule 1. Local desktop / CLI callers are NOT
         restricted, so their ability to delete outside the project (e.g.
         ``rm -rf ~/old_build``) is preserved — no product regression.

    Delete operands come only from rm/rmdir/unlink commands a shell would
    run (:mod:`lib.project_mod.command_structure` delimits the command):
    heredoc bodies, quoted strings and Git pathspecs are data, while ( … )
    groups, command/process substitutions and text a shell executes (stdin
    heredocs of ``bash``/``| sh``, ``sh -c`` strings) are judged as commands.
    Literal in-command bindings, ``cd`` hops and function definitions are
    tracked per shell along the command's control flow (a branch, loop or
    function body that may not run never replaces a value), a variable the
    command never binds takes its value from the starting *environment*,
    and an unproven variable is judged as possibly EMPTY (the
    ``rm -rf "$UNSET/"`` collapse). The program a command runs is read
    through :func:`_program_views`, the view the dangerous-command guard
    reads: assignment prefixes, wrappers (``sudo``/``doas``, ``env``,
    ``command``, ``exec``, ``nohup``, ``timeout``, ``nice``, ``xargs`` …),
    runners (``setsid``, ``su -c``, ``ssh``, ``find -exec``, ``docker exec`` …),
    ``eval`` operands and quoting of the command word (``\\rm``) are seen
    through. Text the parser cannot delimit, and a wrapper or runner whose
    options are not understood, are refused only when they could run a
    delete command.
    """
    detail = _catastrophic_delete_detail(command, cwd=cwd, environment=environment)
    return detail[0] if detail else None


# ── Dangerous commands are judged by the words a shell would run ───
#
# The rules apply to the program word and operands of every simple command
# the parse says a shell may run: the command itself, ( … ) groups,
# command/process substitution bodies and text a shell executes (a ``-c``
# string, a heredoc or here-string a shell reads, ``eval`` operands).
# Quoted arguments, plain arguments, flags and heredoc bodies fed to other
# programs are data. Text the parser cannot delimit, wrappers whose options
# are not understood and program names computed by a substitution get the
# raw ``DANGEROUS_PATTERNS`` match, so unknown structure fails closed only
# when a pattern is present. Decision:
# "Dangerous-command guard structure" in docs/modules/tools_execution.md.

_DANGEROUS_RE = re.compile('|'.join(f'(?:{p})' for p in DANGEROUS_PATTERNS))
# cmd.exe also resolves `shutdown.exe`/`format.com`, and its command names and
# switches ignore case; POSIX program names do not.
_WINDOWS_PROGRAM_SUFFIX = re.compile(r'\.(?:exe|com)\Z', re.IGNORECASE)
_DRIVE_OPERAND = re.compile(r'[A-Za-z]:')
_RECURSIVE_SHORT_OPTION = re.compile(r'-[A-Za-z]*R[A-Za-z]*\Z')
# xargs options (GNU and BSD): letters taking a value (attached or the next
# word), letters whose optional value can only be attached, and flags.
_XARGS_VALUE_LETTERS = frozenset('adEIJLnPRsS')
_XARGS_ATTACHED_LETTERS = frozenset('eil')
_XARGS_FLAG_LETTERS = frozenset('0oprtx')
_XARGS_VALUE_LONG = frozenset({
    '--arg-file', '--delimiter', '--max-args', '--max-chars', '--max-procs',
    '--process-slot-var'})
_XARGS_FLAG_LONG = frozenset({
    '--eof', '--exit', '--interactive', '--max-lines', '--no-run-if-empty',
    '--null', '--open-tty', '--replace', '--show-limits', '--verbose'})
# A wrapper whose options are not understood hides its program word.
_UNKNOWN_PROGRAM = object()


def _xargs_operand(words):
    """Index of the program ``xargs`` at ``words[0]`` runs, else None
    (options not understood, or no program: xargs then runs ``echo``)."""
    position = 1
    while position < len(words):
        word = words[position]
        if word == '--':
            position += 1
            break
        if not word.startswith('-') or word == '-':
            break
        position += 1
        if word.startswith('--'):
            name = word.partition('=')[0]
            if name in _XARGS_VALUE_LONG and '=' not in word:
                position += 1
            elif name not in _XARGS_VALUE_LONG and name not in _XARGS_FLAG_LONG:
                return None
            continue
        for offset, letter in enumerate(word[1:], start=2):
            if letter in _XARGS_FLAG_LETTERS:
                continue
            if letter in _XARGS_VALUE_LETTERS:
                if offset == len(word):
                    position += 1  # the value is the next word
            elif letter not in _XARGS_ATTACHED_LETTERS:
                return None
            break  # the rest of the word is this option's value
    return position if position < len(words) else None


# ── Runners: programs that execute an operand as a command ──────────
#
# Each runner's options follow its documented getopt grammar: short letters
# that are flags ('f'), take a value attached or as the next word ('v'), or
# take an optional value that can only be attached ('o'); long names map to
# (key, kind) and take ``--name=value`` (or the next word for 'v'). GNU
# programs without a leading ``+`` in their option string permute, so their
# options may follow operands. An option outside the table makes the runner
# opaque, which falls back like an unknown wrapper option.
_RunnerGrammar = namedtuple('_RunnerGrammar', 'short long permute')


def _runner_grammar(short, long=(), permute=False):
    letters = {}
    for kind, chars in zip('fvo', short.split('|')):
        letters.update(dict.fromkeys(chars, kind))
    names = {}
    for spec in long:
        name, _, rest = spec.partition(':')
        key, _, kind = rest.partition('/')
        names[name] = (key or name, kind or 'f')
    return _RunnerGrammar(letters, names, permute)


def _runner_options(words, grammar, start=1):
    """``(options, operand indices)`` of ``words[start:]``, else None."""
    options, operands = {}, []
    index = start
    while index < len(words):
        word = words[index]
        if word == '--':
            operands.extend(range(index + 1, len(words)))
            break
        if not word.startswith('-') or word == '-':
            if not grammar.permute:
                operands.extend(range(index, len(words)))
                break
            operands.append(index)
            index += 1
            continue
        if word.startswith('--'):
            name, equals, value = word[2:].partition('=')
            if name not in grammar.long:
                return None
            key, kind = grammar.long[name]
            if equals and kind == 'f':
                return None
            if kind == 'v' and not equals:
                index += 1
                if index >= len(words):
                    return None
                value = words[index]
            options[key] = value
            index += 1
            continue
        for offset in range(1, len(word)):
            kind = grammar.short.get(word[offset])
            if kind is None:
                return None
            if kind == 'f':
                options[word[offset]] = ''
                continue
            value = word[offset + 1:]
            if kind == 'v' and not value:
                index += 1
                if index >= len(words):
                    return None
                value = words[index]
            options[word[offset]] = value
            break
        index += 1
    return options, operands


# Privilege wrappers end their options at the command they run. sudo(8) (the
# getopt_long table "+Aa:BbC:c:D:Eeg:Hh::iKklnPp:R:r:SsT:t:U:u:Vv" of this
# host's sudo 1.9.9): a word holding `=` that does not start with `/` sets the
# command's environment, before or between options; a lone `-h` takes the next
# word as its host unless that word is an option or such an assignment, and
# shows help otherwise. -a (BSD auth type) and -c (BSD login class) take a
# value; sudo built without them exits with a usage error and runs nothing.
# doas(1): getopt "a:C:Lnsu:" (-a with BSD auth).
_PRIVILEGE_GRAMMARS = {
    'sudo': _runner_grammar('ABbEeHiKklnPSsVv|aCcDgpRrTtUu|h', (
        'askpass:A', 'auth-type:a/v', 'background:b', 'bell:B',
        'close-from:C/v', 'login-class:c/v', 'chdir:D/v', 'preserve-env:E/o',
        'edit:e', 'group:g/v', 'set-home:H', 'help', 'host:/v', 'login:i',
        'remove-timestamp:K', 'reset-timestamp:k', 'list:l',
        'non-interactive:n', 'preserve-groups:P', 'prompt:p/v', 'chroot:R/v',
        'role:r/v', 'stdin:S', 'shell:s', 'type:t/v', 'command-timeout:T/v',
        'other-user:U/v', 'user:u/v', 'version:V', 'validate:v')),
    'doas': _runner_grammar('Lns|aCu'),
}


def _privilege_wrapper_parse(words, *, strict):
    """``(environment, command)`` indices for the sudo/doas at ``words[0]``.

    *command* indexes the command word (``len(words)`` when the wrapper names
    none); *environment* is where the shell-assignment ``VAR=value`` words
    directly before it start (*command* when there are none). Returns None
    when *strict* and an option is not understood or lacks its value;
    otherwise such an option counts as a flag.
    """
    sudo = os.path.basename(words[0]) == 'sudo'
    grammar = _PRIVILEGE_GRAMMARS[os.path.basename(words[0])]

    def assignment(word):
        return sudo and '=' in word and not word.startswith(('/', '-'))

    environment, index = None, 1
    while index < len(words):
        word = words[index]
        if word == '--':
            return index + 1, index + 1
        if word == '-' or not word.startswith('-'):
            if not assignment(word):
                break
            if not _ENV_ASSIGNMENT_GUARD_RE.match(word):
                environment = None  # no shell would read `./x=y` as one
            elif environment is None:
                environment = index
            index += 1
            continue
        environment = None
        index += 1
        if word.startswith('--'):
            name, equals, _value = word[2:].partition('=')
            kind = grammar.long[name][1] if name in grammar.long else None
            if kind is None or (equals and kind == 'f'):
                if strict:
                    return None
            elif kind == 'v' and not equals:
                if index >= len(words):
                    return None if strict else (index, index)
                index += 1
            continue
        for offset in range(1, len(word)):
            kind = grammar.short.get(word[offset])
            if kind is None and strict:
                return None
            if kind in (None, 'f'):
                continue
            if offset + 1 == len(word):  # no value attached
                if kind == 'v':
                    if index >= len(words):
                        return None if strict else (index, index)
                    index += 1
                elif (word == '-h' and index < len(words)
                      and not words[index].startswith('-')
                      and not assignment(words[index])):
                    index += 1  # sudo -h HOST
            break
    if environment is None or index == len(words):
        return index, index
    return environment, index


_SU = _runner_grammar('flmpP|cgGsw|', (
    'command:c/v', 'session-command:c/v', 'fast', 'group:g/v', 'supp-group:G/v',
    'login', 'preserve-environment', 'pty', 'shell:s/v',
    'whitelist-environment:w/v'), permute=True)
_RUNUSER = _runner_grammar('flmpP|cgGswu|', (
    'command:c/v', 'session-command:c/v', 'fast', 'group:g/v', 'supp-group:G/v',
    'login', 'preserve-environment', 'pty', 'shell:s/v',
    'whitelist-environment:w/v', 'user:u/v'), permute=True)
_RUNNER_GRAMMARS = {
    'setsid': _runner_grammar('cfw', ('ctty', 'fork', 'wait')),
    'watch': _runner_grammar('bcCegprtwx|nq|d', (
        'beep', 'color', 'no-color', 'differences:d/o', 'errexit', 'chgexit',
        'interval:n/v', 'precise', 'equexit:q/v', 'no-rerun', 'no-title',
        'no-wrap', 'exec:x')),
    'ssh': _runner_grammar('1246afgknqstvxACGKMNTVXYy|bceilmopBDEFIJLOPQRSwW'),
    'flock': _runner_grammar('sexnoFu|wE', (
        'shared', 'exclusive', 'nonblock', 'nb', 'close', 'no-fork', 'unlock',
        'verbose', 'wait:w/v', 'timeout:w/v', 'conflict-exit-code:E/v')),
    'chroot': _runner_grammar('', ('userspec:u/v', 'groups:g/v', 'skip-chdir')),
    'unshare': _runner_grammar('fmuinpCTUrc|RwSGl', (
        'mount:m/o', 'uts:u/o', 'ipc:i/o', 'net:n/o', 'pid:p/o', 'user:U/o',
        'cgroup:C/o', 'time:T/o', 'fork', 'kill-child:/o', 'mount-proc:/o',
        'mount-binfmt:/o', 'map-user:/v', 'map-users:/v', 'map-group:/v',
        'map-groups:/v', 'map-root-user', 'map-current-user', 'map-auto',
        'map-subids', 'propagation:/v', 'setgroups:/v', 'keep-caps',
        'setuid:S/v', 'setgid:G/v', 'root:R/v', 'wd:w/v', 'monotonic:/v',
        'boottime:/v', 'load-interp:l/v')),
    'nsenter': _runner_grammar('aeFZc|tSGW|muinpCUTrw', (
        'all', 'target:t/v', 'mount:m/o', 'uts:u/o', 'ipc:i/o', 'net:n/o',
        'pid:p/o', 'cgroup:C/o', 'user:U/o', 'time:T/o', 'setuid:S/v',
        'setgid:G/v', 'preserve-credentials', 'root:r/o', 'wd:w/o',
        'wdns:W/v', 'env', 'no-fork', 'follow-context', 'join-cgroup',
        'user-parent', 'keep-caps')),
    'ionice': _runner_grammar('tpPu|cn', (
        'class:c/v', 'classdata:n/v', 'ignore:t', 'pid:p', 'pgid:P', 'uid:u')),
    'chrt': _runner_grammar('abdfiorRmpve|TPD', (
        'all-tasks', 'batch', 'deadline', 'fifo', 'idle', 'other', 'rr',
        'reset-on-fork', 'max:m', 'pid:p', 'verbose', 'sched-runtime:T/v',
        'sched-period:P/v', 'sched-deadline:D/v')),
    'taskset': _runner_grammar('acp', ('all-tasks', 'cpu-list', 'pid:p')),
    'script': _runner_grammar('aefqkr|cEBIOTmo|t', (
        'append', 'command:c/v', 'echo:E/v', 'return', 'flush', 'force',
        'log-io:B/v', 'log-in:I/v', 'log-out:O/v', 'log-timing:T/v',
        'logging-format:m/v', 'output-limit:o/v', 'quiet', 'timing:t/o'),
        permute=True),
    'su': _SU,
    'runuser': _RUNUSER,
}
_DOCKER_GLOBAL = _runner_grammar('D|cHl', (
    'config:/v', 'context:c/v', 'debug:D', 'host:H/v', 'log-level:l/v', 'tls',
    'tlscacert:/v', 'tlscert:/v', 'tlskey:/v', 'tlsverify'))
_PODMAN_GLOBAL = _runner_grammar('r|c', (
    'connection:c/v', 'url:/v', 'identity:/v', 'log-level:/v', 'root:/v',
    'runroot:/v', 'remote:r'))
_CONTAINER_EXEC = _runner_grammar('dilt|euw', (
    'detach', 'detach-keys:/v', 'env:e/v', 'env-file:/v', 'interactive:i',
    'privileged', 'tty:t', 'user:u/v', 'workdir:w/v', 'latest:l',
    'preserve-fds:/v', 'preserve-fd:/v'))
_KUBECTL_EXEC = _runner_grammar('qit|cfn', (
    'container:c/v', 'filename:f/v', 'pod-running-timeout:/v', 'quiet:q',
    'stdin:i', 'tty:t', 'namespace:n/v', 'context:/v', 'kubeconfig:/v'),
    permute=True)
_FIND_EXEC_PRIMARIES = frozenset({'-exec', '-execdir', '-ok', '-okdir'})


def _find_launches(words):
    """Each ``-exec``/``-execdir``/``-ok``/``-okdir`` primary runs the words
    up to ``;`` (or ``{} +``) as a program; the rest of find is data."""
    launches = []
    index = 1
    while index < len(words):
        if words[index] not in _FIND_EXEC_PRIMARIES:
            index += 1
            continue
        start = end = index + 1
        while end < len(words) and words[end] != ';' and not (
                words[end] == '+' and words[end - 1] == '{}'):
            end += 1
        if end > start:
            launches.append(('program', start, end))
        index = end + 1
    return launches


def _container_launches(words, global_grammar):
    """``docker``/``podman`` ``[container] exec [options] CONTAINER COMMAND``."""
    if 'exec' not in words:
        return False
    parsed = _runner_options(words, global_grammar)
    if parsed is None:
        return None
    subcommand = parsed[1][:2]
    if [words[i] for i in subcommand[:1]] == ['exec']:
        start = subcommand[0] + 1
    elif [words[i] for i in subcommand] == ['container', 'exec']:
        start = subcommand[1] + 1
    else:
        return False
    parsed = _runner_options(words, _CONTAINER_EXEC, start)
    if parsed is None:
        return None
    options, operands = parsed
    command = operands[0 if 'l' in options or 'latest' in options else 1:]
    return [('program', command[0], len(words))] if command else []


def _kubectl_launches(words):
    """``kubectl … exec POD [options] [--] COMMAND``: the documented form puts
    the command after ``--``; the deprecated one after the pod operand."""
    dash = words.index('--') if '--' in words else len(words)
    if 'exec' not in words[:dash]:
        return False
    if dash < len(words):
        return [('program', dash + 1, len(words))] if dash + 1 < len(words) else []
    parsed = _runner_options(words, _KUBECTL_EXEC, words.index('exec') + 1)
    if parsed is None:
        return None
    command = parsed[1][1:]
    return [('program', command[0], len(words))] if command else []


def _ssh_launches(words):
    """``ssh [options] destination [options] [command …]``: OpenSSH parses
    options again after the destination, and the remote shell runs the
    command words joined by spaces; without one it reads stdin as a script."""
    grammar = _RUNNER_GRAMMARS['ssh']
    parsed = _runner_options(words, grammar)
    if parsed is None or not parsed[1]:
        return None if parsed is None else []
    destination = parsed[1][0]
    parsed = _runner_options(words, grammar, destination + 1)
    if parsed is None:
        return None
    command = parsed[1]
    if not command:
        return [('stdin',)]
    from lib.project_mod.command_heredoc import shell_input_source
    if shell_input_source([words[i] for i in command]) == ('stdin', None):
        return [('stdin',)]  # the remote command is a shell reading stdin
    return [('script', ' '.join(words[i] for i in command), command)]


def _runner_launches(head, words):
    """What the runner at ``words[0]`` executes, as a list of
    ``('program', start, end)`` (an argv slice), ``('script', text, indices)``
    (shell source) and ``('stdin',)`` (a shell reading its stdin). Returns
    False when this invocation runs nothing as a command (``docker ps``),
    [] when it runs no command, None when its options are not understood."""
    if head == 'find':
        return _find_launches(words)
    if head == 'ssh':
        return _ssh_launches(words)
    if head in ('docker', 'podman'):
        return _container_launches(
            words, _DOCKER_GLOBAL if head == 'docker' else _PODMAN_GLOBAL)
    if head == 'kubectl':
        return _kubectl_launches(words)
    parsed = _runner_options(words, _RUNNER_GRAMMARS[head])
    if parsed is None:
        return None
    options, operands = parsed
    if head in ('su', 'runuser', 'script') and 'c' in options:
        launches = [('script', options['c'], ())]
        return launches + ([('program', operands[1], len(words))]
                           if head == 'script' and len(operands) > 1 else [])
    if head in ('su', 'runuser'):
        if head == 'runuser' and 'u' in options and operands:
            return [('program', operands[0], len(words))]
        return [('stdin',)]
    if head == 'watch' and 'x' not in options:
        return [('script', ' '.join(words[i] for i in operands), operands)] if operands else []
    if head == 'flock' and len(operands) > 2 and words[operands[1]] in ('-c', '--command'):
        return [('script', words[operands[2]], operands[2:3])]
    if head in ('ionice', 'chrt', 'taskset') and options.keys() & {'p', 'P', 'u', 'm'}:
        return []  # the operands name existing processes
    skip = {'flock': 1, 'chroot': 1, 'taskset': 1, 'script': 1}.get(head, 0)
    if head == 'chrt' and operands and words[operands[0]].isdigit():
        skip = 1
    command = operands[skip:]
    return [('program', command[0], len(words))] if command else []


_RUNNERS = frozenset(_RUNNER_GRAMMARS) | {'find', 'ssh', 'docker', 'podman', 'kubectl'}
# A shell reading its stdin as a script, for runners whose command is a shell.
_STDIN_SHELL_VIEW = (['sh'], ['sh'])


def _script_view(text, raw_words):
    """Program view of a shell running *text* (``sh -c TEXT``).

    The raw word decides whose bindings the text sees: one word quoted or
    expanded by this shell keeps its quoting, and joined words become a
    single-quoted literal (the child shell parses them)."""
    raw = raw_words[0] if len(raw_words) == 1 else shlex.quote(text)
    if _shell_string_script(raw) is None:
        raw = shlex.quote(text)
    return ['sh', '-c', text], ['sh', '-c', raw]


def _program_views(words, depth=0):
    """(literal, raw) views of every program a simple command runs.

    Reserved words, assignment prefixes (``FOO=1 cmd``), a ``function name``
    header, ``sudo``/``doas``, ``env``, ``builtin``, the process wrappers of
    :func:`process_wrapper_operand` and ``xargs`` are seen through, and each
    runner of :func:`_runner_launches` contributes what it executes: an argv
    view, or ``sh -c TEXT`` for the shell text it runs (``su -c``, ``ssh``,
    ``watch``, ``flock -c``, ``script -c``). sudo/doas options follow
    :data:`_PRIVILEGE_GRAMMARS`; naming no command, they are a shell reading
    stdin (``sudo -s``/``-i``, ``doas -s``). Returns [] when the command runs
    no program (``command -v x``) and ``_UNKNOWN_PROGRAM`` when a wrapper's
    or runner's options are not understood (``env -S …``, ``timeout $T …``,
    ``sudo --bogus …``).
    """
    raw = list(words)
    for _ in range(_MAX_WRAPPER_DEPTH):
        index = 0
        while index < len(raw) and (raw[index] in _PREFIX_KEYWORDS
                                    or _ENV_ASSIGNMENT_GUARD_RE.match(raw[index])):
            index += 1
        raw = raw[index:]
        if not raw:
            return []
        if raw[0] == 'function':
            raw = raw[3:] if raw[2:3] == ['()'] else raw[2:]
            continue
        literal = [_literal_command_word(word) for word in raw]
        head = os.path.basename(literal[0])
        if head in _PRIV_WRAPPERS:
            parsed = _privilege_wrapper_parse(literal, strict=True)
            operand = None if parsed is None else parsed[1]
            if operand == len(raw):
                return [_STDIN_SHELL_VIEW]
        elif head == 'env':
            operand = _env_operand(literal, 0)
        elif head == 'builtin':
            operand = 1
        elif head == 'xargs':
            operand = _xargs_operand(literal)
        elif head in _PROCESS_WRAPPERS:
            operand = process_wrapper_operand(literal)
            if operand is None and head == 'command':
                return []  # `command -v/-V` describes the program, runs nothing
        elif head in _RUNNERS:
            return _runner_views(head, literal, raw, depth)
        else:
            return [(literal, raw)]
        if operand is None:
            return _UNKNOWN_PROGRAM
        raw = raw[operand:]
    return _UNKNOWN_PROGRAM


def _runner_views(head, literal, raw, depth):
    launches = _runner_launches(head, literal)
    if launches is False:
        return [(literal, raw)]
    if launches is None or depth >= _MAX_WRAPPER_DEPTH:
        return _UNKNOWN_PROGRAM
    views = []
    for launch in launches:
        if launch[0] == 'stdin':
            views.append(_STDIN_SHELL_VIEW)
        elif launch[0] == 'script':
            views.append(_script_view(launch[1], [raw[i] for i in launch[2]]))
        else:
            inner = _program_views(raw[launch[1]:launch[2]], depth + 1)
            if inner is _UNKNOWN_PROGRAM:
                return inner
            views.extend(inner)
    return views


def _dangerous_program_rule(literal, raw):
    """Rule label for one program run (wrappers removed), else None."""
    names = {_WINDOWS_PROGRAM_SUFFIX.sub('', name) for name in (
        os.path.basename(literal[0]), re.split(r'[\\/]', raw[0])[-1].strip('\'"'))}
    folded = {name.lower() for name in names}
    operands = literal[1:]
    for name in ('shutdown', 'reboot'):
        if name in names:
            return name
    if any(name == 'mkfs' or name.startswith('mkfs.') for name in names):
        return 'mkfs'
    if 'diskpart' in folded:
        return 'diskpart'
    if 'init' in names and '0' in operands:
        return 'init 0'
    if 'dd' in names and any(operand.startswith('of=/') for operand in operands):
        return 'dd of=/'
    if 'format' in folded and any(_DRIVE_OPERAND.match(operand) for operand in operands):
        return 'format X:'
    switches = {operand.lower() for operand in operands}
    for name in ('rd', 'del'):
        if name in folded and {'/s', '/q'} <= switches:
            return f'{name} /s /q'
    if ('chmod' in names
            and any(operand == '--recursive' or _RECURSIVE_SHORT_OPTION.match(operand)
                    for operand in operands)
            and any(operand.startswith('/') and not operand.strip('/.*')
                    for operand in operands)):
        return 'chmod -R /'
    if 'systemctl' in names and 'reboot' in operands:
        return 'reboot'
    return None


def _dangerous_redirect_rule(redirects):
    for operator, target in redirects:
        if '>' in operator and _literal_command_word(target).startswith('/dev/sd'):
            return '> /dev/sd'
    return None


def _function_name(words):
    """Name a ``name()`` or ``function name`` header defines, else None."""
    index = 0
    while index < len(words) and words[index] in _PREFIX_KEYWORDS:
        index += 1
    words = words[index:]
    if len(words) >= 2 and words[0] == 'function':
        return words[1]
    if len(words) == 2 and words[1] == '()':
        return words[0]
    return None


def _stage_word(words):
    """First command word of a pipeline stage, after reserved words and a
    ``function name [()]`` header."""
    words = list(words)
    if words[:1] == ['function']:
        words = words[3:] if words[2:3] == ['()'] else words[2:]
    index = 0
    while index < len(words) and words[index] in _PREFIX_KEYWORDS:
        index += 1
    return words[index] if index < len(words) else None


def _pipes_function_into_itself(items, functions):
    """A fork bomb: a defined function piped into itself (``:(){ :|:& };:``)."""
    for left, right in zip(items, items[1:]):
        if (isinstance(left, SimpleCommand) and isinstance(right, SimpleCommand)
                and right.separator == '|'):
            word = _stage_word(left.words)
            if word in functions and word == _stage_word(right.words):
                return True
    return False


def _raw_dangerous_rule(text):
    """Fail-closed match for text whose command structure is unknown."""
    match = _DANGEROUS_RE.search(text)
    return match.group(0).strip() if match else None


def _dangerous_rule_in_text(text, functions=frozenset()):
    try:
        items = parse_shell_structure(text)
    except ShellStructureUnknown:
        return _raw_dangerous_rule(text)
    return _dangerous_rule_in_script(items, functions)


def _dangerous_rule_in_script(items, functions):
    """Rule label of the first dangerous command a parsed script may run.

    *functions* are the function names visible to the script; its groups and
    substitution bodies also see the ones it defines.
    """
    functions = functions | {
        name for name in (_function_name(item.words) for item in items
                          if isinstance(item, SimpleCommand)) if name}
    if _pipes_function_into_itself(items, functions):
        return 'fork bomb'
    for index, item in enumerate(items):
        scripts = list(item.nested)
        for heredoc in item.heredocs:
            scripts.extend(heredoc.nested)
        if isinstance(item, Subshell):
            scripts.append(item.body)
        for script in scripts:
            rule = _dangerous_rule_in_script(script, functions)
            if rule:
                return rule
        rule = _dangerous_redirect_rule(item.redirects)
        if rule:
            return rule
        if isinstance(item, SimpleCommand):
            rule = _dangerous_simple_command_rule(items, index, functions)
            if rule:
                return rule
    return None


def _dangerous_simple_command_rule(items, index, functions):
    """Judge one simple command and the text it hands a shell to run."""
    views = _program_views(items[index].words)
    if views is _UNKNOWN_PROGRAM:
        # The hidden program may be a shell reading this stdin.
        for text in [' '.join(items[index].words)] + _stdin_texts(items[index]):
            rule = _raw_dangerous_rule(text)
            if rule:
                return rule
        views = []
    for view in views:
        name = re.split(r'[\\/]', view[1][0])[-1]
        if '$(' in name or '`' in name:
            # A substitution computes the program name: judge the text naming it.
            rule = _raw_dangerous_rule(name)
            if rule:
                return rule
        rule = _dangerous_program_rule(*view)
        if rule:
            return rule
        literal = view[0]
        if os.path.basename(literal[0]) == 'eval' and len(literal) > 1:
            # eval runs its operands, joined by spaces, in this same shell.
            rule = _dangerous_rule_in_text(' '.join(literal[1:]), functions)
            if rule:
                return rule
        action = (_trap_action(view[1]) if os.path.basename(literal[0]) == 'trap'
                  else None)
        if action is not None:
            # A trap action runs later in this same shell.
            script = _shell_string_script(action)
            rule = (_raw_dangerous_rule(action) if script is None
                    else _dangerous_rule_in_text(script[0], functions))
            if rule:
                return rule
        if os.path.basename(literal[0]) == 'alias':
            # A later command naming the alias runs its text in this shell.
            for word in literal[1:]:
                _name, separator, value = word.partition('=')
                rule = separator and _dangerous_rule_in_text(value, functions)
                if rule:
                    return rule
    for source in _shell_source_scripts(items, index, views):
        # A child shell runs the script and sees none of this shell's functions.
        rule = (_raw_dangerous_rule(source.raw) if source.script is None
                else _dangerous_rule_in_text(source.script[0]))
        if rule:
            return rule
    return None


def dangerous_command_rule(command):
    """Label of the dangerous-command rule *command* trips, else None.

    The one judge behind run_command, the desktop agent and the run_command
    safety pre-hook; each keeps its own refusal text. Refused, wherever a
    shell would run them: ``shutdown``/``reboot`` (``systemctl reboot``
    included), ``mkfs``/``mkfs.*``, ``diskpart``, ``init 0``, ``dd`` with an
    ``of=/…`` operand, a write redirect to ``/dev/sd*``, ``format X:``,
    ``rd``/``del /s /q``, a recursive ``chmod`` of ``/`` and a function piped
    into itself (fork bomb).
    """
    if not command or not command.strip():
        return None
    return _dangerous_rule_in_text(command)


def _is_dangerous_command(command):
    """True when *command* trips a rule of :func:`dangerous_command_rule`."""
    return dangerous_command_rule(command) is not None


# ── Unbounded recursive-scan guard ( B2) ─────────────────
# Measured incident (2026-07-31, task 96c56840): the model ended a long
# verification script with ``grep -rn "mcp>=1.0.0" ../ --include=pyproject.toml``
# — a recursive scan of the workspace PARENT, a FUSE mount holding 8GB+ of
# dumps, a PG data dir, 7392 swarm dirs and every sibling repo. The pipeline
# ran 2.5h with zero output; the task wedged; no timeout existed to end it.
#
# The rule (owner ruling 2026-07-31): a recursive scanner whose target is
#   (a) an ANCESTOR of the workspace cwd (scanning it includes the whole
#       workspace + its data trees), or
#   (b) a shallow FUSE-mount path (≤3 components under /mnt),
# is refused when the run_command call carries NO explicit timeout and the
# scan segment is not itself wrapped in coreutils ``timeout``. In-workspace
# scans (``.``, subdirs), sibling dirs (``../other-repo`` — not an ancestor,
# not a mount root), deep specific paths, and bounded scans all stay legal.

# grep-family flags that consume the NEXT token as their argument (so the
# pattern/target positionals are not miscounted).
_GREP_ARG_FLAGS = frozenset({
    '-e', '-f', '-m', '-A', '-B', '-C',
    '--max-count', '--after-context', '--before-context', '--context',
    '--include', '--exclude', '--exclude-dir', '--binary-files',
    '--directories', '--label', '--threads', '--regexp', '--file',
})

# Scanners that recurse only behind a flag.
_FLAGGED_RECURSIVE = frozenset({'grep', 'egrep', 'fgrep'})
# Scanners recursive-by-nature over a target dir (locate excluded: it reads a
# prebuilt index, it does not crawl).
_INHERENT_RECURSIVE = frozenset({
    'find', 'rg', 'ag', 'ack', 'fd', 'fdfind', 'tree', 'du', 'ncdu', 'cloc',
})

# Commands that remain useful in run_command pipelines but can wedge forever
# on a hostile FUSE/NFS traversal. grep has its stronger transparent engine;
# direct find segments get a native streaming timeout wrapper.
_BOUNDED_SCAN_BINARIES = frozenset({'find'})
_SCAN_PREFIX_KEYWORDS = frozenset({'if', 'while', 'until', '!', 'time'})
_SCAN_WRAPPERS = frozenset({'command', 'builtin', 'exec'})
_ENV_ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

_TIMEOUT_WRAPPER_RE = re.compile(r'^timeout\b')


def _strip_quotes(tok):
    if len(tok) >= 2 and tok[0] in '"\'' and tok[-1] == tok[0]:
        return tok[1:-1]
    return tok


def _scan_targets_for_segment(parts, recursive_flagged):
    """Extract scan-target candidates from one command segment's tokens.

    Returns (is_scanner, targets): targets are the positional args that may
    be scan roots (pattern positional already dropped for grep-family when
    no -e/-f is present). Best-effort parsing — a missed exotic shape fails
    OPEN (allowed), the guard only refuses shapes it can prove.
    """
    positionals = []
    saw_pattern_flag = False
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok.startswith('--'):
            name = tok.split('=', 1)[0]
            if name in _GREP_ARG_FLAGS and '=' not in tok:
                if name in ('--regexp', '--file'):
                    saw_pattern_flag = True
                i += 2
                continue
            i += 1
            continue
        if tok.startswith('-') and tok != '-':
            if tok in _GREP_ARG_FLAGS:
                if tok in ('-e', '-f'):
                    saw_pattern_flag = True
                i += 2
                continue
            i += 1
            continue
        positionals.append(tok)
        i += 1
    if recursive_flagged and not saw_pattern_flag and positionals:
        # First positional is the PATTERN, not a target.
        positionals = positionals[1:]
    return True, positionals


def _scan_binary_word_index(words):
    """Return the command-word index after benign shell prefixes."""
    i = 0
    while i < len(words):
        word = words[i]
        if (not word.squote and not word.dquote
                and (_ENV_ASSIGNMENT_RE.match(word.text)
                     or word.text in _SCAN_PREFIX_KEYWORDS)):
            i += 1
            continue
        break
    if i < len(words) and words[i].text.split('/')[-1] in _SCAN_WRAPPERS:
        i += 1
        while i < len(words) and words[i].text.startswith('-'):
            i += 1
    return i


def _bound_scan_segments(command, timeout_binary, seconds, timeout_args=None):
    """Wrap direct recursive scanner segments with a native timeout.

    The rewrite is span-based and leaves the rest of the shell program
    untouched, so pipelines remain streaming and downstream backpressure
    still works. Only command text is wrapped (:func:`command_rewrite_text`);
    heredoc bodies, comments and quoted strings stay byte-identical. Returns
    ``(rewritten, count)``. Unknown/wrapped shapes fail open; the
    ancestor/FUSE-root refusal remains the outer safety boundary.
    """
    try:
        seconds = max(1, int(seconds))
    except (TypeError, ValueError) as exc:
        logger.debug('[CommandAnalysis] invalid scanner timeout %r: %s',
                     seconds, exc)
        return command, 0
    if not command or not timeout_binary:
        return command, 0
    if timeout_args is None:
        timeout_args = ('--signal=TERM', '--kill-after=2s')
    quoted_prefix = ' '.join([
        shlex.quote(str(timeout_binary)),
        *(shlex.quote(str(arg)) for arg in timeout_args),
        shlex.quote(f'{seconds}s'),
    ])
    text = command_rewrite_text(command)
    if text is None:
        return command, 0
    view = text.view
    insertions = []
    for start, end in _split_pipeline_spans(view):
        words = _shell_words(view[start:end])
        index = _scan_binary_word_index(words)
        if index >= len(words):
            continue
        base = words[index].text.split('/')[-1]
        if base not in _BOUNDED_SCAN_BINARIES:
            continue
        insertion_at = start + words[index].start
        timeout_prefix = f'{quoted_prefix} '
        insertions.append((insertion_at, timeout_prefix))
    rewritten = command
    for at, text in sorted(insertions, reverse=True):
        rewritten = rewritten[:at] + text + rewritten[at:]
    return rewritten, len(insertions)


def _unbounded_recursive_scan_target(command, cwd=None):
    """Return the offending target when *command* is an unbounded recursive
    scan of a workspace ancestor or a FUSE-mount root — else None.

    "Unbounded" is judged by the CALLER (this function only answers "is this
    target pathological"); a scan wrapped in coreutils ``timeout`` is
    reported safe here because the segment is self-bounding. Only command
    text is judged (:func:`command_rewrite_text`), so a scan written into a
    heredoc body or a comment is data; a command whose structure cannot be
    delimited is judged as written.
    """
    if not command or not command.strip():
        return None
    cwd_real = os.path.realpath(cwd) if cwd else None
    text = command_rewrite_text(command)
    for seg in _split_pipeline(command if text is None else text.view):
        seg = seg.strip()
        if not seg:
            continue
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        parts = _unwrap_command_parts([w.text for w in _shell_words(seg)])
        if not parts:
            continue
        base_cmd = parts[0].split('/')[-1]
        if _TIMEOUT_WRAPPER_RE.match(base_cmd):
            # coreutils timeout wraps this segment's scan — self-bounding.
            continue
        if base_cmd in _FLAGGED_RECURSIVE:
            # Recursive only behind -r/-R/--recursive (clusters included).
            flags = [t for t in parts[1:] if t.startswith('-') and t != '-']
            recursive = any(
                f == '--recursive'
                or (not f.startswith('--') and ('r' in f[1:] or 'R' in f[1:]))
                for f in flags)
            if not recursive:
                continue
            _, targets = _scan_targets_for_segment(parts, recursive_flagged=True)
        elif base_cmd in _INHERENT_RECURSIVE:
            _, targets = _scan_targets_for_segment(parts, recursive_flagged=False)
        else:
            continue
        if not targets:
            # No target → the tool scans its cwd (inside the workspace).
            continue
        for raw in targets:
            tok = _strip_quotes(raw)
            if not tok or tok == '-':
                continue
            if not (tok.startswith('/') or tok.startswith('..')
                    or tok.startswith('~') or tok.startswith('.')
                    or '/' in tok):
                # A bare word ('src') — relative, inside the workspace.
                continue
            expanded = os.path.expanduser(os.path.expandvars(tok))
            if not expanded:
                continue
            if not expanded.startswith('/'):
                if not cwd_real:
                    continue
                resolved = os.path.realpath(os.path.join(cwd_real, expanded))
            else:
                resolved = os.path.realpath(expanded)
            if cwd_real:
                if resolved == cwd_real or resolved.startswith(cwd_real + os.sep):
                    continue  # inside the workspace — the normal case
                if (cwd_real + os.sep).startswith(resolved + os.sep) \
                        or cwd_real == resolved:
                    return raw  # ancestor of the workspace — the incident shape
            depth = len([c for c in resolved.split('/') if c])
            if (resolved == '/mnt' or resolved.startswith('/mnt/')) \
                    and depth <= 3:
                return raw  # FUSE mount root zone
    return None


# ── Filesystem-grep redirect guard (2026-08-04) ─────────────────────
# The run_command tool description has advised search_files-over-grep for
# months, yet grep-via-run_command kept recurring — measured 2026-08-04:
# ``grep -n 'cream\|…' static/styles.css | head -30; echo ---; grep -rn
# 'cream' static/*.css 2>/dev/null | head -20`` sat RUNNING for 17m04s in a
# FUSE bad window with zero output and no timeout to end it. Advice is not
# enforcement; this guard is the enforcement.
#
# The rule: a grep-family segment (grep / egrep / fgrep, sudo seen through)
# that reads the FILESYSTEM — explicit file/dir operands, or a recursive
# flag (an implicit walk of the cwd) — is refused with a message that
# translates the call to search_files. A grep that filters a STREAM (no
# operands, stdin fed by a pipe: ``pytest 2>&1 | grep PASS``, ``ps aux |
# grep python``) stays legal — search_files cannot replace it. ``rg`` and
# ``git grep`` are already the fast path and stay legal; a coreutils
# ``timeout`` wrapper is not unwrapped (bounded scans stay legal, matching
# the scan guard's philosophy above). Subshells, command substitution and
# multi-line scripts are seen THROUGH (the splitter treats bare parens and
# newlines as command boundaries). Best-effort: exotic shapes (heredocs,
# xargs) fail OPEN, consistent with every other guard in this module.
# Kill switch: TOFU_RUN_GREP_GUARD=0.

_GREP_REDIRECT_BINARIES = frozenset({'grep', 'egrep', 'fgrep'})

# Short grep flags that consume an argument (rest-of-cluster, or the next
# token when last in the cluster): -e -f -m -A -B -C -d -D.
_GREP_SHORT_ARG_CHARS = frozenset('efmABCdD')

# Shell redirections: fused (``2>/dev/null``, ``>out``, ``2>&1``, ``<in``)
# vs bare operator whose target is the NEXT token (``> out``, ``2> err``).
_REDIR_FUSED_RE = re.compile(r'^\d*(?:>>?|<|&>|>&)\S')
_REDIR_BARE_RE = re.compile(r'^\d*(?:>>?|<|&>|>&)$')


def _strip_redirection_tokens(tokens):
    """Drop shell redirection tokens (operator + its target) from *tokens*.

    Without this, ``ps aux | grep -v x 2>/dev/null`` would look like it has
    a file operand (``2>/dev/null``) and be misrefused — it is a pure stream
    filter and must stay legal.
    """
    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _REDIR_BARE_RE.match(tok):
            i += 2  # bare operator consumes the next token as its target
            continue
        if _REDIR_FUSED_RE.match(tok):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _grep_segment_reads_filesystem(parts):
    """True when a grep-family segment reads files: explicit file/dir
    operands, or a recursive walk (``-r``/``-R``, target optional — grep
    then scans the cwd). *parts* may contain strings or :class:`_ShellWord`
    values; argv boundaries are authoritative. A grep with only a pattern
    (stdin filter) returns False.
    """
    parts = [part.text if isinstance(part, _ShellWord) else part
             for part in parts]
    positionals = []
    saw_pattern_flag = False
    recursive = False
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok == '--':
            positionals.extend(parts[i + 1:])
            break
        if tok.startswith('--'):
            name = tok.split('=', 1)[0]
            if name in ('--recursive', '--dereference-recursive'):
                recursive = True
            elif name in ('--regexp', '--file'):
                saw_pattern_flag = True
            if name in _GREP_ARG_FLAGS and '=' not in tok:
                i += 2      # long flag consumes the next token as its arg
                continue
            i += 1
            continue
        if tok.startswith('-') and tok != '-':
            cluster = tok[1:]
            takes_next = False
            for j, ch in enumerate(cluster):
                if ch in 'rR':
                    recursive = True
                elif ch in _GREP_SHORT_ARG_CHARS:
                    if ch in 'ef':
                        saw_pattern_flag = True
                    # Rest of the cluster is this flag's argument; when the
                    # flag is the last char, the NEXT token is the argument.
                    takes_next = (j == len(cluster) - 1)
                    break
            i += 2 if takes_next else 1
            continue
        positionals.append(tok)
        i += 1
    if recursive:
        return True
    if not saw_pattern_flag and positionals:
        positionals = positionals[1:]   # first positional is the PATTERN
    return any(t != '-' for t in positionals)   # '-' alone means stdin


def _grep_filesystem_segment(command):
    """Return the offending segment when *command* aims a grep-family binary
    at the FILESYSTEM (file/dir operands or a recursive walk) — else None.

    See the block comment above for the rule. Stream filters, ``rg`` /
    ``git grep``, ``timeout``-wrapped shapes and text outside command text
    (heredoc bodies, comments: :func:`command_rewrite_text`) all return None;
    a command whose structure cannot be delimited is judged as written.
    """
    if not command or not command.strip():
        return None
    text = command_rewrite_text(command)
    for seg in _split_pipeline(command if text is None else text.view):
        seg = seg.strip()
        if not seg:
            continue
        while _re.match(r'^\w+=\S*\s', seg):
            seg = _re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        parts = _unwrap_command_parts([w.text for w in _shell_words(seg)])
        if not parts:
            continue
        if any(t.startswith('<<') for t in parts):
            # Heredoc / herestring — operand-looking tokens are DATA, not
            # paths; fail open rather than misparse a stream filter.
            continue
        if parts[0].split('/')[-1] not in _GREP_REDIRECT_BINARIES:
            continue
        stripped = [parts[0]] + _strip_redirection_tokens(parts[1:])
        if _grep_segment_reads_filesystem(stripped):
            return seg
    return None


# ── grep-hardening shell-metachar detection ─────────────────────────
# ── grep-hardening shell-metachar detection ─────────────────────────
def _has_unquoted_shell_metachars(cmd):
    in_single = False
    in_double = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == '\\' and not in_single and i + 1 < len(cmd):
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in '|;&<>`':
                return True
            if ch == '$' and i + 1 < len(cmd) and cmd[i + 1] == '(':
                return True
        i += 1
    return False


# ── Git workspace guard classification ─────────────────────────────
# Contract: docs/runbooks/GIT_WORKSPACE_GUARD.md.
# Pure risk classification of a shell command for the shared-checkout
# Git guard. Returns one of:
#   'readonly'        — every segment is provably read-only
#   'safe_write'      — ordinary writes (including preflighted atomic apply)
#                       that need writer exclusion, not a presence veto
#   'tree_destroying' — a Git segment that may rewrite working-tree bytes or
#                       move HEAD under other conversations
#   None              — no Git segment, an unknown Git sub-command, an
#                       unknown global flag, or an opaque non-Git segment;
#                       callers must fall back to the existing attended gate.
# This module stays PURE: verdicts only. The nonce / presence / refusal
# policy lives in lib/project_mod/git_guard.py.

# Ordinary additive writes; destructive ref/history options are checked first.
# This class is not writer exclusion and never bypasses readonly authorization.
_GIT_GUARD_SAFE_WRITE = frozenset({
    'add', 'commit', 'fetch', 'push',
})

# Short-flag bundles that imply a FORCED branch operation: -D is exactly -df
# and -M is -mf, so a bundled 'f' is equally forceful. For ``tag`` the
# destructive short flags stay d/D/c/C/f (tag has no lowercase force aliases).
_GIT_GUARD_FORCE_SHORT_FLAGS = {
    'branch': 'dDf',
    'tag': 'dDcCf',
}

# Sub-commands that may rewrite working-tree bytes or move HEAD. ``stash``
# is handled separately because list/show are read-only.
_GIT_GUARD_TREE_DESTROYING = frozenset({
    'checkout', 'switch', 'reset', 'restore', 'clean', 'rm', 'mv',
    'rebase', 'merge', 'cherry-pick', 'revert', 'am', 'pull',
})

# git global options (BEFORE the sub-command) that consume the NEXT token
# as their value: git -C <path> -c k=v --git-dir <p> ...
_GIT_GUARD_GLOBAL_VALUE_OPTS = frozenset({
    '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env',
})

# Bare global flags that are safe to skip while hunting the sub-command.
_GIT_GUARD_GLOBAL_BARE_FLAGS = frozenset({
    '-p', '-P', '--paginate', '--no-pager', '--bare', '--no-replace-objects',
    '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
    '--icase-pathspecs', '--no-optional-locks', '--version', '--help',
})

_ENV_ASSIGNMENT_GUARD_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def git_command_words(words):
    """Bounded Git wrapper projection; no env option/command interpretation."""
    words = list(words)
    for _ in range(_MAX_WRAPPER_DEPTH):
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words.pop(0)
        words = unwrap_process_wrappers(words)
        if not words or os.path.basename(words[0]) != 'env':
            break
        words = words[1:]
        if words[:1] == ['--']:
            words = words[1:]
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words.pop(0)
        if not words or words[0].startswith('-'):
            return []  # -S/-u/-i and unknown forms never gain Git receipts.
    return words


def git_invocation_parts(args):
    """Stable public syntax-only pair; authority belongs to details parsing."""
    details = _git_invocation_details(args)
    return None if details is None else (details[0], details[1])


def _git_invocation_details(args):
    """Parse argv into (subcommand, rest, unproved_config, autostash) or None.

    One pure owner for validated global-option consumption. Unknown options,
    missing values and invalid config assignments return None; attached -C/-c
    and known long equals forms are supported. Values are argv data, including
    option-looking paths. The returned rest is a fresh list; input is unchanged.
    An empty subcommand denotes bare Git/global help or an exec-path query.
    This is syntax evidence, not shell-expansion or configured-helper authority.
    """
    unproved = False
    autostash = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if not isinstance(token, str) or '\x00' in token:
            return None
        if token in _GIT_GUARD_GLOBAL_BARE_FLAGS:
            unproved |= token in {'--paginate', '-p'}
            continue
        if token == '--exec-path':
            return '', [], unproved, autostash  # Query exits without invoking a verb.
        option, value = token, None
        if token in _GIT_GUARD_GLOBAL_VALUE_OPTS:
            if index >= len(args):
                return None
            value = args[index]
            index += 1
        elif token.startswith(('-C', '-c')) and len(token) > 2:
            option, value = token[:2], token[2:]
        elif token.startswith('--') and '=' in token:
            option, value = token.split('=', 1)
            if option not in _GIT_GUARD_GLOBAL_VALUE_OPTS | {'--exec-path'}:
                return None
        elif token.startswith('-') or not token:
            return None
        else:
            return token, list(args[index:]), unproved, autostash
        if not isinstance(value, str) or '\x00' in value:
            return None
        if option in ('-c', '--config-env'):
            key, separator, setting = value.partition('=')
            if not re.fullmatch(
                    r'[A-Za-z][A-Za-z0-9-]*(?:\.[^=\s]+)*\.[A-Za-z][A-Za-z0-9-]*', key):
                return None
            if option == '--config-env' and (not separator or not setting):
                return None

            # Only bounded presentation or disabling settings prove absence of helpers.
            harmless = (option == '-c' and separator and (
                (key.lower() in {'color.ui', 'color.diff', 'color.status', 'color.branch'}
                 and setting.lower() in {'false', 'true', 'never', 'auto', 'always'})
                or value in _HARMLESS_GIT_CONFIG))
            unproved |= not harmless
            autostash |= (option == '-c' and key.lower() == 'merge.autostash'
                          and setting.lower() in {'true', 'yes', 'on', '1', ''})
        elif option != '-C' and not value:
            return None
        if option == '--exec-path':
            unproved = True
    return '', [], unproved, autostash


def _classify_checkout_like(sub, args):
    """Classify ``git checkout|switch`` for the workspace guard.

    Branch creation (``checkout -b name`` / ``switch -c name``) moves HEAD to
    a new ref pointing at the SAME commit and rewrites no working-tree byte
    → ``safe_write``. Anything else — switching refs, ``--detach``, path
    checkouts (``checkout -- f``), or a start-point after the new branch name
    (``checkout -b new main~2``, which rewrites the tree to the start point)
    → ``tree_destroying``.
    """
    # Accept only the narrow additive form. Force/reset/orphan and unknown
    # options can move existing refs or discard index/worktree state.
    create_flags = ({'-b'} if sub == 'checkout' else {'-c', '--create'})
    if len(args) == 2 and args[0] in create_flags and not args[1].startswith('-'):
        return 'safe_write'
    if sub == 'switch' and len(args) == 1 and args[0].startswith('--create='):
        return 'safe_write' if args[0][9:] else 'tree_destroying'
    return 'tree_destroying'


def _commit_rewrites_history(args):
    """Find amend options, not option values or pathspecs.

    Git accepts unambiguous long-option abbreviations. Conservatively flag
    amend prefixes rather than granting an ordinary-commit receipt to them.
    """
    value_options = {
        '-m', '--message', '-F', '--file', '-C', '--reuse-message',
        '-c', '--reedit-message', '--author', '--date', '--cleanup',
        '--fixup', '--squash', '--pathspec-from-file', '--trailer',
    }
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break
        if token in value_options:
            index += 1
            continue
        option = token.split('=', 1)[0]
        if option.startswith('--') and len(option) > 2 and '--amend'.startswith(option):
            return True
    return False


GitApplyEvidence = namedtuple('GitApplyEvidence', 'risky_options unknown_options report_only')


def git_apply_option_evidence(args):
    """Pure apply option evidence from the SAME parser used for risk.

    Tuple fields contain option spellings, never consumed option values;
    ``--`` ends options, so later words are pathspecs, not flags.
    """
    report_flags = {'--check', '--stat', '--numstat', '--summary'}
    ordinary_flags = {
        '--apply', '--index', '--cached', '--intent-to-add', '-N',
        '--reverse', '-R', '--no-add', '--binary', '--unidiff-zero',
        '--recount', '--inaccurate-eof', '--ignore-space-change',
        '--ignore-whitespace', '--allow-empty', '--verbose', '-v',
        '--quiet', '-q', '-z',
    }
    risky_flags = {'--3way', '-3', '--reject', '--unsafe-paths', '--allow-overlap'}
    value_flags = {'--exclude', '--include', '--directory', '--whitespace', '-p', '-C'}
    report = apply = False
    risky, unknown = [], []
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break
        if token in report_flags:
            report = True
        elif token in ordinary_flags:
            apply = apply or token == '--apply'
        elif token in risky_flags:
            risky.append(token)
        elif token in value_flags:
            if index >= len(args):
                unknown.append(token)
                continue
            value = args[index]
            index += 1
            if token in ('-p', '-C') and not value.isdigit():
                unknown.append(token)
            elif token == '--whitespace' and value not in (
                    'nowarn', 'warn', 'fix', 'error', 'error-all'):
                unknown.append(token)
        elif any(token.startswith(flag + '=') for flag in value_flags
                 if flag.startswith('--')):
            flag, value = token.split('=', 1)
            if flag == '--whitespace' and value not in (
                    'nowarn', 'warn', 'fix', 'error', 'error-all'):
                unknown.append(flag)
        elif token.startswith(('-p', '-C')) and token[2:].isdigit():
            continue
        elif token.startswith('-') and token != '-':
            # Includes long-option abbreviations and --build-fake-ancestor,
            # which writes an index even in otherwise report-only invocations.
            unknown.append(token.split('=', 1)[0])
    return GitApplyEvidence(tuple(risky), tuple(unknown), report and not apply)


def _classify_git_apply(args, *, require_known_options=False):
    """Parameter-sensitive apply policy; unknown shapes retain approval.

    Default apply preflights the patch and fails without partial application.
    It does not move HEAD or rewrite history, but still needs writer admission.
    Conflict/partial/outside-root modes are not in that ordinary-write class.
    Report/check modes are read-only unless --apply requests application too.
    Parse option values and -- before deciding: a pathname is not an option.
    """
    evidence = git_apply_option_evidence(args)
    if require_known_options and evidence.unknown_options:
        return None
    if evidence.risky_options:
        return 'tree_destroying'
    if evidence.unknown_options:
        return None
    return 'readonly' if evidence.report_only else 'safe_write'


# Options that change neither the destination nor what is overwritten:
# progress output, a worktree lock, upstream tracking and checkout of the new
# tree. ``-f``/``--force``/``-B`` and anything unknown keep the human gate.
_GIT_WORKTREE_ADD_FLAGS = frozenset({
    '-q', '--quiet', '--lock', '--track', '--no-track', '--checkout',
    '--no-checkout', '--guess-remote', '--no-guess-remote',
})


def _git_worktree_add_destination(args):
    """Literal additive worktree shape; never force/reset an existing ref.

    Exactly one of ``-b NAME`` or ``--detach``, any of the flags above, then
    the path and an optional commit-ish. Git's option parser permutes, so the
    options may also follow the path: ``add <path> -b <branch> HEAD`` is the
    same creation. A ``-q`` or a trailing ``-b`` used to make an otherwise
    identical add unclassified, so it fenced the whole checkout (and its Git
    metadata) for the add's 30-54 s. ``--``, a repeated mode, an unknown
    option or a third operand leave the shape unclassified.
    """
    if not args or args[0] != 'add':
        return None
    rest, branch_forms, index = args[1:], 0, 0
    positionals = []
    while index < len(rest):
        word = rest[index]
        if word in _GIT_WORKTREE_ADD_FLAGS:
            index += 1
        elif word == '-b':
            if index + 1 >= len(rest) or not rest[index + 1] or rest[index + 1].startswith('-'):
                return None
            branch_forms += 1
            index += 2
        elif word == '--detach':
            branch_forms += 1
            index += 1
        elif not word or word.startswith('-'):
            return None
        else:
            positionals.append(word)
            index += 1
    if branch_forms != 1 or len(positionals) not in (1, 2):
        return None
    return positionals[0]


GitWorktreeLifecycle = namedtuple('GitWorktreeLifecycle', 'operation paths forced')


def git_worktree_lifecycle(args):
    """Parse native remove/move once for classification, targets and review.

    This is syntax evidence only. The target resolver validates relative
    operands against the invoking checkout; force never proves ownership or
    that discarded files, commits and worktree locks are expendable.
    """
    if not args or args[0] not in ('remove', 'move'):
        return None
    operands = []
    options_done = False
    forced = False
    for value in args[1:]:
        if value == '--' and not options_done:
            options_done = True
        elif not options_done and value.startswith('-'):
            if value not in ('-f', '--force'):
                return None
            forced = True
        else:
            operands.append(value)
    required = 1 if args[0] == 'remove' else 2
    if len(operands) != required:
        return None
    if any(not value or value.startswith('~') for value in operands):
        return None
    return GitWorktreeLifecycle(args[0], tuple(operands), forced)


def _git_worktree_lifecycle_targets(args):
    """Classify only literal absolute targets; relative paths need cwd evidence."""
    lifecycle = git_worktree_lifecycle(args)
    if lifecycle and all(os.path.isabs(path) for path in lifecycle.paths):
        return lifecycle.paths
    return None


GitIntegrationEvidence = namedtuple(
    'GitIntegrationEvidence', 'risky_options unknown_options external_options')


def git_integration_option_evidence(subcommand, args):
    """Pure per-verb option evidence from the SAME parser used for risk.

    Tuple fields contain option spellings, never consumed message/key values.
    Recovery/autostash/strategy is risky; unknown or invalid options require
    attended fallback; signing invokes external machinery and is not discard.
    ``--`` ends options. A value looking like --abort is still only data.
    """
    if subcommand not in {'merge', 'cherry-pick', 'revert'}:
        return GitIntegrationEvidence((), ('unsupported integration verb',), ())
    flags = {'--no-commit', '--commit', '-n', '--no-edit', '--edit', '-e',
             '--signoff', '--no-signoff', '--continue', '--no-gpg-sign',
             '--rerere-autoupdate', '--no-rerere-autoupdate'}
    values = {'--cleanup': 'cleanup', '--strategy': 'strategy',
              '--strategy-option': 'strategy', '-X': 'strategy'}
    risky_flags = {'--abort', '--quit', '--skip', '--autostash'}
    if subcommand == 'merge':
        flags |= {'--ff', '--no-ff', '--ff-only', '--squash', '--no-squash',
                  '--quiet', '-q', '--verbose', '-v', '--stat', '--no-stat',
                  '--summary', '--no-summary', '--compact-summary',
                  '--no-compact-summary', '--no-autostash', '--log', '--no-log',
                  '--progress', '--no-progress', '--allow-unrelated-histories',
                  '--overwrite-ignore', '--no-overwrite-ignore',
                  '--verify', '--no-verify'}
        values.update({'-m': 'message', '--message': 'message',
                       '-F': 'file', '--file': 'file', '--into-name': 'name',
                       '-s': 'strategy'})
    else:
        flags |= {'-s'}
        values.update({'-m': 'mainline', '--mainline': 'mainline'})
        if subcommand == 'cherry-pick':
            flags |= {'-x', '--ff', '--no-ff', '--allow-empty', '--no-allow-empty',
                      '--allow-empty-message', '--no-allow-empty-message',
                      '--keep-redundant-commits', '--no-keep-redundant-commits'}
            values['--empty'] = 'empty'
        else:
            flags |= {'--reference', '--no-reference'}
    risky, unknown, external = [], [], []
    if not args:
        unknown.append('missing integration arguments')
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break
        if token in flags or not token.startswith('-'):
            continue
        if token in risky_flags:
            risky.append(token)
            continue
        if (token in {'-S', '--gpg-sign', '--verify-signatures', '--no-verify-signatures'}
                or token.startswith(('-S', '--gpg-sign='))):
            external.append(token.split('=', 1)[0] if token.startswith('--') else '-S')
            continue
        option, value = token, None
        if token in values:
            if index < len(args):
                value = args[index]
                index += 1
        elif token.startswith('--') and '=' in token:
            option, value = token.split('=', 1)
            if option == '--log' and subcommand == 'merge':
                if not value.isdigit():
                    unknown.append(option)
                continue
        elif token[:2] in values and len(token) > 2:
            option, value = token[:2], token[2:]
        kind = values.get(option)
        valid = kind is not None and value is not None
        if kind == 'mainline':
            valid = valid and re.fullmatch(r'[1-9][0-9]*', value) is not None
        elif kind == 'cleanup':
            valid = valid and value in {'strip', 'whitespace', 'verbatim', 'scissors', 'default'}
        elif kind == 'empty':
            valid = valid and value in {'drop', 'keep', 'stop'}
        elif kind in {'strategy', 'file', 'name'}:
            valid = valid and bool(value) and not value.startswith('-')
        if not valid:
            unknown.append(option)
        elif kind == 'strategy':
            risky.append(option)
    return GitIntegrationEvidence(tuple(risky), tuple(unknown), tuple(external))


def _classify_git_integration(subcommand, args, *, require_known_options=False):
    evidence = git_integration_option_evidence(subcommand, args)
    unproved = evidence.unknown_options or evidence.external_options
    if require_known_options and unproved:
        return None
    if evidence.risky_options:
        return 'tree_destroying'
    return None if unproved else 'safe_write'


def _classify_git_clean(args):
    """Only proven non-interactive dry-run syntax earns read authority."""
    preview = False
    unknown = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == '--':
            break  # All remaining tokens are pathspec data.
        if token in {'--dry-run', '--no-dry-run'}:
            preview = token == '--dry-run'
        elif token in {'--force', '--no-force', '--quiet', '--no-quiet'}:
            continue
        elif token in {'--interactive', '--no-interactive'}:
            unknown = True
        elif token in {'-e', '--exclude'}:
            if index >= len(args):
                unknown = True
            else:
                index += 1  # Even --dry-run is a pattern, not a preview flag.
        elif token.startswith('--exclude='):
            continue
        elif token.startswith('--'):
            unknown = True
        elif token.startswith('-') and token != '-':
            for offset, flag in enumerate(token[1:], 1):
                if flag == 'e':
                    if offset == len(token) - 1:
                        if index >= len(args):
                            unknown = True
                        else:
                            index += 1
                    break  # Rest of bundle is the exclude value.
                if flag == 'n':
                    preview = True
                elif flag not in 'dfqxX':
                    unknown = True
    if unknown:
        return None
    return 'readonly' if preview else 'tree_destroying'


def _classify_git_invocation(args, *, require_known_options=False):
    details = _git_invocation_details(args)
    if details is None:
        return None
    sub, rest, unproved, autostash = details
    native = _classify_git_native_invocation(
        sub, rest, require_known_options=require_known_options)
    if autostash and sub == 'merge' and not require_known_options:
        return 'tree_destroying'
    if unproved:
        return 'tree_destroying' if native == 'tree_destroying' and not require_known_options else None
    return native


# Output flags are refused: ``-O``/``--open-files-in-pager`` spawn an arbitrary
# configured pager, which can write. Everything else in this grammar only reads
# the worktree, the index, or a literal pattern file.
_GIT_GREP_FLAGS = frozenset({
    '-n', '--line-number', '-i', '--ignore-case', '-I', '-w', '--word-regexp',
    '-v', '--invert-match', '-c', '--count', '-l', '--files-with-matches',
    '-L', '--files-without-match', '-o', '--only-matching', '-h', '--no-filename',
    '-z', '--null', '-E', '--extended-regexp', '-G', '--basic-regexp',
    '-F', '--fixed-strings', '-P', '--perl-regexp', '-p', '--show-function',
    '--full-name', '--column', '--heading', '--break', '--no-color',
    '--all-match', '--and', '--or', '--not', '--cached', '--untracked',
    '--no-index', '--recurse-submodules', '--no-exclude-standard',
    '--exclude-standard', '-a', '--text', '--no-textconv', '--no-ext-diff',
})
_GIT_GREP_VALUES = frozenset({
    '-e', '--regexp', '-f', '--file', '-A', '--after-context',
    '-B', '--before-context', '-C', '--context', '-m', '--max-count',
    '--max-depth', '--threads',
})
# Short-flag letters Git itself accepts: booleans, and options whose value may
# be attached (``-A3``, ``-ePATTERN``). ``O`` is deliberately absent.
_GIT_GREP_SHORT_FLAGS = frozenset('niIwvclLohzEGFPrWa')
_GIT_GREP_SHORT_VALUES = frozenset('efABCm')
_GIT_GREP_REFUSED = ('-O', '--open-files-in-pager', '--ext-diff', '--textconv')


def _classify_git_grep(rest):
    """Prove a ``git grep`` argv cannot write or spawn an output program.

    Pattern and path arguments are data. Unknown flags and short-flag bundles
    stay unresolved so admission keeps the writer lease.
    """
    if any(token.startswith(_GIT_GREP_REFUSED) for token in rest):
        return None
    index = 0
    while index < len(rest):
        token = rest[index]
        index += 1
        if token == '--':
            break
        if token in _GIT_GREP_VALUES:
            if index == len(rest):
                return None
            index += 1
            continue
        if token.split('=', 1)[0] in _GIT_GREP_VALUES and '=' in token:
            continue
        if token.startswith('--color=') or token.startswith('--threads='):
            continue
        if token in _GIT_GREP_FLAGS or not token.startswith('-'):
            continue
        if not token.startswith('--') and _git_grep_short_argv(token[1:]):
            continue
        return None
    return 'readonly'


def _git_symbolic_ref_reads(rest):
    """``symbolic-ref [-q|--quiet] [--short] <name>`` only prints the target.

    A second operand rewrites the ref, ``-m`` records the reason for that write
    and ``-d`` deletes it. Bundles, abbreviations and unknown options are not
    proved to read.
    """
    operands = [token for token in rest if token not in {'-q', '--quiet', '--short'}]
    return len(operands) == 1 and not operands[0].startswith('-')


def _git_hash_object_runs_filters(rest):
    """Parse a ``git hash-object`` argv that cannot write the object store.

    Returns None for ``-w``, ``--literally``, bundled or unknown options and a
    missing value. Otherwise returns whether a configured clean/process filter
    driver can run: a path input (a file operand, ``--stdin-paths`` or
    ``--stdin --path``) is converted unless ``--no-filters`` is given.
    """
    files, stdin, stdin_paths, no_filters, path = [], False, False, False, None
    index = 0
    while index < len(rest):
        token = rest[index]
        index += 1
        if token == '--':
            files.extend(rest[index:])
            break
        if token in {'-t', '--path'}:
            if index == len(rest):
                return None
            if token == '--path':
                path = rest[index]
            index += 1
        elif token.startswith('--path='):
            path = token.split('=', 1)[1]
        elif token == '--stdin':
            stdin = True
        elif token == '--stdin-paths':
            stdin_paths = True
        elif token == '--no-filters':
            no_filters = True
        elif token.startswith('-'):
            return None
        else:
            files.append(token)
    return not no_filters and bool(files or stdin_paths or (stdin and path is not None))


def _git_grep_short_argv(letters):
    """Prove a bundled short-flag group without expanding Git's own parsing."""
    for position, letter in enumerate(letters):
        if letter in _GIT_GREP_SHORT_FLAGS:
            continue
        if letter in _GIT_GREP_SHORT_VALUES:
            return True  # Attached remainder (or the next token) is its value.
        return False
    return bool(letters)


def _classify_git_native_invocation(sub, rest, *, require_known_options=False):
    """Classify one ``git ...`` argv (words AFTER the ``git`` word).

    Returns 'readonly' | 'safe_write' | 'tree_destroying' | None (unknown
    sub-command or global-flag shape → caller falls back to the human gate).
    """
    if not sub:
        return 'readonly'  # Bare Git/global help prints without mutation.

    if sub == 'worktree':
        if rest in (['list'], ['list', '--porcelain'], ['list', '--porcelain', '-z']):
            return 'readonly'
        if _git_worktree_add_destination(rest) is not None:
            return 'safe_write'
        if _git_worktree_lifecycle_targets(rest) is not None:
            return 'tree_destroying'
        return None
    if sub in {'merge', 'cherry-pick', 'revert'}:
        return _classify_git_integration(
            sub, rest, require_known_options=require_known_options)
    if sub == 'clean':
        return _classify_git_clean(rest)
    if sub == 'apply':
        return _classify_git_apply(rest, require_known_options=require_known_options)
    if sub == 'grep':
        return _classify_git_grep(rest)
    if sub == 'hash-object':
        return 'readonly' if _git_hash_object_runs_filters(rest) is not None else None
    if sub == 'stash':
        if any(tok.startswith(('--output', '--ext-diff', '--textconv')) for tok in rest):
            return None
        # list/show are read-only; every other form (incl. bare `git stash`
        # == push) mutates the working tree or the shared stash stack.
        if rest[:1] not in (['list'], ['show']):
            return 'tree_destroying'
        flags = {'-p', '--patch', '--stat', '--numstat', '--name-only',
                 '--name-status', '--oneline', '--no-patch', '-s', '--raw',
                 '--no-ext-diff', '--no-textconv', '--no-color', '--all',
                 '--include-untracked', '--only-untracked', '-u'}
        values = {'--format', '--pretty', '--date', '--max-count', '-n',
                  '--grep', '--author', '--since', '--until'}
        index = 1
        while index < len(rest):
            token = rest[index]
            index += 1
            if token == '--':
                break
            if token in values:
                if index == len(rest):
                    return None
                value = rest[index]
                index += 1
            elif token.split('=', 1)[0] in values and '=' in token:
                value = token.split('=', 1)[1]
            elif (not token.startswith('-') or token in flags
                  or re.fullmatch(r'-[0-9]+|-n[0-9]+|-U[0-9]+', token)):
                continue
            else:
                return None
            if token.split('=', 1)[0] in {'--format', '--pretty'} and '%G' in value:
                return None
        return 'readonly'
    if sub in {'branch', 'tag', 'push'}:
        # Consume maintained value options before risk scans. Values are data.
        values = ({'--push-option', '-o'} if sub == 'push' else
                  {'--format', '--sort', '--contains', '--no-contains',
                   '--merged', '--no-merged', '--points-at'})
        filtered = []
        index = 0
        while index < len(rest):
            token = rest[index]
            index += 1
            if token == '--':
                filtered.extend(rest[index:])
                break
            if token in values:
                if index == len(rest):
                    if sub != 'push' and token in _GIT_OPTIONAL_COMMIT_OPTIONS:
                        filtered.append(token + '=value')  # Defaults to HEAD.
                        continue
                    return None
                index += 1
                if sub != 'push':
                    filtered.append(token + '=value')
            elif any(token.startswith(option + '=') for option in values if option.startswith('--')):
                if sub != 'push':
                    filtered.append(token.split('=', 1)[0] + '=value')
            else:
                filtered.append(token)
        rest = filtered

        known_flags = ({'--verbose', '--quiet', '--dry-run', '--set-upstream',
                        '--all', '--tags', '--atomic', '--follow-tags', '--porcelain',
                        '--no-verify', '--verify', '--ipv4', '--ipv6', '--force',
                        '--force-with-lease', '--delete', '--mirror'} if sub == 'push' else
                       {'-d', '-D', '--delete', '-m', '-M', '--move', '-c', '-C',
                        '--copy', '-f', '--force', '--edit-description',
                        '--set-upstream-to', '--unset-upstream', '--list', '-l',
                        '-a', '--all', '-r', '--remotes', '-v', '-vv', '--verbose',
                        '--show-current'})
        if require_known_options and any(
                token.startswith('-') and token not in known_flags
                and token.split('=', 1)[0] not in values
                and not (sub == 'push' and re.fullmatch(r'-[vqnu46fd]+|-o.+', token))
                and not (sub != 'push' and re.fullmatch(r'-[avrdDfFmMcC]+', token))
                for token in rest):
            return None
    if sub == 'branch':
        # Plain -d/--delete is self-guarding: Git refuses to delete a branch
        # that is unmerged (against HEAD or its upstream) or checked out in
        # any worktree, so a successful delete can neither orphan commits nor
        # break a sibling's worktree. Only the narrow shape — one delete flag
        # plus plain branch names — earns safe_write; force (-D is -df, so a
        # bundled 'f' too), unknown options, or mixed delete/rename shapes
        # fall through to the conservative destructive-flag scan below.
        delete_flag = next((tok for tok in rest
                            if tok in ('-d', '--delete')), None)
        if (delete_flag is not None
                and rest.count(delete_flag) == 1
                and not any(tok in ('-d', '--delete') and tok != delete_flag
                            for tok in rest)):
            others = [tok for tok in rest if tok != delete_flag]
            if others and all(not tok.startswith('-') for tok in others):
                return 'safe_write'
    if sub in ('branch', 'tag'):
        destructive_flags = {'-d', '-D', '--delete', '-m', '-M', '--move',
                             '-c', '-C', '--copy', '-f', '--force',
                             '--edit-description', '--set-upstream-to',
                             '--unset-upstream'}
        if any(tok.split('=')[0] in destructive_flags
               or (tok.startswith('-') and not tok.startswith('--')
                   and any(flag in tok[1:]
                           for flag in 'Mm' + _GIT_GUARD_FORCE_SHORT_FLAGS[sub]))
               for tok in rest):
            return 'tree_destroying'
        if not rest or all(tok in ('-a', '--all', '-r', '--remotes',
                                  '-v', '-vv', '--verbose', '--show-current')
                           or re.fullmatch(r'-[arv]+', tok) for tok in rest):
            return 'readonly'
        # Value options arrive here as ``--contains=value``.
        if any(tok.split('=', 1)[0] in ('--list', '-l', '--contains', '--no-contains',
                                        '--merged', '--no-merged', '--points-at', '--format',
                                        '--sort') for tok in rest):
            # Unknown options are not proved read-only.
            allowed = ('--list', '--format', '--sort', '--contains', '--no-contains', '--merged',
                       '--no-merged', '--points-at', '--format=', '--sort=')
            if all(not tok.startswith('-') or re.fullmatch(r'-[larv]+', tok)
                   or tok in ('--all', '--remotes', '--verbose')
                   or any(tok == flag or tok.startswith(flag + '=')
                          if not flag.endswith('=') else tok.startswith(flag)
                          for flag in allowed) for tok in rest):
                return 'readonly'
        return 'safe_write' if all(not tok.startswith('-') for tok in rest) else None
    if sub == 'config':
        reads = {'--get', '--get-all', '--get-regexp', '--get-urlmatch',
                 '--list', '-l', '--show-origin', '--show-scope',
                 '--local', '--global', '--system', '--includes', '--null', '-z'}
        if rest and rest[0] in ('get', 'list'):
            return 'readonly' if not any(tok.startswith('--output') for tok in rest) else None
        if any(tok in ('--get', '--get-all', '--get-regexp', '--get-urlmatch',
                       '--list', '-l') for tok in rest):
            return 'readonly' if all(not tok.startswith('-') or tok in reads for tok in rest) else None
        operands = [tok for tok in rest if not tok.startswith('-')]
        if (len(operands) == 1 and operands[0] not in _GIT_CONFIG_ACTIONS
                and all(tok in reads for tok in rest if tok.startswith('-'))):
            return 'readonly'  # `git config [--scope] KEY` prints one value.
        return None  # Setter, editor and action forms keep the existing approval gate.
    if sub == 'remote':
        return 'readonly' if not rest or rest == ['-v'] else None
    if sub == 'reflog':
        # Only these subcommands rewrite reflogs. Any other first argument (none,
        # a log option, show/list/exists, or a ref) runs the default `show`:
        # git 2.34 treats `git reflog list`/`drop` as revisions of `show`; newer
        # git adds `drop` as a rewrite, so it stays destructive on every version.
        return 'tree_destroying' if rest[:1] and rest[0] in ('expire', 'delete', 'drop') else 'readonly'
    if sub in ('update-ref', 'symbolic-ref', 'replace'):
        if sub == 'symbolic-ref' and _git_symbolic_ref_reads(rest):
            return 'readonly'
        return 'tree_destroying'
    if sub == 'commit' and _commit_rewrites_history(rest):
        return 'tree_destroying'
    # Git accepts bundled short options (-vf, -vd, -pf). Scan only the
    # known bare-option prefix: -o consumes the remaining text as its value,
    # and fetch's -j consumes a job count, so neither may expose fake flags.
    if sub == 'fetch' and any(
            tok.startswith(('--force', '--prune', '--update-head-ok', '+'))
            or _re.match(r'^-[vqamtnk46]*[fpPu]', tok) for tok in rest):
        return 'tree_destroying'
    if sub == 'push' and any(
            tok.startswith(('--force', '--delete', '--mirror', '+', ':'))
            or _re.match(r'^-[vqnu46]*[fd]', tok) for tok in rest):
        return 'tree_destroying'

    if sub == 'push':
        flags = {'--verbose', '--quiet', '--dry-run', '--set-upstream',
                 '--all', '--tags', '--atomic', '--follow-tags', '--porcelain',
                 '--no-verify', '--verify', '--ipv4', '--ipv6'}
        if any(token.startswith('-') and token not in flags
               and not re.fullmatch(r'-[vqnu46]+|-o.+', token) for token in rest):
            return None
    if sub in _GIT_READONLY_SUBCOMMANDS:
        if any(tok.startswith(('--output', '--ext-diff', '--textconv')) for tok in rest):
            return None
        return 'readonly'
    if sub in ('checkout', 'switch'):
        return _classify_checkout_like(sub, rest)
    if sub in _GIT_GUARD_SAFE_WRITE:
        return 'safe_write'
    if sub in _GIT_GUARD_TREE_DESTROYING:
        return 'tree_destroying'
    return None


def _git_guard_classify(command, *, require_known_segments=False):
    """Classify *command* for the shared-checkout Git workspace guard.

    Returns 'readonly' | 'safe_write' | 'tree_destroying' | None. None means
    the guard does not engage (no Git segment, unknown Git shape, or an
    opaque non-Git segment without a recognized high-risk Git mutation) and
    the caller keeps the existing attended-gate behavior. High-risk mutations
    take precedence over unknown segments regardless of ordering. Unknown
    segments take precedence over ordinary writes. Risk classification does
    not authorize opaque segments: receipt callers require
    ``require_known_segments=True`` in addition to the risk verdict. A non-Git
    segment is tolerated for automatic ordinary-write receipts only when it
    is provably read-only (``git add -A && git commit -m wip`` is safe_write;
    ``git add x && rm -rf build`` is None → human gate).

    Quoting/pipelines follow the same parser as the destructive classifier
    (``_split_pipeline`` / ``_shell_words``), so words inside quoted literals
    cannot masquerade as sub-commands. Absolute-path git (``/usr/bin/git``)
    IS seen through via basename matching. Best-effort guard-rail on par
    with ``_is_catastrophic_delete`` — not a security boundary.
    """
    if not command or 'git' not in command:
        return None
    saw_git = False
    saw_safe_write = False
    saw_tree_destroying = False
    saw_unknown = False
    for seg in _split_pipeline(command):
        seg = peeled_command(seg.strip())
        if not seg:
            continue
        words = [w.text for w in _shell_words(seg)]
        i = 0
        while i < len(words) and _ENV_ASSIGNMENT_GUARD_RE.match(words[i]):
            i += 1
        words = git_command_words(words[i:])

        if re.search(r'\bGIT_CONFIG_(?:COUNT|KEY_[0-9]+|VALUE_[0-9]+|PARAMETERS)=', seg):
            saw_unknown = True
        if not words:
            saw_unknown = True
            continue
        # Applies to filter/helper segments too, not only the Git argv.
        if shell_source_has_expansion(seg, pathname=False):
            saw_unknown = True
        base = words[0].split('/')[-1]
        if base != 'git':
            if base in _READONLY_COMMANDS and not _is_destructive_command(seg):
                continue
            saw_unknown = True  # opaque non-git segment → existing gate
            continue
        saw_git = True
        cls = _classify_git_invocation(
            words[1:], require_known_options=require_known_segments)

        if cls == 'readonly' and _REDIRECT_PATTERN.search(
                _REDIRECT_TO_DEV_NULL.sub('', _mask_quoted_literals(seg))):
            saw_unknown = True  # e.g. git status > tracked-file is not read-only
        if cls == 'tree_destroying':
            saw_tree_destroying = True
        elif cls is None:
            saw_unknown = True
        elif cls == 'safe_write':
            saw_safe_write = True
    # Inspect the entire chain: opaque prefixes must not hide a later reset,
    # but opaque suffixes must not borrow an additive Git receipt either.
    if require_known_segments and saw_unknown:
        return None
    if saw_tree_destroying:
        return 'tree_destroying'
    if not saw_git or saw_unknown:
        return None
    return 'safe_write' if saw_safe_write else 'readonly'
