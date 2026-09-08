"""Task-level Tool Search disclosure continuity."""

from __future__ import annotations

import json

import pytest

from lib.tools.disclosure_state import (
    TOOL_DISCLOSURE_STATE_MAXIMUM,
    disclosed_entries,
    disclosed_names_for_catalog,
    record_search_items,
)

pytestmark = pytest.mark.unit


def _tool(name: str, field: str = "doc") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Read {field}",
            "parameters": {
                "type": "object",
                "properties": {field: {"type": "string"}},
                "required": [field],
            },
        },
    }


def _run_search(task: dict, query: str) -> dict:
    from lib.tasks_pkg.handlers import tool_gateway as handler

    _call_id, content, aborted = handler.handle_search_tools(
        task, {}, "search_tools", "call-search", {"query": query},
        1, {"llmRound": 1}, {}, None, False,
    )
    assert aborted is False
    return json.loads(content)


def test_later_search_suppresses_same_schema_but_revised_schema_returns(
    monkeypatch,
):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    monkeypatch.setattr(handler, "_finalize", lambda *args, **kwargs: None)
    original = _tool("mcp__xuecheng__get_doc_meta", "doc")
    task = {
        "id": "task-disclosure",
        "_executable_tool_catalog": [original],
        "_executableToolNamespaceByName": {},
    }

    first = _run_search(task, "xuecheng document metadata")
    assert [row["name"] for row in first["items"]] == [
        "mcp__xuecheng__get_doc_meta"
    ]

    second = _run_search(task, "document metadata owner")
    assert second["items"] == []
    assert disclosed_names_for_catalog(task, [original]) == {
        "mcp__xuecheng__get_doc_meta"
    }

    revised = _tool("mcp__xuecheng__get_doc_meta", "document_id")
    task["_executable_tool_catalog"] = [revised]
    third = _run_search(task, "xuecheng document metadata")
    assert [row["name"] for row in third["items"]] == [
        "mcp__xuecheng__get_doc_meta"
    ]


def test_state_is_bounded_without_raw_schema():
    task: dict = {}
    for index in range(TOOL_DISCLOSURE_STATE_MAXIMUM + 12):
        record_search_items(task, [{
            "name": f"tool_{index}",
            "arguments_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        }])

    state = task["_toolDisclosureState"]
    assert len(state["entries"]) == TOOL_DISCLOSURE_STATE_MAXIMUM
    assert state["entries"][0]["name"] == "tool_12"
    assert "arguments_schema" not in json.dumps(state)


def test_record_uses_catalog_identity_when_result_schema_is_compacted():
    tool = _tool("mcp__xuecheng__get_doc_meta", "doc")
    task: dict = {}
    record_search_items(
        task,
        [{
            "name": "mcp__xuecheng__get_doc_meta",
            "arguments_schema": {"type": "object", "properties": {}},
        }],
        catalog=[tool],
    )

    assert disclosed_names_for_catalog(task, [tool]) == {
        "mcp__xuecheng__get_doc_meta"
    }


def test_ledger_carries_bounded_argument_signature_without_values():
    tool = _tool("mcp__llm__experiment_run", "ids")
    tool["function"]["parameters"] = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "default": ["sentinel-secret"]},
            "dry_run": {"type": "boolean"},
        },
        "required": ["ids"],
    }
    task: dict = {}
    record_search_items(
        task,
        [{
            "name": "mcp__llm__experiment_run",
            "arguments_schema": {"type": "object", "properties": {}},
        }],
        catalog=[tool],
    )

    entries = disclosed_entries(task)
    assert [row["name"] for row in entries] == ["mcp__llm__experiment_run"]
    assert entries[0]["signature"] == {
        "required": ["ids"], "optional": ["dry_run"]}
    assert "sentinel-secret" not in json.dumps(task["_toolDisclosureState"])


def test_argument_signature_is_bounded_and_never_demotes_required():
    schema = {
        "type": "object",
        "properties": {
            f"arg_{index}": {"type": "string"} for index in range(40)},
        "required": [f"arg_{index}" for index in range(20)],
    }
    task: dict = {}
    record_search_items(
        task, [{"name": "tool_wide", "arguments_schema": schema}])

    signature = disclosed_entries(task)[0]["signature"]
    assert len(signature["required"]) == 16
    assert len(signature["optional"]) == 16
    assert "arg_16" not in signature["optional"]
    assert "arg_19" not in signature["optional"]
    assert signature["optional"][0] == "arg_20"
