import time
from datetime import datetime

import pytest

from claude_fleet.automations import Automations, next_run
from claude_fleet import lark_integration


@pytest.fixture
def engine(tmp_path):
    runtime = {'db_path': str(tmp_path / 'fleet.sqlite3')}
    lark_integration.save_settings(runtime, {'identity': 'user', 'recipients': [{'type': 'user', 'id': 'ou_self', 'label': '自己'}]})
    return Automations(runtime, lambda: {'services': {'coordinator': True, 'dataplane': True}, 'disks': [{'drive': 'C:', 'freeBytes': 50 * 1024**3}]})


def job(**kw):
    return dict(name='测试监控', kind='services', schedule='interval', minutes=10, at='09:00', threshold=20, content='', notify=True, recipientKeys=['user:ou_self'], enabled=True, **kw)


def test_preview_never_sends_or_changes_schedule(engine, monkeypatch):
    monkeypatch.setattr(lark_integration, 'share', lambda *a: pytest.fail('must not send'))
    saved = engine.save(job())
    before = engine.list()['jobs'][0]['nextRun']
    assert engine.run(saved['id'], preview=True)['status'] == 'preview'
    assert engine.list()['jobs'][0]['nextRun'] == before


def test_monitor_edges_and_restart(engine, monkeypatch):
    sent = []
    monkeypatch.setattr(lark_integration, 'share', lambda r, p: sent.append(p) or {'ok': True, 'sent': 1})
    saved = engine.save(job())
    engine.run(saved['id'])
    assert not sent
    engine.sample = lambda: {'services': {'coordinator': False, 'dataplane': True}}
    engine.run(saved['id'])
    engine.run(saved['id'])
    assert len(sent) == 1
    restored = Automations(engine.runtime, lambda: {'services': {'coordinator': True, 'dataplane': True}})
    restored.run(saved['id'])
    assert len(sent) == 2
    assert len(restored.list()['runs']) == 4


def test_recipient_validation(engine):
    value = job(); value['recipientKeys'] = ['user:ou_unknown']
    with pytest.raises(ValueError): engine.save(value)
    value['recipientKeys'] = []
    with pytest.raises(ValueError): engine.save(value)


def test_missing_disks_not_reported_healthy(engine):
    value = job(); value['kind'] = 'disk'; value['notify'] = False
    saved = engine.save(value)
    engine.sample = lambda: {'disks': []}
    assert engine.run(saved['id'])['status'] == 'failed'


def test_worker_exclusion(engine):
    saved = engine.save(job())
    with engine.execution_lock:
        with pytest.raises(ValueError): engine.run(saved['id'])


def test_daily_schedule():
    now = datetime(2026, 9, 6, 10).timestamp()
    assert datetime.fromtimestamp(next_run({'schedule': 'daily', 'at': '09:00'}, now)) == datetime(2026, 9, 7, 9)


def test_partial_delivery_does_not_repeat(engine, monkeypatch):
    sent = []
    monkeypatch.setattr(lark_integration, 'share', lambda r, p: sent.append(p) or {'ok': False, 'results': [{'ok': False, 'error': 'timeout'}]})
    saved = engine.save(job())
    engine.sample = lambda: {'services': {'coordinator': False}}
    assert engine.run(saved['id'])['status'] == 'failed'
    assert engine.run(saved['id'])['status'] == 'unchanged'
    assert len(sent) == 1


def test_overdue_runs_once_and_paused_skipped(engine):
    value = job(); value.update(kind='reminder', content='hello', notify=False)
    active = engine.save(value)
    value['enabled'] = False
    paused = engine.save(value)
    with engine.connect() as db:
        db.execute('UPDATE jobs SET due=?', (time.time() - 86400,))
    engine.tick()
    engine.tick()
    runs = engine.list()['runs']
    assert len(runs) == 1
    assert runs[0]['job_id'] == active['id']
    assert runs[0]['status'] == 'success'
