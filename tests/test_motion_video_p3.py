#!/usr/bin/env python3
"""tests/test_motion_video_p3.py — P3 slice: burn-in, scenes-only engine,
scene regen, scene endpoints, paper video abstract.

Provider seams are faked; the storyboard / template / verify_spec gates run
for real. The burn-in happy-path command shape is asserted via a fake
ffmpeg; a real libass render smoke is included but skipped when the static
ffmpeg or a CJK font is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time

import pytest

from lib import motion_video as mv

pytestmark = pytest.mark.unit

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句话。

2
00:00:03,000 --> 00:00:05,000
第二句话。
"""


def _make_exe(dirpath: str, name: str, body: str) -> str:
    path = os.path.join(dirpath, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _fake_ffmpeg(tmp_path):
    marker = tmp_path / 'ffmpeg_args.txt'
    cli = _make_exe(str(tmp_path), 'ffmpeg', f"""#!/bin/sh
echo "$@" > "{marker}"
for a in "$@"; do out="$a"; done
printf 'video' > "$out"
exit 0
""")
    return cli, marker


# ══════════════════════════════════════════════════════════
#  burn-in
# ══════════════════════════════════════════════════════════

def test_burn_in_command_shape(monkeypatch, tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'vv')
    srt = tmp_path / 'f.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n你好\n', encoding='utf-8')
    out = tmp_path / 'burned.mp4'
    ffmpeg, marker = _fake_ffmpeg(tmp_path)
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin', lambda: ffmpeg)
    monkeypatch.setattr('lib.motion_video._gates.probe_video',
                        lambda path, **kw: {'codec': 'h264', 'width': 1080,
                                            'height': 1440, 'fps': 30.0,
                                            'duration': 4.0,
                                            'has_audio': False})
    res = mv.burn_in_subtitles(str(video), str(srt), str(out),
                               fontsdir='/fonts')
    assert res['ok'] is True, res
    args = marker.read_text()
    assert 'subtitles=' in args
    assert 'libx264' in args and '-c:v copy' not in args  # re-encode, not copy
    assert 'fontsdir=' in args


def test_burn_in_missing_inputs(tmp_path):
    res = mv.burn_in_subtitles('/nonexistent/v.mp4', '/nonexistent/x.srt',
                               str(tmp_path / 'o.mp4'))
    assert res['ok'] is False
    assert 'missing video file' in res['detail']


def test_escape_filter_path():
    esc = mv._concat._escape_filter_path("/a/b:c/d'e.mp4") \
        if hasattr(mv, '_concat') else None
    import lib.motion_video._concat as concat_mod
    esc = concat_mod._escape_filter_path("/a/b:c/d'e.mp4")
    assert '\\:' in esc and "\\'" in esc


@pytest.mark.skipif(not mv.ffmpeg_bin(), reason='ffmpeg unavailable')
def test_burn_in_real_render(tmp_path):
    """REAL libass render: 1s black clip + one CJK subtitle → non-black pixels."""
    ffmpeg = mv.ffmpeg_bin()
    import subprocess as sp
    video = tmp_path / 'black.mp4'
    sp.run([ffmpeg, '-y', '-f', 'lavfi', '-i', 'color=black:s=320x240:d=1',
            '-pix_fmt', 'yuv420p', str(video)], check=True,
           capture_output=True, timeout=60)
    srt = tmp_path / 't.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n',
                   encoding='utf-8')
    out = tmp_path / 'burned.mp4'
    fontsdir = os.path.expanduser(
        '/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/fonts')
    res = mv.burn_in_subtitles(str(video), str(srt), str(out),
                               fontsdir=fontsdir if os.path.isdir(fontsdir) else '')
    assert res['ok'] is True, res
    assert res['duration'] == pytest.approx(1.0, abs=0.5)
    # Burned frame must differ from the pure-black source.
    before = tmp_path / 'b.png'
    after = tmp_path / 'a.png'
    sp.run([ffmpeg, '-y', '-i', str(video), '-frames:v', '1', str(before)],
           check=True, capture_output=True, timeout=60)
    sp.run([ffmpeg, '-y', '-i', str(out), '-frames:v', '1', str(after)],
           check=True, capture_output=True, timeout=60)
    assert before.read_bytes() != after.read_bytes()


# ══════════════════════════════════════════════════════════
#  engine: scenes-only + burn-in passthrough
# ══════════════════════════════════════════════════════════

def _scenes_only_task(tmp_path, scenes, **over):
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    scenes_path = tmp_path / 'scenes.json'
    scenes_path.write_text(json.dumps(scenes), encoding='utf-8')
    kw = dict(srt_path='', workdir=str(tmp_path / 'job'), voice='',
              speed=None, alignment='loose', narration=False,
              quality='draft', parallel=2, width=1080, height=1440,
              scenes_path=str(scenes_path))
    kw.update(over)
    return _new_motion_task(_motion_task_id(), **kw)


def _fake_media(monkeypatch):
    def fake_render(project_dir, output, **kw):
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'elapsed': 0.1,
                'render_time_s': 0.1, 'category': '', 'detail': ''}
    monkeypatch.setattr('lib.motion_video.render_project', fake_render)

    def fake_probe(path, **kw):
        idx = os.path.join(os.path.dirname(path), 'index.html')
        dur = 4.0
        if os.path.isfile(idx):
            m = re.search(r'data-duration="([0-9.]+)"',
                          open(idx, encoding='utf-8').read())
            if m:
                dur = float(m.group(1))
        return {'codec': 'h264', 'width': 1080, 'height': 1440, 'fps': 30.0,
                'duration': dur, 'has_audio': False}
    monkeypatch.setattr('lib.motion_video.probe_video', fake_probe)
    monkeypatch.setattr(
        'lib.motion_video.concat_mp4s',
        lambda inputs, output, **kw: _touch(output) or {
            'ok': True, 'output': output, 'duration': 8.0, 'mode': 'copy',
            'elapsed': 0.1})
    monkeypatch.setattr(
        'lib.motion_video.mux_audio_video',
        lambda v, a, output, **kw: _touch(output) or {
            'ok': True, 'output': output, 'duration': 8.0, 'elapsed': 0.1})


def _touch(path):
    with open(path, 'wb') as f:
        f.write(b'mp4')
    return None


def test_engine_scenes_only_run(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '第一句。'},
              {'id': 'scene-002', 'start': 3.0, 'end': 6.0, 'text': '第二句。'}]
    task = _scenes_only_task(tmp_path, scenes)
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    phases = [e.get('phase') for e in task['events'] if e['type'] == 'phase']
    assert 'parse' not in phases          # no SRT → no parse phase
    assert phases[0] == 'storyboard'
    assert os.path.isfile(task['result']['srt_path'])  # sidecar still built


def test_engine_scenes_only_gap_rejected(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '一。'},
              {'id': 'scene-002', 'start': 4.0, 'end': 6.0, 'text': '二。'}]
    task = _scenes_only_task(tmp_path, scenes)
    run_motion_task(task)
    assert task['status'] == 'error'
    assert 'storyboard gate' in str(task['error'])


def test_engine_burn_in_step(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_motion_task
    _fake_media(monkeypatch)
    calls = []
    monkeypatch.setattr(
        'lib.motion_video.burn_in_subtitles',
        lambda v, s, output, **kw: calls.append((v, output)) or _touch(output)
        or {'ok': True, 'output': output, 'duration': 8.0, 'elapsed': 0.1})
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '一。'}]
    task = _scenes_only_task(tmp_path, scenes, narration=False)
    task['burn_in'] = True
    run_motion_task(task)
    assert task['status'] == 'done', task.get('error')
    assert calls, 'burn_in_subtitles must be called when burn_in=True'
    assert any(e.get('phase') == 'burn_in' for e in task['events'])


# ══════════════════════════════════════════════════════════
#  scene regen worker
# ══════════════════════════════════════════════════════════

def _seed_job(tmp_path):
    job = tmp_path / 'job'
    scenes = [{'id': 'scene-001', 'start': 0.0, 'end': 3.0, 'text': '一。'},
              {'id': 'scene-002', 'start': 3.0, 'end': 6.0, 'text': '二。'}]
    (job / 'audio').mkdir(parents=True)
    (job / 'scenes.json').write_text(json.dumps(scenes), encoding='utf-8')
    for sc in scenes:
        d = job / 'scenes' / sc['id']
        d.mkdir(parents=True)
        (d / 'index.html').write_text(
            f'<div data-composition-id="main" data-duration="3.0" '
            f'data-width="1080" data-height="1440"></div>', encoding='utf-8')
        (d / f"{sc['id']}.mp4").write_bytes(b'old')
    (job / 'audio' / 'narration.wav').write_bytes(b'wav')
    (job / 'final.mp4').write_bytes(b'old-final')
    return job, scenes


def test_scene_regen_happy(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_regen_task
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    _fake_media(monkeypatch)
    job, _sc = _seed_job(tmp_path)
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=True, quality='draft', parallel=1,
                            width=1080, height=1440)
    task['scene_id'] = 'scene-002'
    task['regen_of'] = 'motion_original'
    task['narration'] = True
    run_scene_regen_task(task)
    assert task['status'] == 'done', task.get('error')
    result = task['result']
    assert result['regen_of'] == 'motion_original'
    assert result['scene_id'] == 'scene-002'
    assert (job / 'final.mp4').read_bytes() == b'mp4'  # replaced by assembly


def test_scene_regen_missing_scene_errors(monkeypatch, tmp_path):
    from lib.motion_video.engine import run_scene_regen_task
    from lib.motion_video.runtime import _new_motion_task, _motion_task_id
    _fake_media(monkeypatch)
    job, _sc = _seed_job(tmp_path)
    task = _new_motion_task(_motion_task_id(), srt_path='', workdir=str(job),
                            voice='', speed=None, alignment='loose',
                            narration=False, quality='draft', parallel=1,
                            width=1080, height=1440)
    task['scene_id'] = 'scene-999'
    run_scene_regen_task(task)
    assert task['status'] == 'error'


# ══════════════════════════════════════════════════════════
#  HTTP: scenes list / scene file / regen
# ══════════════════════════════════════════════════════════

def _done_task_with_job(flask_client, monkeypatch, tmp_path):
    job, scenes = _seed_job(tmp_path)
    sidecar = tmp_path / 'final.srt'
    sidecar.write_text('1\n00:00:00,000 --> 00:00:03,000\n一。\n',
                       encoding='utf-8')

    def fake_worker(task):
        from lib.motion_video.runtime import _motion_runtime
        task['result'] = {'final_path': str(job / 'final.mp4'),
                          'srt_path': str(sidecar), 'duration': 6.0,
                          'scenes': 2, 'narrated': False,
                          'workdir': str(job)}
        _motion_runtime.finish(task['task_id'], result=task['result'])
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task', fake_worker)
    body = flask_client.post('/api/v1/motion/videos',
                             json={'srt': SRT, 'narration': False,
                                   'parallel': 1, 'quality': 'draft'})
    tid = body.get_json()['task_id']
    t0 = time.time()
    from lib.motion_video.runtime import _motion_runtime
    while time.time() - t0 < 10:
        t = _motion_runtime.get(tid)
        if t and t['status'] == 'done':
            break
        time.sleep(0.05)
    return tid, job


def test_scenes_list_and_file(flask_client, monkeypatch, tmp_path):
    tid, job = _done_task_with_job(flask_client, monkeypatch, tmp_path)
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/scenes')
    assert r.status_code == 200
    scenes = r.get_json()['scenes']
    assert len(scenes) == 2
    assert all(s['has_video'] and s['has_composition'] for s in scenes)
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/scenes/scene-001/file')
    assert r.status_code == 200
    assert r.data == b'old'
    r = flask_client.get(f'/api/v1/motion/videos/{tid}/scenes/scene-999/file')
    assert r.status_code == 404


def test_scene_regen_endpoint(flask_client, monkeypatch, tmp_path):
    tid, job = _done_task_with_job(flask_client, monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr('lib.motion_video.engine.run_scene_regen_task',
                        lambda task: fired.append(task['scene_id']))
    r = flask_client.post(
        f'/api/v1/motion/videos/{tid}/scenes/scene-002/regen')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['regen_of'] == tid
    t0 = time.time()
    while not fired and time.time() - t0 < 5:
        time.sleep(0.05)
    assert fired == ['scene-002']


# ══════════════════════════════════════════════════════════
#  paper video abstract
# ══════════════════════════════════════════════════════════

REPORT = ('# 研究报告\n\n本文提出新方法,在 130 亿参数规模上验证。\n\n' * 30 +
         '## 消融实验\n\n成绩提升 3.2 个百分点。\n\n' * 30)


def test_build_abstract_scenes_bounds():
    scenes = mv  # placeholder removed below
    from lib.paper.video_abstract import build_abstract_scenes
    scenes = build_abstract_scenes(REPORT, max_scenes=8)
    assert 1 <= len(scenes) <= 8
    cursor = 0.0
    for sc in scenes:
        assert sc['start'] == pytest.approx(cursor)
        dur = sc['end'] - sc['start']
        assert 3.0 <= dur <= 15.0
        cursor = sc['end']
    assert mv.check_storyboard(
        scenes, (scenes[0]['start'], scenes[-1]['end'])) == []
    assert '#' not in scenes[0]['text']  # markdown stripped


def test_build_abstract_scenes_empty():
    from lib.paper.video_abstract import build_abstract_scenes
    assert build_abstract_scenes('') == []
    assert build_abstract_scenes('### \n\n```') == []


def _insert_report(phash, lang='zh'):
    from lib.database import get_thread_db
    db = get_thread_db()
    db.execute(
        'INSERT OR REPLACE INTO paper_reports (paper_hash, lang, report, model,'
        ' created_at) VALUES (?, ?, ?, ?, ?)',
        (phash, lang, REPORT, 'm', int(time.time())))
    db.commit()


def test_paper_video_start_flow(flask_client, monkeypatch, tmp_path):
    import uuid
    phash = uuid.uuid4().hex[:16]
    # no report → report_required
    r = flask_client.post('/api/v1/paper/video/start',
                          json={'paper_hash': phash, 'narration': False})
    assert r.get_json().get('report_required') is True

    _insert_report(phash)
    monkeypatch.setattr('lib.motion_video.engine.run_motion_task',
                        lambda task: None)
    r = flask_client.post('/api/v1/paper/video/start',
                          json={'paper_hash': phash, 'narration': False})
    body = r.get_json()
    assert body['ok'] is True
    assert body['task_id'].startswith('motion_')
    assert body['scenes'] >= 1
    assert body['source_kind'].startswith('report')

    r = flask_client.post('/api/v1/paper/video/start', json={})
    assert r.status_code == 400
    r = flask_client.post('/api/v1/paper/video/start', json={})
    assert r.status_code == 400


def test_paper_video_lookup(flask_client):
    import uuid
    from lib.motion_video.runtime import _new_motion_task, _motion_runtime
    phash = uuid.uuid4().hex[:16]
    # nothing yet → found False, report_available False
    r = flask_client.get(f'/api/v1/paper/video/lookup?paper_hash={phash}')
    body = r.get_json()
    assert body['ok'] and body['found'] is False
    # seed a finished motion task tagged with this paper
    task = _new_motion_task('motion_lookup1', srt_path='', workdir='/x',
                            voice='', speed=None, alignment='loose',
                            narration=False, quality='draft', parallel=1,
                            width=1080, height=1440)
    task['paper_hash'] = phash
    _motion_runtime.finish('motion_lookup1',
                           result={'final_path': '/x/final.mp4',
                                   'srt_path': '/x/final.srt', 'duration': 6.0})
    r = flask_client.get(f'/api/v1/paper/video/lookup?paper_hash={phash}')
    body = r.get_json()
    assert body['found'] is True
    assert body['running'] is False
    assert body['task_id'] == 'motion_lookup1'
    assert body['result']['final_path'] == '/x/final.mp4'
    r = flask_client.get('/api/v1/paper/video/lookup')
    assert r.status_code == 400
