"""Isolated UI acceptance server: production reads, temporary automation storage."""
import tempfile
from pathlib import Path
from claude_fleet.dashboard import DashboardServer
from claude_fleet.desktop import load_runtime
from claude_fleet.automations import Automations

runtime = load_runtime(None)
with tempfile.TemporaryDirectory(prefix='quotapool-ux-') as temp:
    isolated = {**runtime, 'db_path': str(Path(temp) / 'fleet.sqlite3')}
    server = DashboardServer(('127.0.0.1', 17894), isolated, None)
    server.runtime = runtime
    # Deliberately no scheduler: browser tests only invoke the no-send preview.
    server.serve_forever()
