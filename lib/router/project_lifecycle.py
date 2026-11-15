"""Projects and their Project Routers: one operation for the UI and the Global Router (docs/ROUTER.md R12).

Owner decision 2026-11-09 (Turn 3315ee59-e227-4025-8246-2aee9a9a543e of
coordinator-1a390889aa954da2b18551d1b4a32625): 「总负责人要可以创建项目，设置它的默认路径啊啥的，
并设置项目里的coordinator。以后我们创建项目之后，就默认创建负责人吧，也不需要后面再创建。就反正人能干的
事情，总负责人都要能干」

A project is one of the owner's folder presets (lib/conversations/folders.py).
Its defaults are what the project settings dialog edits: name, color, the
ordered roots new conversations start with (the first is the primary root),
read-only roots and sidebar state. Its Project Router is the Coordinator bound
to the primary root. Model, thinking depth and capabilities belong to that
conversation, so configuring the Router writes its settings the way the
owner's composer does.

``ProjectLifecycle`` is the backend operation of both the folders HTTP adapter
and ``router_project``: ``save_project`` and ``update_project`` persist through
FolderRepository (the dialog's validation and record), then ensure the primary
root's Router with ``forwarding._ensure_project_router``, the deterministic
provisioning route_to_project has always used. Repeated or concurrent ensures
converge on one conversation and one binding; two presets of one root share
its one Router. A Router that cannot be provisioned while the preset saves
(storage unavailable) never undoes the owner's save: the result names the
cause, and the next ensure (the coordinator create button, a forward or this
tool) provisions the same Router. ``create_coordinator`` is the coordinator
create button: it provisions a missing Router and replaces a bound one only
at its current revision. Nothing here deletes, unregisters or prepares a
folder; Worker delegation prepares a root on first use, as before.

``router_project`` is the Global Router's tool. ``list`` and ``get`` are
passive. ``create``, ``register``, ``update_defaults`` and ``configure_router``
act under the owner's delegation (R9, scope ``project_lifecycle``): the grant,
the owner's trigger and one audit record precede the effect, and a retry with
the same idempotency key reads the state back instead of acting again.

Entry points: ProjectLifecycle, router_project, router_setting_keys,
DELEGATION_SCOPE.
Dependencies: conversations.folders, conversations.project_coordination,
conversations.settings_store, conv_config, router.forwarding,
router.owner_delegation, project_mod.tools, project_dispatch.delegation.
"""
from __future__ import annotations

from collections.abc import Mapping
import os

from lib.conversations.folders import FolderRepository, owner_folders, validate_folder_updates
from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

from .contract import validate

logger = get_logger(__name__)

DELEGATION_SCOPE = 'project_lifecycle'
# Settings that place the Router belong to its project binding, never to its configuration.
_WORKSPACE_SETTINGS = frozenset({'projectPath', 'projectPaths', 'readOnlyPaths', 'folderId'})
_BINDING_FIELDS = ('projectPath', 'conversationId', 'revision', 'availability')
_UNCHANGED = object()


def router_setting_keys() -> frozenset:
    """What the owner sets on a Project Router in its composer (contracts/conversation_settings_v1.json)."""
    from lib.conversation_sync.settings_policy import CONVERSATION_SETTINGS_CONTRACT
    return frozenset(key for key, spec in CONVERSATION_SETTINGS_CONTRACT['keys'].items()
                     if spec.get('author') == 'composer') - _WORKSPACE_SETTINGS


def _thinking_depths() -> frozenset:
    """The composer's choices ('off' and the shared reasoning depths); '' returns to the default."""
    from lib.agent_core.run_contract import THINKING_DEPTHS
    return THINKING_DEPTHS | {'off', ''}


class ProjectLifecycle:
    """One owner's projects (folder presets) and the Project Router of each primary root."""

    def __init__(self, user_id: int, *, folders: FolderRepository | None = None):
        self.user_id = require_user_id(user_id, context='project lifecycle')
        self.folders = folders or owner_folders()

    # ── reads ────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        return [self._view(folder) for folder in self.folders.list(self.user_id)]

    def get(self, folder_id: str) -> dict:
        folder = self.folders.get(self.user_id, folder_id)
        if folder is None:
            raise ValueError(f'Project {folder_id} does not exist; list the projects to find its id')
        return self._view(folder)

    def read_router(self, path: str) -> dict:
        """The Project Router of ``path``: its binding and, when bound, its settings and Worker model."""
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        bindings = ProjectCoordinationRepository(self.user_id)
        binding = bindings.read(path)
        view = {key: binding[key] for key in _BINDING_FIELDS}
        if binding['availability'] == 'available':
            from lib.conversations.repository import ConversationRepository
            target = ConversationRepository().get(binding['conversationId'], user_id=self.user_id,
                                                  include_messages=False)
            stored = (target.metadata.get('settings') if target is not None else None) or {}
            view['settings'] = {key: stored[key] for key in sorted(router_setting_keys()) if key in stored}
            view['workerModel'] = bindings.worker_model(path)
        return view

    def _binding(self, path: str) -> dict:
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        binding = ProjectCoordinationRepository(self.user_id).read(path)
        return {key: binding[key] for key in _BINDING_FIELDS}

    def _view(self, folder: dict) -> dict:
        path = folder.get('projectPath') or ''
        return {**folder, 'coordinator': self._binding(path) if path else None}

    # ── the shared create and update ─────────────────────────────────

    def ensure_router(self, path: str, *, folder_id: str | None = None,
                      config: Mapping | None = None) -> tuple[dict, bool]:
        """Create-or-return the one Project Router of ``path`` (route_to_project's provisioning)."""
        from lib.router.forwarding import _ensure_project_router
        return _ensure_project_router(self.user_id, normalize_project_path(path), config or {},
                                      folder_id=folder_id)

    def save_project(self, fields: Mapping) -> dict:
        """Create a project from the settings dialog's fields; its primary root gets its Router."""
        folder = self.folders.create(self.user_id, dict(fields))
        return {'project': folder, **self._ensure_saved(folder)}

    def update_project(self, folder_id: str, fields: Mapping) -> dict | None:
        """Write project defaults; a new primary root gets its Router, the previous one keeps its own.

        A change that leaves the roots alone (a rename, a collapse) does not touch
        coordination at all.
        """
        folder = self.folders.update(self.user_id, folder_id, dict(fields))
        if folder is None:
            return None
        if 'projectPaths' not in fields and 'projectPath' not in fields:
            return {'project': folder, 'coordinator': None, 'coordinator_deferred': None}
        return {'project': folder, **self._ensure_saved(folder)}

    def _ensure_saved(self, folder: dict) -> dict:
        path = folder['projectPath']
        if not path:
            return {'coordinator': None, 'coordinator_deferred': None}
        try:
            binding, created = self.ensure_router(path, folder_id=folder['id'])
        except StorageError as error:
            # The owner's save stands; the next ensure provisions the same Router.
            logger.warning('[Project] Project Router provisioning deferred for %s: %s', path, error)
            return {'coordinator': None, 'coordinator_deferred': f'{type(error).__name__}: {error}'}
        return {'coordinator': {**{key: binding[key] for key in _BINDING_FIELDS}, 'created': created},
                'coordinator_deferred': None}

    def create_coordinator(self, path: str, *, folder_id: str, expected_revision,
                           command_id: str) -> dict:
        """The coordinator create button (sidebar, and Replace coordinator in the workspace).

        A project without an available Router gets its one Router from the shared
        provisioning, so repeated clicks never mint a second one; a bound Router
        is replaced only by an explicit create at its current revision.
        """
        # A create with no idempotency key is invalid input, not an effect: it is
        # refused before any Router is provisioned (the route always required one).
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 256:
            raise ValueError('Idempotency-Key is required')
        binding, created = self.ensure_router(path, folder_id=folder_id)
        if created:
            return binding
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        return ProjectCoordinationRepository(self.user_id).create(
            path, folder_id=folder_id, expected_revision=expected_revision, command_id=command_id)

    # ── what only the Global Router needs on top ─────────────────────

    def create(self, *, parent: str, folder_name: str, name: str | None = None,
               color: str = '') -> dict:
        """Make a new project folder with the file picker's own operation, then save_project."""
        from lib.project_mod.tools import create_directory
        folder_name = folder_name.strip()
        path = os.path.join(os.path.abspath(os.path.expanduser(parent)), folder_name)
        fields = {'name': (name or folder_name).strip(), 'color': color, 'projectPaths': [path]}
        validate_folder_updates(fields, creating=True)  # refused before anything is written
        created = create_directory(parent, folder_name)
        if created.get('error'):
            raise ValueError(created['error'])
        return self.save_project({**fields, 'projectPaths': [created['path']]})

    def register(self, *, path: str, name: str | None = None, color: str = '') -> dict:
        """Register an existing directory as a project; its contents are not touched."""
        if not os.path.isdir(path):
            raise ValueError(f'Directory not found: {path}')
        title = (name or os.path.basename(path.rstrip(os.sep)) or path).strip()
        return self.save_project({'name': title, 'color': color, 'projectPaths': [path]})

    def settings_patch(self, settings: Mapping, *, tenant_id: str | None = None) -> dict:
        """Validate a Project Router settings change in the composer's vocabulary."""
        allowed = router_setting_keys()
        unknown = sorted(set(settings) - allowed)
        if unknown:
            raise ValueError(f'Not a Project Router setting: {", ".join(unknown)}. Its roots are project '
                             f'defaults (update_defaults); settable here: {", ".join(sorted(allowed))}')
        patch = dict(settings)
        depth = patch.get('thinkingDepth')
        if 'thinkingDepth' in patch and depth is not None and depth not in _thinking_depths():
            raise ValueError(f'thinkingDepth must be one of {", ".join(sorted(_thinking_depths() - {""}))}, '
                             'or empty for the model default')
        if 'model' in patch and 'modelRef' not in patch:
            # A new model name replaces the picker's routed reference, which would otherwise win.
            patch.update(modelRef=None, preferredProviderId='')
            if patch['model']:
                from lib.project_dispatch.delegation import admit_worker_model
                patch['model'] = admit_worker_model(self.user_id, patch['model'], tenant_id=tenant_id,
                                                    field='model')
        return patch

    def configure_router(self, path: str, *, settings: Mapping | None = None, worker_model=_UNCHANGED,
                         tenant_id: str | None = None, config: Mapping | None = None) -> dict:
        """Ensure the Project Router of ``path`` and write what the owner sets on it in the UI.

        Settings go through the composer's resolver over every current choice plus
        this change, so keys the change does not name keep their values; the Worker
        model goes through set_worker_model, the backend of /api/v4/router/worker-model.
        """
        patch = self.settings_patch(settings or {}, tenant_id=tenant_id)
        binding, created = self.ensure_router(path, config=config)
        if patch:
            from lib.conv_config import resolve_conv_settings
            from lib.conversation_sync.settings_policy import public_conversation_settings
            from lib.conversations.settings_store import update_conversation_settings

            def apply(stored):
                merged = {**public_conversation_settings(stored), **patch}
                stored.update(public_conversation_settings(resolve_conv_settings(conv_settings=merged)))

            if update_conversation_settings(binding['conversationId'], apply, user_id=self.user_id) is None:
                raise PermissionError('The Project Router conversation is unavailable')
        if worker_model is not _UNCHANGED:
            from lib.conversations.project_coordination import ProjectCoordinationRepository
            ProjectCoordinationRepository(self.user_id).set_worker_model(
                path, worker_model or None, tenant_id=tenant_id)
        return {**self.read_router(path), 'created': created}


def _lifecycle(owner: int) -> ProjectLifecycle:
    return ProjectLifecycle(owner)


def _target(action: str, *, path: str = '', folder_id: str = '') -> dict:
    target = {'kind': 'project', 'action': action}
    if path:
        target['path'] = path
    if folder_id:
        target['folderId'] = folder_id
    return target


def _saved(result: dict) -> dict:
    coordinator, path = result['coordinator'], result['project']['projectPath']
    if coordinator is not None:
        step = (f'The project and its Project Router ({coordinator["conversationId"]}) exist. Forward the '
                f'owner\u2019s work with route_to_project(project_path={path!r}); change the project\u2019s '
                'defaults with update_defaults and the Router\u2019s settings with configure_router.')
    else:
        step = (f'The project is saved, but its Project Router could not be provisioned now '
                f'({result["coordinator_deferred"]}); provision it with router_project(action='
                f'configure_router, project_path={path!r}).')
    return {**result, 'next_step': step}


def _projects_at(lifecycle: ProjectLifecycle, path: str) -> dict:
    root = normalize_project_path(path)
    return {'projects': [project for project in lifecycle.list()
                         if normalize_project_path(project['projectPath'] or '') == root],
            'coordinator': lifecycle.read_router(path)}


def _updated(lifecycle: ProjectLifecycle, folder_id: str, fields: dict) -> dict:
    result = lifecycle.update_project(folder_id, fields)
    if result is None:
        raise ValueError(f'Project {folder_id} does not exist; list the projects to find its id')
    path = result['project']['projectPath']
    if result['coordinator'] is None and path and not result['coordinator_deferred']:
        result['coordinator'] = lifecycle.read_router(path)
    return {**result, 'next_step': 'Saved. New conversations in this project start with these defaults; '
                                   'existing conversations keep their own workspace.'}


def _write(task: dict, scope: dict, arguments: dict, lifecycle: ProjectLifecycle):
    """The audit target, the effect and its read-back for one write; refuses bad input before acting."""
    from .forwarding import _target_project
    action = arguments['action']
    if action == 'create':
        parent, folder_name = arguments.get('parent'), arguments.get('folder_name')
        if not parent or not folder_name:
            raise ValueError('create needs parent (an existing directory) and folder_name (the new folder); '
                             'add an existing directory with register')
        path = os.path.join(os.path.abspath(os.path.expanduser(parent)), folder_name.strip())
        return (_target(action, path=path),
                lambda: _saved(lifecycle.create(parent=parent, folder_name=folder_name,
                                                name=arguments.get('name'), color=arguments.get('color', ''))),
                lambda: _projects_at(lifecycle, path))
    if action == 'register':
        if not arguments.get('project_path'):
            raise ValueError('register needs project_path, an existing directory')
        path = _target_project(scope, arguments['project_path'])
        return (_target(action, path=path),
                lambda: _saved(lifecycle.register(path=path, name=arguments.get('name'),
                                                  color=arguments.get('color', ''))),
                lambda: _projects_at(lifecycle, path))
    if action == 'update_defaults':
        folder_id, defaults = arguments.get('folder_id'), arguments.get('defaults')
        if not folder_id or not defaults:
            raise ValueError('update_defaults needs folder_id (from list) and defaults')
        project = lifecycle.get(folder_id)
        fields = dict(defaults)
        if 'expected_revision' in arguments:
            fields['expectedRevision'] = arguments['expected_revision']
        return (_target(action, path=project['projectPath'], folder_id=folder_id),
                lambda: _updated(lifecycle, folder_id, fields),
                lambda: {'project': lifecycle.get(folder_id)})
    if not arguments.get('project_path'):
        raise ValueError('configure_router needs project_path')
    path = _target_project(scope, arguments['project_path'])
    from lib.identity import PrincipalContext
    tenant = PrincipalContext.from_payload(task.get('_principalContext') or {}).tenant_id
    options = {'settings': arguments.get('settings') or {}, 'tenant_id': tenant,
               'config': task.get('config') or {}}
    lifecycle.settings_patch(options['settings'], tenant_id=tenant)
    if 'worker_model' in arguments:
        options['worker_model'] = arguments['worker_model']
        if arguments['worker_model']:
            from lib.project_dispatch.delegation import admit_worker_model
            admit_worker_model(lifecycle.user_id, arguments['worker_model'], tenant_id=tenant)
    return (_target(action, path=path),
            lambda: {'coordinator': lifecycle.configure_router(path, **options),
                     'next_step': 'Applied; the Project Router uses these settings from its next turn.'},
            lambda: {'coordinator': lifecycle.read_router(path)})


def router_project(task: dict, scope: dict, arguments: dict) -> dict:
    """Create, register, read or configure the owner's projects; every write acts under R9."""
    from .forwarding import _target_project
    arguments = validate(dict(arguments), 'routerProject')
    action = arguments['action']
    lifecycle = _lifecycle(scope['user_id'])
    if action == 'list':
        return {'projects': lifecycle.list()}
    if action == 'get':
        if arguments.get('folder_id'):
            project = lifecycle.get(arguments['folder_id'])
            path = project['projectPath']
            return {'project': project, 'coordinator': lifecycle.read_router(path) if path else None}
        if not arguments.get('project_path'):
            raise ValueError('get needs folder_id (a project from list) or project_path')
        return {'coordinator': lifecycle.read_router(_target_project(scope, arguments['project_path']))}
    target, effect, read_back = _write(task, scope, arguments, lifecycle)
    started = []

    def act():
        started.append(True)
        return effect()

    from .owner_delegation import delegated_action
    try:
        result, action_id = delegated_action(task, scope, arguments, scope_name=DELEGATION_SCOPE,
                                             target=target, action=act)
    except StorageError as error:
        if started:
            error.execution_started = None
            error.recovery = ('Storage failed while the change ran; read router_project(action=get or list) '
                              'and repeat it with a new idempotency_key only if it is still needed.')
        raise
    if result.get('replayed'):
        # The gate never applies a key twice; show what that key left behind.
        status = result.get('status')
        result = {**read_back(), 'replayed': True, 'action_status': status,
                  'interrupted': result.get('interrupted', False), 'code': result.get('code'),
                  'detail': result.get('detail'),
                  'next_step': (f'This idempotency_key already ran ({status}); nothing was done again and '
                                'the current state is shown. A new change needs a new idempotency_key.')}
    return {**result, 'owner_delegation_action_id': action_id}


__all__ = ['DELEGATION_SCOPE', 'ProjectLifecycle', 'router_project', 'router_setting_keys']
