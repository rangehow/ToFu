"""lib/tools/human_guidance.py — ask_human tool schema for the LLM.

Provides the tool definition that allows the LLM to ask the user a question
mid-generation.  Supports two response modes:
- ``free_text``: user types a free-form answer
- ``choice``: user picks from a list of options provided by the LLM
"""

ASK_HUMAN_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_human",
        "description": (
            "Ask the user questions only for an irreversible decision, subjective "
            "preference/product intent, or a fact unavailable from the conversation, "
            "files, and tools. First inspect context, use a quick read/grep when "
            "useful, and take a sensible reversible default when the user can correct "
            "the result. Do not ask for something you can decide. Supports free text "
            "or choices; `questions[]` asks 2-8 questions as one form. "
            "blocking=true (default) means pause and ask: the turn waits for the "
            "answer, so use it only when nothing useful can proceed without it. "
            "blocking=false shows the question and returns at once: state in the "
            "question which default you apply meanwhile and keep working on what does "
            "not depend on the answer; the reply arrives later as a normal user message "
            "quoting the question. A lead (project Coordinator or Global Router) "
            "withdraws its own open question that the conversation has since settled "
            "with `withdraw: {guidance_id, reason}` and nothing else: the card is "
            "marked withdrawn and stops counting as an open owner question; it stays "
            "answerable, and an owner answer that comes anyway still arrives and "
            "overrides the withdrawal."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "maxLength": 32768,
                    "description": (
                        "Clear, specific MARKDOWN with enough context. To show or scan "
                        "an image, embed `![alt](/api/images/name.png)`; for a login QR "
                        "use `lib.qr.qr_login_question(url)`, never base64."
                    ),
                },
                "response_type": {
                    "type": "string",
                    "enum": ["free_text", "choice"],
                    "description": (
                        "free_text for open answers; choice requires options."
                    ),
                },
                "options": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "maxLength": 1024,
                                "description": "Short button label.",
                            },
                            "description": {
                                "type": "string",
                                "maxLength": 8192,
                                "description": "Optional explanation.",
                            },
                        },
                        "required": ["label"],
                    },
                    "description": "1-16 choices for choice mode.",
                },
                "questions": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "maxLength": 32768,
                                "description": "Question markdown.",
                            },
                            "response_type": {
                                "type": "string",
                                "enum": ["free_text", "choice"],
                                "description": "free_text or choice.",
                            },
                            "options": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 16,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "maxLength": 1024,
                                        },
                                        "description": {
                                            "type": "string",
                                            "maxLength": 8192,
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                        },
                        "required": ["question", "response_type"],
                    },
                    "description": (
                        "Batch mode: 2-8 questions asked as one form (single interaction). "
                        "When present, top-level question/response_type/options are ignored."
                    ),
                },
                "blocking": {
                    "type": "boolean",
                    "description": (
                        "Default true: wait for the answer. false: show the question "
                        "(or form) and return at once."
                    ),
                },
                "withdraw": {
                    "type": "object",
                    "properties": {
                        "guidance_id": {
                            "type": "string",
                            "maxLength": 256,
                            "description": "The hg_ id of your own open question.",
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                            "description": "What settled it, e.g. the owner's answer in chat.",
                        },
                    },
                    "required": ["guidance_id", "reason"],
                    "description": (
                        "Withdraw your own open question instead of asking; send no "
                        "other field."
                    ),
                },
            },
            "anyOf": [
                {
                    "properties": {
                        "response_type": {"const": "free_text"},
                        "questions": {"minItems": 2},
                    },
                    "required": ["questions", "response_type"],
                },
                {
                    "properties": {
                        "response_type": {"const": "choice"},
                        "questions": {"minItems": 2},
                    },
                    "required": ["questions", "response_type"],
                },
                {
                    "properties": {
                        "response_type": {"const": "free_text"},
                        "question": {"minLength": 1},
                    },
                    "required": ["question", "response_type"],
                },
                {
                    "properties": {
                        "response_type": {"const": "choice"},
                        "question": {"minLength": 1},
                        "options": {"minItems": 1},
                    },
                    "required": ["question", "options", "response_type"],
                },
                {"required": ["withdraw"]},
            ],
        },
    },
}

ASK_HUMAN_TOOL_NAME = 'ask_human'
HUMAN_GUIDANCE_TOOL_NAMES = frozenset({ASK_HUMAN_TOOL_NAME})

__all__ = ['ASK_HUMAN_TOOL', 'ASK_HUMAN_TOOL_NAME', 'HUMAN_GUIDANCE_TOOL_NAMES']
