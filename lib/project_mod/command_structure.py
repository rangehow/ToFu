"""Pure, bounded shell structure for the catastrophic-delete and
dangerous-command guards and for every ``run_command`` rewrite.

Responsibility: split one shell command into simple commands, subshell groups
and the scripts nested inside them, so the guards
(``command_analysis._catastrophic_delete_detail`` and
``command_analysis.dangerous_command_rule``) judge only words a shell would
run as commands and never data, and so a rewrite edits only command text
(:func:`shell_rewrite_text`); and group those items into the compound
commands a shell executes (:func:`control_structure`) for a caller that
follows execution order. Entry points: :func:`parse_shell_structure`,
:func:`control_structure`, :func:`shell_rewrite_text`. Dependencies: the
standard library only; it executes, reads and writes nothing.

Each construct is delimited the way a POSIX/bash parser delimits it:

* quoting: ``'…'``, ``"…"``, ``$'…'``, ``$"…"``, backslash escapes and
  line continuations;
* comments: ``#`` at the start of a word;
* substitutions ``$(…)``, ``…`` in backticks, ``$((…))``, ``${…}``,
  ``<(…)`` and ``>(…)``: every command or process substitution body is a
  nested script, parsed recursively;
* heredocs (``<<WORD``, ``<<-WORD``, quoted delimiters): the body is captured
  at the newline that ends its command line and is never tokenized as
  commands; an expanding (unquoted-delimiter) body contributes its command
  substitutions as nested scripts;
* here-strings and redirections, which are never command words;
* compound syntax: ``( … )`` groups, ``case … esac`` clauses (marked by
  :class:`CaseClause` and :class:`CaseEnd` items), ``[[ … ]]`` tests,
  ``(( … ))`` arithmetic, ``name()`` function definitions, array
  assignments. Reserved words (``if``, ``then``, ``do``, ``{`` …) stay leading
  words of the next simple command for the caller to strip;
  :func:`control_structure` pairs them up.

Anything this parser cannot delimit raises :class:`ShellStructureUnknown`;
the caller decides how conservative to be. Whether a heredoc body or a
``-c`` string is executed by a shell is the caller's judgement, not syntax.

A rewrite (:func:`shell_rewrite_text`) may edit only text a shell parses as
commands from these exact bytes: never a heredoc body or its delimiter line,
a comment or a backtick body (escaped source), and never a quoted string or
here-string, which stay words. The one exception is the body of a quoted
``<<`` heredoc the caller judges a local shell reads as its script: those
bytes reach that shell unchanged and follow the same rule. A ``$'…'`` string
whose end differs between bash and POSIX sh (an escaped quote inside) leaves
the command undelimited for rewriting.
"""

import re
from collections import namedtuple

_MAX_DEPTH = 32

# A redirection operator at a word start: optional descriptor number or
# {varname}, then the operator. ``&>``/``&>>`` redirect both streams.
_REDIRECT = re.compile(
    r'(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:<<<|<<-|<<|<>|<&|>&|>>|>\||<|>)|&>>?')
_ARRAY_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=\Z')
_WORD_END = frozenset(' \t\n;&|()<>')
# Reserved words after which a new command may start in the same line.
_LEADING_RESERVED = frozenset({
    '!', '{', '}', 'if', 'then', 'else', 'elif', 'fi', 'do', 'done', 'while',
    'until', 'time', 'function',
})


class ShellStructureUnknown(ValueError):
    """The source holds a construct this parser cannot delimit."""

    def __init__(self, reason, offset):
        super().__init__(f'{reason} at offset {offset}')
        self.reason = reason
        self.offset = offset


SimpleCommand = namedtuple(
    'SimpleCommand', ['words', 'redirects', 'separator', 'nested', 'heredocs'])
SimpleCommand.__doc__ = """One simple command.

``words``: raw word texts as written (quotes kept, line continuations
removed), redirections excluded. ``redirects``: ``(operator, raw target)``
pairs, here-strings included, heredocs excluded. ``separator``: the control
operator before the command (``''``, ``';'``, ``'\\n'``, ``'&'``, ``'&&'``,
``'||'``, ``'|'``). ``nested``: scripts run while expanding the words and
redirects (command/process substitutions). ``heredocs``: :class:`Heredoc`.
"""

Subshell = namedtuple(
    'Subshell', ['body', 'separator', 'redirects', 'nested', 'heredocs'])
Subshell.__doc__ = """A ``( … )`` group: *body* runs in a child shell."""

CaseClause = namedtuple(
    'CaseClause',
    ['patterns', 'words', 'redirects', 'separator', 'nested', 'heredocs'])
CaseClause.__doc__ = """Start of one ``case`` clause; its commands follow.

A marker, never a command: ``words``, ``redirects`` and ``heredocs`` are
empty. ``patterns``: the raw pattern words. ``nested``: the substitutions the
patterns run. ``separator``: the terminator of the clause before it (``''``
for the first clause, ``';;'``, ``';&'`` or ``';;&'``).
"""

CaseEnd = namedtuple(
    'CaseEnd', ['words', 'redirects', 'separator', 'nested', 'heredocs'])
CaseEnd.__doc__ = """``esac``, a marker: ``redirects``, ``nested`` and ``heredocs``
apply to the whole ``case`` command; ``separator`` is the terminator of its
last clause (``''`` when none); ``words`` is empty.
"""


class Heredoc:
    """One heredoc: its body is stdin data for the command that owns it."""

    __slots__ = ('delimiter', 'quoted', 'strip_tabs', 'body', 'nested', 'offset',
                 'body_start', 'end')

    def __init__(self, delimiter, quoted, strip_tabs, offset):
        self.delimiter = delimiter
        self.quoted = quoted          # quoted delimiter: the body is literal
        self.strip_tabs = strip_tabs  # <<- strips leading tabs
        self.body = ''
        self.nested = ()              # substitutions an expanding body runs
        self.offset = offset
        # Source offsets of the body and past its delimiter line, set only by
        # a parse in source coordinates (shell_rewrite_text); None otherwise.
        self.body_start = None
        self.end = None


def parse_shell_structure(source):
    """Return the items (``SimpleCommand``/``Subshell``, and the
    ``CaseClause``/``CaseEnd`` markers of a ``case``) of *source*, in order."""
    return _Lexer(source, 0, 0).script()


# ── The commands a shell executes, in execution order ──────────────────────

AndOr = namedtuple('AndOr', ['pipelines', 'background'])
AndOr.__doc__ = """Pipelines joined by ``&&``/``||``: ``pipelines`` are
``(operator, Pipeline)`` pairs, the operator before each (``''`` for the
first); ``background`` when an ``&`` ends the list (it runs in a child shell).
A script is a tuple of these."""

Pipeline = namedtuple('Pipeline', ['commands'])
Pipeline.__doc__ = """Commands joined by ``|`` (:class:`ItemRun` or
:class:`Compound`); with more than one, each runs in a child shell."""

ItemRun = namedtuple('ItemRun', ['index', 'words'])
ItemRun.__doc__ = """Item ``index`` of the script run as one command: a
``Subshell`` (``words`` is None) or a ``SimpleCommand`` whose ``words`` follow
the reserved words in front of it."""

Compound = namedtuple('Compound', ['kind', 'parts', 'items'])
Compound.__doc__ = """One compound command; ``items`` are the indices of the
items that hold only its syntax (``fi``, ``done``, a ``for`` or ``case``
header …), whose redirects and substitutions belong to the whole command.

``kind`` and ``parts``: ``'group'`` ``(body,)``; ``'if'`` ``(clauses,
else_body)`` with ``(condition, body)`` clauses and ``else_body`` None when
absent; ``'while'``/``'until'`` ``(condition, body)``; ``'for'`` ``(header,
body)``, ``header`` the raw words from ``for``/``select`` before ``do``;
``'case'`` ``(clauses,)`` of ``(CaseClause item index, body, terminator)``;
``'function'`` ``(name, body)``, ``body`` the command node it defines. Every
condition and body is a script."""

_Token = namedtuple('_Token', ['kind', 'value', 'index', 'separator'])
# Reserved words in front of a command; ``for``/``select``, ``case``,
# ``function`` and ``time`` have rules of their own.
_CONTROL_WORDS = frozenset({
    '{', '}', '!', 'if', 'then', 'elif', 'else', 'fi', 'while', 'until', 'do',
    'done'})
_CLOSING_WORDS = frozenset({'}', 'fi', 'done'})
_TIMED_WORDS = _CONTROL_WORDS | {'for', 'select', 'case', 'function'}
_IF_BODY_ENDS = frozenset({'elif', 'else', 'fi'})


class _Unreadable(Exception):
    """Reserved words that do not pair up the way a shell requires."""


def control_structure(items):
    """The commands a shell runs for *items* (one script of
    :func:`parse_shell_structure`): a tuple of :class:`AndOr` whose pipelines
    hold :class:`ItemRun` and :class:`Compound` nodes. None when the reserved
    words do not pair up the way a shell requires (the shell rejects such a
    script) or nest deeper than this module parses."""
    reader = _ControlReader(items)
    try:
        return reader.script(frozenset())
    except _Unreadable:
        return None


def _item_tokens(item):
    """``(kind, value)`` control tokens of one ``SimpleCommand`` item."""
    words, tokens, position = item.words, [], 0
    while position < len(words):
        word, rest = words[position], len(words) - position
        if word in _CONTROL_WORDS or (
                word == 'time' and rest > 1 and words[position + 1] in _TIMED_WORDS):
            tokens.append(('word', word))
            position += 1
        elif word in ('for', 'select'):
            end = _loop_header_end(words, position)
            tokens.append(('for', words[position:end]))
            position = end
        elif word == 'case':
            tokens.append(('case', words[position:]))
            position = len(words)
        elif word == 'function' and rest > 1:
            tokens.append(('function', words[position + 1]))
            position += 3 if words[position + 2:position + 3] == ('()',) else 2
        elif rest == 2 and words[position + 1] == '()':
            tokens.append(('function', word))
            position = len(words)
        else:
            tokens.append(('command', words[position:]))
            position = len(words)
    if not tokens or (tokens[-1][0] == 'word' and tokens[-1][1] not in _CLOSING_WORDS
                      and (item.redirects or item.nested or item.heredocs)):
        # `{ >log; … }`, `! >$(…)`: redirections alone are the first command.
        tokens.append(('command', ()))
    return tokens


def _loop_header_end(words, position):
    """End of the ``for``/``select`` header at ``words[position]``: its name and
    ``in`` list (or ``((…))`` clause), before a ``do`` in the same item."""
    following = position + 2
    if (words[position + 1:position + 2] and not words[position + 1].startswith('((')
            and words[following:following + 1] == ('in',)):
        return len(words)
    if words[following:following + 1] == ('do',):
        return following
    return len(words)


class _ControlReader:
    """Recursive descent over the control tokens of one script's items."""

    def __init__(self, items):
        self.tokens, self.commands = [], set()
        for index, item in enumerate(items):
            if isinstance(item, CaseClause):
                self.tokens.append(_Token('clause', None, index, item.separator))
            elif isinstance(item, CaseEnd):
                self.tokens.append(_Token('esac', None, index, item.separator))
            elif isinstance(item, Subshell):
                self.tokens.append(_Token('subshell', None, index, item.separator))
                self.commands.add(index)
            else:
                separator = item.separator
                for kind, value in _item_tokens(item):
                    self.tokens.append(_Token(kind, value, index, separator))
                    separator = ''
                    if kind == 'command':
                        self.commands.add(index)
        self.position = 0
        self.depth = 0

    def _peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _ends(self, stops):
        token = self._peek()
        return token is None or token.kind in stops or (
            token.kind == 'word' and token.value in stops)

    def _at(self, word):
        token = self._peek()
        return token is not None and token.kind == 'word' and token.value == word

    def _take(self, owned):
        """Consume the current token as syntax of the compound being read."""
        token = self._peek()
        if token.index not in self.commands and token.index not in owned:
            owned.append(token.index)
        self.position += 1
        return token

    def _expect(self, word, owned):
        if not self._at(word):
            raise _Unreadable(f'expected {word}')
        self._take(owned)

    def script(self, stops):
        lists = []
        while not self._ends(stops):
            lists.append(self._and_or(stops))
        return tuple(lists)

    def _and_or(self, stops):
        pipelines = [('', self._pipeline(stops))]
        while not self._ends(stops) and self._peek().separator in ('&&', '||'):
            pipelines.append((self._peek().separator, self._pipeline(stops)))
        token = self._peek()
        return AndOr(tuple(pipelines), token is not None and token.separator == '&')

    def _pipeline(self, stops):
        commands = [self._command()]
        while not self._ends(stops) and self._peek().separator == '|':
            commands.append(self._command())
        return Pipeline(tuple(commands))

    def _command(self):
        token = self._peek()
        if token is None:
            raise _Unreadable('a command is missing')
        if token.kind in ('command', 'subshell'):
            self.position += 1
            return ItemRun(token.index, token.value)
        if token.kind == 'word' and token.value in ('!', 'time'):
            self.position += 1
            return self._command()
        reader = {'for': self._for, 'case': self._case, 'function': self._function}.get(
            token.kind) or {'{': self._group, 'if': self._if, 'while': self._loop,
                            'until': self._loop}.get(token.value)
        if reader is None:
            raise _Unreadable(f'unexpected {token.value or token.kind}')
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise _Unreadable('nesting too deep')
        try:
            return reader()
        finally:
            self.depth -= 1

    def _group(self):
        owned = []
        self._expect('{', owned)
        body = self.script(frozenset({'}'}))
        self._expect('}', owned)
        return Compound('group', (body,), tuple(owned))

    def _if(self):
        owned, clauses, else_body = [], [], None
        self._expect('if', owned)
        while True:
            condition = self.script(frozenset({'then'}))
            self._expect('then', owned)
            clauses.append((condition, self.script(_IF_BODY_ENDS)))
            if not self._at('elif'):
                break
            self._take(owned)
        if self._at('else'):
            self._take(owned)
            else_body = self.script(frozenset({'fi'}))
        self._expect('fi', owned)
        return Compound('if', (tuple(clauses), else_body), tuple(owned))

    def _loop(self):
        owned = []
        kind = self._take(owned).value
        condition = self.script(frozenset({'do'}))
        self._expect('do', owned)
        body = self.script(frozenset({'done'}))
        self._expect('done', owned)
        return Compound(kind, (condition, body), tuple(owned))

    def _for(self):
        owned = []
        header = self._take(owned).value
        if self._at('{'):  # bash: `for x in a; { …; }`
            body = (AndOr((('', Pipeline((self._group(),))),), False),)
        else:
            self._expect('do', owned)
            body = self.script(frozenset({'done'}))
            self._expect('done', owned)
        return Compound('for', (header, body), tuple(owned))

    def _case(self):
        owned, markers, bodies = [], [], []
        self._take(owned)
        while True:
            token = self._peek()
            if token is None or token.kind not in ('clause', 'esac'):
                raise _Unreadable('expected a case clause')
            self.position += 1
            markers.append(token)
            if token.kind == 'esac':
                owned.append(token.index)
                break
            bodies.append(self.script(frozenset({'clause', 'esac'})))
        clauses = tuple((marker.index, body, following.separator)
                        for marker, body, following in zip(markers, bodies, markers[1:]))
        return Compound('case', (clauses,), tuple(owned))

    def _function(self):
        owned = []
        name = self._take(owned).value
        return Compound('function', (name, self._command()), tuple(owned))


RewriteText = namedtuple('RewriteText', ['view', 'ranges'])
RewriteText.__doc__ = """The command text of a source: what a rewrite may edit.

``view``: the source with every byte outside command text blanked to a space
(newlines kept), offset for offset, so a span rewriter analyses the view and
splices the original at the same offsets. ``ranges``: sorted, distinct
``(start, end)`` source ranges of the simple commands, nested substitution
bodies included, so a consumer rewrites a proven command in place without
interpreting separators, shell state or heredoc contents a second time; it
must not rewrite two overlapping ranges. Backtick bodies keep their escaped
source and yield no range.
"""


def shell_rewrite_text(source, scripts=None):
    """Return the :class:`RewriteText` of *source*.

    Command text is what a shell parses as commands from these exact bytes:
    the commands at top level, in ``( … )`` groups and in ``$( … )``,
    ``<( … )`` and ``>( … )`` bodies, with quoted strings and here-strings
    kept as the words they are. Heredoc bodies with their delimiter lines,
    comments and backtick bodies are blanked. ``scripts(items, index)`` is the
    caller's judgement of which heredocs of ``items[index]`` a local shell
    reads as its script: such a body with a quoted ``<<`` delimiter reaches
    that shell byte for byte and is command text by this same rule. Every
    other body is data; an unquoted one is expanded before anything reads it.
    Raises :class:`ShellStructureUnknown` when *source* cannot be delimited.
    """
    opaque, ranges = _rewrite_regions(source, 0, 0, scripts)
    view, cursor = [], 0
    for start, end in sorted(opaque):
        start = max(start, cursor)
        if start < end:
            view.append(source[cursor:start])
            view.append(_NOT_NEWLINE.sub(' ', source[start:end]))
            cursor = end
    view.append(source[cursor:])
    return RewriteText(''.join(view), tuple(sorted(set(ranges))))


_NOT_NEWLINE = re.compile(r'[^\n]')


def _rewrite_regions(source, base, depth, scripts):
    """``(regions outside command text, command ranges)`` of *source*, which
    starts at offset *base* of the whole command."""
    lexer = _Lexer(source, depth, base)
    lexer.command_ranges = []
    lexer.opaque = []
    items = lexer.script()
    opaque, ranges = lexer.opaque, lexer.command_ranges
    for heredoc, script in _heredoc_judgements(items, scripts):
        delimiter_line = heredoc.body_start + len(heredoc.body)
        opaque.append((delimiter_line, heredoc.end))
        if script and heredoc.quoted and not heredoc.strip_tabs:
            try:
                body_opaque, body_ranges = _rewrite_regions(
                    heredoc.body, heredoc.body_start, depth + 1, scripts)
            except ShellStructureUnknown:
                pass  # the shell will reject that script; its text stays data here
            else:
                opaque.extend(body_opaque)
                ranges.extend(body_ranges)
                continue
        opaque.append((heredoc.body_start, delimiter_line))
    return opaque, ranges


def _heredoc_judgements(items, scripts):
    """Yield ``(heredoc, read as a script)`` for each heredoc with a body in
    source coordinates that *items* or their nested scripts own."""
    for index, item in enumerate(items):
        for script in item.nested:
            yield from _heredoc_judgements(script, scripts)
        if isinstance(item, Subshell):
            yield from _heredoc_judgements(item.body, scripts)
        if not item.heredocs:
            continue
        chosen = scripts(items, index) if scripts is not None else ()
        for heredoc in item.heredocs:
            if heredoc.body_start is not None:
                yield heredoc, any(heredoc is pick for pick in chosen)


def _heredoc_delimiter(raw):
    """Dequote a heredoc delimiter word; any quoting makes the body literal."""
    quoted = any(char in raw for char in '\'"\\')
    out = []
    quote = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is None:
            if char in '\'"':
                quote = char
            elif char == '\\' and index + 1 < len(raw):
                index += 1
                out.append(raw[index])
            else:
                out.append(char)
        elif char == quote:
            quote = None
        elif (quote == '"' and char == '\\' and index + 1 < len(raw)
              and raw[index + 1] in '$`"\\'):
            index += 1
            out.append(raw[index])
        else:
            out.append(char)
        index += 1
    return ''.join(out), quoted


class _NotArithmetic(Exception):
    """``$((`` / ``((`` that does not close with ``))``: a nested subshell."""


class _Lexer:
    def __init__(self, source, depth, base):
        if depth > _MAX_DEPTH:
            raise ShellStructureUnknown('nesting too deep', base)
        self.s = source
        self.n = len(source)
        self.i = 0
        self.depth = depth
        self.base = base
        self.pending = []
        self.case_terminator = ''
        # Set to lists only by a parse in source coordinates (_rewrite_regions):
        # simple-command ranges and regions outside command text.
        self.command_ranges = None
        self.opaque = None

    def _fail(self, reason, offset=None):
        raise ShellStructureUnknown(reason, self.base + (self.i if offset is None else offset))

    # ── lists of commands ─────────────────────────────────────────────

    def script(self, closer=None):
        """Parse commands until EOF, a closing ``)`` or (``closer='case'``)
        the end of a case clause."""
        items = []
        separator = ''
        while True:
            self._blanks()
            if self.i >= self.n:
                if closer is not None:
                    self._fail(f'unclosed {"case" if closer == "case" else "("}')
                self._heredocs_at_eof()
                return tuple(items)
            char = self.s[self.i]
            if char == '\n':
                self._newline()
                if separator not in ('&&', '||', '|'):
                    separator = '\n'
                continue
            if char == '#':
                self._comment()
                continue
            if char == ')':
                if closer == ')':
                    self.i += 1
                    return tuple(items)
                self._fail('unbalanced )')
            if closer == 'case':
                for terminator in (';;&', ';;', ';&'):
                    if self.s.startswith(terminator, self.i):
                        self.i += len(terminator)
                        self.case_terminator = terminator
                        return tuple(items)
                if self._at_word('esac'):
                    self.case_terminator = ''
                    return tuple(items)
            if char == ';':
                if self.s.startswith(';;', self.i):
                    self._fail("';;' outside a case clause")
                self.i += 1
                separator = ';'
                continue
            if char == '&' and not self.s.startswith('&>', self.i):
                if self.s.startswith('&&', self.i):
                    self.i += 2
                    separator = '&&'
                else:
                    self.i += 1
                    separator = '&'
                continue
            if char == '|':
                if self.s.startswith('||', self.i):
                    self.i += 2
                    separator = '||'
                else:
                    self.i += 2 if self.s.startswith('|&', self.i) else 1
                    separator = '|'
                continue
            if char == '(':
                items.append(self._group(separator))
                separator = ''
                continue
            items.extend(self._command(separator))
            separator = ''

    def _group(self, separator):
        if self.s.startswith('((', self.i):
            nested = []
            start = self.i
            text = self._arithmetic(nested, 2)
            if text is not None:
                command = [text]
                redirects, more, heredocs = self._trailing_redirects()
                return SimpleCommand(tuple(command), redirects, separator,
                                     tuple(nested) + more, heredocs)
            self.i = start
        self.i += 1
        body = self._nested_script()
        redirects, nested, heredocs = self._trailing_redirects()
        return Subshell(body, separator, redirects, nested, heredocs)

    def _trailing_redirects(self):
        redirects, nested, heredocs = [], [], []
        while True:
            self._blanks()
            redirect = self._redirect(nested, heredocs)
            if redirect is None:
                return tuple(redirects), tuple(nested), tuple(heredocs)
            if redirect:
                redirects.append(redirect)

    def _command(self, separator):
        start = self.i
        words, redirects, nested, heredocs = [], [], [], []
        dbracket = False
        while True:
            self._blanks(newlines=dbracket)
            if self.i >= self.n:
                break
            char = self.s[self.i]
            # Control operators end the command even inside [[ … ]]: bash has
            # its own test grammar, but a POSIX shell parses [[ as an ordinary
            # command name and runs what follows &&/|| as the next command.
            if char in '\n;|)' or (char == '&' and not self.s.startswith('&>', self.i)):
                break
            if dbracket:
                word = self._word(nested, dbracket=True)
                words.append(word)
                if word == ']]':
                    dbracket = False
                continue
            if char == '#':
                break  # a comment starts at a word start; the caller skips it
            leading = all(word in _LEADING_RESERVED for word in words)
            if char == '(':
                if words and words[-1] == 'for' and self.s.startswith('((', self.i):
                    text = self._arithmetic(nested, 2)
                    if text is None:
                        self._fail('unexpected (')
                    words.append(text)
                    continue
                if leading:
                    break  # `if ( … )`, `! ( … )`: a group starts a new command
                if self._function_parens():
                    words.append('()')
                    break
                self._fail('unexpected (')
            redirect = self._redirect(nested, heredocs)
            if redirect is not None:
                if redirect:
                    redirects.append(redirect)
                continue
            word = self._word(nested)
            words.append(word)
            if leading and word == 'case':
                return self._case(words, redirects, separator, nested, heredocs)
            if leading and word == '[[':
                dbracket = True
        if dbracket:
            self._fail('unclosed [[')
        if self.command_ranges is not None:
            self.command_ranges.append((self.base + start, self.base + self.i))
        return [SimpleCommand(tuple(words), tuple(redirects), separator,
                              tuple(nested), tuple(heredocs))]

    def _case(self, words, redirects, separator, nested, heredocs):
        self._blanks()
        words.append(self._word(nested))  # the subject
        self._blanks(newlines=True)
        if not self._at_word('in'):
            self._fail('case without in')
        self.i += 2
        words.append('in')
        items = [SimpleCommand(tuple(words), tuple(redirects), separator,
                               tuple(nested), tuple(heredocs))]
        terminator = ''
        while True:
            self._blanks(newlines=True, comments=True)
            if self.i >= self.n:
                self._fail('unclosed case')
            if self._at_word('esac'):
                self.i += 4
                break
            if self.s[self.i] == '(':
                self.i += 1
            patterns, pattern_nested = [], []
            while True:
                self._blanks(newlines=True)
                if self.i >= self.n:
                    self._fail('unclosed case pattern')
                char = self.s[self.i]
                if char == ')':
                    self.i += 1
                    break
                if char == '|':
                    self.i += 1
                    continue
                if char in '\n;&(<>':
                    self._fail('malformed case pattern')
                patterns.append(self._word(pattern_nested))
            items.append(CaseClause(tuple(patterns), (), (), terminator,
                                    tuple(pattern_nested), ()))
            items.extend(self.script('case'))
            terminator = self.case_terminator
        trailing, more, trailing_heredocs = self._trailing_redirects()
        items.append(CaseEnd((), trailing, terminator, more, trailing_heredocs))
        return items

    # ── words ─────────────────────────────────────────────────────────

    def _word(self, nested, dbracket=False):
        start = self.i
        out = []
        s = self.s
        while self.i < self.n:
            char = s[self.i]
            if char in ' \t\n':
                break
            if char == ';' or (not dbracket and char in '&|)'):
                break
            if char in '<>' and not dbracket:
                if s.startswith('(', self.i + 1):
                    begin = self.i
                    self.i += 2
                    nested.append(self._nested_script())
                    out.append(s[begin:self.i])
                    continue
                break
            if char == '(' and not dbracket:
                so_far = ''.join(out)
                if _ARRAY_ASSIGNMENT.match(so_far) or so_far[-1:] in ('@', '!', '*', '+', '?'):
                    out.append(self._paren_words(nested))
                    continue
                break
            if char == '\\':
                if self.i + 1 >= self.n:
                    out.append(char)  # a final backslash stays literal
                    self.i += 1
                    continue
                if s[self.i + 1] == '\n':
                    self.i += 2  # line continuation
                    continue
                out.append(s[self.i:self.i + 2])
                self.i += 2
                continue
            if char == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    self._fail('unclosed single quote')
                out.append(s[self.i:end + 1])
                self.i = end + 1
                continue
            if char == '"':
                out.append(self._double_quoted(nested))
                continue
            if char == '$':
                out.append(self._dollar(nested))
                continue
            if char == '`':
                out.append(self._backtick(nested))
                continue
            out.append(char)
            self.i += 1
        if self.i == start:
            self._fail('expected a word')
        return ''.join(out)

    def _paren_words(self, nested):
        """``name=( … )`` / extglob ``@( … )``: words up to the matching ``)``."""
        begin = self.i
        self.i += 1
        while True:
            self._blanks(newlines=True, comments=True)
            if self.i >= self.n:
                self._fail('unclosed (', begin)
            char = self.s[self.i]
            if char == ')':
                self.i += 1
                return self.s[begin:self.i]
            if char == '|':
                self.i += 1
                continue
            if char == '(':
                self._paren_words(nested)
                continue
            if char in ';&<>':
                self._fail('unexpected operator in ( … )')
            self._word(nested)

    def _double_quoted(self, nested):
        begin = self.i
        self.i += 1
        out = ['"']
        while self.i < self.n:
            char = self.s[self.i]
            if char == '"':
                self.i += 1
                out.append('"')
                return ''.join(out)
            if char == '\\':
                if self.s.startswith('\n', self.i + 1):
                    self.i += 2
                    continue
                out.append(self.s[self.i:self.i + 2])
                self.i += 2
                continue
            if char == '$':
                out.append(self._dollar(nested, in_double=True))
                continue
            if char == '`':
                out.append(self._backtick(nested, in_double=True))
                continue
            out.append(char)
            self.i += 1
        self._fail('unclosed double quote', begin)

    def _dollar(self, nested, in_double=False):
        s = self.s
        begin = self.i
        if s.startswith('$((', begin):
            text = self._arithmetic(nested, 3)
            if text is not None:
                return text
            self.i = begin  # `$( (…) …)`: a substitution opening with a subshell
        if s.startswith('$(', begin):
            self.i += 2
            nested.append(self._nested_script())
            return s[begin:self.i]
        if s.startswith('${', begin):
            return self._parameter(nested, in_double)
        if s.startswith("$'", begin) and not in_double:
            index = begin + 2
            while index < self.n:
                if s[index] == '\\':
                    index += 2
                    continue
                if s[index] == "'":
                    # POSIX sh (dash runs run_command here) reads `$` and a
                    # single-quoted string ending at the first quote.
                    if self.opaque is not None and s.find("'", begin + 2) != index:
                        self._fail("$'…' ends differently in bash and POSIX sh", begin)
                    self.i = index + 1
                    return s[begin:self.i]
                index += 1
            self._fail("unclosed $'", begin)
        if s.startswith('$"', begin) and not in_double:
            self.i += 1
            return '$' + self._double_quoted(nested)
        self.i += 1
        return '$'

    def _parameter(self, nested, in_double):
        begin = self.i
        self.i += 2
        while self.i < self.n:
            char = self.s[self.i]
            if char == '}':
                self.i += 1
                return self.s[begin:self.i]
            if char == '\\':
                self.i += 2
                continue
            if char == "'" and not in_double:
                end = self.s.find("'", self.i + 1)
                if end < 0:
                    self._fail('unclosed single quote')
                self.i = end + 1
                continue
            if char == '"':
                self._double_quoted(nested)
                continue
            if char == '$':
                self._dollar(nested, in_double=True)
                continue
            if char == '`':
                self._backtick(nested, in_double)
                continue
            self.i += 1
        self._fail('unclosed ${', begin)

    def _arithmetic(self, nested, opener):
        """Text of ``$((…))``/``((…))`` from ``self.i``, or None (not arithmetic)."""
        begin = self.i
        mark = len(nested)
        pending = len(self.pending)
        recorded = (len(self.command_ranges) if self.command_ranges is not None else 0,
                    len(self.opaque) if self.opaque is not None else 0)
        self.i += opener
        depth = 0
        try:
            while self.i < self.n:
                char = self.s[self.i]
                if char == '(':
                    depth += 1
                    self.i += 1
                elif char == ')':
                    if depth:
                        depth -= 1
                        self.i += 1
                    elif self.s.startswith('))', self.i):
                        inner_text = self.s[begin + opener:self.i]
                        self.i += 2
                        # bash evaluates the text as arithmetic; a POSIX shell
                        # reads $(( … ))'s twin `( ( … ) )` as nested subshells
                        # and RUNS its words (`(( rm -rf / ))` deletes under
                        # dash). Judge both readings.
                        try:
                            nested.append(_Lexer(inner_text, self.depth + 1,
                                                 self.base + begin + opener).script())
                        except ShellStructureUnknown:
                            pass
                        return self.s[begin:self.i]
                    else:
                        raise _NotArithmetic
                elif char == '$':
                    self._dollar(nested, in_double=True)
                elif char == '`':
                    self._backtick(nested)
                elif char == '"':
                    self._double_quoted(nested)
                elif char == "'":
                    end = self.s.find("'", self.i + 1)
                    if end < 0:
                        raise _NotArithmetic
                    self.i = end + 1
                elif char == '\\':
                    self.i += 2
                else:
                    self.i += 1
            raise _NotArithmetic
        except (_NotArithmetic, ShellStructureUnknown):
            del nested[mark:]
            del self.pending[pending:]
            if self.command_ranges is not None:
                del self.command_ranges[recorded[0]:]
            if self.opaque is not None:
                del self.opaque[recorded[1]:]
            self.i = begin
            return None

    def _backtick(self, nested, in_double=False):
        begin = self.i
        index = begin + 1
        body = []
        while index < self.n:
            char = self.s[index]
            if char == '\\' and index + 1 < self.n:
                follower = self.s[index + 1]
                if follower in '$`\\' or (in_double and follower == '"'):
                    body.append(follower)
                    index += 2
                    continue
                if follower == '\n':
                    index += 2
                    continue
                body.append(char)
                index += 1
                continue
            if char == '`':
                self.i = index + 1
                inner = _Lexer(''.join(body), self.depth + 1, self.base + begin + 1)
                nested.append(inner.script())
                if self.opaque is not None:
                    # Escaped source: its bytes are not the commands it runs.
                    self.opaque.append((self.base + begin + 1, self.base + index))
                return self.s[begin:self.i]
            body.append(char)
            index += 1
        self._fail('unclosed backtick', begin)

    def _nested_script(self):
        """Body of ``$(``/``<(``/``>(``/``(`` starting at ``self.i``, through ``)``."""
        if self.depth >= _MAX_DEPTH:
            self._fail('nesting too deep')
        outer = self.pending
        self.pending = []
        self.depth += 1
        try:
            body = self.script(')')
        finally:
            self.depth -= 1
            inner, self.pending = self.pending, outer
        # A heredoc opened inside a substitution that closed on the same line
        # is read at the enclosing line's newline.
        self.pending.extend(inner)
        return body

    # ── redirections and heredocs ────────────────────────────────────

    def _redirect(self, nested, heredocs):
        """Parse a redirection at ``self.i``: the (op, target) pair, ``()``
        for a heredoc, or None when no redirection starts here."""
        match = _REDIRECT.match(self.s, self.i)
        if match is None:
            return None
        operator = match.group(0)
        if operator in ('<', '>') and self.s.startswith('(', match.end()):
            return None  # process substitution word
        begin = self.i
        self.i = match.end()
        self._blanks()
        if self.i >= self.n or self.s[self.i] in '\n;&|)':
            self._fail('redirection without a target')
        bare = operator.lstrip('0123456789')
        if bare.startswith('{'):
            bare = bare[bare.index('}') + 1:]
        if bare in ('<<', '<<-'):
            delimiter, quoted = _heredoc_delimiter(self._word(nested))
            if not delimiter:
                self._fail('empty heredoc delimiter')
            heredoc = Heredoc(delimiter, quoted, bare == '<<-', self.base + begin)
            self.pending.append(heredoc)
            heredocs.append(heredoc)
            return ()
        return operator, self._word(nested)

    def _newline(self):
        self.i += 1
        pending, self.pending = self.pending, []
        for heredoc in pending:
            self._heredoc_body(heredoc)

    def _heredoc_body(self, heredoc):
        start = self.i
        while self.i < self.n:
            end = self.s.find('\n', self.i)
            line_end = self.n if end < 0 else end
            line = self.s[self.i:line_end]
            if (line.lstrip('\t') if heredoc.strip_tabs else line) == heredoc.delimiter:
                heredoc.body = self.s[start:self.i]
                self.i = self.n if end < 0 else end + 1
                break
            self.i = self.n if end < 0 else end + 1
        else:
            heredoc.body = self.s[start:]  # delimited by end of input
        if self.opaque is not None:
            heredoc.body_start = self.base + start
            heredoc.end = self.base + self.i
        if not heredoc.quoted and heredoc.body:
            heredoc.nested = _Lexer(heredoc.body, self.depth + 1,
                                    self.base + start).expanding_body()

    def _heredocs_at_eof(self):
        self.pending = []  # no newline follows: the bodies are empty

    def expanding_body(self):
        """Scripts run by expanding a heredoc body: quotes are literal there."""
        nested = []
        while self.i < self.n:
            char = self.s[self.i]
            if char == '\\':
                self.i += 2
            elif char == '$':
                self._dollar(nested, in_double=True)
            elif char == '`':
                self._backtick(nested)
            else:
                self.i += 1
        return tuple(nested)

    # ── lexical helpers ───────────────────────────────────────────────

    def _blanks(self, newlines=False, comments=False):
        while self.i < self.n:
            char = self.s[self.i]
            if char in ' \t':
                self.i += 1
            elif char == '\\' and self.s.startswith('\n', self.i + 1):
                self.i += 2
            elif newlines and char == '\n':
                self._newline()
            elif comments and char == '#':
                self._comment()
            else:
                return

    def _comment(self):
        end = self.s.find('\n', self.i)
        end = self.n if end < 0 else end
        if self.opaque is not None:
            self.opaque.append((self.base + self.i, self.base + end))
        self.i = end

    def _at_word(self, word):
        end = self.i + len(word)
        return (self.s.startswith(word, self.i)
                and (end >= self.n or self.s[end] in _WORD_END))

    def _function_parens(self):
        index = self.i + 1
        while index < self.n and self.s[index] in ' \t':
            index += 1
        if index < self.n and self.s[index] == ')':
            self.i = index + 1
            return True
        return False
