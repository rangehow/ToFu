"""The ...6672 night window opens and closes by rule (EVAL-MATRIX-3, coordinator 2a99a3fb, 2026-11-14).

Production's default and fallback models run only on ...6672 until ...5409's credit resets at 00:00, so the lane's
...6672 runs resume at 00:00 and stop again at 08:30 (or earlier at the spend cap). The opening must not lift a STOP a
guard wrote, must not open after a ...6672 guard trip during the day or on the coordinator's hold, and must re-arm the
spend cap for the new day before any relay opens (the running cap counts from the previous night).
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

TOOL = Path("/path/to/your/project"
            "tools/night_window_6672.py")
spec = importlib.util.spec_from_file_location("night_window_6672", TOOL)
night = importlib.util.module_from_spec(spec)
spec.loader.exec_module(night)

pytestmark = pytest.mark.unit

DAYTIME_AT = time.mktime(time.strptime("2026-11-14 08:20:58", "%Y-%m-%d %H:%M:%S"))


@pytest.fixture
def lane(tmp_path):
    relays = {"glm-5.2@6672": "6672", "Doubao-Seed-2.0-pro@6672": "6672", "claude-opus-5.5": "6672",
              "glm-5.2": "4861", "MiniMax-M2.7@6672": "6672"}
    for relay, key in relays.items():
        (tmp_path / "relays" / relay).mkdir(parents=True)
        (tmp_path / "relays" / relay / "watchdog.sh").write_text(f"relay --port 1 --key-suffixes {key} --max-rpm 5\n")
    (tmp_path / "tools").mkdir()
    daytime = json.dumps({"reason": "daytime rule (coordinator af369b64, 2026-11-14 ~08:20): ...", "ts": DAYTIME_AT})
    for relay in ("glm-5.2@6672", "Doubao-Seed-2.0-pro@6672"):
        (tmp_path / "relays" / relay / "STOP").write_text(daytime)
    (tmp_path / "relays" / "claude-opus-5.5" / "STOP").write_text(json.dumps({"reason": "upstream_429"}))
    (tmp_path / "relays" / "glm-5.2" / "STOP").write_text(daytime)
    return tmp_path


def test_the_opening_lifts_only_daytime_stops_on_6672_relays_after_rearming_the_cap(lane):
    calls = []
    result = night.open_window(lane, DAYTIME_AT, restart_cap=lambda: calls.append("cap") or True)
    assert calls == ["cap"]
    assert sorted(result["lifted"]) == ["Doubao-Seed-2.0-pro@6672", "glm-5.2@6672"]
    assert not (lane / "relays" / "glm-5.2@6672" / "STOP").exists()
    assert list((lane / "relays" / "glm-5.2@6672").glob("STOP.lifted-night-*"))
    assert (lane / "relays" / "claude-opus-5.5" / "STOP").exists()
    assert (lane / "relays" / "glm-5.2" / "STOP").exists()


@pytest.mark.parametrize("blocker", ["hold", "keystop"])
def test_a_hold_or_a_6672_guard_trip_during_the_day_keeps_the_window_closed(lane, blocker):
    if blocker == "hold":
        (lane / "tools" / "NIGHT-6672-HOLD").write_text("coordinator: risk on 6672")
    else:
        stamp = int((DAYTIME_AT + 3600) * 1e9)
        (lane / "relays" / "glm-5.2@6672" / f"KEYSTOP-{stamp}.json").write_text("{}")
    calls = []
    result = night.open_window(lane, DAYTIME_AT, restart_cap=lambda: calls.append("cap") or True)
    assert result["blockers"] and result["lifted"] == [] and calls == []
    assert (lane / "relays" / "glm-5.2@6672" / "STOP").exists()


def test_an_older_guard_trip_does_not_block_and_a_failed_cap_restart_lifts_nothing(lane):
    stamp = int((DAYTIME_AT - 86400) * 1e9)
    (lane / "relays" / "glm-5.2@6672" / f"KEYSTOP-{stamp}.json").write_text("{}")
    result = night.open_window(lane, DAYTIME_AT, restart_cap=lambda: False)
    assert result["blockers"] == [] and result["lifted"] == []
    assert (lane / "relays" / "glm-5.2@6672" / "STOP").exists()


def test_the_close_stops_every_6672_relay_without_overwriting_a_stop(lane):
    (lane / "relays" / "glm-5.2@6672" / "STOP").unlink()
    stopped = night.close_window(lane)
    assert sorted(stopped) == ["MiniMax-M2.7@6672", "glm-5.2@6672"]
    assert json.loads((lane / "relays" / "claude-opus-5.5" / "STOP").read_text())["reason"] == "upstream_429"
    assert not (lane / "relays" / "glm-5.2" / "STOP").read_text().startswith('{"reason": "night window')


# 2026-11-14 09:01 (exercising the restart with the running cap's own arguments): the previous run's 'armed' line names
# the same --since, so it must not confirm a restart; only a line written after the restart began does.
def test_a_stale_armed_line_with_the_same_since_does_not_confirm_a_restart():
    lines = ["2026-11-13 21:19:56 spend_cap_6672: armed: cap $100.00 on ...6672 list-price spend since 2026-11-13 22:00"]
    began = time.mktime(time.strptime("2026-11-14 09:01:47", "%Y-%m-%d %H:%M:%S"))
    assert not night.armed_after(lines, "2026-11-13 22:00", began)
    lines.append("2026-11-14 09:01:48 spend_cap_6672: armed: cap $100.00 on ...6672 list-price spend since "
                 "2026-11-13 22:00")
    assert night.armed_after(lines, "2026-11-13 22:00", began)
    assert not night.armed_after(lines, "2026-11-15 00:00", began)
