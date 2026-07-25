"""lib/tools/produce.py — High-level "one sentence → finished video" tool.

The chat surface of the production substrate (docs/PRODUCTION_PIPELINE_DESIGN.md
拍板 #5): a SINGLE semantically-clear tool so the model naturally selects it
when the user says "make me a science-explainer video about <topic>", instead
of orchestrating the 8 low-level ``motion_video_*`` tools by hand.

Unlike the low-level family, ``produce_video`` is NOT project-gated (拍板 #2):
"say one sentence and get a film" cannot require the user to first attach a
project, so a topic job with no project renders under the server's data dir.
It kicks off a background job (research → script → timeline → render →
assemble) and returns immediately with a ``task_id`` the UI polls — the model
does not block on the multi-minute render.
"""

from lib.log import get_logger

logger = get_logger(__name__)

PRODUCE_VIDEO_TOOL_NAME = 'produce_video'

PRODUCE_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "produce_video",
        "description": (
            "Produce a short science-explainer VIDEO from a single topic or "
            "news headline — fully automatic, no storyboard or SRT needed. "
            "The pipeline researches the topic on the web (every claim is "
            "grounded in a real source URL and credited on an end card), "
            "writes a spoken script, times it to real TTS narration, and "
            "renders a narrated vertical MG video. Returns immediately with a "
            "task_id; the video generates in the background (a few minutes) "
            "and the user watches progress in the video panel. Use this when "
            "the user asks for a video ABOUT something and has not supplied "
            "their own script/SRT/storyboard. Does NOT require an attached "
            "project."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The subject or news headline to explain, "
                                   "e.g. '为什么天空是蓝色的' or 'the James Webb "
                                   "telescope's latest exoplanet finding'."
                },
                "lang": {
                    "type": "string",
                    "enum": ["zh", "en"],
                    "description": "Narration language (default zh)."
                },
                "aspect": {
                    "type": "string",
                    "enum": ["1080x1440", "1080x1920", "1920x1080", "1080x1080"],
                    "description": "Frame aspect (default 1080x1440 vertical)."
                },
                "max_scenes": {
                    "type": "integer",
                    "description": "Upper bound on scene count / cost (3..12, "
                                   "default 8). Exceeding it degrades gracefully."
                },
                "narration": {
                    "type": "boolean",
                    "description": "TTS voice-over (default true; degrades to a "
                                   "silent video when no TTS slot is configured)."
                },
                "visual_quality": {
                    "type": "string",
                    "enum": ["template", "authored"],
                    "description": "'template' (default) renders each scene with "
                                   "the fast zero-LLM kinetic-type card. "
                                   "'authored' gives every scene its own small "
                                   "agent that writes a bespoke composition — "
                                   "much better looking, but costs one agent "
                                   "loop per scene and takes longer. A scene "
                                   "whose authoring fails falls back to the "
                                   "template, so the film always completes."
                }
            },
            "required": ["topic"],
        },
    },
}

__all__ = ['PRODUCE_VIDEO_TOOL', 'PRODUCE_VIDEO_TOOL_NAME']
