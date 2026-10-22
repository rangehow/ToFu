"""Bounded preemption acceptance never admits replay, overlap or unknown failure."""
from copy import deepcopy
from datetime import datetime
import pytest
from tofu_environments.contracts import EnvironmentError
from tofu_meituan_environments.hope_attempts import PREEMPTION_DIAGNOSTIC,select_worker_attempt

pytestmark=pytest.mark.unit

def fixture():
    spec={'recovery':{'max_attempts':3,'restart_safe':True,'allowed_reason':'scheduler_preemption'},
          'resources':{'gpu_count':1},'limits':{'gpu_seconds':180}}
    first={'podName':'pod1','containerId':'container1','status':'Failed','startTime':'2026-10-10 15:00:00',
           'finishedTime':'2026-10-10 15:00:20','diagnostics':PREEMPTION_DIAGNOSTIC}
    second={'podName':'pod2','containerId':'container2','status':'Succeeded','startTime':'2026-10-10 15:00:21',
            'finishedTime':'2026-10-10 15:01:00','diagnostics':''}
    return spec,[first,second]

def test_explicit_preemption_selects_latest_from_unordered_platform_history():
    spec,workers=fixture()
    assert select_worker_attempt(list(reversed(workers)),spec)==workers[1]

def test_default_remains_single_attempt():
    spec,workers=fixture();spec.pop('recovery')
    with pytest.raises(EnvironmentError) as error:select_worker_attempt(workers,spec)
    assert error.value.code=='hope_execution_repeated'

@pytest.mark.parametrize('change',[
    {'diagnostics':'OOMKilled'}, {'diagnostics':PREEMPTION_DIAGNOSTIC+' extra'},
    {'finishedTime':''}, {'finishedTime':'2026-10-10 15:00:30'},
    {'finishedTime':'2026-10-10 14:59:00'}, {'startTime':'unknown'}, {'status':'Succeeded'}])
def test_only_proved_closed_nonoverlapping_preemption_allowed(change):
    spec,workers=fixture();workers[0].update(change)
    with pytest.raises(EnvironmentError) as error:select_worker_attempt(workers,spec)
    assert error.value.code=='hope_preemption_unverified'

def test_duplicate_identity_and_extra_attempts_rejected():
    spec,workers=fixture()
    with pytest.raises(EnvironmentError):select_worker_attempt([workers[0],deepcopy(workers[0])],spec)
    spec['recovery']['max_attempts']=2
    with pytest.raises(EnvironmentError) as error:select_worker_attempt(workers+[deepcopy(workers[1])],spec)
    assert error.value.code=='hope_attempt_limit'

def test_total_observed_allocation_cannot_exceed_budget():
    spec,workers=fixture();spec['limits']['gpu_seconds']=58
    with pytest.raises(EnvironmentError) as error:select_worker_attempt(workers,spec)
    assert error.value.code=='hope_preemption_unverified'

def test_running_allocation_included_and_terminal_end_required():
    spec,workers=fixture();workers[1].update(status='Running',finishedTime='')
    assert select_worker_attempt(workers,spec,observed_at=datetime(2026,10,10,15,1,0))==workers[1]
    with pytest.raises(EnvironmentError):select_worker_attempt(workers,spec,observed_at=datetime(2026,10,10,15,4,0))
    workers[1]['status']='Succeeded'
    with pytest.raises(EnvironmentError):select_worker_attempt(workers,spec)
