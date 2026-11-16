"""Provider-facing memory tool schemas backed by shared payload limits."""

from lib.memory.contracts import (
    MEMORY_BODY_MAX_CHARS,
    MEMORY_DESCRIPTION_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    MEMORY_MERGE_MAX_ITEMS,
    MEMORY_NAME_MAX_CHARS,
    MEMORY_POINTER_MAX_CHARS,
    MEMORY_RECORD_AS_NEW,
    MEMORY_SEARCH_MODES,
    MEMORY_SEARCH_QUERY_MAX_CHARS,
    MEMORY_SEARCH_TOP_K_DEFAULT,
    MEMORY_SEARCH_TOP_K_MAX,
    MEMORY_SEARCH_TOP_K_MIN,
    MEMORY_STATUSES,
    MEMORY_STATUS_REASONS,
    MEMORY_TAG_MAX_CHARS,
    MEMORY_TAG_MAX_ITEMS,
    MEMORY_VERDICTS,
)

__all__ = ['ALL_MEMORY_TOOLS', 'MEMORY_TOOL_NAMES',
           'CREATE_MEMORY_TOOL', 'UPDATE_MEMORY_TOOL',
           'DELETE_MEMORY_TOOL', 'MERGE_MEMORY_TOOL',
           'SEARCH_MEMORIES_TOOL']


CREATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "create_memory",
        "description": (
            "Save a verified reusable lesson: project convention, "
            "reproduced failure/root cause/fix, or documented tool/API quirk. "
            "Never store secrets (refused), personal details about anyone, "
            "identity/preferences (use My Context), one-off "
            "requests, chat summaries, speculation, reasoning, or transcripts. "
            "When existing lessons match, the first call writes nothing and shows them; "
            "call again with repeat_of."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_DESCRIPTION_MAX_CHARS,
                    "description": (
                        "Write first: dense sentence front-loading searchable "
                        "symptom, symbol/file, and verified fix/rule; drives "
                        "search and prefetch."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_NAME_MAX_CHARS,
                    "description": "Short descriptive title."
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_BODY_MAX_CHARS,
                    "description": (
                        "Concise Markdown: context, reusable rule/fix, and "
                        "verification evidence; no chat/reasoning recap."
                    ),
                },
                "tags": {
                    "type": "array",
                    "maxItems": MEMORY_TAG_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_TAG_MAX_CHARS,
                    },
                    "description": "Optional category tags."
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": (
                        "global applies across projects; project only to the "
                        "current project. Default: project"
                    ),
                },
                "repeat_of": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_ID_MAX_CHARS,
                    "description": (
                        f"Your decision after the matching lessons were shown: "
                        f"\"{MEMORY_RECORD_AS_NEW}\" records a new lesson; a shown lesson's "
                        "id records that this mistake happened again despite it: your "
                        "complete text becomes its new version and its repeat count rises."
                    ),
                }
            },
            "required": ["description", "name", "body"],
            "additionalProperties": False,
        }
    }
}

UPDATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": (
            "Correct or extend an existing memory. A new name, description or body "
            "is saved as a new version that supersedes this one (the old stays as "
            "history; the result names the new id); tags change in place. When you "
            "verified a lesson is wrong or replaced, set status=disproved or superseded "
            "with status_reason and superseded_by instead of deleting it. verdict "
            "alone records what your run showed about a lesson; it settles when a "
            "reviewer accepts your result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "maxLength": MEMORY_DESCRIPTION_MAX_CHARS,
                    "description": (
                        "Write first: one dense sentence front-loading symptom, "
                        "symbol/file, and verified fix/rule for search/prefetch."
                    ),
                },
                "memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_ID_MAX_CHARS,
                    "description": (
                        "ID from search_memories or <relevant_memories>."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_NAME_MAX_CHARS,
                    "description": "New title."
                },
                "body": {
                    "type": "string",
                    "maxLength": MEMORY_BODY_MAX_CHARS,
                    "description": "New complete Markdown body; replaces the old body."
                },
                "tags": {
                    "type": "array",
                    "maxItems": MEMORY_TAG_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_TAG_MAX_CHARS,
                    },
                    "description": "Complete replacement tag list."
                },
                "status": {
                    "type": "string",
                    "enum": list(MEMORY_STATUSES),
                    "description": (
                        "disproved or superseded needs status_reason and "
                        "superseded_by; active restores the lesson."),
                },
                "status_reason": {
                    "type": "string",
                    "enum": list(MEMORY_STATUS_REASONS),
                },
                "superseded_by": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        key: {"type": "string", "minLength": 1,
                              "maxLength": MEMORY_POINTER_MAX_CHARS}
                        for key in ("item_id", "conversation_id", "turn_id", "at", "evidence")
                    },
                    "description": (
                        "Exactly one of: {item_id} of the replacing memory; the "
                        "owner Turn {conversation_id, turn_id, at}; or {evidence}: "
                        "what you verified, e.g. a command and its result."),
                },
                "verdict": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["result", "evidence"],
                    "properties": {
                        "result": {"type": "string", "enum": list(MEMORY_VERDICTS)},
                        "evidence": {"type": "string", "minLength": 1,
                                     "maxLength": MEMORY_POINTER_MAX_CHARS},
                    },
                    "description": (
                        "Project Workers only, with no other change: your run confirmed "
                        "or contradicted this lesson; evidence is the command and result. "
                        "Accepted, it makes the lesson verified or disproved."),
                }
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        }
    }
}

DELETE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_memory",
        "description": "Delete an obsolete, incorrect, or duplicate memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_ID_MAX_CHARS,
                    "description": "Memory ID to delete."
                }
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        }
    }
}

MERGE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "merge_memories",
        "description": (
            "Consolidate 2–32 overlapping memories, then delete originals. "
            "Use when one replacement is clearer; deletion failures are reported."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_ids": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": MEMORY_MERGE_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_ID_MAX_CHARS,
                    },
                    "description": "Unique source memory IDs."
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_DESCRIPTION_MAX_CHARS,
                    "description": (
                        "Write first: dense sentence front-loading merged search "
                        "triggers and the verified rule."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_NAME_MAX_CHARS,
                    "description": "Merged memory title."
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_BODY_MAX_CHARS,
                    "description": "Complete consolidated Markdown body."
                },
                "tags": {
                    "type": "array",
                    "maxItems": MEMORY_TAG_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_TAG_MAX_CHARS,
                    },
                    "description": "Merged tags; omit to union source tags."
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Destination scope. Default: project"
                }
            },
            "required": ["memory_ids", "description", "name", "body"],
            "additionalProperties": False,
        }
    }
}

SEARCH_MEMORIES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memories",
        "description": (
            "Read-only memory lookup. mode=lessons (default): durable project/global "
            "lessons, when a convention or past fix may apply. mode=owner: what Tofu "
            "knows about the owner and why: every My Context item (active, needs "
            "confirmation, superseded) plus matching lessons, including disproved ones, "
            "each with its trust class (owner-stated, verified, inferred, disproved) and "
            "provenance. memory_id instead of query: one item's full text, provenance, "
            "supersession chain and the Turns of this conversation that used it. Not for "
            "local files (read/find/run_command), external facts (web) or skills "
            "(load_skill)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_SEARCH_QUERY_MAX_CHARS,
                    "description": (
                        "Keywords: a symptom, symbol/file, fix or rule; in owner "
                        "mode, the preference or belief in question."),
                },
                "mode": {
                    "type": "string",
                    "enum": list(MEMORY_SEARCH_MODES),
                    "default": "lessons",
                    "description": "lessons (default) or owner.",
                },
                "memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_ID_MAX_CHARS,
                    "description": (
                        "An item id from search results, <relevant_memories> or a "
                        "citation; returns that item and why it is believed."),
                },
                "top_k": {
                    "type": "integer",
                    "minimum": MEMORY_SEARCH_TOP_K_MIN,
                    "maximum": MEMORY_SEARCH_TOP_K_MAX,
                    "default": MEMORY_SEARCH_TOP_K_DEFAULT,
                    "description": "Maximum results."
                }
            },
            "required": [],
            "additionalProperties": False,
        }
    }
}

ALL_MEMORY_TOOLS = [CREATE_MEMORY_TOOL, UPDATE_MEMORY_TOOL, DELETE_MEMORY_TOOL, MERGE_MEMORY_TOOL, SEARCH_MEMORIES_TOOL]
MEMORY_TOOL_NAMES = {'create_memory', 'update_memory', 'delete_memory', 'merge_memories', 'search_memories'}
