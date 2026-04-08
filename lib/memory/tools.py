"""lib/memory/tools.py — Tool definitions for LLM function calling."""

__all__ = ['ALL_MEMORY_TOOLS', 'MEMORY_TOOL_NAMES',
           'CREATE_MEMORY_TOOL', 'UPDATE_MEMORY_TOOL',
           'DELETE_MEMORY_TOOL', 'MERGE_MEMORY_TOOL']


CREATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "create_memory",
        "description": (
            "Save a new memory (accumulated experience) for future sessions. "
            "Call this proactively whenever you discover a bug pattern, project "
            "convention, user preference, complex workflow, or tool/API quirk. "
            "Memories are stored as Markdown files and can be loaded in future "
            "sessions when the user enables them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short descriptive name for the memory"
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what this memory captures"
                },
                "body": {
                    "type": "string",
                    "description": "The full memory content in Markdown — instructions, patterns, conventions, code examples"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization (e.g. ['python', 'testing', 'convention'])"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Where to store the memory: 'global' (all projects) or 'project' (current project only). Default: 'project'"
                }
            },
            "required": ["name", "description", "body"]
        }
    }
}

UPDATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": (
            "Update an existing memory's content, description, or tags. "
            "Use this when you discover new information that extends or corrects "
            "an existing memory, or when a memory's description needs improvement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to update (from list_all_memories or the memory's filename without .md)"
                },
                "name": {
                    "type": "string",
                    "description": "New name for the memory (optional)"
                },
                "description": {
                    "type": "string",
                    "description": "New one-line description (optional)"
                },
                "body": {
                    "type": "string",
                    "description": "New full memory content in Markdown (optional)"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New tags for categorization (optional)"
                }
            },
            "required": ["memory_id"]
        }
    }
}

DELETE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_memory",
        "description": (
            "Remove an outdated, incorrect, or duplicate memory. "
            "Use this when a memory is completely obsolete, contains harmful "
            "misinformation, or is a duplicate of another better memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to delete"
                }
            },
            "required": ["memory_id"]
        }
    }
}

MERGE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "merge_memories",
        "description": (
            "Combine multiple overlapping or related memories into one consolidated memory. "
            "The original memories are deleted after merging. Use this when two or more "
            "memories cover similar topics and would be better as a single comprehensive memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of memory IDs to merge (at least 2)"
                },
                "name": {
                    "type": "string",
                    "description": "Name for the merged memory"
                },
                "description": {
                    "type": "string",
                    "description": "One-line description for the merged memory"
                },
                "body": {
                    "type": "string",
                    "description": "The consolidated memory content in Markdown — combine the best parts of all source memories"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for the merged memory (optional — if omitted, tags from all source memories are combined)"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Where to store the merged memory: 'global' or 'project'. Default: 'project'"
                }
            },
            "required": ["memory_ids", "name", "description", "body"]
        }
    }
}

ALL_MEMORY_TOOLS = [CREATE_MEMORY_TOOL, UPDATE_MEMORY_TOOL, DELETE_MEMORY_TOOL, MERGE_MEMORY_TOOL]
MEMORY_TOOL_NAMES = {'create_memory', 'update_memory', 'delete_memory', 'merge_memories'}
