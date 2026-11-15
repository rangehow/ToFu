"""lib/tools/code_exec.py — Standalone code execution tool definition.

The standalone ``run_command`` tool (no project attached) shares its schema
with the project-mode ``run_command`` so the model gets the same usage
guidance (WHEN-TO-USE matrix, prefer-search_files hint, pipeline warning)
regardless of mode. The only difference is where the shell starts: without a
project it is the server's working directory, not a project root.
"""
import copy

from lib.log import get_logger
from lib.tools.project import PROJECT_TOOL_RUN_COMMAND, RUN_COMMAND_START

logger = get_logger(__name__)

CODE_EXEC_TOOL = copy.deepcopy(PROJECT_TOOL_RUN_COMMAND)
CODE_EXEC_TOOL['function']['description'] = (
    PROJECT_TOOL_RUN_COMMAND['function']['description'].replace(
        RUN_COMMAND_START,
        "Fresh subprocess starting in the server's working directory"))

CODE_EXEC_TOOL_NAMES = {'run_command'}

__all__ = ['CODE_EXEC_TOOL', 'CODE_EXEC_TOOL_NAMES']
