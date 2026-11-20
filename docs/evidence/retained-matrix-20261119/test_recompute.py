import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('retained_projection', Path(__file__).with_name('recompute.py'))
projection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(projection)


class RetainedProjectionTests(unittest.TestCase):
    def fixture(self, root, *, label='isolated', invalid='', extended='1', contamination='', missing=False, invalidated=False, mismatch=False, rules=True):
        row = dict(model='model', harness='pi', run='r1', task='task', attempt='1', source_run_dir='r1', invalid=invalid, in_extended_E=extended, outcome='pass', ingress_isolation=label, contamination=contamination, cost_usd='0.2', calls='3')
        directory = root / 'analysis'
        directory.mkdir()
        with (directory / 'per_trial.csv').open('w') as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            writer.writeheader()
            writer.writerow(row)
        attempt = root / 'runs/model/r1/trials/pi/task/attempt-1'
        attempt.mkdir(parents=True)
        iso = dict(container_subnet='172.17.0.0/16', vm_subnet='172.30.0.0/16', port=8080, rules=['-I INPUT -s 172.17.0.0/16 -p tcp --dport 8080 -j DROP', '-I DOCKER-USER -s 172.17.0.0/16 -d 172.30.0.0/16 -p tcp --dport 8080 -j DROP'])
        if not rules:
            iso['rules'] = []
        if not missing:
            (attempt / 'state.json').write_text(json.dumps(dict(phase='scored', reward=1)))
            (attempt / 'result.json').write_text(json.dumps(dict(task='task', arm='pi', reward=0 if mismatch else 1, ingress_isolation=iso)))
        if invalidated:
            (attempt / 'invalidated.json').write_text('{}')

    def test_eligibility_boundaries(self):
        cases = [({}, 'eligible'), ({'contamination': 'clean'}, 'eligible'), ({'label': 'pre-isolation'}, 'pre_isolation_or_unknown'), ({'invalid': 'yes'}, 'csv_invalid'), ({'extended': '0'}, 'outside_extended_E'), ({'missing': True}, 'missing_raw_metadata'), ({'invalidated': True}, 'subsequently_invalidated'), ({'mismatch': True}, 'raw_outcome_mismatch'), ({'rules': False}, 'isolation_metadata_missing'), ({'contamination': 'flag'}, 'contamination_flagged')]
        for settings, expected in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as tmp:
                self.fixture(Path(tmp), **settings)
                summary, decisions, files = projection.project(Path(tmp))
                self.assertEqual(summary['decisions'], {expected: 1})
                self.assertEqual(len(summary['cells']), int(expected == 'eligible'))
                if expected == 'eligible':
                    self.assertEqual(summary['cells'][0]['passes'], 1)
                    self.assertEqual(summary['cells'][0]['complete_89_task_runs'], 0)
                    self.assertEqual(summary['cells'][0]['recorded_list_cost_usd'], 0.2)
                self.assertIn('analysis/per_trial.csv', files)

    def test_duplicate_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            source = root / 'analysis/per_trial.csv'
            text = source.read_text()
            source.write_text(text + text.splitlines()[1] + '\n')
            summary, _, _ = projection.project(root)
            self.assertEqual(summary['decisions'], {'duplicate_identity': 2})
            self.assertEqual(summary['cells'], [])


if __name__ == '__main__':
    unittest.main()
