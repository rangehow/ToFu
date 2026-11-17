"""Office team facts: one snapshot plus ordered office.fact deltas (docs/PROJECT_COORDINATION.md).

Executable specification of the busy fact and the delivery contract it rides on:
busy only while an attempt runs, frames only for a real change and never for a
tool call, contiguous ordering, resume after a reconnect and after a server
restart, and nothing for a non-owner. The feed is driven through its broker
scope listener exactly as the commit watch drives it in production.
"""
from __future__ import annotations

import json
import queue
import threading

import pytest

from lib.router import office_facts
from lib.router.office_facts import CHANNEL, TASK, OfficeFacts

pytestmark = pytest.mark.unit

OWNER, OTHER = 1, 2
FACT_KINDS = {'busy', 'report', 'document', 'decision', 'board', 'alarm'}


class FakePorts:
    """The production port surface over in-memory authorities."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running_by_owner: dict[int, dict[str, str]] = {}
        self.placements: dict[str, dict] = {}
        self.placement_failures: set[str] = set()
        self.subscribers: set[int] = set()
        self.frames: queue.Queue = queue.Queue()
        self.running_reads = 0
        # Report authorities: bound leads, report heads, mailboxes, attempts and assignments.
        self.leads_by_owner: dict[int, list[dict]] = {}
        self.report_heads: dict[str, dict] = {}
        self.mailboxes: dict[str, list[dict]] = {}
        self.mailbox_versions: dict[str, int] = {}
        self.results: dict[str, dict] = {}
        self.reviews: dict[str, int] = {}
        self.newest_page: dict[str, list[dict]] = {}
        self.history_reads = 0
        self.changed_reads: list = []
        self.report_reads = 0
        self.review_decisions: dict[str, str] = {}
        self.accepted_page: dict[str, list[dict]] = {}
        self.questions_by_lead: dict[str, list[dict]] = {}
        self.question_reads = 0
        self.boards: dict[str, int] = {}
        self.open_alarms: list[dict] = []
        self.attempt_status_by_id: dict[str, str] = {}
        self.attempt_reads: list[str] = []
        self.updated: dict[str, int] = {}
        self.clock = 1000

    def running(self, owner):
        with self.lock:
            self.running_reads += 1
            return dict(self.running_by_owner.get(owner, {}))

    def placement(self, owner, conversation_id, task_id):
        if task_id in self.placement_failures:
            raise RuntimeError('storage unavailable')
        return dict(self.placements.get(task_id, {}))

    def leads(self, owner):
        return [dict(lead) for lead in self.leads_by_owner.get(owner, [])]

    def report(self, owner, lead):
        self.report_reads += 1
        head = self.report_heads.get(lead['conversationId'])
        return dict(head) if head else None

    def report_messages(self, owner, lead, known_version):
        version = self.mailbox_versions.get(lead['conversationId'], 0)
        if version == known_version:
            return version, None
        return version, [dict(message) for message in self.mailboxes.get(lead['conversationId'], [])
                         if message['kind'] == 'report' and message['state'] == 'submitted']

    def result(self, owner, placement):
        if self.attempt_status_by_id.get(placement['attemptId']) != 'completed':
            return None
        result = self.results.get(placement['attemptId'])
        if result is None or self.reviews.get(result['assignmentId'], -1) >= result['revision']:
            return None
        return dict(result)

    def review(self, owner, fact):
        revision = self.reviews.get(fact['assignmentId'], -1)
        if revision < fact['revision']:
            return None
        return {'decision': self.review_decisions.get(fact['assignmentId'], 'accept'), 'resultRevision': revision}

    def newest_results(self, owner, lead, running):
        self.history_reads += 1
        unreviewed = [dict(result) for result in self.newest_page.get(lead['conversationId'], [])
                      if self.reviews.get(result['assignmentId'], -1) < result['revision']
                      and result['attemptId'] not in running]
        return unreviewed, [dict(result) for result in self.accepted_page.get(lead['conversationId'], [])]

    def questions(self, owner, lead):
        self.question_reads += 1
        return [{name: question[name] for name in ('turnId', 'ordinal', 'guidanceId', 'blocking')}
                for question in self.questions_by_lead.get(lead['conversationId'], []) if not question.get('answered')]

    def board(self, owner, lead):
        return self.boards.get(lead['conversationId'])

    def attempt_status(self, owner, attempt_id):
        self.attempt_reads.append(attempt_id)
        return self.attempt_status_by_id.get(attempt_id, 'running')

    def changed_conversations(self, owner, since_ms):
        self.changed_reads.append(since_ms)
        rows = sorted(((conversation, at) for conversation, at in self.updated.items()
                       if since_ms is None or at >= since_ms), key=lambda row: -row[1])
        return (rows[:1] if since_ms is None else rows), True

    def touch(self, conversation):
        """A commit: the conversation's updatedAt moves (the catalog recency index)."""
        self.clock += 1
        self.updated[conversation] = self.clock

    def alarms(self, owner):
        return [dict(alarm) for alarm in self.open_alarms]

    def publish(self, owner, frame):
        self.frames.put((owner, frame))

    def subscribed(self, owner):
        return owner in self.subscribers

    def drain(self):
        frames = []
        while True:
            try:
                frames.append(self.frames.get_nowait())
            except queue.Empty:
                return frames


@pytest.fixture
def ports():
    return FakePorts()


@pytest.fixture
def feed(ports):
    feed = OfficeFacts(ports)
    yield feed
    feed.close()


def commit(feed, *conversations, owner=OWNER):
    """One commit-watch batch, then wait for the pass it causes."""
    feed.observe_scopes([{'userId': owner, 'conversationId': conversation} for conversation in conversations])
    assert feed.wait_idle(10)


def frames_of(ports, owner=OWNER):
    return [frame for target, frame in ports.drain() if target == owner]


class Reader:
    """The client contract: snapshot, then frames with its streamId and seq == last + 1."""

    def __init__(self, feed, owner=OWNER):
        self.feed, self.owner = feed, owner
        self.resnapshot()

    def resnapshot(self):
        snapshot = self.feed.snapshot(self.owner)
        self.stream, self.seq = snapshot['streamId'], snapshot['seq']
        self.facts = {fact['key']: fact for fact in snapshot['facts']}
        self.snapshots = getattr(self, 'snapshots', 0) + 1

    def apply(self, frame):
        if frame['streamId'] != self.stream or frame['seq'] != self.seq + 1:
            self.resnapshot()
            return
        self.seq = frame['seq']
        fact = {key: value for key, value in frame.items()
                if key not in ('type', 'userId', 'streamId', 'seq', 'state')}
        if frame['state'] == 'set':
            self.facts[fact['key']] = fact
        else:
            self.facts.pop(fact['key'], None)


def test_busy_is_set_only_while_an_attempt_runs_and_cleared_when_it_stops(feed, ports):
    ports.subscribers.add(OWNER)
    ports.placements['conversation-attempt:a1'] = {
        'attemptId': 'a1', 'projectPath': '/p', 'assignmentId': 'as1', 'coordinatorId': 'lead'}
    snapshot = feed.snapshot(OWNER)
    assert snapshot['facts'] == [] and snapshot['seq'] == 0
    assert snapshot['coverage']['busy'] == 'complete'
    assert frames_of(ports) == [], 'the baseline is the snapshot, never frames'

    ports.running_by_owner[OWNER] = {'w1': 'conversation-attempt:a1'}
    commit(feed, 'w1')
    [started] = frames_of(ports)
    assert started == {
        'type': 'office.fact', 'userId': OWNER, 'streamId': snapshot['streamId'], 'seq': 1,
        'state': 'set', 'fact': 'busy', 'key': 'busy:w1', 'conversationId': 'w1', 'source': 'attempt',
        'attemptId': 'a1', 'projectPath': '/p', 'assignmentId': 'as1', 'coordinatorId': 'lead'}

    ports.running_by_owner[OWNER] = {}
    commit(feed, 'w1')
    [stopped] = frames_of(ports)
    assert (stopped['seq'], stopped['state'], stopped['key']) == (2, 'cleared', 'busy:w1')
    assert feed.snapshot(OWNER)['facts'] == []


def test_running_attempts_exclude_queued_suspended_and_pending_work(monkeypatch):
    from lib.tasks_pkg.manager import _registry
    rows = [
        {'taskId': 'conversation-attempt:q', 'userId': OWNER, 'taskKind': 'conversation-attempt',
         'conversationId': 'queued-conv', 'status': 'queued', 'createdAtMs': 1},
        {'taskId': 'conversation-attempt:s', 'userId': OWNER, 'taskKind': 'conversation-attempt',
         'conversationId': 'suspended-conv', 'status': 'suspended', 'createdAtMs': 2},
        {'taskId': 'conversation-attempt:r2', 'userId': OWNER, 'taskKind': 'conversation-attempt',
         'conversationId': 'running-conv', 'status': 'running', 'createdAtMs': 20},
        {'taskId': 'conversation-attempt:r1', 'userId': OWNER, 'taskKind': 'conversation-attempt',
         'conversationId': 'running-conv', 'status': 'running', 'createdAtMs': 10},
    ]
    monkeypatch.setattr('lib.durable_worker.activity.list_active_conversation_tasks',
                        lambda *, user_id=None: list(rows))
    monkeypatch.setattr(_registry.chat_task_runtime, 'snapshot', lambda: [
        {'id': 'local-pending', 'convId': 'local-pending-conv', 'status': 'pending', 'user_id': OWNER},
        {'id': 'local-running', 'convId': 'local-conv', 'status': 'running', 'user_id': OWNER},
        {'id': 'foreign', 'convId': 'foreign-conv', 'status': 'running', 'user_id': OTHER},
    ])
    monkeypatch.setattr(_registry, 'task_user_id', lambda task: task.get('user_id'))
    monkeypatch.setattr(_registry, 'is_carrier_task', lambda task: False)
    monkeypatch.setattr('lib.tasks_pkg.liveness.is_wedged', lambda task, **_: False)
    assert _registry.running_conversation_tasks(user_id=OWNER) == {
        'running-conv': 'conversation-attempt:r1', 'local-conv': 'local-running'}


def test_commits_without_a_fact_change_publish_nothing(feed, ports):
    """A Worker's tool rounds commit constantly; only a running-set change is a fact."""
    ports.subscribers.add(OWNER)
    ports.running_by_owner[OWNER] = {'w1': 'conversation-attempt:a1'}
    feed.snapshot(OWNER)
    reads = ports.running_reads
    for _ in range(5):
        commit(feed, 'w1')
    assert ports.running_reads > reads, 'each commit re-reads the running set'
    assert frames_of(ports) == []


def test_frames_carry_only_declared_fact_fields_never_tool_calls(feed, ports):
    from lib.agent_core.events import get_push_frame_spec
    spec = get_push_frame_spec('office.fact')
    assert (spec.channel, spec.task) == (CHANNEL, TASK)
    declared = {'type'} | {field.name for field in spec.schema}
    ports.subscribers.add(OWNER)
    feed.snapshot(OWNER)
    ports.placements['conversation-attempt:a1'] = {'attemptId': 'a1', 'projectPath': '/p'}
    ports.running_by_owner[OWNER] = {'w1': 'conversation-attempt:a1', 'w2': 'conversation-attempt:a2'}
    commit(feed, 'w1', 'w2')
    ports.running_by_owner[OWNER] = {}
    commit(feed, 'w1', 'w2')
    frames = frames_of(ports)
    assert len(frames) == 4
    for frame in frames:
        assert set(frame) <= declared
        assert frame['fact'] in FACT_KINDS
        assert not any('tool' in key.lower() for key in frame)


def test_frames_are_contiguous_and_ordered_within_one_stream(feed, ports):
    ports.subscribers.add(OWNER)
    stream = feed.snapshot(OWNER)['streamId']
    for running in ({'a': 'conversation-attempt:1'},
                    {'a': 'conversation-attempt:1', 'b': 'conversation-attempt:2'},
                    {'b': 'conversation-attempt:2'},
                    {'b': 'conversation-attempt:3'},
                    {}):
        ports.running_by_owner[OWNER] = running
        commit(feed, 'a', 'b')
    frames = frames_of(ports)
    assert [frame['seq'] for frame in frames] == list(range(1, len(frames) + 1))
    assert {frame['streamId'] for frame in frames} == {stream}
    assert [(frame['state'], frame['key']) for frame in frames] == [
        ('set', 'busy:a'), ('set', 'busy:b'), ('cleared', 'busy:a'),
        ('set', 'busy:b'), ('cleared', 'busy:b')]
    assert feed.snapshot(OWNER)['seq'] == frames[-1]['seq']


def test_a_reconnected_reader_resumes_from_the_snapshot_without_losing_a_change(feed, ports):
    ports.subscribers.add(OWNER)
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1'}
    reader = Reader(feed)
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1', 'b': 'conversation-attempt:2'}
    commit(feed, 'b')
    for _owner, frame in ports.drain():
        reader.apply(frame)
    # The socket drops: frames published meanwhile never reach this reader.
    ports.running_by_owner[OWNER] = {'b': 'conversation-attempt:2', 'c': 'conversation-attempt:3'}
    commit(feed, 'a', 'c')
    lost = ports.drain()
    assert lost
    reader.resnapshot()  # the reconnect rule
    assert set(reader.facts) == {'busy:b', 'busy:c'}
    ports.running_by_owner[OWNER] = {'c': 'conversation-attempt:3'}
    commit(feed, 'b')
    for _owner, frame in ports.drain():
        reader.apply(frame)
    assert set(reader.facts) == {'busy:c'} and reader.snapshots == 2


def test_a_reader_that_misses_a_frame_detects_the_gap_and_resnapshots(feed, ports):
    ports.subscribers.add(OWNER)
    reader = Reader(feed)
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1'}
    commit(feed, 'a')
    frames_of(ports)  # dropped in transit
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1', 'b': 'conversation-attempt:2'}
    commit(feed, 'b')
    for frame in frames_of(ports):
        reader.apply(frame)
    assert reader.snapshots == 2 and set(reader.facts) == {'busy:a', 'busy:b'}


def test_a_server_restart_gives_a_new_stream_rebuilt_from_the_authority(ports):
    ports.subscribers.add(OWNER)
    before = OfficeFacts(ports)
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1'}
    reader = Reader(before)
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1', 'b': 'conversation-attempt:2'}
    commit(before, 'b')
    for frame in frames_of(ports):
        reader.apply(frame)
    before.close()

    # Work changed while the server was down; the new process has no memory of it.
    ports.running_by_owner[OWNER] = {'c': 'conversation-attempt:3'}
    after = OfficeFacts(ports)
    try:
        ports.running_by_owner[OWNER] = {'c': 'conversation-attempt:3', 'd': 'conversation-attempt:4'}
        snapshot = after.snapshot(OWNER)
        assert snapshot['streamId'] != reader.stream
        assert snapshot['seq'] == 0 and frames_of(ports) == [], 'a rebuilt baseline publishes nothing'
        ports.running_by_owner[OWNER] = {'d': 'conversation-attempt:4'}
        commit(after, 'c')
        frame = frames_of(ports)[0]
        reader.feed = after  # the reconnected socket and the next read reach the new process
        reader.apply(frame)  # another streamId: the reader re-reads the snapshot
        assert reader.stream == snapshot['streamId']
        assert set(reader.facts) == {'busy:d'}
    finally:
        after.close()


def test_one_owners_facts_never_reach_another_owner(feed, ports):
    ports.subscribers.update({OWNER, OTHER})
    ports.running_by_owner[OWNER] = {'mine': 'conversation-attempt:1'}
    ports.running_by_owner[OTHER] = {}
    assert feed.snapshot(OTHER)['facts'] == []
    feed.snapshot(OWNER)
    ports.running_by_owner[OWNER] = {'mine': 'conversation-attempt:1', 'mine-too': 'conversation-attempt:2'}
    commit(feed, 'mine-too')
    commit(feed, 'theirs', owner=OTHER)
    published = ports.drain()
    assert published and {target for target, _frame in published} == {OWNER}
    assert all(frame['userId'] == OWNER for _target, frame in published)
    assert feed.snapshot(OTHER)['facts'] == []


def test_push_hub_delivers_office_frames_to_the_owners_sockets_only(monkeypatch):
    from lib.agent_core.push import PushClient, PushHub
    hub = PushHub()
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)

    class HubPorts(FakePorts):
        def publish(self, owner, frame):
            hub.push_event(CHANNEL, TASK, frame, user_id=owner)

        def subscribed(self, owner):
            return hub.has_subscriber(CHANNEL, owner)

    clients = {}
    for owner in (OWNER, OTHER):
        clients[owner] = PushClient(user_id=owner)
        assert hub.register(clients[owner])
        hub.subscribe(clients[owner], CHANNEL, TASK)
    assert hub.has_subscriber(CHANNEL, OWNER) and not hub.has_subscriber('notify', OWNER)
    ports = HubPorts()
    feed = OfficeFacts(ports)
    try:
        feed.snapshot(OWNER)
        ports.running_by_owner[OWNER] = {'w1': 'conversation-attempt:1'}
        commit(feed, 'w1')

        import asyncio

        async def drain(client):
            frames = []
            while not client._queue.empty():
                frames.append(await client.drain())
            return frames
        mine = asyncio.run(drain(clients[OWNER]))
        theirs = asyncio.run(drain(clients[OTHER]))
        assert [(frame['type'], frame['key'], frame['seq']) for frame in mine] == [('office.fact', 'busy:w1', 1)]
        assert theirs == []
    finally:
        feed.close()
        hub.stop()


def _park(feed, ports, owner=OWNER):
    """The office tab leaves: its socket unsubscribes, and a commit finds nobody watching.

    A snapshot read holds the owner through one more pass, so the second commit parks it at the latest.
    """
    ports.subscribers.discard(owner)
    commit(feed, 'w-any', owner=owner)
    commit(feed, 'w-any', owner=owner)
    stats = feed.stats()
    assert stats['owners'] >= 1, 'a baseline nobody watches is kept'
    assert stats['parked'] >= 1


def test_an_owner_nobody_watches_keeps_one_baseline_and_reads_nothing(feed, ports):
    _lead(ports)
    ports.running_by_owner[OWNER] = {'w1': 'conversation-attempt:1'}
    feed.snapshot(OWNER)
    commit(feed, 'w1')
    _park(feed, ports)
    reads = (ports.running_reads, ports.report_reads, ports.question_reads, ports.history_reads,
             len(ports.changed_reads))
    for conversation in ('w1', LEAD, 'w2'):
        commit(feed, conversation)
    feed.observe_scopes(None)
    feed.observe_publication('project', 'k', {'projectKey': PROJECT, '_ownerUserId': str(OWNER)})
    assert feed.wait_idle(10)
    assert (ports.running_reads, ports.report_reads, ports.question_reads, ports.history_reads,
            len(ports.changed_reads)) == reads, 'no read while nobody watches'
    assert frames_of(ports) == []
    assert {name: feed.stats()[name] for name in ('owners', 'parked')} == {'owners': 1, 'parked': 1}


def test_the_next_read_catches_a_parked_baseline_up_as_one_gap_pass_on_the_same_stream(feed, ports):
    """Re-entering the office is a watch gap from the kept watermark, never a cold rebuild."""
    _lead(ports)
    ports.leads_by_owner[OWNER].append({'conversationId': 'coordinator-quiet', 'projectPath': '/quiet',
                                        'revision': 1})
    ports.running_by_owner[OWNER] = {WORKER: 'conversation-attempt:a1'}
    ports.touch(WORKER)
    first = feed.snapshot(OWNER)
    commit(feed, WORKER)
    _park(feed, ports)
    watermark = max(ports.updated.values())
    reads = ports.report_reads
    # While nobody watched: the Worker finished, the lead reported, a question opened.
    ports.running_by_owner[OWNER] = {}
    ports.touch(WORKER)
    ports.report_heads[LEAD] = {'turnId': 't7', 'ordinal': 7, 'kind': 'delivery', 'title': 'x',
                                'createdAt': 1, 'unread': True}
    ports.questions_by_lead[LEAD] = [{'turnId': 't6', 'ordinal': 6, 'guidanceId': 'hg_1', 'blocking': True}]
    ports.touch(LEAD)
    ports.changed_reads.clear()

    snapshot = feed.snapshot(OWNER)
    assert snapshot['streamId'] == first['streamId'] and snapshot['seq'] > first['seq']
    assert ports.changed_reads == [watermark], 'one gap listing from the kept watermark'
    assert ports.report_reads == reads + 1, 'only the lead that committed is re-read'
    assert sorted(fact['key'] for fact in snapshot['facts']) == [
        f'question:{LEAD}:hg_1', f'report:{LEAD}:t7']
    assert feed.stats()['parked'] == 0

    fresh = OfficeFacts(ports)
    try:
        rebuilt = fresh.snapshot(OWNER)
    finally:
        fresh.close()
    assert snapshot['facts'] == rebuilt['facts'], 'the caught-up baseline equals a cold rebuild'


def test_a_parked_baseline_catches_up_what_no_commit_names(feed, ports):
    """A report marked read over HTTP, and a Worker whose whole attempt ran while nobody watched."""
    _lead(ports)
    ports.report_heads[LEAD] = {'turnId': 't1', 'ordinal': 1, 'kind': 'delivery', 'title': 'x',
                                'createdAt': 1, 'unread': True}
    feed.snapshot(OWNER)
    commit(feed, WORKER)
    _park(feed, ports)
    ports.report_heads[LEAD] = {**ports.report_heads[LEAD], 'unread': False}
    feed.observe_publication('project', 'k', {'projectKey': PROJECT, 'kind': 'coordinator_report',
                                              '_ownerUserId': str(OWNER)})
    ports.newest_page[LEAD] = [{'conversationId': 'worker-2', 'attemptId': 'a2', 'projectPath': PROJECT,
                                'assignmentId': 'as2', 'coordinatorId': LEAD, 'turnId': 't2', 'ordinal': 3,
                                'revision': 9}]
    ports.accepted_page[LEAD] = [{'conversationId': 'worker-0', 'projectPath': PROJECT, 'assignmentId': 'as0',
                                  'coordinatorId': LEAD, 'revision': 4}]
    history = ports.history_reads
    facts = {fact['key']: fact for fact in feed.snapshot(OWNER)['facts']}
    assert facts[f'report:{LEAD}:t1']['unread'] is False
    assert {'result:as2:9', 'document:as0:4'} <= set(facts)
    assert ports.history_reads == history + 1, "each lead's newest history page, once"


def test_a_catch_up_that_fails_stays_parked_and_the_next_read_retries_the_gap(feed, ports):
    class Interrupted(BaseException):
        """Raised past every source's own error handling."""
    _lead(ports)
    feed.snapshot(OWNER)
    commit(feed, WORKER)
    _park(feed, ports)
    ports.report_heads[LEAD] = {'turnId': 't3', 'ordinal': 3, 'kind': 'delivery', 'title': 'x',
                                'createdAt': 1, 'unread': True}
    ports.touch(LEAD)
    listing = ports.changed_conversations

    def interrupted(owner, since_ms):
        raise Interrupted()
    ports.changed_conversations = interrupted
    with pytest.raises(Interrupted):
        feed.snapshot(OWNER)
    assert feed.stats()['parked'] == 1
    ports.changed_conversations = listing
    assert f'report:{LEAD}:t3' in {fact['key'] for fact in feed.snapshot(OWNER)['facts']}


def test_a_suspect_kept_through_parking_ends_its_busy_fact_after_the_catch_up(feed, ports):
    """The attempt settled in a commit a watch-gap pass already took past the watermark; the tab left
    before its idle check, and its job is still listed running when the tab comes back."""
    _lead(ports)
    ports.placements['conversation-attempt:a1'] = {
        'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1', 'coordinatorId': LEAD}
    ports.running_by_owner[OWNER] = {WORKER: 'conversation-attempt:a1'}
    feed.snapshot(OWNER)
    commit(feed, WORKER)
    ports.attempt_status_by_id['a1'] = 'completed'
    ports.touch(WORKER)  # the terminal commit
    running = ports.running

    def tab_leaves_during_the_pass(owner):
        ports.running = running
        ports.subscribers.discard(OWNER)
        feed.observe_scopes([{'userId': OWNER, 'conversationId': 'w-any'}])
        return running(owner)
    ports.running = tab_leaves_during_the_pass
    ports.attempt_reads.clear()
    feed.observe_scopes(None)  # a watch gap: this pass moves the watermark past the terminal commit
    assert feed.wait_idle(10)
    assert feed.stats()['parked'] == 1
    assert ports.attempt_reads == [], 'no attempt check while nobody watches'
    assert f'busy:{WORKER}' in {fact['key'] for fact in feed.snapshot(OWNER)['facts']}
    assert feed.wait_idle(10)
    assert ports.attempt_reads == ['a1']
    assert f'busy:{WORKER}' not in {fact['key'] for fact in feed.snapshot(OWNER)['facts']}


def test_a_socket_that_subscribes_again_resumes_a_parked_baseline_with_frames(feed, ports):
    """A reconnected tab that kept its facts applies the catch-up frames in order on its stream."""
    _lead(ports)
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1'}
    reader = Reader(feed)
    _park(feed, ports)
    ports.running_by_owner[OWNER] = {'b': 'conversation-attempt:2'}
    ports.touch('b')
    ports.subscribers.add(OWNER)
    commit(feed, 'b')
    for _owner, frame in ports.drain():
        reader.apply(frame)
    assert reader.snapshots == 1, 'no frame was lost, so the reader never re-read the snapshot'
    assert set(reader.facts) == {'busy:b'}


def test_parking_and_catching_up_keeps_one_baseline_per_owner(feed, ports):
    ports.subscribers.update({OWNER, OTHER})
    feed.snapshot(OTHER)
    stream = feed.snapshot(OWNER)['streamId']
    for cycle in range(5):
        ports.subscribers.add(OWNER)
        ports.running_by_owner[OWNER] = {f'w{cycle}': f'conversation-attempt:{cycle}'}
        commit(feed, f'w{cycle}')
        _park(feed, ports)
        assert feed.snapshot(OWNER)['streamId'] == stream
    stats = feed.stats()
    assert (stats['owners'], stats['parked']) == (2, 0)
    assert stats['facts'] == 1, 'the one running attempt of OWNER; OTHER has none'


def test_a_gap_in_the_commit_watch_refreshes_every_active_owner(feed, ports):
    ports.subscribers.update({OWNER, OTHER})
    feed.snapshot(OWNER)
    feed.snapshot(OTHER)
    ports.running_by_owner[OWNER] = {'a': 'conversation-attempt:1'}
    ports.running_by_owner[OTHER] = {'b': 'conversation-attempt:2'}
    feed.observe_scopes(None)
    assert feed.wait_idle(10)
    assert sorted((target, frame['key']) for target, frame in ports.drain()) == [
        (OWNER, 'busy:a'), (OTHER, 'busy:b')]


def test_an_unreadable_placement_keeps_the_fact_and_is_read_again(feed, ports):
    ports.subscribers.add(OWNER)
    feed.snapshot(OWNER)
    ports.placement_failures.add('conversation-attempt:1')
    ports.running_by_owner[OWNER] = {'w': 'conversation-attempt:1'}
    commit(feed, 'w')
    [bare] = frames_of(ports)
    assert 'projectPath' not in bare and bare['key'] == 'busy:w'
    ports.placement_failures.clear()
    ports.placements['conversation-attempt:1'] = {'attemptId': '1', 'projectPath': '/p'}
    commit(feed, 'w')
    [placed] = frames_of(ports)
    assert (placed['state'], placed['projectPath']) == ('set', '/p')


def test_an_unreadable_running_set_is_reported_and_keeps_the_last_facts(feed, ports):
    ports.subscribers.add(OWNER)
    ports.running_by_owner[OWNER] = {'w': 'conversation-attempt:1'}
    feed.snapshot(OWNER)

    def broken(owner):
        raise RuntimeError('storage unavailable')
    ports.running = broken
    commit(feed, 'w')
    snapshot = feed.snapshot(OWNER)
    assert snapshot['coverage']['busy'] == 'unavailable'
    assert [fact['key'] for fact in snapshot['facts']] == ['busy:w']
    assert frames_of(ports) == [], 'an unreadable source never clears its facts'


def test_the_broker_hands_commit_scopes_to_listeners_after_waking_streams():
    from lib.conversation_sync.broker import ConversationWakeBroker
    broker = ConversationWakeBroker(owner_history_capacity=8)
    seen = []

    def failing(scopes):
        raise RuntimeError('listener fault')
    broker.add_scope_listener(failing)
    broker.add_scope_listener(seen.append)
    broker.add_scope_listener(seen.append)
    scopes = [{'userId': OWNER, 'conversationId': 'c'}]
    broker.invalidate(scopes)
    broker.invalidate(None)
    assert seen == [scopes, None], 'idempotent registration; a failing listener stops nobody'
    broker.remove_scope_listener(seen.append)
    broker.invalidate(scopes)
    assert seen == [scopes, None]


def test_snapshot_matches_the_contract_and_the_frame_schema(feed, ports):
    from lib.agent_core.events import validate_push_frame
    from lib.router.contract import validate
    ports.subscribers.add(OWNER)
    ports.placements['conversation-attempt:1'] = {'attemptId': '1', 'projectPath': '/p',
                                                  'assignmentId': 'as', 'coordinatorId': 'lead'}
    ports.running_by_owner[OWNER] = {'w': 'conversation-attempt:1'}
    snapshot = feed.snapshot(OWNER)
    assert validate(snapshot, 'officeFacts') is snapshot
    assert snapshot['contract'] == 'tofu.office-facts/v1'
    assert set(snapshot['coverage']) == set(office_facts.SOURCES)
    ports.running_by_owner[OWNER] = {}
    commit(feed, 'w')
    for frame in frames_of(ports):
        assert validate_push_frame(frame) == ()
        json.dumps(frame)


def test_recorded_reader_fixture_conforms_to_the_contract():
    """tests/fixtures/office_facts/stream_v1.json feeds browser reducer specs."""
    from pathlib import Path
    from lib.agent_core.events import validate_push_frame
    from lib.router.contract import validate
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/office_facts/stream_v1.json').read_text())
    for sequence in (fixture, fixture['restart']):
        snapshot, frames = sequence['snapshot'], sequence['frames']
        validate(snapshot, 'officeFacts')
        assert [frame['seq'] for frame in frames] == list(range(snapshot['seq'] + 1, snapshot['seq'] + 1 + len(frames)))
        facts = {fact['key']: fact for fact in snapshot['facts']}
        for frame in frames:
            assert validate_push_frame(frame) == () and frame['streamId'] == snapshot['streamId']
            if frame['state'] == 'set':
                facts[frame['key']] = frame
            else:
                facts.pop(frame['key'])
        assert sorted(facts) == sequence['expectedAfterFrames']
    assert fixture['restart']['snapshot']['streamId'] != fixture['snapshot']['streamId']


# ── report: a lead's coordinator report, a project message report, an assignment result ──

LEAD, WORKER, PROJECT = 'coordinator-lead', 'worker-1', '/proj'


def _lead(ports, owner=OWNER):
    ports.subscribers.add(owner)
    ports.leads_by_owner[owner] = [{'conversationId': LEAD, 'projectPath': PROJECT, 'revision': 3}]


def _keys(frames):
    return [(frame['state'], frame['key']) for frame in frames]


def test_a_coordinator_report_walks_in_unread_and_a_mark_read_updates_it(feed, ports):
    _lead(ports)
    snapshot = feed.snapshot(OWNER)
    assert snapshot['coverage']['reports'] == 'newest_page'
    assert [fact for fact in snapshot['facts'] if fact['fact'] == 'report'] == []
    ports.report_heads[LEAD] = {'turnId': 't1', 'ordinal': 528, 'kind': 'delivery', 'title': 'x',
                                'createdAt': 1, 'unread': True}
    commit(feed, LEAD)  # report_to_user commits its Turn in the lead's own conversation
    [arrived] = frames_of(ports)
    assert {name: arrived[name] for name in ('state', 'fact', 'key', 'source', 'conversationId', 'projectPath',
                                             'turnId', 'ordinal', 'reportKind', 'unread')} == {
        'state': 'set', 'fact': 'report', 'key': f'report:{LEAD}:t1', 'source': 'coordinator_report',
        'conversationId': LEAD, 'projectPath': PROJECT, 'turnId': 't1', 'ordinal': 528,
        'reportKind': 'delivery', 'unread': True}
    assert 'title' not in arrived, 'titles stay on the existing report read'

    # The owner marks it read over HTTP: no conversation commit, only the API host's project hint.
    ports.report_heads[LEAD] = {**ports.report_heads[LEAD], 'unread': False}
    feed.observe_publication('project', 'k', {'projectKey': PROJECT, 'kind': 'coordinator_report',
                                              '_ownerUserId': str(OWNER)})
    assert feed.wait_idle(10)
    [read] = frames_of(ports)
    assert (read['state'], read['key'], read['unread']) == ('set', f'report:{LEAD}:t1', False)

    ports.report_heads[LEAD] = {'turnId': 't2', 'ordinal': 531, 'kind': 'progress', 'title': 'y',
                                'createdAt': 2, 'unread': True}
    commit(feed, LEAD)
    assert _keys(frames_of(ports)) == [('cleared', f'report:{LEAD}:t1'), ('set', f'report:{LEAD}:t2')]


def test_a_report_appears_only_for_a_real_report(feed, ports):
    """Tool rounds of the lead and its Workers, and other message kinds, are never reports."""
    _lead(ports)
    feed.snapshot(OWNER)
    ports.mailboxes[LEAD] = [{'id': 'm-ref', 'kind': 'reference', 'state': 'submitted', 'sender': WORKER},
                             {'id': 'm-q', 'kind': 'question', 'state': 'submitted', 'sender': WORKER}]
    ports.mailbox_versions[LEAD] = 1
    for conversation in (LEAD, WORKER, LEAD):
        commit(feed, conversation)
    ports.attempt_status_by_id['a1'] = 'failed'
    ports.placements['conversation-attempt:a1'] = {
        'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1', 'coordinatorId': LEAD}
    ports.running_by_owner[OWNER] = {WORKER: 'conversation-attempt:a1'}
    commit(feed, WORKER)
    ports.running_by_owner[OWNER] = {}
    commit(feed, WORKER)  # the attempt ended without a completed result
    assert [frame for frame in frames_of(ports) if frame['fact'] == 'report'] == []


def test_a_workers_project_message_report_walks_in_until_its_lead_takes_it(feed, ports):
    _lead(ports)
    feed.snapshot(OWNER)
    ports.mailboxes[LEAD] = [{'id': 'm1', 'kind': 'report', 'state': 'submitted', 'sender': WORKER}]
    ports.mailbox_versions[LEAD] = 1
    commit(feed, WORKER)  # the sender's send_project_message round is the only commit
    [walked] = frames_of(ports)
    assert (walked['state'], walked['key'], walked['source'], walked['conversationId'],
            walked['senderId'], walked['messageId']) == (
        'set', 'message:m1', 'project_message', LEAD, WORKER, 'm1')
    commit(feed, WORKER)
    assert frames_of(ports) == [], 'an unchanged mailbox is not re-read into frames'
    ports.mailboxes[LEAD][0]['state'] = 'input_confirmed'
    ports.mailbox_versions[LEAD] = 2
    commit(feed, LEAD)  # the lead's round confirmed the input
    assert _keys(frames_of(ports)) == [('cleared', 'message:m1')]


def test_a_completed_assignment_result_walks_in_with_its_ids_until_reviewed(feed, ports):
    _lead(ports)
    ports.placements['conversation-attempt:a1'] = {
        'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1', 'coordinatorId': LEAD}
    ports.running_by_owner[OWNER] = {WORKER: 'conversation-attempt:a1'}
    feed.snapshot(OWNER)
    ports.attempt_status_by_id['a1'] = 'completed'
    ports.results['a1'] = {'conversationId': WORKER, 'attemptId': 'a1', 'projectPath': PROJECT,
                           'assignmentId': 'as1', 'coordinatorId': LEAD, 'turnId': 'turn-9',
                           'ordinal': 4, 'revision': 711}
    ports.running_by_owner[OWNER] = {}
    commit(feed, WORKER)
    frames = frames_of(ports)
    assert _keys(frames) == [('cleared', f'busy:{WORKER}'), ('set', 'result:as1:711')]
    result = frames[1]
    assert {name: result[name] for name in ('fact', 'source', 'conversationId', 'coordinatorId', 'projectPath',
                                            'assignmentId', 'revision', 'turnId', 'ordinal')} == {
        'fact': 'report', 'source': 'assignment_result', 'conversationId': WORKER, 'coordinatorId': LEAD,
        'projectPath': PROJECT, 'assignmentId': 'as1', 'revision': 711, 'turnId': 'turn-9', 'ordinal': 4}
    commit(feed, WORKER)
    assert frames_of(ports) == []
    ports.reviews['as1'] = 711
    ports.review_decisions['as1'] = 'rework'
    commit(feed, LEAD)  # the lead's review round
    assert _keys(frames_of(ports)) == [('cleared', 'result:as1:711')]


def test_the_snapshot_seats_unreviewed_results_without_the_assignment_page(feed, ports):
    _lead(ports)
    ports.newest_page[LEAD] = [
        {'conversationId': WORKER, 'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1',
         'coordinatorId': LEAD, 'turnId': 't', 'ordinal': 2, 'revision': 702},
        {'conversationId': 'worker-2', 'attemptId': 'a2', 'projectPath': PROJECT, 'assignmentId': 'as2',
         'coordinatorId': LEAD, 'turnId': 't2', 'ordinal': 2, 'revision': 40}]
    ports.reviews['as2'] = 40
    snapshot = feed.snapshot(OWNER)
    assert [fact['key'] for fact in snapshot['facts']] == ['result:as1:702']
    assert snapshot['coverage']['reports'] == 'newest_page'


def test_an_unbound_lead_takes_its_report_facts_with_it(feed, ports):
    _lead(ports)
    ports.report_heads[LEAD] = {'turnId': 't1', 'ordinal': 1, 'kind': 'progress', 'title': 'x',
                                'createdAt': 1, 'unread': True}
    feed.snapshot(OWNER)
    ports.leads_by_owner[OWNER] = []
    commit(feed, LEAD)
    assert _keys(frames_of(ports)) == [('cleared', f'report:{LEAD}:t1')]


def test_report_reads_stay_scoped_to_the_leads_that_committed(feed, ports):
    _lead(ports)
    ports.leads_by_owner[OWNER].append({'conversationId': 'coordinator-other', 'projectPath': '/other',
                                        'revision': 1})
    feed.snapshot(OWNER)
    reads = ports.report_reads
    commit(feed, WORKER)
    assert ports.report_reads == reads, 'a Worker commit never re-reads report heads'
    commit(feed, LEAD)
    assert ports.report_reads == reads + 1


# ── StoragePorts over the real owners' record layouts ──

@pytest.fixture
def records(monkeypatch):
    from tests.swarm.test_retained_agents import RecordClient
    client = RecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_kw: client)
    return client


def test_storage_leads_page_every_binding_of_the_owner_only(records):
    from lib.conversations.project_coordination import NAMESPACE
    from lib.project_dispatch.history import PAGE_ROWS
    for index in range(PAGE_ROWS + 3):
        records.command('record.put', {'namespace': NAMESPACE, 'key': f'1:{index:04d}', 'expected_version': 0,
            'value': {'user_id': 1, 'projectPath': f'/p{index}', 'conversationId': f'lead-{index}'}}, 'c')
    records.command('record.put', {'namespace': NAMESPACE, 'key': '2:0000', 'expected_version': 0,
        'value': {'user_id': 2, 'projectPath': '/foreign', 'conversationId': 'foreign'}}, 'c')
    leads = office_facts.StoragePorts().leads(1)
    assert len(leads) == PAGE_ROWS + 3
    assert leads[0] == {'conversationId': 'lead-0', 'projectPath': '/p0', 'revision': 1, 'watchdog': False}
    assert 'foreign' not in {lead['conversationId'] for lead in leads}


def test_storage_report_messages_read_the_real_mailbox_and_only_reports(monkeypatch):
    from types import SimpleNamespace
    from lib.conversations import project_messages as pm
    # The mailbox draws tickets with unconditional puts; this fake models both
    # authorities' put-without-expected_version and command-ID replay.
    from tests.project_mod.test_project_message_history import LockedRecordClient
    records = LockedRecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_kw: records)
    projects = {(OWNER, 'sender'): '/project', (OWNER, 'lead'): '/project'}

    def get(self, conversation, *, user_id, include_messages):
        project = projects.get((user_id, conversation))
        return SimpleNamespace(metadata={'settings': {'projectPath': project}}) if project else None
    monkeypatch.setattr(pm.ConversationRepository, 'get', get)
    sender = {'id': 'task-s', 'convId': 'sender', '_userId': OWNER, 'config': {'projectPath': '/project'}}
    for kind, key in (('report', 'r1'), ('reference', 'x1'), ('question', 'q1')):
        pm.execute('send_project_message', {'conversation_id': 'lead', 'content': 'done', 'idempotency_key': key,
                                            'kind': kind}, sender)
    ports, lead = office_facts.StoragePorts(), {'conversationId': 'lead', 'projectPath': '/project'}
    version, messages = ports.report_messages(OWNER, lead, None)
    assert [(message['kind'], message['sender']) for message in messages] == [('report', 'sender')]
    assert ports.report_messages(OWNER, lead, version) == (version, None)
    # Sends never write the head: a report the lead has not absorbed yet is still news.
    pm.execute('send_project_message', {'conversation_id': 'lead', 'content': 'more', 'idempotency_key': 'r2',
                                        'kind': 'report'}, sender)
    later, messages = ports.report_messages(OWNER, lead, version)
    assert later != version and [message['content'] for message in messages] == ['done', 'more']
    pm.ProjectMailbox(OWNER, '/project', 'lead').absorb()
    absorbed, messages = ports.report_messages(OWNER, lead, later)
    assert [message['content'] for message in messages] == ['done', 'more']
    assert ports.report_messages(OWNER, lead, absorbed) == (absorbed, None)


def test_storage_results_follow_the_assignment_association_attempt_and_review(monkeypatch):
    from collections import Counter
    from lib.project_dispatch.repository import ProjectDispatchRepository
    row = {'id': 'as1', 'request': {'recipientId': WORKER, 'coordinatorId': LEAD, 'projectPath': PROJECT,
                                    'profile': {'mode': 'project'}},
           'grant': {'revoked': False}, 'execution': {'turnId': 'turn-9', 'attemptId': 'a1'}, 'review': None}
    attempts = {'a1': {'status': 'completed', 'conversationId': WORKER}}
    turn = {'projectionRevision': 711, 'ordinal': 4, 'status': 'completed', 'currentAttemptId': 'a1'}
    reads = Counter()
    monkeypatch.setattr(ProjectDispatchRepository, 'read_point',
                        lambda self, assignment_id: reads.update(['point']) or dict(row))
    monkeypatch.setattr(ProjectDispatchRepository, 'history_page',
                        lambda self, cursor: reads.update(['page']) or {'rows': [dict(row)]})
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', lambda attempt_id, *, user_id, **_:
                        reads.update(['attempt']) or attempts[attempt_id])
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', lambda conversation, turn_id, *, user_id, include_projection=True:
                        reads.update(['turn']) or dict(turn))
    ports = office_facts.StoragePorts()
    placement = {'conversationId': WORKER, 'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1',
                 'coordinatorId': LEAD}
    expected = {**placement, 'turnId': 'turn-9', 'ordinal': 4, 'revision': 711}
    lead = {'conversationId': LEAD, 'projectPath': PROJECT}
    accepted = {'conversationId': WORKER, 'projectPath': PROJECT, 'assignmentId': 'as1', 'coordinatorId': LEAD,
                'revision': 711}

    def newest(running=frozenset()):
        reads.clear()
        return ports.newest_results(OWNER, lead, running)

    def edge():
        """The busy-to-idle edge decides its one row by the same rule as the page scan."""
        reads.clear()
        return ports.result(OWNER, placement)
    assert edge() == expected and reads == {'point': 1, 'turn': 1}
    turn.update(projectionRevision=713, modelContextRevision=711)  # a translation overlay: the same result
    assert edge() == expected
    assert ports.result(OWNER, {**placement, 'conversationId': 'worker-other'}) is None
    assert newest() == ([expected], [])
    assert reads == {'page': 1, 'turn': 1}, "the Turn's current attempt is the association's: no attempt read"
    assert newest(frozenset({'a1'})) == ([], []) and reads == {'page': 1}, 'a running attempt has no result yet'
    assert ports.review(OWNER, expected) is None
    row['review'] = {'decision': 'accept', 'resultRevision': 711}
    assert edge() is None and reads == {'point': 1}, 'a reviewed row is final: no Turn read'
    turn['projectionRevision'] = 712  # a projection update after the review: still final, never a new result
    assert edge() is None and newest() == ([], [accepted])
    turn['projectionRevision'] = 711
    assert ports.review(OWNER, expected) == {'decision': 'accept', 'resultRevision': 711}
    assert newest() == ([], [accepted]) and reads == {'page': 1}, 'a reviewed row is final: no read'
    row['review'] = {'decision': 'rework', 'resultRevision': 711}
    assert ports.review(OWNER, expected)['decision'] == 'rework'
    assert newest() == ([], []) and reads == {'page': 1}
    row['review'], attempts['a1']['status'], turn['status'] = None, 'failed', 'failed'
    assert edge() is None
    assert newest() == ([], []) and reads == {'page': 1, 'turn': 1}
    # The Turn moved on to another attempt: the association's own attempt decides, on both paths.
    attempts['a1']['status'], turn['currentAttemptId'], turn['status'] = 'completed', 'a9', 'running'
    assert newest() == ([expected], []) and reads == {'page': 1, 'turn': 1, 'attempt': 1}
    assert edge() == expected and reads == {'point': 1, 'turn': 1, 'attempt': 1}
    row['execution'] = {'turnId': 'turn-10', 'attemptId': 'a2'}
    assert ports.result(OWNER, placement) is None
    assert ports.review(OWNER, expected) == {'decision': 'withdrawn'}


def test_a_cold_baseline_reads_each_leads_history_page_once_and_no_attempt_of_a_settled_row(monkeypatch):
    """The office's result scan: one newest history page per lead; a reviewed row is decided by its
    review, a running one by the running set, and an unreviewed settled one by its Turn alone."""
    from collections import Counter
    from copy import deepcopy
    from lib.project_dispatch.repository import ProjectDispatchRepository
    reviews = {**{index: 'accept' for index in range(0, 16)}, **{index: 'rework' for index in range(16, 24)}}
    running = {index for index in range(24, 28)}
    statuses = {28: 'completed', 29: 'completed', 30: 'completed', 31: 'failed'}

    def row(index):
        review = reviews.get(index)
        return {'id': f'as{index}', 'request': {'recipientId': f'worker-{index}', 'coordinatorId': LEAD,
                                                'projectPath': PROJECT, 'profile': {'mode': 'project'}},
                'grant': {'revoked': False}, 'execution': {'turnId': f't{index}', 'attemptId': f'a{index}'},
                'review': {'decision': review, 'resultRevision': 5} if review else None}
    rows = [row(index) for index in range(32)]
    reads = Counter()

    def turn(conversation, turn_id, *, user_id, include_projection=True):
        reads['turn.get'] += 1
        index = int(turn_id[1:])
        return {'turnId': turn_id, 'ordinal': 3, 'projectionRevision': 5, 'currentAttemptId': f'a{index}',
                'status': 'running' if index in running else statuses.get(index, 'completed')}

    def attempt(attempt_id, *, user_id, **_):
        reads['turn.attempt.get'] += 1
        index = int(attempt_id[1:])
        return {'attemptId': attempt_id, 'conversationId': f'worker-{index}',
                'status': 'running' if index in running else statuses.get(index, 'completed')}
    monkeypatch.setattr(ProjectDispatchRepository, 'history_page',
                        lambda self, cursor: reads.update(['history page']) or {'rows': deepcopy(rows)})
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', turn)
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', attempt)

    class Ports(office_facts.StoragePorts):
        """Every other source is empty, so only the result scan reads storage."""
        def running(self, owner):
            return {f'worker-{index}': f'conversation-attempt:a{index}' for index in running}

        def placement(self, owner, conversation_id, task_id):
            return {'attemptId': task_id.split(':', 1)[1]}

        def changed_conversations(self, owner, since_ms):
            return [], True

        def leads(self, owner):
            return [{'conversationId': LEAD, 'projectPath': PROJECT, 'revision': 1}]

        def questions(self, owner, lead):
            return []

        def board(self, owner, lead):
            return None

        def report(self, owner, lead):
            return None

        def report_messages(self, owner, lead, known_version):
            return 0, []

        def alarms(self, owner):
            return []

        def publish(self, owner, frame):
            pass

        def subscribed(self, owner):
            return False

    feed = OfficeFacts(Ports())
    try:
        snapshot = feed.snapshot(OWNER)
    finally:
        feed.close()
    assert reads == {'history page': 1, 'turn.get': 4}, dict(reads)
    assert sorted(fact['key'] for fact in snapshot['facts'] if fact['fact'] == 'report') == [
        'result:as28:5', 'result:as29:5', 'result:as30:5']
    assert sorted(fact['key'] for fact in snapshot['facts'] if fact['fact'] == 'document') == sorted(
        f'document:as{index}:5' for index in range(16))
    assert snapshot['coverage']['documents'] == 'newest_page'


# ── decision, board, alarm, document ──

def test_a_decision_is_set_only_for_an_open_owner_question_and_cleared_when_answered(feed, ports):
    _lead(ports)
    feed.snapshot(OWNER)
    ports.questions_by_lead[LEAD] = [{'turnId': 't-ask', 'ordinal': 530, 'guidanceId': 'hg_1', 'blocking': True}]
    commit(feed, LEAD)  # the ask_human round commits in the lead's conversation
    [asked] = frames_of(ports)
    assert {name: asked[name] for name in ('state', 'fact', 'key', 'source', 'conversationId', 'projectPath',
                                           'turnId', 'ordinal', 'guidanceId', 'blocking')} == {
        'state': 'set', 'fact': 'decision', 'key': f'question:{LEAD}:hg_1', 'source': 'owner_question',
        'conversationId': LEAD, 'projectPath': PROJECT, 'turnId': 't-ask', 'ordinal': 530,
        'guidanceId': 'hg_1', 'blocking': True}
    commit(feed, WORKER)
    commit(feed, LEAD)  # unrelated rounds: no question opened or closed
    assert frames_of(ports) == []
    ports.questions_by_lead[LEAD][0]['answered'] = True
    commit(feed, LEAD)  # the owner's answer commits in the lead's conversation
    assert _keys(frames_of(ports)) == [('cleared', f'question:{LEAD}:hg_1')]
    commit(feed, LEAD)
    assert frames_of(ports) == []


def test_the_snapshot_holds_every_open_question_not_a_turn_window(feed, ports):
    _lead(ports)
    ports.questions_by_lead[LEAD] = [
        {'turnId': f't{index}', 'ordinal': index, 'guidanceId': f'hg_{index}', 'blocking': index % 2 == 0}
        for index in range(25)]
    snapshot = feed.snapshot(OWNER)
    assert len([fact for fact in snapshot['facts'] if fact['fact'] == 'decision']) == 25
    assert snapshot['coverage']['decisions'] == 'complete'


def test_a_board_fact_follows_the_board_revision(feed, ports):
    _lead(ports)
    ports.boards[LEAD] = 12
    snapshot = feed.snapshot(OWNER)
    [board] = [fact for fact in snapshot['facts'] if fact['fact'] == 'board']
    assert (board['key'], board['revision'], board['conversationId']) == (f'board:{PROJECT}', 12, LEAD)
    commit(feed, LEAD)
    assert frames_of(ports) == []
    ports.boards[LEAD] = 13
    commit(feed, LEAD)  # update_task_progress commits its round in the lead
    [moved] = frames_of(ports)
    assert (moved['state'], moved['key'], moved['revision']) == ('set', f'board:{PROJECT}', 13)


def test_an_alarm_is_set_while_a_watchdog_signal_is_open(feed, ports):
    _lead(ports)
    ports.leads_by_owner[OWNER].append({'conversationId': 'watchdog', 'projectPath': '/watchdog',
                                        'revision': 1, 'watchdog': True})
    feed.snapshot(OWNER)
    ports.open_alarms = [{'signalId': 'sig-1', 'conversationId': WORKER},
                         {'signalId': 'sig-2', 'conversationId': ''}]
    commit(feed, WORKER)
    assert frames_of(ports) == [], 'only the Watchdog conversation records and resolves signals'
    commit(feed, 'watchdog')  # a new signal wakes the Watchdog
    frames = frames_of(ports)
    assert [(frame['state'], frame['key'], frame['conversationId']) for frame in frames] == [
        ('set', 'alarm:sig-1', WORKER), ('set', 'alarm:sig-2', 'watchdog')]
    ports.open_alarms = ports.open_alarms[1:]
    commit(feed, 'watchdog')  # the Watchdog resolves it
    assert _keys(frames_of(ports)) == [('cleared', 'alarm:sig-1')]


def test_a_document_appears_only_for_an_accepted_result(feed, ports):
    _lead(ports)
    ports.accepted_page[LEAD] = [{'conversationId': 'worker-old', 'projectPath': PROJECT, 'assignmentId': 'as0',
                                  'coordinatorId': LEAD, 'revision': 5}]
    ports.placements['conversation-attempt:a1'] = {
        'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1', 'coordinatorId': LEAD}
    ports.placements['conversation-attempt:a2'] = {
        'attemptId': 'a2', 'projectPath': PROJECT, 'assignmentId': 'as2', 'coordinatorId': LEAD}
    ports.running_by_owner[OWNER] = {WORKER: 'conversation-attempt:a1', 'worker-2': 'conversation-attempt:a2'}
    snapshot = feed.snapshot(OWNER)
    assert [fact['key'] for fact in snapshot['facts'] if fact['fact'] == 'document'] == ['document:as0:5']
    assert snapshot['coverage']['documents'] == 'newest_page'
    for attempt, assignment, worker in (('a1', 'as1', WORKER), ('a2', 'as2', 'worker-2')):
        ports.attempt_status_by_id[attempt] = 'completed'
        ports.results[attempt] = {'conversationId': worker, 'attemptId': attempt, 'projectPath': PROJECT,
                                  'assignmentId': assignment, 'coordinatorId': LEAD, 'turnId': 't-' + attempt,
                                  'ordinal': 3, 'revision': 9}
    ports.running_by_owner[OWNER] = {}
    commit(feed, WORKER, 'worker-2')
    frames_of(ports)
    ports.reviews.update({'as1': 9, 'as2': 9})
    ports.review_decisions['as2'] = 'rework'
    commit(feed, LEAD)
    frames = frames_of(ports)
    assert _keys(frames) == [('cleared', 'result:as1:9'), ('cleared', 'result:as2:9'), ('set', 'document:as1:9')]
    document = frames[-1]
    assert {name: document[name] for name in ('fact', 'source', 'conversationId', 'coordinatorId',
                                              'assignmentId', 'revision')} == {
        'fact': 'document', 'source': 'accepted_result', 'conversationId': WORKER, 'coordinatorId': LEAD,
        'assignmentId': 'as1', 'revision': 9}


def test_storage_questions_read_the_leads_index_and_no_lane(records, monkeypatch):
    """Recorded answers cost nothing; an open non-blocking question needs no Turn; an open
    blocking one costs its card read."""
    from lib.router.proactive import questions as q

    def put(lead, guidance, **value):
        records.command('record.put', {'namespace': q.NAMESPACE, 'key': f'{OWNER}:{lead}:{guidance}',
            'expected_version': 0, 'value': {'owner': OWNER, 'coordinatorId': lead, 'projectPath': PROJECT,
            'guidanceId': guidance, 'turnId': 'turn-' + guidance, 'question': '?', 'createdAt': len(records.rows),
            **value}}, 'c')
    put(LEAD, 'hg_card', blocking=True, laneId='main', ordinal=3)
    put(LEAD, 'hg_done', blocking=False, laneId='main', ordinal=5, answersRecorded=True, answeredAt=1,
        answeredOrdinal=6)
    put(LEAD, 'hg_open', blocking=False, laneId='main', ordinal=7, answersRecorded=True)
    put(LEAD, 'hg_carded', blocking=True, laneId='main', ordinal=8)
    put('other-lead', 'hg_x', blocking=True)
    cards = []

    def card(conversation, turn_id, *, user_id, include_projection=True):
        cards.append(turn_id)
        rounds = [{'guidanceId': 'hg_carded', 'guidanceResponse': 'yes'}] if turn_id == 'turn-hg_carded' else []
        return {'turnId': turn_id, 'laneId': 'main', 'ordinal': 3 if turn_id == 'turn-hg_card' else 8,
                'projection': {'toolRounds': rounds}}
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', card)
    monkeypatch.setattr('lib.turn_lifecycle.lane_turns_newest_first',
                        lambda *args, **kwargs: pytest.fail('the office never reads a lead lane'))
    lead = {'conversationId': LEAD, 'projectPath': PROJECT}
    assert office_facts.StoragePorts().questions(OWNER, lead) == [
        {'turnId': 'turn-hg_card', 'guidanceId': 'hg_card', 'blocking': True, 'ordinal': 3},
        {'turnId': 'turn-hg_open', 'guidanceId': 'hg_open', 'blocking': False, 'ordinal': 7}]
    assert sorted(cards) == ['turn-hg_card', 'turn-hg_carded']
    assert records.rows[(q.NAMESPACE, f'{OWNER}:{LEAD}:hg_carded')]['value']['answeredOrdinal'] == 8
    cards.clear()
    assert [question['guidanceId'] for question in office_facts.StoragePorts().questions(OWNER, lead)] == [
        'hg_card', 'hg_open']
    assert cards == ['turn-hg_card'], 'a recorded card answer is never read again'


def test_storage_board_reads_the_current_bindings_head_revision(records):
    from lib.conversations.coordinator_progress import POLICY, _head_key
    lead = {'conversationId': LEAD, 'projectPath': PROJECT, 'revision': 3}
    assert office_facts.StoragePorts().board(OWNER, lead) is None
    scope = {'owner': OWNER, 'projectPath': PROJECT, 'coordinatorId': LEAD, 'bindingRevision': 3}
    records.command('record.put', {'namespace': POLICY['namespace'], 'key': _head_key(OWNER, PROJECT),
        'expected_version': 0, 'value': {'scope': scope, 'board': {
            'revision': 12, 'updatedAt': 1, 'summary': 's', 'tasks': [{'title': 'a', 'status': 'pending'}]}}}, 'c')
    assert office_facts.StoragePorts().board(OWNER, lead) == 12
    assert office_facts.StoragePorts().board(OWNER, {**lead, 'revision': 4}) is None


def test_storage_alarms_page_every_open_signal(monkeypatch):
    from lib.router.watchdog import signals

    pages = {None: {'signals': [{'signalId': 's1', 'subject': {'conversationId': 'c1'}}], 'next_cursor': 's1'},
             's1': {'signals': [{'signalId': 's2', 'subject': {}}], 'next_cursor': None}}
    monkeypatch.setattr(signals.FaultSignals, 'open_page', lambda self, after_id=None: pages[after_id])
    assert office_facts.StoragePorts().alarms(OWNER) == [{'signalId': 's1', 'conversationId': 'c1'},
                                                         {'signalId': 's2', 'conversationId': ''}]


def test_busy_clears_at_the_attempts_terminal_commit_while_its_job_still_completes(feed, ports):
    """The attempt settles in a commit; its worker job completes afterwards with no commit at all."""
    _lead(ports)
    ports.placements['conversation-attempt:a1'] = {
        'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1', 'coordinatorId': LEAD}
    ports.running_by_owner[OWNER] = {WORKER: 'conversation-attempt:a1'}
    feed.snapshot(OWNER)
    commit(feed, WORKER)  # a tool round: the attempt still runs
    assert frames_of(ports) == []
    ports.attempt_status_by_id['a1'] = 'completed'
    ports.results['a1'] = {'conversationId': WORKER, 'attemptId': 'a1', 'projectPath': PROJECT,
                           'assignmentId': 'as1', 'coordinatorId': LEAD, 'turnId': 't', 'ordinal': 2,
                           'revision': 5}
    commit(feed, WORKER)  # the terminal commit; the job is still listed running
    assert _keys(frames_of(ports)) == [('cleared', f'busy:{WORKER}'), ('set', 'result:as1:5')]
    ports.running_by_owner[OWNER] = {}
    commit(feed, LEAD)  # the job completed; nothing changes again
    assert frames_of(ports) == []


def test_after_a_watch_gap_only_conversations_that_moved_are_re_read(feed, ports):
    _lead(ports)
    ports.leads_by_owner[OWNER].append({'conversationId': 'coordinator-other', 'projectPath': '/other',
                                        'revision': 1})
    ports.touch(LEAD)
    feed.snapshot(OWNER)
    reads = ports.report_reads
    feed.observe_scopes(None)  # a gap with no commit since the watermark
    assert feed.wait_idle(10)
    assert ports.report_reads == reads
    ports.touch('coordinator-other')
    ports.report_heads['coordinator-other'] = {'turnId': 't9', 'ordinal': 9, 'kind': 'delivery', 'title': 'x',
                                               'createdAt': 1, 'unread': True}
    feed.observe_scopes(None)  # the report commit landed inside a gap
    assert feed.wait_idle(10)
    assert ports.report_reads == reads + 1, 'only the lead the catalog names is re-read'
    assert _keys(frames_of(ports)) == [('set', 'report:coordinator-other:t9')]


def test_a_terminal_commit_inside_a_gap_still_ends_busy_while_its_job_completes(feed, ports):
    _lead(ports)
    ports.placements['conversation-attempt:a1'] = {
        'attemptId': 'a1', 'projectPath': PROJECT, 'assignmentId': 'as1', 'coordinatorId': LEAD}
    ports.running_by_owner[OWNER] = {WORKER: 'conversation-attempt:a1', 'worker-2': 'conversation-attempt:a2'}
    feed.snapshot(OWNER)
    assert feed.wait_idle(10)
    ports.attempt_reads.clear()
    ports.attempt_status_by_id['a1'] = 'completed'
    ports.touch(WORKER)
    feed.observe_scopes(None)
    assert feed.wait_idle(10)
    assert ports.attempt_reads == ['a1'], 'only the attempt whose conversation moved is read'
    assert _keys(frames_of(ports)) == [('cleared', f'busy:{WORKER}')]


def test_attempt_reads_wait_while_passes_are_pending(ports):
    """Under load the next pass re-reads the running jobs; attempt reads only use idle time."""
    ports.subscribers.add(OWNER)
    ports.running_by_owner[OWNER] = {f'w{index}': f'conversation-attempt:a{index}' for index in range(5)}
    feed = OfficeFacts(ports)
    try:
        feed.snapshot(OWNER)
        assert feed.wait_idle(10)
        ports.attempt_reads.clear()
        real = ports.attempt_status

        def read_and_commit(owner, attempt_id):
            if len(ports.attempt_reads) == 1:
                feed.observe_scopes([{'userId': OWNER, 'conversationId': 'w0'}])  # a trigger arrives
            return real(owner, attempt_id)
        ports.attempt_status = read_and_commit
        feed.observe_scopes([{'userId': OWNER, 'conversationId': f'w{index}'} for index in range(5)])
        assert feed.wait_idle(10)
        reads = ports.attempt_reads
        assert sorted(set(reads)) == [f'a{index}' for index in range(5)], 'every suspect is read in the end'
        assert ports.running_reads >= 3, 'the pending trigger ran its pass before the remaining reads'
    finally:
        feed.close()


def test_storage_changed_conversations_read_the_recency_index_without_transcripts(monkeypatch):
    calls = []

    class Client:
        def query(self, operation, payload, **_kw):
            calls.append((operation, payload))
            rows = [('c2', 1500), ('c1', 1200)][:payload['limit']]
            return [{'metadata': {'id': cid, 'updated_at': at}, 'messages': []} for cid, at in rows]
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_kw: Client())
    ports = office_facts.StoragePorts()
    assert ports.changed_conversations(OWNER, None) == ([('c2', 1500)], True)
    assert ports.changed_conversations(OWNER, 1200) == ([('c2', 1500), ('c1', 1200)], True)
    (first_op, first), (_op, second) = calls
    assert first_op == 'conversation.list' and first['limit'] == 1 and 'updated_at_gte' not in first
    assert second['updated_at_gte'] == 1200 and second['limit'] == office_facts.CONVERSATION_LIST_MAXIMUM
    assert all(call['include_messages'] is False and call['settings_keys'] == [] and call['user_id'] == OWNER
               for _op, call in calls)
