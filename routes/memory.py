"""routes/memory.py — Memory CRUD API."""

import io
import os

import requests
from flask import Blueprint, jsonify, request

from lib.log import get_logger

logger = get_logger(__name__)

memory_bp = Blueprint('memory', __name__)

# Max bytes for catalog-driven downloads (matches installer._MAX_BYTES).
_CATALOG_DL_CAP = 50 * 1024 * 1024  # catalog zips (full repos) can be bigger
_CATALOG_DL_TIMEOUT = 60  # seconds

# Default project root — same directory that contains server.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pp():
    """Get project_path from request args, falling back to project root."""
    explicit = None
    if request.is_json and request.json:
        explicit = request.json.get('project_path')
    if not explicit:
        explicit = request.args.get('project_path')
    return explicit or _PROJECT_ROOT


# ── Local Memory CRUD ──────────────────────────────────

@memory_bp.route('/api/memory', methods=['GET'])
def api_list_memories():
    from lib.memory import list_memories
    scope = request.args.get('scope', 'all')
    memories = list_memories(project_path=_pp(), scope=scope)
    # Strip filepath from response (internal detail)
    for m in memories:
        m.pop('filepath', None)
    return jsonify({'memories': memories, 'skills': memories})  # 'skills' for backward compat


@memory_bp.route('/api/memory/<memory_id>', methods=['GET'])
def api_get_memory(memory_id):
    from lib.memory import get_memory
    mem = get_memory(memory_id, project_path=_pp())
    if not mem:
        return jsonify({'error': 'Memory not found'}), 404
    mem.pop('filepath', None)
    return jsonify(mem)


@memory_bp.route('/api/memory', methods=['POST'])
def api_create_memory():
    from lib.memory import create_memory
    data = request.get_json(force=True)
    mem_name = data.get('name', 'Untitled')
    logger.info('[Memory] creating memory: %s (scope=%s)', mem_name, data.get('scope', 'global'))
    mem = create_memory(
        name=mem_name,
        description=data.get('description', ''),
        body=data.get('body', ''),
        tags=data.get('tags'),
        scope=data.get('scope', 'global'),
        project_path=_pp(),
    )
    logger.info('[Memory] created memory %s', mem.get('id', '?'))
    mem.pop('filepath', None)
    return jsonify(mem), 201


@memory_bp.route('/api/memory/<memory_id>', methods=['PUT'])
def api_update_memory(memory_id):
    from lib.memory import update_memory
    data = request.get_json(force=True)
    mem = update_memory(memory_id, data, project_path=_pp())
    if not mem:
        return jsonify({'error': 'Memory not found'}), 404
    mem.pop('filepath', None)
    return jsonify(mem)


@memory_bp.route('/api/memory/<memory_id>', methods=['DELETE'])
def api_delete_memory(memory_id):
    from lib.memory import delete_memory
    logger.warning('[Memory] deleting memory %s', memory_id)
    ok = delete_memory(memory_id, project_path=_pp())
    if not ok:
        logger.warning('[Memory] memory %s not found for deletion', memory_id)
    return jsonify({'deleted': ok}), (200 if ok else 404)


@memory_bp.route('/api/memory/merge', methods=['POST'])
def api_merge_memories():
    """Merge multiple memories into one. JSON body: memory_ids, name, description, body, tags?, scope?"""
    from lib.memory import merge_memories
    data = request.get_json(force=True)
    logger.info('[Memory] merging memories: %s → %s', data.get('memory_ids', []), data.get('name', '?'))
    try:
        result = merge_memories(
            memory_ids=data.get('memory_ids', []),
            name=data.get('name', 'Merged Memory'),
            description=data.get('description', ''),
            body=data.get('body', ''),
            tags=data.get('tags'),
            scope=data.get('scope', 'project'),
            project_path=_pp(),
        )
    except ValueError as e:
        logger.debug('[Memory] merge_memories validation error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 400
    result['merged_memory'].pop('filepath', None)
    return jsonify(result), 201


@memory_bp.route('/api/memory/<memory_id>/toggle', methods=['POST'])
def api_toggle_memory(memory_id):
    from lib.memory import toggle_memory
    data = request.get_json(silent=True) or {}
    mem = toggle_memory(memory_id, enabled=data.get('enabled'), project_path=_pp())
    if not mem:
        return jsonify({'error': 'Memory not found'}), 404
    mem.pop('filepath', None)
    return jsonify(mem)


# ── Skill-package install (drag-and-drop zip) ──────────────────

_INSTALL_MAX_BYTES = 25 * 1024 * 1024  # mirror lib.memory.installer._MAX_BYTES


@memory_bp.route('/api/memory/install', methods=['POST'])
def api_install_skill_package():
    """Install a skill package (zip) into the memory tree.

    Accepts:
      * ``multipart/form-data`` with a ``file`` field carrying the zip
        and optional ``scope`` / ``overwrite`` / ``project_path`` form fields.
      * JSON body ``{path: '/abs/path/to/zip-or-dir', scope, overwrite}``
        when running locally.
    """
    from lib.memory.installer import InstallerError, install_skill_package

    scope = 'project'
    overwrite = False
    source = None
    fname = None

    if request.content_type and request.content_type.startswith('multipart/'):
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        f = request.files['file']
        fname = f.filename or 'upload.zip'
        scope = (request.form.get('scope') or 'project').strip().lower()
        overwrite = request.form.get('overwrite', '').lower() in ('1', 'true', 'yes')
        # Read with a hard cap so we don't materialise a 100 MB upload.
        data = f.read(_INSTALL_MAX_BYTES + 1)
        if len(data) > _INSTALL_MAX_BYTES:
            return jsonify({
                'error': f'File exceeds {_INSTALL_MAX_BYTES // (1024*1024)} MB limit'
            }), 413
        source = bytes(data)
    else:
        body = request.get_json(silent=True) or {}
        scope = (body.get('scope') or 'project').strip().lower()
        overwrite = bool(body.get('overwrite'))
        path = body.get('path') or ''
        if not path or not os.path.exists(path):
            return jsonify({'error': 'Provide a file upload or {"path": ...}'}), 400
        source = path
        fname = os.path.basename(path)

    if scope not in ('project', 'global'):
        return jsonify({'error': f'Invalid scope: {scope}'}), 400

    project_path = _pp()
    try:
        result = install_skill_package(
            source,
            scope=scope,
            project_path=project_path,
            overwrite=overwrite,
            original_filename=fname,
        )
    except InstallerError as e:
        logger.warning('[Memory] Skill install rejected (%s): %s', fname, e)
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error('[Memory] Skill install crashed (%s): %s', fname, e,
                     exc_info=True)
        return jsonify({'error': f'Install failed: {e}'}), 500

    mem = result['memory']
    mem.pop('filepath', None)
    return jsonify({
        'memory': mem,
        'replaced': result['replaced'],
        'install_hints': result['install_hints'],
    }), 201


# ── Curated Catalog (App-Store style) ───────────────────────────

@memory_bp.route('/api/memory/catalog', methods=['GET'])
def api_skill_catalog():
    """Return the curated skill catalog annotated with install state."""
    from lib.memory import list_all_memories
    from lib.memory.catalog import get_catalog

    project_path = _pp()
    # Gather all currently-installed package ids so the UI can mark cards.
    installed_ids = {
        m['id'] for m in list_all_memories(project_path=project_path)
        if m.get('is_package')
    }
    catalog = get_catalog()
    for entry in catalog:
        entry['installed'] = entry['id'] in installed_ids
    return jsonify({'catalog': catalog, 'installed_ids': sorted(installed_ids)})


@memory_bp.route('/api/memory/catalog/install', methods=['POST'])
def api_skill_catalog_install():
    """Install a skill package from the curated catalog.

    JSON body: ``{skill_id: '<id>', scope?: 'project'|'global', overwrite?: bool}``
    """
    from lib.memory.catalog import get_catalog_entry
    from lib.memory.installer import InstallerError, install_skill_package

    data = request.get_json(silent=True) or {}
    skill_id = (data.get('skill_id') or '').strip()
    scope = (data.get('scope') or 'project').strip().lower()
    overwrite = bool(data.get('overwrite'))

    if not skill_id:
        return jsonify({'error': 'skill_id is required'}), 400
    if scope not in ('project', 'global'):
        return jsonify({'error': f'Invalid scope: {scope}'}), 400

    entry = get_catalog_entry(skill_id)
    if entry is None:
        return jsonify({'error': f'Unknown skill id: {skill_id}'}), 404

    url = entry.get('download_url', '')
    if not url.startswith('https://'):
        return jsonify({'error': 'Catalog entry has no https download_url'}), 400

    logger.info('[Memory] Catalog install: %s (scope=%s) from %s',
                skill_id, scope, url)

    # ── Fetch the zip into memory ─────────────────────────────
    try:
        resp = requests.get(url, timeout=_CATALOG_DL_TIMEOUT, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning('[Memory] Catalog download failed for %s: %s', skill_id, e)
        return jsonify({'error': f'Download failed: {e}'}), 502

    buf = io.BytesIO()
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _CATALOG_DL_CAP:
                logger.warning('[Memory] Catalog zip %s exceeds cap (%d bytes)',
                               skill_id, _CATALOG_DL_CAP)
                return jsonify({
                    'error': f'Archive exceeds {_CATALOG_DL_CAP // (1024*1024)} MB'
                }), 413
            buf.write(chunk)
    except requests.RequestException as e:
        logger.warning('[Memory] Catalog stream error for %s: %s', skill_id, e)
        return jsonify({'error': f'Download interrupted: {e}'}), 502

    project_path = _pp()
    try:
        result = install_skill_package(
            bytes(buf.getvalue()),
            scope=scope,
            project_path=project_path,
            overwrite=overwrite,
            original_filename=f'{skill_id}.zip',
        )
    except InstallerError as e:
        logger.warning('[Memory] Catalog install rejected (%s): %s', skill_id, e)
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error('[Memory] Catalog install crashed (%s): %s', skill_id, e,
                     exc_info=True)
        return jsonify({'error': f'Install failed: {e}'}), 500

    mem = result['memory']
    mem.pop('filepath', None)
    return jsonify({
        'memory': mem,
        'replaced': result['replaced'],
        'install_hints': result['install_hints'],
        'catalog_id': skill_id,
    }), 201


@memory_bp.route('/api/memory/<memory_id>/files', methods=['GET'])
def api_memory_files(memory_id):
    """List files inside an installed package (for the UI file browser).

    Returns ``{files: [{path, size, kind}], root: <package_dir>}``.
    """
    from lib.memory import get_memory
    mem = get_memory(memory_id, project_path=_pp())
    if not mem:
        return jsonify({'error': 'Memory not found'}), 404
    if not mem.get('is_package') or not mem.get('package_dir'):
        return jsonify({'error': 'Not a package memory'}), 400

    root = mem['package_dir']
    # Safety: root must exist and be a directory.
    if not os.path.isdir(root):
        return jsonify({'error': 'Package directory missing'}), 404

    files = []
    for dirpath, _dirs, fnames in os.walk(root):
        for fname in fnames:
            if fname.startswith('.'):
                continue
            full = os.path.join(dirpath, fname)
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            rel = os.path.relpath(full, root)
            # Kind for UI icon
            low = fname.lower()
            if low == 'skill.md':
                kind = 'skill'
            elif low.endswith(('.md', '.txt', '.rst')):
                kind = 'doc'
            elif low.endswith(('.py', '.js', '.ts', '.sh', '.go', '.rb')):
                kind = 'script'
            elif low.endswith(('.json', '.yaml', '.yml', '.toml')):
                kind = 'config'
            else:
                kind = 'asset'
            files.append({'path': rel, 'size': sz, 'kind': kind})
    files.sort(key=lambda f: (f['kind'] != 'skill', f['path']))
    return jsonify({
        'memory_id': memory_id,
        'root': root,
        'files': files,
        'count': len(files),
    })
