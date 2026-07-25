"""lib/paper/podcast_engine — report → spoken-script → audio pipeline.

The paper-podcast worker package (docs/PAPER_PODCAST_DESIGN.md). Layout:

  * ``_validate`` — the deterministic quality gates (LaTeX residue, Unicode
    math symbols, zh abbreviation watchlist, number provenance incl. derived
    channels, structure, duration). A script must pass these before TTS.
  * ``_script``   — prompt assembly, JSON parse/repair, validator-feedback
    revision, critic round, server-side duration estimates.
  * ``_audio``    — per-segment TTS synthesis + WAV concat + MP3 transcode
    (added with the lib/tts layer).
  * this file     — the facade + (with the runtime layer) the task worker
    ``_run_podcast_task``.
"""

from __future__ import annotations

from lib.paper.podcast_engine._script import (  # noqa: F401
    ScriptParseError,
    build_critic_prompt,
    critic_enabled,
    generate_script,
    normalize_script,
    parse_script_json,
    render_figure_list,
    script_plain_text,
    stamp_estimates,
)
from lib.paper.podcast_engine._validate import (  # noqa: F401
    MATH_SYMBOLS,
    check_abbreviations,
    check_duration,
    check_latex_residue,
    check_number_provenance,
    check_structure,
    check_unicode_math,
    estimate_seconds,
    extract_data_numbers,
    validate_script,
)

__all__ = [
    'ScriptParseError',
    'critic_enabled',
    'generate_script',
    'normalize_script',
    'parse_script_json',
    'render_figure_list',
    'script_plain_text',
    'stamp_estimates',
    'MATH_SYMBOLS',
    'check_abbreviations',
    'check_duration',
    'check_latex_residue',
    'check_number_provenance',
    'check_structure',
    'check_unicode_math',
    'estimate_seconds',
    'extract_data_numbers',
    'validate_script',
]
