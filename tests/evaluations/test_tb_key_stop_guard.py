"""Contract for the ...6672 production guard after the 22:20:17 false positive (coordinator a10e84ea).

A routine per-model RPM 429 on a model the eval does not use on ...6672 is logged, not a key-wide stop. A key-level
error, a 429 on an eval-used model, or a user-visible production failure still stops the key. The fixture line is the
real production line from tonight.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

GUARD = Path("/path/to/your/project")
spec = importlib.util.spec_from_file_location("key_stop_guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

pytestmark = pytest.mark.unit

FALSE_POSITIVE = (
    "2026-11-13 22:20:17 [INFO] lib.llm_dispatch.api [tool-parallel_0]: [Fetch:Extract] "
    "429 rate-limited on credential-1788712374132-sts18d:dep_0aa956559c90e2416d48de74:gemini-4-argon "
    "(cycle #1) — body: API HTTP 429: {\"status\":429,\"message\":\"App:**6672在模型:gemini-4-argon每分钟请求次数超过限制\"}"
)
SHARED_429 = FALSE_POSITIVE.replace("gemini-4-argon", "grok-4.6")
KEY_DISABLED = (
    "2026-11-13 22:21:00 [WARNING] lib.llm.llm_errors [x]: [Task t][D:credential-1788712374132-sts18d:"
    "dep_1:grok-4.6] Permission error (HTTP 403): API HTTP 403: {\"status\":403,\"message\":\"App ID 已被禁用\"}"
)
USER_ERROR = "2026-11-13 22:21:02 [ERROR] lib.llm_dispatch.api [x]: response to user failed: upstream error"


def test_tonights_non_eval_per_model_rpm_refusal_is_not_a_trip():
    assert guard.classify_production_line(FALSE_POSITIVE, {"grok-4.6"}) is None


def test_a_429_on_an_eval_used_model_is_a_shared_bucket_trip():
    event = guard.classify_production_line(SHARED_429, {"grok-4.6"})
    assert event["action"] == "trip" and event["key"] == "6672" and event["model"] == "grok-4.6"


def test_key_level_disabled_errors_trip_even_on_a_non_eval_model():
    event = guard.classify_production_line(KEY_DISABLED, {"grok-4.6"})
    assert event["action"] == "trip" and event["key"] == "6672"


def test_user_visible_production_errors_trip():
    assert guard.classify_production_line(USER_ERROR, {"grok-4.6"})["action"] == "trip"


def test_a_429_for_a_non_eval_model_on_another_key_is_not_a_6672_trip():
    assert guard.classify_production_line(FALSE_POSITIVE.replace("6672", "4861"), {"grok-4.6"}) is None


def test_production_model_alias_of_an_eval_model_still_trips():
    line = SHARED_429.replace("grok-4.6", "kunlunbedrock.claude-opus-5.5-m17")
    event = guard.classify_production_line(line, {"claude-opus-5.5"})
    assert event["action"] == "trip" and event["model"] == "kunlunbedrock.claude-opus-5.5-m17"


# EVAL-MATRIX-3, 2026-11-14 overnight rules (coordinator): ...6672 is production's only key for claude-opus-5.5 and
# kimi-k3.1-pro, so the lane's ...6672 relays pause on ANY 429 there, on any model, production's own included
# (--prod-any-429 6672); and a 402 (credit exhausted) on the lane's own traffic stops its key like 401/403/429.
def test_with_prod_any_429_a_non_eval_model_429_on_that_key_is_a_trip():
    event = guard.classify_production_line(FALSE_POSITIVE, {"grok-4.6"}, any_model_keys={"6672"})
    assert event["action"] == "trip" and event["key"] == "6672" and event["model"] == "gemini-4-argon"


def test_prod_any_429_on_another_key_leaves_6672_classification_unchanged():
    assert guard.classify_production_line(FALSE_POSITIVE, {"grok-4.6"}, any_model_keys={"4861"}) is None


def test_a_402_on_the_lanes_own_relay_traffic_stops_its_key():
    row = '{"kind": "chat", "status": 402, "key": "4861", "error": "您的Credit已耗尽"}'
    assert guard.own_event(row) == "HTTP 402"


# 2026-11-14 07:08:51: another lane's MiniMax-M2.7 run filled the ...4861 M2.7 bucket; this lane's M2.7 relay got one
# retried 429 and the guard stopped every ...4861 relay (grok-4.6, glm-5.2, MiniMax-M3, ...). The coordinator's rule
# for ...4861 is stop on the first 429 PER MODEL (--own-per-model 4861); a key-level answer still stops the key.
def test_on_a_per_model_key_an_own_429_stops_only_that_models_relays(tmp_path):
    for relay, key in (("MiniMax-M2.7", "4861"), ("grok-4.6", "4861"), ("MiniMax-M2.7@6672", "6672")):
        (tmp_path / "relays" / relay).mkdir(parents=True)
        (tmp_path / "relays" / relay / "watchdog.sh").write_text(f"relay --port 1 --key-suffixes {key} --max-rpm 5\n")
    relays = guard.relay_keys(tmp_path)
    scope = guard.own_trip_scope("4861", "MiniMax-M2.7", "HTTP 429 (retried)", {"4861"})
    assert scope == "MiniMax-M2.7"
    assert guard.trip(tmp_path, "4861", "own", "MiniMax-M2.7", "429", relays, only_model=scope) == ["MiniMax-M2.7"]
    assert not (tmp_path / "relays" / "grok-4.6" / "STOP").exists()
    assert not (tmp_path / "relays" / "MiniMax-M2.7@6672" / "STOP").exists()


def test_a_key_level_answer_or_a_key_wide_rule_still_stops_the_whole_key():
    assert guard.own_trip_scope("4861", "MiniMax-M2.7", "HTTP 402", {"4861"}) is None
    assert guard.own_trip_scope("4861", "MiniMax-M2.7", "key_retired 401", {"4861"}) is None
    assert guard.own_trip_scope("6672", "glm-5.2@6672", "HTTP 429", {"4861"}) is None


# 2026-11-14 08:10: production 429s on a request-scoped route name no credential, only the AppId in the body. The guard
# matched credential routes only, so an ephemeral-route 429 on ...6672 would not have tripped --prod-any-429 6672.
EPHEMERAL_4861 = (
    "2026-11-14 08:10:28 [INFO] lib.llm_dispatch.api [tofu-durable_23]: [rid:8a590194-f014-4bb1-894e-8cab4b123407] "
    "[Task 8a590194-f014-4bb1-894e-8cab4b123407][Summary] 429 rate-limited on ephemeral_9aa97b1c78b4e554:gpt-6-luna "
    "(cycle #2) — body: API HTTP 429: {\"status\":429,\"message\":\"App:**4861在模型:gpt-6-luna的tpm超过限制\","
    "\"data\":null,\"ext\":{\"error\":{\"source\":\"AIGC\",\"service\":\"aigc\",\"stage\":\"validation\"}}}"
)
EPHEMERAL_6672 = EPHEMERAL_4861.replace("App:**4861", "App:**6672")


def test_an_ephemeral_route_429_is_attributed_by_the_appid_in_its_body():
    event = guard.classify_production_line(EPHEMERAL_6672, set(), any_model_keys={"6672"})
    assert event["action"] == "trip" and event["key"] == "6672" and event["model"] == "gpt-6-luna"
    assert guard.classify_production_line(EPHEMERAL_4861, set(), any_model_keys={"6672"}) is None


# Coordinator c97e7a36 (2026-11-14): deepseek-v4-flash runs on ...4861, where production calls the DeepSeek wires
# deepseek-v4-pro-baidu and deepseek-v4-flash-yourprovider. A production 429 on a DeepSeek wire there, or a quota / 'ended for
# the day' answer, stops this lane's DeepSeek relays on ...4861 for the day; timeouts and empty responses do not.
DS_RULES = [("4861", "deepseek")]
CREDS = {"cred_ec6300b9f58ffa14e3e6df16": "4861", "credential-1788712374132-sts18d": "6672"}
PROD_DS_429 = (
    "2026-11-14 09:00:01 [INFO] lib.llm_dispatch.api [tool-parallel_1]: [Fetch:Extract] 429 rate-limited on "
    "cred_ec6300b9f58ffa14e3e6df16:dep_19a32024f42020b3041fd7ec:deepseek-v4-flash-yourprovider (cycle #1) — body: API HTTP "
    "429: {\"status\":429,\"message\":\"App:**4861在模型:deepseek-v4-flash-yourprovider每分钟请求次数超过限制\"}"
)
PROD_DS_DAILY = PROD_DS_429.replace("每分钟请求次数超过限制", "每天请求次数超过限制")
PROD_DS_EPHEMERAL = EPHEMERAL_4861.replace("gpt-6-luna", "deepseek-v4-pro-baidu")
PROD_DS_QUOTA = (
    "2026-11-14 09:00:02 [WARNING] lib.llm_dispatch.api [x]: [DailyReport] Quota exhausted on "
    "cred_ec6300b9f58ffa14e3e6df16:dep_7d922d1710e4bfc78f28339a:deepseek-v4-pro-baidu — disabling key for today: API "
    "HTTP 402: {\"status\":402,\"message\":\"您的Credit已耗尽\"}"
)
# Real production lines on ...4861's DeepSeek wires on 2026-11-14: a read timeout, an empty stream, a success.
PROD_DS_TIMEOUT = (
    "2026-11-14 00:04:31 [WARNING] lib.llm.chat [tofu-scheduled-system-1-daily-report-backfill]: [DailyReport]"
    "[D:cred_ec6300b9f58ffa14e3e6df16:dep_7d922d1710e4bfc78f28339a:deepseek-v4-pro-baidu] Transport failed during "
    "read: Response read timed out (timeout budget: 90s)"
)
PROD_DS_EMPTY = (
    "2026-11-14 01:15:44 [WARNING] lib.llm_dispatch.api [tool-parallel_1]: [Dispatch] Unusable provider stream on "
    "cred_ec6300b9f58ffa14e3e6df16:dep_7d922d1710e4bfc78f28339a:deepseek-v4-pro-baidu (state=empty_response) — "
    "recorded soft failure (consecutive_errors=1)"
)
PROD_DS_OK = (
    "2026-11-14 06:03:14 [INFO] lib.daily_report.llm [tofu-scheduled-system-1-daily-report-backfill]: [DailyReport] "
    "LLM via cred_ec6300b9f58ffa14e3e6df16:dep_19a32024f42020b3041fd7ec:deepseek-v4-flash-yourprovider in 12671ms"
)
# Tool-call adapter lines echo message text: an agent quoting a 429 is not a production 429.
QUOTED_BY_AN_AGENT = (
    "2026-11-14 08:13:58 [INFO] lib.tasks_pkg.handlers._adapter [tool-parallel_2]: [ProjectMessages] "
    "send_project_message called with args={'content': \"429 rate-limited on cred_ec6300b9f58ffa14e3e6df16:dep_1:"
    "deepseek-v4-flash-yourprovider App:**4861在模型:deepseek-v4-flash每分钟请求次数超过限制\"}"
)
PROD_DS_429_ON_6672 = PROD_DS_429.replace("App:**4861", "App:**6672").replace(
    "cred_ec6300b9f58ffa14e3e6df16", "credential-1788712374132-sts18d")


@pytest.mark.parametrize("line", [PROD_DS_429, PROD_DS_DAILY, PROD_DS_EPHEMERAL, PROD_DS_QUOTA])
def test_a_production_limit_on_a_deepseek_wire_on_4861_stops_our_deepseek_relays(line):
    event = guard.classify_production_model_stop(line, DS_RULES, CREDS)
    assert event["action"] == "trip" and event["key"] == "4861" and "deepseek" in event["model"]


@pytest.mark.parametrize("line", [PROD_DS_TIMEOUT, PROD_DS_EMPTY, PROD_DS_OK])
def test_timeouts_empty_responses_and_successes_are_scanned_not_a_stop(line):
    assert guard.classify_production_model_stop(line, DS_RULES, CREDS)["action"] == "log"


@pytest.mark.parametrize("line", [EPHEMERAL_4861, QUOTED_BY_AN_AGENT, PROD_DS_429_ON_6672])
def test_other_wires_other_keys_and_quoted_text_do_not_stop_deepseek(line):
    assert guard.classify_production_model_stop(line, DS_RULES, CREDS) is None


def test_the_stop_reaches_only_this_lanes_deepseek_relays_on_that_key(tmp_path):
    for relay, key in (("deepseek-v4-flash@4861", "4861"), ("deepseek-v4-flash", "6672"), ("grok-4.6", "4861")):
        (tmp_path / "relays" / relay).mkdir(parents=True)
        (tmp_path / "relays" / relay / "watchdog.sh").write_text(f"relay --port 1 --key-suffixes {key} --max-rpm 5\n")
    relays = guard.relay_keys(tmp_path)
    assert guard.prod_model_stop_scope(relays, "4861", "deepseek") == ["deepseek-v4-flash"]
    for model in guard.prod_model_stop_scope(relays, "4861", "deepseek"):
        guard.trip(tmp_path, "4861", "production", "deepseek-v4-flash-yourprovider", "429", relays, only_model=model)
    assert (tmp_path / "relays" / "deepseek-v4-flash@4861" / "STOP").exists()
    assert not (tmp_path / "relays" / "deepseek-v4-flash" / "STOP").exists()
    assert not (tmp_path / "relays" / "grok-4.6" / "STOP").exists()


# Tool-call adapter lines echo message text, and agents quote whole production log lines (2026-11-14 messages quote
# 429 lines). Production signals count only from their emitting logger at the start of the line.
QUOTED_FULL_LINE = (
    "2026-11-14 08:40:00 [INFO] lib.tasks_pkg.handlers._adapter [tool-parallel_0]: [ProjectMessages] "
    "send_project_message called with args={'content': \"" + EPHEMERAL_6672.replace('"', "'") + " / "
    + KEY_DISABLED.replace('"', "'") + " / " + USER_ERROR + "\"}"
)


def test_a_production_line_quoted_by_an_agent_is_not_a_production_signal():
    assert guard.classify_production_line(QUOTED_FULL_LINE, {"grok-4.6"}, any_model_keys={"6672"}) is None
    learned = {}
    guard.learn(learned, QUOTED_FULL_LINE)
    assert learned == {}
