"""Artificial Analysis measured speed and the derived ``fast`` tier.

AA publishes Performance v3 means (output tokens/s, time to first token, time
to first answer token) beside its Intelligence Index. A Model is fast when its
measured output speed exceeds the median of every model the same snapshot
measured. The catalog block reports the facts; the dispatcher reads the
verdict through ``routing_speed_index`` only from a fresh snapshot of the
current row schema, by exact identity.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from lib.model_catalog import aa


pytestmark = pytest.mark.unit


def _row(name, slug, *, creator="DeepSeek", intelligence=None,
         speed=None, ttft=None, ttfat=None):
    measured = (speed, ttft, ttfat) != (None, None, None)
    return {
        "name": name,
        "slug": slug,
        "model_creator": {"name": creator},
        "evaluations": {"artificial_analysis_intelligence_index": intelligence},
        "performance_v3": {
            "mean_output_tokens_per_second": speed,
            "mean_time_to_first_token_seconds": ttft,
            "mean_time_to_first_answer_token_seconds": ttfat,
        } if measured else None,
    }


# Measured output speeds 40/80/120/160 -> snapshot median 100 tokens/s.
_ROWS = [
    _row("Slow Smart", "slow-smart", creator="Moonshot", intelligence=46.5,
         speed=40.0, ttft=4.0, ttfat=170.0),
    _row("Middle Model", "middle-model", creator="Zhipu", intelligence=27.8,
         speed=80.0, ttft=4.3, ttfat=89.9),
    _row("Quick Smart", "quick-smart", intelligence=32.6,
         speed=160.0, ttft=1.3, ttfat=49.4),
    _row("Quick Plain", "quick-plain", creator="OpenAI", intelligence=27.9,
         speed=120.0, ttft=1.6, ttfat=81.4),
    _row("Unmeasured", "unmeasured", creator="Tencent", intelligence=34.6),
]

_MODELS = [
    {"creator_id": creator, "model_id": model_id, "display_name": ""}
    for creator, model_id in (
        ("moonshot", "slow-smart"), ("zhipu", "middle-model"),
        ("deepseek", "quick-smart"), ("openai", "quick-plain"),
        ("tencent", "unmeasured"),
    )
]


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("TOFU_AA_API_KEY", raising=False)
    monkeypatch.setattr(aa, "_memo", None)
    monkeypatch.setattr(aa, "_disk_memo", None)
    monkeypatch.setattr(aa, "_routing_memo", None)
    monkeypatch.setattr(aa, "_background", {
        "running": False, "last_attempt": 0.0, "thread": None,
    })
    monkeypatch.setattr(
        aa, "_cache_path", lambda: str(tmp_path / "aa_index" / "models.json"))


def _fetch(rows=_ROWS, *, now=None):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": rows, "intelligence_index_version": 5}

    return aa._dataset(
        "owner-key", force=True, fetcher=lambda _url, **_kw: Response(),
        now=time.time() if now is None else now)


def test_performance_v3_means_parse_and_invalid_values_stay_unmeasured():
    rows = aa._parse_dataset({"data": [
        _row("Quick Smart", "quick-smart", speed=160.0, ttft=1.3, ttfat=49.4),
        {**_row("Broken", "broken"), "performance_v3": {
            "mean_output_tokens_per_second": -1,
            "mean_time_to_first_token_seconds": float("nan"),
            "mean_time_to_first_answer_token_seconds": True,
        }},
        _row("Absent", "absent"),
    ]})

    assert [(row["output_tokens_per_second"], row["time_to_first_token_seconds"],
             row["time_to_first_answer_token_seconds"]) for row in rows] == [
        (160.0, 1.3, 49.4), (None, None, None), (None, None, None)]


def test_fast_means_above_the_snapshot_median_and_unmeasured_is_never_fast():
    block = aa._block(_MODELS, _fetch(), key_source="settings", key_hint="")

    assert block["speed_reference"] == {
        "median_output_tokens_per_second": 100.0, "measured_models": 4}
    assert {key: speed["fast"] for key, speed in block["speeds"].items()} == {
        "moonshot::slow-smart": False,
        "zhipu::middle-model": False,
        "deepseek::quick-smart": True,
        "openai::quick-plain": True,
    }
    assert block["speeds"]["deepseek::quick-smart"] == {
        "output_tokens_per_second": 160.0,
        "time_to_first_token_seconds": 1.3,
        "time_to_first_answer_token_seconds": 49.4,
        "fast": True,
        "aa_name": "Quick Smart",
        "aa_slug": "quick-smart",
    }
    # Scores keep their meaning: speed is a separate published fact.
    assert block["scores"]["tencent::unmeasured"]["intelligence"] == 34.6
    assert "speed" not in str(block["scores"])


def test_a_measured_but_unscored_row_reports_speed_without_becoming_a_score():
    rows = [*_ROWS, _row("Speed Only", "speed-only", creator="Alibaba", speed=200.0)]
    model = {"creator_id": "alibaba", "model_id": "speed-only", "display_name": ""}

    block = aa._block([model], _fetch(rows), key_source="settings", key_hint="")

    assert block["speeds"]["alibaba::speed-only"]["fast"] is True
    assert "alibaba::speed-only" not in block["scores"]


def test_no_dataset_reports_no_speed_reference():
    block = aa.aa_block_for_models(_MODELS, api_key="", key_source=None)

    assert block["speeds"] == {}
    assert block["speed_reference"] is None


def test_speed_survives_the_disk_cache():
    _fetch()
    aa._memo = None

    block = aa.aa_block_for_models(
        _MODELS, api_key="owner-key", key_source="settings")

    assert block["status"] == "ok"
    assert block["speeds"]["deepseek::quick-smart"]["output_tokens_per_second"] == 160.0


def test_routing_index_is_empty_without_a_fresh_snapshot():
    assert not aa.routing_speed_index()
    _fetch(now=time.time() - aa._ttl_seconds() - 60)

    assert not aa.routing_speed_index()


def test_routing_index_serves_exact_identity_evidence():
    _fetch()
    index = aa.routing_speed_index()

    assert index.evidence("quick-smart") == aa.RoutingSpeed(
        output_tokens_per_second=160.0, intelligence=32.6, fast=True)
    assert index.evidence("Quick.Smart") == index.evidence("quick-smart")
    assert index.evidence("slow-smart").fast is False
    # A variant AA never measured inherits nothing from its base model.
    assert index.evidence("quick-smart-highspeed") is None
    assert index.evidence("unmeasured") is None
    # Unchanged snapshots reuse one index instead of rebuilding per pick.
    assert aa.routing_speed_index() is index


def test_routing_index_withholds_an_identity_two_rows_claim():
    rows = [
        _row("Twin", "twin", creator="A Corp", speed=150.0),
        _row("Twin", "twin", creator="B Corp", speed=50.0),
        *_ROWS,
    ]
    _fetch(rows)

    assert aa.routing_speed_index().evidence("twin") is None


def test_routing_index_honors_a_newer_snapshot_from_another_process():
    _fetch(now=time.time() - 30)
    assert aa.routing_speed_index().evidence("quick-plain").fast is True

    slower = [_row("Quick Plain", "quick-plain", creator="OpenAI",
                   intelligence=27.9, speed=20.0),
              *(row for row in _ROWS if row["slug"] != "quick-plain")]
    aa._write_disk_cache({
        "fetched_at": time.time(),
        "index_version": 5,
        "models": aa._parse_dataset({"data": slower}),
    })

    assert aa.routing_speed_index().evidence("quick-plain").fast is False


def test_an_older_schema_cache_is_never_fresh_and_schedules_a_refresh(monkeypatch):
    # The 2026-11-11 cache predated the speed fields: fresh by age, it
    # silently disabled every speed verdict until its TTL ran out.
    path = aa._cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"fetched_at": time.time(), "index_version": 5, "models": [{
            "aa_name": "Quick Smart", "aa_slug": "quick-smart",
            "aa_creator": "DeepSeek", "intelligence": 32.6,
            "coding": None, "agentic": None, "math": None,
        }]}, handle)
    scheduled = []
    monkeypatch.setattr(
        aa, "_schedule_background_refresh",
        lambda api_key, _now, _fetcher: scheduled.append(api_key))

    assert aa.refresh_when_stale("owner-key") == "stale"
    assert scheduled == ["owner-key"]
    assert not aa.routing_speed_index()


def test_a_current_cache_needs_no_refresh_and_no_key_costs_nothing(monkeypatch):
    _fetch()
    aa._memo = None
    scheduled = []
    monkeypatch.setattr(
        aa, "_schedule_background_refresh",
        lambda *arguments: scheduled.append(arguments))

    assert aa.refresh_when_stale("owner-key") == "ok"
    assert aa.refresh_when_stale("") == "no_key"
    assert scheduled == []
    assert aa.routing_speed_index().evidence("quick-smart").fast is True
