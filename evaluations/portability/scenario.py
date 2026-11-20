"""Scenario declaration for the cross-model reproducibility harness.

Responsibility: one immutable declaration per scenario benchmark — its frozen
task set, its single metric, and the two arms the harness compares on every
subject model: ``baseline`` (the bare model) and ``tofu`` (the same model inside
Tofu's mechanism for that scenario: workflow tools, staged recipe, agent loop).
A scenario is declared once and the harness runs it unchanged across model
families; nothing in a declaration may name a subject model.

Entry points: ``Scenario``, ``Arm``, ``load_scenario``, ``SCENARIO_MODULES``.
Dependencies: stdlib; ``evaluations.portability.models.ModelClient`` at run time.
Authority: docs/PORTABILITY.md ("Declaring a scenario").
"""
from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# Arm callable: (task, subject model client, private scratch dir) -> artifact (JSON-serialisable dict).
Arm = Callable[[Mapping, Any, str], dict]
# Score callable: (task, artifact, judge client or None) -> float in [0, 1], higher is better.
Score = Callable[[Mapping, dict, Any], float]

# Built-in declarations; a scenario owned elsewhere is named as "package.module:ATTRIBUTE".
SCENARIO_MODULES: dict[str, str] = {}


@dataclass(frozen=True)
class Scenario:
    id: str
    version: str
    metric: str                 # e.g. "hidden_unit_test_pass_fraction"; one metric per scenario
    tasks: tuple                # frozen task set; each a Mapping with a unique string "id"
    baseline: Arm
    tofu: Arm
    score: Score
    # A model-judged metric names ONE fixed judge for every subject model, so the
    # judge never varies with the model under test. None: deterministic scoring.
    judge_model: str | None = None
    description: str = ''
    extra_arms: Mapping[str, Arm] = field(default_factory=dict)
    comparison_version: str = '2'

    def __post_init__(self):
        ids = [task.get('id') for task in self.tasks]
        if not ids or not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError(f'scenario {self.id}: tasks need unique non-empty string ids')
        reserved = {'baseline', 'tofu'} & set(self.extra_arms)
        if reserved:
            raise ValueError(f'scenario {self.id}: extra arm names collide with {sorted(reserved)}')

    def provenance(self) -> dict:
        """Versioned comparison inputs, shared by both arms and resume checks."""
        return {'version': self.version, 'comparison_version': self.comparison_version,
                'metric': self.metric, 'judge_model': self.judge_model,
                'tasks_digest': hashlib.sha256(
                    json.dumps(self.tasks, sort_keys=True).encode()).hexdigest()}

    def arms(self) -> dict:
        return {'baseline': self.baseline, 'tofu': self.tofu, **dict(self.extra_arms)}


def load_scenario(name: str) -> Scenario:
    target = SCENARIO_MODULES.get(name, name)
    module_name, _, attribute = target.partition(':')
    if not attribute:
        raise ValueError(f'unknown scenario {name!r}; built-ins: {sorted(SCENARIO_MODULES)}; '
                         'or pass package.module:ATTRIBUTE')
    value = getattr(importlib.import_module(module_name), attribute)
    if not isinstance(value, Scenario):
        raise TypeError(f'{target} is not a Scenario')
    return value
