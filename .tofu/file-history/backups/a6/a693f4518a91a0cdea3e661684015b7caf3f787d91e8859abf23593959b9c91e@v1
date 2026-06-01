"""lib/tools/emit.py — emit_to_user tool definition.

The ``emit_to_user`` tool allows the model to end its turn by pointing the
user to the most recent tool result instead of re-outputting it verbatim.
This saves output tokens and latency when the tool output speaks for itself.

The referenced tool round is auto-inferred (last tool round in toolRounds),
so the model only needs to provide a brief comment.
"""

EMIT_TO_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_to_user",
        "description": (
            "End your response by pointing the user to the most recent tool result "
            "instead of re-outputting it. The user can already see all tool results "
            "in expandable panels in the UI. Use this when a tool's raw output fully "
            "answers the user's question and you don't need to analyze, transform, or "
            "add significant commentary — just add a brief note.\n\n"
            "This is a TERMINAL tool — calling it ends your turn immediately. "
            "Do NOT call other tools in the same turn after this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "comment": {
                    "type": "string",
                    "description": (
                        "Brief commentary (1-3 sentences). Do NOT repeat the tool output here — "
                        "the user already sees it. Just summarize the key takeaway or confirm the action."
                    )
                }
            },
            "required": ["comment"]
        }
    }
}

EMIT_TO_USER_TOOL_NAMES = {'emit_to_user'}

__all__ = ['EMIT_TO_USER_TOOL', 'EMIT_TO_USER_TOOL_NAMES']
