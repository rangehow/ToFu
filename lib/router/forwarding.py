"""Forwarded human requests: route_to_project and its verification (docs/ROUTER.md R2, R4).

``route_to_project`` hands one owner message of the Global Router's current
request, or the frozen owner text of a standing instruction, to the Project
Router (bound Project Coordinator) of a project, byte for byte, through the
ordinary turn command: ``dispatch_scheduled_turn`` steers a running target at
its next round, starts an idle one and queues when the steer window closed.
The input carries ``origin = {initiator: 'human', forwardedFrom:
{conversationId, turnId[, steerId | instructionId]}}`` and the verified source's
canonical uploaded-image links and durable media refs; ``require_human_turn``
admits it only through ``verify_forwarded_turn``. An owner message steered into
the router's running reply is named by ``steerId`` (its user-steer record in
that reply Turn, whose committed author is the authority); ``owner_steer_carriers``
labels such a message with its steer id in the router's model view. The router's
note and an instruction's triggering event are nonhuman evidence **inside that
same input**: ``content`` is the owner's text followed by the labelled evidence,
``originalContent`` keeps the owner's verbatim words, which is exactly what
``authored_text`` and the forward verification compare. A second input would
become its own synthetic Coordinator request whenever the steer window had
closed and re-attempt the work with no authority. A deterministic command id per
(router conversation, source, project) makes every retry deliver at most once:
a repeated forward, with any note, delivers nothing and answers
``already_forwarded`` (the target conversation replays its earlier acceptance).
The portfolio review (R10) applies a standing instruction through the same
resolution and delivery (``apply_standing_instruction``) but queues behind a
busy Project Router instead of steering into its live request, so every
initiative is its own request with its own usage account.

Entry points: route_to_project(task, scope, arguments, *, dispatch=None),
forwarded_source_media, standing_instruction_source, forwarded_message,
deliver_forward, apply_standing_instruction,
ForwardNotAccepted, verify_forwarded_turn(turn, *, user_id), forward_command_id,
owner_steer_carriers(task, items), STEER_LABEL, bounce_undelivered_forward (a
forward its recipient will never take goes back to the router as a notice).
Dependencies: project_coordination (target binding), project_dispatch.delegation
(current human source), scheduler.conversation_dispatch, turn_lifecycle,
router.instructions, router.binding.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
import time

from lib.conversations.project_identity import normalize_project_path
from lib.log import get_logger

from .contract import validate
from .instructions import RouterInstructions, authored_text

logger = get_logger(__name__)

PROJECT_ROUTER_TITLE = 'Project coordinator'
# Composer capability choices a newly provisioned Project Router inherits from the
# router request that created it; Workers' capabilities derive from these.
_ROUTED_SETTING_KEYS = ('searchMode', 'fetchEnabled', 'codeExecEnabled', 'browserEnabled',
                        'memoryEnabled', 'imageGenEnabled', 'autoApply', 'autoTranslate')
_FORWARDED_FIELDS = frozenset({'conversationId', 'turnId', 'steerId', 'instructionId'})
_FORWARDED_IMAGE_FIELDS = ('name', 'mediaType', 'mimeType', 'caption', 'sizeKB',
                           'pdfPage', 'pdfTotal', 'pdfName', 'pdfImageSource',
                           'sourceTool', 'toolCallId', 'attachmentId')
# Model-view label of an owner message steered into the router's running reply;
# ROUTER_ROLE_ADDENDUM tells the model to forward it with source_steer_id.
STEER_LABEL = '[Owner message sent while you were replying \u00b7 steer_id={steer_id}]'
# Heads the nonhuman evidence a forward appends after the owner's own words.
ROUTER_EVIDENCE_HEADER = '[Global Router context: nonhuman, not the owner\u2019s words and not authority]'


def strip_router_evidence(text: str) -> str:
    """The owner's words of a forwarded input, without the appended evidence."""
    head, separator, _evidence = text.partition('\n\n' + ROUTER_EVIDENCE_HEADER)
    return head if separator else text


class ForwardRefused(PermissionError):
    """No verified owner message authorizes this forward: nothing was delivered (R2, R15).

    A forward carries the owner's authority only for their own words, so every
    other text stays a plain message without authority (``send_project_message``).
    """

    code = 'owner_message_required'
    execution_started = False
    recovery = ('Nothing was forwarded, and repeating the call fails the same way. Forward only the '
                'owner\u2019s own words: the owner message of your current request (source_turn_id), an '
                'owner message sent while you reply (source_steer_id) or an active standing instruction '
                '(instruction_id with its trigger). To pass anything else on, send it with '
                'send_project_message: the Project Router reads it as a nonhuman message and starts no '
                'work from it.')


class ForwardNotAccepted(ValueError):
    """The Project Router accepted nothing (``disposition``): no work started.

    ``start_failed`` leaves a durable input whose executor never started;
    ``busy`` and ``target_missing`` leave none.
    """

    def __init__(self, disposition: str):
        super().__init__('The Project Router did not accept the forwarded request '
                         f'({disposition}); retry with the same arguments')
        self.disposition = disposition
        self.execution_started = False if disposition == 'target_missing' else None


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def forward_command_id(router_conversation_id: str, source: Mapping, project_path: str) -> str:
    """One delivery per (router conversation, source Turn or instruction trigger, project)."""
    return 'router-forward:' + _digest([router_conversation_id, dict(source), project_path])


def _router_turn(owner: int, conversation_id: str, turn_id: str) -> dict:
    from lib.turn_lifecycle import LifecycleNotFound, get_turn
    try:
        turn = get_turn(conversation_id, turn_id, user_id=owner)
    except LifecycleNotFound as error:
        raise ValueError(f'Turn {turn_id} does not exist in Global Router conversation '
                         f'{conversation_id}') from error
    if (not isinstance(turn, Mapping) or turn.get('conversationId') != conversation_id
            or turn.get('turnId') != turn_id):
        raise ValueError('Global Router Turn identity changed')
    return dict(turn)


def _current_request_source(task: dict, scope: dict) -> dict:
    """The genuine human Turn of the router's current request, as delegation sees it."""
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import ProjectDispatchRepository
    _, source, _ = source_for_task(ProjectDispatchRepository(scope['user_id'], scope['project_path']),
                                   task)
    if 'steerId' in source:
        # The request is an owner message steered into this reply, which has no
        # human Turn of its own: its words are forwarded by their steer.
        raise PermissionError('Your current request is the owner\u2019s message steered into this reply '
                              f'(steer_id={source["steerId"]}); forward it with source_steer_id')
    return source


def _current_steer_source(task: dict, scope: dict, steer_id: str) -> dict:
    """The owner message ``steer_id`` steered into the router's current reply.

    The physical fences match delegation's source resolution (this task is the
    executing attempt of the current router binding and was not stopped), but
    the authority is the steer's own committed author, so an owner message
    steered into a reply that a channel event or report started still counts.
    Returns the steer's Turn view (``steered_input_turn``).
    """
    from lib.project_dispatch.repository import ProjectDispatchRepository, stopped_after
    from lib.turn_initiation import require_human_turn, steered_input_turn
    repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    if task.get('aborted') or task.get('abort_event') and task['abort_event'].is_set():
        raise PermissionError('Forwarding requires the current executing Global Router request')
    repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
    turn = repository.turn_get(scope['conversation_id'], task.get('_turnId'), user_id=repository.owner)
    attempt = repository.get_attempt(task.get('_attemptId'))
    if (not isinstance(turn, dict) or turn.get('conversationId') != scope['conversation_id']
            or turn.get('turnId') != task.get('_turnId')
            or not isinstance(attempt, dict) or attempt.get('status') not in ('pending', 'running')
            or attempt.get('attemptId') != task.get('_attemptId') or attempt.get('taskId') != task.get('id')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != turn['conversationId']):
        raise PermissionError('A steered message is forwardable only from your current reply')
    if stopped_after(repository.owner, scope['conversation_id'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('The current Global Router request was stopped')
    from lib.project_dispatch.continuation import current_continuation
    continued = current_continuation(task, require_account=True)
    source = continued['request']['authorizationSource'] if continued is not None else {}
    if source.get('steerId') == steer_id:
        # An automatic continuation keeps its request's attribution (R15): the owner message
        # that request came from was steered into the request's own reply Turn.
        turn = repository.turn_get(scope['conversation_id'], source['turnId'], user_id=repository.owner)
    try:
        steered = steered_input_turn(turn, steer_id)
        origin = steered['projection'].get('origin')
        if isinstance(origin, Mapping) and 'forwardedFrom' in origin:
            raise ValueError('it was itself forwarded')
        require_human_turn(steered)
    except ValueError as error:
        raise PermissionError(f'Steered message {steer_id} cannot be forwarded as the owner\u2019s own '
                              f'words: {error}') from error
    return steered


def _target_project(scope: dict, raw_path: str) -> str:
    project = normalize_project_path(raw_path.strip())
    if not os.path.isabs(project) or not os.path.isdir(project):
        raise ValueError('project_path must be an existing absolute local project directory')
    if project == scope['project_path']:
        raise ValueError('The router home is not a project; delegate home work with '
                         'delegate_project_work')
    from lib.router.watchdog.binding import is_watchdog_location
    if is_watchdog_location(scope['user_id'], project):
        raise ValueError('The Watchdog is not a project and receives no forwarded requests')
    return project


def _ensure_project_router(owner: int, project: str, config: Mapping, *,
                           folder_id: str | None = None) -> tuple[dict, bool]:
    """Create-or-return the one Project Router of ``project``; every project create uses it too (R12).

    Provisioning derives the conversation from owner, path, binding revision and
    this command id, so forwards and project creates, repeated or concurrent,
    converge on one Router. ``folder_id`` files it in the owner's project.
    """
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    bindings = ProjectCoordinationRepository(owner)
    binding = bindings.read(project)
    if binding['availability'] == 'available':
        return binding, False
    settings = {key: config[key] for key in _ROUTED_SETTING_KEYS if key in config}
    if folder_id:
        settings['folderId'] = folder_id
    return bindings.provision(project, title=PROJECT_ROUTER_TITLE, settings=settings,
                              command_id='route-to-project'), True


def _evidence_text(router_id: str, forwarded: dict, note: str | None, trigger: dict) -> str | None:
    if not forwarded.get('instructionId') and not note:
        return None
    lines = [ROUTER_EVIDENCE_HEADER]
    if forwarded.get('instructionId'):
        lines.append(f'The owner request above applies standing instruction {forwarded["instructionId"]} '
                     f'(owner Turn {forwarded["turnId"]} of Global Router conversation {router_id}).')
        if trigger.get('event_id'):
            lines.append(f'Triggering channel event: {trigger["event_id"]}.')
        if trigger.get('source_turn_id'):
            lines.append(f'Triggering Global Router Turn: {trigger["source_turn_id"]}.')
    elif forwarded.get('steerId'):
        lines.append(f'The owner request above was forwarded from Global Router conversation '
                     f'{router_id}: the owner sent it while the router was replying in Turn '
                     f'{forwarded["turnId"]} (steered message {forwarded["steerId"]}).')
    else:
        lines.append(f'The owner request above was forwarded from Global Router conversation '
                     f'{router_id}, Turn {forwarded["turnId"]}.')
    if note:
        lines.append('Router note (evidence):\n' + note)
    return '\n'.join(lines)


def _forwarded_image(image: Mapping) -> dict | None:
    """One stored uploaded-image link; inline payloads stay with their source Turn."""
    from lib.attachments import canonical_image_ref
    link = ''
    for key in ('url', 'preview'):
        value = image.get(key)
        if isinstance(value, str):
            link = canonical_image_ref(value)
            if link:
                break
    if not link:
        return None
    projected = {'preview': link, 'url': link}
    for key in _FORWARDED_IMAGE_FIELDS:
        value = image.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            projected[key] = value
    return projected


def forwarded_source_media(owner: int, router_id: str,
                           forwarded: Mapping) -> tuple[list[dict], list[dict]]:
    """Canonical image links and media refs of a verified forwarded owner message.

    The Global Router source Turn is re-read owner-scoped; a steered owner
    message is judged again by its committed author. Only stored upload links
    and canonical durable attachment refs are copied: no inline bytes, no note
    text and no media from a conversation other than this Global Router.
    """
    if (not isinstance(forwarded, Mapping) or forwarded.get('conversationId') != router_id
            or not isinstance(forwarded.get('turnId'), str) or not forwarded['turnId']):
        raise ValueError('Forwarded media does not come from this Global Router')
    source = _router_turn(owner, router_id, forwarded['turnId'])
    projection = source.get('projection') or {}
    if 'steerId' in forwarded:
        from lib.turn_initiation import STEER_BLOCK_PREFIX, steered_input_turn
        steer_id = forwarded['steerId']
        if not isinstance(steer_id, str) or not steer_id:
            raise ValueError('Malformed forwarded steer identity')
        steered_input_turn(source, steer_id)
        previews = [preview for record in projection.get('_userSteerInjects') or ()
                    if isinstance(record, Mapping) and record.get('commandId') == steer_id
                    and record.get('blockId') == STEER_BLOCK_PREFIX + steer_id
                    for preview in record.get('previews') or () if isinstance(preview, Mapping)]
        raw_images = [image for preview in previews for image in preview.get('images') or ()]
        raw_attachments = [item for preview in previews
                           for item in preview.get('attachments') or ()]
    else:
        from lib.turn_initiation import require_human_turn
        require_human_turn(source, user_id=owner)
        raw_images = projection.get('images') or ()
        raw_attachments = projection.get('attachments') or ()
    images = []
    for item in raw_images:
        image = _forwarded_image(item) if isinstance(item, Mapping) else None
        if image is not None:
            images.append(image)
    attachments = [dict(item) for item in raw_attachments
                   if isinstance(item, Mapping) and isinstance(item.get('attachmentId'), str)
                   and item['attachmentId'].strip()]
    return images, attachments


def forwarded_message(text: str, forwarded: dict, *, note: str | None, trigger: dict, router_id: str,
                      images: list[dict] | None = None,
                      attachments: list[dict] | None = None) -> dict:
    """The one input of a forward: the owner's verbatim words, then the labelled nonhuman evidence.

    Every forward builds it here: to a Project Router (``deliver_forward``) and
    to a repair conversation (``lib.router.project_repair``). The owner's own
    uploaded images and durable media refs ride the same input when supplied.
    """
    evidence = _evidence_text(router_id, forwarded, note, trigger)
    message = {'role': 'user', 'origin': {'initiator': 'human', 'forwardedFrom': forwarded}}
    if evidence is None:
        message['content'] = text
    else:
        # One Coordinator request, not two: the owner's verbatim words stay the
        # authored text that require_human_turn/verify_forwarded_turn compare,
        # while the model reads them together with the nonhuman evidence. A
        # second input would become its own synthetic Coordinator request
        # whenever the steer window had closed, re-attempting this work with no
        # authority (docs/ROUTER.md R2 "Evidence rides the same request").
        message['content'] = text + '\n\n' + evidence
        message['originalContent'] = text
    if images:
        message['images'] = list(images)
    if attachments:
        message['attachments'] = list(attachments)
    return message


def standing_instruction_source(owner: int, router_id: str, instruction_id: str, project: str, trigger: dict):
    """Resolve a standing instruction into the owner's frozen text (R4).

    The only authority resolution shared by route_to_project and the scheduled
    portfolio review: an active instruction of the current Global Router, scoped
    to this project, whose source human Turn is unchanged. Without an owner-named
    project it applies only where a Project Router already exists. Returns
    (text, forwarded, command_id).
    """
    from lib.turn_initiation import require_human_turn
    instruction = RouterInstructions(owner).read(instruction_id)
    if instruction['state'] == 'paused':
        raise PermissionError(f'Standing instruction {instruction_id} is paused by the owner and cannot be '
                              'applied until the owner resumes it in Tofu; do not forward this event '
                              'under it')
    if instruction['state'] != 'active':
        raise PermissionError('This standing instruction was cancelled')
    if instruction['routerConversationId'] != router_id:
        raise PermissionError('This standing instruction belongs to an earlier Global Router')
    if instruction['projectPath'] not in (None, project):
        raise PermissionError(f'This standing instruction targets {instruction["projectPath"]}')
    if instruction['projectPath'] is None:
        # A trigger may suggest the target: without an owner-named project,
        # apply only where the owner already has a Project Router.
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        if ProjectCoordinationRepository(owner).read(project)['availability'] != 'available':
            raise PermissionError('This standing instruction names no project; it applies only to '
                                  'projects that already have a Project Router')
    source_turn = _router_turn(owner, router_id, instruction['sourceTurnId'])
    require_human_turn(source_turn)
    if authored_text(source_turn) != instruction['text']:
        raise PermissionError('The standing instruction source Turn changed')
    if 'source_turn_id' in trigger:
        _router_turn(owner, router_id, trigger['source_turn_id'])
    forwarded = {'conversationId': router_id, 'turnId': instruction['sourceTurnId'],
                 'instructionId': instruction_id}
    command_id = forward_command_id(router_id, {'instructionId': instruction_id, **trigger}, project)
    return instruction['text'], forwarded, command_id


def deliver_forward(owner: int, project: str, text: str, forwarded: dict, *, command_id: str | None = None,
                    note: str | None,
                    trigger: dict, router_id: str, instruction_id: str | None, config: Mapping,
                    dispatch=None, inject_mode: str = 'steer') -> dict:
    """Deliver one verified owner text and its nonhuman evidence to a Project Router.

    ``inject_mode='steer'`` hands a busy Project Router the request at its next
    step (the owner's own message); ``'queue'`` waits for its live request to
    end, so the forward opens a request of its own (``input_turn_id``). A
    forward whose command id the Project Router already accepted reports that
    acceptance with ``already_forwarded`` and points new evidence to
    send_project_message: the repeat delivered nothing, its note included.
    """
    target, created = _ensure_project_router(owner, project, config)
    if command_id is None:
        # R2 identity: (router, source Turn or instruction trigger, project).
        # forwardedFrom carries conversationId and (optionally) instructionId,
        # which are not part of the identity and must not change it.
        source = {name: forwarded[name] for name in ('turnId', 'steerId') if name in forwarded}
        if instruction_id is not None:
            source = {'instructionId': instruction_id, **trigger}
        command_id = forward_command_id(router_id, source, project)
    images, attachments = forwarded_source_media(owner, router_id, forwarded)
    message = forwarded_message(text, forwarded, note=note, trigger=trigger, router_id=router_id,
                                images=images, attachments=attachments)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    outcome = dispatch(
        target['conversationId'], message, {}, user_id=owner, command_id=command_id,
        log_prefix='[Router]', input_actor='human', inject_mode=inject_mode)
    if not outcome.accepted:
        raise ForwardNotAccepted(outcome.disposition)
    result = {'project_path': project, 'coordinator_conversation_id': target['conversationId'],
              'binding_revision': target['revision'], 'project_router_created': created,
              'disposition': outcome.disposition, 'forwarded_from': forwarded,
              'context_disposition': None, 'command_id': command_id,
              'input_turn_id': outcome.input_turn_id or None,
              'already_forwarded': outcome.replayed}
    if instruction_id is not None:
        application = RouterInstructions(owner).record_application({
            'version': 1, 'owner': owner, 'instructionId': instruction_id,
            'applicationId': 'ra-' + _digest(command_id)[:32], 'projectPath': project,
            'coordinatorConversationId': target['conversationId'],
            'eventId': trigger.get('event_id'), 'triggerTurnId': trigger.get('source_turn_id'),
            'note': note, 'commandId': command_id,
            'disposition': outcome.disposition, 'createdAt': int(time.time() * 1000)})
        result['instruction_application_id'] = application['applicationId']
    if outcome.replayed:
        result['next_step'] = ('This forward already reached the Project Router earlier, so this call '
                               'delivered nothing, its note included. Do not forward the same message '
                               'again: pass new evidence with send_project_message, which the Project '
                               'Router reads as a nonhuman message.')
    else:
        result['next_step'] = ('The Project Router now owns this work and its Workers; its reports reach '
                               'you as nonhuman notices. Do not delegate or forward the same work again; '
                               'follow it with get_router_projects or get_conversation on the Project '
                               'Router.')
    return result


def apply_standing_instruction(owner: int, router_id: str, project: str, instruction_id: str, trigger: dict,
                               note: str, *, dispatch=None, inject_mode: str = 'steer') -> dict:
    """Apply a standing instruction outside a router tool call (the portfolio review).

    Identical checks and delivery to route_to_project's instruction path; the
    opportunity's problem and expected outcome travel as nonhuman evidence, never
    inside the owner's text.
    """
    text, forwarded, command_id = standing_instruction_source(owner, router_id, instruction_id, project, trigger)
    return deliver_forward(owner, project, text, forwarded, command_id=command_id, note=note, trigger=trigger,
                           router_id=router_id, instruction_id=instruction_id, config={}, dispatch=dispatch,
                           inject_mode=inject_mode)


def route_to_project(task: dict, scope: dict, arguments: dict, *, dispatch=None) -> dict:
    """Forward the owner's exact words to a Project Router; never executes work itself."""
    arguments = validate(dict(arguments), 'routeToProject')
    owner, router_id = scope['user_id'], scope['conversation_id']
    project = _target_project(scope, arguments['project_path'])
    instruction_id = arguments.get('instruction_id')
    steer_id = arguments.get('source_steer_id')
    trigger = {key: arguments[key] for key in ('event_id', 'source_turn_id') if arguments.get(key)}
    if instruction_id is None and steer_id is not None:
        if trigger:
            raise ValueError('source_steer_id names the whole source; omit source_turn_id and event_id')
        source = _owner_source(lambda: _current_steer_source(task, scope, steer_id))
        text = authored_text(source)
        forwarded = {'conversationId': router_id, 'turnId': source['turnId'], 'steerId': steer_id}
    elif instruction_id is None:
        if 'source_turn_id' not in trigger:
            raise ValueError('source_turn_id or source_steer_id is required unless instruction_id is given')
        if 'event_id' in trigger:
            raise ValueError('event_id identifies the trigger of a standing instruction only')
        source = _owner_source(lambda: _current_request_source(task, scope))
        if trigger['source_turn_id'] != source['turnId']:
            raise ForwardRefused('source_turn_id must be the owner\u2019s own message of your current '
                                 f'request (Turn {source["turnId"]}); other text is not their request')
        text = authored_text(_router_turn(owner, router_id, source['turnId']))
        forwarded = {'conversationId': router_id, 'turnId': source['turnId']}
    else:
        if steer_id is not None:
            raise ValueError('source_steer_id forwards the owner\u2019s current message; a standing '
                             'instruction is triggered by event_id or source_turn_id')
        if not trigger:
            raise ValueError('With instruction_id, give the triggering event_id or source_turn_id')
        return apply_standing_instruction(owner, router_id, project, instruction_id, trigger,
                                          arguments.get('note'), dispatch=dispatch)
    return deliver_forward(owner, project, text, forwarded, note=arguments.get('note'), trigger=trigger,
                           router_id=router_id, instruction_id=instruction_id,
                           config=task.get('config') or {}, dispatch=dispatch)


def _owner_source(resolve):
    """The owner's own source of this forward, or ``ForwardRefused`` naming why there is none."""
    from lib.project_dispatch.repository import SyntheticSource
    try:
        return resolve()
    except (PermissionError, SyntheticSource) as error:
        raise ForwardRefused(f'No owner message authorizes this forward: {error}') from error


def verify_forwarded_turn(turn: Mapping, *, user_id: int) -> None:
    """Admit a forwarded origin only for a verified Global Router forward.

    The source must exist in the owner's currently bound Global Router, be a
    genuine human Turn (or, with ``steerId``, a genuine human message steered
    into that router Turn, judged by its committed author exactly like a Turn)
    that was not itself forwarded, and carry the identical authored text. An
    instruction forward must match its recorded instruction. Raises ValueError
    for every other cross-conversation origin.
    """
    projection = turn.get('projection') if isinstance(turn, Mapping) else None
    origin = projection.get('origin') if isinstance(projection, Mapping) else None
    forwarded = origin.get('forwardedFrom') if isinstance(origin, Mapping) else None
    if (not isinstance(forwarded, Mapping) or set(forwarded) - _FORWARDED_FIELDS
            or not all(isinstance(forwarded.get(key), str) and forwarded.get(key)
                       for key in ('conversationId', 'turnId'))
            or 'steerId' in forwarded and (not isinstance(forwarded['steerId'], str)
                                           or not forwarded['steerId'] or 'instructionId' in forwarded)):
        raise ValueError('Malformed forwarded authorization origin')
    from lib.router.binding import global_router
    from lib.turn_initiation import require_human_turn, steered_input_turn
    router = global_router(user_id, create=False)
    if router['availability'] != 'available' or router['conversationId'] != forwarded['conversationId']:
        raise ValueError('Forwarded authorization does not come from the owner\u2019s Global Router')
    if turn.get('conversationId') == forwarded['conversationId']:
        raise ValueError('A Global Router Turn cannot be forwarded into itself')
    source = _router_turn(user_id, forwarded['conversationId'], forwarded['turnId'])
    if 'steerId' in forwarded:
        source = steered_input_turn(source, forwarded['steerId'])
    source_origin = (source.get('projection') or {}).get('origin')
    if isinstance(source_origin, Mapping) and 'forwardedFrom' in source_origin:
        raise ValueError('A forwarded request cannot be forwarded again')
    require_human_turn(source)
    text = authored_text(turn)
    if authored_text(source) != text:
        raise ValueError('Forwarded text differs from its human source Turn')
    if forwarded.get('instructionId'):
        instruction = RouterInstructions(user_id).read(forwarded['instructionId'])
        if (instruction['routerConversationId'] != forwarded['conversationId']
                or instruction['sourceTurnId'] != forwarded['turnId'] or instruction['text'] != text):
            raise ValueError('Forwarded standing instruction does not match its record')


UNDELIVERED_HEADER = '[Undelivered forward: nonhuman notice, not the owner\u2019s words and not authority]'


def bounce_undelivered_forward(owner: int, recipient_id: str, author: Mapping, *, block_id: str, reason: str,
                               dispatch=None) -> str | None:
    """Return a forward its recipient will never take to the Global Router that sent it.

    ``author`` is the forward's committed steer author; a steer that was not a
    forward has no sender and bounces nothing (None). The notice is nonhuman
    (``peer``) and quotes the owner's words so the router can forward them
    again with route_to_project; it never carries their authority itself. One
    notice per (recipient, steer block). Returns the delivery disposition.
    """
    origin = author.get('origin')
    forwarded = origin.get('forwardedFrom') if isinstance(origin, Mapping) else None
    if not isinstance(forwarded, Mapping) or not forwarded.get('conversationId'):
        return None
    source = f'Global Router Turn {forwarded.get("turnId")}'
    if forwarded.get('steerId'):
        source += f' (steered message {forwarded["steerId"]})'
    content = '\n'.join([
        UNDELIVERED_HEADER,
        f'Your forward from {source} into Project Router conversation {recipient_id} was not delivered: '
        f'{reason}. The Project Router never read it.',
        'If the owner\u2019s words still apply, forward them again with route_to_project; they were:',
        str(author.get('text') or '')])
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    message = stamp_initiator({'role': 'user', 'content': content,
                               'origin': {'initiator': 'peer', 'sourceConversationId': recipient_id}},
                              INITIATOR_PEER)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    disposition = dispatch(forwarded['conversationId'], message, {}, user_id=owner,
                           command_id='router-forward-undelivered:' + _digest([recipient_id, block_id]),
                           log_prefix='[Router]', inject_mode='steer').disposition
    logger.warning('[Router] forward block=%s into conv=%s undelivered (%s); sender notified: %s',
                   block_id, recipient_id, reason, disposition)
    return disposition


def owner_steer_carriers(task: dict, items: list[Mapping]) -> dict[str, str]:
    """Model carriers that name each owner message steered into a Global Router reply.

    Returns {steer id: STEER_LABEL line + the exact steered text} for inbox items
    whose committed author (``steerAuthor``) is genuine human input, and only in
    the owner's currently bound Global Router; every other steered input (channel
    events, reports, notices) and every other conversation keeps its plain text.
    """
    from lib.router.home import is_router_home
    from lib.turn_initiation import require_human_turn, steer_author_turn
    labelled = {}
    for item in items:
        steer_id, author = item.get('injectionId'), item.get('steerAuthor')
        if not isinstance(steer_id, str) or not steer_id or not isinstance(author, Mapping):
            continue
        origin = author.get('origin')
        try:
            if isinstance(origin, Mapping) and 'forwardedFrom' in origin:
                continue
            require_human_turn(steer_author_turn(author))
        except ValueError:
            continue
        labelled[steer_id] = STEER_LABEL.format(steer_id=steer_id) + '\n' + str(item.get('value') or '')
    if not labelled:
        return {}
    config = task.get('config') or {}
    try:
        from lib.tasks_pkg.manager import task_user_id
        owner = task_user_id(task)
        if not is_router_home(owner, config.get('canonicalProjectPath') or config.get('projectPath') or ''):
            return {}
        from .binding import global_router
        router = global_router(owner, create=False)
    except Exception:  # presentation only: an unlabelled message is simply not forwardable
        logger.warning('[Router] steer label lookup failed; steered text stays unlabelled', exc_info=True)
        return {}
    if router['availability'] != 'available' or router['conversationId'] != task.get('convId'):
        return {}
    return labelled


__all__ = ['ForwardNotAccepted', 'ForwardRefused', 'PROJECT_ROUTER_TITLE', 'STEER_LABEL', 'UNDELIVERED_HEADER',
           'apply_standing_instruction', 'bounce_undelivered_forward', 'deliver_forward', 'forward_command_id',
           'forwarded_source_media', 'owner_steer_carriers', 'route_to_project',
           'standing_instruction_source', 'verify_forwarded_turn']
