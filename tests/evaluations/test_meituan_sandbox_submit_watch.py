"""提交器（submit）与监控器（watch）的离线单元测试（注入 stub client）。"""
from __future__ import annotations

import pytest

from evaluations.swebench.meituan_sandbox.submit import (
    EVIDENCE_CANVAS_PARAMS,
    MassConfig,
    build_submit_plan,
    execute_submit,
)
from evaluations.swebench.meituan_sandbox.watch import watch_flow

pytestmark = pytest.mark.unit


class _StubCanvas:
    """按提交序列记录调用，支持注入 read-back 失配 / SSO 失效。"""

    def __init__(self) -> None:
        self.logged_in = True
        self.calls: list[tuple[str, object]] = []
        self._params: dict[str, dict] = {}
        self._next_canvas = 400000
        self.model_mismatch = False
        self.anchor_mismatch_once = False

    def check_login(self) -> bool:
        return self.logged_in

    def whoami(self) -> str:
        return "ruanjunhao04"

    def set_canvas_params(self, canvas_id: str, params: dict) -> None:
        self.calls.append(("set", canvas_id))
        self._params.setdefault(canvas_id, {}).update(params)

    def get_canvas_params(self, canvas_id: str) -> dict:
        self.calls.append(("get_params", canvas_id))
        params = dict(self._params.get(canvas_id, {}))
        if self.anchor_mismatch_once and "agent" in params:
            self.anchor_mismatch_once = False
            params["agent"] = "agentix@template@simple@opencode"
        return params

    def fork(self, base_id: str, name: str, model_id: str) -> str:
        self.calls.append(("fork", base_id))
        self._next_canvas += 1
        cid = str(self._next_canvas)
        self._params[cid] = {"_model": model_id}
        return cid

    def get(self, canvas_id: str) -> dict:
        self.calls.append(("get", canvas_id))
        model = self._params.get(canvas_id, {}).get("_model")
        if self.model_mismatch:
            model = "stale-model"
        return {"modelId": model}

    def run(self, canvas_id: str) -> str:
        self.calls.append(("run", canvas_id))
        return f"flow-{canvas_id}"


def _plan(**overrides):
    kwargs = {
        "base_canvas_id": "362610",
        "model_id": "3819071",
        "tofu_commit": "abc123",
        "agentix_branch": "ruanjunhao/tofu-streamfix-20260908",
        "tb_git_branch": "feat/agentix-opencode",
        "evaluate_name": "tofu-sandbox-test",
    }
    kwargs.update(overrides)
    return build_submit_plan(**kwargs)


def test_plan_pins_evidence_params() -> None:
    plan = _plan()
    merged = plan.merged_canvas_params()
    for key, value in EVIDENCE_CANVAS_PARAMS.items():
        assert merged[key] == value
    assert merged["repeatRunTime"] == 0
    assert merged["max_agent_timeout_sec"] == 604800


def test_plan_rejects_evidence_override() -> None:
    with pytest.raises(ValueError, match="retries erase evidence"):
        _plan(canvas_params={"repeatRunTime": 3})


def test_plan_agent_alias_and_mt_prefix() -> None:
    plan = _plan()
    assert plan.agent_alias == "agentix@ruanjunhao/tofu-streamfix-20260908@simple@tofu"
    assert plan.evaluate_name.startswith("MT/")


def test_mass_config_sizing() -> None:
    mass = MassConfig()
    assert mass.total_trials == 445
    assert mass.agent_workers == 64
    assert mass.gpu_cards == 0
    # ~300 核预算内（cpu_weight_convert 1002 核空闲）
    assert 250 <= mass.estimated_cpu_cores <= 400


def test_execute_submit_happy_path() -> None:
    stub = _StubCanvas()
    result = execute_submit(stub, _plan())
    assert result.owner == "ruanjunhao04"
    assert result.canvas_id != "362610"
    assert result.flow_id.startswith("flow-")
    # 序列证据：fork 在 set(donor) 之后，run 在最后
    kinds = [c[0] for c in stub.calls]
    assert kinds.index("fork") > 0  # donor set/get_params 先发生
    assert kinds[-1] == "run"


def test_execute_submit_sso_expired_fails_closed() -> None:
    stub = _StubCanvas()
    stub.logged_in = False
    with pytest.raises(RuntimeError, match="SSO session expired"):
        execute_submit(stub, _plan())
    # 探活失败必须零副作用
    assert stub.calls == []


def test_execute_submit_model_mismatch_aborts() -> None:
    stub = _StubCanvas()
    stub.model_mismatch = True
    with pytest.raises(RuntimeError, match="model binding mismatch"):
        execute_submit(stub, _plan())
    # 模型绑定错必须停在 run 之前
    assert ("run", stub._next_canvas) not in stub.calls


def test_execute_submit_anchor_readback_mismatch_aborts() -> None:
    stub = _StubCanvas()
    stub.anchor_mismatch_once = True
    with pytest.raises(RuntimeError, match="agent anchor read-back mismatch"):
        execute_submit(stub, _plan())


class _StubJobs:
    def __init__(self, script: list[object]) -> None:
        self.script = list(script)

    def status(self, flow_id: str) -> dict:
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"status": item}


def test_watch_terminal_ok() -> None:
    sleeps: list[float] = []
    result = watch_flow(
        _StubJobs(["RUNNING", "RUNNING", "DONE"]),
        "flow-1",
        interval_sec=0,
        sleep=sleeps.append,
    )
    assert result.ok
    assert result.status == "DONE"
    assert result.stop_reason == "terminal"
    assert result.polls == 3


def test_watch_terminal_bad() -> None:
    result = watch_flow(
        _StubJobs(["RUNNING", "ERROR"]), "flow-1", interval_sec=0, sleep=lambda s: None
    )
    assert not result.ok
    assert result.status == "ERROR"


def test_watch_observer_errors_stop() -> None:
    result = watch_flow(
        _StubJobs([RuntimeError("504"), RuntimeError("504"), RuntimeError("504")]),
        "flow-1",
        interval_sec=0,
        max_consecutive_errors=3,
        sleep=lambda s: None,
    )
    assert not result.ok
    assert result.stop_reason == "observer_errors"


def test_watch_deadline() -> None:
    clock = {"t": 0.0}

    def _mono() -> float:
        return clock["t"]

    def _sleep(s: float) -> None:
        clock["t"] += s

    result = watch_flow(
        _StubJobs(["RUNNING"] * 100),
        "flow-1",
        interval_sec=10,
        deadline_sec=25,
        sleep=_sleep,
        monotonic=_mono,
    )
    assert not result.ok
    assert result.stop_reason == "deadline"
