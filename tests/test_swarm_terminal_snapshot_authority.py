"""Terminal swarm snapshots must resolve the rendered roster, not only its header."""
from pathlib import Path
import json
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_terminal_snapshot_replaces_stale_live_roster_without_mutation():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync('frontend/src/runtime/sections/ui/streaming_swarm_panel.js', 'utf8');
const start = src.indexOf('function _buildSwarmPanelHTML(round, allRounds) {');
const end = src.indexOf('  /* Wall-clock age', start);
const body = src.slice(src.indexOf('{', start) + 1, end);
const round = {
  status: 'searching', _swarmActive: true, _asyncRunning: true,
  _swarmAgents: [{id:'review',status:'unknown'}, {id:'evidence',status:'unknown'}],
  _swarmSnapshot: {settled:true, agents:[
    {id:'review',status:'done',preview:'review result'},
    {id:'evidence',status:'done',preview:'evidence result'}]}
};
const before = JSON.stringify(round);
const context = {round,allRounds:[],_recoverSwarmAgents:r=>r._swarmSnapshot.agents};
vm.createContext(context);
vm.runInContext(body + '\nthis.result={agents,active:round._swarmActive,async:round._asyncRunning};',context);
console.log(JSON.stringify({result:context.result,immutable:before===JSON.stringify(round)}));
"""
    run = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True,
                         text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    observed = json.loads(run.stdout)
    assert [a['status'] for a in observed['result']['agents']] == ['done', 'done']
    assert [a['preview'] for a in observed['result']['agents']] == [
        'review result', 'evidence result']
    assert observed['result']['active'] is False
    assert observed['result']['async'] is False
    assert observed['immutable'] is True
