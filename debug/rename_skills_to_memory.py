#!/usr/bin/env python3
"""Rename all 'skill' references to 'memory' throughout the project.

This script performs a systematic rename of the concept formerly called
"skill" to "memory" across all project files (Python, JS, HTML, CSS).

Must be run from the project root directory.
"""

import os
import re
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)

# ══════════════════════════════════════════════════════════════════
#  Step 1: Rename directories
# ══════════════════════════════════════════════════════════════════

DIR_RENAMES = [
    ('lib/skills', 'lib/memory'),
    ('lib/tasks_pkg/handlers/skills.py', 'lib/tasks_pkg/handlers/memory.py'),
    ('routes/skills.py', 'routes/memory.py'),
    ('static/js/skills.js', 'static/js/memory.js'),
]


# ══════════════════════════════════════════════════════════════════
#  Step 2: Text replacements (order matters — longer patterns first)
# ══════════════════════════════════════════════════════════════════

# Python/JS/HTML/CSS replacements — applied to all text files
# Each tuple: (pattern, replacement)
# Order: most specific → least specific to avoid double-replace

REPLACEMENTS = [
    # ── Python imports and module references ──
    ('lib.skills.storage', 'lib.memory.storage'),
    ('lib.skills.injection', 'lib.memory.injection'),
    ('lib.skills.relevance', 'lib.memory.relevance'),
    ('lib.skills.tools', 'lib.memory.tools'),
    ('lib/skills/tools.py', 'lib/memory/tools.py'),
    ('lib/skills/injection.py', 'lib/memory/injection.py'),
    ('lib/skills/relevance.py', 'lib/memory/relevance.py'),
    ('lib/skills/storage.py', 'lib/memory/storage.py'),
    ('lib/skills/', 'lib/memory/'),
    ('lib.skills', 'lib.memory'),
    ('from lib.memory import', 'from lib.memory import'),  # idempotent guard
    
    # ── Route / Blueprint ──
    ('from .skills import skills_bp', 'from .memory import memory_bp'),
    ('skills_bp', 'memory_bp'),
    ("Blueprint('skills'", "Blueprint('memory'"),
    
    # ── Handler file references ──
    ('lib/tasks_pkg/handlers/skills.py', 'lib/tasks_pkg/handlers/memory.py'),
    ('handlers.skills', 'handlers.memory'),
    ('handlers/skills', 'handlers/memory'),
    
    # ── Routes ──
    ('routes/skills.py', 'routes/memory.py'),
    ('routes.skills', 'routes.memory'),
    ('.skills import skills_bp', '.memory import memory_bp'),
    
    # ── JS file references ──
    ('skills.js', 'memory.js'),
    ('static/js/skills.js', 'static/js/memory.js'),
    
    # ── API endpoints (CRITICAL — affects frontend/backend contract) ──
    ('/api/skills/merge', '/api/memory/merge'),
    ('/api/skills/', '/api/memory/'),
    ('/api/skills', '/api/memory'),
    
    # ── Python constants (UPPER_CASE) ──
    ('SKILL_BUDGET_CONTEXT_PERCENT', 'MEMORY_BUDGET_CONTEXT_PERCENT'),
    ('SKILL_ACCUMULATION_INSTRUCTIONS_COMPACT', 'MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT'),
    ('SKILL_ACCUMULATION_INSTRUCTIONS', 'MEMORY_ACCUMULATION_INSTRUCTIONS'),
    ('ALL_SKILL_TOOLS', 'ALL_MEMORY_TOOLS'),
    ('SKILL_TOOL_NAMES', 'MEMORY_TOOL_NAMES'),
    ('CREATE_SKILL_TOOL', 'CREATE_MEMORY_TOOL'),
    ('UPDATE_SKILL_TOOL', 'UPDATE_MEMORY_TOOL'),
    ('DELETE_SKILL_TOOL', 'DELETE_MEMORY_TOOL'),
    ('MERGE_SKILLS_TOOL', 'MERGE_MEMORY_TOOL'),
    ('GLOBAL_SKILLS_DIR', 'GLOBAL_MEMORY_DIR'),
    ('GLOBAL_SKILLS_SUBDIR', 'GLOBAL_MEMORY_SUBDIR'),
    ('PROJECT_SKILLS_SUBDIR', 'PROJECT_MEMORY_SUBDIR'),
    ('MIN_DESCRIPTION_LENGTH', 'MIN_DESCRIPTION_LENGTH'),  # keep same
    ('_LEGACY_GLOBAL_SKILLS_DIR', '_LEGACY_GLOBAL_MEMORY_DIR'),
    ('MAX_LISTING_DESC_CHARS', 'MAX_LISTING_DESC_CHARS'),  # keep same
    
    # ── Python function/variable names ──
    ('inject_skills_to_user', 'inject_memory_to_user'),
    ('_inject_skills', '_inject_memory'),
    ('build_skills_context', 'build_memory_context'),
    ('filter_relevant_skills', 'filter_relevant_memories'),
    ('_build_skill_doc', '_build_memory_doc'),
    ('list_all_skills', 'list_all_memories'),
    ('list_skills', 'list_memories'),
    ('get_skill', 'get_memory'),
    ('get_enabled_skills', 'get_enabled_memories'),
    ('get_eligible_skills', 'get_eligible_memories'),
    ('create_skill', 'create_memory'),
    ('update_skill', 'update_memory'),
    ('delete_skill', 'delete_memory'),
    ('merge_skills', 'merge_memories'),
    ('toggle_skill', 'toggle_memory'),
    ('_make_skill_id', '_make_memory_id'),
    ('_skill_from_file', '_memory_from_file'),
    ('_write_skill_file', '_write_memory_file'),
    ('_list_skills_in_dir', '_list_memories_in_dir'),
    ('_get_global_skills_dir', '_get_global_memory_dir'),
    ('_migrate_legacy_global_skills', '_migrate_legacy_global_memories'),
    ('_check_skill_eligible', '_check_memory_eligible'),
    ('_strip_old_skills_listing', '_strip_old_memory_listing'),
    ('_SKILLS_MARKER', '_MEMORY_MARKER'),
    ('_prefetch_skills', '_prefetch_memory'),
    ('_prefetch_skills_future', '_prefetch_memory_future'),
    ('_SKILL_OP_DISPATCH', '_MEMORY_OP_DISPATCH'),
    ('_skill_create', '_memory_create'),
    ('_skill_update', '_memory_update'),
    ('_skill_delete', '_memory_delete'),
    ('_skill_merge', '_memory_merge'),
    ('_handle_skill_tool', '_handle_memory_tool'),
    
    # ── Python config key names (camelCase in JSON config from frontend) ──
    ('skillsEnabled', 'memoryEnabled'),
    ('skills_enabled', 'memory_enabled'),
    ('skillOk', 'memoryOk'),
    ('skillName', 'memoryName'),
    ('skillScope', 'memoryScope'),
    
    # ── LLM tool function names (CRITICAL — must match tool definitions) ──
    ('"create_skill"', '"create_memory"'),
    ('"update_skill"', '"update_memory"'),
    ('"delete_skill"', '"delete_memory"'),
    ('"merge_skills"', '"merge_memories"'),
    ("'create_skill'", "'create_memory'"),
    ("'update_skill'", "'update_memory'"),
    ("'delete_skill'", "'delete_memory'"),
    ("'merge_skills'", "'merge_memories'"),
    
    # ── LLM tool parameter names ──
    ('"skill_id"', '"memory_id"'),
    ("'skill_id'", "'memory_id'"),
    ('"skill_ids"', '"memory_ids"'),
    ("'skill_ids'", "'memory_ids'"),
    ('skill_id', 'memory_id'),  # variable name  
    ('skill_ids', 'memory_ids'),  # variable name
    
    # ── JavaScript function/variable names ──
    ('toggleSkills()', 'toggleMemory()'),
    ('toggleSkills;', 'toggleMemory;'),
    ("toggleSkills'", "toggleMemory'"),
    ('toggleSkillsFromModal', 'toggleMemoryFromModal'),
    ('openSkillsModal', 'openMemoryModal'),
    ('closeSkillsModal', 'closeMemoryModal'),
    ('_updateSkillsModalBtn', '_updateMemoryModalBtn'),
    ('toggleSkillsAddForm', 'toggleMemoryAddForm'),
    ('switchSkillsTab', 'switchMemoryTab'),
    ('filterSkillsList', 'filterMemoryList'),
    ('refreshSkillsList', 'refreshMemoryList'),
    ('_renderSkillCards', '_renderMemoryCards'),
    ('_buildSkillCardEl', '_buildMemoryCardEl'),
    ('_updateSkillsStats', '_updateMemoryStats'),
    ('_renderSkillBody', '_renderMemoryBody'),
    ('toggleSkillBody', 'toggleMemoryBody'),
    ('toggleSkillEnabled', 'toggleMemoryEnabled'),
    ('deleteSkill', 'deleteMemory'),
    ('createSkillFromModal', 'createMemoryFromModal'),
    ('_skillsCache', '_memoryCache'),
    ('_skillsFilter', '_memoryFilter'),
    ('_applySkillsUI', '_applyMemoryUI'),
    ('updateSubmenuCounts', 'updateSubmenuCounts'),  # keep same
    
    # ── HTML element IDs ──
    ('skillsModal', 'memoryModal'),
    ('skillsBadge', 'memoryBadge'),
    ('skillsToggle', 'memoryToggle'),
    ('skillsSearchInput', 'memorySearchInput'),
    ('skillsList', 'memoryList'),
    ('skillsStats', 'memoryStats'),
    ('skillsAddSection', 'memoryAddSection'),
    ('skillsModalToggleBtn', 'memoryModalToggleBtn'),
    ('skillsModalStatus', 'memoryModalStatus'),
    ('skillNewName', 'memoryNewName'),
    ('skillNewDesc', 'memoryNewDesc'),
    ('skillNewBody', 'memoryNewBody'),
    ('skillNewScope', 'memoryNewScope'),
    ('skillNewTags', 'memoryNewTags'),
    ('mobileSkills', 'mobileMemory'),
    
    # ── CSS class names ──
    ('skills-badge', 'memory-badge'),
    ('sk-dot', 'mem-dot'),
    ('skills-modal', 'memory-modal'),
    ('skills-modal-header', 'memory-modal-header'),
    ('skills-modal-title', 'memory-modal-title'),
    ('skills-modal-subtitle', 'memory-modal-subtitle'),
    ('skills-modal-close', 'memory-modal-close'),
    ('skills-modal-status', 'memory-modal-status'),
    ('skills-stats', 'memory-stats'),
    ('skills-stat', 'memory-stat'),
    ('skills-stat-num', 'memory-stat-num'),
    ('skills-stat-active', 'memory-stat-active'),
    ('skills-stat-label', 'memory-stat-label'),
    ('skills-stat-divider', 'memory-stat-divider'),
    ('skills-toolbar', 'memory-toolbar'),
    ('skills-tabs', 'memory-tabs'),
    ('skills-tab', 'memory-tab'),
    ('skills-search-box', 'memory-search-box'),
    ('skills-search-input', 'memory-search-input'),
    ('skills-search-icon', 'memory-search-icon'),
    ('skills-list', 'memory-list'),
    ('skills-loading', 'memory-loading'),
    ('skills-loading-dot', 'memory-loading-dot'),
    ('skillsDotPulse', 'memoryDotPulse'),
    ('skills-skeleton', 'memory-skeleton'),
    ('skills-empty', 'memory-empty'),
    ('skills-empty-icon', 'memory-empty-icon'),
    ('skills-empty-title', 'memory-empty-title'),
    ('skills-empty-hint', 'memory-empty-hint'),
    ('skill-card-error', 'memory-card-error'),
    ('skills-retry-btn', 'memory-retry-btn'),
    ('skill-card', 'memory-card'),
    ('skill-card-header', 'memory-card-header'),
    ('skill-card-expand-icon', 'memory-card-expand-icon'),
    ('skill-card-name', 'memory-card-name'),
    ('skill-card-scope', 'memory-card-scope'),
    ('skill-card-actions', 'memory-card-actions'),
    ('skill-toggle-switch', 'memory-toggle-switch'),
    ('skill-toggle-track', 'memory-toggle-track'),
    ('skill-toggle-thumb', 'memory-toggle-thumb'),
    ('skill-delete-btn', 'memory-delete-btn'),
    ('skill-card-desc', 'memory-card-desc'),
    ('skill-card-tags', 'memory-card-tags'),
    ('skill-card-tag', 'memory-card-tag'),
    ('skill-card-body', 'memory-card-body'),
    ('skill-card-body-inner', 'memory-card-body-inner'),
    ('skillBodyFadeIn', 'memoryBodyFadeIn'),
    ('skills-add-section', 'memory-add-section'),
    ('skills-add-header', 'memory-add-header'),
    ('skills-input', 'memory-input'),
    ('skills-textarea', 'memory-textarea'),
    ('skills-select', 'memory-select'),
    ('skills-add-row', 'memory-add-row'),
    ('skills-modal-footer', 'memory-modal-footer'),
    ('skills-action-btn', 'memory-action-btn'),
    ('skills-btn-close', 'memory-btn-close'),
    ('skills-btn-on', 'memory-btn-on'),
    ('skills-btn-off', 'memory-btn-off'),
    ('skills-btn-add', 'memory-btn-add'),
    
    # ── Descriptive text in code (docstrings, comments, log messages, user-facing text) ──
    # Be careful — only replace the concept name, not generic English usage
    ('Skill accumulation system', 'Memory accumulation system'),
    ('skill accumulation', 'memory accumulation'),
    ('<skill_accumulation>', '<memory_accumulation>'),
    ('</skill_accumulation>', '</memory_accumulation>'),
    ('Skill management tool handlers', 'Memory management tool handlers'),
    ('skill management tools', 'memory management tools'),
    ('skill CRUD tools', 'memory CRUD tools'),
    ('skill CRUD', 'memory CRUD'),
    ('Skill operation', 'Memory operation'),
    ('skill operation', 'memory operation'),
    ('skill fn_name', 'memory fn_name'),
    
    # ── XML tag in system prompt ──
    ('<available_skills>', '<available_memories>'),
    ('</available_skills>', '</available_memories>'),
    ('available_skills', 'available_memories'),
    
    # ── Remaining "skill" references in strings and text ──
    ('Skill created', 'Memory created'),
    ('Skill updated', 'Memory updated'),
    ('Skill deleted', 'Memory deleted'),
    ('Skill not found', 'Memory not found'),
    ('Skill Failed', 'Memory Failed'),
    ('Skill:', 'Memory:'),
    ('Skill "', 'Memory "'),  # log messages
    ("Skill '", "Memory '"),
    
    # ── Tool category in registry ──
    ("category='skills'", "category='memory'"),
    
    # ── Source label ──
    ("source='Skills'", "source='Memory'"),
    
    # ── Debug/log markers ──
    ('[Skills]', '[Memory]'),
    ('[Skill]', '[Memory]'),
    ('[SkillBM25]', '[MemoryBM25]'),
    ('[SkillsToUser]', '[MemoryToUser]'),
    
    # ── UI text (Chinese) ──
    ('停用 Skills', '停用 Memory'),
    ('启用 Skills', '启用 Memory'),
    ('经验积累 · AI 自动学习并应用的知识库', '记忆积累 · AI 自动学习并应用的知识库'),
    ('还没有积累任何技能', '还没有积累任何记忆'),
    ('AI 在对话中发现有用模式时会自动保存技能', 'AI 在对话中发现有用模式时会自动保存记忆'),
    ('搜索技能', '搜索记忆'),
    ('创建新技能', '创建新记忆'),
    ('技能名称', '记忆名称'),
    ('简短描述 — 什么时候该使用这个技能', '简短描述 — 什么时候该使用这条记忆'),
    ('技能内容', '记忆内容'),
    ('删除此技能', '删除此记忆'),
    ('确定要删除这个 Skill 吗', '确定要删除这条 Memory 吗'),
    ('没有匹配「', '没有匹配「'),  # keep same, dynamic text
    ('的技能', '的记忆'),
    ('个技能', '条记忆'),
    
    # ── Badge text ──
    ('>SKILLS<', '>MEMORY<'),
    
    # ── Special: tool display lookup ──
    ("'create_memory':  { icon:", "'create_memory':  { icon:"),
    
    # ── Accumulated experience text ──
    ('Skills (Accumulated Experience)', 'Memory (Accumulated Experience)'),
    ('Accumulated Experience', 'Accumulated Experience'),  # keep same
    ('Skills — accumulated experience', 'Memory — accumulated experience'),
    
    # ── Remaining generic "skill" → "memory" for log messages and docstrings ──
    # These must come LAST to avoid interfering with more specific patterns
    ('skill file', 'memory file'),
    ('skill files', 'memory files'),
    ('skill dict', 'memory dict'),
    ('Skill dict', 'Memory dict'),
    ('skill listing', 'memory listing'),
    ('skills listing', 'memory listing'),
    ('skill context', 'memory context'),
    ('skills context', 'memory context'),
    ('skill ID', 'memory ID'),
    ('skill IDs', 'memory IDs'),
    ("a skill", "a memory"),
    ("A skill", "A memory"),
    ("the skill", "the memory"),
    ("The skill", "The memory"),
    ("this skill", "this memory"),
    ("This skill", "this memory"),
    ("new skill", "new memory"),
    ("New skill", "New memory"),
    ("existing skill", "existing memory"),
    ("each skill", "each memory"),
    ("merged skill", "merged memory"),
    ("Merged skill", "Merged memory"),
    ("Merged Skill", "Merged Memory"),
    ("merged_skill", "merged_memory"),
    ("Untitled Skill", "Untitled Memory"),
    ("skill's", "memory's"),
    ("Skill's", "Memory's"),
]

# Files/dirs to skip
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.chatui', 'logs', 'data',
             'uploads', '.project_sessions', 'venv', 'debug/rename_skills_to_memory.py'}
SKIP_FILES = {'rename_skills_to_memory.py', 'CLAUDE.md', 'export.py'}
SKIP_EXTENSIONS = {'.pyc', '.pyo', '.woff', '.woff2', '.ttf', '.ico', '.png',
                   '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.db', '.sqlite',
                   '.pdf', '.zip', '.tar', '.gz'}

# Bundle file — will be regenerated, but we process it too
PROCESS_EXTENSIONS = {'.py', '.js', '.html', '.css', '.md', '.json', '.txt', '.yaml', '.yml', '.cfg', '.ini', '.toml'}


def should_process(filepath):
    """Check if a file should be processed."""
    basename = os.path.basename(filepath)
    if basename in SKIP_FILES:
        return False
    _, ext = os.path.splitext(filepath)
    if ext in SKIP_EXTENSIONS:
        return False
    if ext not in PROCESS_EXTENSIONS:
        return False
    # Skip files in skip dirs
    parts = filepath.split(os.sep)
    for part in parts:
        if part in SKIP_DIRS:
            return False
    return True


def apply_replacements(text, filepath=''):
    """Apply all replacements to text."""
    for old, new in REPLACEMENTS:
        if old == new:
            continue
        text = text.replace(old, new)
    return text


def process_file(filepath):
    """Process a single file, applying replacements."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return False
    
    new_content = apply_replacements(content, filepath)
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    return False


def main():
    # Step 1: Rename directories and files
    print("Step 1: Renaming directories and files...")
    for src, dst in DIR_RENAMES:
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst) if os.path.dirname(dst) else '.', exist_ok=True)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                shutil.rmtree(src)
                print(f"  Renamed dir: {src} → {dst}")
            else:
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.copy2(src, dst)
                os.remove(src)
                print(f"  Renamed file: {src} → {dst}")
        else:
            print(f"  SKIP (not found): {src}")
    
    # Step 2: Apply text replacements
    print("\nStep 2: Applying text replacements...")
    changed_count = 0
    for root, dirs, files in os.walk('.'):
        # Skip directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            filepath = os.path.join(root, fname)
            if should_process(filepath):
                if process_file(filepath):
                    changed_count += 1
                    print(f"  Modified: {filepath}")
    
    print(f"\nDone! Modified {changed_count} files.")
    print("\nNext steps:")
    print("  1. Rebuild JS bundle: python3 -c 'from lib.js_bundler import build_bundle; build_bundle()'")
    print("  2. Test the server: python3 server.py")
    print("  3. Verify API endpoints work: curl http://localhost:15000/api/memory")


if __name__ == '__main__':
    main()
