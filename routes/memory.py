"""routes/memory.py — Memory CRUD API."""

import os

from flask import Blueprint, jsonify, request

from lib.log import get_logger

logger = get_logger(__name__)

memory_bp = Blueprint('memory', __name__)

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
