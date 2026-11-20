"""One shared storage exchange for a model round's boundary reads.

Responsibility: name, at the start of each round's context preparation, the
reads that the round's boundary owners make anyway -- the committed-steer Turn
head (``committed_steers``), the execution requirements (attempt, Turn head and
requirements record, ``acceptance.turns``), the working-fact aggregate
(``memory.working_facts``) and the project-message inbox's conversation scope
(``conversations.project_messages.identity``) -- so that they cost ONE
pipelined ``query_many`` instead of six sequential round trips (the Turn head they share is read
once). The first of them to read (the steer check, on the Turn thread, where
the head was read before) sends the exchange; the requirement and fact reads
on the provider executor then find their answers. Each owner still decides
what its read means: it passes the round's ``PrefetchedReads`` to its own
read path, which answers exactly the requests named here and reads anything
else (a continued Turn's source record, a retry after a transient failure)
live.

Sending: the exchange is a storage round trip, so the Turn thread puts it on
the wire (``PrefetchedReads.send_now``: the send half of one pipelined
exchange on a pooled session) as soon as its rows can no longer change before
the boundary reads them: at the previous round's ``ready`` end (its tools have
settled; the round's deferred events persist meanwhile), or at the round's
preparation when there was none (the first round, a ``tools_in_flight`` round
end). The authority answers while the Turn thread runs the round close, the
next round's tool assembly, input translation and hygiene; the steer check
then only reads the answers. A hand-off to an executor thread was measured
first and did not help: under the GIL a worker waits for the busy Turn thread's
switch interval before it can send.
A steer committed between the send and the boundary is no different from one
committed just after the boundary read: the next boundary, a blocking wait or
the finalizer (which reads live) delivers it.

Freshness: one round. The exchange is dropped once the round's prompt is
admitted (or the loop ends); a later reader of the same rows (the finalizer's
steer check) reads live.

Entry: prefetch_round_reads, begin_round_reads, round_reads, end_round_reads.
Dependencies: lib.storage.port, lib.turn_lifecycle, lib.acceptance.turns,
lib.memory.working_facts.repository, lib.conversations.repository.
"""
from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

ROUND_READS_KEY = '_roundReads'
# Set by prefetch_round_reads: the next begin_round_reads keeps that exchange.
_PREFETCHED_KEY = '_roundReadsPrefetched'


def _requests(task: dict[str, Any]) -> list:
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.conversation_delivery import completion_conversation_id
    from lib.tasks_pkg.manager import task_user_id
    from lib.tasks_pkg.requirement_source import requirement_source
    requests = []
    if (task.get('convId') and not task.get('_parentTaskId')
            and not task.get('_transientRuntime')):
        # The project-message inbox's conversation scope
        # (``project_messages.identity``).
        from lib.conversations.repository import conversation_read_request
        try:
            requests.append(conversation_read_request(
                str(task['convId']), user_id=task_user_id(task), include_messages=False))
        except ValueError:
            pass  # identity() refuses the same task itself, reading nothing.
    conversation_id = completion_conversation_id(task)
    if conversation_id and task.get('_turnId') and task.get('_attemptId'):
        from lib.turn_lifecycle import turn_read_request
        requests.append(turn_read_request(conversation_id, task['_turnId'],
                                          user_id=task_user_id(task), include_projection=False))
    if not task.get('_principalContext'):
        return requests
    principal = PrincipalContext.from_payload(task['_principalContext'])
    source = requirement_source(task)
    if source is not None:
        from lib.acceptance.turns import execution_requirements_read_requests
        conversation, turn, attempt = source.identity[:3]
        requests += execution_requirements_read_requests(conversation, turn, attempt,
                                                         principal=principal)
    if task.get('convId') and task.get('_userId') and not task.get('_transientRuntime'):
        from lib.memory.working_facts.repository import facts_read_request
        requests.append(facts_read_request(str(task['convId']), principal))
    return requests


def _send(task: dict[str, Any]):
    try:
        requests = _requests(task)
        if not requests:
            return None
        from lib.storage import get_storage_client
        from lib.storage.port import PrefetchedReads
        reads = PrefetchedReads(get_storage_client(), requests)
        reads.send_now()
    except Exception:
        # Each owner reads live and reports its own failure.
        logger.debug('[Task %s] round reads not issued', task.get('id'), exc_info=True)
        return None
    task[ROUND_READS_KEY] = reads
    return reads


def prefetch_round_reads(task: dict[str, Any]):
    """Send the next round's boundary reads now, at a ``ready`` round end."""
    end_round_reads(task)
    reads = _send(task)
    if reads is not None:
        task[_PREFETCHED_KEY] = reads
    return reads


def begin_round_reads(task: dict[str, Any]):
    """This round's boundary reads: the prefetched exchange, else one sent now."""
    prefetched = task.pop(_PREFETCHED_KEY, None)
    if prefetched is not None and task.get(ROUND_READS_KEY) is prefetched:
        return prefetched
    end_round_reads(task)
    return _send(task)


def round_reads(task: dict[str, Any]):
    """This round's ``PrefetchedReads``, or None (owners then read live)."""
    return task.get(ROUND_READS_KEY)


def end_round_reads(task: dict[str, Any]) -> None:
    reads = task.pop(ROUND_READS_KEY, None)
    task.pop(_PREFETCHED_KEY, None)
    if reads is not None:
        # The last round end's exchange has no next round: read and drop its
        # answers (already sent long ago) so its pooled session stays usable.
        reads.close()
