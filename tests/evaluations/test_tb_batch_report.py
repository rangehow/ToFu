"""The batch report does not present finished calls per minute as the relay's start pace.

The relay's RequestWindow admits at most max_rpm upstream starts in any 60 s. Its records are stamped when the call
ends, carry no start time, and their rpm_wait_s omits time queued behind the window's lock, so starts per minute
cannot be rebuilt from them (EVAL-MATRIX-2, 2026-11-13: an estimate from latency and rpm_wait_s showed impossible
over-cap minutes). The report counts finished calls by log time and names the figure that way.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from evaluations.terminal_bench.relay import RequestWindow

ANALYSIS = Path("/path/to/your/project")
sys.path.insert(0, str(ANALYSIS))
spec = importlib.util.spec_from_file_location("batch_report", ANALYSIS / "batch_report.py")
batch_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch_report)

pytestmark = pytest.mark.unit

MINUTE = 1_794_000_000 // 60 * 60


def test_finished_calls_are_counted_in_the_minute_they_were_logged(tmp_path, monkeypatch):
    relay = tmp_path / "relays" / "glm-5.2"
    relay.mkdir(parents=True)
    records = [{"kind": "relay_start", "max_rpm": 2, "ts": MINUTE - 120}] + [
        {"kind": "chat", "key": "4861", "status": 200, "ts": MINUTE + 125 + i, "latency_s": latency}
        for i, latency in enumerate((120, 118, 60, 58))]
    (relay / "requests.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    monkeypatch.setattr(batch_report, "ROOT", tmp_path)
    bucket = batch_report.relay_window(MINUTE - 60, MINUTE + 600)[("4861", "glm-5.2", "glm-5.2")]
    assert dict(bucket["minutes"]) == {MINUTE // 60 + 2: 4}
    assert not hasattr(batch_report, "minutes_over_cap")


def test_the_relay_window_admits_at_most_its_limit_in_any_sixty_seconds():
    clock = {"now": 0.0}

    async def sleep(seconds):
        clock["now"] += seconds

    window = RequestWindow(2, clock=lambda: clock["now"], sleep=sleep)

    async def three_starts():
        starts = []
        for _ in range(3):
            await window.acquire()
            starts.append(clock["now"])
        return starts

    assert asyncio.run(three_starts()) == [0.0, 0.0, 60.0]


# 2026-11-14 (EVAL-MATRIX-3): the checkpoint's production scan missed request-scoped routes ("429 rate-limited on
# ephemeral_<hex>:<wire>", attributed only by the body's App:**<tail>) and counted any line containing the phrase,
# including agents quoting it. It now attributes and classifies exactly as tools/key_stop_guard.py does, and lists
# production's DeepSeek lines on ...4861 (coordinator c97e7a36).
def test_production_scan_attributes_ephemeral_routes_and_lists_4861_deepseek_lines(tmp_path, monkeypatch):
    def at(clock, text):
        return f"2026-11-14 {clock} {text}"
    lines = [
        at("08:10:28", "[INFO] lib.llm_dispatch.api [tofu-durable_23]: [Summary] 429 rate-limited on "
                       "ephemeral_9aa97b1c78b4e554:gpt-6-luna (cycle #2) — body: API HTTP 429: {\"status\":429,"
                       "\"message\":\"App:**4861在模型:gpt-6-luna的tpm超过限制\"}"),
        at("08:11:00", "[INFO] lib.llm_dispatch.api [x]: [Fetch] 429 rate-limited on credential-1788712374132-sts18d:"
                       "dep_1:gemini-4-argon (cycle #1) — body: API HTTP 429: {\"status\":429,\"message\":"
                       "\"App:**6672在模型:gemini-4-argon每分钟请求次数超过限制\"}"),
        at("08:12:00", "[INFO] lib.tasks_pkg.handlers._adapter [x]: send_project_message called with args={'content': "
                       "\"429 rate-limited on cred_ec6300b9f58ffa14e3e6df16:dep_1:deepseek-v4-flash-yourprovider \"}"),
        at("08:13:00", "[WARNING] lib.llm.chat [x]: [DailyReport][D:cred_ec6300b9f58ffa14e3e6df16:dep_7:"
                       "deepseek-v4-pro-baidu] Transport failed during read: Response read timed out"),
        at("08:14:00", "[INFO] lib.llm_dispatch.api [x]: [Fetch] 429 rate-limited on cred_ec6300b9f58ffa14e3e6df16:"
                       "dep_19:deepseek-v4-flash-yourprovider (cycle #1) — body: API HTTP 429: {\"status\":429,\"message\":"
                       "\"App:**4861在模型:deepseek-v4-flash-yourprovider每分钟请求次数超过限制\"}"),
    ]
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(batch_report, "TOFU", tmp_path)
    since, until = batch_report.epoch("2026-11-14 08:00"), batch_report.epoch("2026-11-14 09:00")
    _, throttled, deepseek_4861 = batch_report.production(since, until)
    assert throttled == {("4861", "gpt-6-luna"): 1, ("6672", "gemini-4-argon"): 1,
                         ("4861", "deepseek-v4-flash-yourprovider"): 1}
    assert deepseek_4861 == {("deepseek-v4-pro-baidu", "log"): 1, ("deepseek-v4-flash-yourprovider", "trip"): 1}
