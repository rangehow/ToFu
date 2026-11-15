"""Trusted project dispatch admission; no new loop, budget or task lifecycle.

The project repository owns explicit human grants and conserved physical-attempt
receipts. ExecutionBudget remains the admission/settlement owner. Project and
Coordinator grants carry no spend ceiling: occurrences are recorded and missing
receipts stay lower bounds. Earlier finite grants keep their recorded ceilings.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from collections.abc import Mapping
from pathlib import Path
import time

from lib.conversations.session_model import apply_session_model, session_model
from lib.identity import PrincipalContext
from lib.llm.llm_errors import AbortedError, ToolAdmissionRefused
from lib.log import get_logger
from lib.tools.execution_timing import timing_phase

KEY = '_projectDispatchAdmission'
# A Coordinator follow-up's witness: the settled attempt whose association it continues.
FOLLOW_UP_KEY = '_projectDispatchFollowUp'
WORKSPACE_TASK_KEY = '_projectDispatchWorkspaceTaskId'
ROOT_TASK_KEY = '_projectDispatchRootTask'
ROLE_KEY = '_projectDispatchRole'
logger = get_logger(__name__)


class _LiveDispatchRoot:
    """Process-local reference to the bound Worker task; copies share, never clone, it."""
    __slots__ = ('task',)

    def __init__(self, task):
        self.task = task

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def share_dispatch_root(parent, carrier):
    """Let a sub-agent carrier borrow its parent's live dispatch authority.

    The carrier holds no grant of its own: every admission revalidates the live
    root task, so the Worker's revocation, Stop or settlement fences it too.
    """
    if parent is None or not is_dispatch_task(parent):
        return
    carrier[ROOT_TASK_KEY] = _LiveDispatchRoot(dispatch_root(parent))


class DispatchTaskInterrupted(AbortedError):
    """The refusal of a call whose task is already stopped: it names that stop, never a lost grant.

    ``abort_reason`` is the stop's own first cause (``server_shutdown`` for a
    restart's quiesce, empty for a person's Stop); the executor reads it to
    decide whether the cut-off call is repeatable or cancelled. Workers that
    recovery continued after the 2026-11-13 restart read "task identity is no
    longer authorized" in their last tool result as a revoked grant and ended
    their Turns.
    """

    def __init__(self, abort_reason):
        from lib.turn_verdict import RESTART_ABORT_REASONS, stopped_tool_result
        self.abort_reason = str(abort_reason or '')
        message = stopped_tool_result(self.abort_reason)
        if self.abort_reason in RESTART_ABORT_REASONS:
            message += ' The project dispatch grant is unchanged.'
        super().__init__(message)


class DispatchIdentityMismatch(AbortedError):
    """One named identity fact of the task (``mismatch``) differs from its accepted grant."""

    def __init__(self, mismatch, detail):
        self.mismatch = mismatch
        super().__init__(f'Project dispatch task identity is no longer authorized: {detail}')


def dispatch_root(task):
    """The bound physical task whose grant admits ``task`` (itself for a root)."""
    from lib.project_mod.foreground_command_slots import CommandTaskView
    if isinstance(task, CommandTaskView):
        # A run_command runner sees its task through a per-command view; the
        # view is that task, so the call's admission covers it.
        task = task.viewed_task
    holder = task.get(ROOT_TASK_KEY)
    if holder is None:
        return task
    if task.get('aborted'):
        raise DispatchTaskInterrupted(task.get('_abort_reason'))
    root = holder.task if isinstance(holder, _LiveDispatchRoot) else None
    if (not isinstance(root, dict) or ROOT_TASK_KEY in root
            or task.get('_principalContext') != root.get('_principalContext')
            or task.get('convId') != root.get('convId')
            or task.get('_executionBudget') is not root.get('_executionBudget')):
        raise AbortedError('Project dispatch sub-agent no longer shares its Worker authority')
    return root


def authority_stop_reason(error):
    """``dispatch_grant_revoked`` when a revocation caused ``error``, else ``dispatch_authority_lost``."""
    from lib.turn_verdict import DISPATCH_AUTHORITY_LOST, DISPATCH_GRANT_REVOKED
    from .repository import DispatchGrantRevoked
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, DispatchGrantRevoked):
            return DISPATCH_GRANT_REVOKED
        seen.add(id(error))
        error = error.__cause__ if error.__cause__ is not None else (
            None if error.__suppress_context__ else error.__context__)
    return DISPATCH_AUTHORITY_LOST


def stop_on_authority_failure(task, error):
    """Halt the task before any further provider/tool call, naming the dispatch cause.

    The first cause wins: a task already stopped by a person, a fence or an
    earlier authority failure keeps its own reason.
    """
    event = task.get('abort_event')
    if task.get('aborted') or (event is not None and event.is_set()):
        return
    reason = authority_stop_reason(error)
    task['_abort_reason'] = reason
    task['_abortReasonDetail'] = f'Authority check refused: {error}'
    task['_abort_timestamp'] = time.time()
    task['aborted'] = True
    if event is not None:
        event.set()
    logger.warning('[ProjectDispatch] authority stopped task=%s reason=%s: %s',
                   str(task.get('id') or '?'), reason, error)


def repository_for(binding):
    from .repository import ProjectDispatchRepository
    return ProjectDispatchRepository(binding['owner'], binding['projectPath'])


def wait_for_dispatch(task, operation, *, label, cancellable=True, effect_free=False,
                      stopping=None):
    """``wait_for_persistence`` that re-runs one dispatch read or ledger write through contention.

    A lost head CAS or a torn snapshot proves only that another writer
    committed: the operation reads and validates the live grant again and
    proceeds, or meets a real verdict. It is bounded by the persistence-wait
    liveness contract (ACK, owner Stop, server shutdown), never by a count or
    timeout of its own; its existing backoff lets a burst of writers drain.
    """
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    from .repository import transient_dispatch_failure
    return wait_for_persistence(task, operation, label=label, cancellable=cancellable,
                                effect_free=effect_free, transient=transient_dispatch_failure,
                                stopping=stopping)


def reread_on_contention(task, operation, *, label, cancellable=True, effect_free=False):
    """Run ``operation`` once; only a transient dispatch failure re-runs it in ``wait_for_dispatch``.

    For call sites without a persistence fence of their own: an uncontended call
    keeps its existing Stop handling, and only a lost CAS, a torn snapshot or a
    transient storage failure enters the wait.
    """
    from .repository import transient_dispatch_failure
    try:
        return operation()
    except Exception as error:
        if not transient_dispatch_failure(error):
            raise
    return wait_for_dispatch(task, operation, label=label, cancellable=cancellable, effect_free=effect_free)


def principal_for(binding):
    return PrincipalContext.system(subject_id='project-dispatch:' + binding['assignmentId'],
        owner_user_id=binding['owner'], tenant_id=binding['tenant'], scopes={'chat'})


def profile_for(assignment):
    if assignment['request'].get('profile') is not None:
        from .scope import profile_for as checked_profile
        return checked_profile(assignment)
    return {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}


def asks_human(profile):
    """Only a Coordinator continuation whose issued policy froze ask_human may ask.

    The Coordinator conversation is the human's channel, so its questions reach
    a reader even without an attending request. Older policies never gain this.
    """
    return profile['mode'] == 'coordinator' and 'ask_human' in profile['tools']


def requires_isolated_workspace(task):
    """File grants retain reviewed candidates regardless of direct preference.

    This selects storage policy only; task/account/IO admission still validates
    the bound physical executor. Ordinary tasks never read dispatch storage.
    The mode is the grant's immutable profile, so a task whose ``bind_account``
    validated this carrier reads it from its role.
    """
    if not is_dispatch_task(task):
        return False
    binding = (task.get('config') or {}).get(KEY)
    if not isinstance(binding, dict):
        raise AbortedError('Project dispatch lost its accepted grant carrier')
    role = task.get(ROLE_KEY)
    if role is not None and role['binding'] == binding and 'mode' in role:
        return role['mode'] in ('files', 'project')
    assignment = repository_for(binding).validate(binding)
    return profile_for(assignment)['mode'] in ('files', 'project')


class DispatchAdmission:
    """One live validation of a bound dispatch root, shared by every gate of one admission.

    ``admit_dispatch`` makes it. A tool batch hands it to the calls it admitted
    and ``admitted_call`` to one call's nested gates; a gate passes it back to
    ``validated_dispatch``, which rechecks the task's in-memory carrier, identity
    and account against its row and reads no storage. It is never stored on a
    task or reused by a later admission: the tool intent (the effect fence),
    model intents, descriptor I/O and every new admission validate live.
    """
    __slots__ = ('root', 'binding', 'assignment', '_workspace')

    def __init__(self, root, binding, assignment):
        self.root = root
        self.binding = deepcopy(binding)
        self.assignment = assignment
        self._workspace = None

    def covers(self, task):
        """Whether this admission validated ``task``'s own root and its unchanged carrier."""
        return (dispatch_root(task) is self.root
                and (self.root.get('config') or {}).get(KEY) == self.binding)

    def registered_workspace(self, task):
        """The root's registered workspace row, resolved at most once per admission."""
        if self._workspace is None:
            self._workspace = owned_workspace(task, self.binding)
        return self._workspace


_CALL_ADMISSION = ContextVar('project_dispatch_call_admission', default=None)


@contextmanager
def admitted_call(admission):
    """Make ``admission`` the one every dispatch gate nested in this call decides from."""
    token = _CALL_ADMISSION.set(admission)
    try:
        yield admission
    finally:
        _CALL_ADMISSION.reset(token)


def call_admission(task):
    """The admission of the call executing now, when it validated ``task``'s own root."""
    admission = _CALL_ADMISSION.get()
    if admission is None or not isinstance(task, dict) or not is_dispatch_task(task):
        return None
    try:
        return admission if admission.covers(task) else None
    except AbortedError:
        # A sub-agent that no longer shares its Worker validates live and fails there.
        return None


def owned_workspace(task, binding=None):
    """Neither config, cwd nor a cached receipt can transfer a worktree."""
    task = dispatch_root(task)
    binding = binding or (task.get('config') or {}).get(KEY)
    if not isinstance(binding, dict):
        raise ToolAdmissionRefused('File execution has no accepted project binding')
    from lib.integration_state_repository import find_workspace
    from lib.tasks_pkg.workspace_delivery import execution_repositories, workspace_work_id
    # The grant keeps the conversation's project key. Integration registers the
    # physical root; compare it with the captured binding so retargeted aliases
    # cannot adopt another checkout. The checkout is keyed by the assignment's
    # work id that bind_account stamped from the validated association.
    project_root = str(Path(binding['projectPath']).expanduser().resolve())
    work_id = workspace_work_id(task)
    with timing_phase('dispatch_authority'):
        row = find_workspace(project_root, work_id, user_id=binding['owner'])
    cfg = task.get('config') or {}
    if (not isinstance(row, dict) or row.get('user_id') != binding['owner']
            or row.get('task_id') != work_id or row.get('project_root') != project_root
            or row.get('state') == 'discarded' or row.get('workspace_path') != cfg.get('projectPath')
            or row.get('workspace_path') == project_root
            or not any(item.get('canonicalRoot') == project_root
                       and item.get('workspacePath') == row.get('workspace_path')
                       and item.get('workId') == work_id
                       for item in execution_repositories(task))):
        raise ToolAdmissionRefused('File execution requires its registered owned isolated worktree')
    return row


class WorkerWorkspaceUnavailable(ValueError):
    """The project root cannot host the isolated Git checkout every worker needs.

    The refusal stays true until the human acts, so it carries its own
    Coordinator recovery instead of the generic retry advice. ``code`` and
    ``details`` (the owner's question for ``ambiguous_project_root``) reach the
    Coordinator in its tool result.
    """
    execution_started = False
    _RECOVERY = {
        'ambiguous_project_root': (
            'No worker was created and nothing was written into the folder. Only the owner '
            'can answer this: ask them the question in details (recommended answer: shared) '
            'and report it as a blocker; once Tofu has prepared the folder with their answer, '
            'delegate again. Delegating before that fails the same way.'),
        'volume_stalled': (
            'No worker was created. The project folder\'s storage stopped responding while Tofu '
            'prepared it; delegate again once it responds, and preparation resumes where it '
            'stopped. If it keeps stalling, report the blocker to the human.'),
    }

    def __init__(self, message: str, *, code: str = 'unsupported_root', details: dict | None = None):
        super().__init__(message)
        self.code, self.details = code, details
        self.recovery = self._RECOVERY.get(code, (
            'No worker was created. Delegating again fails the same way until the human '
            'changes the project root; report this blocker and its remedy to the human '
            'instead of retrying.'))


def require_isolated_worker_root(project_path, *, cancel=None, progress=None):
    """Refuse delegation before any worker conversation or grant exists.

    Preparing the root can take as long as a large folder's volume needs;
    ``progress`` observes each advance and ``cancel`` is the caller's Stop.
    """
    from lib.tasks_pkg.workspace_delivery import isolated_workspace_refusal
    refusal = isolated_workspace_refusal(project_path, cancel=cancel, progress=progress)
    if refusal is not None:
        raise WorkerWorkspaceUnavailable(refusal.message(), code=refusal.code,
                                         details=refusal.details)
    # The Worker about to start takes a prepared checkout instead of a whole tree.
    from lib.project_mod import task_spares
    task_spares.ensure(Path(project_path).expanduser().resolve())



# Model-view selections of the human request a Coordinator continuation resumes.
# Never authority: the grant fixes capabilities, tools and the model.
REQUEST_SELECTION_FIELDS = ('chatMode', 'memoryEnabled', 'thinkingEnabled', 'defaultThinkingDepth')
REQUEST_ROUTE_FIELDS = ('modelRef', 'preferredProviderId', 'routing', 'preset', 'provider_id', 'providerId')


def request_selection(binding, assignment):
    """Selections a Coordinator continuation keeps from its accepted human request.

    The continuation resumes that request's conversation and model view. Admitted
    from the grant alone it ran under another route and replay policy, so every
    alternation with a human Turn rebuilt the whole history. Experiment blocks
    are copied normalized (the grant owns ``tools``); a route naming another
    model than the grant's is not inherited.
    """
    from lib.context_experiment_flags import normalize_context_experiment_flags
    request = assignment['request']
    # validate() already fenced this exact source attempt of the delegation.
    accepted = repository_for(binding).accepted_attempt_config(request['delegation']['attemptId'])
    selection = {key: deepcopy(accepted[key]) for key in REQUEST_SELECTION_FIELDS if key in accepted}
    selection.update((block, value) for block, value in
                     normalize_context_experiment_flags(accepted).items() if block != 'tools')
    route = accepted.get('modelRef')
    model = apply_session_model(binding['recipientId'], request['model'], owner=binding['owner'])
    if (route.get('model_id') if isinstance(route, dict) else accepted.get('model')) == model:
        selection.update((key, deepcopy(accepted[key])) for key in REQUEST_ROUTE_FIELDS if key in accepted)
    return selection


def execution_limits(assignment):
    """Absent request dimensions are unbounded; earlier records keep their values."""
    budget = assignment['request']['budget']
    return {'max_tokens': budget.get('maxTokens'), 'max_tool_calls': profile_for(assignment).get('maxToolCalls'),
            'deadline': budget.get('deadline')}


def unbounded_dispatch(assignment):
    """Project/Coordinator grants issued without ceilings record usage, never refuse it."""
    return profile_for(assignment)['mode'] in ('project', 'coordinator') and all(
        value is None for value in execution_limits(assignment).values())


@dataclass(frozen=True)
class TrustedDispatch:
    """Internal keyword-only authority; a public config cannot instantiate it.

    ``follows`` pins a Coordinator follow-up: the exact settled association
    (``execution``) its new Turn continues from, and the predecessor attempt
    (``source``) its accepted config carries as the account's witness.

    One instance serves one command. The command validates the grant live
    before its first effect (``capture``, before the Turn exists) and again
    where it binds the association after creating the Turn; the principal, the
    grant's birth time and the work id it reads in between come from that
    command's latest live validation, never from another command's.
    """
    binding: dict
    follows: dict | None = None
    _validation: list = field(default_factory=list, init=False, compare=False, repr=False)

    @classmethod
    def admitted(cls, binding, assignment):
        """The dispatch of a caller that has just validated ``binding`` as ``validate`` does."""
        trusted = cls(binding)
        trusted._validation.append(assignment)
        return trusted

    def checked(self):
        assignment = repository_for(self.binding).validate(self.binding)
        self._validation[:] = [assignment]
        return assignment

    def validated(self):
        """The row of this command's latest live validation, validating now if it has none."""
        return self._validation[0] if self._validation else self.checked()

    @property
    def principal(self):
        self.validated()
        return principal_for(self.binding)

    def capture(self, config, owner, principal, recipient):
        assignment = self.validated()
        if (owner != self.binding['owner'] or recipient != self.binding['recipientId']
                or principal != principal_for(self.binding)):
            raise PermissionError('Project dispatch identity does not match its authorization')
        from lib.conversation_sync.execution_identity import public_execution_fields
        config = public_execution_fields(config)

        profile = profile_for(assignment)
        if profile['mode'] in ('project', 'coordinator'):
            config = deepcopy(assignment['request']['delegation']['policy']['config'])
        if profile['mode'] == 'coordinator':
            config = {**request_selection(self.binding, assignment), **config}
        config.update({KEY: deepcopy(self.binding),
            WORKSPACE_TASK_KEY: self.task_id,
            '_turnPrincipalTenantId': self.binding['tenant'] or '',
            'canonicalProjectPath': self.binding['projectPath'], 'projectPath': self.binding['projectPath'],
            'projectPaths': [], 'readOnlyPaths': [self.binding['projectPath']] if profile['mode'] == 'consult' else [],
            'model': assignment['request']['model'], 'executionLimits': execution_limits(assignment),
            'planMode': False, 'autopilot': False, 'activeFlow': '', 'humanGuidanceEnabled': asks_human(profile),
            'autoTranslate': False})
        if profile['mode'] in ('project', 'coordinator'):
            # The owner may change the session's model between accepted attempts
            # (lib/conversations/session_model.py). The grant, tools, checkout and
            # account are unchanged; an in-flight attempt is never rewritten.
            # Route fields come only from request_selection, which inherits them
            # only when they name this same effective model.
            config['model'] = apply_session_model(recipient, config['model'], owner=owner)
        if self.follows is not None:
            config[FOLLOW_UP_KEY] = deepcopy(self.follows['source'])
        if profile['mode'] != 'project':
            config['tools'] = {**config.get('tools', {}), 'programmaticCalling': 'off'}
        if profile['mode'] not in ('project', 'coordinator'):
            config.update(searchEnabled=False, fetchEnabled=False, codeExecEnabled=False,
                          browserEnabled=False, desktopEnabled=False)
        return config

    def bind_execution(self, turn_id, attempt_id):
        execution = self.validated()['execution'] or {}
        if execution.get('turnId') == turn_id and execution.get('attemptId') == attempt_id:
            return self.validated()
        # One assignment owns its checkout across all of its resumed attempts.
        # The repository validates live once more: the Turn exists now.
        assignment = repository_for(self.binding).bind_execution(
            self.binding['assignmentId'], turn_id, attempt_id, task_id=self.task_id,
            admission=self.binding)
        self._validation[:] = [assignment]
        return assignment

    def continue_execution(self, turn_id, attempt_id):
        assignment = self.validated()
        execution = assignment['execution']
        if execution is None:
            return self.bind_execution(turn_id, attempt_id)
        if execution['attemptId'] == attempt_id and execution['turnId'] == turn_id:
            return assignment
        # A continuation advances from the live association.
        execution = self.checked()['execution']
        if execution['attemptId'] == attempt_id and execution['turnId'] == turn_id:
            return self.validated()
        # A follow-up advances only from the association its Coordinator read.
        previous = execution if self.follows is None else self.follows['execution']
        return repository_for(self.binding).continue_execution(
            self.binding['assignmentId'], previous, turn_id, attempt_id)

    @property
    def repository(self):
        return repository_for(self.binding)

    @property
    def task_id(self):
        # The work id is fixed at birth and kept by every continuation.
        execution = self.validated().get('execution') or {}
        if execution.get('taskId'):
            return execution['taskId']
        if execution:
            predecessor = self.repository.get_attempt(execution['attemptId'])
            if predecessor and predecessor.get('taskId'):
                return predecessor['taskId']
        return 'project-worker:' + self.binding['assignmentId']


# A dispatch birth's command id is ``project-dispatch:<assignment>``; storage
# names its output Turn's presentation after it (service._dispatch).
DISPATCH_OUTPUT_PRESENTATION = ('project-dispatch:', ':output')


def turn_dispatch(conversation_id, turn, accepted_config, *, user_id):
    """The grant a new Attempt of ``turn`` runs under, or ``None`` for an ordinary Turn.

    A dispatched Attempt carries its grant in its accepted config. An Attempt
    that ran unbound (a browser Continue accepted before the UI path bound
    Worker Turns) does not, yet its Turn is still the assignment's bound output
    Turn: the birth's presentation locates the assignment, and only an
    association naming this exact Turn of this recipient conversation lends its
    grant. A locator never grants authority; callers still validate the grant.
    """
    carried = accepted_config.get(KEY) if isinstance(accepted_config, Mapping) else None
    if isinstance(carried, Mapping):
        return TrustedDispatch(deepcopy(dict(carried)))
    prefix, suffix = DISPATCH_OUTPUT_PRESENTATION
    presentation = turn.get('presentationId') if isinstance(turn, Mapping) else None
    if (not isinstance(presentation, str) or not presentation.startswith(prefix)
            or not presentation.endswith(suffix)
            or len(presentation) <= len(prefix) + len(suffix)):
        return None
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.repository import ConversationRepository
    from .repository import ProjectDispatchRepository
    snapshot = ConversationRepository().get(conversation_id, user_id=user_id, include_messages=False)
    metadata = getattr(snapshot, 'metadata', snapshot)
    settings = metadata.get('settings') if isinstance(metadata, Mapping) else None
    project = ((settings.get('canonicalProjectPath') or settings.get('projectPath'))
               if isinstance(settings, Mapping) else None)
    if not isinstance(project, str) or not project.strip():
        return None
    repository = ProjectDispatchRepository(user_id, normalize_project_path(project.strip()))
    assignment_id = presentation[len(prefix):-len(suffix)]
    # Absence means an ordinary Turn; every read failure still propagates.
    assignment = next((row for row in repository.list() if row['id'] == assignment_id), None)
    execution = (assignment or {}).get('execution') or {}
    if (assignment is None or assignment['request']['recipientId'] != conversation_id
            or execution.get('turnId') != turn.get('turnId')):
        return None
    return TrustedDispatch(repository.admission(assignment_id))


def continue_recovered_execution(config, turn_id, attempt_id):
    """Bind or advance the association for an attempt started by recovery.

    Accepted-attempt recovery can claim an attempt whose delegation committed it
    but has not yet bound it. Under that exclusive claim this performs the same
    idempotent bind/exact continuation from the accepted config's server-captured
    carrier, so binding still precedes executor birth. Other configs are untouched.
    """
    if KEY not in config:
        return None
    if not isinstance(config[KEY], dict):
        raise PermissionError('Malformed project dispatch authorization')
    return TrustedDispatch(config[KEY]).continue_execution(turn_id, attempt_id)


def restore_dispatch_identity(config, *, task=None):
    principal, attended, _ = _restored_identity(config, task=task)
    return principal, attended


def _restored_identity(config, *, task=None, admission=None):
    """The restored identity plus the assignment row its one live validation returned.

    With ``admission`` the row and the registered workspace are that admission's;
    only the config fences below run again.
    """
    binding = config.get(KEY)
    if not isinstance(binding, dict):
        raise PermissionError('Malformed project dispatch authorization')
    assignment = (repository_for(binding).validate(binding) if admission is None
                  else admission.assignment)
    runtime_path = config.get('projectPath')
    profile = profile_for(assignment)
    if (config.get('_turnOwnerUserId') != binding['owner']
            or config.get('_turnPrincipalTenantId', '') != (binding['tenant'] or '')
            or config.get('projectPaths') != []
            or config.get('readOnlyPaths') != ([binding['projectPath']] if profile['mode'] == 'consult' else [])
            or config.get('canonicalProjectPath') != binding['projectPath']
            or config.get('executionLimits') != execution_limits(assignment)
            or not _model_admitted(config, assignment)
            or config.get('autopilot') is not False or config.get('planMode') is not False
            or config.get('activeFlow') or config.get('humanGuidanceEnabled') is not asks_human(profile)
            or config.get('autoTranslate') is not False
            or '_conversationExecutionIdentity' in config):
        raise PermissionError('Project dispatch config differs from its accepted grant')
    if runtime_path != binding['projectPath']:
        if task is None or profile['mode'] not in ('files', 'project'):
            raise PermissionError('Project dispatch project path differs from its accepted grant')
        if admission is None:
            owned_workspace(task, binding)
        else:
            admission.registered_workspace(task)
    if profile['mode'] in ('project', 'coordinator'):
        original = assignment['request']['delegation']['policy']['config']
        # humanGuidanceEnabled is fenced exactly above: the shared policy config
        # disables it for Workers, while a Coordinator continuation may ask.
        for key, value in original.items():
            if key not in ('tools', 'humanGuidanceEnabled') and config.get(key) != value:
                raise PermissionError('Project worker capability policy changed after admission')
        expected = (original.get('tools', {}) if profile['mode'] == 'project'
                    else {**original.get('tools', {}), 'programmaticCalling': 'off'})
        if config.get('tools', {}) != expected:
            raise PermissionError('Project worker programmatic policy changed after admission')
    return principal_for(binding), False, assignment


def _model_admitted(config, assignment):
    """The carrier runs its grant's model or one its owner chose for this session.

    An owner's session choice (lib/conversations/session_model.py) is captured
    when an attempt is accepted, so at acceptance the carrier names the
    session's current choice. The owner may choose again while that attempt
    runs; its bound execution's durable accepted config still names the model
    it was accepted with, so the running call keeps its authority. Evidence
    comes only from durable owner and storage records, never from the carrier
    itself, so a carrier rewritten to any other model loses its authority.
    Storage is read only when the model differs from the grant's.
    """
    model = config.get('model')
    if model == assignment['request']['model']:
        return True
    # Only the grants capture() applies a session choice to may run one.
    if (not isinstance(model, str) or not model
            or profile_for(assignment)['mode'] not in ('project', 'coordinator')):
        return False
    binding = config[KEY]
    if model == session_model(binding['recipientId'], owner=binding['owner']):
        return True
    attempt = (assignment.get('execution') or {}).get('attemptId')
    if not attempt:
        return False
    accepted = repository_for(binding).accepted_attempt_config(attempt)
    return isinstance(accepted, Mapping) and accepted.get('model') == model


def is_file_dispatch_task(task, *, admission=None):
    """Only descriptor-confined file grants use the fixed first-party handler."""
    if not is_dispatch_task(task):
        return False
    _, assignment = validated_dispatch(task, admission=admission)
    return profile_for(assignment)['mode'] == 'files'


def is_dispatch_task(task):
    payload = task.get('_principalContext') or {}
    return (KEY in (task.get('config') or {}) or '_projectDispatchBinding' in task
            or isinstance(payload, dict) and payload.get('kind') == 'system'
            and str(payload.get('subject_id', '')).startswith('project-dispatch:'))


def validate_task(task, *, require_account=True, admission=None):
    """Re-establish the bound authority; a failure also stops the task with its cause."""
    validated = validated_dispatch(task, require_account=require_account, admission=admission)
    return None if validated is None else validated[0]


def validated_dispatch(task, *, require_account=True, admission=None):
    """``validate_task`` that also returns the assignment row its single validation read.

    The row serves only the caller's current admission (its immutable request and
    execution association); it is never retained, so every later admission
    validates live authority again. ``admission`` is the ``DispatchAdmission``
    this same admission already made: deciding from it rechecks the task's
    in-memory carrier, identity and account and reads no storage. Without one,
    or for a task or carrier it did not validate, the grant is validated live.
    """
    if not is_dispatch_task(task):
        return None
    try:
        if admission is not None and admission.covers(task):
            return _validated_binding(task, require_account=require_account, admission=admission)
        with timing_phase('dispatch_authority'):
            return wait_for_dispatch(task,
                lambda: _validated_binding(task, require_account=require_account),
                label='project dispatch authority', effect_free=True)
    except AbortedError as error:
        stop_on_authority_failure(task, error)
        raise


def admit_dispatch(task):
    """One live validation of ``task``'s grant, shared by every gate of this admission.

    Returns the ``DispatchAdmission`` (``None`` for a task without dispatch
    authority); a failure stops the task with its cause, as ``validated_dispatch``.
    """
    validated = validated_dispatch(task)
    if validated is None:
        return None
    binding, assignment = validated
    return DispatchAdmission(dispatch_root(task), binding, assignment)


def _validated_binding(task, *, require_account, admission=None):
    """``(binding, assignment)`` of one validation.

    ``admission`` (one that covers ``task``) supplies the row, registered
    workspace and bound-executor verdict; every in-memory fence below still runs.
    """
    config = task.get('config') or {}
    if not is_dispatch_task(task):
        return None
    root = dispatch_root(task)
    if root is not task:
        return _validated_binding(root, require_account=require_account, admission=admission)
    if task.get('aborted'):
        raise DispatchTaskInterrupted(task.get('_abort_reason'))
    if KEY not in config:
        raise AbortedError('Project dispatch lost its accepted grant carrier')
    try:
        # The validated row is current evidence: its association is immutable once
        # bound, and binding precedes executor birth, so a reread cannot differ.
        principal, attended, assignment = _restored_identity(config, task=task, admission=admission)
    except Exception as error:
        raise AbortedError(f'Project dispatch authority is no longer available: {error}') from error
    # A stop (restart quiesce, owner Stop, fence) can also land while the identity
    # is restored from storage (the 2026-11-13 04:28 calls); it keeps its own cause.
    if task.get('aborted'):
        raise DispatchTaskInterrupted(task.get('_abort_reason'))
    execution = assignment['execution']
    if (execution is None or task.get('_turnId') != execution['turnId']
            or task.get('_attemptId') != execution['attemptId']):
        raise AbortedError('Project dispatch execution does not match its immutable birth association')
    if task.get('_principalContext') != principal.to_payload():
        raise DispatchIdentityMismatch(
            'principal', 'the task principal differs from the principal its grant admitted')
    if task.get('_attended') is not attended:
        raise DispatchIdentityMismatch(
            'attended', f'the task is attended={task.get("_attended")!r}, its grant admits '
                        f'attended={attended!r}')
    if task.get('convId') != config[KEY]['recipientId']:
        raise DispatchIdentityMismatch(
            'recipient', f'the task runs in conversation {task.get("convId")!r}, not its grant '
                         f'recipient {config[KEY]["recipientId"]!r}')
    if task.get('_parentTaskId'):
        raise DispatchIdentityMismatch(
            'parent_task', f'a child task (parent {task["_parentTaskId"]!r}) holds no grant of its '
                           'own; only the bound Worker task does')
    # An admission validated its bound executor live (admit_dispatch requires the account).
    if require_account and admission is None:
        repository = repository_for(config[KEY])
        if repository.attempt_get is None:
            from lib.turn_lifecycle import get_attempt
            attempt = get_attempt(execution['attemptId'], user_id=config[KEY]['owner'])
        else:
            attempt = repository.attempt_get(execution['attemptId'], user_id=config[KEY]['owner'])
        if (not isinstance(attempt, dict) or attempt.get('taskId') != task.get('id')
                or attempt.get('attemptId') != execution['attemptId']
                or attempt.get('turnId') != execution['turnId']
                or attempt.get('conversationId') != config[KEY]['recipientId']
                or attempt.get('status') not in ('pending', 'running')):
            raise AbortedError('Project dispatch physical task is not the current bound executor')
    if require_account and (task.get('_executionBudget') is None
            or id(task['_executionBudget']) != task.get('_projectDispatchAccountId')):
        raise AbortedError('Project dispatch requires its bound conserved execution account')
    return config[KEY], assignment


def granted_coordinator_scope(task):
    """``(bound, scope)``: the Coordinator scope the grant ``bind_account`` validated fixed.

    ``bound`` is False for a task that never bound its account. The scope (None
    for a Worker, consultation or file grant) comes from request and grant
    fields no mutation can change, so it reads no storage; live authority stays
    with the admissions that fence effects (model dispatch intents, tool
    admission). A carrier that no longer matches the bound grant is lost
    authority and stops the task.
    """
    role = task.get(ROLE_KEY)
    if role is None:
        return False, None
    if (task.get('config') or {}).get(KEY) != role['binding']:
        error = AbortedError('Project dispatch lost its accepted grant carrier')
        stop_on_authority_failure(task, error)
        raise error
    return True, (None if role['coordinatorScope'] is None else dict(role['coordinatorScope']))


def _granted_role(binding, assignment):
    from .scope import coordinator_grant_scope
    return {'binding': deepcopy(binding), 'coordinatorScope': coordinator_grant_scope(assignment),
            'mode': profile_for(assignment)['mode']}


def bind_account(task):
    validated = validated_dispatch(task, require_account=False)
    if validated is None:
        return
    binding, validated_row = validated

    task['_projectDispatchBinding'] = deepcopy(binding)
    task[ROLE_KEY] = _granted_role(binding, validated_row)
    from lib.agent_core.execution_budget import _ensure_execution_budget
    budget = _ensure_execution_budget(task)

    from .accounting import holder_consumption
    assigned = wait_for_dispatch(task,
        lambda: repository_for(binding).read(binding['assignmentId']),
        label='project dispatch account', effect_free=True)
    # The checkout and the occurrence ledger belong to the assignment. Each
    # physical attempt is its own durable-worker task, so every attempt borrows
    # the association's work id: it keys the checkout registration and is the
    # ledger's root, and a resumed attempt continues both.
    from lib.tasks_pkg.workspace_delivery import WORKSPACE_WORK_ID_KEY
    work_id = repository_for(binding).workspace_task_id(assigned)
    task[WORKSPACE_WORK_ID_KEY] = work_id

    # A Coordinator follow-up starts a new Turn, so storage issues it no
    # _resumeSource; its accepted witness names the settled predecessor instead.
    config = task.get('config') or {}
    follow_up = config.get(FOLLOW_UP_KEY) if not config.get('_resumeSource') else None
    resume = config.get('_resumeSource') or follow_up or {}
    account_nonce = next((row['account_nonce'] for row in assigned.get('modelAttempts', [])
                          if row.get('account_nonce')), None) if resume else None
    if account_nonce is None and resume:
        account_nonce = (assigned.get('toolReservations') or {}).get('accountNonce')
    if account_nonce is None:
        import uuid
        account_nonce = uuid.uuid4().hex
    shared_id = assigned['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId')
    limited = not unbounded_dispatch(assigned)
    # Every successor Attempt of the bound Turn continues the assignment's one
    # account, whatever settled its predecessor. task_start supplies the
    # authority-issued _resumeSource through restore_accepted_attempt_config.
    execution = assigned.get('execution') or {}
    continued = bool(resume.get('attemptId')) and resume.get('attemptId') != execution.get('attemptId')
    if continued:
        from lib.turn_lifecycle import LIVE_ATTEMPT_STATUSES
        predecessor = repository_for(binding).get_attempt(resume['attemptId'])
        if (not isinstance(predecessor, Mapping)
                or predecessor.get('attemptId') != resume['attemptId']
                or predecessor.get('taskId') != resume.get('taskId')
                or predecessor.get('turnId') != (resume.get('turnId') if follow_up else execution.get('turnId'))
                or predecessor.get('conversationId') != assigned['request']['recipientId']
                or not predecessor.get('status') or predecessor['status'] in LIVE_ATTEMPT_STATUSES
                or predecessor.get('status') != resume.get('status')):
            raise AbortedError('Project dispatch %s source differs from its settled predecessor'
                               % ('follow-up' if follow_up else 'resume'))
        task['_interruptedToolEffects'] = settle_interrupted_tools(task, binding, work_id, assigned,
                                                                   fold=not limited)

    def observe_reservation(event):
        # A reservation only advances a usage counter and precedes no effect
        # of its own: the tool intent that follows is the effect fence (intent
        # CAS plus one live validation), so the reservation validates nothing.
        def record(row):
            counter = row['toolReservations']
            if (counter['rootTaskId'] != work_id or counter['accountNonce'] != account_nonce
                    or event['reservation_ordinal'] not in (counter['count'], counter['count'] + 1)):
                raise AbortedError('Worker tool reservation differs from its physical occurrence')
            counter['count'] = event['reservation_ordinal']
        wait_for_dispatch(task,
            lambda: repository_for(binding).mutate(binding['assignmentId'], record),
            label='project dispatch tool reservation')

    reservations = 0
    if shared_id:
        def register_counter(row):
            counter = {'rootTaskId': work_id, 'accountNonce': account_nonce, 'count': 0}
            stored = row.get('toolReservations')
            if stored is None:
                row['toolReservations'] = counter
            elif stored != counter and not (continued and {**stored, 'count': 0} == counter):
                raise AbortedError('Worker tool reservations cannot be replenished')
        registered = wait_for_dispatch(task,
            lambda: repository_for(binding).mutate(binding['assignmentId'], register_counter),
            label='project dispatch reservation account')
        reservations = registered['toolReservations']['count']
    def observe(event):

        event = {**event, 'root_task_id': work_id, 'account_nonce': account_nonce}
        repository = repository_for(binding)
        # One live validation per model dispatch, before its intent, so a refused
        # dispatch records nothing. The intent CAS refuses a revoked or revised
        # grant; after it, the same admission rechecks the in-memory carrier,
        # identity and account (no storage), so a Stop or identity loss during
        # the intent's wait is still refused before the provider sees the request.
        decided = admit_dispatch(task) if event['phase'] == 'dispatch_intent' else None
        def record(assignment):
            from .accounting import record_model_event, fold_settled_models, folded_model_event
            if folded_model_event(assignment, event):
                return assignment
            if event['phase'] == 'dispatch_intent' and (assignment['grant']['revoked']
                    or assignment['grant']['revision'] != binding['revision']):
                from .repository import DispatchGrantRevoked
                raise DispatchGrantRevoked('Project dispatch revocation refuses a new request')
            attempts = assignment['modelAttempts']
            if event['phase'] == 'dispatch_intent' and limited and event not in attempts:
                # Earlier finite grants keep their recorded ceiling.
                # An exact intent replay after a lost ACK is not a new request.
                policy = assignment['request']['budget']
                if (len(attempts) >= policy['maxModelRequests']
                        or sum(row['reported_tokens'] for row in attempts) + event['reserved_tokens'] > policy['maxTokens']
                        or any(not row['usage_complete'] and row['root_task_id'] == event['root_task_id']
                               and row['phase'] != 'dispatch_intent' for row in attempts)):
                    raise AbortedError('Model allowance exhausted or prior consumption is unconfirmed')
            record_model_event(attempts, event)
            if not limited:
                fold_settled_models(assignment)
            return assignment
        try:
            wait_for_dispatch(task,
                lambda: repository.mutate(binding['assignmentId'], record),
                label='project dispatch model occurrence',
                cancellable=event['phase'] == 'dispatch_intent')
        except AbortedError:
            raise
        except Exception as error:
            stop_on_authority_failure(task, error)
            from .repository import DispatchGrantRevoked
            message = (str(error) if isinstance(error, DispatchGrantRevoked)
                       else 'Project dispatch usage could not be durably acknowledged')
            raise AbortedError(message) from error
        if decided is not None:
            validated_dispatch(task, admission=decided)
    # ExecutionBudget serializes observer admission/settlement. Only locally
    # live occurrences from this exact account may overlap; an intent whose
    # settlement failed remains durable and blocks further effects.
    active_tools = set()
    # Ordinals whose intent this observer went on to write (a write that raised
    # may still have committed). Until its settlement commits, an ordinal stays
    # here, so a retained settlement is written again.
    written_intents = set()

    def observe_tool(event):
        from .repository import DispatchGrantRevoked
        if event['phase'] == 'intent':
            # The call's own admission decided this effect; recheck the live task only.
            validate_task(task, admission=call_admission(task))
            written_intents.add(event['tool_ordinal'])
        elif event['phase'] == 'not_entered' and event['tool_ordinal'] not in written_intents:
            # Refused before its intent was written: nothing to settle. Touching
            # storage here let a restart that also stops storage turn a call that
            # never started into an unsettled one (drill admission-cut, window A).
            return
        selected = {key: event[key] for key in ('tool_ordinal', 'task_id',
                    'tool_call_id', 'tool_name', 'phase')}
        selected.update(root_task_id=work_id, account_nonce=account_nonce)
        def record(row):
            from .accounting import UNKNOWN_TOOL_PHASES, fold_settled_tools, folded_tool_event
            from .scope import granted_tools
            if folded_tool_event(row, selected):
                return row
            profile = profile_for(row)
            attempts = row.setdefault('toolAttempts', [])
            old = next((item for item in attempts if item['root_task_id'] == selected['root_task_id']
                        and item['tool_ordinal'] == selected['tool_ordinal']), None)
            if old is not None:
                if (old['account_nonce'] != account_nonce or any(old[key] != selected[key]
                        for key in ('task_id', 'tool_call_id', 'tool_name'))):
                    raise AbortedError('Tool settlement belongs to another immutable account occurrence')
                # An unknown-effect settlement yields to the occurrence's own outcome.
                if old['phase'] not in ('intent', *UNKNOWN_TOOL_PHASES) and old != selected:
                    raise AbortedError('Tool occurrence is already terminal')
                old.update(deepcopy(selected))
            elif selected['phase'] == 'intent':
                # Exact invalidation of the shared admission, keyed to this
                # authoritative row: no intent is recorded under a revised grant.
                if row['grant']['revoked'] or row['grant']['revision'] != binding['revision']:
                    raise DispatchGrantRevoked(
                        f'Grant revoked (assignment {row["id"]}, Coordinator conversation '
                        f'{row["request"]["coordinatorId"]})')
                # Distinct refusals: a Coordinator must not read an unconfirmed
                # effect as a call budget (none exists unless maxToolCalls is set).
                if selected['tool_name'] not in granted_tools(profile, assignment=row):
                    raise AbortedError(f"Tool {selected['tool_name']!r} is not in this Worker's grant")
                if profile.get('maxToolCalls') is not None and len(attempts) >= profile['maxToolCalls']:
                    raise AbortedError(f"Tool call limit reached: maxToolCalls={profile['maxToolCalls']}")
                blocking = next((item for item in attempts if item['root_task_id'] != selected['root_task_id']
                                 or item['phase'] == 'intent' and not (
                                     profile['mode'] in ('project', 'coordinator')
                                     and item['account_nonce'] == account_nonce
                                     and item['tool_ordinal'] in active_tools)), None)
                if blocking is not None:
                    raise UnconfirmedToolEffect(blocking)
                attempts.append(deepcopy(selected))
            elif selected['phase'] != 'not_entered':
                raise AbortedError('Cannot settle an unrecorded tool occurrence')
            if not limited:
                from .repository import CONTRACT
                fold_settled_tools(row, CONTRACT['terminalToolPhases'])
            return row
        try:
            wait_for_dispatch(task,
                lambda: repository_for(binding).mutate(binding['assignmentId'], record),
                label='project dispatch tool occurrence',
                cancellable=event['phase'] == 'intent')
        except UnconfirmedToolEffect as refused:
            raise AbortedError(unconfirmed_effect_message(
                refused.row, _producing_turn(task, binding, refused.row),
                repeatable_exempt=not limited)) from None
        except DispatchGrantRevoked as error:
            stop_on_authority_failure(task, error)
            raise AbortedError(f'Project dispatch authority is no longer available: {error}') from error
        finally:
            if event['phase'] != 'intent':
                active_tools.discard(event['tool_ordinal'])
        if event['phase'] != 'intent':
            written_intents.discard(event['tool_ordinal'])
        if event['phase'] == 'intent':
            # The effect fence: one live validation once the intent is durable.
            validate_task(task)
            active_tools.add(event['tool_ordinal'])
    # A finite grant's ledger is its conserved call budget, so it keeps every call.
    # A repeatable call elsewhere opens no intent but keeps the intent's live
    # validation: authority withdrawn inside a batch still stops a read.
    budget.bind_model_attempt_observer(observe, tool_observer=observe_tool,
        tool_reservation_observer=observe_reservation if shared_id else None,
        observe_repeatable_tools=limited,
        repeatable_tool_admission=None if limited else (lambda _actor: validate_task(task)))
    if continued:
        tokens, _, complete = holder_consumption(assigned)
        ordinal = max([0, *[row['attempt_ordinal'] for row in assigned.get('modelAttempts', [])],
                       *assigned.get('settledModelUsage', {}).get('watermarks', {}).values()])
        tool_ordinal = max([0, *[row['tool_ordinal'] for row in assigned.get('toolAttempts', [])],
                            *assigned.get('settledToolUsage', {}).get('watermarks', {}).values()])
        # An account with no recorded occurrence has nothing to continue; it
        # starts like a first attempt, so a finite legacy cap is not refused.
        if tokens or ordinal or tool_ordinal or reservations or not complete:
            budget.restore_model_account(
                root_task_id=task['id'], consumed_tokens=tokens,
                last_attempt_ordinal=ordinal, usage_complete=complete)
            budget.restore_tool_account(root_task_id=task['id'], last_tool_ordinal=tool_ordinal,
                                        last_reservation_ordinal=reservations)
    task['_projectDispatchAccountId'] = id(budget)


class UnconfirmedToolEffect(AbortedError):
    """An earlier occurrence's durable intent fences this effect; ``row`` is that intent."""

    def __init__(self, row):
        self.row = deepcopy(row)
        super().__init__(unconfirmed_effect_message(self.row, None, repeatable_exempt=False))


def unconfirmed_effect_message(row, turn_id, *, repeatable_exempt):
    """The fence refusal: the exact occurrence and how to settle it."""
    from .accounting import tool_operation_id
    operation = tool_operation_id(row)
    target = f'operation_id={operation}' + (f', turn_id={turn_id}' if turn_id else '')
    return ('Prior tool effect remains unconfirmed (not a call budget): %s #%d %s phase=%s; '
            'its completion was never durably recorded, so further effects are refused. '
            'operation_id=%s turn_id=%s. Verify what that call changed, then call '
            'inspect_tool_effect(%s) to acknowledge its unknown outcome; continuing the Turn '
            'also settles it%s.'
            % (row['tool_name'], row['tool_ordinal'], row['tool_call_id'], row['phase'], operation,
               turn_id or 'unknown', target,
               '. Read-only and idempotent calls are not fenced' if repeatable_exempt else ''))


def _producing_turn(task, binding, row):
    """The Turn whose attempt admitted ``row``, or ``None`` when it cannot be read."""
    if row['task_id'] == task.get('id'):
        return task.get('_turnId')
    prefix = 'conversation-attempt:'
    if not row['task_id'].startswith(prefix):
        return None
    try:
        attempt = repository_for(binding).get_attempt(row['task_id'][len(prefix):])
    except Exception:
        logger.debug('[ProjectDispatch] producing Turn of %s unavailable', row['task_id'], exc_info=True)
        return None
    return attempt.get('turnId') if isinstance(attempt, Mapping) else None


def settle_interrupted_tools(task, binding, work_id, assigned, *, fold):
    """Settle the earlier attempts' open tool intents as interrupted, effect unknown.

    Runs once when an attempt continues the account, before it admits any call,
    so every open intent belongs to a settled predecessor. Returns the settled
    calls for the model's notice; none were assumed to have taken effect.
    """
    from .accounting import INTERRUPTED_TOOL_PHASE, fold_settled_tools, tool_operation_id
    from .repository import CONTRACT
    if not any(item['phase'] == 'intent' for item in assigned.get('toolAttempts', [])):
        return []
    settled = []

    def settle(row):
        settled.clear()
        for item in row.get('toolAttempts', []):
            if item['phase'] == 'intent' and item['root_task_id'] == work_id:
                item['phase'] = INTERRUPTED_TOOL_PHASE
                settled.append({'tool_name': item['tool_name'], 'tool_call_id': item['tool_call_id'],
                                'tool_ordinal': item['tool_ordinal'],
                                'operation_id': tool_operation_id(item)})
        if settled and fold:
            fold_settled_tools(row, CONTRACT['terminalToolPhases'])
        return row
    wait_for_dispatch(task,
        lambda: repository_for(binding).mutate(binding['assignmentId'], settle),
        label='project dispatch interrupted tool settlement')
    if settled:
        logger.warning('[ProjectDispatch] continuation settled interrupted tool intents assignment=%s '
                       'task=%s calls=%s', binding['assignmentId'], task.get('id'),
                       ','.join('%s#%d' % (item['tool_name'], item['tool_ordinal']) for item in settled))
    return list(settled)


def acknowledge_tool_effect(task, operation_id):
    """Record the Worker's acknowledgement of one unknown effect in its own ledger.

    Returns the acknowledged occurrence, or ``None`` when ``operation_id`` names
    no unknown occurrence of this task's assignment. Acknowledging again changes
    nothing; a later real outcome of the same occurrence still replaces it.
    """
    if not is_dispatch_task(task):
        return None
    from .accounting import ACKNOWLEDGED_TOOL_PHASE, UNKNOWN_TOOL_PHASES, tool_operation_id
    binding = validate_task(task)
    repository = repository_for(binding)

    def unknown(row):
        return next((item for item in row.get('toolAttempts', [])
                     if (item['phase'] == 'intent' or item['phase'] in UNKNOWN_TOOL_PHASES)
                     and tool_operation_id(item) == operation_id), None)
    current = unknown(reread_on_contention(task, lambda: repository.read(binding['assignmentId']),
                                           label='project dispatch tool ledger', effect_free=True))
    if current is None:
        return None
    if current['phase'] != 'intent':
        return deepcopy(current)
    acknowledged = []

    def acknowledge(row):
        acknowledged.clear()
        item = unknown(row)
        if item is not None:
            if item['phase'] == 'intent':
                item['phase'] = ACKNOWLEDGED_TOOL_PHASE
            acknowledged.append(deepcopy(item))
        return row
    wait_for_dispatch(task,
        lambda: repository.mutate(binding['assignmentId'], acknowledge),
        label='project dispatch tool acknowledgement')
    if acknowledged:
        logger.warning('[ProjectDispatch] unknown tool effect acknowledged assignment=%s call=%s#%d',
                       binding['assignmentId'], acknowledged[0]['tool_name'],
                       acknowledged[0]['tool_ordinal'])
    return acknowledged[0] if acknowledged else None
