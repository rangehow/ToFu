"""routes/folders.py — Conversation folder CRUD endpoints.

Folders are lightweight organizational groups for conversations.
Stored in data/config/folders.json as a simple JSON array.
Each conversation references a folder via its settings.folderId field.
"""

import json
import os
import time

from flask import Blueprint, jsonify, request

from lib.config_dir import config_path as _config_path
from lib.log import get_logger

logger = get_logger(__name__)

folders_bp = Blueprint('folders', __name__)

_FOLDERS_PATH = _config_path('folders.json')


def _read_folders():
    """Read folders.json and return as list (empty list on failure)."""
    try:
        if os.path.isfile(_FOLDERS_PATH):
            with open(_FOLDERS_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.warning('[Folders] Failed to read folders.json: %s', e)
    return []


def _write_folders(folders):
    """Write folders.json, creating directories as needed."""
    try:
        d = os.path.dirname(_FOLDERS_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_FOLDERS_PATH, 'w') as f:
            json.dump(folders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error('[Folders] Failed to write folders.json: %s', e, exc_info=True)
        raise


@folders_bp.route('/api/folders', methods=['GET'])
def list_folders():
    """Return all folders."""
    return jsonify(_read_folders())


@folders_bp.route('/api/folders', methods=['POST'])
def create_folder():
    """Create a new folder.

    Body: { name: str, color?: str }
    Returns: the created folder object.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Folder name is required'}), 400

    folders = _read_folders()

    folder = {
        'id': 'f_' + hex(int(time.time() * 1000))[2:] + hex(id(data) & 0xFFFF)[2:],
        'name': name,
        'color': data.get('color', ''),
        'collapsed': False,
        'order': len(folders),
        'createdAt': int(time.time() * 1000),
    }
    folders.append(folder)
    _write_folders(folders)
    logger.info('[Folders] Created folder id=%s name=%s', folder['id'], name)
    return jsonify(folder), 201


@folders_bp.route('/api/folders/<folder_id>', methods=['PUT'])
def update_folder(folder_id):
    """Update a folder's name, color, collapsed state, or order.

    Body: { name?: str, color?: str, collapsed?: bool, order?: int }
    """
    data = request.get_json(silent=True) or {}
    folders = _read_folders()
    folder = next((f for f in folders if f['id'] == folder_id), None)
    if not folder:
        return jsonify({'error': 'Folder not found'}), 404

    if 'name' in data:
        folder['name'] = (data['name'] or '').strip() or folder['name']
    if 'color' in data:
        folder['color'] = data['color']
    if 'collapsed' in data:
        folder['collapsed'] = bool(data['collapsed'])
    if 'order' in data:
        folder['order'] = int(data['order'])

    _write_folders(folders)
    logger.info('[Folders] Updated folder id=%s name=%s', folder_id, folder.get('name'))
    return jsonify(folder)


@folders_bp.route('/api/folders/<folder_id>', methods=['DELETE'])
def delete_folder(folder_id):
    """Delete a folder. Conversations in it become unfiled."""
    folders = _read_folders()
    before = len(folders)
    folders = [f for f in folders if f['id'] != folder_id]
    if len(folders) == before:
        return jsonify({'error': 'Folder not found'}), 404

    _write_folders(folders)
    logger.info('[Folders] Deleted folder id=%s', folder_id)
    return jsonify({'ok': True})


@folders_bp.route('/api/folders/reorder', methods=['POST'])
def reorder_folders():
    """Reorder folders.

    Body: { order: [folder_id, folder_id, ...] }
    """
    data = request.get_json(silent=True) or {}
    order = data.get('order', [])
    if not isinstance(order, list):
        return jsonify({'error': 'order must be a list'}), 400

    folders = _read_folders()
    folder_map = {f['id']: f for f in folders}
    for i, fid in enumerate(order):
        if fid in folder_map:
            folder_map[fid]['order'] = i

    _write_folders(folders)
    return jsonify({'ok': True})
