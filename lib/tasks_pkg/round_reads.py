"""One shared storage exchange for a model round's boundary reads.

Responsibility: name, at the start of each round's context preparation, the
reads that the round's boundary owners make anyway -- the committed-steer Turn
head (``committed_steers``), the execution requirements (attempt, Turn head and
requirements record, ``acceptance.turns``) and the working-fact aggregate
(``memory.working_facts``) -- so that they cost ONE pipelined ``query_many``
instead of five sequential round trips (the Turn head they share is read
once). The first of them to read (the steer check, on the Turn thread, where
the head was read before) sends the exchange; the requirement and fact reads
on the provider executor then find their answers. Each owner still decides
what its read means: it passes the round's ``PrefetchedReads`` to its own
read path, which answers exactly the requests named here and reads anything
else (a continued Turn's source record, a retry after a transient failure)
live.

Freshness: one round. The exchange is sent at the round's first boundary
read and dropped once the round's prompt is admitted; a later reader of the
same rows (the finalizer's steer check) reads live.

Entry: begin_round_reads, round_reads, end_round_reads.
Dependencies: lib.storage.port, lib.turn_lifecycle, lib.acceptance.turns,
lib.memory.working_facts.repository.
"""
from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

ROUND_READS_KEY = '_roundReads'


def _requests(task: dict[str, Any]) -> list:
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.conversation_delivery import completion_conversation_id
    from lib.tasks_pkg.manager import task_user_id
    from lib.tasks_pkg.requirement_source import requirement_source
    from lib.turn_lifecycle import turn_read_request
    requests = []
    conversation_id = completion_conversation_id(task)
    if conversation_id and task.get('_turnId') and task.get('_attemptId'):
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


def begin_round_reads(task: dict[str, Any]):
    """Issue this round's boundary reads in one exchange; the previous round's are dropped."""
    task.pop(ROUND_READS_KEY, None)
    from lib.storage import get_storage_client
    from lib.storage.port import PrefetchedReads
    try:
        requests = _requests(task)
        if not requests:
            return None
        reads = PrefetchedReads(get_storage_client(), requests)
    except Exception:
        # Each owner reads live and reports its own failure.
        logger.debug('[Task %s] round reads not issued', task.get('id'), exc_info=True)
        return None
    task[ROUND_READS_KEY] = reads
    return reads


def round_reads(task: dict[str, Any]):
    """This round's ``PrefetchedReads``, or None (owners then read live)."""
    return task.get(ROUND_READS_KEY)


def end_round_reads(task: dict[str, Any]) -> None:
    task.pop(ROUND_READS_KEY, None)
