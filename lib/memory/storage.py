"""lib/memory/storage.py — File I/O, YAML frontmatter, CRUD operations.

Memories are plain Markdown files stored in:
  • Global:  <project>/.chatui/memory/global/*.md  (apply across projects)
  • Project: <project>/.chatui/memory/*.md           (project-specific)

All memories live under the project directory — no external ~/.chatui/ dependency.
"""

import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'GLOBAL_MEMORY_DIR', 'GLOBAL_MEMORY_SUBDIR', 'PROJECT_MEMORY_SUBDIR', 'MIN_DESCRIPTION_LENGTH',
    'list_all_memories', 'list_memories', 'get_memory', 'get_enabled_memories',
    'get_eligible_memories',
    'create_memory', 'update_memory', 'delete_memory', 'merge_memories',
    'toggle_memory',
]

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

# Legacy path kept for one-time migration only
_LEGACY_GLOBAL_MEMORY_DIR = os.path.join(Path.home(), '.chatui', 'skills')

# Both global and project memories now live under the project directory
# NOTE: Physical paths still use 'skills' for backward compatibility with
# existing .chatui/skills/ directories on disk. A future migration can rename
# the directories themselves.
GLOBAL_MEMORY_SUBDIR = os.path.join('.chatui', 'skills', 'global')
PROJECT_MEMORY_SUBDIR = os.path.join('.chatui', 'skills')
MIN_DESCRIPTION_LENGTH = 20

# Keep GLOBAL_MEMORY_DIR as a computed property for backward compat
# (injection.py references it for the path template)
GLOBAL_MEMORY_DIR = None  # Set dynamically; see _get_global_memory_dir()

_lock = threading.Lock()


# ═══════════════════════════════════════════════════════
#  Frontmatter Parsing
# ═══════════════════════════════════════════════════════

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter from markdown text. Returns (meta_dict, body)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text

    fm_text = m.group(1)
    body = text[m.end():]
    meta = {}

    for line in fm_text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            key, _, val = line.partition(':')
            key = key.strip()
            val = val.strip()
            if val.lower() in ('true', 'yes'):
                val = True
            elif val.lower() in ('false', 'no'):
                val = False
            elif val.startswith('[') and val.endswith(']'):
                val = [v.strip().strip('"\'') for v in val[1:-1].split(',') if v.strip()]
            elif (val.startswith('"') and val.endswith('"')) or \
                 (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            meta[key] = val

    return meta, body


def _build_frontmatter(meta):
    """Build YAML-like frontmatter string from dict."""
    lines = ['---']
    for key, val in meta.items():
        if isinstance(val, bool):
            lines.append(f'{key}: {"true" if val else "false"}')
        elif isinstance(val, list):
            inner = ', '.join(str(v) for v in val)
            lines.append(f'{key}: [{inner}]')
        else:
            lines.append(f'{key}: {val}')
    lines.append('---')
    return '\n'.join(lines) + '\n'


# ═══════════════════════════════════════════════════════
#  Memory Eligibility Gating (OpenClaw-inspired)
# ═══════════════════════════════════════════════════════

def _check_memory_eligible(mem):
    """Check whether a memory's runtime requirements are satisfied.

    Returns (eligible: bool, reasons: list[str]).
    """
    reasons = []
    required_bins = mem.get('requires_bins') or []
    if isinstance(required_bins, str):
        required_bins = [required_bins]
    for binary in required_bins:
        if not shutil.which(binary):
            reasons.append(f'binary `{binary}` not found on PATH')

    required_env = mem.get('requires_env') or []
    if isinstance(required_env, str):
        required_env = [required_env]
    for var in required_env:
        if not os.environ.get(var):
            reasons.append(f'env var `{var}` not set')

    return (len(reasons) == 0), reasons


# ═══════════════════════════════════════════════════════
#  Memory File I/O
# ═══════════════════════════════════════════════════════

def _ensure_dir(dirpath):
    """Create directory if it doesn't exist."""
    os.makedirs(dirpath, exist_ok=True)


def _memory_from_file(filepath, scope='global'):
    """Read a single memory file and return a memory dict."""
    try:
        with open(filepath, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        logger.debug('Failed to read memory file %s', filepath, exc_info=True)
        return None

    meta, body = _parse_frontmatter(text)
    filename = os.path.basename(filepath)
    memory_id = os.path.splitext(filename)[0]

    mem = {
        'id': memory_id,
        'name': meta.get('name', memory_id.replace('_', ' ').replace('-', ' ').title()),
        'description': meta.get('description', ''),
        'enabled': meta.get('enabled', True),
        'tags': meta.get('tags', []),
        'requires_bins': meta.get('requires_bins', []),
        'requires_env': meta.get('requires_env', []),
        'created': meta.get('created', ''),
        'updated': meta.get('updated', ''),
        'scope': scope,
        'body': body.strip(),
        'filepath': filepath,
    }

    eligible, reasons = _check_memory_eligible(mem)
    mem['eligible'] = eligible
    mem['ineligible_reasons'] = reasons
    return mem


def _write_memory_file(filepath, mem):
    """Write a memory dict back to a markdown file."""
    _ensure_dir(os.path.dirname(filepath))
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    meta = {
        'name': mem.get('name', 'Untitled Memory'),
        'description': mem.get('description', ''),
        'enabled': mem.get('enabled', True),
        'tags': mem.get('tags', []),
        'created': mem.get('created', now),
        'updated': now,
    }
    if mem.get('requires_bins'):
        meta['requires_bins'] = mem['requires_bins']
    if mem.get('requires_env'):
        meta['requires_env'] = mem['requires_env']

    body = mem.get('body', '')
    content = _build_frontmatter(meta) + '\n' + body + '\n'

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    return now


# ═══════════════════════════════════════════════════════
#  List / Load Memories
# ═══════════════════════════════════════════════════════

def _list_memories_in_dir(dirpath, scope='global'):
    """List all .md memory files in a directory."""
    memories = []
    if not os.path.isdir(dirpath):
        return memories
    for fname in sorted(os.listdir(dirpath)):
        if fname.endswith('.md') and not fname.startswith('.'):
            fpath = os.path.join(dirpath, fname)
            mem = _memory_from_file(fpath, scope=scope)
            if mem:
                memories.append(mem)
    return memories


def _get_global_memory_dir(project_path):
    """Return the global memory directory for a given project.

    Global memories are stored at <project>/.chatui/skills/global/.
    On first call, migrates any legacy memories from ~/.chatui/skills/.
    """
    if not project_path:
        # Fallback when no project is set — use legacy path
        return _LEGACY_GLOBAL_MEMORY_DIR
    return os.path.join(project_path, GLOBAL_MEMORY_SUBDIR)


def _migrate_legacy_global_memories(project_path):
    """One-time migration: copy memories from ~/.chatui/skills/ into the project.

    Only copies files that don't already exist in the destination.
    """
    if not project_path:
        return
    src_dir = _LEGACY_GLOBAL_MEMORY_DIR
    if not os.path.isdir(src_dir):
        return
    dst_dir = os.path.join(project_path, GLOBAL_MEMORY_SUBDIR)
    _ensure_dir(dst_dir)

    migrated = 0
    for fname in os.listdir(src_dir):
        if not fname.endswith('.md') or fname.startswith('.'):
            continue
        src_path = os.path.join(src_dir, fname)
        dst_path = os.path.join(dst_dir, fname)
        if not os.path.exists(dst_path):
            try:
                shutil.copy2(src_path, dst_path)
                migrated += 1
            except OSError as e:
                logger.warning('Failed to migrate memory %s: %s', fname, e)
    if migrated:
        logger.info('[Memory] Migrated %d global memory(s) from %s → %s',
                     migrated, src_dir, dst_dir)


_migration_done = set()  # Track which project_paths have been migrated


def list_all_memories(project_path=None):
    """List all global + project memories."""
    with _lock:
        # One-time migration from legacy ~/.chatui/skills/
        if project_path and project_path not in _migration_done:
            _migration_done.add(project_path)
            _migrate_legacy_global_memories(project_path)

        global_dir = _get_global_memory_dir(project_path)
        memories = _list_memories_in_dir(global_dir, scope='global')
        if project_path:
            proj_dir = os.path.join(project_path, PROJECT_MEMORY_SUBDIR)
            memories += _list_memories_in_dir(proj_dir, scope='project')
    return memories


def list_memories(project_path=None, scope='all'):
    """List memories, optionally filtered by scope."""
    all_memories = list_all_memories(project_path)
    if scope == 'global':
        return [s for s in all_memories if s['scope'] == 'global']
    elif scope == 'project':
        return [s for s in all_memories if s['scope'] == 'project']
    return all_memories


def get_memory(memory_id, project_path=None):
    """Get a single memory by ID. Returns memory dict or None."""
    for s in list_all_memories(project_path):
        if s['id'] == memory_id:
            return s
    return None


def get_enabled_memories(project_path=None):
    """Get only enabled memories."""
    return [s for s in list_all_memories(project_path) if s.get('enabled', True)]


def get_eligible_memories(project_path=None):
    """Get memories that are both enabled AND meet all runtime requirements."""
    return [
        s for s in get_enabled_memories(project_path)
        if s.get('eligible', True)
    ]


# ═══════════════════════════════════════════════════════
#  CRUD Operations
# ═══════════════════════════════════════════════════════

def _make_memory_id(name):
    """Generate a filesystem-safe ID from a memory name."""
    safe = re.sub(r'[^\w\s-]', '', name.lower())
    safe = re.sub(r'[\s]+', '_', safe).strip('_')
    if not safe:
        safe = uuid.uuid4().hex[:8]
    return safe


def create_memory(name, description='', body='', tags=None, scope='global', project_path=None):
    """Create a new memory file. Returns the memory dict."""
    if description and len(description.strip()) < MIN_DESCRIPTION_LENGTH:
        logger.warning(
            'Memory "%s" has a very short description (%d chars). '
            'Consider making it ≥%d chars for discoverability.',
            name, len(description.strip()), MIN_DESCRIPTION_LENGTH,
        )
    if not description or not description.strip():
        for line in (body or '').split('\n'):
            line = line.strip().lstrip('#').strip()
            if line and len(line) >= 10:
                description = line[:120]
                logger.info('Memory "%s" had no description; auto-set to: %s', name, description)
                break

    memory_id = _make_memory_id(name)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    mem = {
        'id': memory_id, 'name': name, 'description': description,
        'enabled': True, 'tags': tags or [],
        'requires_bins': [], 'requires_env': [],
        'created': now, 'updated': now, 'body': body, 'scope': scope,
    }

    if scope == 'project' and project_path:
        dirpath = os.path.join(project_path, PROJECT_MEMORY_SUBDIR)
    else:
        dirpath = _get_global_memory_dir(project_path)

    filepath = os.path.join(dirpath, f'{memory_id}.md')
    counter = 1
    while os.path.exists(filepath):
        filepath = os.path.join(dirpath, f'{memory_id}_{counter}.md')
        mem['id'] = f'{memory_id}_{counter}'
        counter += 1

    _write_memory_file(filepath, mem)
    mem['filepath'] = filepath
    return mem


def update_memory(memory_id, updates, project_path=None):
    """Update an existing memory. Returns updated memory or None."""
    all_memories = list_all_memories(project_path)
    target = None
    for s in all_memories:
        if s['id'] == memory_id:
            target = s
            break
    if not target:
        return None
    for key in ('name', 'description', 'body', 'tags', 'enabled',
                'requires_bins', 'requires_env'):
        if key in updates:
            target[key] = updates[key]
    target['updated'] = _write_memory_file(target['filepath'], target)
    return target


def delete_memory(memory_id, project_path=None):
    """Delete a memory file. Returns True if deleted."""
    all_memories = list_all_memories(project_path)
    for s in all_memories:
        if s['id'] == memory_id:
            try:
                os.remove(s['filepath'])
                return True
            except OSError:
                logger.warning('Failed to delete memory file %s', s['filepath'], exc_info=True)
                return False
    return False


def merge_memories(memory_ids, name, description, body, tags=None, scope='project', project_path=None):
    """Merge multiple memories into one new consolidated memory, deleting the originals."""
    if not memory_ids or len(memory_ids) < 2:
        raise ValueError("merge_memories requires at least 2 memory IDs")

    all_memories = list_all_memories(project_path)
    mem_map = {s['id']: s for s in all_memories}
    missing = [sid for sid in memory_ids if sid not in mem_map]
    if missing:
        raise ValueError(f"Memories not found: {', '.join(missing)}")

    if tags is None:
        merged_tags = set()
        for sid in memory_ids:
            merged_tags.update(mem_map[sid].get('tags', []))
        tags = sorted(merged_tags)

    merged = create_memory(name=name, description=description, body=body,
                          tags=tags, scope=scope, project_path=project_path)

    deleted_ids = []
    for sid in memory_ids:
        if delete_memory(sid, project_path):
            deleted_ids.append(sid)

    return {'merged_memory': merged, 'deleted_ids': deleted_ids}


def toggle_memory(memory_id, enabled=None, project_path=None):
    """Toggle a memory's enabled state."""
    if enabled is None:
        mem = get_memory(memory_id, project_path)
        if not mem:
            return None
        enabled = not mem.get('enabled', True)
    return update_memory(memory_id, {'enabled': enabled}, project_path)
