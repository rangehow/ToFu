"""A lesson's trigger: the situation in which it applies, written beside its outcome.

Lessons are written after the fact ("X was fixed by Y"), but they are needed in
the situation that precedes the fix: a request to do some kind of work, a
command, a file, a symptom or an error text. The writer records that situation
as ``trigger`` (``create_memory``/``update_memory``); retrieval scores it with
the name, description and tags through the one BM25 document
(``relevance._tokenize._memory_document_text``) and the prefetch confidence gate
(``prefetch._run._metadata``), so no selection rule changes.

Lessons written before the field existed get one by a single backfill pass
(``scripts/backfill_memory_triggers.py``): a model reads only the lesson and
writes its trigger with :data:`TRIGGER_BACKFILL_SYSTEM`. Entry points:
:func:`trigger_backfill_input`, :func:`parse_trigger`.
"""

from __future__ import annotations

import json
import re

from lib.memory.contracts import MEMORY_TRIGGER_MAX_CHARS, normalize_memory_trigger

_FRONTMATTER_RE = re.compile(r'\A---\n(.*?)\n---', re.DOTALL)

TRIGGER_BACKFILL_SYSTEM = f"""You write the TRIGGER of one lesson from an AI coding agent's memory.

The trigger describes the situation in which an agent needs this lesson, the way that situation
looks BEFORE the agent knows the lesson. Memory is searched by keywords from the task request the
agent receives, so the trigger must use the words such a request, or the work leading up to the
problem, would contain:
- the kind of work that leads into it (what someone asks for, e.g. "change the studio page UI and
  check it in a browser", "certify the storage engine", "submit a training job to Hope");
- the specific components, files, paths, commands, tools or services involved;
- the symptoms or exact error text an agent would observe.

Do not state the cause, the fix or the rule: those are already in the lesson. Name concrete things;
leave out generic words that fit any lesson (agent, task, issue, problem, fix, bug). Write in the
language(s) the lesson itself uses, keeping identifiers verbatim.

One line, at most {MEMORY_TRIGGER_MAX_CHARS} characters. Reply with one JSON object only:
{{"trigger": "<the trigger>"}}"""


def trigger_backfill_input(lesson_markdown: str) -> str:
    """The backfill model's whole input: the lesson file, nothing else."""
    return 'LESSON:\n' + lesson_markdown


def parse_trigger(text: str) -> str:
    """The trigger in a backfill reply; ``ValueError`` when it is missing or out of contract."""
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('no JSON object')
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict) or not isinstance(value.get('trigger'), str):
        raise ValueError('trigger must be a string')
    trigger = normalize_memory_trigger(' '.join(value['trigger'].split()))
    if not trigger:
        raise ValueError('empty trigger')
    return trigger


def has_trigger_line(lesson_markdown: str) -> bool:
    match = _FRONTMATTER_RE.match(lesson_markdown)
    return bool(match) and any(line.startswith('trigger:')
                               for line in match.group(1).splitlines())


def with_trigger_line(lesson_markdown: str, trigger: str) -> str:
    """The lesson file with ``trigger: <line>`` appended to its frontmatter, every other byte kept.

    The backfill edits the file text instead of rewriting the record, so
    hand-written frontmatter keys the storage writer does not emit survive.
    A file without frontmatter, or with a trigger already, is returned as is.
    """
    match = _FRONTMATTER_RE.match(lesson_markdown)
    if match is None or has_trigger_line(lesson_markdown):
        return lesson_markdown
    trigger = normalize_memory_trigger(trigger)
    return (lesson_markdown[:match.end(1)] + f'\ntrigger: {trigger}'
            + lesson_markdown[match.end(1):])


__all__ = ['TRIGGER_BACKFILL_SYSTEM', 'trigger_backfill_input', 'parse_trigger',
           'has_trigger_line', 'with_trigger_line']
