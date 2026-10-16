import threading
import time
import pytest
from claude_fleet.db import FleetDB, WriteBudgetExceeded


def test_writer_budget_expires_and_does_not_unlock_other_writer(tmp_path):
    db = FleetDB(str(tmp_path / 'budget.sqlite3'))
    held = threading.Event()
    release = threading.Event()
    def owner():
        with db.transaction():
            held.set()
            release.wait(2)
    thread = threading.Thread(target=owner)
    thread.start()
    assert held.wait(1)
    start = time.monotonic()
    try:
        with pytest.raises(WriteBudgetExceeded):
            with db.write_budget(.1):
                with db.transaction():
                    pytest.fail('must not acquire held writer')
        assert time.monotonic() - start < .8
    finally:
        release.set()
        thread.join(2)
    with db.transaction() as conn:
        assert conn.execute('SELECT 1').fetchone()[0] == 1


def test_file_budget_is_shared_across_database_instances(tmp_path):
    path = str(tmp_path / 'shared.sqlite3')
    first, second = FleetDB(path), FleetDB(path)
    with first.transaction():
        with pytest.raises(WriteBudgetExceeded):
            with second.write_budget(.1):
                with second.transaction():
                    pytest.fail('must not steal file lock')
    with second.transaction() as conn:
        assert conn.execute('SELECT 1').fetchone()[0] == 1


def test_budget_covers_sync_lock_and_cleans_up(tmp_path):
    db = FleetDB(str(tmp_path / 'sync.sqlite3'))
    lock = threading.Lock()
    lock.acquire()
    try:
        with pytest.raises(WriteBudgetExceeded):
            with db.write_budget(.05):
                with db.budget_lock(lock):
                    pytest.fail('held sync lock')
    finally:
        lock.release()
    assert getattr(db._write_budget, 'deadline', None) is None


def test_http_admission_returns_busy_not_no_capacity(tmp_path):
    import json
    import urllib.request
    import urllib.error
    from claude_fleet.coordinator import serve_coordinator
    server = serve_coordinator(str(tmp_path / 'http.sqlite3'), port=0, token='test')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gate = server.coordinator.route_slots
    for _ in range(4):
        assert gate.acquire(blocking=False)
    try:
        request = urllib.request.Request(
            'http://127.0.0.1:%d/v1/route/decide' % server.server_address[1],
            data=b'{"session_id":"existing","holder":"owner"}',
            headers={'Content-Type':'application/json', 'Authorization':'Bearer test'},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as caught:
            opener.open(request, timeout=2)
        assert caught.value.code == 503
        assert json.load(caught.value)['error'] == 'coordinator_busy'
    finally:
        for _ in range(4):
            gate.release()
        server.shutdown()
        server.server_close()
        thread.join(2)
